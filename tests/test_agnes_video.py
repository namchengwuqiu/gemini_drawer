"""Agnes 视频协议与现有视频下载链路的离线回归测试。"""
import asyncio
import base64
import json
import logging
from datetime import datetime, timezone
from unittest.mock import Mock

import httpx
import pytest

from gemini_drawer.core import agnes_video, video

URL = "https://apihub.agnes-ai.com/v1/videos"
MODEL = "agnes-video-2.5-flash"
VIDEO_URL = "https://cdn.example/result.mp4"
LOGGER = logging.getLogger(__name__)
B64 = base64.b64encode(b"reference-image").decode()


def endpoint(url=URL, model=MODEL):
    return {"url": url, "model": model, "key": "test-key", "type": "custom_agnes_video"}


def queued(**extra):
    return {"id": "task-1", "task_id": "task-1", "video_id": "video-1", "status": "queued", **extra}


def completed(url=VIDEO_URL):
    return {"video_id": "video-1", "status": "completed", "metadata": {"url": url}}


def completed_top_level(url=VIDEO_URL):
    """2026-09-12 实际查询响应的脱敏结构：无 metadata，地址在顶层 url。"""
    return {
        "id": "task-example", "object": "video", "status": "completed",
        "progress": 100, "internal_progress": 0, "internal_status": "pending",
        "seconds": "5", "size": "720P", "quality": "standard", "error": None,
        "url": url,
    }


@pytest.fixture
def mock_api(monkeypatch):
    state = {"requests": [], "client_kwargs": [], "sleeps": [], "handler": None}
    real_client = httpx.AsyncClient
    real_sleep = asyncio.sleep

    def dispatch(request):
        state["requests"].append(request)
        assert state["handler"] is not None, "An HTTP mock handler must be provided"
        return state["handler"](request)

    def factory(**kwargs):
        state["client_kwargs"].append(dict(kwargs))
        kwargs.pop("proxy", None)
        return real_client(transport=httpx.MockTransport(dispatch), **kwargs)

    async def fast_sleep(seconds):
        state["sleeps"].append(seconds)
        await real_sleep(0)  # 仍让出事件循环，保证取消/整体超时可以生效

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    monkeypatch.setattr(agnes_video.asyncio, "sleep", fast_sleep)
    return state


@pytest.mark.parametrize("url,model,expected", [
    (URL, MODEL, True),
    ("/v1/videos", MODEL, False),
    ("ftp://apihub.agnes-ai.com/v1/videos", MODEL, False),
    ("https://[invalid/v1/videos", MODEL, False),
    (URL + "/", MODEL, True),
    (URL, None, True),
    (URL, "  " + MODEL + "  ", True),
    ("http://proxy.example:8080/agnes/v1/videos", MODEL, True),
    ("https://proxy.example/v1/videos", None, False),
    ("https://apihub.agnes-ai.com.evil.example/v1/videos", None, False),
    (URL, "sora-2", False),
    (URL + "/video-1", MODEL, False),
    ("https://proxy.example/v1/chat/completions", MODEL, False),
    ("https://ark.example/api/v3/contents/generations/tasks", "doubao-video", False),
])
def test_agnes_video_matching(url, model, expected):
    assert agnes_video.matches_agnes_video_endpoint(endpoint(url, model)) is expected


def test_text_video_payload_defaults():
    assert agnes_video.build_agnes_video_payload(endpoint(model=None), "城市夜景", None, None) == {
        "model": MODEL, "prompt": "城市夜景", "mode": "text", "seconds": "5",
        "size": "720P", "aspect_ratio": "16:9",
    }


@pytest.mark.parametrize("image,mime,expected", [
    (B64, "image/jpeg", f"data:image/jpeg;base64,{B64}"),
    (B64, None, f"data:image/png;base64,{B64}"),
    (f"data:image/webp;base64,{B64}", "image/webp", f"data:image/webp;base64,{B64}"),
])
def test_keyframe_payload_uses_base64_without_upload(image, mime, expected):
    payload = agnes_video.build_agnes_video_payload(endpoint(), "挥手", image, mime)
    assert payload["first_frame"] == expected
    assert payload["mode"] == "keyframe"
    assert "images" not in payload
    assert "last_frame" not in payload
    assert "extra_body" not in payload


