
import asyncio
import base64
import json
import time
from datetime import datetime
from typing import Tuple, Optional, List, Dict, Any

import httpx
import re
import os

from .utils import extract_image_data, safe_json_dumps, download_image
from .managers import key_manager, data_manager

from src.plugin_system.apis import message_api
try:
    from src.common.database.database_model import Images, Messages
except ImportError:
    Images = None
    Messages = None

async def extract_source_image(
    message,
    proxy: Optional[str] = None,
    logger = None
) -> Optional[bytes]:
    """
    从消息对象中提取图片（优先回复 > 消息内图片 > @用户头像 > 发送者头像）
    
    Args:
        message: MaiMessages 对象
        proxy: 代理地址
        logger: 日志对象（如果为None则不记录日志）
        
    Returns:
        图片字节或 None
    """
    
    
    # 1. 尝试从消息段中提取
    async def _extract_image_from_segments(segments) -> Optional[bytes]:
        if not segments:
            return None
        if hasattr(segments, 'type') and segments.type == 'seglist':
            segments = segments.data
        if not isinstance(segments, list):
            segments = [segments]
        for seg in segments:
            if seg.type == 'image' or seg.type == 'emoji':
                if isinstance(seg.data, dict) and seg.data.get('url'):
                    if logger: logger.info(f"在消息段中找到URL图片 (类型: {seg.type})。")
                    return await download_image(seg.data.get('url'), proxy)
                elif isinstance(seg.data, str) and len(seg.data) > 200:
                    try:
                        if logger: logger.info(f"在消息段中找到Base64图片 (类型: {seg.type})。")
                        return base64.b64decode(seg.data)
                    except Exception:
                        if logger: logger.warning(f"无法将类型为 '{seg.type}' 的段解码为图片，已跳过。")
                        continue
        return None

    # 2. 尝试从回复的消息中提取
    async def _extract_from_reply() -> Optional[bytes]:
        # 情况 A: MaiMessages 对象 (Runtime)
        if hasattr(message, 'reply') and message.reply:
            return await extract_source_image(message.reply, proxy, logger)
        
        # 情况 B: DatabaseMessages 对象 (Historical)
        if Messages:
            reply_to_id = getattr(message, 'reply_to', None)
            if reply_to_id and isinstance(reply_to_id, str):
                try:
                    reply_msg = Messages.get_or_none(Messages.message_id == reply_to_id)
                    if reply_msg:
                        return await extract_source_image(reply_msg, proxy, logger)
                except Exception as e:
                    if logger: logger.warning(f"Failed to fetch reply message from DB: {e}")
        return None

    # 3. 尝试从当前消息中提取
    async def _extract_from_current() -> Optional[bytes]:
        # 情况 A: MaiMessages 对象 (Runtime) - 有 message_segment
        if hasattr(message, 'message_segment'):
            return await _extract_image_from_segments(message.message_segment)
        
        # 情况 B: DatabaseMessages 对象 (Historical) - 检查 processed_plain_text 中的 [picid:xxx]
        if Images:
            text = getattr(message, 'processed_plain_text', '') or getattr(message, 'display_message', '') or ''
            matches = re.findall(r'\[picid:([^\]]+)\]', text)
            for pic_id in matches:
                try:
                    img_record = Images.get_or_none(Images.image_id == pic_id)
                    if img_record and img_record.path:
                        if os.path.exists(img_record.path):
                            with open(img_record.path, 'rb') as f:
                                return f.read()
                except Exception as e:
                    if logger: logger.warning(f"Failed to load image from path for picid {pic_id}: {e}")
        return None

    # 4. 尝试从 @的用户头像提取
    async def _extract_from_at_user() -> Optional[bytes]:
        async def _download_avatar(user_id: str) -> Optional[bytes]:
             avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"
             if logger: logger.info(f"使用 @用户 {user_id} 的头像。")
             return await download_image(avatar_url, proxy)

        # 情况 A: MaiMessages
        if hasattr(message, 'message_segment'):
            segments = message.message_segment
            if hasattr(segments, 'type') and segments.type == 'seglist':
                segments = segments.data
            if not isinstance(segments, list):
                segments = [segments]
            
            for seg in segments:
                # 检查 type='at'
                if seg.type == 'at':
                    if isinstance(seg.data, dict):
                        qq = seg.data.get('qq') or seg.data.get('user_id')
                        if qq and str(qq) != 'all':
                            return await _download_avatar(str(qq))
                # 检查 type='text' 中的 @<nick:id>
                elif seg.type == 'text' and isinstance(seg.data, str):
                    matches = re.findall(r'@<[^:>]+:([^:>]+)>', seg.data)
                    for user_id in matches:
                        return await _download_avatar(str(user_id))
        
        # 情况 B: DatabaseMessages (检查文本中的 @<nick:id>)
        text = getattr(message, 'processed_plain_text', '') or getattr(message, 'display_message', '') or ''
        at_matches = re.findall(r'@<[^:>]+:([^:>]+)>', text)
        for user_id in at_matches:
             return await _download_avatar(str(user_id))
            
        return None

    # === 执行提取逻辑 ===
    
    try:
        img = await _extract_from_reply()
        if img: return img
    except Exception as e:
        if logger: logger.warning(f"Error extracting from reply: {e}")

    try:
        img = await _extract_from_current()
        if img: return img
    except Exception as e:
        if logger: logger.warning(f"Error extracting from current message: {e}")

    try:
        img = await _extract_from_at_user()
        if img: return img
    except Exception as e:
        if logger: logger.warning(f"Error extracting from at user: {e}")
    
    return None

    
    """
    根据配置获取所有可用的绘图API端点
    Args:
        config_getter: 用于获取配置的函数 (key, default) -> value
    Returns:
        端点列表
    """
