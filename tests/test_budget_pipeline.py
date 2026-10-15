"""整体预算、提交状态分类与结构化错误的回归测试（P0-1 / P0-2 / P1-1 / P1-9）。"""
import base64
import json
import logging

import httpx
import pytest

from gemini_drawer.core import pipeline
from gemini_drawer.core.budget import BudgetExpiredError, TimeBudget
from gemini_drawer.providers import DrawRequest

GEMINI_URL = "https://gemini.example/v1beta/models/m:generateContent"
OPENAI_URL = "https://openai.example/v1/chat/completions"
IMG_B64 = base64.b64encode(b"generated-image-bytes" * 80).decode()


class FakeKeyManager:
    def __init__(self):
        self.calls = []

    def record_key_usage(self, key, success, force_disable=False):
        self.calls.append({"key": key, "success": success, "force_disable": force_disable})


@pytest.fixture
def fake_keys(monkeypatch):
    fake = FakeKeyManager()
    monkeypatch.setattr(pipeline, "key_manager", fake)
    return fake


@pytest.fixture
def mock_http(monkeypatch):
    state = {"handler": None, "requests": [], "client_kwargs": []}
    real_client = httpx.AsyncClient

    def factory(**kwargs):
        state["client_kwargs"].append(dict(kwargs))
        kwargs.pop("proxy", None)
        kwargs.pop("transport", None)
        return real_client(transport=httpx.MockTransport(_dispatch), **kwargs)

    def _dispatch(request):
        state["requests"].append(request)
        return state["handler"](request)

    monkeypatch.setattr(pipeline.httpx, "AsyncClient", factory)
    return state


def gemini_ok():
    return {"candidates": [{"content": {"parts": [{"inlineData": {"data": IMG_B64}}]}}]}


def endpoint(type_="custom_g", url=GEMINI_URL, key="k1", model=None, stream=False):
    return {"type": type_, "url": url, "key": key, "model": model, "stream": stream}


def second_endpoint(key="k2"):
    return endpoint(type_="custom_o", url=OPENAI_URL, key=key, model="m")


def _logger():
    return logging.getLogger("test.budget")


@pytest.mark.asyncio
async def test_budget_exhaustion_skips_remaining_endpoints(mock_http, fake_keys):
    """期限耗尽后不得开始新的生成请求（P0-1）。"""
    budget = TimeBudget(1.0, reserve_seconds=0.0)

    img, err = await pipeline.run_drawing(
        DrawRequest(prompt="p"), [endpoint()], None, _logger(), budget=budget
    )

    assert img == []
    assert pipeline.BUDGET_EXHAUSTED_MESSAGE in err
    assert mock_http["requests"] == []
    assert fake_keys.calls == []


@pytest.mark.asyncio
async def test_request_timeout_is_clamped_to_remaining_budget(mock_http, fake_keys):
    """单次请求的 timeout 收敛到剩余预算内，并给结果下载/发送留出时间（P0-1）。"""
    mock_http["handler"] = lambda r: httpx.Response(200, json=gemini_ok())
    budget = TimeBudget(100.0, reserve_seconds=0.0)

    await pipeline.run_drawing(
        DrawRequest(prompt="p"), [endpoint()], None, _logger(), budget=budget
    )

    timeout = mock_http["client_kwargs"][0]["timeout"]
    assert 60 <= timeout <= 100 - pipeline.POST_GENERATION_RESERVE_SECONDS


@pytest.mark.asyncio
async def test_uncertain_submission_stops_failover(mock_http, fake_keys):
    """读超时说明请求已发出：提交结果不确定，不得换端点重复生成（P0-2）。"""
    def handler(request):
        raise httpx.ReadTimeout("submission may have succeeded", request=request)

    mock_http["handler"] = handler

    img, err = await pipeline.run_drawing(
        DrawRequest(prompt="p"), [endpoint(key="k1"), second_endpoint()], None, _logger()
    )

    assert img == []
    assert "未自动切换其他端点" in err
    assert len(mock_http["requests"]) == 1   # 第二个端点没有被提交
    assert fake_keys.calls == []              # 不确定的失败不记 Key 失效


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,should_stop",
    [(504, False), (408, False), (502, False), (503, False), (500, False), (400, False), (404, False)],
)
async def test_gateway_errors_follow_submission_state(mock_http, fake_keys, status, should_stop):
    """HTTP 错误响应继续换端点；服务端故障不记 Key 失效。"""
    mock_http["handler"] = lambda r: httpx.Response(status, text="gateway error")

    await pipeline.run_drawing(
        DrawRequest(prompt="p"), [endpoint(key="k1"), second_endpoint()], None, _logger()
    )

    assert len(mock_http["requests"]) == (1 if should_stop else 2)
    if status in (408, 500, 502, 503, 504):
        assert fake_keys.calls == []
    else:
        assert [call["key"] for call in fake_keys.calls] == ["k1", "k2"]


