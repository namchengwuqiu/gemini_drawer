"""Google Gemini 原生 generateContent 协议。"""
from __future__ import annotations

from typing import Any, Dict, List

from .base import DrawRequest, Endpoint, HttpCall, Provider


class GeminiProvider(Provider):
    name = "gemini"
    supports_stream = True

    @classmethod
    def matches(cls, endpoint: Endpoint) -> bool:
        return "generateContent" in endpoint.url

    def build(self, endpoint: Endpoint, request: DrawRequest) -> HttpCall:
        parts: List[Dict[str, Any]] = []

        # 多图时给每张图加序号标签，帮助模型区分参考图
        multi = len(request.images) > 1
        for i in range(len(request.images)):
            if multi:
                parts.append({"text": f"Image {i + 1}:"})
            parts.append(
                {
                    "inline_data": {
                        "mime_type": request.mime_types[i],
                        "data": request.b64(i),
                    }
                }
            )

        parts.append({"text": f"Prompt: {request.prompt}" if multi else request.prompt})

        payload = {
            "contents": [{"parts": parts}],
            "safetySettings": [
                {
                    "category": "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                    "threshold": "BLOCK_NONE",
                }
            ],
        }

        stream = self.wants_stream(endpoint)
        return HttpCall(
            url=f"{endpoint.url}?key={endpoint.key}",
            headers={"Content-Type": "application/json"},
            json=payload,
            stream=stream,
            timeout=self.stream_timeout if stream else self.timeout,
        )
