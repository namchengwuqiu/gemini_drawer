"""
Gemini Drawer 基础命令模块

BaseAdminCommand:
    管理员命令基类：权限校验 + 转发消息发送

BaseDrawCommand:
    绘图命令基类。执行流程：
      准入校验 → 取提示词 → 提示用户 → 收集参考图 → 交给 pipeline.run_drawing
      → 发图（@提及 / 图文混合）→ 撤回过程中的状态消息
    子类通常只需实现 get_prompt()；需要改变取图方式时覆写 collect_images()/check_images()。

BaseMultiImageDrawCommand:
    多图绘图基类，只覆写取图与校验，其余复用 BaseDrawCommand 的流程。

BaseVideoCommand:
    视频生成基类，仅使用标记为 is_video=True 的渠道。

协议差异（Gemini / OpenAI / 豆包 / gpt-image / TS-AI）全部在 providers/ 中，
端点轮询与 Key 记账在 pipeline.py 中，本模块不再关心。
"""
import asyncio
import base64
import time
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, List, Optional, Tuple

from maibot_sdk.compat.base import BaseCommand

from ..core.host_bridge import get_plugin_logger

from ..core.endpoints import build_drawing_endpoints, build_video_endpoints
from ..core.image_source import download_avatar, extract_source_image
from ..core.guards import evaluate_access, message_user_id
from ..core.pipeline import run_drawing
from ..providers import DrawRequest
from ..utils import convert_if_gif, download_image, get_image_mime_type

logger = get_plugin_logger("plugin.gemini_drawer")



class BaseAdminCommand(BaseCommand, ABC):
    permission: str = "owner"

    async def execute(self) -> Tuple[bool, Optional[str], bool]:
        if not self.get_config("general.enable_gemini_drawer", True):
            return True, "Plugin disabled", False

        str_user_id = message_user_id(self.message)
        if not str_user_id:
            logger.warning("无法从 self.message.message_info.user_info 中获取 user_id")
            await self.send_text("无法获取用户信息，操作失败。")
            return False, "无法获取用户信息", True

        admin_list = self.get_config("general.admins", []) or []
        if str_user_id not in {str(admin) for admin in admin_list}:
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
            if not getattr(self, 'ctx', None):
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
                text_parts = [str(seg[1]) for seg in segments]
                messages.append({
                    "user_id": "0",
                    "nickname": nickname,
                    "segments": [{"type": "text", "content": "\n".join(text_parts)}]
                })

            stream_id = self._get_stream_id()
            if stream_id:
                await self.ctx.send.forward(messages, stream_id)
                return True

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


class _ChatContextMixin:
    """从消息对象中定位当前会话的通用逻辑。"""

    def _get_current_chat_id(self) -> Optional[str]:
        """获取当前聊天的 chat_id（优先使用框架注入的 stream_id）"""
        if self._stream_id:
            return self._stream_id
        try:
            chat_stream = self.message.chat_stream
            if not chat_stream:
                return None

            stream_id = getattr(chat_stream, 'stream_id', None)
            if stream_id:
                logger.debug(f"使用 stream_id 作为 chat_id: {stream_id}")
                return stream_id

            group_info = getattr(chat_stream, 'group_info', None)
            if group_info and getattr(group_info, 'group_id', None):
                chat_id = f"{chat_stream.platform}:{group_info.group_id}"
                logger.debug(f"使用 group_id 构造 chat_id: {chat_id}")
                return chat_id

            user_info = getattr(chat_stream, 'user_info', None)
            if user_info and getattr(user_info, 'user_id', None):
                chat_id = f"{chat_stream.platform}:{user_info.user_id}"
                logger.debug(f"使用 user_id 构造 chat_id: {chat_id}")
                return chat_id
            return None
        except Exception as e:
            logger.warning(f"获取 chat_id 失败: {e}")
            return None

    def _get_current_group_id(self) -> Optional[str]:
        """获取当前群 ID；私聊返回 None"""
        try:
            chat_stream = self.message.chat_stream
            if chat_stream:
                group_info = getattr(chat_stream, 'group_info', None)
                if group_info and getattr(group_info, 'group_id', None):
                    return str(group_info.group_id)
            return None
        except Exception as e:
            logger.warning(f"获取 group_id 失败: {e}")
            return None

    def _check_access(self):
        return evaluate_access(
            self.get_config,
            group_id=self._get_current_group_id(),
            user_id=message_user_id(self.message),
        )

    def _proxy(self) -> Optional[str]:
        return self.get_config("proxy.proxy_url") if self.get_config("proxy.enable") else None


