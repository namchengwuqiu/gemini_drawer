"""
Gemini Drawer Provider 适配层 —— 数据契约与基类

本模块定义绘图请求在"渠道端点"与"具体 API 协议"之间的翻译契约。
每个 provider 只负责三件事：

- matches():  判断某个端点是否归自己处理（注册表按顺序取首个命中者）
- build():    把 Endpoint + DrawRequest 翻译成一次 HttpCall（URL / headers / body）
- fetch():    发出请求并返回图片数据列表；默认实现覆盖「一次性 POST」与「SSE 流式」
              两种常见形态；"建任务 + 轮询"的异步 API 需覆写此方法。

新增一种渠道类型 = 新增一个 provider 文件 + 在 providers/__init__.py 注册表登记一行。
"""
from __future__ import annotations

import base64
import json
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

from ..core.errors import (UpstreamHTTPError, SubmissionUncertainError, GenerationRejectedError, safe_model_rejection)
from ..utils.http import read_body_capped as _read_body_capped, iter_sse_lines
from ..utils import (
    MediaTooLargeError,
    extract_all_image_data,
    extract_http_error_reason,
    redact_url,
)
from ..utils.limits import (
    MAX_DEBUG_SSE_LINE_CHARS,
    MAX_DEBUG_SSE_LINES,
    MAX_ERROR_BODY_BYTES,
    MAX_HTTP_RESPONSE_BYTES,
    MAX_SSE_ACCUMULATED_CHARS,
)


@dataclass
class Endpoint:
    """一个可用的绘图端点：渠道 URL + 一把具体的 Key。"""
    type: str
    url: str
    key: str = ""
    model: Optional[str] = None
    stream: bool = False
    #: 渠道级输出格式覆盖（如 "url" / "b64_json"）；None 表示沿用 provider 默认值。
    #: 用于 /images/generations 形态的中转：同一协议下不同上游对输出格式的要求不同。
    response_format: Optional[str] = None
    #: 渠道显式指定的协议 id（见 core/protocols.py）；None 表示按 URL / 模型名自动识别。
    protocol: Optional[str] = None
    #: 渠道单次生成等待上限（秒）；仍受整条命令的总预算限制。
    timeout: Optional[float] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Endpoint":
        """兼容 endpoints.py 产出的 dict 形态。"""
        return cls(
            type=data.get("type") or "",
            url=data.get("url") or "",
            key=data.get("key") or "",
            model=data.get("model"),
            stream=bool(data.get("stream", False)),
            response_format=data.get("response_format") or None,
            protocol=data.get("protocol") or None,
            timeout=cls.normalize_timeout(data.get("timeout")),
        )

    @staticmethod
    def normalize_timeout(value: Any) -> Optional[float]:
        if value is None or isinstance(value, bool):
            return None
        try:
            timeout = float(value)
        except (TypeError, ValueError):
            return None
        return timeout if math.isfinite(timeout) and timeout > 0 else None

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

    #: 规范协议 id（见 core/protocols.py）；显式 protocol 路由与 /渠道列表 展示都用它
    protocol: str = ""
    name: str = ""
    #: 该协议是否支持本插件的 SSE 流式解析路径。为 False 时忽略渠道的 stream 开关。
    supports_stream: bool = False
    #: 非流式请求的默认超时
    timeout: float = 120.0
    #: 流式请求的默认超时
    stream_timeout: float = 180.0
    #: 异步任务型协议（建任务 + 轮询）整个生成阶段的绝对上限（秒）。
    #: None 表示 ``call.timeout`` 就是该次 fetch 的绝对上限；任务型协议必须
    #: 覆写此值，否则单次 HTTP timeout 会被误当成整个任务的上限（F8）。
    task_timeout: Optional[float] = None

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
        # 用流式读取代替 client.post：响应体在读取阶段就受字节上限约束，
        # 不会把超大响应先完整载入内存再检查（F6）。
        if call.files is not None:
            request_kwargs: Dict[str, Any] = {"data": call.data, "files": call.files}
        else:
            request_kwargs = {"json": call.json}
        async with client.stream(
            "POST", call.url, headers=call.headers, **request_kwargs
        ) as response:
            if response.status_code != 200:
                body = await _read_body_capped(response, MAX_ERROR_BODY_BYTES)
                # 结构化错误：状态码、阶段与简短原因分开保存，原始响应体只进日志。
                raise UpstreamHTTPError(
                    response.status_code,
                    stage="fetch",
                    body=body.decode("utf-8", "ignore"),
                    reason=extract_http_error_reason(body),
                )
            body = await _read_body_capped(response, MAX_HTTP_RESPONSE_BYTES, enforce=True)

        try:
            data = json.loads(body)
        except ValueError as exc:
            raise SubmissionUncertainError("API 响应解析失败，生成结果不确定") from exc

        img_data = await extract_all_image_data(data)
        if img_data:
            return img_data

        if debug_mode:
            logger.warning("[调试模式] 非流式响应未提取到图片，原始响应:")
            logger.warning("[调试模式] 已省略上游原文，避免暴露凭据或参考图")
        else:
            logger.warning("API 响应成功但未提取到图片。")
        reason = safe_model_rejection(data)
        if reason:
            raise GenerationRejectedError(f"API未返回图片：{reason}")
        raise SubmissionUncertainError("API响应未包含可提取的图片，生成结果不确定")

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
                raw_body = await _read_body_capped(response, MAX_ERROR_BODY_BYTES)
                raise UpstreamHTTPError(
                    response.status_code,
                    stage="fetch",
                    body=raw_body.decode("utf-8", "ignore"),
                    reason=extract_http_error_reason(raw_body),
                )

            async for line in iter_sse_lines(response):
                line = line.strip()
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    continue

                data_str = line[5:].strip()
                if data_str in ("DONE", "[DONE]"):
                    break

                if debug_sse_lines is not None and len(debug_sse_lines) < MAX_DEBUG_SSE_LINES:
                    debug_sse_lines.append(data_str[:MAX_DEBUG_SSE_LINE_CHARS])

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
                    if len(accumulated_content) > MAX_SSE_ACCUMULATED_CHARS:
                        # 本地资源上限：统一用 MediaTooLargeError，调用方不得换端点重试（F6）。
                        raise MediaTooLargeError(
                            f"流式响应累积内容超过上限（{MAX_SSE_ACCUMULATED_CHARS} 字符），已中止"
                        )

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
        if debug_sse_lines:
            logger.warning(
                f"[调试模式] SSE流未提取到图片，累积 {len(debug_sse_lines)} 条数据:"
            )
            for idx, dl in enumerate(debug_sse_lines):
                logger.warning(f"[调试模式] SSE[{idx}]: 已省略原文（{len(dl)} 字符）")

        raise SubmissionUncertainError("API流式响应未包含可提取的图片，生成结果不确定")