@pytest.mark.asyncio
@pytest.mark.parametrize("create_status", [200, 201, 202])
@pytest.mark.parametrize("api_url,query_path", [
    (URL, "/agnesapi"),
    ("http://proxy.example:8080/agnes/v1/videos/", "/agnes/agnesapi"),
])
async def test_create_poll_with_video_id_and_model(mock_api, create_status, api_url, query_path):
    responses = [
        httpx.Response(create_status, json=queued()),
        httpx.Response(200, json={"status": "in_progress", "metadata": {"url": "https://cdn.example/not-ready.mp4"}}),
        httpx.Response(200, json=completed()),
    ]
    mock_api["handler"] = lambda request: responses.pop(0)
    result = await agnes_video.generate_agnes_video(
        endpoint(api_url), "电影级运镜", None, None, "http://proxy.local:8080", LOGGER,
    )
    assert result == VIDEO_URL
    requests = mock_api["requests"]
    assert [r.method for r in requests] == ["POST", "GET", "GET"]
    for request in requests[1:]:
        assert request.url.path == query_path
        assert dict(request.url.params) == {"video_id": "video-1", "model_name": MODEL}
        assert request.headers["Authorization"] == "Bearer test-key"
    assert mock_api["sleeps"] == [5.0, 5.0]
    assert mock_api["client_kwargs"][0]["proxy"] == "http://proxy.local:8080"
    assert mock_api["client_kwargs"][0]["follow_redirects"] is True


@pytest.mark.asyncio
async def test_keyframe_base64_is_sent_in_actual_post(mock_api):
    responses = [httpx.Response(200, json=queued()), httpx.Response(200, json=completed())]
    mock_api["handler"] = lambda request: responses.pop(0)
    assert await agnes_video.generate_agnes_video(endpoint(), "挥手", B64, "image/jpeg", None, LOGGER) == VIDEO_URL
    payload = json.loads(mock_api["requests"][0].content)
    assert payload["first_frame"] == f"data:image/jpeg;base64,{B64}"
    assert payload["mode"] == "keyframe"
    assert len(mock_api["requests"]) == 2  # 没有图床上传或参考图下载请求
    assert dict(mock_api["requests"][1].url.params)["model_name"] == MODEL


@pytest.mark.asyncio
@pytest.mark.parametrize("result_factory", [completed, completed_top_level])
async def test_immediately_completed_create_needs_no_poll(mock_api, result_factory):
    mock_api["handler"] = lambda request: httpx.Response(200, json=result_factory())
    assert await agnes_video.generate_agnes_video(endpoint(), "p", None, None, None, LOGGER) == VIDEO_URL
    assert len(mock_api["requests"]) == 1
    assert not mock_api["sleeps"]


