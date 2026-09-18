"""
Gemini Drawer Provider 注册表

resolve_provider() 按 REGISTRY 顺序返回首个 matches() 命中的 provider，
**顺序有语义**：

- GptImage 必须早于 OpenAICompat —— 模型名含 gpt-image 的渠道要由 GptImage
  统一接管，再按渠道 URL 决定走 images 端点还是 chat 端点；若先匹配 OpenAI
  就会被一律当成 chat 处理，images 形态的渠道拿不到 multipart 图生图。
- AgnesImage 必须早于 Doubao —— 两者都使用 /images/generations，但请求字段不同。
- TsAi 放在最后 —— 其 URL 特征最宽松（endpoint=image 等），避免误吞其他渠道。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Type, Union

from .agnes_image import AgnesImageProvider
from .base import DrawRequest, Endpoint, HttpCall, Provider
from .doubao import DoubaoProvider
from .gemini import GeminiProvider
from .gpt_image import GptImageProvider
from .openai_compat import OpenAICompatProvider
from .tsai import TsAiProvider

REGISTRY: List[Type[Provider]] = [
    GptImageProvider,
    OpenAICompatProvider,
    AgnesImageProvider,
    DoubaoProvider,
    GeminiProvider,
    TsAiProvider,
]


def resolve_provider(endpoint: Union[Endpoint, Dict[str, Any]]) -> Optional[Provider]:
    """返回处理该端点的 provider 实例；无人认领时返回 None。"""
    if not isinstance(endpoint, Endpoint):
        endpoint = Endpoint.from_dict(endpoint)
    for provider_cls in REGISTRY:
        if provider_cls.matches(endpoint):
            return provider_cls()
    return None


__all__ = [
    "DrawRequest",
    "Endpoint",
    "HttpCall",
    "Provider",
    "REGISTRY",
    "resolve_provider",
    "AgnesImageProvider",
    "DoubaoProvider",
    "GeminiProvider",
    "GptImageProvider",
    "OpenAICompatProvider",
    "TsAiProvider",
]