@pytest.mark.asyncio
async def test_rate_limit_is_not_recorded_and_not_disabled(mock_http, fake_keys):
    """限流是可恢复冷却：不累计错误次数，也不自动禁用 Key（P1-1）。"""
    mock_http["handler"] = lambda r: httpx.Response(429, text="too many requests, retry later")

    img, err = await pipeline.run_drawing(DrawRequest(prompt="p"), [endpoint()], None, _logger())

    assert img == []
    assert "429" in err
    assert fake_keys.calls == []


@pytest.mark.asyncio
async def test_quota_429_still_force_disables(mock_http, fake_keys):
    mock_http["handler"] = lambda r: httpx.Response(
        429, text='{"error": {"message": "insufficient_quota"}}'
    )

    await pipeline.run_drawing(DrawRequest(prompt="p"), [endpoint()], None, _logger())

    assert fake_keys.calls == [{"key": "k1", "success": False, "force_disable": True}]


@pytest.mark.asyncio
async def test_text_containing_429_is_not_a_quota_error(mock_http, fake_keys):
    """正文里出现 "429" 不再被当成配额错误（P1-1）。"""
    mock_http["handler"] = lambda r: httpx.Response(
        200, json={"error": {"message": "task 429-abc not found"}}
    )

    img, err = await pipeline.run_drawing(DrawRequest(prompt="p"), [endpoint()], None, _logger())

    assert img == []
    assert "429-abc" not in err
    assert "不确定" in err
    assert fake_keys.calls == []


@pytest.mark.asyncio
async def test_oversized_result_stops_without_retrying(mock_http, fake_keys, monkeypatch):
    """超大结果给出统一错误，且不换端点重新生成（P1-8）。"""
    monkeypatch.setattr(pipeline, "MAX_RESULT_BASE64_CHARS", 100)
    mock_http["handler"] = lambda r: httpx.Response(200, json=gemini_ok())

    img, err = await pipeline.run_drawing(
        DrawRequest(prompt="p"), [endpoint(key="k1"), second_endpoint()], None, _logger()
    )

    assert img == []
    assert "过大" in err
    assert len(mock_http["requests"]) == 1


@pytest.mark.asyncio
async def test_upstream_body_is_not_in_user_message(mock_http, fake_keys):
    """用户文案只含状态码与简短原因，原始响应体只进日志（P1-9）。"""
    mock_http["handler"] = lambda r: httpx.Response(500, text="SECRET-INTERNAL-TOKEN-abcdef")

    img, err = await pipeline.run_drawing(DrawRequest(prompt="p"), [endpoint()], None, _logger())

    assert img == []
    assert "500" in err
    assert "SECRET-INTERNAL-TOKEN" not in err


@pytest.mark.asyncio
async def test_json_error_reason_is_sanitized_for_user(mock_http, fake_keys):
    """JSON 错误体里的凭据与内网地址不能进入用户文案（F5）。"""
    mock_http["handler"] = lambda r: httpx.Response(
        400, json={"error": {"message": "invalid key DUMMY-SECRET-CREDENTIAL at http://internal.local"}}
    )

    img, err = await pipeline.run_drawing(DrawRequest(prompt="p"), [endpoint()], None, _logger())

    assert img == []
    assert "400" in err
    assert "DUMMY-SECRET-CREDENTIAL" not in err
    assert "internal.local" not in err


@pytest.mark.asyncio
async def test_reserve_is_deducted_from_every_budget_check():
    """宿主返回余量在 can_start / stage_timeout / expired 全路径统一扣除（F1）。"""
    budget = TimeBudget(10.0, reserve_seconds=4.0, clock=lambda: 0.0)

    assert budget.remaining == pytest.approx(6.0)
    assert budget.can_start(minimum=5.0)
    assert not budget.can_start(reserve=2.0, minimum=5.0)
    assert not budget.expired()
    assert budget.stage_timeout(60.0) == pytest.approx(6.0)

    spent = TimeBudget(10.0, reserve_seconds=10.0, clock=lambda: 0.0)
    assert spent.expired()
    assert not spent.can_start(minimum=1.0)
    with pytest.raises(BudgetExpiredError):
        spent.stage_timeout(60.0)


