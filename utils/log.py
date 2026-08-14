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

def safe_json_dumps(obj: Any) -> str:
    """安全地序列化JSON对象，对base64数据进行截断"""
    def truncate_base64_values(o):
        if isinstance(o, dict):
            new_dict = {}
            for k, v in o.items():
                if isinstance(v, str) and ('base64' in v.lower() or len(v) > 500):
                    new_dict[k] = truncate_for_log(v)
                elif isinstance(v, (dict, list)):
                    new_dict[k] = truncate_base64_values(v)
                else:
                    new_dict[k] = v
            return new_dict
        elif isinstance(o, list):
            return [truncate_base64_values(item) for item in o]
        return o
    
    truncated_obj = truncate_base64_values(obj)
    return json.dumps(truncated_obj, ensure_ascii=False)
