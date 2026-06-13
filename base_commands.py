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
from maibot_sdk.compat.base import BaseCommand
import logging

from .utils import (
    download_image, convert_if_gif, get_image_mime_type,
    safe_json_dumps, extract_image_data, extract_all_image_data, extract_video_data,
    extract_text_failure_reason
)

from .managers import key_manager
from .draw_logic import build_drawing_endpoints, extract_source_image

logger = logging.getLogger("plugin.gemini_drawer")

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

    async def _send_forward_via_ctx(self, nodes_to_send: list) -> bool:
        """将旧格式的转发节点通过原生 ctx.send.forward() 发送

        旧格式: [(user_id, nickname, [(ReplyContentType, content), ...]), ...]
        新格式: [{"user_id": "0", "nickname": name, "segments": [{"type": "text", "content": text}]}, ...]
        """
        try:
            if not hasattr(self, 'ctx') or not self.ctx:
                # 降级为纯文本
                all_text = "\n".join(
                    "\n".join(seg[1] for seg in node[2] if seg[1])
                    for node in nodes_to_send
                )
                await self.send_text(all_text)
                return True

            messages = []
            for node in nodes_to_send:
                _, nickname, segments = node
                text_parts = []
                for seg in segments:
                    # seg = (ReplyContentType, content)
                    text_parts.append(str(seg[1]))
                messages.append({
                    "user_id": "0",
                    "nickname": nickname,
                    "segments": [{"type": "text", "content": "\n".join(text_parts)}]
                })

            stream_id = self._get_stream_id()
            if stream_id:
                await self.ctx.send.forward(messages, stream_id)
                return True
            else:
                # 降级
                all_text = "\n".join(m["segments"][0]["content"] for m in messages)
                await self.send_text(all_text)
                return True
        except Exception as e:
            logger.warning(f"转发消息失败，降级为纯文本: {e}")
            all_text = "\n".join(
                "\n".join(seg[1] for seg in node[2] if seg[1])
                for node in nodes_to_send
            )
            await self.send_text(all_text)
            return True

