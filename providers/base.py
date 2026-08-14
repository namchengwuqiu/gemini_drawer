"""
Gemini Drawer Provider 适配层 —— 数据契约与基类

本模块定义绘图请求在"渠道端点"与"具体 API 协议"之间的翻译契约。
每个 provider 只负责三件事：

- matches():  判断某个端点是否归自己处理（注册表按顺序取首个命中者）
- build():    把 Endpoint + DrawRequest 翻译成一次 HttpCall（URL / headers / body）
- fetch():    发出请求并返回图片数据列表；默认实现覆盖「一次性 POST」与「SSE 流式」
              两种常见形态，TS-AI 这类"建任务 + 轮询"的异步 API 覆写此方法。

新增一种渠道类型 = 新增一个 provider 文件 + 在 providers/__init__.py 注册表登记一行。
"""
from __future__ import annotations

import base64
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

from ..utils import extract_all_image_data, extract_text_failure_reason, redact_url


@dataclass
class Endpoint:
    """一个可用的绘图端点：渠道 URL + 一把具体的 Key。"""
    type: str
    url: str
    key: str = ""
    model: Optional[str] = None
    stream: bool = False

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Endpoint":
        """兼容 endpoints.py 产出的 dict 形态。"""
        return cls(
            type=data.get("type") or "",
            url=data.get("url") or "",
            key=data.get("key") or "",
            model=data.get("model"),
            stream=bool(data.get("stream", False)),
        )

    @property
    def model_name(self) -> str:
        return (self.model or "").strip()


@dataclass
class DrawRequest:
    """一次绘图诉求：一段提示词 + 0..N 张参考图。

    images 与 mime_types 一一对应，调用方需已完成 convert_if_gif。
    images 为空即文生图。
    """
    prompt: str
    images: List[bytes] = field(default_factory=list)
    mime_types: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if len(self.images) != len(self.mime_types):
            raise ValueError(
                f"images 与 mime_types 数量不匹配: {len(self.images)} vs {len(self.mime_types)}"
            )

    @property
    def has_image(self) -> bool:
        return bool(self.images)

    @property
    def first_image(self) -> Optional[bytes]:
        return self.images[0] if self.images else None

    @property
    def first_mime(self) -> Optional[str]:
        return self.mime_types[0] if self.mime_types else None

    def b64(self, index: int = 0) -> str:
        return base64.b64encode(self.images[index]).decode("utf-8")

    def data_url(self, index: int = 0) -> str:
        return f"data:{self.mime_types[index]};base64,{self.b64(index)}"

    def data_urls(self) -> List[str]:
        return [self.data_url(i) for i in range(len(self.images))]


@dataclass
class HttpCall:
    """一次待发出的 HTTP 请求的完整描述。"""
    url: str
    headers: Dict[str, str] = field(default_factory=dict)
    json: Optional[Dict[str, Any]] = None
    files: Optional[Dict[str, Any]] = None   # multipart（gpt-image edits）
    data: Optional[Dict[str, Any]] = None    # multipart 的表单字段
    stream: bool = False
    timeout: float = 120.0
    use_proxy: bool = True                   # lmarena 强制直连

    @property
    def safe_url(self) -> str:
        """供日志使用的脱敏 URL。"""
        return redact_url(self.url)


