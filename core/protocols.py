"""
渠道协议目录

渠道的请求格式由 ``protocol`` 字段显式指定，取值固定为市面上几种常见格式。
没有写 ``protocol`` 的老渠道沿用 URL / 模型名自动识别（见 providers.detect_provider
与 core.video.detect_video_protocol），/渠道列表 会显示识别结果。

本模块只放"有哪些协议、别名怎么归一"，不 import providers / video，避免循环依赖。
"""
from __future__ import annotations

from typing import Dict, Optional

#: 绘图渠道协议：id -> 说明
IMAGE_PROTOCOLS: Dict[str, str] = {
    "openai_chat": "OpenAI /chat/completions 兼容；参考图以 Data URI 放进 messages（lmarena、web2api 等中转）",
    "gpt_image": "OpenAI gpt-image 系列；按 URL 自动选 /images/* 或 /chat/completions 形态",
    "images_json": "豆包 seedream 及同协议中转；/images/generations，JSON 带 image / response_format",
    "gemini": "Google Gemini 原生 generateContent",
    "agnes_image": "Agnes Image；/images/generations，参数放在 extra_body",
}

#: 视频渠道协议：id -> 说明
VIDEO_PROTOCOLS: Dict[str, str] = {
    "openai_chat": "OpenAI /chat/completions 兼容，从回复里提取视频",
    "seedance_video": "豆包 seedance；/contents/generations/tasks 建任务 + 轮询",
    "agnes_video": "Agnes Video；/v1/videos 建任务 + 轮询",
    "gemini": "Google Gemini 原生 generateContent",
}

#: 常见别名 -> 规范 id（大小写不敏感）
PROTOCOL_ALIASES: Dict[str, str] = {
    "openai": "openai_chat",
    "openai_compat": "openai_chat",
    "chat": "openai_chat",
    "chat_completions": "openai_chat",
    "gpt-image": "gpt_image",
    "gptimage": "gpt_image",
    "openai_images": "gpt_image",
    "doubao": "images_json",
    "seedream": "images_json",
    "images": "images_json",
    "google": "gemini",
    "agnes": "agnes_image",
    "agnes_img": "agnes_image",
    "doubao_video": "seedance_video",
    "seedance": "seedance_video",
}

ALL_PROTOCOLS: Dict[str, str] = {**IMAGE_PROTOCOLS, **VIDEO_PROTOCOLS}

#: /渠道设置协议 里表示"清除显式设置、回到自动识别"的词
PROTOCOL_AUTO_WORDS = ("自动", "auto", "默认", "default", "清除", "none")


def normalize_protocol(value: Optional[str]) -> Optional[str]:
    """把用户输入 / 配置里的协议名归一成规范 id；未知返回 None。"""
    if not value:
        return None
    key = str(value).strip().lower().replace(" ", "")
    if not key:
        return None
    key = PROTOCOL_ALIASES.get(key, key)
    return key if key in ALL_PROTOCOLS else None


def is_image_protocol(protocol: Optional[str]) -> bool:
    return protocol in IMAGE_PROTOCOLS


def is_video_protocol(protocol: Optional[str]) -> bool:
    return protocol in VIDEO_PROTOCOLS


def protocol_choices_text(video: bool) -> str:
    table = VIDEO_PROTOCOLS if video else IMAGE_PROTOCOLS
    return "\n".join(f"- `{pid}`：{desc}" for pid, desc in table.items())
