"""OpenAI gpt-image 系列：/v1/images/generations 与 /v1/images/edits。

这类模型不支持 /chat/completions，必须切到专用图片端点；
图生图走 multipart/form-data 上传原图。
"""
from __future__ import annotations

import io

from .base import DrawRequest, Endpoint, HttpCall, Provider

DEFAULT_MODEL = "gpt-image-2"
_EXT_BY_MIME = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}


class GptImageProvider(Provider):
    name = "gpt_image"
    supports_stream = False
    timeout = 180.0

    @classmethod
    def matches(cls, endpoint: Endpoint) -> bool:
        return "gpt-image" in endpoint.model_name.lower()

    def build(self, endpoint: Endpoint, request: DrawRequest) -> HttpCall:
        base = (
            endpoint.url.replace("/v1/chat/completions", "")
            .replace("/chat/completions", "")
            .rstrip("/")
        )
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
