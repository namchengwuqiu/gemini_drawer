"""Agnes Images API：参考图和输出格式位于 JSON 的 extra_body 内。"""
from __future__ import annotations

from typing import Any, Dict
from urllib.parse import urlsplit

from .base import DrawRequest, Endpoint, HttpCall, Provider

DEFAULT_MODEL = "agnes-image-2.5-flash"


class AgnesImageProvider(Provider):
    protocol = "agnes_image"
    name = "agnes_image"
    supports_stream = False
    timeout = 180.0

    @classmethod
    def matches(cls, endpoint: Endpoint) -> bool:
        try:
            url = urlsplit(endpoint.url)
        except ValueError:
            return False
        if url.scheme not in ("https", "http") or not url.netloc:
            return False
        model = endpoint.model_name.lower()
        return url.path.rstrip("/").endswith("/images/generations") and (
            model.startswith("agnes-image-")
            or (not model and url.hostname == "apihub.agnes-ai.com")
        )

    def build(self, endpoint: Endpoint, request: DrawRequest) -> HttpCall:
        response_format = endpoint.response_format or "url"
        extra_body: Dict[str, Any] = {"response_format": response_format}
        if request.has_image:
            # 单图也必须是数组；直接发送 Data URI，不需要额外上传图片。
            extra_body["image"] = request.data_urls()

        payload = {
            "model": endpoint.model_name or DEFAULT_MODEL,
            "prompt": request.prompt,
            "size": endpoint.image_size or "2K",
            "ratio": "1:1",
            "extra_body": extra_body,
        }
        if response_format == "b64_json" and not request.has_image:
            # 文生图的 Base64 示例还使用这个顶层开关。
            payload["return_base64"] = True

        return HttpCall(
            url=endpoint.url,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {endpoint.key}",
            },
            json=payload,
            timeout=self.timeout,
        )
