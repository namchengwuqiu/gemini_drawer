"""OpenAI /chat/completions 兼容协议（含 lmarena 中转）。"""
from __future__ import annotations

from typing import Any, Dict, List

from .base import DrawRequest, Endpoint, HttpCall, Provider

DEFAULT_MODEL = "gemini-pro-vision"


def build_chat_completions_call(
    endpoint: Endpoint,
    request: DrawRequest,
    *,
    default_model: str = DEFAULT_MODEL,
    stream: bool = False,
    timeout: float = 120.0,
    use_proxy: bool = True,
) -> HttpCall:
    """构造一份 /chat/completions 形态的请求。

    OpenAI 兼容协议与 gpt-image 的 chat 形态共用这段 payload 逻辑，
    保证两边发出的请求体逐字一致，响应也就能走同一条图片提取路径。
    """
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

    payload = {
        "model": endpoint.model_name or default_model,
        "messages": [{"role": "user", "content": content}],
        "stream": stream,
    }

    return HttpCall(
        url=endpoint.url,
        headers=headers,
        json=payload,
        stream=stream,
        timeout=timeout,
        use_proxy=use_proxy,
    )


class OpenAICompatProvider(Provider):
    name = "openai_compat"
    supports_stream = True

    @classmethod
    def matches(cls, endpoint: Endpoint) -> bool:
        return endpoint.type == "lmarena" or "/chat/completions" in endpoint.url

    def build(self, endpoint: Endpoint, request: DrawRequest) -> HttpCall:
        stream = self.wants_stream(endpoint)
        return build_chat_completions_call(
            endpoint,
            request,
            stream=stream,
            timeout=self.stream_timeout if stream else self.timeout,
            # lmarena 是本地/内网中转，走代理反而不通
            use_proxy=endpoint.type != "lmarena",
        )
