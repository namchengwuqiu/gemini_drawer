
import asyncio
import base64
import json
import time
from datetime import datetime
from typing import Tuple, Optional, List, Dict, Any
from urllib.parse import urlparse

import httpx
import re

from .utils import extract_all_image_data, safe_json_dumps, download_image, extract_text_failure_reason
from .managers import key_manager, data_manager

try:
    from src.common.database.database_model import Images, Messages
except ImportError:
    Images = None
    Messages = None

async def extract_source_image(
    message,
    proxy: Optional[str] = None,
    logger = None,
    ctx = None
) -> Optional[bytes]:
    """
    从消息对象中提取图片（优先回复 > 消息内图片 > @用户头像 > 发送者头像）
    
    Args:
        message: MaiMessages 对象
        proxy: 代理地址
        logger: 日志对象（如果为None则不记录日志）
        ctx: 插件上下文，优先通过官方 message capability 查询历史回复消息
        
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
            if isinstance(seg, dict):
                seg_type = seg.get('type')
                seg_data = seg.get('data')
                binary_data_base64 = seg.get('binary_data_base64')
            else:
                seg_type = getattr(seg, 'type', None)
                seg_data = getattr(seg, 'data', None)
                binary_data_base64 = getattr(seg, 'binary_data_base64', None)

            if seg_type == 'image' or seg_type == 'emoji':
                if isinstance(binary_data_base64, str) and len(binary_data_base64) > 200:
                    try:
                        if logger: logger.info(f"在消息段中找到Base64图片 (类型: {seg_type})。")
                        return base64.b64decode(binary_data_base64)
                    except Exception:
                        if logger: logger.warning(f"无法将类型为 '{seg_type}' 的二进制段解码为图片，已跳过。")
                        continue
                if isinstance(seg_data, dict) and seg_data.get('url'):
                    if logger: logger.info(f"在消息段中找到URL图片 (类型: {seg_type})。")
                    image_bytes = await download_image(seg_data.get('url'), proxy)
                    if image_bytes:
                        return image_bytes
                    if logger: logger.warning(f"消息段URL图片下载失败，继续尝试后续片段 (类型: {seg_type})。")
                    continue
                elif isinstance(seg_data, str) and len(seg_data) > 200:
                    try:
                        if logger: logger.info(f"在消息段中找到Base64图片 (类型: {seg_type})。")
                        return base64.b64decode(seg_data)
                    except Exception:
                        if logger: logger.warning(f"无法将类型为 '{seg_type}' 的段解码为图片，已跳过。")
                        continue
        return None

    async def _fetch_message_via_capability(message_id: Any) -> Optional[dict]:
        if not ctx or not getattr(ctx, 'message', None):
            return None

        normalized_message_id = str(message_id or "").strip()
        if not normalized_message_id:
            return None

        stream_id = (
            str(getattr(message, 'session_id', '') or '').strip()
            or str(getattr(message, 'stream_id', '') or '').strip()
        )
        if not stream_id:
            chat_stream = getattr(message, 'chat_stream', None)
            stream_id = str(getattr(chat_stream, 'stream_id', '') or '').strip()

        return await ctx.message.get_by_id(
            normalized_message_id,
            stream_id=stream_id,
            include_binary_data=True,
        )

    async def _extract_image_from_message_dict(message_dict: dict, source_label: str) -> Optional[bytes]:
        segments = (
            message_dict.get('message_segments')
            or message_dict.get('raw_message')
            or message_dict.get('message_segment')
            or []
        )
        img = await _extract_image_from_segments(segments)
        if img:
            if logger: logger.info(f"从{source_label}中提取到图片")
            return img
        return None

    async def _fetch_mai_message_from_db(message_id: Any):
        """Fetch and deserialize a DB message before the SQLAlchemy session closes."""
        if not Messages:
            return None

        normalized_message_id = str(message_id or "").strip()
        if not normalized_message_id:
            return None

        from src.common.database.database import get_db_session
        from sqlmodel import select
        from src.common.data_models.mai_message_data_model import MaiMessage

        def _fetch():
            with get_db_session(auto_commit=False) as session:
                statement = select(Messages).where(Messages.message_id == normalized_message_id).limit(1)
                db_msg = session.exec(statement).first()
                return MaiMessage.from_db_instance(db_msg) if db_msg else None

        return await asyncio.to_thread(_fetch)

    async def _extract_image_from_mai_message(mai_msg, source_label: str) -> Optional[bytes]:
        from src.common.data_models.message_component_data_model import ImageComponent, EmojiComponent

        for comp in mai_msg.raw_message.components:
            if isinstance(comp, ImageComponent):
                await comp.load_image_binary()
            elif isinstance(comp, EmojiComponent):
                await comp.load_emoji_binary()
            else:
                continue

            if comp.binary_data:
                if logger: logger.info(f"从{source_label}中提取到图片")
                return comp.binary_data
        return None

    # 2. 尝试从回复的消息中提取
    async def _extract_from_reply() -> Optional[bytes]:
        # 情况 A: 递归检查 reply (因为 message.reply 可能本身是 CompatMessage 但不含图)
        if hasattr(message, 'reply') and message.reply:
            if hasattr(message.reply, 'message_segment'):
                img = await extract_source_image(message.reply, proxy, logger, ctx)
                if img: return img
        
        # 情况 B: 官方 message capability 查询历史回复消息
        reply_to_id = getattr(message, 'reply_to', None)
        if not reply_to_id and hasattr(message, 'reply') and message.reply:
            reply_to_id = getattr(message.reply, 'message_id', None)

        if reply_to_id:
            try:
                message_dict = await _fetch_message_via_capability(reply_to_id)
                if isinstance(message_dict, dict):
                    img = await _extract_image_from_message_dict(message_dict, "官方消息能力引用的消息")
                    if img:
                        return img
            except Exception as e:
                if logger: logger.warning(f"Failed to fetch reply message via capability: {e}")

        # 情况 C: DatabaseMessages 对象 (Historical)
        if Messages:
            if reply_to_id:
                try:
                    mai_msg = await _fetch_mai_message_from_db(reply_to_id)
                    if mai_msg:
                        return await _extract_image_from_mai_message(mai_msg, "数据库引用的消息")
                except Exception as e:
                    if logger: logger.warning(f"Failed to fetch reply message from DB: {e}")
        return None

    # 3. 尝试从当前消息中提取
    async def _extract_from_current() -> Optional[bytes]:
        # 情况 A: MaiMessages 对象 (Runtime) - 有 message_segment
        if hasattr(message, 'message_segment'):
            return await _extract_image_from_segments(message.message_segment)
        
        # 情况 B: 当前消息如果也是一个 MaiMessage 或 DatabaseMessages
        if Messages:
            # Check if this is already a db record or mai message
            msg_id = getattr(message, 'message_id', None)
            if msg_id:
                try:
                    mai_msg = await _fetch_mai_message_from_db(msg_id)
                    if mai_msg:
                        return await _extract_image_from_mai_message(mai_msg, "当前消息(数据库回查)")
                except Exception as e:
                    if logger: logger.warning(f"Failed to fetch current message from DB: {e}")
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
            
            if logger:
                segment_preview = [
                    ({'type': s.type, 'data': s.data} if hasattr(s, 'type') else str(s))
                    for s in segments
                ]
                try:
                    logger.debug(f"[调试] 提取@，当前 segments: {safe_json_dumps(segment_preview)}")
                except TypeError:
                    logger.debug(f"[调试] 提取@，当前 segments: {str(segment_preview)[:500]}")
            
            for seg in segments:
                # 检查 type='at'
                if seg.type == 'at':
                    if isinstance(seg.data, dict):
                        qq = seg.data.get('qq') or seg.data.get('user_id') or seg.data.get('id') or seg.data.get('target_user_id')
                        if qq and str(qq) != 'all':
                            return await _download_avatar(str(qq))
                    elif isinstance(seg.data, str) and str(seg.data) != 'all':
                        return await _download_avatar(str(seg.data))
                # 检查 type='text' 中的 @<nick:id> 和 @id
                elif seg.type == 'text' and isinstance(seg.data, str):
                    matches = re.findall(r'@<[^:>]+:([^:>]+)>', seg.data)
                    for user_id in matches:
                        return await _download_avatar(str(user_id))
                    matches = re.findall(r'@(\d{5,11})\b', seg.data)
                    for user_id in matches:
                        return await _download_avatar(str(user_id))
        
        # 情况 B: DatabaseMessages (检查文本中的 @<nick:id> 和 @id)
        text = getattr(message, 'processed_plain_text', '') or getattr(message, 'display_message', '') or ''
        if logger: logger.debug(f"[调试] 提取@，当前纯文本: {text[:500]}")
        at_matches = re.findall(r'@<[^:>]+:([^:>]+)>', text)
        for user_id in at_matches:
             return await _download_avatar(str(user_id))
        at_matches = re.findall(r'@(\d{5,11})\b', text)
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

def build_drawing_endpoints() -> List[Dict[str, Any]]:
    """从渠道配置和渠道 Key 中构建绘图端点列表。"""

    endpoints_to_try = []
    custom_channels = data_manager.get_channels()

    # 1. 渠道内直接保存的 Key（兼容旧数据）
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

    # 2. Key 管理器中的渠道 Key
    for key_info in key_manager.get_all_keys():
        if key_info.get('status') != 'active':
            continue
        
        key_type = key_info.get('type')
        if not key_type:
            key_type = 'bailili' if key_info['value'].startswith('sk-') else 'google'

        if key_type in custom_channels:
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


async def get_drawing_endpoints(config_getter=None) -> List[Dict[str, Any]]:
    """
    获取所有可用的绘图 API 端点。

    config_getter 参数保留给旧调用方；端点已统一改为通过“渠道 + 渠道 Key”配置。
    """

    return build_drawing_endpoints()


async def process_drawing_api_request(
    payload: Dict[str, Any],
    endpoints: List[Dict[str, Any]],
    image_bytes: Optional[bytes],
    mime_type: Optional[str],
    proxy: Optional[str],
    logger,
    config_getter,
    debug_mode: bool = False
) -> Tuple[Optional[List[str]], str]:
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
        (image_data_list, error_message)
        image_data_list: 图片数据列表（Base64或URL），成功时返回
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
            is_tsai = False
            is_gpt_image = False
            
            # 获取模型名称（用于判断特殊模型类型）
            endpoint_model = endpoint.get("model") or ""
            
            # 判断 API 类型
            if endpoint_type == 'lmarena':
                is_openai = True
                request_url = f"{api_url}" 
                client_proxy = None 
            elif endpoint_model and "gpt-image" in endpoint_model.lower():
                # gpt-image 系列模型只支持 /v1/images/generations 和 /v1/images/edits
                is_gpt_image = True
                is_openai = False
                # 自动将 /v1/chat/completions 替换为正确的端点
                base_api_url = api_url.replace("/v1/chat/completions", "").replace("/chat/completions", "").rstrip("/")
                if image_bytes and mime_type:
                    request_url = f"{base_api_url}/v1/images/edits"
                else:
                    request_url = f"{base_api_url}/v1/images/generations"
                logger.info(f"检测到 gpt-image 模型，自动切换端点: {request_url}")
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
            elif endpoint_type.startswith("custom_tsart") or "tavr.top" in api_url.lower() or "tsart.lat" in api_url.lower() or "endpoint=image" in api_url.lower():
                is_tsai = True
                is_openai = False
                is_doubao = False
                base_url = api_url.split("?")[0]
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
            if is_gpt_image:
                # gpt-image-2 使用 /v1/images/generations 或 /v1/images/edits
                headers["Authorization"] = f"Bearer {api_key}"
                model_name = endpoint_model or "gpt-image-2"
                
                if image_bytes and mime_type:
                    # 图生图模式：使用 /v1/images/edits (multipart/form-data)
                    # gpt-image 的 edits 端点需要 multipart 上传
                    del headers["Content-Type"]  # 让 httpx 自动设置 multipart boundary
                    
                    # 构建 multipart 表单数据
                    import io as _io
                    image_file = _io.BytesIO(image_bytes)
                    # 根据 mime_type 确定文件扩展名
                    ext_map = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}
                    file_ext = ext_map.get(mime_type, "png")
                    
                    files = {
                        "image": (f"input.{file_ext}", image_file, mime_type),
                    }
                    form_data = {
                        "model": model_name,
                        "prompt": user_text_prompt,
                        "size": "auto",
                    }
                    
                    logger.info(f"构建 gpt-image 图生图请求 (edits): model={model_name}")
                    
                    # 直接发送 multipart 请求
                    async with httpx.AsyncClient(proxy=client_proxy, timeout=180.0, follow_redirects=True) as client:
                        response = await client.post(request_url, data=form_data, files=files, headers=headers)
                    
                    if response.status_code == 200:
                        data = response.json()
                        img_data = await extract_all_image_data(data)
                        if img_data:
                            if endpoint_type != 'lmarena':
                                key_manager.record_key_usage(api_key, True)
                            elapsed = (datetime.now() - start_time).total_seconds()
                            logger.info(f"使用 {endpoint_type} (gpt-image edits) 端点成功生成图片，耗时 {elapsed:.2f}s")
                            return img_data, ""
                        else:
                            if debug_mode:
                                logger.warning(f"[调试模式] gpt-image edits 响应未提取到图片，原始响应:")
                                logger.warning(f"[调试模式] {json.dumps(data, ensure_ascii=False)[:2000]}")
                            raise Exception("gpt-image edits API未返回图片")
                    else:
                        raise Exception(f"API请求失败, 状态码: {response.status_code} - {response.text}")
                else:
                    # 文生图模式：使用 /v1/images/generations (JSON)
                    gpt_image_payload = {
                        "model": model_name,
                        "prompt": user_text_prompt,
                        "size": "auto",
                    }
                    current_payload = gpt_image_payload
                    logger.info(f"构建 gpt-image 文生图请求 (generations): model={model_name}")
            
            elif is_doubao:
                headers["Authorization"] = f"Bearer {api_key}"
                model_name = endpoint.get("model") or "doubao-seedream-4-5-251128"
                
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
            
            elif is_tsai:
                headers["x-api-key"] = api_key
                if image_bytes and mime_type:
                    request_url = f"{base_url}?endpoint=image_editing"
                    workflow = endpoint.get("model") or "rr3"
                    base64_img = base64.b64encode(image_bytes).decode('utf-8')
                    tsai_payload = {
                        "prompt": user_text_prompt,
                        "workflow": workflow,
                        "image": f"data:{mime_type};base64,{base64_img}",
                        "seed": -1
                    }
                else:
                    request_url = f"{base_url}?endpoint=image_generation"
                    workflow = endpoint.get("model") or "rr3"
                    tsai_payload = {
                        "prompt": user_text_prompt,
                        "workflow": workflow,
                        "seed": -1
                    }
                current_payload = tsai_payload

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
                    model_name = "gemini-pro-vision"

                openai_payload = {
                    "model": model_name,
                    "messages": openai_messages,
                    "stream": endpoint.get("stream", False),
                }
                current_payload = openai_payload

            logger.info(f"准备向 {endpoint_type} 端点发送请求。")
            
            img_data = None
            failure_reason = ""
            use_stream = endpoint.get("stream", False)
            
            # gpt-image、豆包、TS-AI 图像接口不支持当前流式解析路径，强制关闭
            if is_gpt_image or is_doubao or is_tsai:
                use_stream = False
            
            if use_stream:
                try:
                    debug_sse_lines = [] if debug_mode else None
                    accumulated_content = ""
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
                                    data_str = line[5:].strip()
                                    if data_str == "DONE" or data_str == "[DONE]": break
                                    
                                    if debug_sse_lines is not None:
                                        debug_sse_lines.append(data_str)
                                    
                                    try:
                                        response_data = json.loads(data_str)
                                        # 只累积流式正文，避免在半截 base64 chunk 上误提取并截断。
                                        if "choices" in response_data and response_data["choices"]:
                                            choice = response_data["choices"][0]
                                            delta = choice.get("delta", {})
                                            chunk_content = delta.get("content", "")
                                            message = choice.get("message", {})
                                            if not chunk_content and isinstance(message, dict):
                                                chunk_content = message.get("content", "")
                                            if chunk_content:
                                                accumulated_content += chunk_content
                                    except json.JSONDecodeError:
                                        pass
                    
                    # 流结束后：尝试从累积内容中提取
                    if not img_data and accumulated_content:
                        logger.info(f"[图片] SSE流结束，尝试从累积内容中提取图片 (长度: {len(accumulated_content)})")
                        pseudo_response = {
                            "choices": [{
                                "message": {
                                    "content": accumulated_content
                                }
                            }]
                        }
                        img_data = await extract_all_image_data(pseudo_response)
                        if img_data:
                            logger.info(f"从累积内容中成功提取 {len(img_data)} 张图片数据。")
                        else:
                            failure_reason = extract_text_failure_reason(pseudo_response)
                    
                    if not img_data and debug_mode and debug_sse_lines:
                        logger.warning(f"[调试模式] SSE流未提取到图片，累积 {len(debug_sse_lines)} 条数据:")
                        for idx, dl in enumerate(debug_sse_lines):
                            logger.warning(f"[调试模式] SSE[{idx}]: {dl[:500]}")
                except Exception as e:
                    logger.error(f"SSE 请求错误: {type(e).__name__}: {e!r}")
                    raise
            
            else:
                try:
                    if is_tsai:
                        async with httpx.AsyncClient(proxy=client_proxy, timeout=60.0, follow_redirects=True) as client:
                            response = await client.post(request_url, json=current_payload, headers=headers)
                            if response.status_code != 200:
                                raise Exception(f"创建任务失败: {response.status_code} - {response.text}")
                            
                            resp_json = response.json()
                            task_id = resp_json.get("data", {}).get("id")
                            if not task_id:
                                raise Exception(f"未能获取TS-AI任务ID: {resp_json}")
                                
                            poll_url = f"{base_url}?endpoint=task_status&task_id={task_id}"
                            for _ in range(60):
                                await asyncio.sleep(3)
                                poll_resp = await client.get(poll_url, headers=headers)
                                if poll_resp.status_code != 200:
                                    continue
                                    
                                poll_data = poll_resp.json()
                                status = poll_data.get("data", {}).get("status")
                                if status == "completed":
                                    image_url = poll_data["data"]["result"]["image_url"]
                                    img_data = [image_url]
                                    break
                                elif status == "failed":
                                    error_msg = poll_data.get("data", {}).get("error", "未知错误")
                                    raise Exception(f"TS-AI生成失败: {error_msg}")
                            else:
                                raise Exception("TS-AI任务轮询超时")
                    else:
                        async with httpx.AsyncClient(proxy=client_proxy, timeout=120.0, follow_redirects=True) as client:
                            response = await client.post(request_url, json=current_payload, headers=headers)
                except httpx.RequestError as e:
                    logger.error(f"httpx.RequestError: {type(e).__name__}: {e!r}")
                    raise

                if not is_tsai:
                    if response.status_code == 200:
                        data = response.json()
                        img_data = await extract_all_image_data(data)
                        if not img_data:
                            if debug_mode:
                                logger.warning(f"[调试模式] 非流式响应未提取到图片，原始响应:")
                                logger.warning(f"[调试模式] {json.dumps(data, ensure_ascii=False)[:2000]}")
                            else:
                                logger.warning(f"API 响应成功但未提取到图片。")
                            reason = extract_text_failure_reason(data)
                            raise Exception(f"API未返回图片, 原因: {reason or '未知'}")
                    else:
                        error_text = response.text
                        raise Exception(f"API请求失败, 状态码: {response.status_code} - {error_text}")

            if img_data:
                if endpoint_type != 'lmarena':
                    key_manager.record_key_usage(api_key, True)
                
                elapsed = (datetime.now() - start_time).total_seconds()
                logger.info(f"使用 {endpoint_type} 端点成功生成图片，耗时 {elapsed:.2f}s")
                return img_data, ""

            if not img_data:
                if failure_reason:
                    raise Exception(f"API未返回图片, 原因: {failure_reason}")
                raise Exception("审核不通过，未能从API响应中获取图片数据")

        except Exception as e:
            logger.warning(f"端点 {endpoint_type} 尝试失败: {type(e).__name__}: {e}")
            if endpoint_type != 'lmarena':
                is_quota_error = "429" in str(e)
                key_manager.record_key_usage(api_key, False, force_disable=is_quota_error)
            last_error = str(e)
            await asyncio.sleep(1)

    return None, last_error


async def get_video_endpoints(config_getter, logger=None) -> List[Dict[str, Any]]:
    """
    获取视频生成端点列表（只返回 is_video=True 的渠道）
    """
    endpoints_to_try = []
    custom_channels = data_manager.get_channels()
    
    for name, channel_info in custom_channels.items():
        if not isinstance(channel_info, dict):
            continue
        if not channel_info.get("is_video", False):
            continue
        
        c_url = channel_info.get("url")
        c_enabled = channel_info.get("enabled", True)
        c_model = channel_info.get("model")
        c_key = channel_info.get("key")
        
        if c_url and c_enabled:
            if c_key:
                endpoints_to_try.append({
                    "type": f"custom_{name}",
                    "url": c_url,
                    "key": c_key,
                    "model": c_model,
                    "stream": channel_info.get("stream", False)
                })
            
            # 检查 key_manager 中的 keys
            key_manager_keys_count = 0
            for key_info in key_manager.get_all_keys():
                if key_info.get('status') != 'active':
                    continue
                if key_info.get('type') == name:
                    key_manager_keys_count += 1
                    endpoints_to_try.append({
                        "type": f"custom_{name}",
                        "url": c_url,
                        "key": key_info['value'],
                        "model": c_model,
                        "stream": channel_info.get("stream", False)
                    })
                    
            if not c_key and key_manager_keys_count == 0:
                if logger:
                    logger.warning(f"[视频] 渠道 '{name}' 已启用但未找到有效Key (检查了 key_manager 和 data.json)")
    
    return endpoints_to_try


async def process_video_generation(
    prompt: str,
    base64_img: Optional[str],
    mime_type: Optional[str],
    endpoints: List[Dict[str, Any]],
    proxy: Optional[str],
    logger,
    debug_mode: bool = False
) -> Tuple[Optional[str], str]:
    """
    处理视频生成请求，返回 (video_base64_data, error_message)
    
    Args:
        prompt: 视频描述
        base64_img: 可选的 base64 编码图片
        mime_type: 图片 MIME 类型
        endpoints: 视频端点列表
        proxy: 代理地址
        logger: 日志对象
    """
    from .utils import extract_video_data
    
    video_data = None
    last_error = ""
    
    for endpoint in endpoints:
        api_url = endpoint["url"]
        api_key = endpoint["key"]
        endpoint_type = endpoint["type"]
        
        logger.info(f"[视频] 尝试端点: {endpoint_type}")
        
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}"
        }
        
        try:
            # 豆包 API (异步任务模式)
            if "volces.com" in api_url or "/contents/generations/tasks" in api_url:
                doubao_content = [{"type": "text", "text": prompt}]
                if base64_img:
                    doubao_content.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime_type};base64,{base64_img}"}
                    })
                
                model_name = endpoint.get("model") or "doubao-seedance-1-5-pro-251215"
                doubao_payload = {
                    "model": model_name,
                    "content": doubao_content,
                    "duration": -1,  # 自动时长: 模型在 4~12 秒范围内自主选择
                    "resolution": "1080p"
                }
                
                async with httpx.AsyncClient(proxy=proxy, timeout=60.0, follow_redirects=True) as client:
                    response = await client.post(api_url, json=doubao_payload, headers=headers)
                    if response.status_code != 200:
                        raise Exception(f"创建任务失败: {response.status_code} - {response.text}")
                    
                    task_id = response.json().get("id")
                    if not task_id:
                        raise Exception("未获取到任务ID")
                    
                    logger.info(f"[视频] 豆包任务已创建: {task_id}")
                    
                    # 轮询任务状态
                    poll_url = f"{api_url}/{task_id}"
                    for poll_count in range(120):  # 最多10分钟
                        await asyncio.sleep(5)
                        poll_resp = await client.get(poll_url, headers=headers)
                        if poll_resp.status_code != 200:
                            continue
                        
                        poll_data = poll_resp.json()
                        status = poll_data.get("status")
                        
                        if status == "succeeded":
                            content = poll_data.get("content", {})
                            video_url = None
                            if isinstance(content, list) and len(content) > 0 and isinstance(content[0], dict):
                                video_url = content[0].get("video_url") or content[0].get("url")
                            elif isinstance(content, dict):
                                video_url = content.get("video_url") or content.get("url")
                            
                            if video_url:
                                video_resp = await client.get(video_url)
                                if video_resp.status_code == 200:
                                    video_data = base64.b64encode(video_resp.content).decode('utf-8')
                                    logger.info(f"[视频] 豆包视频下载完成")
                            break
                        elif status == "failed":
                            error_msg = poll_data.get("error", {}).get("message", "未知错误")
                            raise Exception(f"任务失败: {error_msg}")
                    else:
                        raise Exception("任务超时")
            
            # OpenAI 格式
            elif "/chat/completions" in api_url:
                content_list = [{"type": "text", "text": prompt}]
                if base64_img:
                    content_list.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime_type};base64,{base64_img}"}
                    })
                
                openai_payload = {
                    "model": endpoint.get("model") or "video-preview",
                    "messages": [{"role": "user", "content": content_list}],
                    "stream": endpoint.get("stream", False),
                    "video_config": {
                        "video_length": 10,
                        "resolution_name": "720p"
                    }
                }

                # current_payload = openai_payload

                # logger.info(f"[视频] OpenAI格式: {safe_json_dumps(current_payload)}")

                use_stream = endpoint.get("stream", False)
                
                async with httpx.AsyncClient(proxy=proxy, timeout=300.0, follow_redirects=True) as client:
                    if use_stream:
                        # 流式模式：累积所有 content，流结束后统一提取
                        accumulated_content = ""
                        async with client.stream("POST", api_url, json=openai_payload, headers=headers) as response:
                            if response.status_code != 200:
                                raw_body = await response.aread()
                                error_msg = raw_body.decode('utf-8', 'ignore')
                                raise Exception(f"API请求失败: {response.status_code} - {error_msg}")
                            
                            async for line in response.aiter_lines():
                                line = line.strip()
                                if not line or line.startswith(':'):
                                    continue
                                if line.startswith('data:'):
                                    data_str = line[5:].strip()
                                    if data_str in ["DONE", "[DONE]"]:
                                        break
                                    try:
                                        response_data = json.loads(data_str)
                                        # 只累积流式正文，避免在半截 base64 chunk 上误提取并截断。
                                        if "choices" in response_data and response_data["choices"]:
                                            choice = response_data["choices"][0]
                                            delta = choice.get("delta", {})
                                            chunk_content = delta.get("content", "")
                                            message = choice.get("message", {})
                                            if not chunk_content and isinstance(message, dict):
                                                chunk_content = message.get("content", "")
                                            if chunk_content:
                                                accumulated_content += chunk_content
                                    except json.JSONDecodeError:
                                        pass
                        
                        # 流结束后：如果还没拿到 video_data，尝试从累积内容中提取
                        if not video_data and accumulated_content:
                            logger.info(f"[视频] 流式响应累积内容长度: {len(accumulated_content)}")
                            if debug_mode:
                                logger.warning(f"[调试模式] 视频流式响应累积内容: {accumulated_content[:2000]}")
                            # 构造一个伪响应对象，用于 extract_video_data
                            pseudo_response = {
                                "choices": [{
                                    "message": {
                                        "content": accumulated_content
                                    }
                                }]
                            }
                            video_data = await extract_video_data(pseudo_response)
                    else:
                        response = await client.post(api_url, json=openai_payload, headers=headers)
                        if response.status_code == 200:
                            data = response.json()
                            video_data = await extract_video_data(data)
                            if not video_data and debug_mode:
                                logger.warning(f"[调试模式] 视频非流式响应未提取到数据，原始响应:")
                                logger.warning(f"[调试模式] {json.dumps(data, ensure_ascii=False)[:2000]}")
                        else:
                            raise Exception(f"API请求失败: {response.status_code} - {response.text}")
            
            # TS-AI 视频生成
            elif "api.tavr.top" in api_url or "api.tsart.lat" in api_url or "tsart.lat" in api_url or "endpoint=video_generation" in api_url:
                base_url = api_url.split("?")[0]
                request_url = f"{base_url}?endpoint=video_generation"
                headers["x-api-key"] = api_key
                
                tsai_video_payload = {
                    "prompt": prompt,
                    "seed": -1
                }
                
                if base64_img and mime_type:
                    tsai_video_payload["mode"] = "i2v"
                    tsai_video_payload["image"] = f"data:{mime_type};base64,{base64_img}"
                else:
                    tsai_video_payload["mode"] = "t2v"
                    tsai_video_payload["width"] = 832
                    tsai_video_payload["height"] = 480
                    
                async with httpx.AsyncClient(proxy=proxy, timeout=60.0, follow_redirects=True) as client:
                    response = await client.post(request_url, json=tsai_video_payload, headers=headers)
                    if response.status_code != 200:
                        raise Exception(f"创建TS-AI视频任务失败: {response.status_code} - {response.text}")
                    
                    task_id = response.json().get("data", {}).get("id")
                    if not task_id:
                        raise Exception(f"未获取到TS-AI视频任务ID: {response.text}")
                        
                    logger.info(f"[视频] TS-AI任务已创建: {task_id}")
                    
                    poll_url = f"{base_url}?endpoint=task_status&task_id={task_id}"
                    for _ in range(120): # 最多10分钟
                        await asyncio.sleep(5)
                        poll_resp = await client.get(poll_url, headers=headers)
                        if poll_resp.status_code != 200:
                            continue
                            
                        poll_data = poll_resp.json()
                        status = poll_data.get("data", {}).get("status")
                        if status == "completed":
                            result_data = poll_data.get("data", {}).get("result", {})
                            video_url = result_data.get("video_url") or result_data.get("image_url")
                            if video_url:
                                video_data = f"url:{video_url}"
                            break
                        elif status == "failed":
                            error_msg = poll_data.get("data", {}).get("error", "未知错误")
                            raise Exception(f"TS-AI视频生成失败: {error_msg}")
                    else:
                        raise Exception("TS-AI视频任务轮询超时")

            # Gemini 格式
            elif "generateContent" in api_url:
                parts = [{"text": prompt}]
                if base64_img:
                    parts.append({"inline_data": {"mime_type": mime_type, "data": base64_img}})
                
                gemini_payload = {"contents": [{"parts": parts}]}
                request_url = f"{api_url}?key={api_key}"
                
                async with httpx.AsyncClient(proxy=proxy, timeout=300.0, follow_redirects=True) as client:
                    response = await client.post(request_url, json=gemini_payload, headers={"Content-Type": "application/json"})
                    if response.status_code == 200:
                        data = response.json()
                        video_data = await extract_video_data(data)
                    else:
                        raise Exception(f"API请求失败: {response.status_code} - {response.text}")
            
            # 如果提取到的是 URL，需要下载视频并转为 base64
            if video_data and video_data.startswith("url:"):
                video_url = video_data[4:]  # 去掉 "url:" 前缀
                logger.info(f"[视频] 正在下载视频: {video_url[:100]}...")
                dl_headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Accept": "video/mp4,video/*,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "Referer": video_url.split("?")[0],
                }
                api_host = urlparse(api_url).hostname
                video_host = urlparse(video_url).hostname
                if api_key and api_host and video_host and api_host == video_host:
                    dl_headers["Authorization"] = f"Bearer {api_key}"
                try:
                    async with httpx.AsyncClient(proxy=proxy, timeout=120.0, follow_redirects=True) as dl_client:
                        dl_response = await dl_client.get(video_url, headers=dl_headers)
                        if dl_response.status_code == 200 and dl_response.content:
                            video_data = base64.b64encode(dl_response.content).decode('utf-8')
                            logger.info(f"[视频] 视频下载完成，大小: {len(dl_response.content)} 字节")
                        else:
                            if "Authorization" in dl_headers:
                                logger.warning(f"[视频] 同域带认证头下载失败 (HTTP {dl_response.status_code})，尝试不带认证头...")
                                dl_headers.pop("Authorization", None)
                                dl_response2 = await dl_client.get(video_url, headers=dl_headers)
                                if dl_response2.status_code == 200 and dl_response2.content:
                                    video_data = base64.b64encode(dl_response2.content).decode('utf-8')
                                    logger.info(f"[视频] 视频下载完成（无认证头），大小: {len(dl_response2.content)} 字节")
                                else:
                                    raise Exception(f"下载视频失败: HTTP {dl_response.status_code} / {dl_response2.status_code}")
                            else:
                                raise Exception(f"下载视频失败: HTTP {dl_response.status_code}")
                except Exception as dl_err:
                    logger.error(f"[视频] 下载视频失败: {type(dl_err).__name__}: {dl_err!r}")
                    video_data = None
                    last_error = f"视频URL获取成功但下载失败: {dl_err}"
            
            if video_data:
                key_manager.record_key_usage(api_key, True)
                return video_data, ""
            else:
                # API 调用成功但未提取到视频数据
                error_msg = f"端点 {endpoint_type} 未返回有效视频数据"
                logger.warning(f"[视频] {error_msg}")
                last_error = error_msg
                
        except Exception as e:
            logger.warning(f"[视频] 端点 {endpoint_type} 失败: {type(e).__name__}: {e}")
            is_quota_error = "429" in str(e)
            key_manager.record_key_usage(api_key, False, force_disable=is_quota_error)
            last_error = str(e)
            await asyncio.sleep(1)
    
    # 所有端点都失败了，记录最终错误
    if not last_error:
        last_error = "所有端点均未返回有效视频数据"
    logger.error(f"[视频] 生成失败: {last_error}")
    return None, last_error


async def send_video_via_napcat(
    video_base64: str,
    group_id: Optional[str],
    user_id: Optional[str],
    napcat_host: str,
    napcat_port: int,
    logger
) -> Tuple[bool, str]:
    """
    通过 NapCat HTTP API 发送视频
    
    Returns:
        (success, error_message)
    """
    video_base64_uri = f"base64://{video_base64}"
    
    if group_id:
        api_url = f"http://{napcat_host}:{napcat_port}/send_group_msg"
        request_data = {"group_id": group_id, "message": [{"type": "video", "data": {"file": video_base64_uri}}]}
    elif user_id:
        api_url = f"http://{napcat_host}:{napcat_port}/send_private_msg"
        request_data = {"user_id": user_id, "message": [{"type": "video", "data": {"file": video_base64_uri}}]}
    else:
        return False, "无法确定发送目标"
    
    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            response = await client.post(api_url, json=request_data)
            if response.status_code == 200:
                result = response.json()
                if result.get("status") == "ok" or result.get("retcode") == 0:
                    logger.info(f"[视频] 发送成功")
                    return True, ""
                else:
                    return False, f"napcat返回错误: {result}"
            else:
                return False, f"HTTP {response.status_code}"
    except Exception as e:
        logger.error(f"[视频] 发送失败: {e}")
        return False, str(e)
