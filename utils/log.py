"""日志记录器与日志安全化工具。

- logger: 插件专用的日志记录器
- truncate_for_log(): 截断过长的日志数据
- redact_url(): 隐去 URL 查询串中的 key/token，供日志输出
- safe_json_dumps(): 安全的 JSON 序列化，自动截断 base64 数据
"""
import json
import logging
import re
from contextvars import ContextVar
from contextlib import asynccontextmanager
from urllib.parse import quote, quote_plus
from typing import Any

from ..core.host_bridge import get_plugin_logger

# 日志记录器
logger = get_plugin_logger("plugin.gemini_drawer")

_request_secrets: ContextVar[tuple[str, ...]] = ContextVar("drawing_request_secrets", default=())


def set_request_secrets(*secrets):
    values = set(_request_secrets.get())
    for secret in secrets:
        if secret:
            values.update((str(secret), quote(str(secret), safe=""), quote_plus(str(secret))))
    return _request_secrets.set(tuple(sorted(values, key=len, reverse=True)))


def reset_request_secrets(token):
    _request_secrets.reset(token)


@asynccontextmanager
async def redact_request(*secrets):
    token = set_request_secrets(*secrets)
    try:
        yield
    finally:
        reset_request_secrets(token)


def _hide_known_secrets(value: str) -> str:
    for secret in _request_secrets.get():
        value = value.replace(secret, "***")
    return value


class _RequestLogFilter(logging.Filter):
    gemini_drawer_request_filter = True
    def filter(self, record):
        # HTTPX 会自行记录带查询 Key 的 URL；只处理本插件当前请求的日志上下文。
        if _request_secrets.get():
            record.msg = sanitize_upstream_text(record.getMessage(), max_length=2000)
            record.args = ()
        return True


for _log_name in ("httpx", "httpcore.connection", "httpcore.http11", "httpcore.http2", "httpcore.proxy"):
    _lib_logger = logging.getLogger(_log_name)
    for _old_filter in list(_lib_logger.filters):
        if getattr(_old_filter, "gemini_drawer_request_filter", False):
            _lib_logger.removeFilter(_old_filter)
    _lib_logger.addFilter(_RequestLogFilter())

def truncate_for_log(data: str, max_length: int = 100) -> str:
    """截断用于日志的数据，避免过长"""
    if len(data) <= max_length:
        return data
    return data[:max_length//2] + "...[truncated]..." + data[-max_length//2:]

# 需要在日志中脱敏的查询参数名（Gemini 把 key 直接拼在 URL 上）
_SENSITIVE_QUERY_KEYS = ("key", "api_key", "apikey", "token", "access_token")
_REDACT_PATTERN = re.compile(
    r'(?i)\b(' + "|".join(_SENSITIVE_QUERY_KEYS) + r')=[^&\s]+'
)

def redact_url(url: str) -> str:
    """隐去 URL 查询串中的密钥，供日志输出使用。"""
    if not url:
        return url
    return _REDACT_PATTERN.sub(lambda m: f"{m.group(1)}=***", _hide_known_secrets(url))


#: 上游错误文本里的凭据/地址特征：Bearer 令牌、赋值式密钥、常见密钥前缀、URL
_CREDENTIAL_PATTERNS = (
    re.compile(r'(?i)\b(bearer)\s+[A-Za-z0-9._\-+/=]{6,}'),
    re.compile(
        r'(?i)\b(api[-_]?key|key|access[-_]?token|auth[-_]?token|secret|token|password|passwd|pwd)'
        r'["\']?\s*[:=]\s*["\']?[A-Za-z0-9._\-+/=]{6,}'
    ),
    re.compile(
        r'(?i)\b(api[-_]?key|key|access[-_]?token|auth[-_]?token|secret|token|password)'
        r'\s+["\']?[A-Za-z0-9._\-+/=]{8,}'
    ),
    re.compile(r'\b(sk|rk|pk)-[A-Za-z0-9._\-]{8,}'),
    re.compile(r'\bAIza[0-9A-Za-z._\-]{10,}'),
    re.compile(r'\bgh[pousr]_[A-Za-z0-9]{16,}'),
)
#: 上游错误文本里的 URL（含内网主机名与查询串）；用户文案不展示具体地址
_URL_PATTERN = re.compile(r'https?://[^\s"\'<>)\]]+')

#: 上游信息进入用户文案前的默认截断长度
UPSTREAM_TEXT_MAX_LENGTH = 300


def _redact_credential(match: "re.Match[str]") -> str:
    """把命中的凭据替换为可读占位；无捕获组的模式（令牌前缀）整体替换。"""
    if match.re.groups:
        return f"{match.group(1)} ***"
    return "***"


def sanitize_upstream_text(text: Any, max_length: int = UPSTREAM_TEXT_MAX_LENGTH) -> str:
    """把上游返回的错误文本整理成可安全展示的简短说明。

    上游原文可能以"invalid key sk-xxx at http://internal.host"这类形式开头，
    只截断并不能阻止凭据或内网地址进入用户回复。这里先隐去 Bearer 令牌、
    赋值式密钥、常见密钥前缀与 URL，再截断；无法识别的内容按原样保留。
    日志与用户文案共用此函数，不允许把上游原文直接拼进任何一处。
    """
    value = _hide_known_secrets(str(text or ""))
    for pattern in _CREDENTIAL_PATTERNS:
        value = pattern.sub(_redact_credential, value)
    value = _URL_PATTERN.sub("[地址已隐藏]", value)
    return truncate_for_log(value.strip(), max_length)

#: 单个字符串超过此长度即视为疑似 base64/二进制数据，日志中需截断
_MAX_INLINE_STR_LEN = 500

def _is_blob_like(value: str) -> bool:
    """判断字符串是否为 base64/Data URI 这类需要截断的超长数据。"""
    return "base64" in value.lower() or len(value) > _MAX_INLINE_STR_LEN

def _sanitize_for_log(obj: Any, max_length: int = 100) -> Any:
    """递归地把任意嵌套结构里的 base64/超长字符串替换为截断版本。

    字典、列表、元组都会被递归展开，列表里的裸字符串同样会被截断
    （图片 Data URI 常以 ``extra_body.image = [data_url, ...]`` 的形态出现）。
    """
    if isinstance(obj, str):
        value = _hide_known_secrets(obj)
        return truncate_for_log(value, max_length) if _is_blob_like(value) else value
    if isinstance(obj, dict):
        return {k: _sanitize_for_log(v, max_length) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_for_log(item, max_length) for item in obj]
    return obj

def safe_json_dumps(obj: Any, max_length: int = 100) -> str:
    """安全地序列化JSON对象，对base64数据进行截断"""
    return json.dumps(_sanitize_for_log(obj, max_length), ensure_ascii=False)