class BaseDrawCommand(_ChatContextMixin, BaseCommand, ABC):
    permission: str = "user"
    allow_text_only: bool = False

    # ── 通知与撤回 ──────────────────────────────────────────

    async def _safe_recall(self, message_ids: List[str]) -> int:
        """安全地撤回消息列表，返回成功撤回的数量

        使用 NapCat 适配器的跨插件 API: adapter.napcat.message.delete_msg
        """
        recalled_count = 0
        for mid in message_ids:
            try:
                if not getattr(self, 'ctx', None):
                    logger.debug(f"跳过撤回消息 {mid}（ctx 不可用）")
                    continue
                resp = await self.ctx.api.call(
                    "adapter.napcat.message.delete_msg", message_id=str(mid)
                )
                if resp is not None and not (isinstance(resp, dict) and resp.get("success") is False):
                    recalled_count += 1
                    logger.debug(f"成功撤回消息: {mid}")
                else:
                    logger.debug(f"撤回消息未成功: {mid}")
            except Exception as e:
                logger.warning(f"撤回消息失败 {mid}: {e}")
        return recalled_count

    async def _notify_success(self, elapsed: float) -> None:
        """成功生成后通知用户"""
        if self.get_config("behavior.reply_with_image", True):
            logger.debug("[通知] 已启用回复图片模式，跳过额外通知")
            return

        if self.get_config("behavior.success_notify_poke", True):
            if await self._send_poke_via_napcat():
                return

        await self.send_text(f"✅ 生成完成 ({elapsed:.2f}s)")

    def get_image_caption(self) -> Optional[str]:
        """子类可重写此方法，返回要与图片一起发送的文字说明"""
        return None

    async def _notify_start(self) -> None:
        """开始处理时通知用户：优先戳一戳，失败回落文字"""
        if not await self._send_poke_via_napcat():
            await self.send_text("🎨 开始处理...")

    async def _send_poke_via_napcat(self) -> bool:
        """通过 NapCat 适配器发送戳一戳，返回是否成功

        使用跨插件 API: adapter.napcat.message.send_poke
        """
        try:
            if not getattr(self, 'ctx', None):
                return False

            user_id = message_user_id(self.message)
            if not user_id:
                return False

            call_kwargs = {"user_id": int(user_id)}
            group_id = self._get_current_group_id()
            if group_id:
                call_kwargs["group_id"] = int(group_id)
                call_kwargs["target_id"] = int(user_id)

            resp = await self.ctx.api.call("adapter.napcat.message.send_poke", **call_kwargs)

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

    # ── 取图 ────────────────────────────────────────────────

    async def get_source_image_bytes(self) -> Optional[bytes]:
        proxy = self._proxy()

        image_bytes = await extract_source_image(self.message, proxy, logger, getattr(self, 'ctx', None))
        if image_bytes:
            return image_bytes

        if self.allow_text_only:
            logger.info("允许纯文本模式且未找到图片，跳过自动获取头像。")
            return None

        # 兜底：都没找到就用发送者头像（Action 不走这条兜底）
        logger.info("未找到图片、Emoji或@提及，回退到发送者头像。")
        user_id = self.message.message_info.user_info.user_id
        return await download_avatar(user_id, proxy)

    async def collect_images(self) -> List[bytes]:
        """收集参考图。多图子类覆写此方法。"""
        image_bytes = await self.get_source_image_bytes()
        return [image_bytes] if image_bytes else []

    def check_images(self, images: List[bytes]) -> Optional[str]:
        """校验参考图数量；返回 None 表示通过，否则返回给用户的提示。"""
        if not images and not self.allow_text_only:
            return "❌ 未找到可供处理的图片或图片处理失败。"
        return None

    async def get_multiple_source_images(self, min_count: int = 2) -> List[bytes]:
        """获取多张源图片

        来源优先级：回复消息中的图片 > 当前消息中的图片 > @提及用户的头像
        """
        from ..core.image_source import extract_images_from_segments, extract_mentioned_user_ids, resolve_reply_images

        proxy = self._proxy()
        images: List[bytes] = []

        # 1. 回复消息中的图片
        #    走 resolve_reply_images 而不是直接读 reply 的消息段——被回复消息
        #    往往只剩一句渲染后的纯文本，真正的图要靠官方消息能力按 id 回查。
        if getattr(self.message, 'reply', None) is not None or getattr(self.message, 'reply_to', None):
            logger.info("[多图] 尝试从回复消息中提取图片...")
            reply_images = await resolve_reply_images(
                self.message, proxy, logger, getattr(self, 'ctx', None)
            )
            images.extend(reply_images)
            logger.info(f"[多图] 从回复消息中提取到 {len(reply_images)} 张图片")

        # 2. 当前消息中的图片
        images.extend(await extract_images_from_segments(self.message.message_segment, proxy, logger))

        # 3. @提及用户的头像
        for user_id in extract_mentioned_user_ids(self.message.message_segment):
            logger.info(f"[多图] 获取 @{user_id} 的头像")
            img_bytes = await download_avatar(user_id, proxy)
            if img_bytes:
                images.append(img_bytes)

        logger.info(f"[多图] 共收集到 {len(images)} 张图片")
        return images

    @abstractmethod
    async def get_prompt(self) -> Optional[str]:
        raise NotImplementedError

    # ── 主流程 ──────────────────────────────────────────────

    async def execute(self) -> Tuple[bool, Optional[str], bool]:
        decision = self._check_access()
        if not decision.allowed:
            if decision.message:
                await self.send_text(decision.message)
            else:
                logger.info(f"拒绝执行绘图命令: {decision.reason}")
            return True, decision.reason, decision.should_stop

        start_time = datetime.now()
        status_msg_start_time = time.time()

        prompt = await self.get_prompt()
        if not prompt:
            return True, "无效的Prompt", True

        await self._notify_start()

        images = await self.collect_images()
        error = self.check_images(images)
        if error:
            await self.send_text(error)
            return True, "参考图不满足要求", True

        images = [convert_if_gif(img) for img in images]
        request = DrawRequest(
            prompt=prompt,
            images=images,
            mime_types=[get_image_mime_type(img) for img in images],
        )

        endpoints = build_drawing_endpoints()
        if not endpoints:
            await self.send_text("❌ 未配置任何API密钥或端点。")
            return True, "无可用密钥或端点", True

        proxy = self._proxy()
        img_data, last_error = await run_drawing(
            request=request,
            endpoints=endpoints,
            proxy=proxy,
            logger=logger,
            debug_mode=self.get_config("behavior.debug_mode", False),
        )

        elapsed = (datetime.now() - start_time).total_seconds()

        if img_data:
            logger.info(f"绘图成功，耗时 {elapsed:.2f}s")
            try:
                await self._deliver_images(img_data, proxy, elapsed)
            except Exception as e:
                logger.error(f"发送图片失败: {e}")
                await self.send_text("❌ 图片发送失败。")
            await self._recall_status_messages(status_msg_start_time)
            return True, "绘图成功", True

        fail_msg = f"❌ 生成失败 ({elapsed:.2f}s, {len(endpoints)}次尝试)\n最终错误: {last_error}"
        fail_msg_send_time = time.time()
        await self.send_text(fail_msg)
        asyncio.create_task(self._delayed_recall_fail_message(fail_msg_send_time, fail_msg))
        await self._recall_status_messages(status_msg_start_time)
        return True, "所有尝试均失败", True

    # ── 发图 ────────────────────────────────────────────────

    @staticmethod
    def _clean_base64(value: str) -> str:
        return value.replace('\n', '').replace('\r', '').replace(' ', '')

    async def _to_base64(self, raw: str, proxy: Optional[str]) -> Optional[str]:
        """把 pipeline 返回的图片数据（URL / data URL / 裸 base64）统一成裸 base64。"""
        if raw.startswith(('http://', 'https://')):
            downloaded = await download_image(raw, proxy)
            return base64.b64encode(downloaded).decode('utf-8') if downloaded else None
        if 'base64,' in raw:
            return self._clean_base64(raw.split('base64,', 1)[1])
        return self._clean_base64(raw)

    async def _send_one_image(
        self, image_b64: str, caption: Optional[str], with_at: bool, stream_id: str
    ) -> bool:
        if not getattr(self, 'ctx', None):
            return await self.send_image(image_b64)

        # 用消息段自带 @提及，而不是有坑的 set_reply
        segments: List[dict] = []
        if with_at:
            segments.append({"type": "at", "data": {"target_user_id": self.message.user_id}})
        if caption:
            segments.append({"type": "text", "content": f" {caption}\n"})
        elif with_at:
            segments.append({"type": "text", "content": "\n"})  # 分隔 @ 与图片
        segments.append({"type": "image", "content": image_b64})

        return await self.ctx.send.hybrid(segments, stream_id)

    async def _deliver_images(self, img_data: List[str], proxy: Optional[str], elapsed: float) -> None:
        stream_id = self._get_stream_id()
        if not stream_id:
            raise RuntimeError("无法从当前消息中确定stream_id")

        reply_with_image = self.get_config("behavior.reply_with_image", True)
        # @提及仅群聊附带：QQ 私聊不支持 at 消息段，附带会导致整条消息被平台拒绝
        can_at = bool(self._get_current_group_id() and getattr(self.message, 'user_id', None))

        sent_count = 0
        for idx, raw in enumerate(img_data):
            image_b64 = await self._to_base64(raw, proxy)
            if not image_b64:
                logger.error(f"第 {idx + 1} 张图片下载或转换失败")
                continue

            is_first = idx == 0
            caption = self.get_image_caption() if is_first else None
            if await self._send_one_image(
                image_b64, caption, with_at=reply_with_image and is_first and can_at,
                stream_id=stream_id,
            ):
                sent_count += 1
                if caption:
                    logger.info(f"[发送] 发送图文混合消息，说明: {caption}")
            else:
                logger.warning(f"第 {idx + 1} 张图片发送返回失败 (stream_id={stream_id})")

        if sent_count == 0:
            raise RuntimeError("所有图片均未能发送（下载/转换失败或发送被平台拒绝）")

        await self._notify_success(elapsed)

    # ── 状态消息清理 ────────────────────────────────────────

    async def _iter_recent_bot_messages(self, since: float, limit: int) -> List[Any]:
        chat_id = self._get_stream_id()
        if not chat_id:
            return []
        return await self.ctx.message.get_by_time_in_chat(
            chat_id=chat_id,
            start_time=str(since),
            end_time=str(time.time() + 5),
            limit=limit,
        )

    @staticmethod
    def _message_fields(msg: Any) -> Tuple[str, Optional[str], float]:
        if isinstance(msg, dict):
            return (
                msg.get('processed_plain_text', ''),
                msg.get('message_id', None),
                msg.get('timestamp', 0),
            )
        return (
            getattr(msg, 'processed_plain_text', ''),
            getattr(msg, 'message_id', None),
            getattr(msg, 'time', getattr(msg, 'timestamp', 0)),
        )

    async def _delayed_recall_fail_message(self, fail_msg_send_time: float, fail_msg_content: str) -> None:
        try:
            await asyncio.sleep(6)
            messages = await self._iter_recent_bot_messages(fail_msg_send_time - 2, limit=10)
            for msg in messages:
                content, msg_id, msg_time = self._message_fields(msg)
                if not content.startswith("❌ 生成失败"):
                    continue
                if float(msg_time) < fail_msg_send_time - 2:
                    continue
                if msg_id and not str(msg_id).startswith('send_api_'):
                    await self._safe_recall([str(msg_id)])
                    return
        except Exception:
            pass

    #: 需要在绘图结束后自动撤回的状态提示前缀
    STATUS_PREFIXES = ("戳一戳", "✅ ", "🎨")

    async def _recall_status_messages(self, status_msg_start_time: float) -> None:
        if not self.get_config("behavior.auto_recall_status", True):
            return

        try:
            await asyncio.sleep(2)
            messages = await self._iter_recent_bot_messages(status_msg_start_time - 5, limit=20)
            to_recall = []
            for msg in messages:
                content, msg_id, msg_time = self._message_fields(msg)
                if float(msg_time) < status_msg_start_time - 1:
                    continue
                if not content.startswith(self.STATUS_PREFIXES):
                    continue
                if msg_id and not str(msg_id).startswith('send_api_'):
                    to_recall.append(str(msg_id))
            if to_recall:
                await self._safe_recall(to_recall)
        except Exception:
            pass


