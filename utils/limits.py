"""解码、下载与流式累积的资源上限。

消息段内联 base64、上游返回的生成结果、下载内容和 SSE 累积都受这些上限
约束。超限时给出统一、可读的错误，并且不触发重新生成。
"""

class MediaTooLargeError(RuntimeError):
    """本地资源限制：停止处理，不重新生成，也不惩罚 Key。"""

#: 消息段内联 base64 的最大编码长度（字符，约 18MB 二进制）
MAX_INLINE_BASE64_CHARS = 24_000_000
#: 单条消息最多处理的图片张数
MAX_MESSAGE_IMAGES = 10
#: 单张图片下载的最大字节数
MAX_IMAGE_BYTES = 32 * 1024 * 1024
#: 视频下载的最大字节数
MAX_VIDEO_BYTES = 64 * 1024 * 1024
#: SSE 流式响应累积内容的最大字符数
MAX_SSE_ACCUMULATED_CHARS = 32_000_000
#: 生成结果中 base64 图片数据的最大编码长度（字符）
MAX_RESULT_BASE64_CHARS = 48_000_000
#: GIF 转换允许的最大像素数（宽 × 高）
MAX_GIF_PIXELS = 40_000_000
#: 非流式成功响应允许读入的最大字节数（在读取阶段生效，不信任 Content-Length）
MAX_HTTP_RESPONSE_BYTES = 64 * 1024 * 1024
#: 错误响应体最多读入的字节数（只用于日志诊断，超出即截断）
MAX_ERROR_BODY_BYTES = 64 * 1024
#: 生成结果中内联 base64 视频数据的最大编码长度（字符，约 67MB 二进制）
MAX_VIDEO_BASE64_CHARS = 90_000_000
#: 调试模式下保留的 SSE 数据行数上限
MAX_DEBUG_SSE_LINES = 200
#: 调试模式下单条 SSE 数据行保留的最大字符数
MAX_DEBUG_SSE_LINE_CHARS = 2000
MAX_SSE_LINE_BYTES = 48_000_000
MAX_SSE_TOTAL_BYTES = 96 * 1024 * 1024


def format_bytes(size: int) -> str:
    """把字节数格式化成便于阅读的文本。"""
    value = float(max(0, int(size)))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}GB"
