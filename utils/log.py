"""日志记录器与日志安全化工具。

- logger: 插件专用的日志记录器
- truncate_for_log(): 截断过长的日志数据
- redact_url(): 隐去 URL 查询串中的 key/token，供日志输出
- safe_json_dumps(): 安全的 JSON 序列化，自动截断 base64 数据
"""
import json
import re
from typing import Any

from ..core.host_bridge import get_plugin_logger

# 日志记录器
logger = get_plugin_logger("plugin.gemini_drawer")

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
    return _REDACT_PATTERN.sub(lambda m: f"{m.group(1)}=***", url)

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
        return truncate_for_log(obj, max_length) if _is_blob_like(obj) else obj
    if isinstance(obj, dict):
        return {k: _sanitize_for_log(v, max_length) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_for_log(item, max_length) for item in obj]
    return obj

def safe_json_dumps(obj: Any, max_length: int = 100) -> str:
    """安全地序列化JSON对象，对base64数据进行截断"""
    return json.dumps(_sanitize_for_log(obj, max_length), ensure_ascii=False)