class BaseDrawCommand(BaseCommand, ABC):
    permission: str = "user"
    allow_text_only: bool = False

    def _get_current_chat_id(self) -> Optional[str]:
        """获取当前聊天的 chat_id（优先使用框架注入的 stream_id）"""
        # 优先使用框架注入的 _stream_id
        if self._stream_id:
            return self._stream_id
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

    def _get_current_group_id(self) -> Optional[str]:
        """获取当前群 ID"""
        try:
            chat_stream = self.message.chat_stream
            if chat_stream:
                group_info = getattr(chat_stream, 'group_info', None)
                if group_info and hasattr(group_info, 'group_id') and group_info.group_id:
                    return str(group_info.group_id)
            return None
        except Exception as e:
            logger.warning(f"获取 group_id 失败: {e}")
            return None

    async def _safe_recall(self, message_ids: List[str]) -> int:
        """安全地撤回消息列表，返回成功撤回的数量

        使用 NapCat 适配器的跨插件 API: adapter.napcat.message.delete_msg
        """
        recalled_count = 0
        for mid in message_ids:
            try:
                if hasattr(self, 'ctx') and self.ctx:
                    resp = await self.ctx.api.call(
                        "adapter.napcat.message.delete_msg",
                        message_id=str(mid)
                    )
                    if resp is not None and not (isinstance(resp, dict) and resp.get("success") is False):
                        recalled_count += 1
                        logger.debug(f"成功撤回消息: {mid}")
                    else:
                        logger.debug(f"撤回消息未成功: {mid}")
                else:
                    logger.debug(f"跳过撤回消息 {mid}（ctx 不可用）")
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
            poke_ok = await self._send_poke_via_napcat()
            if poke_ok:
                return

        await self.send_text(f"✅ 生成完成 ({elapsed:.2f}s)")

    def get_image_caption(self) -> Optional[str]:
        """子类可重写此方法，返回要与图片一起发送的文字说明"""
        return None

    async def _notify_start(self) -> None:
        """开始处理时通知用户：使用戳一戳"""
        poke_ok = await self._send_poke_via_napcat()
        if not poke_ok:
            await self.send_text("🎨 开始处理...")

    async def _send_poke_via_napcat(self) -> bool:
        """通过 NapCat 适配器发送戳一戳，返回是否成功

        使用跨插件 API: adapter.napcat.message.send_poke
        参考: https://github.com/TAIY2020/smart_poke_plugin
        """
        try:
            if not hasattr(self, 'ctx') or not self.ctx:
                return False

            user_id = None
            group_id = None
            if hasattr(self.message, 'message_info') and self.message.message_info:
                user_info = getattr(self.message.message_info, 'user_info', None)
                if user_info:
                    user_id = getattr(user_info, 'user_id', None)

            if not user_id:
                return False

            # 获取 group_id
            if hasattr(self.message, 'message_info') and self.message.message_info:
                g_info = getattr(self.message.message_info, 'group_info', None)
                if g_info:
                    group_id = getattr(g_info, 'group_id', None)

            call_kwargs = {"user_id": int(user_id)}
            if group_id:
                call_kwargs["group_id"] = int(group_id)
                call_kwargs["target_id"] = int(user_id)

            resp = await self.ctx.api.call(
                "adapter.napcat.message.send_poke", **call_kwargs
            )

            if resp is None:
                logger.debug("[戳一戳] send_poke 无响应")
                return False
            if isinstance(resp, dict) and resp.get("success") is False:
                logger.debug(f"[戳一戳] send_poke 失败: {resp.get('error')}")
                return False

            logger.info(f"[戳一戳] 已戳用户 {user_id}")
            return True
        except Exception as e:
            logger.warning(f"[戳一戳] 失败，回退到文本通知: {e}")
            return False

    async def get_source_image_bytes(self) -> Optional[bytes]:
        proxy = self.get_config("proxy.proxy_url") if self.get_config("proxy.enable") else None

        # 使用 draw_logic.py 中的共享逻辑
        image_bytes = await extract_source_image(self.message, proxy, logger, getattr(self, 'ctx', None))
        if image_bytes:
            return image_bytes

        if self.allow_text_only:
            logger.info("允许纯文本模式且未找到图片，跳过自动获取头像。")
            return None

        # 兜底逻辑：BaseDrawCommand 特有的行为（Action 不使用这个兜底）
        # 如果以上都没找到图片，使用发送者头像
        logger.info("未找到图片、Emoji或@提及，回退到发送者头像。")
        user_id = self.message.message_info.user_info.user_id
        return await download_image(f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640", proxy)

    async def get_multiple_source_images(self, min_count: int = 2) -> List[bytes]:
        """
        获取多张源图片
        来源优先级：回复消息中的图片 > 当前消息中的图片 > @提及用户的头像
        返回图片字节列表
        """
        proxy = self.get_config("proxy.proxy_url") if self.get_config("proxy.enable") else None
        images = []

        async def _extract_images_from_segments(segments) -> List[bytes]:
            """从消息段中提取所有图片"""
            extracted = []
            if hasattr(segments, 'type') and segments.type == 'seglist':
                segments = segments.data
            if not isinstance(segments, list):
                segments = [segments]

            for seg in segments:
                if seg.type == 'image' or seg.type == 'emoji':
                    if isinstance(seg.data, dict) and seg.data.get('url'):
                        logger.info(f"[多图] 在消息段中找到URL图片 (类型: {seg.type})。")
                        img_bytes = await download_image(seg.data.get('url'), proxy)
                        if img_bytes:
                            extracted.append(img_bytes)
                    elif isinstance(seg.data, str) and len(seg.data) > 200:
                        try:
                            logger.info(f"[多图] 在消息段中找到Base64图片 (类型: {seg.type})。")
                            extracted.append(base64.b64decode(seg.data))
                        except Exception:
                            continue
            return extracted

        # 1. 从回复消息中提取图片
        if hasattr(self.message, 'reply') and self.message.reply:
            reply_msg = self.message.reply
            if hasattr(reply_msg, 'message_segment') and reply_msg.message_segment:
                logger.info("[多图] 尝试从回复消息中提取图片...")
                reply_images = await _extract_images_from_segments(reply_msg.message_segment)
                images.extend(reply_images)
                logger.info(f"[多图] 从回复消息中提取到 {len(reply_images)} 张图片")

        # 2. 从当前消息中提取图片
        segments = self.message.message_segment
        current_images = await _extract_images_from_segments(segments)
        images.extend(current_images)

        # 准备处理 @ 提及
        if hasattr(segments, 'type') and segments.type == 'seglist':
            segments = segments.data
        if not isinstance(segments, list):
            segments = [segments]

        # 3. 收集 @ 提及的用户头像
        mentioned_users = []
        for seg in segments:
            if seg.type == 'text' and isinstance(seg.data, str) and '@' in seg.data:
                # 提取所有 @ 的用户 ID
                # 匹配标准 @123456
                for match in re.finditer(r'@(\d{5,11})\b', seg.data):
                    mentioned_users.append(match.group(1))
                # 匹配特殊格式 @<Name:123456>
                for match in re.finditer(r'@<[^>]+:(\d+)>', seg.data):
                    mentioned_users.append(match.group(1))
            elif seg.type == 'at':
                # 处理 at 类型的消息段
                if isinstance(seg.data, dict):
                     # 尝试多种可能的键名
                    uid = seg.data.get('qq') or seg.data.get('user_id') or seg.data.get('id') or seg.data.get('target_user_id')
                    if uid:
                        mentioned_users.append(str(uid))
                elif isinstance(seg.data, str):
                    mentioned_users.append(seg.data)

        # 下载 @ 用户的头像
        for user_id in mentioned_users:
            logger.info(f"[多图] 获取 @{user_id} 的头像")
            img_bytes = await download_image(f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640", proxy)
            if img_bytes:
                images.append(img_bytes)

        logger.info(f"[多图] 共收集到 {len(images)} 张图片")
        return images

    @abstractmethod
    async def get_prompt(self) -> Optional[str]:
        raise NotImplementedError

    async def execute(self) -> Tuple[bool, Optional[str], bool]:
        if not self.get_config("general.enable_gemini_drawer", True):
            return True, "Plugin disabled", False

        # 检查群黑名单
        blacklist_groups = self.get_config("general.blacklist_groups", [])
        current_group_id = self._get_current_group_id()
        if current_group_id and blacklist_groups:
            str_blacklist = [str(g) for g in blacklist_groups]
            if current_group_id in str_blacklist:
                logger.info(f"群 {current_group_id} 在黑名单中，拒绝执行绘图命令")
                return True, "群黑名单", False

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

        endpoints_to_try = build_drawing_endpoints()

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
                is_tsai = False

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

                # 提取用户文本 prompt
                user_text_prompt = ""
                for p in parts:
                    if "text" in p:
                        user_text_prompt = p["text"]
                        break

                if is_doubao:
                    # 火山豆包图片生成 API
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

                    # 如果有图片，添加到请求中（图生图模式）
                    if image_bytes:
                        # 豆包支持 data URL 格式的图片
                        image_data_url = f"data:{mime_type};base64,{base64_img}"
                        doubao_payload["image"] = image_data_url
                        logger.info(f"构建豆包图生图请求: model={model_name}, prompt={user_text_prompt[:50]}...")
                    else:
                        logger.info(f"构建豆包文生图请求: model={model_name}, prompt={user_text_prompt[:50]}...")

                    current_payload = doubao_payload

                elif is_tsai:
                    headers["x-api-key"] = api_key
                    if image_bytes and mime_type:
                        request_url = f"{base_url}?endpoint=image_editing"
                        workflow = endpoint.get("model") or "rr3"
                        current_payload = {
                            "prompt": user_text_prompt,
                            "workflow": workflow,
                            "image": f"data:{mime_type};base64,{base64_img}",
                            "seed": -1
                        }
                    else:
                        request_url = f"{base_url}?endpoint=image_generation"
                        workflow = endpoint.get("model") or "rr3"
                        current_payload = {
                            "prompt": user_text_prompt,
                            "workflow": workflow,
                            "seed": -1
                        }

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
                        model_name = "gemini-pro-vision"

                    openai_payload = {
                        "model": model_name,
                        "messages": openai_messages,
                        "stream": endpoint.get("stream", False),
                    }
                    current_payload = openai_payload

                logger.info(f"准备向 {endpoint_type} 端点发送请求。URL: {request_url}, Payload: {safe_json_dumps(current_payload)}")

                img_data = None
                failure_reason = ""
                use_stream = endpoint.get("stream", False)
                if is_doubao or is_tsai:
                    use_stream = False

                debug_mode = self.get_config("behavior.debug_mode", False)

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
                                    if not line:
                                        continue
                                    if line.startswith(':'):
                                        continue

                                    if line.startswith('data:'):
                                        data_str = line[5:].strip()
                                        if data_str == "DONE" or data_str == "[DONE]":
                                            break

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
                    except httpx.RequestError as e:
                        logger.error(f"SSE 请求错误: {type(e).__name__}: {e!r}")
                        raise
                    except Exception as e:
                        logger.error(f"SSE 流处理失败: {type(e).__name__}: {e!r}")
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
                                for _ in range(100):
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
                        logger.error(
                            f"httpx.RequestError for endpoint {endpoint_type} ({request_url}): "
                            f"{type(e).__name__}: {repr(e)}"
                        )
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
                                    logger.warning(f"API 响应成功但未提取到图片。响应: {safe_json_dumps(data)}")
                                reason = extract_text_failure_reason(data)
                                raise Exception(f"API未返回图片, 原因: {reason or '未知'}")
                        else:
                            raise Exception(f"API请求失败, 状态码: {response.status_code} - {response.text}")

                if img_data:
                    if endpoint_type != 'lmarena':
                        key_manager.record_key_usage(api_key, True)

                    elapsed = (datetime.now() - start_time).total_seconds()
                    logger.info(f"使用 {endpoint_type} 端点成功生成图片，耗时 {elapsed:.2f}s")

                    try:
                        stream_id = self._get_stream_id()

                        if stream_id:
                            sent_count = 0
                            for img_idx, single_img_data in enumerate(img_data):
                                image_to_send_b64 = None
                                if single_img_data.startswith(('http://', 'https')):
                                    image_bytes = await download_image(single_img_data, proxy)
                                    if image_bytes:
                                        image_to_send_b64 = base64.b64encode(image_bytes).decode('utf-8')
                                elif 'base64,' in single_img_data:
                                    image_to_send_b64 = single_img_data.split('base64,')[1]
                                else:
                                    image_to_send_b64 = single_img_data

                                if image_to_send_b64:
                                    reply_with_image = self.get_config("behavior.reply_with_image", True)
                                    trigger_msg = None

                                    if reply_with_image and img_idx == 0:  # 仅对第一张回复
                                        trigger_msg = self.message

                                    # 如果 trigger_msg 是 CompatMessage 等自定义对象，提取其底层的原始字典，避免 RPC 序列化失败
                                    reply_msg_data = getattr(trigger_msg, '_raw_data', trigger_msg) if trigger_msg else None

                                    # 检查是否有图片说明文字（如随机风格名），仅第一张图片发送
                                    caption = self.get_image_caption() if img_idx == 0 else ""

                                    if hasattr(self, 'ctx') and self.ctx:
                                        # 始终使用 hybrid API 来确保我们可以带上 @ 提及
                                        hybrid_segments = []
                                        if trigger_msg and getattr(trigger_msg, 'user_id', None):
                                            hybrid_segments.append({"type": "at", "data": {"target_user_id": trigger_msg.user_id}})
                                        
                                        if caption:
                                            hybrid_segments.append({"type": "text", "content": f" {caption}\n"})
                                        elif trigger_msg:
                                            hybrid_segments.append({"type": "text", "content": "\n"}) # 换行分隔头像和图片
                                            
                                        hybrid_segments.append({"type": "image", "content": image_to_send_b64})
                                        
                                        # 不再使用有坑的 set_reply，直接在消息段里带上 @提及
                                        send_ok = await self.ctx.send.hybrid(hybrid_segments, stream_id)
                                        if caption:
                                            logger.info(f"[发送] 发送图文混合消息，说明: {caption}")
                                    else:
                                        # 兼容旧版本 API
                                        if caption:
                                            await self.send_text(caption)
                                        send_ok = await self.ctx.send.image(
                                            image_to_send_b64, stream_id,
                                            set_reply=trigger_msg is not None, reply_message=reply_msg_data
                                        )

                                    if send_ok:
                                        sent_count += 1
                                    else:
                                        logger.warning(f"第 {img_idx+1} 张图片发送返回失败 (stream_id={stream_id})")
                                else:
                                    logger.error(f"第 {img_idx+1} 张图片下载或转换失败")

                            if sent_count > 0:
                                await self._notify_success(elapsed)
                            else:
                                raise Exception("所有提取的图片下载或转换失败")
                        else:
                            raise Exception("无法从当前消息中确定stream_id")
                    except Exception as e:
                        logger.error(f"发送图片失败: {e}")
                        await self.send_text("❌ 图片发送失败。" )

                    await self._recall_status_messages(status_msg_start_time)
                    return True, "绘图成功", True

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
            chat_id = self._get_stream_id()
            if not chat_id: return
            await asyncio.sleep(1)
            current_time = time.time()
            bot_messages = await self.ctx.message.get_by_time_in_chat(
                chat_id=chat_id,
                start_time=str(fail_msg_send_time - 2),
                end_time=str(current_time + 5),
                limit=10
            )
            for msg in bot_messages:
                if isinstance(msg, dict):
                    content = msg.get('processed_plain_text', '')
                    msg_id = msg.get('message_id', None)
                    msg_time = msg.get('timestamp', 0)
                else:
                    content = getattr(msg, 'processed_plain_text', '')
                    msg_id = getattr(msg, 'message_id', None)
                    msg_time = getattr(msg, 'time', getattr(msg, 'timestamp', 0))

                if content.startswith("❌ 生成失败") and float(msg_time) >= fail_msg_send_time - 2:
                    if msg_id and not str(msg_id).startswith('send_api_'):
                        await self._safe_recall([str(msg_id)])
                        return
        except Exception: pass

    async def _recall_status_messages(self, status_msg_start_time: float) -> None:
        auto_recall = self.get_config("behavior.auto_recall_status", True)
        if not auto_recall: return

        try:
            chat_id = self._get_stream_id()
            if not chat_id: return
            await asyncio.sleep(2)
            current_time = time.time()
            bot_messages = await self.ctx.message.get_by_time_in_chat(
                chat_id=chat_id,
                start_time=str(status_msg_start_time - 5),
                end_time=str(current_time + 5),
                limit=20
            )
            status_prefixes = ("戳一戳", "✅ ")
            to_recall = []
            for msg in bot_messages:
                if isinstance(msg, dict):
                    msg_time = msg.get('timestamp', 0)
                    content = msg.get('processed_plain_text', '')
                    msg_id = msg.get('message_id', None)
                else:
                    msg_time = getattr(msg, 'time', getattr(msg, 'timestamp', 0))
                    content = getattr(msg, 'processed_plain_text', '')
                    msg_id = getattr(msg, 'message_id', None)
                    
                if float(msg_time) >= status_msg_start_time - 1:
                    if content.startswith(status_prefixes):
                        if msg_id and not str(msg_id).startswith('send_api_'):
                            to_recall.append(str(msg_id))
            if to_recall:
                await self._safe_recall(to_recall)
        except Exception: pass

class BaseMultiImageDrawCommand(BaseDrawCommand):
    """
    多图绘图命令基类
    继承自 BaseDrawCommand，重写 execute 方法以支持多图输入
    """
    async def execute(self) -> Tuple[bool, Optional[str], bool]:
        if not self.get_config("general.enable_gemini_drawer", True):
            return True, "Plugin disabled", False

        # 检查群黑名单
        blacklist_groups = self.get_config("general.blacklist_groups", [])
        current_group_id = self._get_current_group_id()
        if current_group_id and blacklist_groups:
            str_blacklist = [str(g) for g in blacklist_groups]
            if current_group_id in str_blacklist:
                logger.info(f"群 {current_group_id} 在黑名单中，拒绝执行多图绘图命令")
                return True, "群黑名单", False

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

        # 获取多张图片
        images = await self.get_multiple_source_images(min_count=2)

        if len(images) < 2:
            await self.send_text("❌ 请至少提供2张图片（通过回复消息、@用户或直接发送）")
            return True, "图片数量不足", True

        # 构造 Gemini 格式的 parts
        parts = []
        for i, img_bytes in enumerate(images):
            img_bytes = convert_if_gif(img_bytes)
            base64_img = base64.b64encode(img_bytes).decode('utf-8')
            mime_type = get_image_mime_type(img_bytes)
            # 添加图片标签，帮助模型识别
            parts.append({"text": f"Image {i+1}:"})
            parts.append({"inline_data": {"mime_type": mime_type, "data": base64_img}})

        parts.append({"text": f"Prompt: {prompt}"})

        payload = {
            "contents": [{"parts": parts}],
            "safetySettings": [
                {
                    "category": "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                    "threshold": "BLOCK_NONE"
                }
            ]
        }

        endpoints_to_try = build_drawing_endpoints()

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
                is_tsai = False

                if endpoint_type == 'lmarena':
                    is_openai = True
                    request_url = f"{api_url}"
                    client_proxy = None
                elif "/chat/completions" in api_url:
                    is_openai = True
                    request_url = api_url
                elif "/images/generations" in api_url:
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

                user_text_prompt = prompt

                if is_doubao:
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

                    image_list = []
                    for img in images:
                        img = convert_if_gif(img)
                        b64_img = base64.b64encode(img).decode('utf-8')
                        mime = get_image_mime_type(img)
                        image_list.append(f"data:{mime};base64,{b64_img}")

                    doubao_payload["image"] = image_list

                    current_payload = doubao_payload

                elif is_tsai:
                    headers["x-api-key"] = api_key
                    if images:
                        first_image = convert_if_gif(images[0])
                        first_image_b64 = base64.b64encode(first_image).decode('utf-8')
                        first_image_mime = get_image_mime_type(first_image)
                        if len(images) > 1:
                            logger.info(f"TS-AI 多图暂仅使用第 1 张参考图，其余 {len(images) - 1} 张将被忽略。")
                        request_url = f"{base_url}?endpoint=image_editing"
                        workflow = endpoint.get("model") or "rr3"
                        current_payload = {
                            "prompt": user_text_prompt,
                            "workflow": workflow,
                            "image": f"data:{first_image_mime};base64,{first_image_b64}",
                            "seed": -1
                        }
                    else:
                        request_url = f"{base_url}?endpoint=image_generation"
                        workflow = endpoint.get("model") or "rr3"
                        current_payload = {
                            "prompt": user_text_prompt,
                            "workflow": workflow,
                            "seed": -1
                        }

                elif is_openai:
                    if api_key:
                        headers["Authorization"] = f"Bearer {api_key}"

                    content_list = [{"type": "text", "text": f"Prompt: {user_text_prompt}"}]

                    for i, img_bytes in enumerate(images):
                        img_bytes = convert_if_gif(img_bytes)
                        base64_img = base64.b64encode(img_bytes).decode('utf-8')
                        mime_type = get_image_mime_type(img_bytes)
                        content_list.append({"type": "text", "text": f"Image {i+1}:"})
                        content_list.append({
                            "type": "image_url",
                            "image_url": { "url": f"data:{mime_type};base64,{base64_img}" }
                        })

                    openai_messages = [{"role": "user", "content": content_list}]

                    model_name = endpoint.get("model")
                    if not model_name:
                        model_name = "gemini-pro-vision"

                    openai_payload = {
                        "model": model_name,
                        "messages": openai_messages,
                        "stream": endpoint.get("stream", False),
                    }
                    current_payload = openai_payload

                logger.info(f"准备向 {endpoint_type} 端点发送多图请求。")

                img_data = None
                failure_reason = ""
                use_stream = endpoint.get("stream", False)
                if is_doubao or is_tsai:
                    use_stream = False

                debug_mode = self.get_config("behavior.debug_mode", False)

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
                                        except json.JSONDecodeError: pass

                        # 流结束后：尝试从累积内容中提取
                        if not img_data and accumulated_content:
                            logger.info(f"[多图] SSE流结束，尝试从累积内容中提取图片 (长度: {len(accumulated_content)})")
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
                            logger.warning(f"[调试模式] 多图SSE流未提取到图片，累积 {len(debug_sse_lines)} 条数据:")
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
                                    logger.warning(f"[调试模式] 多图非流式响应未提取到图片，原始响应:")
                                    logger.warning(f"[调试模式] {json.dumps(data, ensure_ascii=False)[:2000]}")
                                else:
                                    logger.warning(f"API 响应成功但未提取到图片。")
                                reason = extract_text_failure_reason(data)
                                raise Exception(f"API未返回图片, 原因: {reason or '未知'}")
                        else:
                            raise Exception(f"API请求失败, 状态码: {response.status_code} - {response.text}")

                if img_data:
                    if endpoint_type != 'lmarena':
                        key_manager.record_key_usage(api_key, True)

                    elapsed = (datetime.now() - start_time).total_seconds()
                    logger.info(f"使用 {endpoint_type} 端点成功生成图片，耗时 {elapsed:.2f}s")

                    try:
                        stream_id = self._get_stream_id()

                        if stream_id:
                            sent_count = 0
                            for img_idx, single_img_data in enumerate(img_data):
                                image_to_send_b64 = None
                                if single_img_data.startswith(('http://', 'https')):
                                    image_bytes = await download_image(single_img_data, proxy)
                                    if image_bytes:
                                        image_to_send_b64 = base64.b64encode(image_bytes).decode('utf-8')
                                elif 'base64,' in single_img_data:
                                    image_to_send_b64 = single_img_data.split('base64,')[1]
                                else:
                                    image_to_send_b64 = single_img_data

                                if image_to_send_b64:
                                    reply_with_image = self.get_config("behavior.reply_with_image", True)
                                    trigger_msg = None

                                    if reply_with_image and img_idx == 0:  # 仅对第一张回复
                                        trigger_msg = self.message
                                        
                                    # 如果 trigger_msg 是 CompatMessage，提取 _raw_data 以避免 RPC 序列化错误
                                    reply_msg_data = getattr(trigger_msg, '_raw_data', trigger_msg) if trigger_msg else None

                                    # 使用原生 ctx.send.image API 发送图片
                                    send_ok = await self.ctx.send.image(
                                        image_to_send_b64, stream_id,
                                        set_reply=trigger_msg is not None, reply_message=reply_msg_data
                                    )
                                    if send_ok:
                                        sent_count += 1
                                    else:
                                        logger.warning(f"第 {img_idx+1} 张图片发送返回失败 (stream_id={stream_id})")
                                else:
                                    logger.error(f"第 {img_idx+1} 张图片下载或转换失败")

                            if sent_count > 0:
                                await self._notify_success(elapsed)
                            else:
                                raise Exception("所有提取的图片下载或转换失败")
                        else:
                            raise Exception("无法从当前消息中确定stream_id")
                    except Exception as e:
                        logger.error(f"发送图片失败: {e}")
                        await self.send_text("❌ 图片发送失败。" )

                    await self._recall_status_messages(status_msg_start_time)
                    return True, "绘图成功", True

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

        elapsed = (datetime.now() - start_time).total_seconds()
        fail_msg = f"❌ 生成失败 ({elapsed:.2f}s, {len(endpoints_to_try)}次尝试)\n最终错误: {last_error}"
        fail_msg_send_time = time.time()
        await self.send_text(fail_msg)
        asyncio.create_task(self._delayed_recall_fail_message(fail_msg_send_time, fail_msg))
        await self._recall_status_messages(status_msg_start_time)
        return True, "所有尝试均失败", True


class BaseVideoCommand(BaseCommand, ABC):
    """
    视频生成命令基类
    仅使用标记为 is_video=True 的渠道进行视频生成

    子类通过设置 requires_image 属性控制是否需要图片输入：
    - requires_image = True: 图生视频（需要图片）
    - requires_image = False: 文生视频（纯文字）
    """
    permission: str = "user"
    requires_image: bool = True  # 默认需要图片，子类可覆盖

    def _get_current_chat_id(self) -> Optional[str]:
        """获取当前聊天的 chat_id（优先使用框架注入的 stream_id）"""
        # 优先使用框架注入的 _stream_id
        if self._stream_id:
            return self._stream_id
        try:
            chat_stream = self.message.chat_stream
            if chat_stream:
                stream_id = getattr(chat_stream, 'stream_id', None)
                if stream_id:
                    return stream_id
            return None
        except Exception:
            return None

    async def get_source_image_bytes(self) -> Optional[bytes]:
        """获取源图片，复用 draw_logic 中的逻辑"""
        proxy = self.get_config("proxy.proxy_url") if self.get_config("proxy.enable") else None
        image_bytes = await extract_source_image(self.message, proxy, logger, getattr(self, 'ctx', None))
        return image_bytes

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

        prompt = await self.get_prompt()
        if not prompt:
            return True, "无效的Prompt", True

        # 根据 requires_image 决定是否需要图片
        image_bytes = None
        base64_img = None
        mime_type = None

        if self.requires_image:
            image_bytes = await self.get_source_image_bytes()

            if not image_bytes:
                await self.send_text("❌ 图生视频需要一张图片作为输入！\n请回复图片或@用户或发送图片后使用此指令。")
                return True, "缺少图片", True

            # 构造请求 payload (带图片)
            image_bytes = convert_if_gif(image_bytes)
            base64_img = base64.b64encode(image_bytes).decode('utf-8')
            mime_type = get_image_mime_type(image_bytes)

        # 使用复用函数获取端点
        from .draw_logic import get_video_endpoints, process_video_generation, send_video_via_napcat

        endpoints_to_try = await get_video_endpoints(self.get_config, logger=logger)

        if not endpoints_to_try:
            await self.send_text("❌ 未配置视频生成渠道。\n请使用 `/渠道设置视频 <渠道名> true` 启用视频渠道。")
            return True, "无视频渠道", True

        # 发送开始提示
        await self.send_text("🎬 开始生成视频，请稍候...")

        proxy = self.get_config("proxy.proxy_url") if self.get_config("proxy.enable") else None

        # 使用复用函数生成视频
        video_data, last_error = await process_video_generation(
            prompt=prompt,
            base64_img=base64_img,
            mime_type=mime_type,
            endpoints=endpoints_to_try,
            proxy=proxy,
            logger=logger,
            debug_mode=self.get_config("behavior.debug_mode", False)
        )

        if video_data:
            elapsed = (datetime.now() - start_time).total_seconds()

            # 获取群ID或用户ID
            group_id = None
            user_id = None

            if hasattr(self.message, 'message_info') and self.message.message_info:
                group_info = getattr(self.message.message_info, 'group_info', None)
                if group_info and hasattr(group_info, 'group_id') and group_info.group_id:
                    group_id = str(group_info.group_id)

                user_info = getattr(self.message.message_info, 'user_info', None)
                if user_info and hasattr(user_info, 'user_id'):
                    user_id = str(user_info.user_id)

            if not group_id and hasattr(self.message, 'chat_id'):
                chat_id = str(self.message.chat_id)
                if chat_id.isdigit():
                     group_id = chat_id

            if not user_id and hasattr(self.message, 'user_id'):
                 user_id = str(self.message.user_id)

            if hasattr(self.message, 'message_type') and self.message.message_type == 'private':
                group_id = None

            # 发送视频
            napcat_host = self.get_config("api.napcat_host", "napcat")
            napcat_port = self.get_config("api.napcat_port", 3033)

            success, send_error = await send_video_via_napcat(
                video_base64=video_data,
                group_id=group_id,
                user_id=user_id,
                napcat_host=napcat_host,
                napcat_port=napcat_port,
                logger=logger
            )

            if success:
                await self.send_text(f"✅ 视频生成完成 ({elapsed:.2f}s)")
                return True, "视频生成成功", True
            else:
                await self.send_text(f"❌ 视频发送失败: {send_error}")
                return True, f"视频发送失败: {send_error}", True
        else:
            elapsed = (datetime.now() - start_time).total_seconds()
            await self.send_text(f"❌ 视频生成失败 ({elapsed:.2f}s)\n错误: {last_error}")
            return True, "所有尝试均失败", True