async def get_drawing_endpoints(config_getter) -> List[Dict[str, Any]]:
    endpoints_to_try = []

    # 1. LM Arena
    if config_getter("api.enable_lmarena", True):
        lmarena_url = config_getter("api.lmarena_api_url", "https://chat.lmsys.org")
        lmarena_key = config_getter("api.lmarena_api_key", "") 
        endpoints_to_try.append({
            "type": "lmarena",
            "url": lmarena_url,
            "key": lmarena_key,
            "stream": True
        })

    # 2. 自定义渠道 (排除视频渠道)
    custom_channels = data_manager.get_channels()
    for name, channel_info in custom_channels.items():
        c_url = ""
        c_key = ""
        c_model = None
        c_enabled = True
        c_is_video = False
        
        if isinstance(channel_info, dict):
            c_url = channel_info.get("url")
            c_key = channel_info.get("key")
            c_model = channel_info.get("model")
            c_enabled = channel_info.get("enabled", True)
            c_is_video = channel_info.get("is_video", False)
        elif isinstance(channel_info, str) and ":" in channel_info:
            c_url, c_key = channel_info.rsplit(":", 1)
        
        # 跳过视频渠道
        if c_is_video:
            continue
        
        if c_url and c_key and c_enabled:
            c_stream = channel_info.get("stream", False) if isinstance(channel_info, dict) else False
            endpoints_to_try.append({
                "type": f"custom_{name}",
                "url": c_url,
                "key": c_key,
                "model": c_model,
                "stream": c_stream
            })

    # 3. 密钥管理器的 Key (Google / Channel)
    enable_google = config_getter("api.enable_google", True)
    google_api_url = config_getter("api.api_url")

    for key_info in key_manager.get_all_keys():
        if key_info.get('status') != 'active':
            continue
        
        key_type = key_info.get('type')
        if not key_type:
            key_type = 'bailili' if key_info['value'].startswith('sk-') else 'google'

        if key_type == 'google':
            if enable_google:
                endpoints_to_try.append({
                    "type": "google",
                    "url": google_api_url,
                    "key": key_info['value']
                })
        
        elif key_type in custom_channels:
            channel_info = custom_channels[key_type]
            c_enabled = True
            c_url = ""
            c_model = None
            c_is_video = False
            
            if isinstance(channel_info, dict):
                c_url = channel_info.get("url")
                c_model = channel_info.get("model")
                c_enabled = channel_info.get("enabled", True)
                c_is_video = channel_info.get("is_video", False)
            
            # 跳过视频渠道
            if c_is_video:
                continue
            
            if c_enabled and c_url:
                c_stream = channel_info.get("stream", False)
                endpoints_to_try.append({
                    "type": f"custom_{key_type}",
                    "url": c_url,
                    "key": key_info['value'],
                    "model": c_model,
                    "stream": c_stream
                })
    
    return endpoints_to_try

