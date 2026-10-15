"""插件后台任务与媒体冷却表的生命周期回归测试（P1-5 / P1-6）。"""
import asyncio
import logging
from types import SimpleNamespace

import pytest

from gemini_drawer import plugin as plugin_module
from gemini_drawer.plugin import GeminiDrawerPlugin


def make_plugin() -> GeminiDrawerPlugin:
    plugin = GeminiDrawerPlugin()
    plugin._ctx = SimpleNamespace(logger=logging.getLogger("test.lifecycle"))
    return plugin


@pytest.mark.asyncio
async def test_tracked_task_is_removed_after_completion():
    plugin = make_plugin()

    task = plugin.track_background_task(asyncio.sleep(0, result=None))
    assert task in plugin._background_tasks

    await task
    await asyncio.sleep(0)  # 让 done callback 运行

    assert task not in plugin._background_tasks


@pytest.mark.asyncio
async def test_unload_cancels_pending_background_tasks():
    plugin = make_plugin()
    task = plugin.track_background_task(asyncio.sleep(30))
    await asyncio.sleep(0)

    await plugin.on_unload()

    assert task.cancelled()
    assert plugin._background_tasks == set()


@pytest.mark.asyncio
async def test_task_exception_is_consumed_not_raised():
    plugin = make_plugin()

    async def boom():
        raise RuntimeError("后台任务失败")

    task = plugin.track_background_task(boom())
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert task.done()
    assert task not in plugin._background_tasks


def test_cooldown_entries_expire_and_are_capped(monkeypatch):
    import time

    plugin = make_plugin()
    monkeypatch.setattr(plugin, "_media_cooldown_seconds", lambda: 60.0)

    # 过期条目在下次记账时被回收
    plugin._media_last_sent_at["stale"] = time.monotonic() - 120
    plugin._mark_media_sent("fresh")
    assert "stale" not in plugin._media_last_sent_at
    assert "fresh" in plugin._media_last_sent_at

    # 表大小有上限，不会随会话数无限增长
    monkeypatch.setattr(plugin, "_media_cooldown_seconds", lambda: 10_000_000.0)
    now = time.monotonic()
    plugin._media_last_sent_at = {f"session-{i}": now for i in range(plugin_module._MEDIA_COOLDOWN_MAX_ENTRIES + 20)}
    plugin._mark_media_sent("newest")
    assert len(plugin._media_last_sent_at) <= plugin_module._MEDIA_COOLDOWN_MAX_ENTRIES + 1


def test_download_policy_is_applied_from_config(monkeypatch):
    """配置里的下载地址策略会应用到 utils.net（P1-7）。"""
    from gemini_drawer.utils import net

    net.reset_policy()
    try:
        plugin = make_plugin()
        behavior = SimpleNamespace(
            allow_private_media_hosts=True,
            media_host_allowlist=["host.docker.internal"],
        )
        monkeypatch.setattr(
            type(plugin), "config", property(lambda self: SimpleNamespace(behavior=behavior))
        )

        plugin._apply_download_policy()

        policy = net.current_policy()
        assert policy.allow_private_hosts is True
        assert policy.allows_host("host.docker.internal")
    finally:
        net.reset_policy()


def test_cooldown_disabled_keeps_table_empty(monkeypatch):
    plugin = make_plugin()
    monkeypatch.setattr(plugin, "_media_cooldown_seconds", lambda: 0.0)

    plugin._mark_media_sent("s1")

    assert plugin._media_last_sent_at == {}
    assert plugin._media_cooldown_remaining("s1") == 0.0
