"""主动绘图的持久化次数与冷却；提交前记账，失败也不自动重试。"""

import asyncio
import json
import math
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ..utils.config_file import _atomic_write_text


class AutonomousQuota:
    def __init__(self, path: Path):
        self.path = path
        self.lock = asyncio.Lock()
        self.error = False
        self.rows = {}
        try:
            if path.exists():
                saved = json.loads(path.read_text(encoding="utf-8"))
                if (
                    not isinstance(saved, dict)
                    or saved.get("version") != 1
                    or not isinstance(saved.get("streams"), dict)
                ):
                    raise ValueError("主动绘图记录格式错误")
                for sid, row in saved["streams"].items():
                    if (
                        not isinstance(row, dict)
                        or not isinstance(row.get("day"), str)
                        or type(row.get("count")) is not int
                        or row["count"] < 0
                        or type(row.get("last")) not in (int, float)
                        or not math.isfinite(row["last"])
                        or row["last"] < 0
                    ):
                        raise ValueError("主动绘图记录无效")
                    datetime.strptime(row["day"], "%Y-%m-%d")
                self.rows = saved["streams"]
        except (OSError, ValueError, TypeError):
            self.error = True

    def blocked(self, sid: str, config, now: float) -> str:
        if self.error:
            return "主动绘图记录无法读取或保存，已暂停主动绘图，请管理员检查。"
        if config.daily_limit == 0 or config.global_daily_limit == 0:
            return "主动绘图每日额度为 0，已暂停主动绘图。"
        day = datetime.fromtimestamp(now, ZoneInfo(config.timezone)).date().isoformat()
        row = self.rows.get(sid, {})
        if row.get("day") == day and row.get("count", 0) >= config.daily_limit:
            return "本会话今天的主动绘图次数已用完。"
        total = sum(r["count"] for r in self.rows.values() if r["day"] == day)
        if total >= config.global_daily_limit:
            return "今天所有会话的主动绘图总次数已用完。"
        if now - row.get("last", 0) < config.cooldown_seconds:
            return "主动绘图仍在冷却，请继续自然聊天。"
        return ""

    async def reserve(self, sid: str, config, now: float) -> str:
        async with self.lock:
            reason = self.blocked(sid, config, now)
            if reason:
                return reason
            day = (
                datetime.fromtimestamp(now, ZoneInfo(config.timezone))
                .date()
                .isoformat()
            )
            # 保留跨零点冷却；冷却配置至多一天，所以两天以前的记录可清除。
            rows = {
                k: v
                for k, v in self.rows.items()
                if v["day"] == day or now - v["last"] < 172800
            }
            if sid not in rows and len(rows) >= 2048:
                return "主动绘图会话记录已满，本次未提交。"
            previous = rows.get(sid, {})
            rows[sid] = {
                "day": day,
                "count": (previous.get("count", 0) if previous.get("day") == day else 0)
                + 1,
                "last": now,
            }
            try:
                # 小型有界 JSON 同步原子替换，保证取消也不会释放未确认的收费任务额度。
                _atomic_write_text(
                    self.path, json.dumps({"version": 1, "streams": rows})
                )
            except OSError:
                self.error = True
                return "主动绘图记录保存失败，本次未提交，请管理员检查。"
            self.rows = rows
            return ""
