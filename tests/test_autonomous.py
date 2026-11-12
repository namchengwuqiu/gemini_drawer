"""自主决策入口、参考图隔离、持久化额度及主动任务上下文的无付费回归。"""

import asyncio
import json
from copy import deepcopy
from datetime import datetime
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from gemini_drawer.config import AutonomousConfig
from gemini_drawer.core.autonomous import AutonomousQuota
from gemini_drawer.commands.actions import ImageGenerateAction, SelfieGenerateAction
from gemini_drawer.plugin import GeminiDrawerPlugin
from _components import component_metadata


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "selfie_base.jpg").write_bytes(b"fixture-reference")
    monkeypatch.setattr("gemini_drawer.plugin.__file__", str(tmp_path / "plugin.py"))
    result = GeminiDrawerPlugin()
    result.set_plugin_config(
        {
            "plugin": {"config_version": "1.16.0"},
            "autonomous": {"enabled": True},
            "selfie": {"enable": True},
        }
    )
    result._autonomous_quota = AutonomousQuota(tmp_path / "quota.json")
    result._run_media_tool = AsyncMock(
        return_value={
            "name": "gemini_autonomous_draw",
            "success": True,
            "content": "图片已经生成并发送",
        }
    )
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,cls,key",
    [
        ("self", SelfieGenerateAction, "requested_action"),
        ("free", ImageGenerateAction, "prompt"),
    ],
)
async def test_mode_routes_to_existing_pipeline(plugin, mode, cls, key):
    result = await plugin.handle_autonomous_draw(
        prompt="窗边看书", mode=mode, stream_id="s"
    )
    assert result["success"] and result["retryable"] is False
    args = plugin._run_media_tool.await_args.args
    assert args[0] == "gemini_autonomous_draw" and args[1] is cls and args[2] == "s"
    assert args[4][key] == "窗边看书" and args[4]["single_image_only"]
    if mode == "free":
        assert args[4]["use_source_image"] is False
    assert (
        json.loads(plugin._autonomous_quota.path.read_text())["streams"]["s"]["count"]
        == 1
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "disabled",
        "general",
        "plugin",
        "missing_session",
        "bad_mode",
        "empty_prompt",
        "selfie_off",
        "missing_reference",
        "blacklist",
        "admin",
        "unknown_admin",
        "pending",
        "recent_media",
        "quota_uninitialized",
    ],
)
async def test_rejections_do_not_generate_or_spend_quota(plugin, case):
    args = dict(prompt="猫", mode="free", stream_id="s")
    if case == "disabled":
        plugin.config.autonomous.enabled = False
    elif case == "general":
        plugin.config.general.enable_gemini_drawer = False
    elif case == "plugin":
        plugin.config.plugin.enabled = False
    elif case == "missing_session":
        args["stream_id"] = ""
    elif case == "bad_mode":
        args["mode"] = "auto"
    elif case == "empty_prompt":
        args["prompt"] = " "
    elif case == "selfie_off":
        args["mode"] = "self"
        plugin.config.selfie.enable = False
    elif case == "missing_reference":
        args["mode"] = "self"
        plugin.config.selfie.reference_image_path = "nonexistent-fixture.jpg"
    elif case == "blacklist":
        plugin.set_plugin_config(
            {**plugin.get_plugin_config_data(), "general": {"blacklist_groups": [123]}}
        )
        args["group_id"] = "123"
    elif case in {"admin", "unknown_admin"}:
        plugin.set_plugin_config(
            {**plugin.get_plugin_config_data(), "behavior": {"admin_only_mode": True}}
        )
        if case == "admin":
            args["user_id"] = "stranger"
    elif case == "pending":
        plugin._begin_media_task("s")
    elif case == "recent_media":
        plugin._mark_media_sent("s")
    elif case == "quota_uninitialized":
        plugin._autonomous_quota = None
    result = await plugin.handle_autonomous_draw(**args)
    assert not result["success"] and result["retryable"] is False
    plugin._run_media_tool.assert_not_called()
    if plugin._autonomous_quota:
        assert not plugin._autonomous_quota.rows


@pytest.mark.asyncio
async def test_failed_and_interrupted_generation_still_spends_quota(plugin):
    plugin._run_media_tool.return_value = {"success": False, "content": "失败"}
    assert not (
        await plugin.handle_autonomous_draw(prompt="猫", mode="free", stream_id="s")
    )["success"]
    assert not (
        await plugin.handle_autonomous_draw(prompt="猫", mode="free", stream_id="s")
    )["success"]
    assert plugin._run_media_tool.await_count == 1
    plugin._run_media_tool.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await plugin.handle_autonomous_draw(prompt="猫", mode="free", stream_id="other")
    assert plugin._autonomous_quota.rows["other"]["count"] == 1


@pytest.mark.asyncio
async def test_simultaneous_same_chat_requests_only_generate_once(plugin):
    results = await asyncio.gather(
        *(
            plugin.handle_autonomous_draw(prompt="猫", mode="free", stream_id="s")
            for _ in range(5)
        )
    )
    assert sum(bool(r["success"]) for r in results) == 1
    assert plugin._run_media_tool.await_count == 1


