"""渠道 URL 解析/分类与管理命令校验一致性的回归测试（P1-10）。"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gemini_drawer.commands import admin_commands
from gemini_drawer.core.channel_url import (
    KIND_AGNES_VIDEO,
    KIND_GEMINI,
    KIND_IMAGES,
    KIND_OPENAI_CHAT,
    KIND_SEEDANCE_VIDEO,
    KIND_TSAI,
    KIND_UNKNOWN,
    classify_channel_url,
    is_gemini_suffix,
    split_url_and_model,
)

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3-pro-image-preview:generateContent"


@pytest.mark.parametrize("url,expected", [
    (GEMINI_URL, KIND_GEMINI),
    ("https://api.example.com/v1/chat/completions", KIND_OPENAI_CHAT),
    ("https://ark.example/api/v3/images/generations", KIND_IMAGES),
    ("https://ark.example/api/v3/contents/generations/tasks", KIND_SEEDANCE_VIDEO),
    ("https://apihub.agnes-ai.com/v1/videos", KIND_AGNES_VIDEO),
    ("https://api.tavr.top/?endpoint=image", KIND_TSAI),
    ("https://api.tsart.lat/v1/x", KIND_TSAI),
    ("not-a-url", KIND_UNKNOWN),
    ("ftp://example.com/v1/chat/completions", KIND_UNKNOWN),
])
def test_classify_channel_url(url, expected):
    assert classify_channel_url(url) == expected


def test_gemini_suffix_check_is_path_based():
    assert is_gemini_suffix(GEMINI_URL)
    assert not is_gemini_suffix("https://example.com/generateContent/extra")


@pytest.mark.parametrize("rest,url,model", [
    (f"{GEMINI_URL}", GEMINI_URL, None),
    ("https://api.example.com/v1/chat/completions:gpt-4", "https://api.example.com/v1/chat/completions", "gpt-4"),
    ("https://api.example.com:8443/v1/chat/completions", "https://api.example.com:8443/v1/chat/completions", None),
    ("https://api.example.com:8443/v1/chat/completions:gpt-4", "https://api.example.com:8443/v1/chat/completions", "gpt-4"),
    ("https://api.tsart.lat:8443", "https://api.tsart.lat:8443", None),
    ("https://api.tavr.top/?endpoint=video_generation:default", "https://api.tavr.top/?endpoint=video_generation", "default"),
])
def test_split_url_and_model(rest, url, model):
    assert split_url_and_model(rest) == (url, model)


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
async def test_gemini_channel_with_trailing_junk_is_rejected(command_factory):
    """子串匹配曾让后缀约束失效：`:generateContent:extra` 必须被拒绝（P1-10）。"""
    cmd, channels = command_factory(f"/添加渠道 g:{GEMINI_URL}:extra")

    result = await cmd.handle_admin_command()

    assert result[1] != "添加成功"
    assert not channels


@pytest.mark.asyncio
async def test_scheme_less_url_is_rejected(command_factory):
    cmd, channels = command_factory("/添加渠道 bad:notaurl:generateContent")

    result = await cmd.handle_admin_command()

    assert result[1] != "添加成功"
    assert not channels


@pytest.mark.asyncio
async def test_openai_channel_with_port_requires_model(command_factory):
    cmd, channels = command_factory("/添加渠道 o:https://api.example.com:8443/v1/chat/completions")

    result = await cmd.handle_admin_command()

    assert result[1] == "缺少模型"
    assert not channels


@pytest.mark.asyncio
async def test_openai_channel_with_port_and_model_is_accepted(command_factory):
    cmd, channels = command_factory("/添加渠道 o:https://api.example.com:8443/v1/chat/completions:gpt-4")

    result = await cmd.handle_admin_command()

    assert result == (True, "添加成功", True)
    assert channels["o"]["url"] == "https://api.example.com:8443/v1/chat/completions"
    assert channels["o"]["model"] == "gpt-4"


def test_gemini_url_with_query_is_not_split_into_model():
    """带 query 的合法 Gemini URL 不能被拆成 URL 和模型（F10）。"""
    url = f"{GEMINI_URL}?alt=sse"
    assert split_url_and_model(url) == (url, None)
    assert classify_channel_url(url) == KIND_GEMINI
    assert is_gemini_suffix(url)

    stream_url = GEMINI_URL.replace(":generateContent", ":streamGenerateContent") + "?alt=sse#frag"
    assert split_url_and_model(stream_url) == (stream_url, None)
    assert classify_channel_url(stream_url) == KIND_GEMINI


@pytest.mark.asyncio
async def test_gemini_channel_with_query_is_accepted(command_factory):
    url = f"{GEMINI_URL}?alt=sse"
    cmd, channels = command_factory(f"/添加渠道 gq:{url}")

    result = await cmd.handle_admin_command()

    assert result == (True, "添加成功", True)
    assert channels["gq"]["url"] == url
    assert "model" not in channels["gq"]


@pytest.mark.asyncio
async def test_tsai_channel_without_model_is_accepted(command_factory):
    cmd, channels = command_factory("/添加渠道 tsai:https://api.tavr.top/?endpoint=image")

    result = await cmd.handle_admin_command()

    assert result == (True, "添加成功", True)
    assert channels["tsai"]["url"] == "https://api.tavr.top/?endpoint=image"
    assert channels["tsai"].get("is_video", False) is False
