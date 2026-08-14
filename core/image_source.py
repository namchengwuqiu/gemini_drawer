"""
Gemini Drawer 取图模块

从消息中定位用户想要处理的图片，优先级：

    回复的消息 > 当前消息 > @提及用户的头像

每一层都有多条获取路径（消息段内联 base64 / 消息段 URL / SDK message capability /
宿主数据库回查），任一层失败都不影响后续层继续尝试。

宿主数据库相关的依赖统一走 host_bridge，本模块不直接 import src.*。
"""
from __future__ import annotations

import base64
import re
from typing import Any, List, Optional, Tuple

from . import host_bridge
from ..utils import download_image, safe_json_dumps

AVATAR_URL = "https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"
#: 消息段里 @对象 的可能键名
_AT_ID_KEYS = ("qq", "user_id", "id", "target_user_id")
#: 文本里的 @提及：@<昵称:12345> 与 @12345
_AT_PATTERNS = (r'@<[^:>]+:([^:>]+)>', r'@(\d{5,11})\b')
#: 短于此长度的字符串不当作 base64 图片
_MIN_BASE64_LEN = 200


# ── 消息段基础操作 ───────────────────────────────────────────


def normalize_segments(segments: Any) -> List[Any]:
    """把 seglist 包装 / 单段 / 段列表统一成一个列表。"""
    if not segments:
        return []
    if hasattr(segments, 'type') and segments.type == 'seglist':
        segments = segments.data
    if not isinstance(segments, list):
        segments = [segments]
    return [seg for seg in segments if seg]


def _seg_fields(seg: Any) -> Tuple[Optional[str], Any, Any]:
    """取出消息段的 (type, data, binary_data_base64)，兼容 dict 与对象两种形态。"""
    if isinstance(seg, dict):
        return seg.get('type'), seg.get('data'), seg.get('binary_data_base64')
    return (
        getattr(seg, 'type', None),
        getattr(seg, 'data', None),
        getattr(seg, 'binary_data_base64', None),
    )


async def _segment_to_image(seg: Any, proxy: Optional[str], logger) -> Optional[bytes]:
    """从单个图片/表情消息段中取出图片字节。"""
    seg_type, seg_data, binary_data_base64 = _seg_fields(seg)
    if seg_type not in ('image', 'emoji'):
        return None

    def _decode(value: str) -> Optional[bytes]:
        try:
            if logger:
                logger.info(f"在消息段中找到Base64图片 (类型: {seg_type})。")
            return base64.b64decode(value)
        except Exception:
            if logger:
                logger.warning(f"无法将类型为 '{seg_type}' 的段解码为图片，已跳过。")
            return None

    if isinstance(binary_data_base64, str) and len(binary_data_base64) > _MIN_BASE64_LEN:
        return _decode(binary_data_base64)

    if isinstance(seg_data, dict) and seg_data.get('url'):
        if logger:
            logger.info(f"在消息段中找到URL图片 (类型: {seg_type})。")
        image_bytes = await download_image(seg_data.get('url'), proxy)
        if image_bytes:
            return image_bytes
        if logger:
            logger.warning(f"消息段URL图片下载失败，继续尝试后续片段 (类型: {seg_type})。")
        return None

    if isinstance(seg_data, str) and len(seg_data) > _MIN_BASE64_LEN:
        return _decode(seg_data)

    return None


async def extract_images_from_segments(
    segments: Any, proxy: Optional[str] = None, logger=None
) -> List[bytes]:
    """提取消息段中的**所有**图片（多图绘图用）。"""
    images = []
    for seg in normalize_segments(segments):
        img = await _segment_to_image(seg, proxy, logger)
        if img:
            images.append(img)
    return images


async def extract_first_image_from_segments(
    segments: Any, proxy: Optional[str] = None, logger=None
) -> Optional[bytes]:
    """返回消息段中的第一张图片。"""
    for seg in normalize_segments(segments):
        img = await _segment_to_image(seg, proxy, logger)
        if img:
            return img
    return None


def extract_mentioned_user_ids(segments: Any) -> List[str]:
    """提取消息段中所有被 @ 的用户 ID，保持出现顺序、自动去重。"""
    user_ids: List[str] = []

    def add(uid: Any) -> None:
        uid = str(uid).strip()
        if uid and uid != 'all' and uid not in user_ids:
            user_ids.append(uid)

    for seg in normalize_segments(segments):
        seg_type, seg_data, _ = _seg_fields(seg)
        if seg_type == 'at':
            if isinstance(seg_data, dict):
                for key in _AT_ID_KEYS:
                    if seg_data.get(key):
                        add(seg_data[key])
                        break
            elif isinstance(seg_data, str):
                add(seg_data)
        elif seg_type == 'text' and isinstance(seg_data, str) and '@' in seg_data:
            for pattern in _AT_PATTERNS:
                for match in re.finditer(pattern, seg_data):
                    add(match.group(1))

    return user_ids


