"""Agnes Images API：参考图和输出格式位于 JSON 的 extra_body 内。"""
from __future__ import annotations

from typing import Any, Dict
from urllib.parse import urlsplit

from .base import DrawRequest, Endpoint, HttpCall, Provider

DEFAULT_MODEL = "agnes-image-2.5-flash"


class AgnesImageProvider(Provider):
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
        extra_body: Dict[str, Any] = {"response_format": "url"}
        if request.has_image:
            # 单图也必须是数组；直接发送 Data URI，不需要额外上传图片。
            extra_body["image"] = request.data_urls()

        return HttpCall(
            url=endpoint.url,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {endpoint.key}",
            },
            json={
                "model": endpoint.model_name or DEFAULT_MODEL,
                "prompt": request.prompt,
                "size": "2K",
                "ratio": "1:1",
                "extra_body": extra_body,
            },
            timeout=self.timeout,
        )
