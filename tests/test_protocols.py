"""协议目录、视频协议识别与 /渠道列表 协议标签测试。"""
import pytest

from gemini_drawer.core import video
from gemini_drawer.core.protocols import (
    ALL_PROTOCOLS, IMAGE_PROTOCOLS, PROTOCOL_ALIASES, VIDEO_PROTOCOLS, normalize_protocol,
)


@pytest.mark.parametrize("raw,expected", [
    ("openai_chat", "openai_chat"), ("OpenAI", "openai_chat"), (" chat ", "openai_chat"),
    ("doubao", "images_json"), ("Seedream", "images_json"), ("gpt-image", "gpt_image"),
    ("google", "gemini"), ("agnes", "agnes_image"), ("seedance", "seedance_video"),
    ("doubao_video", "seedance_video"), ("tsai_video", "tsai_video"),
    ("nonsense", None), ("", None), (None, None), ("   ", None),
])
def test_normalize_protocol(raw, expected):
    assert normalize_protocol(raw) == expected


def test_aliases_point_to_known_protocols():
    assert set(PROTOCOL_ALIASES.values()) <= set(ALL_PROTOCOLS)
    assert not (set(PROTOCOL_ALIASES) & set(ALL_PROTOCOLS)), "别名不能与规范 id 重名"


def test_shared_protocol_ids_between_image_and_video():
    # openai_chat 与 gemini 两边都能用，其余各自独立
    assert set(IMAGE_PROTOCOLS) & set(VIDEO_PROTOCOLS) == {"openai_chat", "gemini"}


# ── 视频协议识别 ──────────────────────────────────────────────


@pytest.mark.parametrize("url,model,expected", [
    ("https://apihub.agnes-ai.com/v1/videos", "agnes-video-2.5-flash", "agnes_video"),
    ("https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks", "doubao-seedance", "seedance_video"),
    ("https://relay.example/contents/generations/tasks", "", "seedance_video"),
    ("http://host.docker.internal:4399/v1/chat/completions", "grok-imagine-video", "openai_chat"),
    ("https://api.tavr.top/?endpoint=video_generation", "default", "tsai_video"),
    ("https://linux.tsart.lat/api", "", "tsai_video"),
    ("https://generativelanguage.googleapis.com/v1beta/models/veo:generateContent", "", "gemini"),
    ("https://a.b/unknown", "", None),
])
def test_detect_video_protocol(url, model, expected):
    assert video.detect_video_protocol({"url": url, "model": model}) == expected


def test_describe_video_protocol_prefers_explicit_and_falls_back_on_bad_value():
    ep = {"url": "https://a.b/unknown", "model": "", "protocol": "seedance"}
    assert video.describe_video_protocol(ep) == ("seedance_video", True)
    ep = {"url": "http://x/v1/chat/completions", "protocol": "images_json"}  # 绘图协议不能用于视频
    assert video.describe_video_protocol(ep) == ("openai_chat", False)
    ep = {"url": "http://x/v1/chat/completions", "protocol": "nonsense"}
    assert video.describe_video_protocol(ep) == ("openai_chat", False)