@pytest.mark.asyncio
async def test_task_id_is_not_used_as_video_id(mock_api):
    data = queued()
    del data["video_id"]
    mock_api["handler"] = lambda request: httpx.Response(200, json=data)
    with pytest.raises(RuntimeError, match="video_id.*id/task_id"):
        await agnes_video.generate_agnes_video(endpoint(), "p", None, None, None, LOGGER)
    assert len(mock_api["requests"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["create", "poll"])
async def test_failed_task_surfaces_error(mock_api, phase):
    failed = httpx.Response(200, json={"status": "failed", "error": {"code": "generation_failed", "message": "输入图片不合法"}})
    responses = [failed] if phase == "create" else [httpx.Response(200, json=queued()), failed]
    mock_api["handler"] = lambda request: responses.pop(0)
    with pytest.raises(RuntimeError, match="输入图片不合法"):
        await agnes_video.generate_agnes_video(endpoint(), "p", B64, "image/png", None, LOGGER)
    assert sum(r.method == "POST" for r in mock_api["requests"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("data,error", [
    (completed("file:///tmp/movie.mp4"), "HTTP"),
    ({"status": "unknown"}, "未知视频任务状态"),
])
async def test_invalid_task_results_are_rejected(mock_api, data, error):
    responses = [httpx.Response(200, json=queued()), httpx.Response(200, json=data)]
    mock_api["handler"] = lambda request: responses.pop(0)
    with pytest.raises(RuntimeError, match=error):
        await agnes_video.generate_agnes_video(endpoint(), "p", None, None, None, LOGGER)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["create", "poll"])
@pytest.mark.parametrize("status", [400, 401, 403, 404])
async def test_non_retryable_http_errors(mock_api, phase, status):
    failure = httpx.Response(status, json={"detail": "request denied"})
    responses = [failure] if phase == "create" else [httpx.Response(200, json=queued()), failure]
    mock_api["handler"] = lambda request: responses.pop(0)
    with pytest.raises(RuntimeError, match=f"HTTP {status}.*request denied"):
        await agnes_video.generate_agnes_video(endpoint(), "p", None, None, None, LOGGER)
    assert len(mock_api["requests"]) == (1 if phase == "create" else 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [408, 429, 500, 503])
async def test_transient_poll_errors_retry_same_task(mock_api, status):
    responses = [
        httpx.Response(200, json=queued()), httpx.Response(status),
        httpx.Response(200, json={"status": "in_progress"}), httpx.Response(200, json=completed()),
    ]
    mock_api["handler"] = lambda request: responses.pop(0)
    assert await agnes_video.generate_agnes_video(endpoint(), "p", None, None, None, LOGGER) == VIDEO_URL
    assert mock_api["sleeps"] == [5.0, 10.0, 10.0]
    assert sum(r.method == "POST" for r in mock_api["requests"]) == 1
    assert all(r.url.params["video_id"] == "video-1" for r in mock_api["requests"][1:])


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_after,delay", [("1", 10.0), ("10", 10.0), ("999", 999.0), ("invalid", 10.0), ("-1", 10.0), ("nan", 10.0), ("inf", 10.0)])
async def test_poll_retry_after_is_a_minimum(mock_api, retry_after, delay):
    responses = [
        httpx.Response(200, json=queued()), httpx.Response(429, headers={"Retry-After": retry_after}),
        httpx.Response(200, json=completed()),
    ]
    mock_api["handler"] = lambda request: responses.pop(0)
    await agnes_video.generate_agnes_video(endpoint(), "p", None, None, None, LOGGER)
    assert mock_api["sleeps"] == [5.0, delay]


@pytest.mark.asyncio
@pytest.mark.parametrize("error_cls", [httpx.ReadTimeout, httpx.ConnectError])
async def test_poll_network_errors_do_not_resubmit(mock_api, error_cls):
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        if count == 1:
            return httpx.Response(200, json=queued())
        if count == 2:
            raise error_cls("temporary failure", request=request)
        return httpx.Response(200, json=completed())

    mock_api["handler"] = handler
    assert await agnes_video.generate_agnes_video(endpoint(), "p", None, None, None, LOGGER) == VIDEO_URL
    assert [r.method for r in mock_api["requests"]] == ["POST", "GET", "GET"]


@pytest.mark.asyncio
async def test_create_network_timeout_is_not_retried(mock_api):
    def handler(request):
        raise httpx.ReadTimeout("ambiguous submission", request=request)

    mock_api["handler"] = handler
    with pytest.raises(agnes_video.AgnesVideoPendingError, match="提交结果不确定"):
        await agnes_video.generate_agnes_video(endpoint(), "p", None, None, None, LOGGER)
    assert len(mock_api["requests"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["create", "poll"])
async def test_total_generation_timeout(mock_api, monkeypatch, phase):
    monkeypatch.setattr(agnes_video, "GENERATION_TIMEOUT", 0.01)
    blocker = asyncio.Event()

    async def handler(request):
        if phase == "poll" and request.method == "POST":
            return httpx.Response(200, json=queued())
        await blocker.wait()
        raise AssertionError("Request should have been cancelled")

    mock_api["handler"] = handler
    with pytest.raises(RuntimeError, match="生成/轮询超时") as exc:
        await agnes_video.generate_agnes_video(endpoint(), "p", None, None, None, LOGGER)
    if phase == "poll":
        assert "video-1" in str(exc.value)
    assert sum(r.method == "POST" for r in mock_api["requests"]) == 1


@pytest.mark.asyncio
async def test_cancellation_propagates_without_new_task(mock_api):
    started = asyncio.Event()
    blocker = asyncio.Event()

    async def handler(request):
        started.set()
        await blocker.wait()
        raise AssertionError("Request should have been cancelled")

    mock_api["handler"] = handler
    task = asyncio.create_task(agnes_video.generate_agnes_video(endpoint(), "p", None, None, None, LOGGER))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(mock_api["requests"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("response,error", [
    (httpx.Response(200, text="not json"), "无效 JSON"),
    (httpx.Response(200, json=[]), "JSON 对象"),
])
async def test_malformed_responses(mock_api, response, error):
    mock_api["handler"] = lambda request: response
    with pytest.raises(RuntimeError, match=error):
        await agnes_video.generate_agnes_video(endpoint(), "p", None, None, None, LOGGER)


@pytest.mark.asyncio
@pytest.mark.parametrize("download_url,needs_auth", [
    (VIDEO_URL, False), ("https://apihub.agnes-ai.com/files/result.mp4", True),
])
@pytest.mark.parametrize("result_factory", [completed, completed_top_level])
async def test_existing_video_pipeline_downloads_result(mock_api, monkeypatch, download_url, needs_auth, result_factory):
    movie = b"fake-mp4-video"
    keys = Mock()
    monkeypatch.setattr(video, "key_manager", keys)

    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json=queued())
        if request.url.path == "/agnesapi":
            return httpx.Response(200, json=result_factory(download_url))
        assert str(request.url) == download_url
        assert ("Authorization" in request.headers) is needs_auth
        return httpx.Response(200, content=movie)

    mock_api["handler"] = handler
    result, error = await video.process_video_generation("p", B64, "image/png", [endpoint()], None, LOGGER)
    assert result == base64.b64encode(movie).decode()
    assert error == ""
    assert len(mock_api["requests"]) == 3
    keys.record_key_usage.assert_called_once_with("test-key", True)


@pytest.mark.asyncio
async def test_agnes_failure_can_fall_back_to_existing_chat_video(mock_api, monkeypatch):
    keys = Mock()
    monkeypatch.setattr(video, "key_manager", keys)
    movie = base64.b64encode(b"legacy-mp4").decode()

    def handler(request):
        if request.url.path == "/v1/videos":
            return httpx.Response(400, json={"detail": "generation rejected"})
        assert request.url.path == "/v1/chat/completions"
        return httpx.Response(200, json={"choices": [{"message": {"content": f"data:video/mp4;base64,{movie}"}}]})

    mock_api["handler"] = handler
    legacy = {"type": "custom_legacy", "url": "https://legacy.example/v1/chat/completions", "key": "legacy-key", "model": "legacy-video"}
    result, error = await video.process_video_generation("p", None, None, [endpoint(), legacy], None, LOGGER)
    assert (result, error) == (movie, "")
    assert keys.record_key_usage.call_count == 2
    keys.record_key_usage.assert_any_call("test-key", False, force_disable=False)
    keys.record_key_usage.assert_any_call("legacy-key", True)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["create", "poll"])
async def test_pending_task_does_not_switch_keys_or_channels(mock_api, monkeypatch, phase):
    monkeypatch.setattr(agnes_video, "GENERATION_TIMEOUT", 0.01)
    keys = Mock()
    monkeypatch.setattr(video, "key_manager", keys)
    blocker = asyncio.Event()

    async def handler(request):
        if phase == "create":
            raise httpx.ReadTimeout("submission may have succeeded", request=request)
        if request.method == "POST":
            return httpx.Response(200, json=queued(video_id="video-429"))
        await blocker.wait()
        raise AssertionError("Polling should have been cancelled")

    mock_api["handler"] = handler
    other = {**endpoint(), "key": "another-key"}
    result, error = await video.process_video_generation("p", None, None, [endpoint(), other], None, LOGGER)
    assert result is None
    assert "未自动切换其他渠道" in error
    assert sum(r.method == "POST" for r in mock_api["requests"]) == 1
    # video_id 含 429 也不能被旧的字符串判断误认为密钥额度不足。
    keys.record_key_usage.assert_not_called()


def test_actual_completed_response_uses_url_not_internal_status():
    response = completed_top_level()
    assert "metadata" not in response
    assert response["internal_status"] == "pending"
    assert agnes_video._task_result(response) == VIDEO_URL


@pytest.mark.parametrize("fields,expected", [
    ({"url": VIDEO_URL}, VIDEO_URL),
    ({"metadata": None, "url": VIDEO_URL}, VIDEO_URL),
    ({"metadata": {"url": ""}, "url": VIDEO_URL}, VIDEO_URL),
    ({"metadata": {"url": "file:///invalid.mp4"}, "url": VIDEO_URL}, VIDEO_URL),
    ({"metadata": {"url": VIDEO_URL}, "url": "https://cdn.example/other.mp4"}, VIDEO_URL),
])
def test_documented_and_live_result_fields(fields, expected):
    assert agnes_video._task_result({"status": "completed", **fields}) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("pending", [
    {"status": "completed", "metadata": None},
    completed(""), completed(42), completed_top_level(""),
])
async def test_completed_without_url_waits_for_same_task(mock_api, pending):
    responses = [
        httpx.Response(200, json=queued()), httpx.Response(200, json=pending),
        httpx.Response(200, json=completed_top_level()),
    ]
    mock_api["handler"] = lambda request: responses.pop(0)
    logger = Mock()
    result = await agnes_video.generate_agnes_video(endpoint(), "p", None, None, None, logger)
    assert result == VIDEO_URL
    assert [r.method for r in mock_api["requests"]] == ["POST", "GET", "GET"]
    assert all(r.url.params["video_id"] == "video-1" for r in mock_api["requests"][1:])
    logger.warning.assert_called_once()
    assert "响应字段" in logger.warning.call_args.args[0]


@pytest.mark.asyncio
async def test_missing_result_diagnostics_do_not_log_sensitive_values(mock_api):
    pending = {
        "status": "completed", "prompt": "PRIVATE-PROMPT", "first_frame": f"data:image/png;base64,{B64}",
        "metadata": {"url": None, "secret": "PRIVATE-TOKEN"},
    }
    responses = [
        httpx.Response(200, json=queued()), httpx.Response(200, json=pending),
        httpx.Response(200, json=pending), httpx.Response(200, json=completed_top_level()),
    ]
    mock_api["handler"] = lambda request: responses.pop(0)
    logger = Mock()
    await agnes_video.generate_agnes_video(endpoint(), "p", None, None, None, logger)
    logger.warning.assert_called_once()  # 不反复打印诊断
    message = logger.warning.call_args.args[0]
    assert "PRIVATE-PROMPT" not in message
    assert "PRIVATE-TOKEN" not in message
    assert B64 not in message
    assert "prompt" in message and "first_frame" in message  # 只记录字段名


@pytest.mark.asyncio
async def test_missing_completed_url_times_out_without_regenerating(mock_api, monkeypatch):
    monkeypatch.setattr(agnes_video, "GENERATION_TIMEOUT", 0.01)
    keys = Mock()
    monkeypatch.setattr(video, "key_manager", keys)
    blocker = asyncio.Event()
    polls = 0

    async def handler(request):
        nonlocal polls
        if request.method == "POST":
            return httpx.Response(200, json=queued())
        polls += 1
        if polls == 1:
            return httpx.Response(200, json={"status": "completed", "metadata": None})
        await blocker.wait()
        raise AssertionError("Polling should be cancelled")

    mock_api["handler"] = handler
    result, error = await video.process_video_generation("p", None, None, [endpoint(), endpoint()], None, LOGGER)
    assert result is None
    assert "已报告完成" in error and "一直未返回可用视频地址" in error
    assert "未自动切换其他渠道" in error
    assert sum(r.method == "POST" for r in mock_api["requests"]) == 1
    keys.record_key_usage.assert_not_called()


@pytest.mark.asyncio
async def test_invalid_completed_url_does_not_trigger_new_generation(mock_api, monkeypatch):
    keys = Mock()
    monkeypatch.setattr(video, "key_manager", keys)
    responses = [httpx.Response(200, json=queued()), httpx.Response(200, json=completed_top_level("file:///bad.mp4"))]
    mock_api["handler"] = lambda request: responses.pop(0)
    result, error = await video.process_video_generation("p", None, None, [endpoint(), endpoint()], None, LOGGER)
    assert result is None and "HTTP(S)" in error
    assert sum(r.method == "POST" for r in mock_api["requests"]) == 1
    keys.record_key_usage.assert_not_called()


def test_retry_after_http_date_is_respected(monkeypatch):
    now = datetime(2026, 9, 12, 0, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(agnes_video, "datetime", Mock(now=Mock(return_value=now)))
    response = httpx.Response(429, headers={"Retry-After": "Sat, 12 Sep 2026 00:01:00 GMT"})
    assert agnes_video._retry_delay(response, 5.0) == 60.0


def test_exponential_backoff_is_capped_without_retry_after():
    assert agnes_video._retry_delay(httpx.Response(429), 30.0) == 30.0