class Provider(ABC):
    """所有渠道协议适配器的基类。"""

    name: str = ""
    #: 该协议是否支持本插件的 SSE 流式解析路径。为 False 时忽略渠道的 stream 开关。
    supports_stream: bool = False
    #: 非流式请求的默认超时
    timeout: float = 120.0
    #: 流式请求的默认超时
    stream_timeout: float = 180.0

    @classmethod
    @abstractmethod
    def matches(cls, endpoint: Endpoint) -> bool:
        """判断该端点是否由本 provider 处理。"""
        raise NotImplementedError

    @abstractmethod
    def build(self, endpoint: Endpoint, request: DrawRequest) -> HttpCall:
        """把端点与绘图诉求翻译成一次具体的 HTTP 调用。"""
        raise NotImplementedError

    def wants_stream(self, endpoint: Endpoint) -> bool:
        return bool(endpoint.stream) and self.supports_stream

    # ── 默认取图实现 ──────────────────────────────────────────

    async def fetch(
        self,
        call: HttpCall,
        client: httpx.AsyncClient,
        logger: Any,
        debug_mode: bool = False,
    ) -> List[str]:
        """发出请求并返回图片数据列表（base64 或 URL）。

        失败时抛异常，由 pipeline 捕获后切换到下一个端点。
        """
        if call.stream:
            return await self._fetch_stream(call, client, logger, debug_mode)
        return await self._fetch_once(call, client, logger, debug_mode)

    async def _fetch_once(
        self,
        call: HttpCall,
        client: httpx.AsyncClient,
        logger: Any,
        debug_mode: bool,
    ) -> List[str]:
        if call.files is not None:
            response = await client.post(
                call.url, data=call.data, files=call.files, headers=call.headers
            )
        else:
            response = await client.post(call.url, json=call.json, headers=call.headers)

        if response.status_code != 200:
            raise RuntimeError(
                f"API请求失败, 状态码: {response.status_code} - {response.text}"
            )

        data = response.json()
        img_data = await extract_all_image_data(data)
        if img_data:
            return img_data

        if debug_mode:
            logger.warning("[调试模式] 非流式响应未提取到图片，原始响应:")
            logger.warning(f"[调试模式] {json.dumps(data, ensure_ascii=False)[:2000]}")
        else:
            logger.warning("API 响应成功但未提取到图片。")
        reason = extract_text_failure_reason(data)
        raise RuntimeError(f"API未返回图片, 原因: {reason or '响应中没有可提取的图片数据'}")

    async def _fetch_stream(
        self,
        call: HttpCall,
        client: httpx.AsyncClient,
        logger: Any,
        debug_mode: bool,
    ) -> List[str]:
        """SSE 流式解析。

        只累积 delta/message 的正文，等流结束后再统一提取——
        中途提取会在半截 base64 chunk 上误命中并截断图片。
        """
        debug_sse_lines: Optional[List[str]] = [] if debug_mode else None
        accumulated_content = ""

        async with client.stream(
            "POST", call.url, json=call.json, headers=call.headers
        ) as response:
            if response.status_code != 200:
                raw_body = await response.aread()
                raise RuntimeError(
                    f"API请求失败, 状态码: {response.status_code} - "
                    f"{raw_body.decode('utf-8', 'ignore')}"
                )

            async for line in response.aiter_lines():
                line = line.strip()
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    continue

                data_str = line[5:].strip()
                if data_str in ("DONE", "[DONE]"):
                    break

                if debug_sse_lines is not None:
                    debug_sse_lines.append(data_str)

                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                choices = chunk.get("choices")
                if not choices:
                    continue
                choice = choices[0]
                chunk_content = (choice.get("delta") or {}).get("content", "")
                if not chunk_content:
                    message = choice.get("message")
                    if isinstance(message, dict):
                        chunk_content = message.get("content", "")
                if chunk_content:
                    accumulated_content += chunk_content

        if accumulated_content:
            logger.info(
                f"[图片] SSE流结束，尝试从累积内容中提取图片 (长度: {len(accumulated_content)})"
            )
            pseudo_response = {
                "choices": [{"message": {"content": accumulated_content}}]
            }
            img_data = await extract_all_image_data(pseudo_response)
            if img_data:
                logger.info(f"从累积内容中成功提取 {len(img_data)} 张图片数据。")
                return img_data
            reason = extract_text_failure_reason(pseudo_response)
        else:
            reason = ""

        if debug_sse_lines:
            logger.warning(
                f"[调试模式] SSE流未提取到图片，累积 {len(debug_sse_lines)} 条数据:"
            )
            for idx, dl in enumerate(debug_sse_lines):
                logger.warning(f"[调试模式] SSE[{idx}]: {dl[:500]}")

        raise RuntimeError(
            f"API未返回图片, 原因: {reason or '响应中没有可提取的图片数据'}"
        )