def extract_mentioned_ids_from_text(text: str) -> List[str]:
    """从纯文本中提取 @提及的用户 ID（数据库消息没有结构化消息段）。"""
    ids: List[str] = []
    for pattern in _AT_PATTERNS:
        for match in re.finditer(pattern, text or ""):
            uid = str(match.group(1)).strip()
            if uid and uid not in ids:
                ids.append(uid)
    return ids


async def download_avatar(user_id: Any, proxy: Optional[str] = None) -> Optional[bytes]:
    """下载 QQ 用户头像。"""
    return await download_image(AVATAR_URL.format(user_id=user_id), proxy)


def _message_dict_segments(message_dict: dict) -> Any:
    """从 message capability 返回的字典里取出消息段，兼容几种字段名。"""
    return (
        message_dict.get('message_segments')
        or message_dict.get('raw_message')
        or message_dict.get('message_segment')
        or []
    )


# ── 主入口 ───────────────────────────────────────────────────


async def extract_source_image(
    message: Any,
    proxy: Optional[str] = None,
    logger=None,
    ctx: Any = None,
    *,
    allow_avatar_fallback: bool = True,
) -> Optional[bytes]:
    """
    从消息对象中提取图片（优先回复 > 消息内图片 > @用户头像）

    Args:
        message: 消息对象
        proxy: 代理地址
        logger: 日志对象（为 None 时不记录日志）
        ctx: 插件上下文，用于走官方 message capability 查询历史消息
        allow_avatar_fallback: 是否允许"拿 @提及用户的头像"这一兜底。
            只对**当前**消息成立——"@某人 + 指令"是在要那个人的头像。
            递归查被回复消息时必须关掉：被回复内容里出现 @某人，
            只是那条消息的正文，不代表用户想要那个人的头像。

    Returns:
        图片字节或 None
    """
    finder = _SourceImageFinder(message, proxy, logger, ctx, allow_avatar_fallback)
    return await finder.run()


async def resolve_reply_images(
    message: Any,
    proxy: Optional[str] = None,
    logger=None,
    ctx: Any = None,
) -> List[bytes]:
    """取回**被回复消息**里的所有图片（多图绘图用）。

    和 extract_source_image 走同样的三条路径（消息段 → 官方消息能力 → 数据库），
    但返回全部图片而非第一张，且永不退化为取头像。

    没有回复、或回复里没有图片时返回空列表。
    """
    finder = _SourceImageFinder(message, proxy, logger, ctx, allow_avatar_fallback=False)
    return await finder.all_reply_images()


