"""Agnes 渠道添加入口与既有渠道格式的回归测试。"""
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


@pytest.fixture
def command_factory(monkeypatch):
    channels = {}
    monkeypatch.setattr(admin_commands, "data_manager", SimpleNamespace(
        add_channel=lambda name, info: channels.update({name: info}),
    ))

    def make(text):
        cmd = admin_commands.AddChannelCommand(
            message=SimpleNamespace(raw_message=text), plugin_config={},
        )
        cmd.send_text = AsyncMock(return_value=True)
        return cmd, channels

    return make


@pytest.mark.asyncio
@pytest.mark.parametrize("url,model,is_video,label", [
    ("https://apihub.agnes-ai.com/v1/images/generations", "agnes-image-2.5-flash", False, "Agnes 图片"),
    ("https://apihub.agnes-ai.com/v1/videos", "agnes-video-2.5-flash", True, "Agnes 视频"),
    ("http://proxy.example:8080/agnes/v1/videos/", "agnes-video-2.5-flash", True, "Agnes 视频"),
    ("https://proxy.example/v1/images/generations", "agnes-image-2.5-flash", False, "Agnes 图片"),
])
async def test_add_agnes_channel(command_factory, url, model, is_video, label):
    cmd, channels = command_factory(f"/添加渠道 agnes:{url}:{model}")
    assert await cmd.handle_admin_command() == (True, "添加成功", True)
    assert channels["agnes"]["url"] == url
    assert channels["agnes"]["model"] == model
    assert channels["agnes"].get("is_video", False) is is_video
    assert channels["agnes"]["enabled"] is True
    assert channels["agnes"]["stream"] is False
    message = cmd.send_text.call_args.args[0]
    assert label in message
    assert ("自动标记为视频渠道" in message) is is_video


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", [
    "/v1/videos:agnes-video-2.5-flash",
    "ftp://apihub.agnes-ai.com/v1/videos:agnes-video-2.5-flash",
    "https://apihub.agnes-ai.com/v1/videos",
    "https://apihub.agnes-ai.com/v1/videos/",
    "https://apihub.agnes-ai.com/v1/videos:",
    "https://apihub.agnes-ai.com/v1/videos:sora-2",
    "https://apihub.agnes-ai.com/v1/videos/content:agnes-video-2.5-flash",
])
async def test_invalid_agnes_channel_is_not_saved(command_factory, suffix):
    cmd, channels = command_factory(f"/添加渠道 invalid:{suffix}")
    result = await cmd.handle_admin_command()
    assert result[1] != "添加成功"
    assert not channels
    cmd.send_text.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix,url,model,is_video", [
    ("https://api.example/v1/chat/completions:gemini-3-pro", "https://api.example/v1/chat/completions", "gemini-3-pro", False),
    ("https://generativelanguage.googleapis.com/v1beta/models/gemini-3-pro-image-preview:generateContent", "https://generativelanguage.googleapis.com/v1beta/models/gemini-3-pro-image-preview:generateContent", None, False),
    ("https://ark.example/api/v3/images/generations:doubao-seedream-4-5", "https://ark.example/api/v3/images/generations", "doubao-seedream-4-5", False),
    ("https://ark.example/api/v3/contents/generations/tasks:doubao-seedance", "https://ark.example/api/v3/contents/generations/tasks", "doubao-seedance", True),
    ("https://api.tavr.top/?endpoint=image:rr3", "https://api.tavr.top/?endpoint=image", "rr3", False),
    ("https://api.tavr.top/?endpoint=video_generation:default", "https://api.tavr.top/?endpoint=video_generation", "default", True),
])
async def test_existing_channel_formats_are_unchanged(command_factory, suffix, url, model, is_video):
    cmd, channels = command_factory(f"/添加渠道 legacy:{suffix}")
    assert await cmd.handle_admin_command() == (True, "添加成功", True)
    assert channels["legacy"]["url"] == url
    assert channels["legacy"].get("model") == model
    assert channels["legacy"].get("is_video", False) is is_video
