"""图片字节流的下载、类型识别与格式转换。

- download_image(): 异步下载图片，支持代理
- get_image_mime_type(): 根据图片内容检测 MIME 类型
- convert_if_gif(): 将 GIF 图片转换为 PNG 格式（取第一帧）
"""
import io
from typing import Optional

import httpx
from PIL import Image

from .log import logger

async def download_image(url: str, proxy: Optional[str]) -> Optional[bytes]:
    """下载图片，支持需要浏览器级别请求头的 CDN"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": url.split("?")[0],  # 使用 URL 基础部分作为 Referer
    }
    try:
        async with httpx.AsyncClient(proxy=proxy, timeout=60.0, follow_redirects=True) as client:
            response = await client.get(url, headers=headers)
            if response.status_code != 200:
                logger.error(f"下载图片失败: {url}, HTTP 状态码: {response.status_code}, 响应: {response.text[:500]}")
                return None
            if not response.content:
                logger.error(f"下载图片失败: {url}, 响应内容为空")
                return None
            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type and not content_type.startswith("image/"):
                detected_mime = get_image_mime_type(response.content)
                if detected_mime == "application/octet-stream":
                    logger.warning(f"下载URL并非图片内容: {url}, Content-Type: {content_type}")
                    return None
            return response.content
    except httpx.RequestError as e:
        logger.error(f"下载图片请求异常: {url}, 错误: {e}")
        return None
    except Exception as e:
        logger.error(f"下载图片未知异常: {url}, 错误: {type(e).__name__}: {e}")
        return None

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
                img.seek(0)
                output = io.BytesIO()
                img.save(output, format='PNG')
                return output.getvalue()
        except Exception as e:
            logger.error(f"GIF转PNG失败: {e}")
            return image_bytes
    return image_bytes
