"""渠道 URL 的解析与分类。

管理命令与 WebUI 共用这里的解析/校验逻辑，避免"命令按子串识别、页面按
urlsplit 校验"两套规则漂移（P1-10）。分类只基于解析后的 path / query /
hostname，不做任意子串匹配。
"""
from __future__ import annotations

from typing import Optional, Tuple
from urllib.parse import SplitResult, urlsplit
import httpx

#: Gemini 原生协议 URL 的合法结尾
_GEMINI_SUFFIXES = (":generateContent", ":streamGenerateContent")
#: Gemini 生成方法名（出现在 path 最后一个冒号之后；可能带 query/fragment）
_GEMINI_METHOD_NAMES = ("generateContent", "streamGenerateContent")
#: TS-AI 的域名特征
_TSAI_HOSTS = ("tsart.lat", "tavr.top")

#: 分类结果（与 core/protocols.py 的协议 id 对齐）
KIND_OPENAI_CHAT = "openai_chat"
KIND_GEMINI = "gemini"
KIND_IMAGES = "images"
KIND_SEEDANCE_VIDEO = "seedance_video"
KIND_AGNES_VIDEO = "agnes_video"
KIND_TSAI = "tsai"
KIND_UNKNOWN = "unknown"


class ChannelUrlError(ValueError):
    """渠道 URL 不符合要求。"""


def parse_channel_url(url: str) -> SplitResult:
    """解析渠道 URL；要求 http(s) 且带主机名。"""
    try:
        parts = urlsplit(str(url or "").strip())
        _ = parts.port
    except ValueError as exc:
        raise ChannelUrlError("API 地址或端口不合法。") from exc
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise ChannelUrlError("请输入完整的 http:// 或 https:// API 地址。")
    return parts


def with_api_key(url: str, key: str) -> str:
    """保留已有查询参数，替换已有 Key，并移除不参与 HTTP 请求的 fragment。"""
    return str(httpx.URL(url).copy_with(fragment=None).copy_set_param("key", key))


def classify_channel_url(url: str) -> str:
    """按解析后的 path / query / hostname 分类渠道 URL。"""
    try:
        parts = parse_channel_url(url)
    except ChannelUrlError:
        return KIND_UNKNOWN
    path = parts.path
    query = parts.query.lower()
    host = (parts.hostname or "").lower()

    if path.rstrip("/").endswith("/v1/videos"):
        return KIND_AGNES_VIDEO
    if "/chat/completions" in path:
        return KIND_OPENAI_CHAT
    if "/images/generations" in path:
        return KIND_IMAGES
    if "/contents/generations/tasks" in path:
        return KIND_SEEDANCE_VIDEO
    if path.endswith(_GEMINI_SUFFIXES):
        return KIND_GEMINI
    if "endpoint=" in query or any(marker in host for marker in _TSAI_HOSTS):
        return KIND_TSAI
    return KIND_UNKNOWN


def is_gemini_suffix(url: str) -> bool:
    """URL 的 path 是否以 Gemini 的 :generateContent 结尾。"""
    try:
        parts = parse_channel_url(url)
    except ChannelUrlError:
        return False
    return parts.path.endswith(_GEMINI_SUFFIXES)


def _tail_is_gemini_method(tail: str) -> bool:
    """最后一个冒号之后的部分是否是 Gemini 生成方法（允许带 query/fragment）。

    ``https://host/v1beta/models/m:generateContent?alt=sse`` 的最后一段是
    ``generateContent?alt=sse``，它不是模型名，整段应视为 URL。
    """
    method = tail.split("?", 1)[0].split("#", 1)[0].strip()
    return method in _GEMINI_METHOD_NAMES


def split_url_and_model(rest: str) -> Tuple[str, Optional[str]]:
    """把 ``URL[:模型]`` 拆成 (url, model)。

    仅当最后一个冒号之后的部分像模型名（不含 ``/``、不是纯数字端口，也不是
    Gemini 的生成方法，含带 query/fragment 的写法）且前半部分是合法 URL 时
    才拆分；否则整体视为 URL。端口号不会被误当作模型名。
    """
    text = str(rest or "").strip()
    if is_gemini_suffix(text):
        return text, None
    head, _, tail = text.rpartition(":")
    tail = tail.strip()
    if (
        head
        and tail
        and "/" not in tail
        and not tail.startswith("//")
        and not tail.isdigit()
        and not text.endswith(_GEMINI_SUFFIXES)
        and not _tail_is_gemini_method(tail)
    ):
        try:
            parse_channel_url(head)
        except ChannelUrlError:
            pass
        else:
            return head.strip(), tail
    return text, None