class _SourceImageFinder:
    """把取图的三层策略拆开，任一层抛异常都不影响后续层。"""

    def __init__(
        self,
        message: Any,
        proxy: Optional[str],
        logger,
        ctx: Any,
        allow_avatar_fallback: bool = True,
    ):
        self.message = message
        self.proxy = proxy
        self.logger = logger
        self.ctx = ctx
        self.allow_avatar_fallback = allow_avatar_fallback

    def _log(self, level: str, msg: str) -> None:
        if self.logger:
            getattr(self.logger, level)(msg)

    async def run(self) -> Optional[bytes]:
        strategies = [
            ("reply", self._from_reply),
            ("current message", self._from_current),
        ]
        if self.allow_avatar_fallback:
            strategies.append(("at user", self._from_at_user))

        for label, strategy in strategies:
            try:
                img = await strategy()
                if img:
                    return img
            except Exception as e:
                self._log("warning", f"Error extracting from {label}: {e}")
        return None

    # ── 各层策略 ────────────────────────────────────────────

    async def _from_reply(self) -> Optional[bytes]:
        reply = getattr(self.message, 'reply', None)

        # A. 递归查 reply 对象本身（它可能带着完整消息段）。
        #    关掉头像兜底：被回复消息常常只剩一句渲染过的纯文本（如
        #    "@<某人:12345> [图片]"），一旦允许兜底就会误取那个人的头像，
        #    并且短路掉下面真正能拿到原图的 B/C 两条路径。
        if reply is not None and hasattr(reply, 'message_segment'):
            img = await extract_source_image(
                reply, self.proxy, self.logger, self.ctx, allow_avatar_fallback=False
            )
            if img:
                return img

        reply_to_id = self._reply_target_id()
        if not reply_to_id:
            return None

        # B. 官方 message capability
        try:
            message_dict = await self._fetch_via_capability(reply_to_id)
            if isinstance(message_dict, dict):
                img = await self._image_from_message_dict(message_dict, "官方消息能力引用的消息")
                if img:
                    return img
        except Exception as e:
            self._log("warning", f"Failed to fetch reply message via capability: {e}")

        # C. 宿主数据库兜底
        try:
            return await self._image_from_db(reply_to_id, "数据库引用的消息")
        except Exception as e:
            self._log("warning", f"Failed to fetch reply message from DB: {e}")
        return None

    async def _from_current(self) -> Optional[bytes]:
        if hasattr(self.message, 'message_segment'):
            return await extract_first_image_from_segments(
                self.message.message_segment, self.proxy, self.logger
            )

        msg_id = getattr(self.message, 'message_id', None)
        if msg_id:
            try:
                return await self._image_from_db(msg_id, "当前消息(数据库回查)")
            except Exception as e:
                self._log("warning", f"Failed to fetch current message from DB: {e}")
        return None

    async def _from_at_user(self) -> Optional[bytes]:
        segments = getattr(self.message, 'message_segment', None)
        if segments is not None:
            self._debug_dump_segments(segments)
            for user_id in extract_mentioned_user_ids(segments):
                self._log("info", f"使用 @用户 {user_id} 的头像。")
                return await download_avatar(user_id, self.proxy)

        # 数据库消息只有纯文本可用
        text = (
            getattr(self.message, 'processed_plain_text', '')
            or getattr(self.message, 'display_message', '')
            or ''
        )
        self._log("debug", f"[调试] 提取@，当前纯文本: {text[:500]}")
        for user_id in extract_mentioned_ids_from_text(text):
            self._log("info", f"使用 @用户 {user_id} 的头像。")
            return await download_avatar(user_id, self.proxy)

        return None

    # ── 辅助 ────────────────────────────────────────────────

    def _reply_target_id(self) -> Optional[Any]:
        """被回复消息的 message_id。"""
        reply_to_id = getattr(self.message, 'reply_to', None)
        if not reply_to_id:
            reply = getattr(self.message, 'reply', None)
            if reply is not None:
                reply_to_id = getattr(reply, 'message_id', None)
        return reply_to_id or None

    async def all_reply_images(self) -> List[bytes]:
        """被回复消息里的全部图片，路径同 _from_reply 但不取第一张就停。"""
        reply = getattr(self.message, 'reply', None)

        # A. reply 自带的消息段
        if reply is not None and getattr(reply, 'message_segment', None):
            images = await extract_images_from_segments(
                reply.message_segment, self.proxy, self.logger
            )
            if images:
                return images

        reply_to_id = self._reply_target_id()
        if not reply_to_id:
            return []

        # B. 官方 message capability
        try:
            message_dict = await self._fetch_via_capability(reply_to_id)
            if isinstance(message_dict, dict):
                segments = _message_dict_segments(message_dict)
                images = await extract_images_from_segments(segments, self.proxy, self.logger)
                if images:
                    self._log("info", f"从官方消息能力引用的消息中提取到 {len(images)} 张图片")
                    return images
        except Exception as e:
            self._log("warning", f"Failed to fetch reply message via capability: {e}")

        # C. 数据库兜底（只拿得到第一张）
        try:
            img = await self._image_from_db(reply_to_id, "数据库引用的消息")
            if img:
                return [img]
        except Exception as e:
            self._log("warning", f"Failed to fetch reply message from DB: {e}")

        return []

    def _debug_dump_segments(self, segments: Any) -> None:
        if not self.logger:
            return
        preview = [
            ({'type': s.type, 'data': s.data} if hasattr(s, 'type') else str(s))
            for s in normalize_segments(segments)
        ]
        try:
            self.logger.debug(f"[调试] 提取@，当前 segments: {safe_json_dumps(preview)}")
        except TypeError:
            self.logger.debug(f"[调试] 提取@，当前 segments: {str(preview)[:500]}")

    async def _fetch_via_capability(self, message_id: Any) -> Optional[dict]:
        if not self.ctx or not getattr(self.ctx, 'message', None):
            return None

        normalized_message_id = str(message_id or "").strip()
        if not normalized_message_id:
            return None

        stream_id = (
            str(getattr(self.message, 'session_id', '') or '').strip()
            or str(getattr(self.message, 'stream_id', '') or '').strip()
        )
        if not stream_id:
            chat_stream = getattr(self.message, 'chat_stream', None)
            stream_id = str(getattr(chat_stream, 'stream_id', '') or '').strip()

        return await self.ctx.message.get_by_id(
            normalized_message_id,
            stream_id=stream_id,
            include_binary_data=True,
        )

    async def _image_from_message_dict(self, message_dict: dict, source_label: str) -> Optional[bytes]:
        img = await extract_first_image_from_segments(
            _message_dict_segments(message_dict), self.proxy, self.logger
        )
        if img:
            self._log("info", f"从{source_label}中提取到图片")
        return img

    async def _image_from_db(self, message_id: Any, source_label: str) -> Optional[bytes]:
        if not host_bridge.HAS_DB:
            return None
        mai_msg = await host_bridge.fetch_mai_message(message_id)
        if not mai_msg:
            return None
        img = await host_bridge.image_from_mai_message(mai_msg)
        if img:
            self._log("info", f"从{source_label}中提取到图片")
        return img
