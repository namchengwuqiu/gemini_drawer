"""
Gemini Drawer 工具函数模块

本模块提供插件使用的各种工具函数：

配置文件处理：
- fix_broken_toml_config(): 修复 TOML 配置文件中未加引号的中文键名
- save_config_file(): 统一的配置文件保存入口，确保中文 Key 正确处理

日志工具：
- truncate_for_log(): 截断过长的日志数据
- safe_json_dumps(): 安全的 JSON 序列化，自动截断 base64 数据

图片处理：
- download_image(): 异步下载图片，支持代理
- get_image_mime_type(): 根据图片内容检测 MIME 类型
- convert_if_gif(): 将 GIF 图片转换为 PNG 格式（取第一帧）
- extract_image_data(): 从 API 响应中提取图片数据（URL 或 Base64）

模块级实例：
- logger: 插件专用的日志记录器
"""
import asyncio
import json
import re
import io
import httpx
import base64
from pathlib import Path
from typing import List, Tuple, Type, Optional, Dict, Any
from PIL import Image
from src.common.logger import get_logger

# 日志记录器
logger = get_logger("gemini_drawer")

def fix_broken_toml_config(file_path: Path):
    """
    读取配置文件原始文本，使用正则强制修复未加引号的中文键名。
    专门解决框架自动生成时 key 不带引号导致 Empty key 报错的问题。
    """
    if not file_path.exists():
        return

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        fixed_lines = []
        modified = False
        
        # 匹配规则：行首是非引号、非注释、非方括号的字符，且包含中文，后接等号
        pattern = re.compile(r'^([^#\n"\'\[]*[\u4e00-\u9fa5][^#\n"\'\[]*?)\s*=')
        
        # 简单的状态机，用于处理 admins 列表
        in_admins_block = False
        
        for line in lines:
            stripped = line.strip()
            
            # 1. 修复中文键名 (现有逻辑)
            match = pattern.match(line)
            if match:
                key = match.group(1).strip()
                parts = line.split('=', 1)
                if len(parts) == 2:
                    new_line = f'"{key}" ={parts[1]}'
                    fixed_lines.append(new_line)
                    modified = True
                    continue
            
            # 2. 修复 admins 列表中的纯数字 (新增逻辑)
            if stripped.startswith('admins = ['):
                in_admins_block = True
                fixed_lines.append(line)
            elif in_admins_block and stripped == ']':
                in_admins_block = False
                fixed_lines.append(line)
            elif in_admins_block:
                # 检查是否是纯数字（可能带逗号）
                # 匹配: 空白 + 数字 + 可选逗号 + 空白
                digit_match = re.match(r'^(\s*)(\d+)(\s*,?\s*)$', line)
                if digit_match:
                    # 给数字加上双引号
                    prefix, number, suffix = digit_match.groups()
                    fixed_lines.append(f'{prefix}"{number}"{suffix}')
                    modified = True
                else:
                    fixed_lines.append(line)
            else:
                fixed_lines.append(line)
        
        if modified:
            with open(file_path, 'w', encoding='utf-8') as f:
                f.writelines(fixed_lines)
            logger.info("配置文件格式已自动修复（中文Key引号/Admins列表格式）。")
            
    except Exception as e:
        logger.error(f"尝试自动修复配置文件失败: {e}")

def save_config_file(config_path: Path, config_data: Dict[str, Any]):
    """
    统一的保存入口，保存前先转为字符串并二次处理，确保中文Key有引号。
    """
    try:
        import toml
        # 1. 先生成标准 TOML 字符串
        content = toml.dumps(config_data)
        
        # 2. 再次进行正则修复
        lines = content.splitlines()
        final_lines = []
        for line in lines:
            stripped = line.strip()
            if '=' in stripped and not stripped.startswith('#') and not stripped.startswith('['):
                key_part, rest = stripped.split('=', 1)
                key_clean = key_part.strip()
                # 如果包含非ASCII且没引号
                if any(ord(c) > 127 for c in key_clean) and not (key_clean.startswith('"') or key_clean.startswith("'")):
                    line = f'"{key_clean}" ={rest}'
            final_lines.append(line)
            
        with open(config_path, 'w', encoding='utf-8') as f:
            f.write("\n".join(final_lines))
            
    except Exception as e:
        logger.error(f"保存配置文件失败: {e}")