async def process_drawing_api_request(
    payload: Dict[str, Any],
    endpoints: List[Dict[str, Any]],
    image_bytes: Optional[bytes],
    mime_type: Optional[str],
    proxy: Optional[str],
    logger,
    config_getter
) -> Tuple[Optional[str], str]:
    """
    处理绘图API请求，包含失败重试和多渠道轮询逻辑
    Args:
        payload: 请求体
        endpoints: 端点列表
        image_bytes: 原图字节（用于特定API如Doubao）
        mime_type: 原图MIME类型
        proxy: 代理地址
        logger: 日志记录器
        config_getter: 配置获取函数
    
    Returns:
        (image_data_str, error_message)
        image_data_str: 图片数据（Base64或URL），成功时返回
        error_message: 错误信息（如果全部失败）
    """
    last_error = ""
    start_time = datetime.now()

    for i, endpoint in enumerate(endpoints):
        api_url = endpoint["url"]
        api_key = endpoint["key"]
        endpoint_type = endpoint["type"]
        
        logger.info(f"尝试第 {i+1}/{len(endpoints)} 个端点: {endpoint_type} ({api_url})")

        headers = {"Content-Type": "application/json"}
        request_url = api_url

        try:
            current_payload = payload.copy()
            client_proxy = proxy 
            
            is_openai = False
            is_doubao = False
            
            # 判断 API 类型
            if endpoint_type == 'lmarena':
                is_openai = True
                request_url = f"{api_url}" 
                client_proxy = None 
            elif "/chat/completions" in api_url:
                is_openai = True
                request_url = api_url
            elif "/images/generations" in api_url:
                # 火山豆包图片生成 API
                is_doubao = True
                is_openai = False
                request_url = api_url
            elif "generateContent" in api_url:
                is_openai = False
                request_url = f"{api_url}?key={api_key}"
            else:
                logger.warning(f"无法识别的API地址格式: {api_url}，跳过。请检查配置。")
                continue

            # 提取用户文本 prompt (简单提取，用于日志或特定API)
            user_text_prompt = ""
            if "contents" in current_payload and current_payload["contents"]:
                for p in current_payload["contents"][0].get("parts", []):
                    if "text" in p:
                        user_text_prompt = p["text"]
                        if user_text_prompt.startswith("Prompt: "):
                            user_text_prompt = user_text_prompt[8:]
                        break
            
            # 特定 API 格式转换
            if is_doubao:
                headers["Authorization"] = f"Bearer {api_key}"
                model_name = endpoint.get("model", "doubao-seedream-4-5-251128")
                
                doubao_payload = {
                    "model": model_name,
                    "prompt": user_text_prompt,
                    "response_format": "url",
                    "size": "2k",
                    "stream": False,
                    "watermark": False
                }
                
                if image_bytes and mime_type:
                    # 获取 image parts 中的 base64
                    # 尝试从 payload 中恢复原图 base64，或者使用传入的 bytes
                    base64_img = base64.b64encode(image_bytes).decode('utf-8')
                    image_data_url = f"data:{mime_type};base64,{base64_img}"
                    doubao_payload["image"] = image_data_url
                    logger.info(f"构建豆包图生图请求: model={model_name}...")
                else:
                    logger.info(f"构建豆包文生图请求: model={model_name}...")
                
                current_payload = doubao_payload
            
            elif is_openai:
                if api_key:
                    headers["Authorization"] = f"Bearer {api_key}"
                
                # 重新构建 OpenAI 格式的消息
                # 简单起见，如果原 payload 是 Gemini 格式，我们尝试转换
                # 这里假设 payload 就是 Gemini 格式的 {"contents": [{"parts": ...}]}
                
                openai_content = []
                
                parts = payload.get("contents", [{}])[0].get("parts", [])
                for part in parts:
                    if "text" in part:
                        openai_content.append({"type": "text", "text": part["text"]})
                    elif "inline_data" in part:
                        mime = part["inline_data"]["mime_type"]
                        data = part["inline_data"]["data"]
                        openai_content.append({
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{data}"}
                        })

                openai_messages = [
                    {
                        "role": "user",
                        "content": openai_content
                    }
                ]

                model_name = endpoint.get("model")
                if not model_name:
                    # 回退逻辑
                    model_name = config_getter("api.lmarena_model_name", "gemini-pro-vision") if endpoint_type != 'lmarena' else "gemini-3-pro-image-preview"

                openai_payload = {
                    "model": model_name,
                    "messages": openai_messages,
                    "stream": endpoint.get("stream", False),
                }
                current_payload = openai_payload

            logger.info(f"准备向 {endpoint_type} 端点发送请求。")
            
            img_data = None
            use_stream = endpoint.get("stream", False)
            
            if use_stream:
                try:
                    async with httpx.AsyncClient(proxy=client_proxy, timeout=180.0, follow_redirects=True) as client:
                        async with client.stream("POST", request_url, json=current_payload, headers=headers) as response:
                            if response.status_code != 200:
                                raw_body = await response.aread()
                                raise Exception(f"API请求失败, 状态码: {response.status_code} - {raw_body.decode('utf-8', 'ignore')}")

                            async for line in response.aiter_lines():
                                line = line.strip()
                                if not line: continue
                                if line.startswith(':'): continue
                                
                                if line.startswith('data:'):
                                    data_str = line.replace('data:', '').strip()
                                    if data_str == "DONE" or data_str == "[DONE]": break
                                    
                                    try:
                                        response_data = json.loads(data_str)
                                        extracted = await extract_image_data(response_data)
                                        if extracted:
                                            img_data = extracted
                                            logger.info("从SSE流中成功提取图片数据。")
                                            break
                                    except json.JSONDecodeError:
                                        pass
                except Exception as e:
                    logger.error(f"SSE 请求错误: {e}")
                    raise
            
            else:
                try:
                    async with httpx.AsyncClient(proxy=client_proxy, timeout=120.0, follow_redirects=True) as client:
                        response = await client.post(request_url, json=current_payload, headers=headers)
                except httpx.RequestError as e:
                    logger.error(f"httpx.RequestError: {e}")
                    raise

                if response.status_code == 200:
                    data = response.json()
                    img_data = await extract_image_data(data)
                    if not img_data:
                        logger.warning(f"API 响应成功但未提取到图片。")
                        raise Exception(f"API未返回图片")
                else:
                    raise Exception(f"API请求失败, 状态码: {response.status_code} - {response.text}")

            if img_data:
                if endpoint_type != 'lmarena':
                    key_manager.record_key_usage(api_key, True)
                
                elapsed = (datetime.now() - start_time).total_seconds()
                logger.info(f"使用 {endpoint_type} 端点成功生成图片，耗时 {elapsed:.2f}s")
                return img_data, ""

            if not img_data:
                raise Exception("审核不通过，未能从API响应中获取图片数据")

        except Exception as e:
            logger.warning(f"端点 {endpoint_type} 尝试失败: {e}")
            if endpoint_type != 'lmarena':
                is_quota_error = "429" in str(e)
                key_manager.record_key_usage(api_key, False, force_disable=is_quota_error)
            last_error = str(e)
            await asyncio.sleep(1)

    return None, last_error
