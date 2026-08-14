"""火山豆包 /images/generations 图片生成协议。"""
from __future__ import annotations

from .base import DrawRequest, Endpoint, HttpCall, Provider

DEFAULT_MODEL = "doubao-seedream-4-5-251128"


class DoubaoProvider(Provider):
    name = "doubao"
    supports_stream = False  # 豆包图片接口不走本插件的 SSE 解析路径

    @classmethod
    def matches(cls, endpoint: Endpoint) -> bool:
        return "/images/generations" in endpoint.url

    def build(self, endpoint: Endpoint, request: DrawRequest) -> HttpCall:
        payload = {
            "model": endpoint.model_name or DEFAULT_MODEL,
            "prompt": request.prompt,
            "response_format": "url",
            "size": "2k",
            "stream": False,
            "watermark": False,
        }

        if request.has_image:
            # 单图传字符串、多图传列表，与豆包 API 的入参约定一致
            urls = request.data_urls()
            payload["image"] = urls[0] if len(urls) == 1 else urls

        return HttpCall(
            url=endpoint.url,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {endpoint.key}",
            },
            json=payload,
            timeout=self.timeout,
        )
