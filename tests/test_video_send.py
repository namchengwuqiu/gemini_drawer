"""视频投递确认丢失不能触发第二次发送或重新生成。"""
import logging

import pytest

from gemini_drawer.core import video
from gemini_drawer.core.budget import TimeBudget

LOGGER = logging.getLogger("test.video_send")


class FakeSend:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    async def custom(self, message_type, content, stream_id, **kwargs):
        self.calls.append((message_type, content, stream_id, kwargs))
        if self.error:
            raise self.error
        return self.result


def make_ctx(send):
    return type("Ctx", (), {"send": send})()


async def _send(send, ctx=None, **overrides):
    kwargs = {
        "ctx": make_ctx(send) if ctx is None else ctx,
        "stream_id": "s1",
        "group_id": "g1",
        "user_id": None,
        "napcat_host": "napcat",
        "napcat_port": 3033,
        "logger": LOGGER,
    }
    kwargs.update(overrides)
    return await video.send_video_media("ZmFrZQ==", **kwargs)


def mock_http(monkeypatch, payload=None, status=200, error=None):
    import httpx
    calls = []

    async def request(client, method, url, **kwargs):
        calls.append((method, url, kwargs))
        if error:
            raise error
        return httpx.Response(status, json=payload)

    monkeypatch.setattr(video, "request_capped", request)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("group_id,user_id,path,target", [
    ("g1", None, "send_group_msg", "group_id"),
    (None, "u1", "send_private_msg", "user_id"),
])
async def test_video_uses_only_http_and_never_queries_full_message(monkeypatch, group_id, user_id, path, target):
    send = FakeSend(error=AssertionError("视频不应调用 SDK"))
    calls = mock_http(monkeypatch, {"status": "ok", "retcode": 0, "data": {"message_id": -123}})
    budget = TimeBudget(100, reserve_seconds=0)
    assert await _send(send, group_id=group_id, user_id=user_id, budget=budget) == (True, "")
    assert send.calls == []
    assert len(calls) == 1
    method, url, kwargs = calls[0]
    assert method == "POST" and url == f"http://napcat:3033/{path}"
    assert kwargs["json"][target] == (group_id or user_id)
    assert kwargs["json"]["message"] == [{"type": "video", "data": {"file": "base64://ZmFrZQ=="}}]
    assert budget.media_sent and not budget.media_delivery_pending


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,status", [
    ({"status": "ok", "retcode": 0}, 200),
    ({"status": "failed", "retcode": 0}, 200),
    ({"status": "ok", "retcode": 100}, 200),
    ({"status": "async", "retcode": 1}, 200),
    ({"status": "ok", "retcode": 0, "data": {"message_id": True}}, 200),
    ({"status": "ok", "retcode": 0, "data": {"message_id": 0}}, 200),
    ({}, 200), ([], 200), ({}, 503), ({}, 408),
])
async def test_ambiguous_http_response_never_repeats_send(monkeypatch, payload, status):
    calls = mock_http(monkeypatch, payload, status)
    budget = TimeBudget(100, reserve_seconds=0)
    ok, error = await _send(FakeSend(), budget=budget)
    assert not ok and isinstance(error, video.MediaDeliveryUncertain)
    assert len(calls) == 1
    assert budget.media_delivery_pending and not budget.media_sent


@pytest.mark.asyncio
@pytest.mark.parametrize("status,payload", [
    (400, {}), (401, {}),
    (200, {"status": "failed", "retcode": 100}),
])
async def test_explicit_http_failure_is_not_success(monkeypatch, status, payload):
    calls = mock_http(monkeypatch, payload, status)
    ok, error = await _send(FakeSend())
    assert not ok and not isinstance(error, video.MediaDeliveryUncertain)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_expired_budget_blocks_http_sending(monkeypatch):
    calls = mock_http(monkeypatch)
    ok, error = await _send(FakeSend(), budget=TimeBudget(0, reserve_seconds=0))
    assert not ok and "时间预算耗尽" in error
    assert calls == []


