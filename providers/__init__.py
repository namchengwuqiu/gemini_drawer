"""
Gemini Drawer Provider 注册表

resolve_provider() 按 REGISTRY 顺序返回首个 matches() 命中的 provider，
**顺序有语义**：

- GptImage 必须早于 OpenAICompat —— 模型名含 gpt-image 的渠道要由 GptImage
  统一接管，再按渠道 URL 决定走 images 端点还是 chat 端点；若先匹配 OpenAI
  就会被一律当成 chat 处理，images 形态的渠道拿不到 multipart 图生图。
- AgnesImage 必须早于 Doubao —— 两者都使用 /images/generations，但请求字段不同。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple, Type, Union

from ..core.protocols import IMAGE_PROTOCOLS, normalize_protocol
from .agnes_image import AgnesImageProvider
from .base import DrawRequest, Endpoint, HttpCall, Provider
from .doubao import DoubaoProvider
from .gemini import GeminiProvider
from .gpt_image import GptImageProvider
from .openai_compat import OpenAICompatProvider

REGISTRY: List[Type[Provider]] = [
    GptImageProvider,
    OpenAICompatProvider,
    AgnesImageProvider,
    DoubaoProvider,
    GeminiProvider,
]


#: 协议 id -> provider 类；键必须与 core/protocols.py 的 IMAGE_PROTOCOLS 一一对应
PROTOCOL_PROVIDERS: Dict[str, Type[Provider]] = {cls.protocol: cls for cls in REGISTRY}
assert set(PROTOCOL_PROVIDERS) == set(IMAGE_PROTOCOLS), (
    f"providers 与 IMAGE_PROTOCOLS 不一致: {set(PROTOCOL_PROVIDERS) ^ set(IMAGE_PROTOCOLS)}"
)


def _as_endpoint(endpoint: Union[Endpoint, Dict[str, Any]]) -> Endpoint:
    return endpoint if isinstance(endpoint, Endpoint) else Endpoint.from_dict(endpoint)


def detect_provider(endpoint: Union[Endpoint, Dict[str, Any]]) -> Optional[Type[Provider]]:
    """按 URL / 模型名自动识别 provider 类（忽略显式 protocol）；无人认领返回 None。"""
    endpoint = _as_endpoint(endpoint)
    for provider_cls in REGISTRY:
        if provider_cls.matches(endpoint):
            return provider_cls
    return None


def describe_protocol(endpoint: Union[Endpoint, Dict[str, Any]]) -> Tuple[Optional[str], bool]:
    """返回 (协议 id, 是否显式指定)。

    显式 protocol 合法时直接采用；不合法或未指定时回退自动识别，识别不到返回 (None, False)。
    """
    endpoint = _as_endpoint(endpoint)
    explicit = normalize_protocol(endpoint.protocol)
    if explicit in PROTOCOL_PROVIDERS:
        return explicit, True
    detected = detect_provider(endpoint)
    return (detected.protocol if detected else None), False


def resolve_provider(endpoint: Union[Endpoint, Dict[str, Any]]) -> Optional[Provider]:
    """返回处理该端点的 provider 实例；无人认领时返回 None。

    渠道显式写了 protocol 就按 protocol 取，不再猜；否则按 REGISTRY 顺序自动识别。
    """
    protocol, _ = describe_protocol(endpoint)
    if protocol is None:
        return None
    return PROTOCOL_PROVIDERS[protocol]()


__all__ = [
    "DrawRequest",
    "Endpoint",
    "HttpCall",
    "Provider",
    "REGISTRY",
    "PROTOCOL_PROVIDERS",
    "describe_protocol",
    "detect_provider",
    "resolve_provider",
    "AgnesImageProvider",
    "DoubaoProvider",
    "GeminiProvider",
    "GptImageProvider",
    "OpenAICompatProvider",
]
