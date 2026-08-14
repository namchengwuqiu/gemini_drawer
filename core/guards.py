"""
Gemini Drawer 准入校验

三重开关（总开关 / 群黑名单 / 管理员专用模式）此前在 6 处各写了一遍且互相不一致
（视频命令漏了黑名单，三个 Action 漏了总开关与管理员模式）。这里收敛为唯一实现，
语义以 config.py 中各字段的说明为准：

- general.enable_gemini_drawer: "关闭后插件仍加载，但绘图命令和绘图 Action 不再响应"
- general.blacklist_groups:     "这些群内的绘图、自拍、视频生成功能会被拒绝"
- behavior.admin_only_mode:     "开启后只有管理员 QQ 列表中的用户能使用绘图功能"
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

ADMIN_ONLY_NOTICE = "⚠️ 管理员已关闭绘图功能"


@dataclass
class AccessDecision:
    allowed: bool
    reason: str = ""
    #: 需要回复给用户的提示；None 表示静默拒绝（不打扰群内其他人）
    message: Optional[str] = None

    @property
    def should_stop(self) -> bool:
        """作为 Command 返回值的 stop 位：仅在已回复用户时截断后续处理。"""
        return self.message is not None


def evaluate_access(
    get_config: Callable[..., Any],
    group_id: Any = None,
    user_id: Any = None,
) -> AccessDecision:
    """判断当前上下文是否允许使用绘图功能。"""
    if not get_config("general.enable_gemini_drawer", True):
        return AccessDecision(False, "Plugin disabled")

    blacklist = get_config("general.blacklist_groups", []) or []
    if group_id and blacklist:
        if str(group_id) in {str(g) for g in blacklist}:
            return AccessDecision(False, "群黑名单")

    if get_config("behavior.admin_only_mode", False):
        admins = {str(a) for a in (get_config("general.admins", []) or [])}
        # 取不到 user_id 时不拦截，与重构前的行为一致
        if user_id and str(user_id) not in admins:
            return AccessDecision(False, "管理员专用模式", ADMIN_ONLY_NOTICE)

    return AccessDecision(True)


def is_admin(get_config: Callable[..., Any], user_id: Any) -> bool:
    admins = {str(a) for a in (get_config("general.admins", []) or [])}
    return bool(user_id) and str(user_id) in admins


def message_user_id(message: Any) -> Optional[str]:
    """从消息对象中取发送者 QQ 号。"""
    try:
        user_info = getattr(getattr(message, "message_info", None), "user_info", None)
        user_id = getattr(user_info, "user_id", None)
        return str(user_id) if user_id else None
    except Exception:
        return None
