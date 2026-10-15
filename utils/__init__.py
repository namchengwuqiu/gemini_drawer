"""Gemini Drawer 工具函数包。

按职责拆为七个模块，此处统一重导出，调用点仍写 ``from ..utils import xxx``：

- log:         logger / truncate_for_log / redact_url / safe_json_dumps
- config_file: fix_broken_toml_config / save_config_file
- response:    extract_text_failure_reason / extract_all_image_data / extract_video_data
- image:       download_image / get_image_mime_type / convert_if_gif
- net:         下载地址策略与受限下载（configure_policy / stream_download / ...）
- limits:      解码、下载与流式累积的资源上限常量
- version:     get_plugin_version
"""
from .config_file import fix_broken_toml_config, save_config_file
from .image import convert_if_gif, download_image, get_image_mime_type
from .limits import (
    MAX_DEBUG_SSE_LINE_CHARS,
    MAX_DEBUG_SSE_LINES,
    MAX_ERROR_BODY_BYTES,
    MAX_GIF_PIXELS,
    MAX_HTTP_RESPONSE_BYTES,
    MAX_IMAGE_BYTES,
    MAX_INLINE_BASE64_CHARS,
    MAX_MESSAGE_IMAGES,
    MAX_RESULT_BASE64_CHARS,
    MAX_SSE_ACCUMULATED_CHARS,
    MAX_VIDEO_BASE64_CHARS,
    MAX_VIDEO_BYTES,
)
from .log import logger, redact_url, safe_json_dumps, sanitize_upstream_text, truncate_for_log
from .net import (
    DownloadedMedia,
    DownloadNotAllowedError,
    DownloadPolicy,
    MediaTooLargeError,
    configure_policy,
    current_policy,
    ensure_url_allowed,
    parse_http_url,
    reset_policy,
    stream_download,
)
from .response import (
    extract_all_image_data,
    extract_http_error_reason,
    extract_text_failure_reason,
    extract_video_data,
    interpret_send_result,
)
from .version import get_plugin_version

__all__ = [
    "DownloadedMedia",
    "DownloadNotAllowedError",
    "DownloadPolicy",
    "MAX_DEBUG_SSE_LINE_CHARS",
    "MAX_DEBUG_SSE_LINES",
    "MAX_ERROR_BODY_BYTES",
    "MAX_GIF_PIXELS",
    "MAX_HTTP_RESPONSE_BYTES",
    "MAX_IMAGE_BYTES",
    "MAX_INLINE_BASE64_CHARS",
    "MAX_MESSAGE_IMAGES",
    "MAX_RESULT_BASE64_CHARS",
    "MAX_SSE_ACCUMULATED_CHARS",
    "MAX_VIDEO_BASE64_CHARS",
    "MAX_VIDEO_BYTES",
    "MediaTooLargeError",
    "configure_policy",
    "convert_if_gif",
    "current_policy",
    "download_image",
    "ensure_url_allowed",
    "extract_all_image_data",
    "extract_http_error_reason",
    "extract_text_failure_reason",
    "extract_video_data",
    "fix_broken_toml_config",
    "get_image_mime_type",
    "get_plugin_version",
    "interpret_send_result",
    "logger",
    "parse_http_url",
    "redact_url",
    "reset_policy",
    "sanitize_upstream_text",
    "safe_json_dumps",
    "save_config_file",
    "stream_download",
    "truncate_for_log",
]