def truncate_for_log(data: str, max_length: int = 100) -> str:
    """截断用于日志的数据，避免过长"""
    if len(data) <= max_length:
        return data
    return data[:max_length//2] + "...[truncated]..." + data[-max_length//2:]

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

async def extract_image_data(response_data: Dict[str, Any]) -> Optional[str]:
    """从API响应中提取图片数据（URL或Base64）"""
    try:
        # 豆包格式响应解析
        # 格式: {"data": [{"url": "...", "size": "..."} 或 {"b64_json": "..."}]}
        if "data" in response_data and isinstance(response_data["data"], list):
            for item in response_data["data"]:
                if isinstance(item, dict):
                    # 优先检查 URL
                    if "url" in item and item["url"]:
                        logger.info(f"从豆包响应中提取到图片URL: {item['url'][:100]}...")
                        return item["url"]
                    # 检查 base64 格式
                    if "b64_json" in item and item["b64_json"]:
                        logger.info("从豆包响应中提取到 base64 图片数据")
                        return item["b64_json"]
        
        if "choices" in response_data and isinstance(response_data["choices"], list) and response_data["choices"]:
            choice = response_data["choices"][0]
            content_data = None

            # Handle streaming response with 'delta'
            delta = choice.get("delta")
            if delta and "content" in delta:
                content_data = delta["content"]
            
            # Handle non-streaming response with 'message'
            message = choice.get("message")
            if content_data is None and message:
                if "content" in message:
                    content_data = message["content"]
            
            # 检查 message.images 数组格式（某些 API 把图片放在 images 字段中）
            # 格式: {"message": {"images": [{"type": "image_url", "image_url": {"url": "..."}}]}}
            if message and "images" in message and isinstance(message["images"], list):
                for img_item in message["images"]:
                    if isinstance(img_item, dict):
                        img_type = img_item.get("type", "")
                        
                        # 处理 type: "image_url" 格式
                        if img_type == "image_url":
                            image_url_obj = img_item.get("image_url", {})
                            if isinstance(image_url_obj, dict) and "url" in image_url_obj:
                                url = image_url_obj["url"]
                                if url.startswith("data:image"):
                                    if "base64," in url:
                                        logger.info("从 message.images 中提取到图片 base64 数据")
                                        return url.split("base64,")[1]
                                else:
                                    logger.info(f"从 message.images 中提取到图片 URL: {url[:100]}...")
                                    return url
                        
                        # 处理直接的 url 字段
                        if "url" in img_item and img_item["url"]:
                            url = img_item["url"]
                            if url.startswith("data:image") and "base64," in url:
                                logger.info("从 message.images 中提取到图片 base64 数据")
                                return url.split("base64,")[1]
                            else:
                                logger.info(f"从 message.images 中提取到图片 URL: {url[:100]}...")
                                return url

            if content_data is not None:
                # 处理 content 为数组格式的情况（新版 OpenAI 兼容格式）
                # 格式: [{"type": "image", "image": {"data": "base64..."}}]
                if isinstance(content_data, list):
                    for item in content_data:
                        if isinstance(item, dict):
                            item_type = item.get("type", "")
                            
                            # 处理 type: "image" 格式
                            if item_type == "image":
                                image_obj = item.get("image", {})
                                if isinstance(image_obj, dict):
                                    # 检查 data 字段（base64）
                                    if "data" in image_obj and image_obj["data"]:
                                        logger.info("从响应中提取到图片 base64 数据 (content array 格式)")
                                        return image_obj["data"]
                                    # 检查 url 字段
                                    if "url" in image_obj and image_obj["url"]:
                                        logger.info(f"从响应中提取到图片 URL: {image_obj['url'][:100]}...")
                                        return image_obj["url"]
                            
                            # 处理 type: "image_url" 格式
                            if item_type == "image_url":
                                image_url_obj = item.get("image_url", {})
                                if isinstance(image_url_obj, dict) and "url" in image_url_obj:
                                    url = image_url_obj["url"]
                                    if url.startswith("data:image"):
                                        # data URL 格式，提取 base64 部分
                                        if "base64," in url:
                                            logger.info("从响应中提取到图片 base64 数据 (data URL 格式)")
                                            return url.split("base64,")[1]
                                    else:
                                        logger.info(f"从响应中提取到图片 URL: {url[:100]}...")
                                        return url
                            
                            # 处理纯文本中的图片链接
                            if item_type == "text" and "text" in item:
                                text_content = item["text"]
                                if isinstance(text_content, str):
                                    # 匹配 markdown 图片格式
                                    match_url = re.search(r"!\[.*?\]\((.*?)\)", text_content)
                                    if match_url:
                                        image_url = match_url.group(1)
                                        logger.info(f"从文本中提取到图片 URL: {image_url[:100] if len(image_url) > 100 else image_url}...")
                                        return image_url
                    
                    logger.debug("content 为数组格式但未找到图片数据")
                
                # 处理 content 为字符串格式的情况（旧版格式）
                elif isinstance(content_data, str):
                    content_text = content_data
                    
                    match_url = re.search(r"!\[.*?\]\((.*?)\)", content_text)
                    if match_url:
                        image_url = match_url.group(1)
                        log_url = image_url[:100] + "..." if len(image_url) > 100 else image_url
                        logger.info(f"从响应中提取到图片URL: {log_url}")
                        return image_url

                    # 匹配裸露的HTTP/HTTPS URL
                    # 优先匹配常见的图片后缀
                    match_plain_url = re.search(r"https?://[^\s]+\.(?:png|jpg|jpeg|gif|webp|bmp|ico|tiff?)(?:\?[^\s]*)?", content_text, re.IGNORECASE)
                    if match_plain_url:
                        image_url = match_plain_url.group(0)
                        logger.info(f"从响应中提取到裸图片URL(带后缀): {image_url[:100]}...")
                        return image_url
                    
                    # 如果没有带后缀的URL，再次尝试匹配所有URL，但在日志中标记风险
                    # 这一步是为了兼容某些不带后缀的图片API（如某些重定向链接）
                    # 但为了避免匹配到 dashboard 等页面，我们可以尝试排除一些关键词
                    match_all_url = re.search(r"https?://[^\s]+", content_text)
                    if match_all_url:
                        possible_url = match_all_url.group(0)
                        # 简单的逻辑排除非图片页面
                        if not any(kw in possible_url.lower() for kw in ['dashboard', 'login', 'signin', 'register', 'admin']):
                            logger.info(f"从响应中提取到裸可能是图片的URL: {possible_url[:100]}...")
                            return possible_url
                        else:
                            logger.warning(f"跳过疑似非图片的URL: {possible_url}")

                    match_b64 = re.search(r"data:image/\w+;base64,([a-zA-Z0-9+/=\n]+)", content_text)
                    if match_b64:
                        logger.info("从响应中提取到 base64 图片数据 (字符串格式)")
                        return match_b64.group(1)

        candidates = response_data.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            return None

        content = candidates[0].get("content")
        if not isinstance(content, dict):
            return None

        parts = content.get("parts")
        if not isinstance(parts, list) or not parts:
            return None

        for part in parts:
            if not isinstance(part, dict):
                continue
            inline_data = part.get("inlineData") or part.get("inline_data")
            if isinstance(inline_data, dict):
                image_b64 = inline_data.get("data")
                if isinstance(image_b64, str):
                    return image_b64

            text_content = part.get("text")
            if isinstance(text_content, str):
                match = re.search(r"data:image/\w+;base64,([a-zA-Z0-9+/=\n]+)", text_content)
                if match:
                    return match.group(1)

        return None
    except Exception:
        return None

async def extract_video_data(response_data: Dict[str, Any]) -> Optional[str]:
    """从API响应中提取视频数据（Base64）
    
    支持的响应格式:
    {
        "choices": [{
            "message": {
                "content": "![image](data:video/mp4;base64,AAAAIGZ0eXBpc29tAA..."
            }
        }]
    }
    
    Returns:
        base64 编码的视频数据字符串，或 None
    """
    try:
        if "choices" in response_data and isinstance(response_data["choices"], list) and response_data["choices"]:
            choice = response_data["choices"][0]
            content_data = None

            # Handle streaming response with 'delta'
            delta = choice.get("delta")
            if delta and "content" in delta:
                content_data = delta["content"]
            
            # Handle non-streaming response with 'message'
            message = choice.get("message")
            if content_data is None and message:
                if "content" in message:
                    content_data = message["content"]

            if content_data is not None and isinstance(content_data, str):
                # 匹配 markdown 格式的视频 data URL
                # 格式: ![image](data:video/mp4;base64,...)
                match_video = re.search(r"!\[.*?\]\(data:video/[^;]+;base64,([a-zA-Z0-9+/=\n]+)\)", content_data)
                if match_video:
                    logger.info("从响应中提取到视频 base64 数据 (markdown 格式)")
                    return match_video.group(1)
                
                # 匹配裸露的 data URL 格式
                match_video_raw = re.search(r"data:video/[^;]+;base64,([a-zA-Z0-9+/=\n]+)", content_data)
                if match_video_raw:
                    logger.info("从响应中提取到视频 base64 数据 (裸 data URL 格式)")
                    return match_video_raw.group(1)
        
        # Gemini 格式响应解析
        candidates = response_data.get("candidates")
        if isinstance(candidates, list) and candidates:
            content = candidates[0].get("content")
            if isinstance(content, dict):
                parts = content.get("parts")
                if isinstance(parts, list):
                    for part in parts:
                        if isinstance(part, dict):
                            inline_data = part.get("inlineData") or part.get("inline_data")
                            if isinstance(inline_data, dict):
                                mime_type = inline_data.get("mimeType") or inline_data.get("mime_type", "")
                                if "video" in mime_type:
                                    video_b64 = inline_data.get("data")
                                    if isinstance(video_b64, str):
                                        logger.info("从 Gemini 响应中提取到视频 base64 数据")
                                        return video_b64
                            
                            # 检查文本内容中的视频 data URL
                            text_content = part.get("text")
                            if isinstance(text_content, str):
                                match = re.search(r"data:video/[^;]+;base64,([a-zA-Z0-9+/=\n]+)", text_content)
                                if match:
                                    logger.info("从 Gemini 文本响应中提取到视频 base64 数据")
                                    return match.group(1)
        
        return None
    except Exception:
        return None

async def download_image(url: str, proxy: Optional[str]) -> Optional[bytes]:
    try:
        async with httpx.AsyncClient(proxy=proxy, timeout=30.0, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.content
    except httpx.RequestError as e:
        logger.error(f"下载图片失败: {url}, 错误: {e}")
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