"""/渠道设置输出格式 命令测试。"""
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

# 与命令层现有测试保持一致，优先复用测试进程里已加载的 SDK。
REPO_ROOT = Path(__file__).resolve().parents[4]
if "maibot_sdk" not in sys.modules:
    sdk_dir = REPO_ROOT / "maibot_sdk_tmp"
    if not sdk_dir.exists():
        pytest.skip("maibot_sdk 快照不存在，跳过命令层集成测试", allow_module_level=True)
    spec = importlib.util.spec_from_file_location(
        "maibot_sdk", sdk_dir / "__init__.py", submodule_search_locations=[str(sdk_dir)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["maibot_sdk"] = module
    spec.loader.exec_module(module)

from gemini_drawer.commands import admin_commands  # noqa: E402

IMAGES_URL = "https://welfare.example/v1/images/generations"


@pytest.fixture
def command_factory(monkeypatch):
    channels = {"grok": {"url": IMAGES_URL, "enabled": True, "stream": False, "model": "grok-imagine-image-2.0"}}
    saved = {}
    monkeypatch.setattr(admin_commands, "data_manager", SimpleNamespace(
        get_channels=lambda: channels,
        update_channel=lambda name, info: saved.update({name: info}),
    ))

    def make(text):
        cmd = admin_commands.ChannelSetResponseFormatCommand(
            message=SimpleNamespace(raw_message=text), plugin_config={},
        )
        cmd.send_text = AsyncMock(return_value=True)
        return cmd, saved

    return make


@pytest.mark.asyncio
@pytest.mark.parametrize("arg,expected", [("b64_json", "b64_json"), ("URL", "url"), ("B64_JSON", "b64_json")])
async def test_set_response_format_persists_lowercased(command_factory, arg, expected):
    cmd, saved = command_factory(f"/渠道设置输出格式 grok {arg}")
    assert await cmd.handle_admin_command() == (True, "设置成功", True)
    assert saved["grok"]["response_format"] == expected
    assert saved["grok"]["model"] == "grok-imagine-image-2.0"
    assert expected in cmd.send_text.call_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("word", ["默认", "default", "清除"])
async def test_reset_removes_override(command_factory, word):
    cmd, saved = command_factory(f"/渠道设置输出格式 grok b64_json")
    await cmd.handle_admin_command()
    assert saved["grok"]["response_format"] == "b64_json"

    cmd, saved = command_factory(f"/渠道设置输出格式 grok {word}")
    assert await cmd.handle_admin_command() == (True, "设置成功", True)
    assert "response_format" not in saved["grok"]


@pytest.mark.asyncio
async def test_unknown_format_is_rejected(command_factory):
    cmd, saved = command_factory("/渠道设置输出格式 grok base64")
    assert await cmd.handle_admin_command() == (True, "格式不支持", True)
    assert not saved


@pytest.mark.asyncio
async def test_unknown_channel_is_rejected(command_factory):
    cmd, saved = command_factory("/渠道设置输出格式 nope b64_json")
    assert await cmd.handle_admin_command() == (True, "渠道不存在", True)
    assert not saved


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["/渠道设置输出格式", "/渠道设置输出格式 grok", "/渠道设置输出格式 grok b64_json extra"])
async def test_bad_arity_shows_usage(command_factory, text):
    cmd, saved = command_factory(text)
    assert await cmd.handle_admin_command() == (True, "参数不足", True)
    assert not saved
    assert "/渠道设置输出格式" in cmd.send_text.call_args.args[0]
