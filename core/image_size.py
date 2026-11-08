"""图片渠道的分辨率档位及 Gemini Chat 兼容规则。"""

import re
from typing import Any, Optional

IMAGE_SIZES = ("1K", "2K", "4K")
_WEB_GEMINI_MODEL = re.compile(r"(?:gemini/)?gemini-nano-banana-2[.]1(?:-[24]k)?")
_GEMINI_IMAGE_MODEL = re.compile(r"gemini-.+-image(?:-.*)?")


def normalize_image_size(value: Any) -> Optional[str]:
    size = value.strip().upper() if isinstance(value, str) else ""
    return size if size in IMAGE_SIZES else None


def chat_image_size_mode(model: str) -> Optional[str]:
    model = model.strip()
    if _WEB_GEMINI_MODEL.fullmatch(model):
        return "model_suffix"
    if _GEMINI_IMAGE_MODEL.fullmatch(model):
        return "google_extra_body"
    return None


def web_gemini_model_for_size(model: str, size: str) -> str:
    # 网页适配器只有 2K / 4K 专用入口。选 1K 时去掉高分辨率后缀，避免硬性覆盖。
    base = re.sub(r"-[24]k$", "", model)
    return base if size == "1K" else f"{base}-{size.lower()}"
