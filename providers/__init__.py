"""
Gemini Drawer Provider 注册表

resolve_provider() 按 REGISTRY 顺序返回首个 matches() 命中的 provider，
**顺序有语义**：

- GptImage 必须早于 OpenAICompat —— gpt-image 渠道的 URL 通常就是
  /v1/chat/completions，若先匹配 OpenAI 就会发到错误的端点。
- TsAi 放在最后 —— 其 URL 特征最宽松（endpoint=image 等），避免误吞其他渠道。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Type, Union

from .base import DrawRequest, Endpoint, HttpCall, Provider
from .doubao import DoubaoProvider
from .gemini import GeminiProvider
from .gpt_image import GptImageProvider
from .openai_compat import OpenAICompatProvider
from .tsai import TsAiProvider

REGISTRY: List[Type[Provider]] = [
    GptImageProvider,
    OpenAICompatProvider,
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
    "DoubaoProvider",
    "GeminiProvider",
    "GptImageProvider",
    "OpenAICompatProvider",
    "TsAiProvider",
]
