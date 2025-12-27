"""
Gemini Drawer 基础命令模块

本模块定义了插件所有命令的基础类，提供核心功能的抽象和实现：

BaseAdminCommand:
    管理员命令的基类，提供：
    - 管理员权限验证 (通过配置文件中的 admins 列表)
    - 统一的命令执行流程
    - 抽象方法 handle_admin_command() 供子类实现具体逻辑

BaseDrawCommand:
    绘图命令的基类，提供完整的绘图流程控制：
    - 图片获取：支持回复图片、@用户头像、消息中的图片、发送者头像
    - API 调用：支持多渠道轮询、自动重试、流式/非流式请求
    - Key 管理：自动记录使用情况、错误计数、自动禁用失效 Key
    - 消息通知：开始提示、成功通知（戳一戳/文字）、失败提示
    - 状态消息撤回：可配置自动撤回过程中的状态提示
    - 代理支持：可配置 HTTP 代理

关键方法：
- get_source_image_bytes(): 获取源图片（优先回复 > @用户 > 消息图片 > 头像）
- get_prompt(): 抽象方法，获取绘图提示词，由子类实现
- execute(): 主执行流程，处理所有绘图逻辑
- _recall_status_messages(): 撤回状态消息
- _notify_success(): 发送成功通知
"""
import asyncio
import json
import re
import time
import base64
from datetime import datetime
from abc import ABC, abstractmethod
from typing import Tuple, Optional, List, Dict, Any

import httpx
from src.plugin_system import BaseCommand
from src.plugin_system.apis import message_api, send_api, chat_api
from src.common.logger import get_logger

from .utils import (
    download_image, convert_if_gif, get_image_mime_type, 
    safe_json_dumps, extract_image_data
)
from .managers import key_manager, data_manager

logger = get_logger("gemini_drawer")

class BaseAdminCommand(BaseCommand, ABC):
    permission: str = "owner"

    async def execute(self) -> Tuple[bool, Optional[str], bool]:
        if not self.get_config("general.enable_gemini_drawer", True):
            return True, "Plugin disabled", False
        
        user_id_from_msg = getattr(self.message.message_info.user_info, 'user_id', None)
        if not user_id_from_msg:
            logger.warning("无法从 self.message.message_info.user_info 中获取 user_id")
            await self.send_text("无法获取用户信息，操作失败。")
            return False, "无法获取用户信息", True

        str_user_id = str(user_id_from_msg)
        admin_list = self.get_config("general.admins", [])
        str_admin_list = [str(admin) for admin in admin_list]
        
        if str_user_id not in str_admin_list:
            await self.send_text("❌ 仅管理员可用")
            return True, "无权限访问", True
        
        return await self.handle_admin_command()

    @abstractmethod
    async def handle_admin_command(self) -> Tuple[bool, Optional[str], bool]:
        raise NotImplementedError

