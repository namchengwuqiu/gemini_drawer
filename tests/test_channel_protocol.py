"""/渠道设置协议 命令与 /渠道列表 协议标签测试。"""
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
CHAT_URL = "http://host.docker.internal:4399/v1/chat/completions"


@pytest.fixture
def command_factory(monkeypatch):
    channels = {
        "grok": {"url": IMAGES_URL, "enabled": True, "stream": False, "model": "grok-imagine-image-2.0"},
        "vid": {"url": CHAT_URL, "enabled": True, "stream": True, "model": "grok-imagine-video", "is_video": True},
        "legacy": "https://legacy.example/v1/chat/completions:sk-legacy",
    }
    saved = {}
    monkeypatch.setattr(admin_commands, "data_manager", SimpleNamespace(
        get_channels=lambda: channels,
        update_channel=lambda name, info: saved.update({name: info}),
    ))

    def make(text):
        cmd = admin_commands.ChannelSetProtocolCommand(
            message=SimpleNamespace(raw_message=text), plugin_config={},
        )
        cmd.send_text = AsyncMock(return_value=True)
        return cmd, saved

    return make


@pytest.mark.asyncio
@pytest.mark.parametrize("arg,expected", [("images_json", "images_json"), ("Doubao", "images_json"), ("gpt_image", "gpt_image")])
async def test_set_image_protocol(command_factory, arg, expected):
    cmd, saved = command_factory(f"/渠道设置协议 grok {arg}")
    assert await cmd.handle_admin_command() == (True, "设置成功", True)
    assert saved["grok"]["protocol"] == expected
    assert saved["grok"]["model"] == "grok-imagine-image-2.0"
    assert expected in cmd.send_text.call_args.args[0]


@pytest.mark.asyncio
async def test_set_video_protocol(command_factory):
    cmd, saved = command_factory("/渠道设置协议 vid seedance")
    assert await cmd.handle_admin_command() == (True, "设置成功", True)
    assert saved["vid"]["protocol"] == "seedance_video"
    assert saved["vid"]["is_video"] is True


@pytest.mark.asyncio
async def test_video_protocol_rejected_on_image_channel(command_factory):
    cmd, saved = command_factory("/渠道设置协议 grok seedance_video")
    assert await cmd.handle_admin_command() == (True, "协议类型不匹配", True)
    assert not saved


@pytest.mark.asyncio
async def test_image_protocol_rejected_on_video_channel(command_factory):
    cmd, saved = command_factory("/渠道设置协议 vid images_json")
    assert await cmd.handle_admin_command() == (True, "协议类型不匹配", True)
    assert not saved


@pytest.mark.asyncio
async def test_shared_protocol_allowed_on_both(command_factory):
    cmd, saved = command_factory("/渠道设置协议 vid openai_chat")
    assert await cmd.handle_admin_command() == (True, "设置成功", True)
    cmd, saved = command_factory("/渠道设置协议 grok openai_chat")
    assert await cmd.handle_admin_command() == (True, "设置成功", True)


@pytest.mark.asyncio
@pytest.mark.parametrize("word", ["自动", "auto", "清除"])
async def test_auto_removes_explicit_protocol(command_factory, word):
    cmd, saved = command_factory("/渠道设置协议 grok images_json")
    await cmd.handle_admin_command()
    assert saved["grok"]["protocol"] == "images_json"
    cmd, saved = command_factory(f"/渠道设置协议 grok {word}")
    assert await cmd.handle_admin_command() == (True, "设置成功", True)
    assert "protocol" not in saved["grok"]
    assert "images_json(自动)" in cmd.send_text.call_args.args[0]


@pytest.mark.asyncio
async def test_legacy_string_channel_is_upgraded(command_factory):
    cmd, saved = command_factory("/渠道设置协议 legacy openai_chat")
    assert await cmd.handle_admin_command() == (True, "设置成功", True)
    assert saved["legacy"] == {"url": "https://legacy.example/v1/chat/completions", "key": "sk-legacy", "protocol": "openai_chat"}


@pytest.mark.asyncio
async def test_unknown_protocol_is_rejected(command_factory):
    cmd, saved = command_factory("/渠道设置协议 grok nonsense")
    assert await cmd.handle_admin_command() == (True, "协议不支持", True)
    assert not saved


@pytest.mark.asyncio
async def test_unknown_channel_is_rejected(command_factory):
    cmd, saved = command_factory("/渠道设置协议 nope images_json")
    assert await cmd.handle_admin_command() == (True, "渠道不存在", True)


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["/渠道设置协议", "/渠道设置协议 grok", "/渠道设置协议 grok a b"])
async def test_bad_arity_lists_choices(command_factory, text):
    cmd, saved = command_factory(text)
    assert await cmd.handle_admin_command() == (True, "参数不足", True)
    msg = cmd.send_text.call_args.args[0]
    assert "images_json" in msg and "seedance_video" in msg


# ── /渠道列表 协议标签 ────────────────────────────────────────


@pytest.mark.parametrize("info,expected", [
    ({"url": IMAGES_URL, "model": "grok-imagine-image-2.0"}, "协议: images_json(自动)"),
    ({"url": IMAGES_URL, "model": "grok-imagine-image-2.0", "protocol": "images_json"}, "协议: images_json"),
    ({"url": IMAGES_URL, "model": "agnes-image-2.5-flash"}, "协议: agnes_image(自动)"),
    ({"url": CHAT_URL, "model": "gpt-image-2"}, "协议: gpt_image(自动)"),
    ({"url": CHAT_URL, "model": "x", "is_video": True}, "协议: openai_chat(自动)"),
    ({"url": "https://a.b/unknown", "model": "x"}, "⚠️ 协议未识别，请用 /渠道设置协议 指定"),
    ({"url": "https://a.b/unknown", "model": "x", "protocol": "gemini"}, "协议: gemini"),
    ("https://legacy.example/v1/chat/completions:sk", "协议: openai_chat(自动)"),
])
def test_channel_protocol_summary(info, expected):
    assert admin_commands.channel_protocol_summary(info) == expected
