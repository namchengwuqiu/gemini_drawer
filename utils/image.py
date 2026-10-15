"""图片字节流的下载、类型识别与格式转换。

- download_image(): 异步下载图片，支持代理；目标地址经 utils.net 的地址策略
  校验（含逐跳重定向与 DNS 解析结果），并按实际累计字节数限制大小
- get_image_mime_type(): 根据图片内容检测 MIME 类型（只做字节前缀判断，不解码）
- convert_if_gif(): 将 GIF 图片转换为 PNG 格式（取第一帧），并限制像素规模
"""
import io
from typing import Optional

from PIL import Image

from .limits import MAX_GIF_PIXELS, MAX_IMAGE_BYTES
from .log import logger, redact_url
from .net import DownloadNotAllowedError, MediaTooLargeError, stream_download

_IMAGE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


async def download_image(
    url: str,
    proxy: Optional[str],
    *,
    timeout: float = 60.0,
    max_bytes: int = MAX_IMAGE_BYTES,
) -> Optional[bytes]:
    """下载图片，支持需要浏览器级别请求头的 CDN。

    任何失败（地址策略拒绝、超过大小上限、网络错误、非图片内容）都返回 None
    并记录日志；不会抛出异常。
    """
    headers = {
        **_IMAGE_HEADERS,
        "Referer": url.split("?")[0],  # 使用 URL 基础部分作为 Referer
    }
    try:
        media = await stream_download(
            url, proxy=proxy, headers=headers, timeout=timeout, max_bytes=max_bytes
        )
    except (DownloadNotAllowedError, MediaTooLargeError) as e:
        logger.error(f"下载图片被拒绝: {e}")
        return None
    except Exception as e:
        logger.error(f"下载图片失败: {type(e).__name__}")
        return None

    content_type = media.content_type
    if content_type and not content_type.startswith("image/"):
        detected_mime = get_image_mime_type(media.data)
        if detected_mime == "application/octet-stream":
            logger.warning("下载结果不是可识别的图片")
            return None
    return media.data


def get_image_mime_type(image_bytes: bytes) -> str:
    if image_bytes.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if image_bytes.startswith(b'\xff\xd8'):
        return 'image/jpeg'
    if image_bytes.startswith(b'GIF8'):
        return 'image/gif'
    if image_bytes.startswith(b'RIFF') and image_bytes[8:12] == b'WEBP':
        return 'image/webp'
    return 'application/octet-stream'


def convert_if_gif(image_bytes: bytes) -> bytes:
    mime = get_image_mime_type(image_bytes)
    if mime == 'image/gif':
        logger.info("检测到GIF图片，正在转换为PNG...")
        try:
            with Image.open(io.BytesIO(image_bytes)) as img:
                width, height = img.size
                if width * height > MAX_GIF_PIXELS:
                    logger.error(
                        f"GIF 尺寸过大（{width}x{height}，上限 {MAX_GIF_PIXELS} 像素），"
                        "已跳过转换，直接使用原始数据。"
                    )
                    return image_bytes
                img.seek(0)
                output = io.BytesIO()
                img.save(output, format='PNG')
                return output.getvalue()
        except Exception as e:
            logger.error(f"GIF转PNG失败: {e}")
            return image_bytes
    return image_bytes
