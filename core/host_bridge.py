"""
Gemini Drawer 宿主桥接层

本模块是插件中**唯一**允许 import 宿主内部模块（src.*）的地方。

插件正常运行只依赖 maibot_sdk 提供的能力；但"取回复消息里的图片"这一路径
在 SDK 的 message capability 拿不到数据时，需要回落到宿主数据库直查。
把这层依赖收敛在一处的好处：

- 宿主重构 src.* 时只需改这一个文件
- 每个入口都做了降级，宿主不可用时插件仍能工作（只是少一条兜底取图路径）
- 单元测试可以在没有宿主的环境里导入插件其余模块

能力位 HAS_DB 表示数据库兜底是否可用。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

# ── 日志 ─────────────────────────────────────────────────────

try:
    from src.common.logger import get_logger as _host_get_logger
except Exception:  # pragma: no cover - 取决于宿主环境
    _host_get_logger = None


def get_plugin_logger(name: str = "plugin.gemini_drawer") -> Any:
    """统一的 logger 入口：优先用宿主 logger，取不到则回落标准库。"""
    if _host_get_logger is not None:
        try:
            return _host_get_logger(name)
        except Exception:
            pass
    return logging.getLogger(name)


logger = get_plugin_logger()


# ── 数据库兜底 ───────────────────────────────────────────────

try:
    from src.common.database.database_model import Messages as _Messages
except Exception:  # pragma: no cover - 取决于宿主环境
    _Messages = None

HAS_DB: bool = _Messages is not None


async def fetch_mai_message(message_id: Any) -> Optional[Any]:
    """按 message_id 从宿主数据库取一条消息并反序列化。

    必须在 SQLAlchemy session 关闭前完成反序列化，因此整段放在同一个线程里跑。
    宿主不可用、查无此条或出错时返回 None。
    """
    if not HAS_DB:
        return None

    normalized_message_id = str(message_id or "").strip()
    if not normalized_message_id:
        return None

    try:
        from sqlmodel import select

        from src.common.data_models.mai_message_data_model import MaiMessage
        from src.common.database.database import get_db_session
    except Exception as e:
        logger.debug(f"[宿主桥接] 数据库依赖不可用: {e}")
        return None

    def _fetch():
        with get_db_session(auto_commit=False) as session:
            statement = (
                select(_Messages)
                .where(_Messages.message_id == normalized_message_id)
                .limit(1)
            )
            db_msg = session.exec(statement).first()
            return MaiMessage.from_db_instance(db_msg) if db_msg else None

    return await asyncio.to_thread(_fetch)


async def image_from_mai_message(mai_msg: Any) -> Optional[bytes]:
    """从宿主的 MaiMessage 对象里加载第一张图片/表情的二进制数据。"""
    if mai_msg is None:
        return None

    try:
        from src.common.data_models.message_component_data_model import (
            EmojiComponent,
            ImageComponent,
        )
    except Exception as e:
        logger.debug(f"[宿主桥接] 消息组件依赖不可用: {e}")
        return None

    for comp in mai_msg.raw_message.components:
        if isinstance(comp, ImageComponent):
            await comp.load_image_binary()
        elif isinstance(comp, EmojiComponent):
            await comp.load_emoji_binary()
        else:
            continue

        if comp.binary_data:
            return comp.binary_data
    return None
