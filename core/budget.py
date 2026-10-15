"""整体时间预算（deadline）控制。

宿主对命令/工具的 RPC 调用有固定超时（见 plugin.py 的
``DRAW_COMMAND_TIMEOUT_MS`` / ``VIDEO_COMMAND_TIMEOUT_MS``）。宿主超时后只会
移除等待对象并返回错误，不会向插件侧发送取消；插件若继续生成并发送，就会出现
"宿主已报失败、图片仍然发出"的事故。

因此一次完整流程（参考图获取 → 生成 → 结果下载 → 媒体发送）共享同一个
deadline：

- 预算 = RPC 上限 − 宿主返回余量，给宿主超时返回与收尾留时间；
  ``remaining`` 返回的就是**已扣除余量**的可用时间，``can_start`` /
  ``stage_timeout`` / ``expired`` 全部基于它，任何阶段都不能再拿宿主余量
  去开始新请求；
- 每个阶段开始前检查剩余预算，期限耗尽后不得开始新的生成、下载或发送；
  下载完成之后、每次真实发送及回退之前也要重新检查；
- 单次 HTTP 请求的 timeout 收敛到剩余预算内，避免一个端点吃满全部时间；
  持续有数据的流式响应、异步任务轮询等长等待阶段需要额外的绝对截止控制。

注意：``asyncio.wait_for`` 之类的取消只终止本地等待，不能撤回上游已提交的
任务。预算耗尽同样要遵守 core/errors.py 的提交状态分类：不自动换端点重试。
"""
from __future__ import annotations

import asyncio
import time
from contextvars import ContextVar
from functools import wraps
from typing import Awaitable, Callable, Optional, TypeVar

T = TypeVar("T")
_active_budget: ContextVar[Optional["TimeBudget"]] = ContextVar("media_budget", default=None)

#: 给宿主 RPC 超时返回预留的余量（秒）
HOST_RETURN_MARGIN_SECONDS = 20.0

#: 进入一个新端点/阶段所需的最小剩余时间（秒）；低于此值不再开始
MIN_STAGE_SECONDS = 5.0

#: 宿主对图片命令/工具的 RPC 超时（毫秒）。组件注册（plugin.py）与命令层
#: 建立预算共用这一处定义，避免两边漂移。
DRAW_COMMAND_TIMEOUT_MS = 300_000
#: 宿主对视频命令/工具的 RPC 超时（毫秒）
VIDEO_COMMAND_TIMEOUT_MS = 600_000
#: 视频流程给宿主返回与收尾预留的余量（秒）
VIDEO_HOST_RETURN_MARGIN_SECONDS = 30.0


class BudgetExpiredError(RuntimeError):
    """整体时间预算耗尽，不应再开始新的生成或发送。"""


class MediaDeliveryUncertain(str):
    """带投递状态的内部文案，兼容 Action/发送函数现有的 tuple 返回接口。

    转成普通字符串之前由工具层识别，不能把投递未确认误当成生成失败。
    """

    def __new__(cls):
        return super().__new__(cls, (
            "视频已经生成，并已尝试投递，但发送回执未确认，投递状态不确定；视频可能已经送达。"
            "为避免重复生成和重复发送，本次不再重试。禁止自动再次调用视频工具，"
            "不要声称视频生成失败或已经确认送达；请等待用户确认是否收到。"
        ))


class TimeBudget:
    """一次命令/工具流程的整体时间预算。"""

    __slots__ = ("_clock", "_deadline", "_reserve", "media_sent", "media_delivery_pending")

    def __init__(
        self,
        total_seconds: float,
        *,
        reserve_seconds: float = HOST_RETURN_MARGIN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = clock
        self._reserve = max(0.0, float(reserve_seconds))
        self._deadline = clock() + max(0.0, float(total_seconds))
        self.media_sent = False
        self.media_delivery_pending = False

    @classmethod
    def from_rpc_timeout_ms(
        cls,
        timeout_ms: int,
        *,
        reserve_seconds: float = HOST_RETURN_MARGIN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> "TimeBudget":
        """按宿主 RPC 超时（毫秒）建立预算，并扣除返回余量。"""
        return cls(float(timeout_ms) / 1000.0, reserve_seconds=reserve_seconds, clock=clock)

    @property
    def remaining(self) -> float:
        """可用于新阶段的剩余秒数（已扣除宿主返回余量）。"""
        return max(0.0, self._deadline - self._clock() - self._reserve)

    @property
    def time_left(self) -> float:
        """距 deadline 的原始剩余秒数（含宿主返回余量），仅用于日志/诊断。"""
        return max(0.0, self._deadline - self._clock())

    def can_start(self, *, reserve: float = 0.0, minimum: float = MIN_STAGE_SECONDS) -> bool:
        """当前是否还值得开始一个新阶段；reserve 为该阶段之后仍需保留的秒数。"""
        return self.remaining - max(0.0, reserve) >= minimum

    def expired(self) -> bool:
        """预算是否已耗尽（只剩宿主返回余量）。"""
        return self.remaining <= 0.0

    def stage_timeout(self, default: float, *, reserve: float = 0.0, minimum: float = 1.0) -> float:
        """把某阶段的超时收敛到剩余预算内；预算耗尽时抛出 BudgetExpiredError。"""
        available = self.remaining - max(0.0, reserve)
        if available <= 0:
            raise BudgetExpiredError("时间预算已耗尽，未开始新的请求")
        # minimum 只用于旧调用兼容，不能把 timeout 抬到剩余预算之外。
        return min(float(default), available)

    async def run(self, operation: Callable[[], Awaitable[T]], default: float = float("inf")) -> T:
        """先检查期限再创建协程，取消整个阶段而非仅限制单次网络读取。"""
        timeout = self.stage_timeout(default)
        async with asyncio.timeout(timeout):
            result = await operation()
        if self.expired():
            raise BudgetExpiredError("时间预算已耗尽，已停止后续处理")
        return result


def current_budget() -> Optional[TimeBudget]:
    return _active_budget.get()


def budgeted_entry(timeout_ms: int, *, reserve: float = HOST_RETURN_MARGIN_SECONDS, command: bool = False):
    """命令/工具入口的绝对期限，覆盖前置提示、文件读取、收尾与全部发送。"""
    def decorate(func):
        @wraps(func)
        async def wrapped(self, *args, **kwargs):
            budget = current_budget() or TimeBudget.from_rpc_timeout_ms(
                getattr(self, "rpc_timeout_ms", timeout_ms),
                reserve_seconds=getattr(self, "budget_reserve_seconds", reserve),
            )
            token = _active_budget.set(budget)
            try:
                return await budget.run(lambda: func(self, *args, **kwargs))
            except (TimeoutError, BudgetExpiredError):
                # 已发出的远端操作不能撤销，不发送新的提示或触发备用发送。
                if budget.media_sent:
                    message = "已确认媒体发送；后续处理超时已停止，请勿立即重复操作"
                    return (True, message, True) if command else (True, message)
                if budget.media_delivery_pending:
                    message = MediaDeliveryUncertain()
                    return (False, message, True) if command else (False, message)
                message = "时间预算耗尽，已停止等待；已提交的生成或发送状态可能不确定，请勿立即重复操作"
                return (False, message, True) if command else (False, message)
            finally:
                _active_budget.reset(token)
        return wrapped
    return decorate