class BaseMultiImageDrawCommand(BaseDrawCommand):
    """多图绘图命令基类：只改取图与校验，流程完全复用 BaseDrawCommand。"""

    min_images: int = 2

    async def collect_images(self) -> List[bytes]:
        return await self.get_multiple_source_images(min_count=self.min_images)

    def check_images(self, images: List[bytes]) -> Optional[str]:
        if len(images) < self.min_images:
            return f"❌ 请至少提供{self.min_images}张图片（通过回复消息、@用户或直接发送）"
        return None


class BaseVideoCommand(_ChatContextMixin, BaseCommand, ABC):
    """
    视频生成命令基类
    仅使用标记为 is_video=True 的渠道进行视频生成

    子类通过设置 requires_image 属性控制是否需要图片输入：
    - requires_image = True: 图生视频（需要图片）
    - requires_image = False: 文生视频（纯文字）
    """
    permission: str = "user"
    requires_image: bool = True

    async def get_source_image_bytes(self) -> Optional[bytes]:
        return await extract_source_image(
            self.message, self._proxy(), logger, getattr(self, 'ctx', None)
        )

    @abstractmethod
    async def get_prompt(self) -> Optional[str]:
        raise NotImplementedError

    def _resolve_send_target(self) -> Tuple[Optional[str], Optional[str]]:
        """确定视频发往哪里：(group_id, user_id)。私聊时 group_id 为 None。"""
        group_id = self._get_current_group_id()
        user_id = message_user_id(self.message)

        if not group_id:
            chat_id = str(getattr(self.message, 'chat_id', '') or '')
            if chat_id.isdigit():
                group_id = chat_id

        if not user_id:
            raw_user_id = getattr(self.message, 'user_id', None)
            user_id = str(raw_user_id) if raw_user_id else None

        if getattr(self.message, 'message_type', None) == 'private':
            group_id = None

        return group_id, user_id

    async def execute(self) -> Tuple[bool, Optional[str], bool]:
        decision = self._check_access()
        if not decision.allowed:
            if decision.message:
                await self.send_text(decision.message)
            else:
                logger.info(f"拒绝执行视频生成命令: {decision.reason}")
            return True, decision.reason, decision.should_stop

        start_time = datetime.now()

        prompt = await self.get_prompt()
        if not prompt:
            return True, "无效的Prompt", True

        base64_img = None
        mime_type = None

        if self.requires_image:
            image_bytes = await self.get_source_image_bytes()
            if not image_bytes:
                await self.send_text(
                    "❌ 图生视频需要一张图片作为输入！\n请回复图片或@用户或发送图片后使用此指令。"
                )
                return True, "缺少图片", True

            image_bytes = convert_if_gif(image_bytes)
            base64_img = base64.b64encode(image_bytes).decode('utf-8')
            mime_type = get_image_mime_type(image_bytes)

        from ..core.video import process_video_generation, send_video_via_napcat

        endpoints_to_try = build_video_endpoints(logger=logger)
        if not endpoints_to_try:
            await self.send_text(
                "❌ 未配置视频生成渠道。\n请使用 `/渠道设置视频 <渠道名> true` 启用视频渠道。"
            )
            return True, "无视频渠道", True

        await self.send_text("🎬 开始生成视频，请稍候...")

        video_data, last_error = await process_video_generation(
            prompt=prompt,
            base64_img=base64_img,
            mime_type=mime_type,
            endpoints=endpoints_to_try,
            proxy=self._proxy(),
            logger=logger,
            debug_mode=self.get_config("behavior.debug_mode", False),
        )

        elapsed = (datetime.now() - start_time).total_seconds()

        if not video_data:
            await self.send_text(f"❌ 视频生成失败 ({elapsed:.2f}s)\n错误: {last_error}")
            return True, "所有尝试均失败", True

        group_id, user_id = self._resolve_send_target()
        success, send_error = await send_video_via_napcat(
            video_base64=video_data,
            group_id=group_id,
            user_id=user_id,
            napcat_host=self.get_config("api.napcat_host", "napcat"),
            napcat_port=self.get_config("api.napcat_port", 3033),
            logger=logger,
        )

        if success:
            await self.send_text(f"✅ 视频生成完成 ({elapsed:.2f}s)")
            return True, "视频生成成功", True

        await self.send_text(f"❌ 视频发送失败: {send_error}")
        return True, f"视频发送失败: {send_error}", True
