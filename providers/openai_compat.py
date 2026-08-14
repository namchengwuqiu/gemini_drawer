"""OpenAI /chat/completions 兼容协议（含 lmarena 中转）。"""
from __future__ import annotations

from typing import Any, Dict, List

from .base import DrawRequest, Endpoint, HttpCall, Provider

DEFAULT_MODEL = "gemini-pro-vision"


class OpenAICompatProvider(Provider):
    name = "openai_compat"
    supports_stream = True

    @classmethod
    def matches(cls, endpoint: Endpoint) -> bool:
        return endpoint.type == "lmarena" or "/chat/completions" in endpoint.url

    def build(self, endpoint: Endpoint, request: DrawRequest) -> HttpCall:
        multi = len(request.images) > 1
        content: List[Dict[str, Any]] = [
            {"type": "text", "text": f"Prompt: {request.prompt}" if multi else request.prompt}
        ]

        for i in range(len(request.images)):
            if multi:
                content.append({"type": "text", "text": f"Image {i + 1}:"})
            content.append(
                {"type": "image_url", "image_url": {"url": request.data_url(i)}}
            )

        headers = {"Content-Type": "application/json"}
        if endpoint.key:
            headers["Authorization"] = f"Bearer {endpoint.key}"

        stream = self.wants_stream(endpoint)
        payload = {
            "model": endpoint.model_name or DEFAULT_MODEL,
            "messages": [{"role": "user", "content": content}],
            "stream": stream,
        }

        return HttpCall(
            url=endpoint.url,
            headers=headers,
            json=payload,
            stream=stream,
            timeout=self.stream_timeout if stream else self.timeout,
            # lmarena 是本地/内网中转，走代理反而不通
            use_proxy=endpoint.type != "lmarena",
        )