def timestamp(text):
    return (
        datetime.fromisoformat(text)
        .replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        .timestamp()
    )


@pytest.mark.asyncio
async def test_quota_restart_midnight_and_limits(tmp_path):
    cfg = AutonomousConfig(daily_limit=2, global_daily_limit=3)
    path = tmp_path / "quota.json"
    quota = AutonomousQuota(path)
    t = timestamp("2026-11-12T22:50:00")
    assert not await quota.reserve("s", cfg, t)
    quota = AutonomousQuota(path)
    assert "冷却" in await quota.reserve("s", cfg, t + 60)
    assert not await quota.reserve("s", cfg, t + 3600)
    assert "用完" in await quota.reserve("s", cfg, t + 3700)
    assert not await quota.reserve("other", cfg, t + 3700)
    assert "总次数" in await quota.reserve("third", cfg, t + 3700)
    midnight = timestamp("2026-11-13T00:01:00")
    assert "冷却" in await quota.reserve("s", cfg, midnight)
    assert not await quota.reserve("s", cfg, midnight + 3600)
    assert quota.rows["s"]["count"] == 1


@pytest.mark.asyncio
async def test_corrupt_and_unwritable_quota_stop_generation(tmp_path, monkeypatch):
    path = tmp_path / "quota.json"
    path.write_text("broken")
    quota = AutonomousQuota(path)
    assert await quota.reserve("s", AutonomousConfig(), 1800000000)
    path.unlink()
    quota = AutonomousQuota(path)

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr("gemini_drawer.core.autonomous._atomic_write_text", fail)
    assert "保存失败" in await quota.reserve("s", AutonomousConfig(), 1800000000)
    assert quota.error and not quota.rows


def planner_payload():
    return {
        "session_id": "s",
        "tool_definitions": [
            {
                "type": "function",
                "function": {
                    "name": "namchengwuqiu.gemini_drawer.gemini_autonomous_draw"
                },
            },
            {"type": "function", "function": {"name": "reply"}},
        ],
        "items": [
            {
                "item_type": "SystemMessageItem",
                "parts": [{"type": "text", "text": "已有的人设和主动任务"}],
            }
        ],
    }


@pytest.mark.asyncio
async def test_planner_adds_optional_choice_and_preserves_other_context(plugin):
    payload = planner_payload()
    original = deepcopy(payload)
    result = await plugin.autonomous_planner_context(**payload)
    assert payload == original
    parts = result["modified_kwargs"]["items"][0]["parts"]
    assert parts[0]["text"] == "已有的人设和主动任务"
    assert "mode=self" in parts[1]["text"] and "mode=free" in parts[1]["text"]
    assert "主动唤醒" in parts[1]["text"] and "保持沉默" in parts[1]["text"]
    await plugin.handle_autonomous_draw(prompt="猫", mode="free", stream_id="s")
    result = await plugin.autonomous_planner_context(**payload)
    assert [
        d["function"]["name"] for d in result["modified_kwargs"]["tool_definitions"]
    ] == ["reply"]


@pytest.mark.asyncio
async def test_new_media_tool_shares_same_turn_reply_and_duplicate_guard(plugin):
    def call(name):
        return {"item_type": "FunctionCallItem", "tool_call": {"func_name": name}}

    result = await plugin.suppress_parallel_media_reply(
        output_items=[
            call("namchengwuqiu.gemini_drawer.gemini_autonomous_draw"),
            call("reply"),
            call("gemini_selfie"),
        ]
    )
    assert len(result["modified_kwargs"]["output_items"]) == 1


@pytest.mark.asyncio
async def test_free_creation_never_reads_chat_picture_and_sends_one_image(monkeypatch):
    action = ImageGenerateAction()
    action.action_data = {"single_image_only": True, "use_source_image": False}
    action.action_message = object()
    action._draw = AsyncMock(return_value=(["b25l", "dHdv"], ""))
    action._send_image_data = AsyncMock(return_value=True)
    action._proxy = lambda: None
    extract = AsyncMock(side_effect=AssertionError("must not read conversation image"))
    monkeypatch.setattr("gemini_drawer.commands.actions.extract_source_image", extract)
    success, _ = await action._draw_and_send("猫")
    assert success
    extract.assert_not_called()
    assert action._draw.await_args.args[1] == []
    assert action._send_image_data.await_count == 1


def test_native_schema_has_required_mode_enum():
    schema = component_metadata("handle_autonomous_draw")["parameters_raw"]
    assert {name for name, value in schema.items() if value.get("required")} == {
        "mode",
        "prompt",
    }
    assert schema["mode"]["enum"] == ["self", "free"]


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["daily_limit", "global_daily_limit"])
async def test_zero_limit_blocks_a_new_conversation(plugin, field):
    setattr(plugin.config.autonomous, field, 0)
    result = await plugin.handle_autonomous_draw(
        prompt="猫", mode="free", stream_id="brand-new"
    )
    assert not result["success"]
    assert not plugin._autonomous_quota.rows
    plugin._run_media_tool.assert_not_called()