@pytest.mark.asyncio
async def test_http_timeout_is_clamped_to_remaining_budget(monkeypatch):
    called = {}
    async def direct(**kwargs):
        called.update(kwargs)
        return True, ""
    monkeypatch.setattr(video, "send_video_via_napcat", direct)
    assert await _send(FakeSend(), budget=TimeBudget(100, reserve_seconds=0)) == (True, "")
    assert 0 < called["timeout"] <= 100


@pytest.mark.asyncio
async def test_missing_ctx_still_uses_http(monkeypatch):
    calls = mock_http(monkeypatch, {"status": "ok", "retcode": 0, "data": {"message_id": 1}})
    assert await _send(None, ctx=None) == (True, "")
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_entry_deadline_preserves_pending_http_delivery(monkeypatch):
    import asyncio
    from gemini_drawer.core.budget import budgeted_entry, current_budget
    monkeypatch.setattr(TimeBudget, "can_start", lambda *args, **kwargs: True)
    cancelled = asyncio.Event()
    async def slow_request(*args, **kwargs):
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.set()
    monkeypatch.setattr(video, "request_capped", slow_request)
    class Action:
        rpc_timeout_ms = 30
        budget_reserve_seconds = 0
        @budgeted_entry(30, reserve=0)
        async def execute(self):
            return await _send(FakeSend(), budget=current_budget())
    ok, error = await Action().execute()
    assert not ok and isinstance(error, video.MediaDeliveryUncertain)
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_shared_file_keeps_large_http_request_small(monkeypatch, tmp_path):
    import base64
    import json
    from pathlib import Path
    from urllib.parse import unquote, urlparse
    import httpx

    data = b'video-test' * 350_000  # 3.5 MB，Base64 已超过服务端 2 MiB 请求限制。
    paths = []
    async def request(client, method, url, **kwargs):
        payload = kwargs['json']
        assert len(json.dumps(payload).encode()) < 1024
        uri = payload['message'][0]['data']['file']
        assert uri.startswith('file:///')
        path = Path(unquote(urlparse(uri).path))
        paths.append(path)
        assert path.read_bytes() == data
        assert path.stat().st_mode & 0o004  # 其他容器 UID 可读
        return httpx.Response(200, json={'status': 'ok', 'retcode': 0, 'data': {'message_id': 123}})
    monkeypatch.setattr(video, 'request_capped', request)
    assert await video.send_video_via_napcat(
        base64.b64encode(data).decode(), 'g1', None, 'napcat', 3033, LOGGER,
        shared_video_dir=str(tmp_path),
    ) == (True, '')
    assert len(paths) == 1 and not paths[0].exists()


@pytest.mark.asyncio
async def test_uncertain_shared_file_send_keeps_file_without_retry(monkeypatch, tmp_path):
    import httpx
    calls = mock_http(monkeypatch, error=httpx.RemoteProtocolError('ack lost'))
    ok, error = await _send(FakeSend(), shared_video_dir=str(tmp_path))
    assert not ok and isinstance(error, video.MediaDeliveryUncertain)
    assert len(calls) == 1
    files = list(tmp_path.glob('gemini-video-*.mp4'))
    assert len(files) == 1 and files[0].read_bytes() == b'fake'


@pytest.mark.asyncio
async def test_invalid_shared_directory_does_not_start_http(monkeypatch):
    calls = mock_http(monkeypatch)
    ok, message = await _send(FakeSend(), shared_video_dir='relative/path')
    assert not ok and '未尝试发送' in message
    assert calls == []


def test_shared_staging_prunes_only_own_expired_files(tmp_path):
    import os
    import time
    stale = tmp_path / 'gemini-video-old.mp4'
    foreign = tmp_path / 'other.mp4'
    recent = tmp_path / 'gemini-video-recent.mp4'
    for path in (stale, foreign, recent):
        path.write_bytes(b'old')
    before = time.time() - 90000
    os.utime(stale, (before, before))
    os.utime(foreign, (before, before))
    path = video._stage_shared_video('ZmFrZQ==', str(tmp_path))
    assert path.read_bytes() == b'fake'
    assert not stale.exists() and foreign.exists() and recent.exists()
