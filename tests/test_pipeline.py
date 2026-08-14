"""绘图管线的端到端测试：用 httpx.MockTransport 假造各家 API 的响应。

覆盖的是重构最容易出错的部分——多端点失败转移、Key 健康度记账、
SSE 累积解析、以及 lmarena 的特例（不记账、不走代理）。
"""
import base64
import json

import httpx
import pytest

from gemini_drawer.core import pipeline
from gemini_drawer.providers import DrawRequest

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 32
IMG_B64 = base64.b64encode(b"generated-image-bytes" * 80).decode()

GEMINI_URL = "https://gemini.example/v1beta/models/m:generateContent"
OPENAI_URL = "https://openai.example/v1/chat/completions"
DOUBAO_URL = "https://doubao.example/api/v3/images/generations"


class FakeKeyManager:
    """记录 record_key_usage 的调用，替代真实的 keys.json 读写。"""

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
    """把 pipeline 内部新建的 AsyncClient 换成挂了 MockTransport 的客户端。"""
    state = {"handler": None, "requests": [], "client_kwargs": []}

    real_client = httpx.AsyncClient

    def factory(**kwargs):
        state["client_kwargs"].append(dict(kwargs))  # 先留档，下面会改动 kwargs
        kwargs.pop("proxy", None)
        return real_client(transport=httpx.MockTransport(_dispatch), **kwargs)

    def _dispatch(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        return state["handler"](request)

    monkeypatch.setattr(pipeline.httpx, "AsyncClient", factory)
    return state


def gemini_image_response():
    return {"candidates": [{"content": {"parts": [{"inlineData": {"data": IMG_B64}}]}}]}


def openai_image_response():
    return {"choices": [{"message": {"content": f"![img](data:image/png;base64,{IMG_B64})"}}]}


def endpoint(type_="custom_g", url=GEMINI_URL, key="k1", model=None, stream=False):
    return {"type": type_, "url": url, "key": key, "model": model, "stream": stream}


@pytest.mark.asyncio
async def test_first_endpoint_success_records_key(mock_http, fake_keys):
    mock_http["handler"] = lambda r: httpx.Response(200, json=gemini_image_response())

    img, err = await pipeline.run_drawing(
        DrawRequest(prompt="一只猫"), [endpoint()], None, _logger()
    )

    assert img == [IMG_B64]
    assert err == ""
    assert fake_keys.calls == [{"key": "k1", "success": True, "force_disable": False}]


@pytest.mark.asyncio
async def test_falls_over_to_second_endpoint(mock_http, fake_keys):
    def handler(request):
        if "bad.example" in str(request.url):
            return httpx.Response(500, text="upstream boom")
        return httpx.Response(200, json=openai_image_response())

    mock_http["handler"] = handler
    endpoints = [
        endpoint(type_="custom_bad", url="https://bad.example/v1/chat/completions", key="k-bad", model="m"),
        endpoint(type_="custom_ok", url=OPENAI_URL, key="k-good", model="m"),
    ]

    img, err = await pipeline.run_drawing(DrawRequest(prompt="p"), endpoints, None, _logger())

    assert img == [IMG_B64]
    assert err == ""
    assert fake_keys.calls == [
        {"key": "k-bad", "success": False, "force_disable": False},
        {"key": "k-good", "success": True, "force_disable": False},
    ]


@pytest.mark.asyncio
async def test_429_force_disables_key(mock_http, fake_keys):
    mock_http["handler"] = lambda r: httpx.Response(429, text="quota exceeded")

    img, err = await pipeline.run_drawing(DrawRequest(prompt="p"), [endpoint()], None, _logger())

    assert img == []
    assert "429" in err
    assert fake_keys.calls == [{"key": "k1", "success": False, "force_disable": True}]


@pytest.mark.asyncio
async def test_all_endpoints_fail_returns_last_error(mock_http, fake_keys):
    mock_http["handler"] = lambda r: httpx.Response(503, text="down")

    img, err = await pipeline.run_drawing(
        DrawRequest(prompt="p"), [endpoint(key="a"), endpoint(key="b")], None, _logger()
    )

    assert img == []
    assert "503" in err
    assert [c["key"] for c in fake_keys.calls] == ["a", "b"]


@pytest.mark.asyncio
async def test_unrecognized_endpoint_is_skipped_not_attempted(mock_http, fake_keys):
    mock_http["handler"] = lambda r: httpx.Response(200, json=gemini_image_response())

    img, err = await pipeline.run_drawing(
        DrawRequest(prompt="p"),
        [endpoint(type_="custom_weird", url="https://a.b/unknown-shape")],
        None,
        _logger(),
    )

    assert img == []
    assert "没有可用的绘图端点" in err
    assert mock_http["requests"] == []   # 没发出任何请求
    assert fake_keys.calls == []          # 也没冤枉任何 Key


@pytest.mark.asyncio
async def test_api_200_without_image_surfaces_model_reason(mock_http, fake_keys):
    """模型拒绝作图时应把原因透传给用户，而不是一句笼统的失败。"""
    mock_http["handler"] = lambda r: httpx.Response(
        200,
        json={"promptFeedback": {"blockReason": "SAFETY"}},
    )

    img, err = await pipeline.run_drawing(DrawRequest(prompt="p"), [endpoint()], None, _logger())

    assert img == []
    assert "SAFETY" in err
    assert fake_keys.calls == [{"key": "k1", "success": False, "force_disable": False}]


@pytest.mark.asyncio
async def test_sse_stream_accumulates_before_extracting(mock_http, fake_keys):
    """半截 base64 分布在多个 chunk 里，必须等流结束再提取，否则图片会被截断。"""
    half = len(IMG_B64) // 2
    chunks = [
        "![img](data:image/png;base64,",
        IMG_B64[:half],
        IMG_B64[half:],
        ")",
    ]
    body = "".join(
        f"data: {json.dumps({'choices': [{'delta': {'content': c}}]})}\n\n" for c in chunks
    ) + "data: [DONE]\n\n"

    mock_http["handler"] = lambda r: httpx.Response(
        200, content=body.encode(), headers={"Content-Type": "text/event-stream"}
    )

    img, err = await pipeline.run_drawing(
        DrawRequest(prompt="p"),
        [endpoint(type_="custom_o", url=OPENAI_URL, key="k1", model="m", stream=True)],
        None,
        _logger(),
    )

    assert img == [IMG_B64]          # 完整，未被截断
    assert err == ""


@pytest.mark.asyncio
async def test_lmarena_is_not_key_accounted_and_bypasses_proxy(mock_http, fake_keys):
    mock_http["handler"] = lambda r: httpx.Response(200, json=openai_image_response())

    img, _ = await pipeline.run_drawing(
        DrawRequest(prompt="p"),
        [{"type": "lmarena", "url": "http://127.0.0.1:666/v1/chat/completions", "key": ""}],
        "http://127.0.0.1:7890",
        _logger(),
    )

    assert img == [IMG_B64]
    assert fake_keys.calls == []                              # 无 Key 中转不参与记账
    assert mock_http["client_kwargs"][0]["proxy"] is None     # 且不走代理


@pytest.mark.asyncio
async def test_normal_endpoint_uses_configured_proxy(mock_http, fake_keys):
    mock_http["handler"] = lambda r: httpx.Response(200, json=gemini_image_response())

    await pipeline.run_drawing(
        DrawRequest(prompt="p"), [endpoint()], "http://127.0.0.1:7890", _logger()
    )

    assert mock_http["client_kwargs"][0]["proxy"] == "http://127.0.0.1:7890"


@pytest.mark.asyncio
async def test_image_request_reaches_provider_payload(mock_http, fake_keys):
    mock_http["handler"] = lambda r: httpx.Response(200, json=gemini_image_response())

    await pipeline.run_drawing(
        DrawRequest(prompt="换个风格", images=[PNG], mime_types=["image/png"]),
        [endpoint()],
        None,
        _logger(),
    )

    sent = json.loads(mock_http["requests"][0].content)
    parts = sent["contents"][0]["parts"]
    assert parts[0]["inline_data"]["data"] == base64.b64encode(PNG).decode()
    assert parts[1]["text"] == "换个风格"


@pytest.mark.asyncio
async def test_doubao_url_response_is_returned_as_url(mock_http, fake_keys):
    mock_http["handler"] = lambda r: httpx.Response(
        200, json={"data": [{"url": "https://cdn.example/out.png"}]}
    )

    img, _ = await pipeline.run_drawing(
        DrawRequest(prompt="p"),
        [endpoint(type_="custom_d", url=DOUBAO_URL, key="ark", model="doubao-x")],
        None,
        _logger(),
    )

    assert img == ["https://cdn.example/out.png"]


def _logger():
    import logging

    return logging.getLogger("test.pipeline")
