"""视频发送链路：SDK 发送优先、NapCat 回退的回归测试（P2-6 / F1）。"""
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


@pytest.mark.asyncio
async def test_sdk_send_is_used_first_and_reports_success(monkeypatch):
    send = FakeSend(result={"sent": True, "message_id": "m1"})

    async def fail_napcat(*args, **kwargs):
        raise AssertionError("SDK 发送成功时不应回退 NapCat")

    monkeypatch.setattr(video, "send_video_via_napcat", fail_napcat)

    ok, error = await _send(send)

    assert (ok, error) == (True, "")
    message_type, content, stream_id, kwargs = send.calls[0]
    assert message_type == "video"
    assert content == {"binary_data_base64": "ZmFrZQ=="}
    assert stream_id == "s1"
    assert kwargs["return_details"] is True


@pytest.mark.asyncio
async def test_failed_sdk_send_falls_back_to_napcat(monkeypatch):
    send = FakeSend(result={"sent": False, "message_id": None})
    called = {}

    async def fake_napcat(**kwargs):
        called.update(kwargs)
        return True, ""

    monkeypatch.setattr(video, "send_video_via_napcat", fake_napcat)

    ok, error = await _send(send)

    assert (ok, error) == (True, "")
    assert called["group_id"] == "g1"


@pytest.mark.asyncio
async def test_sdk_unclassified_exception_does_not_repeat_send(monkeypatch):
    send = FakeSend(error=RuntimeError("unclassified RPC failure"))

    async def fake_napcat(**kwargs):
        raise AssertionError("未知 SDK 异常不能触发再次发送")

    monkeypatch.setattr(video, "send_video_via_napcat", fake_napcat)

    ok, error = await _send(send, group_id=None, user_id="u1")

    assert not ok and "状态不确定" in error


@pytest.mark.asyncio
async def test_missing_ctx_goes_straight_to_napcat(monkeypatch):
    async def fake_napcat(**kwargs):
        return True, ""

    monkeypatch.setattr(video, "send_video_via_napcat", fake_napcat)

    ok, _ = await _send(None, ctx=None)

    assert ok is True


@pytest.mark.asyncio
async def test_expired_budget_blocks_sending(monkeypatch):
    """预算耗尽后不得再开始发送，也不回退 NapCat（F1）。"""
    send = FakeSend(result={"sent": True, "message_id": "m1"})
    called = {}

    async def fake_napcat(**kwargs):
        called.update(kwargs)
        return True, ""

    monkeypatch.setattr(video, "send_video_via_napcat", fake_napcat)
    clock = {"now": 1000.0}
    budget = TimeBudget(1.0, reserve_seconds=0.0, clock=lambda: clock["now"])
    clock["now"] += 2.0  # 预算已耗尽

    ok, error = await _send(send, budget=budget)

    assert ok is False
    assert send.calls == []
    assert called == {}
    assert "时间预算耗尽" in error


@pytest.mark.asyncio
async def test_napcat_timeout_is_clamped_to_remaining_budget(monkeypatch):
    """NapCat 回退的 300 秒 timeout 收敛到剩余预算内（F1）。"""
    send = FakeSend(result={"sent": False, "message_id": None})
    called = {}

    async def fake_napcat(**kwargs):
        called.update(kwargs)
        return True, ""

    monkeypatch.setattr(video, "send_video_via_napcat", fake_napcat)
    budget = TimeBudget(100.0, reserve_seconds=0.0)

    ok, _ = await _send(send, budget=budget)

    assert ok is True
    assert 5.0 <= called["timeout"] <= 100.0
