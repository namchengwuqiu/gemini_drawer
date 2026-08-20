"""媒体 Tool 与 Replyer 的时序保护回归测试。"""

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[4]
if "maibot_sdk" not in sys.modules:
    sdk_dir = REPO_ROOT / "maibot_sdk_tmp"
    spec = importlib.util.spec_from_file_location(
        "maibot_sdk", sdk_dir / "__init__.py", submodule_search_locations=[str(sdk_dir)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["maibot_sdk"] = module
    spec.loader.exec_module(module)

from gemini_drawer.commands.actions import ImageGenerateAction  # noqa: E402
from gemini_drawer.plugin import (  # noqa: E402
    DRAW_COMMAND_TIMEOUT_MS,
    GeminiDrawerPlugin,
)


def function_call(name: str) -> dict:
    return {
        "item_type": "FunctionCallItem",
        "tool_call": {"func_name": name, "args": {}},
    }


@pytest.mark.asyncio
async def test_planner_removes_reply_when_media_tool_is_in_same_response():
    plugin = GeminiDrawerPlugin()
    items = [
        {"item_type": "AssistantMessageItem", "parts": []},
        function_call("gemini_selfie"),
        function_call("reply"),
    ]

    result = await plugin.suppress_parallel_media_reply(
        output_items=items,
        session_id="stream-1",
        item_schema_version=1,
    )

    modified = result["modified_kwargs"]
    assert modified["session_id"] == "stream-1"
    assert [
        item["tool_call"]["func_name"]
        for item in modified["output_items"]
        if item.get("item_type") == "FunctionCallItem"
    ] == ["gemini_selfie"]


@pytest.mark.asyncio
async def test_planner_keeps_reply_when_no_media_tool_is_present():
    plugin = GeminiDrawerPlugin()

    result = await plugin.suppress_parallel_media_reply(
        output_items=[function_call("query_memory"), function_call("reply")],
        session_id="stream-1",
    )

    assert result == {"action": "continue"}


@pytest.mark.asyncio
async def test_pending_guard_is_scoped_and_preserves_existing_kwargs():
    plugin = GeminiDrawerPlugin()
    plugin._begin_media_task("stream-1")

    result = await plugin.guard_pending_media_reply(
        session_id="stream-1",
        extra_prompt="原有约束",
        reply_tool_args={"reply_style": "简短"},
    )

    modified = result["modified_kwargs"]
    assert modified["reply_tool_args"] == {"reply_style": "简短"}
    assert "原有约束" in modified["extra_prompt"]
    assert "禁止声称图片/视频已经发送" in modified["extra_prompt"]

    other = await plugin.guard_pending_media_reply(
        session_id="stream-2",
        extra_prompt="原有约束",
    )
    assert other == {"action": "continue"}


def test_pending_counter_does_not_clear_early():
    plugin = GeminiDrawerPlugin()
    plugin._begin_media_task("stream-1")
    plugin._begin_media_task("stream-1")

    plugin._end_media_task("stream-1")
    assert plugin._media_is_pending("stream-1")

    plugin._end_media_task("stream-1")
    assert not plugin._media_is_pending("stream-1")


@pytest.mark.asyncio
async def test_media_tool_stays_pending_until_action_finishes(monkeypatch):
    plugin = GeminiDrawerPlugin()
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_run_action(action_cls, stream_id, **kwargs):
        assert action_cls is ImageGenerateAction
        assert kwargs["wait_message"] == "正在生成"
        assert plugin._media_is_pending(stream_id)
        started.set()
        await release.wait()
        return True, "图片已经生成并发送"

    monkeypatch.setattr(plugin, "_run_action", fake_run_action)
    monkeypatch.setattr(plugin, "_pick_wait_message", lambda kind: "正在生成")
    task = asyncio.create_task(
        plugin._run_media_tool(
            "gemini_generate_image",
            ImageGenerateAction,
            "stream-1",
            "image",
            {"prompt": "一只猫"},
        )
    )

    await started.wait()
    assert not task.done()
    assert plugin._media_is_pending("stream-1")

    release.set()
    result = await task
    assert result["success"] is True
    assert result["content"] == "图片已经生成并发送"
    assert not plugin._media_is_pending("stream-1")


@pytest.mark.asyncio
async def test_media_tool_clears_pending_after_unexpected_error(monkeypatch):
    plugin = GeminiDrawerPlugin()

    async def fail(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(plugin, "_run_action", fail)

    with pytest.raises(RuntimeError, match="boom"):
        await plugin._run_media_tool(
            "gemini_generate_image",
            ImageGenerateAction,
            "stream-1",
            "image",
            {"prompt": "一只猫"},
        )

    assert not plugin._media_is_pending("stream-1")


@pytest.mark.asyncio
async def test_planner_drops_duplicate_media_calls_in_same_response():
    """串行执行会在第一次结束时清掉 pending，同轮重复调用只能在 Planner 侧掐掉。"""
    plugin = GeminiDrawerPlugin()

    result = await plugin.suppress_parallel_media_reply(
        output_items=[
            function_call("gemini_selfie"),
            function_call("gemini_selfie"),
            function_call("gemini_generate_image"),
        ],
    )

    modified = result["modified_kwargs"]
    assert [item["tool_call"]["func_name"] for item in modified["output_items"]] == ["gemini_selfie"]


@pytest.mark.asyncio
async def test_planner_hook_falls_back_when_payload_shape_changes(caplog):
    """output_items 结构变化时必须报错而不是静默退回旧行为。"""
    plugin = GeminiDrawerPlugin()

    result = await plugin.suppress_parallel_media_reply(output_items=[object()])

    assert result == {"action": "continue"}


@pytest.mark.asyncio
async def test_duplicate_media_request_reports_failure():
    """并发去重必须返回失败，返回成功会让模型顺着说'已经发了'。"""
    plugin = GeminiDrawerPlugin()
    plugin._begin_media_task("stream-1")

    result = await plugin._run_media_tool(
        "gemini_selfie",
        ImageGenerateAction,
        "stream-1",
        "selfie",
        {"prompt": "一只猫"},
    )

    assert result["success"] is False
    assert result["error"]
    assert "没有任何内容被发送" in result["content"]


def test_wait_message_is_picked_from_config(monkeypatch):
    plugin = GeminiDrawerPlugin()
    candidates = ["提示一", "  ", "提示二"]
    monkeypatch.setattr(
        type(plugin),
        "config",
        property(lambda _self: type("Cfg", (), {"wait_notice": type("W", (), {"selfie_messages": candidates})()})()),
    )

    picked = {plugin._pick_wait_message("selfie") for _ in range(30)}
    assert picked <= {"提示一", "提示二"}
    assert picked


def test_wait_message_is_empty_when_config_unavailable():
    """配置读不到时静默降级为不发等待提示，不能让工具整体失败。"""
    assert GeminiDrawerPlugin()._pick_wait_message("selfie") == ""


def _plugin_with_cooldown(monkeypatch, seconds: int) -> GeminiDrawerPlugin:
    plugin = GeminiDrawerPlugin()
    monkeypatch.setattr(
        type(plugin),
        "config",
        property(
            lambda _self: type(
                "Cfg", (), {"behavior": type("B", (), {"media_cooldown_seconds": seconds})()}
            )()
        ),
    )
    return plugin


@pytest.mark.asyncio
async def test_second_media_call_is_blocked_during_cooldown(monkeypatch):
    """图片发完后循环不停，下一轮 Planner 常常还在处理同一条消息而再拍一张。"""
    plugin = _plugin_with_cooldown(monkeypatch, 90)

    async def ok(*args, **kwargs):
        return True, "自拍已经生成并发送"

    monkeypatch.setattr(plugin, "_run_action", ok)
    monkeypatch.setattr(plugin, "_pick_wait_message", lambda kind: "")

    first = await plugin._run_media_tool(
        "gemini_selfie", ImageGenerateAction, "stream-1", "selfie", {}
    )
    assert first["success"] is True

    second = await plugin._run_media_tool(
        "gemini_selfie", ImageGenerateAction, "stream-1", "selfie", {}
    )
    assert second["success"] is False
    assert "冷却中" in second["content"]
    assert "没有任何内容被发送" in second["content"]

    # 冷却按会话隔离，不能波及其他聊天
    other = await plugin._run_media_tool(
        "gemini_selfie", ImageGenerateAction, "stream-2", "selfie", {}
    )
    assert other["success"] is True


@pytest.mark.asyncio
async def test_failed_generation_does_not_start_cooldown(monkeypatch):
    """生成失败时用户应该能立刻重试，不该被冷却挡住。"""
    plugin = _plugin_with_cooldown(monkeypatch, 90)
    calls = []

    async def fail(*args, **kwargs):
        calls.append(1)
        return False, "自拍生成失败: 渠道超时"

    monkeypatch.setattr(plugin, "_run_action", fail)
    monkeypatch.setattr(plugin, "_pick_wait_message", lambda kind: "")

    for _ in range(2):
        result = await plugin._run_media_tool(
            "gemini_selfie", ImageGenerateAction, "stream-1", "selfie", {}
        )
        assert result["success"] is False
        assert "冷却中" not in result["content"]
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_cooldown_can_be_disabled(monkeypatch):
    plugin = _plugin_with_cooldown(monkeypatch, 0)

    async def ok(*args, **kwargs):
        return True, "自拍已经生成并发送"

    monkeypatch.setattr(plugin, "_run_action", ok)
    monkeypatch.setattr(plugin, "_pick_wait_message", lambda kind: "")

    for _ in range(3):
        result = await plugin._run_media_tool(
            "gemini_selfie", ImageGenerateAction, "stream-1", "selfie", {}
        )
        assert result["success"] is True


def test_cooldown_remaining_decays_with_time(monkeypatch):
    plugin = _plugin_with_cooldown(monkeypatch, 90)
    clock = {"now": 1000.0}
    monkeypatch.setattr("gemini_drawer.plugin.time.monotonic", lambda: clock["now"])

    plugin._mark_media_sent("stream-1")
    assert plugin._media_cooldown_remaining("stream-1") == pytest.approx(90.0)

    clock["now"] += 76.0  # 复现日志里两次自拍的实际间隔
    assert plugin._media_cooldown_remaining("stream-1") == pytest.approx(14.0)

    clock["now"] += 20.0
    assert plugin._media_cooldown_remaining("stream-1") == 0.0


def test_media_tool_descriptions_keep_anti_repeat_guidance():
    """迁移到 @Tool 时丢过一次这段约束，导致一条用户消息连发两张自拍。"""
    from maibot_sdk.components import _COMPONENT_INFO_ATTR

    for handler_name in ("handle_generate_image", "handle_selfie", "handle_selfie_video"):
        info = getattr(getattr(GeminiDrawerPlugin, handler_name), _COMPONENT_INFO_ATTR)
        text = f"{info.description}{info.detailed_description}"
        assert "不要连续" in text, handler_name


@pytest.mark.asyncio
async def test_action_execute_waits_for_generation_to_finish(monkeypatch):
    action = ImageGenerateAction()
    action.action_data = {"prompt": "一只猫"}
    started = asyncio.Event()
    release = asyncio.Event()

    monkeypatch.setattr(action, "_precheck", lambda feature: None)

    async def fake_draw_and_send(prompt):
        started.set()
        await release.wait()
        return True, "图片已经生成并发送"

    monkeypatch.setattr(action, "_draw_and_send", fake_draw_and_send)
    task = asyncio.create_task(action.execute())

    await started.wait()
    assert not task.done()
    release.set()
    assert await task == (True, "图片已经生成并发送")


@pytest.mark.asyncio
async def test_failed_send_is_not_counted_as_sent(monkeypatch):
    action = ImageGenerateAction()
    action._stream_id = "stream-1"
    monkeypatch.setattr(action, "_proxy", lambda: None)

    class Send:
        async def image(self, image_base64, stream_id, **kwargs):
            assert stream_id == "stream-1"
            assert kwargs["return_details"] is True
            return {"success": False, "sent": False, "error": "platform rejected"}

    action.ctx = type("Ctx", (), {"send": Send()})()
    assert await action._send_images(["ZmFrZQ=="]) == 0


def test_media_components_are_native_tools_with_long_timeouts():
    plugin = GeminiDrawerPlugin()
    components = {
        component["name"]: component
        for component in plugin.get_components()
        if component["name"] in plugin._MEDIA_TOOLS
    }

    assert set(components) == plugin._MEDIA_TOOLS
    assert all(component["type"] == "TOOL" for component in components.values())
    assert all(
        component["metadata"]["invoke_method"] == "plugin.invoke_tool"
        for component in components.values()
    )
    assert components["gemini_selfie"]["metadata"]["timeout_ms"] == DRAW_COMMAND_TIMEOUT_MS
