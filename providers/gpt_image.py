"""OpenAI gpt-image 系列。

同一个协议名支持两种渠道形态，按**你配置的 URL** 自动选择：

- ``/chat/completions`` 形态：只暴露 chat 端点的中转站与本地 web2api 桥
  （例如 ``http://host:4399/v1/chat/completions`` + ``gpt-image-2``）。
  请求按 OpenAI chat 协议原样发到该地址，参考图以 Data URI 放进 messages，
  响应沿用既有的图片提取逻辑。
- ``/images/*`` 形态：OpenAI 官方及提供图片端点的中转。文生图发
  ``/v1/images/generations``；图生图走 multipart/form-data 上传原图到
  ``/v1/images/edits``。

两种形态的 URL 都会先归一成 base 再拼接，所以既能直接填图片端点，也能只填
API base，不会出现 ``/v1/images/generations/v1/images/generations`` 这类重复。
"""
from __future__ import annotations

import io

from .base import DrawRequest, Endpoint, HttpCall, Provider
from .openai_compat import build_chat_completions_call

DEFAULT_MODEL = "gpt-image-2"

#: URL 里带这个片段就用 chat 形态
_CHAT_COMPLETIONS_MARKER = "/chat/completions"

#: 归一 base 时需要剥掉的端点后缀（含 chat，兼容历史写法）
_ENDPOINT_SUFFIXES = (
    "/v1/chat/completions",
    "/chat/completions",
    "/v1/images/generations",
    "/images/generations",
    "/v1/images/edits",
    "/images/edits",
)

_EXT_BY_MIME = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}


def _images_base(url: str) -> str:
    """把配置的 URL 归一成可拼接 ``/v1/images/...`` 的 base。

    ``https://a.b/v1/images/generations`` → ``https://a.b``
    ``https://a.b/v1``                   → ``https://a.b``
    ``https://a.b``                      → ``https://a.b``
    """
    base = (url or "").rstrip("/")
    for suffix in _ENDPOINT_SUFFIXES:
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    base = base.rstrip("/")
    if base.endswith("/v1"):
        # 已经带版本段，拼接时不要再重复一次
        base = base[: -len("/v1")]
    return base


class GptImageProvider(Provider):
    name = "gpt_image"
    #: images API 不支持本插件的 SSE 解析路径；chat 形态另按渠道 stream 开关处理
    supports_stream = False
    timeout = 180.0

    @classmethod
    def matches(cls, endpoint: Endpoint) -> bool:
        return "gpt-image" in endpoint.model_name.lower()

    @staticmethod
    def uses_chat_completions(endpoint: Endpoint) -> bool:
        """渠道 URL 是 chat 端点时走 chat 形态。"""
        return _CHAT_COMPLETIONS_MARKER in (endpoint.url or "")

    def build(self, endpoint: Endpoint, request: DrawRequest) -> HttpCall:
        if self.uses_chat_completions(endpoint):
            return self._build_chat(endpoint, request)
        return self._build_images(endpoint, request)

    def _build_chat(self, endpoint: Endpoint, request: DrawRequest) -> HttpCall:
        """chat 形态：与 OpenAI 兼容协议同构，原样发到渠道配置的地址。"""
        stream = bool(endpoint.stream)
        return build_chat_completions_call(
            endpoint,
            request,
            default_model=DEFAULT_MODEL,
            stream=stream,
            timeout=self.stream_timeout if stream else self.timeout,
            use_proxy=endpoint.type != "lmarena",
        )

    def _build_images(self, endpoint: Endpoint, request: DrawRequest) -> HttpCall:
        base = _images_base(endpoint.url)
        model = endpoint.model_name or DEFAULT_MODEL

        if not request.has_image:
            return HttpCall(
                url=f"{base}/v1/images/generations",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {endpoint.key}",
                },
                json={"model": model, "prompt": request.prompt, "size": "auto"},
                timeout=self.timeout,
            )

        # 图生图：multipart 上传，不设 Content-Type，交给 httpx 生成 boundary
        mime = request.first_mime or "image/png"
        ext = _EXT_BY_MIME.get(mime, "png")
        return HttpCall(
            url=f"{base}/v1/images/edits",
            headers={"Authorization": f"Bearer {endpoint.key}"},
            files={"image": (f"input.{ext}", io.BytesIO(request.first_image), mime)},
            data={"model": model, "prompt": request.prompt, "size": "auto"},
            timeout=self.timeout,
        )