@pytest.mark.asyncio
async def test_success_record_failure_does_not_failover(mock_http, monkeypatch):
    """图片已生成后记账失败：结果照常返回，绝不换端点重复生成（F7）。"""
    class ExplodingKeyManager:
        def record_key_usage(self, *args, **kwargs):
            raise RuntimeError("disk full")

    monkeypatch.setattr(pipeline, "key_manager", ExplodingKeyManager())
    mock_http["handler"] = lambda r: httpx.Response(200, json=gemini_ok())

    img, err = await pipeline.run_drawing(
        DrawRequest(prompt="p"), [endpoint(key="k1"), second_endpoint()], None, _logger()
    )

    assert img == [IMG_B64]
    assert err == ""
    assert len(mock_http["requests"]) == 1


@pytest.mark.asyncio
async def test_sse_accumulation_limit_stops_without_failover(mock_http, fake_keys, monkeypatch):
    """SSE 超限是不可重试的本地资源上限：不换端点、不记 Key 失效（F6）。"""
    monkeypatch.setattr("gemini_drawer.providers.base.MAX_SSE_ACCUMULATED_CHARS", 10)
    body = "data: " + json.dumps({"choices": [{"delta": {"content": "x" * 50}}]}) + "\n\n"

    def handler(request):
        if "openai" in str(request.url):
            return httpx.Response(
                200, content=body.encode(), headers={"Content-Type": "text/event-stream"}
            )
        return httpx.Response(200, json=gemini_ok())

    mock_http["handler"] = handler

    img, err = await pipeline.run_drawing(
        DrawRequest(prompt="p"),
        [
            endpoint(type_="custom_o", url=OPENAI_URL, key="k1", model="m", stream=True),
            endpoint(key="k2"),
        ],
        None,
        _logger(),
    )

    assert img == []
    assert "上限" in err
    assert len(mock_http["requests"]) == 1
    assert fake_keys.calls == []


@pytest.mark.asyncio
async def test_error_reason_from_json_body_is_kept_short(mock_http, fake_keys):
    """JSON 错误体只提取简短原因（截断），不整段回显。"""
    long_reason = "x" * 5000
    mock_http["handler"] = lambda r: httpx.Response(
        400, json={"error": {"message": long_reason}}
    )

    img, err = await pipeline.run_drawing(DrawRequest(prompt="p"), [endpoint()], None, _logger())

    assert img == []
    assert "400" in err
    assert len(err) < 1000


@pytest.mark.asyncio
async def test_nonstream_response_size_cap_stops_without_failover(mock_http, fake_keys, monkeypatch):
    """非流式响应在读取阶段就受字节上限约束，超限不换端点、不记 Key 失效（F6）。"""
    monkeypatch.setattr("gemini_drawer.providers.base.MAX_HTTP_RESPONSE_BYTES", 64)
    mock_http["handler"] = lambda r: httpx.Response(200, json=gemini_ok())

    img, err = await pipeline.run_drawing(
        DrawRequest(prompt="p"), [endpoint(key="k1"), second_endpoint()], None, _logger()
    )

    assert img == []
    assert "上限" in err
    assert len(mock_http["requests"]) == 1
    assert fake_keys.calls == []


@pytest.mark.asyncio
async def test_502_failover_reaches_success(mock_http, fake_keys):
    attempts = pipeline.DrawingAttempts()
    def handler(request):
        if "openai" in str(request.url):
            return httpx.Response(502)
        return httpx.Response(200, json=gemini_ok())
    mock_http["handler"] = handler
    img, err = await pipeline.run_drawing(
        DrawRequest(prompt="p"), [second_endpoint(), endpoint()], None, _logger(),
        attempts=attempts,
    )
    assert img == [IMG_B64]
    assert err == ""
    assert attempts.count == 2
    assert len(mock_http["requests"]) == 2


@pytest.mark.asyncio
async def test_attempt_count_when_budget_prevents_requests(mock_http, fake_keys):
    attempts = pipeline.DrawingAttempts()
    await pipeline.run_drawing(
        DrawRequest(prompt="p"), [endpoint()], None, _logger(),
        budget=TimeBudget(1.0), attempts=attempts,
    )
    assert attempts.count == 0