class BaseDrawCommand(BaseCommand, ABC):
    permission: str = "user"
    allow_text_only: bool = False

    def _get_current_chat_id(self) -> Optional[str]:
        """获取当前聊天的 chat_id（使用 stream_id）"""
        try:
            chat_stream = self.message.chat_stream
            if chat_stream:
                stream_id = getattr(chat_stream, 'stream_id', None)
                if stream_id:
                    logger.debug(f"使用 stream_id 作为 chat_id: {stream_id}")
                    return stream_id
                
                group_info = getattr(chat_stream, 'group_info', None)
                if group_info and hasattr(group_info, 'group_id') and group_info.group_id:
                    chat_id = f"{chat_stream.platform}:{group_info.group_id}"
                    logger.debug(f"使用 group_id 构造 chat_id: {chat_id}")
                    return chat_id
                    
                user_info = getattr(chat_stream, 'user_info', None)
                if user_info and hasattr(user_info, 'user_id') and user_info.user_id:
                    chat_id = f"{chat_stream.platform}:{user_info.user_id}"
                    logger.debug(f"使用 user_id 构造 chat_id: {chat_id}")
                    return chat_id
            return None
        except Exception as e:
            logger.warning(f"获取 chat_id 失败: {e}")
            return None

    async def _safe_recall(self, message_ids: List[str]) -> int:
        """安全地撤回消息列表，返回成功撤回的数量"""
        recalled_count = 0
        for mid in message_ids:
            try:
                result = await self.send_command(
                    "DELETE_MSG",
                    {"message_id": str(mid)},
                    display_message="",
                    storage_message=False
                )
                if result:
                    recalled_count += 1
                    logger.debug(f"成功撤回消息: {mid}")
            except Exception as e:
                logger.warning(f"撤回消息失败 {mid}: {e}")
        return recalled_count

    async def _notify_success(self, elapsed: float) -> None:
        """成功生成后通知用户"""
        if self.get_config("behavior.reply_with_image", True):
            logger.debug("[通知] 已启用回复图片模式，跳过额外通知")
            return
        
        use_poke = self.get_config("behavior.success_notify_poke", True)
        
        if use_poke:
            try:
                user_id = None
                if hasattr(self.message, 'message_info') and self.message.message_info:
                    user_info = getattr(self.message.message_info, 'user_info', None)
                    if user_info:
                        user_id = getattr(user_info, 'user_id', None)
                
                if user_id:
                    logger.info(f"[通知] 使用戳一戳通知用户 {user_id}")
                    await self.send_command(
                        "SEND_POKE",
                        {"qq_id": str(user_id)},
                        display_message=f"✅ 生成完成 ({elapsed:.2f}s)",
                        storage_message=False
                    )
                    return
            except Exception as e:
                logger.warning(f"[通知] 戳一戳失败，回退到文本通知: {e}")
        
        await self.send_text(f"✅ 生成完成 ({elapsed:.2f}s)")

    async def _notify_start(self) -> None:
        """开始处理时通知用户：使用戳一戳"""
        try:
            user_id = None
            if hasattr(self.message, 'message_info') and self.message.message_info:
                user_info = getattr(self.message.message_info, 'user_info', None)
                if user_info:
                    user_id = getattr(user_info, 'user_id', None)
            
            if user_id:
                logger.info(f"[通知] 使用戳一戳通知用户开始处理 {user_id}")
                await self.send_command(
                    "SEND_POKE",
                    {"qq_id": str(user_id)},
                    display_message="🎨 开始处理...",
                    storage_message=False
                )
                return
        except Exception as e:
            logger.warning(f"[通知] 戳一戳失败: {e}")

    async def get_source_image_bytes(self) -> Optional[bytes]:
        proxy = self.get_config("proxy.proxy_url") if self.get_config("proxy.enable") else None

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
                        logger.info(f"在消息段中找到URL图片 (类型: {seg.type})。")
                        return await download_image(seg.data.get('url'), proxy)
                    elif isinstance(seg.data, str) and len(seg.data) > 200:
                        try:
                            logger.info(f"在消息段中找到Base64图片 (类型: {seg.type})。")
                            return base64.b64decode(seg.data)
                        except Exception:
                            logger.warning(f"无法将类型为 '{seg.type}' 的段解码为图片，已跳过。")
                            continue
            return None

        image_bytes = await _extract_image_from_segments(self.message.message_segment)
        if image_bytes:
            return image_bytes

        segments = self.message.message_segment
        if hasattr(segments, 'type') and segments.type == 'seglist':
            segments = segments.data
        if not isinstance(segments, list):
            segments = [segments]
        
        for seg in segments:
            if seg.type == 'text' and '@' in seg.data:
                match = re.search(r'(\d+)', seg.data)
                if match:
                    mentioned_user_id = match.group(1)
                    logger.info(f"在消息中找到@提及用户 {mentioned_user_id}，获取其头像。")
                    return await download_image(f"https://q1.qlogo.cn/g?b=qq&nk={mentioned_user_id}&s=640", proxy)

        if self.allow_text_only:
            logger.info("允许纯文本模式且未找到图片，跳过自动获取头像。")
            return None

        logger.info("未找到图片、Emoji或@提及，回退到发送者头像。")
        user_id = self.message.message_info.user_info.user_id
        return await download_image(f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640", proxy)

    @abstractmethod
    async def get_prompt(self) -> Optional[str]:
        raise NotImplementedError

    async def execute(self) -> Tuple[bool, Optional[str], bool]:
        if not self.get_config("general.enable_gemini_drawer", True):
            return True, "Plugin disabled", False
        
        # 检查管理员专用模式
        if self.get_config("behavior.admin_only_mode", False):
            user_id_from_msg = getattr(self.message.message_info.user_info, 'user_id', None)
            if user_id_from_msg:
                str_user_id = str(user_id_from_msg)
                admin_list = self.get_config("general.admins", [])
                str_admin_list = [str(admin) for admin in admin_list]
                
                if str_user_id not in str_admin_list:
                    await self.send_text("⚠️ 管理员已关闭绘图功能")
                    return True, "管理员专用模式", True
        
        start_time = datetime.now()
        status_msg_start_time = time.time()

        prompt = await self.get_prompt()
        if not prompt:
            return True, "无效的Prompt", True

        await self._notify_start()
        image_bytes = await self.get_source_image_bytes()
        
        if not image_bytes and not self.allow_text_only:
            await self.send_text("❌ 未找到可供处理的图片或图片处理失败。" )
            return True, "缺少图片或处理失败", True
        
        parts = []
        if image_bytes:
            image_bytes = convert_if_gif(image_bytes)
            base64_img = base64.b64encode(image_bytes).decode('utf-8')
            mime_type = get_image_mime_type(image_bytes)
            parts.append({"inline_data": {"mime_type": mime_type, "data": base64_img}})
        
        parts.append({"text": prompt})

        payload = {
            "contents": [{"parts": parts}],
            "safetySettings": [
                {
                    "category": "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                    "threshold": "BLOCK_NONE"
                }
            ]
        }

        endpoints_to_try = []

        if self.get_config("api.enable_lmarena", True):
            lmarena_url = self.get_config("api.lmarena_api_url", "https://chat.lmsys.org")
            lmarena_key = self.get_config("api.lmarena_api_key", "") 
            endpoints_to_try.append({
                "type": "lmarena",
                "url": lmarena_url,
                "key": lmarena_key,
                "stream": True
            })

        custom_channels = data_manager.get_channels()
        for name, channel_info in custom_channels.items():
            c_url = ""
            c_key = ""
            c_model = None
            c_enabled = True
            
            if isinstance(channel_info, dict):
                c_url = channel_info.get("url")
                c_key = channel_info.get("key")
                c_model = channel_info.get("model")
                c_enabled = channel_info.get("enabled", True)
            elif isinstance(channel_info, str) and ":" in channel_info:
                c_url, c_key = channel_info.rsplit(":", 1)
            
            if c_url and c_key and c_enabled:
                c_stream = channel_info.get("stream", False) if isinstance(channel_info, dict) else False
                endpoints_to_try.append({
                    "type": f"custom_{name}",
                    "url": c_url,
                    "key": c_key,
                    "model": c_model,
                    "stream": c_stream
                })

        enable_google = self.get_config("api.enable_google", True)

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
                        "url": self.get_config("api.api_url"),
                        "key": key_info['value']
                    })
            
            elif key_type in custom_channels:
                channel_info = custom_channels[key_type]
                c_enabled = True
                c_url = ""
                c_model = None
                
                if isinstance(channel_info, dict):
                    c_url = channel_info.get("url")
                    c_model = channel_info.get("model")
                    c_enabled = channel_info.get("enabled", True)
                
                if c_enabled and c_url:
                    c_stream = channel_info.get("stream", False)
                    endpoints_to_try.append({
                        "type": f"custom_{key_type}",
                        "url": c_url,
                        "key": key_info['value'],
                        "model": c_model,
                        "stream": c_stream
                    })

        if not endpoints_to_try:
            await self.send_text("❌ 未配置任何API密钥或端点。" )
            return True, "无可用密钥或端点", True

        last_error = ""
        proxy = self.get_config("proxy.proxy_url") if self.get_config("proxy.enable") else None

        for i, endpoint in enumerate(endpoints_to_try):
            api_url = endpoint["url"]
            api_key = endpoint["key"]
            endpoint_type = endpoint["type"]
            
            logger.info(f"尝试第 {i+1}/{len(endpoints_to_try)} 个端点: {endpoint_type} ({api_url})")

            headers = {"Content-Type": "application/json"}
            request_url = api_url

            try:
                current_payload = payload 
                client_proxy = proxy 
                
                is_openai = False
                
                is_doubao = False
                
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

                # 提取用户文本 prompt
                user_text_prompt = ""
                for p in parts:
                    if "text" in p:
                        user_text_prompt = p["text"]
                        break
                
                if is_doubao:
                    # 火山豆包图片生成 API
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
                    
                    # 如果有图片，添加到请求中（图生图模式）
                    if image_bytes:
                        # 豆包支持 data URL 格式的图片
                        image_data_url = f"data:{mime_type};base64,{base64_img}"
                        doubao_payload["image"] = image_data_url
                        logger.info(f"构建豆包图生图请求: model={model_name}, prompt={user_text_prompt[:50]}...")
                    else:
                        logger.info(f"构建豆包文生图请求: model={model_name}, prompt={user_text_prompt[:50]}...")
                    
                    current_payload = doubao_payload
                
                elif is_openai:
                    if api_key:
                        headers["Authorization"] = f"Bearer {api_key}"
                    
                    openai_messages = [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": user_text_prompt
                                }
                            ]
                        },
                    ]
                    
                    if image_bytes:
                        openai_messages[0]["content"].append({
                            "type": "image_url",
                            "image_url": { "url": f"data:{mime_type};base64,{base64_img}" }
                        })

                    model_name = endpoint.get("model")
                    if not model_name:
                        model_name = self.get_config("api.lmarena_model_name", "gemini-pro-vision") if endpoint_type != 'lmarena' else "gemini-3-pro-image-preview"

                    openai_payload = {
                        "model": model_name,
                        "messages": openai_messages,
                        "stream": endpoint.get("stream", False),
                    }
                    current_payload = openai_payload

                logger.info(f"准备向 {endpoint_type} 端点发送请求。URL: {request_url}, Payload: {safe_json_dumps(current_payload)}")
                
                img_data = None
                use_stream = endpoint.get("stream", False)
                
                if use_stream:
                    try:
                        async with httpx.AsyncClient(proxy=client_proxy, timeout=180.0) as client:
                            async with client.stream("POST", request_url, json=current_payload, headers=headers) as response:
                                if response.status_code != 200:
                                    raw_body = await response.aread()
                                    raise Exception(f"API请求失败, 状态码: {response.status_code} - {raw_body.decode('utf-8', 'ignore')}")

                                async for line in response.aiter_lines():
                                    line = line.strip()
                                    if not line:
                                        continue
                                    if line.startswith(':'):
                                        continue
                                    
                                    if line.startswith('data:'):
                                        data_str = line.replace('data:', '').strip()
                                        if data_str == "DONE" or data_str == "[DONE]":
                                            break
                                        
                                        try:
                                            response_data = json.loads(data_str)
                                            extracted_data = await extract_image_data(response_data)
                                            if extracted_data:
                                                img_data = extracted_data
                                                logger.info("从SSE流中成功提取图片数据。")
                                                break
                                        except json.JSONDecodeError:
                                            pass
                    except httpx.RequestError as e:
                        logger.error(f"SSE 请求错误: {e}")
                        raise
                    except Exception as e:
                        logger.error(f"SSE 流处理失败: {e}")
                        raise
                
                else:
                    try:
                        async with httpx.AsyncClient(proxy=client_proxy, timeout=120.0) as client:
                            response = await client.post(request_url, json=current_payload, headers=headers)
                    except httpx.RequestError as e:
                        logger.error(f"httpx.RequestError for endpoint {endpoint_type} ({request_url}): {e}")
                        raise

                    if response.status_code == 200:
                        data = response.json()
                        img_data = await extract_image_data(data)
                        if not img_data:
                            logger.warning(f"API 响应成功但未提取到图片。响应: {json.dumps(data, indent=2, ensure_ascii=False)}")
                            raise Exception(f"API未返回图片, 原因: {data.get('candidates', [{}])[0].get('finishReason', '未知')}")
                    else:
                        raise Exception(f"API请求失败, 状态码: {response.status_code} - {response.text}")

                if img_data:
                    if endpoint_type != 'lmarena':
                        key_manager.record_key_usage(api_key, True)
                    
                    elapsed = (datetime.now() - start_time).total_seconds()
                    logger.info(f"使用 {endpoint_type} 端点成功生成图片，耗时 {elapsed:.2f}s")
                    
                    try:
                        stream_id = None
                        if hasattr(self.message, 'chat_stream') and self.message.chat_stream:
                            stream_info = chat_api.get_stream_info(self.message.chat_stream)
                            stream_id = stream_info.get('stream_id')

                        if stream_id:
                            image_to_send_b64 = None
                            if img_data.startswith(('http://', 'https')):
                                image_bytes = await download_image(img_data, proxy)
                                if image_bytes:
                                    image_to_send_b64 = base64.b64encode(image_bytes).decode('utf-8')
                            elif 'base64,' in img_data:
                                image_to_send_b64 = img_data.split('base64,')[1]
                            else:
                                image_to_send_b64 = img_data
                            
                            if image_to_send_b64:
                                reply_with_image = self.get_config("behavior.reply_with_image", True)
                                trigger_msg = None
                                
                                if reply_with_image:
                                    try:
                                        from src.common.data_models.database_data_model import DatabaseMessages
                                        msg_info = self.message.message_info
                                        user_info = msg_info.user_info
                                        group_info = getattr(msg_info, 'group_info', None)
                                        chat_stream = self.message.chat_stream
                                        
                                        trigger_msg = DatabaseMessages(
                                            message_id=msg_info.message_id,
                                            time=msg_info.time,
                                            chat_id=self._get_current_chat_id() or "",
                                            processed_plain_text=self.message.processed_plain_text or self.message.raw_message,
                                            user_id=user_info.user_id if user_info else "",
                                            user_nickname=user_info.user_nickname if user_info else "",
                                            user_cardname=getattr(user_info, 'user_cardname', None) if user_info else None,
                                            user_platform=user_info.platform if user_info else "",
                                            chat_info_group_id=group_info.group_id if group_info else None,
                                            chat_info_group_name=group_info.group_name if group_info else None,
                                            chat_info_group_platform=getattr(group_info, 'group_platform', None) if group_info else None,
                                            chat_info_stream_id=chat_stream.stream_id if chat_stream else "",
                                            chat_info_platform=chat_stream.platform if chat_stream else "",
                                            chat_info_user_id=user_info.user_id if user_info else "",
                                            chat_info_user_nickname=user_info.user_nickname if user_info else "",
                                            chat_info_user_cardname=getattr(user_info, 'user_cardname', None) if user_info else None,
                                            chat_info_user_platform=user_info.platform if user_info else "",
                                        )
                                    except Exception as e:
                                        logger.warning(f"构造触发消息失败: {e}，将使用普通发送模式")
                                        trigger_msg = None
                                
                                await send_api.image_to_stream(
                                    image_base64=image_to_send_b64,
                                    stream_id=stream_id,
                                    set_reply=trigger_msg is not None,
                                    reply_message=trigger_msg,
                                    storage_message=False
                                )
                                
                                await self._notify_success(elapsed)
                            else:
                                raise Exception("图片下载或转换失败")
                        else:
                            raise Exception("无法从当前消息中确定stream_id")
                    except Exception as e:
                        logger.error(f"发送图片失败: {e}")
                        await self.send_text("❌ 图片发送失败。" )

                    await self._recall_status_messages(status_msg_start_time)
                    return True, "绘图成功", True 

                if not img_data:
                    raise Exception("审核不通过，未能从API响应中获取图片数据")

            except Exception as e:
                logger.warning(f"端点 {endpoint_type} 尝试失败: {e}")
                if endpoint_type != 'lmarena':
                    is_quota_error = "429" in str(e)
                    key_manager.record_key_usage(api_key, False, force_disable=is_quota_error)
                last_error = str(e)
                await asyncio.sleep(1)

        elapsed = (datetime.now() - start_time).total_seconds()
        fail_msg = f"❌ 生成失败 ({elapsed:.2f}s, {len(endpoints_to_try)}次尝试)\n最终错误: {last_error}"
        fail_msg_send_time = time.time()
        await self.send_text(fail_msg)
        asyncio.create_task(self._delayed_recall_fail_message(fail_msg_send_time, fail_msg))
        await self._recall_status_messages(status_msg_start_time)
        return True, "所有尝试均失败", True

    async def _delayed_recall_fail_message(self, fail_msg_send_time: float, fail_msg_content: str) -> None:
        try:
            await asyncio.sleep(5)
            chat_id = self._get_current_chat_id()
            if not chat_id: return
            await asyncio.sleep(1)
            current_time = time.time()
            bot_messages = message_api.get_messages_by_time_in_chat(
                chat_id=chat_id,
                start_time=fail_msg_send_time - 2,
                end_time=current_time + 5,
                limit=10,
                limit_mode="latest",
                filter_mai=False
            )
            for msg in bot_messages:
                content = getattr(msg, 'processed_plain_text', '')
                msg_id = getattr(msg, 'message_id', None)
                msg_time = getattr(msg, 'time', 0)
                if content.startswith("❌ 生成失败") and msg_time >= fail_msg_send_time - 2:
                    if msg_id and not str(msg_id).startswith('send_api_'):
                        await self._safe_recall([str(msg_id)])
                        return
        except Exception: pass

    async def _recall_status_messages(self, status_msg_start_time: float) -> None:
        auto_recall = self.get_config("behavior.auto_recall_status", True)
        if not auto_recall: return
        
        try:
            chat_id = self._get_current_chat_id()
            if not chat_id: return
            await asyncio.sleep(2)
            current_time = time.time()
            bot_messages = message_api.get_messages_by_time_in_chat(
                chat_id=chat_id,
                start_time=status_msg_start_time - 5,
                end_time=current_time + 5,
                limit=20,
                limit_mode="latest",
                filter_mai=False
            )
            status_prefixes = ("戳一戳", "✅ ")
            to_recall = []
            for msg in bot_messages:
                msg_time = getattr(msg, 'time', 0)
                content = getattr(msg, 'processed_plain_text', '')
                msg_id = getattr(msg, 'message_id', None)
                if msg_time >= status_msg_start_time - 1:
                    if content.startswith(status_prefixes):
                        if msg_id and not str(msg_id).startswith('send_api_'):
                            to_recall.append(str(msg_id))
            if to_recall:
                await self._safe_recall(to_recall)
        except Exception: pass