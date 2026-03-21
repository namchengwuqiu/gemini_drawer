
import asyncio
import base64
import json
import time
from datetime import datetime
from typing import Tuple, Optional, List, Dict, Any

import httpx
import re
import os

from .utils import extract_all_image_data, safe_json_dumps, download_image
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
    config_getter,
    debug_mode: bool = False
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
            is_tsai = False
            
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
            
            elif is_tsai:
                headers["x-api-key"] = api_key
                if image_bytes and mime_type:
                    request_url = f"{base_url}?endpoint=image_editing"
                    workflow = endpoint.get("model", "rr3")
                    base64_img = base64.b64encode(image_bytes).decode('utf-8')
                    tsai_payload = {
                        "prompt": user_text_prompt,
                        "image": f"data:{mime_type};base64,{base64_img}",
                        "seed": -1
                    }
                else:
                    request_url = f"{base_url}?endpoint=image_generation"
                    workflow = endpoint.get("model", "rr3")
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
                    # 回退逻辑
                    default_model = "gemini-3-pro-image-preview" if endpoint_type == 'lmarena' else "gemini-pro-vision"
                    model_name = config_getter("api.lmarena_model_name", default_model)

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
                                    data_str = line.replace('data:', '').strip()
                                    if data_str == "DONE" or data_str == "[DONE]": break
                                    
                                    if debug_sse_lines is not None:
                                        debug_sse_lines.append(data_str)
                                    
                                    try:
                                        response_data = json.loads(data_str)
                                        extracted = await extract_all_image_data(response_data)
                                        if extracted:
                                            img_data = extracted
                                            logger.info(f"从SSE流中成功提取 {len(extracted)} 张图片数据。")
                                            break
                                        
                                        # 累积 delta.content
                                        if "choices" in response_data and response_data["choices"]:
                                            delta = response_data["choices"][0].get("delta", {})
                                            chunk_content = delta.get("content", "")
                                            if chunk_content:
                                                accumulated_content += chunk_content
                                    except json.JSONDecodeError:
                                        pass
                    
                    # 流结束后：尝试从累积内容中提取
                    if not img_data and accumulated_content:
                        logger.info(f"[图片] SSE流逐chunk未提取到图片，尝试从累积内容中提取 (长度: {len(accumulated_content)})")
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
                    
                    if not img_data and debug_mode and debug_sse_lines:
                        logger.warning(f"[调试模式] SSE流未提取到图片，累积 {len(debug_sse_lines)} 条数据:")
                        for idx, dl in enumerate(debug_sse_lines):
                            logger.warning(f"[调试模式] SSE[{idx}]: {dl[:500]}")
                except Exception as e:
                    logger.error(f"SSE 请求错误: {e}")
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
                    logger.error(f"httpx.RequestError: {e}")
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
                            raise Exception(f"API未返回图片")
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
                raise Exception("审核不通过，未能从API响应中获取图片数据")

        except Exception as e:
            logger.warning(f"端点 {endpoint_type} 尝试失败: {e}")
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
                
                model_name = endpoint.get("model", "doubao-seedance-1-5-pro-251215")
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
                            video_url = content.get("video_url") or content.get("url")
                            if isinstance(content, list) and len(content) > 0:
                                video_url = content[0].get("video_url") or content[0].get("url")
                            
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
                    "model": endpoint.get("model", "video-preview"),
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
                                    data_str = line.replace('data:', '').strip()
                                    if data_str in ["DONE", "[DONE]"]:
                                        break
                                    try:
                                        response_data = json.loads(data_str)
                                        # 快速路径：尝试逐 chunk 提取 base64 数据
                                        extracted = await extract_video_data(response_data)
                                        if extracted and not extracted.startswith("url:"):
                                            # 直接拿到了 base64 数据，无需累积
                                            video_data = extracted
                                            break
                                        
                                        # 累积 delta.content（用于流结束后提取 URL）
                                        if "choices" in response_data and response_data["choices"]:
                                            delta = response_data["choices"][0].get("delta", {})
                                            chunk_content = delta.get("content", "")
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
                    "Authorization": f"Bearer {api_key}",
                }
                try:
                    async with httpx.AsyncClient(proxy=proxy, timeout=120.0, follow_redirects=True) as dl_client:
                        dl_response = await dl_client.get(video_url, headers=dl_headers)
                        if dl_response.status_code == 200 and dl_response.content:
                            video_data = base64.b64encode(dl_response.content).decode('utf-8')
                            logger.info(f"[视频] 视频下载完成，大小: {len(dl_response.content)} 字节")
                        else:
                            # 如果带 Auth 头失败，再尝试不带 Auth 头的纯浏览器请求
                            logger.warning(f"[视频] 带认证头下载失败 (HTTP {dl_response.status_code})，尝试不带认证头...")
                            del dl_headers["Authorization"]
                            dl_response2 = await dl_client.get(video_url, headers=dl_headers)
                            if dl_response2.status_code == 200 and dl_response2.content:
                                video_data = base64.b64encode(dl_response2.content).decode('utf-8')
                                logger.info(f"[视频] 视频下载完成（无认证头），大小: {len(dl_response2.content)} 字节")
                            else:
                                raise Exception(f"下载视频失败: HTTP {dl_response.status_code} / {dl_response2.status_code}")
                except Exception as dl_err:
                    logger.error(f"[视频] 下载视频失败: {dl_err}")
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
            logger.warning(f"[视频] 端点 {endpoint_type} 失败: {e}")
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
