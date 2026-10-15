"""
Gemini Drawer 绘图管线

整个插件唯一的"遍历端点 → 发请求 → 取图 → 记账 → 失败换下一个"循环。
命令（/绘图 /bnn /+ /随机 /多图）与 Action（自然语言绘图、自拍）共用此入口。

协议差异全部下沉到 providers/，本模块只负责编排与容错：

- 整体时间预算（core/budget.py）：预算耗尽后不再开始新的端点或请求；每次
  任务协议的 fetch 使用独立任务期限，一次性请求使用该请求的绝对期限；
- HTTP 错误响应继续切换端点；请求已发出但未收到成功响应时，读写超时或单端点
  等待超时也在总预算内继续轮换（可能重复生成）；已接收结果的异常不再换端点；
- Key 记账通过工作线程执行，不在事件循环里做同步文件读写；记账失败只记
  警告，绝不影响本次生成结果或触发换端点。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

import httpx

from ..utils.log import set_request_secrets, reset_request_secrets
from .budget import BudgetExpiredError, TimeBudget
from .errors import (
    SUBMISSION_UNCERTAIN,
    GenerationState,
    SubmissionUncertainError,
    UpstreamHTTPError,
    describe_error,
    submission_state_for,
)
from .managers import key_manager
from ..providers import DrawRequest, Endpoint, describe_protocol, resolve_provider
from .protocols import normalize_protocol
from ..utils import MediaTooLargeError, logger, redact_url, safe_json_dumps
from ..utils.limits import MAX_RESULT_BASE64_CHARS

#: lmarena 是无 Key 的中转，不参与 Key 健康度记账
KEYLESS_ENDPOINT_TYPES = {"lmarena"}
#: 端点之间的退避间隔
RETRY_DELAY = 1.0
#: 生成阶段之后（结果下载 + 发送）需要保留的时间
POST_GENERATION_RESERVE_SECONDS = 30.0
#: 单个端点至少需要这么多剩余时间才值得尝试
MIN_ENDPOINT_SECONDS = 5.0

BUDGET_EXHAUSTED_MESSAGE = "本次生成已超出时间预算，剩余端点未再尝试，也没有发送任何内容"
UNCERTAIN_SUBMISSION_SUFFIX = "；上游可能已接收本次请求，为避免重复生成，未自动切换其他端点"


@dataclass
class DrawingAttempts:
    count: int = 0


async def _record(endpoint: Endpoint, success: bool, force_disable: bool = False) -> None:
    if endpoint.type in KEYLESS_ENDPOINT_TYPES:
        return
    # 记账会同步读写 keys.json 并持有锁，放到工作线程，避免阻塞事件循环。
    # 记账失败（磁盘满、文件损坏等）只是统计问题：成功生成后的记账异常若
    # 冒泡，会重新进入失败转移循环造成重复提交（F7），因此这里只记警告。
    try:
        await asyncio.to_thread(key_manager.record_key_usage, endpoint.key, success, force_disable)
    except Exception as e:
        logger.warning(f"Key 记账失败（不影响本次生成结果）: {type(e).__name__}")


def _validate_result_size(img_data: List[str]) -> None:
    """限制生成结果里内联 base64 的大小，避免超大结果进入下载/发送链路。"""
    for item in img_data:
        if not isinstance(item, str):
            continue
        if item.startswith(("http://", "https://")):
            continue
        if len(item) > MAX_RESULT_BASE64_CHARS:
            raise MediaTooLargeError(
                f"生成结果图片数据过大（{len(item)} 字符，上限 {MAX_RESULT_BASE64_CHARS}），已拒绝处理"
            )


async def run_drawing(
    request: DrawRequest,
    endpoints: Iterable[Union[Endpoint, Dict[str, Any]]],
    proxy: Optional[str],
    logger: Any,
    debug_mode: bool = False,
    budget: Optional[TimeBudget] = None,
    attempts: Optional[DrawingAttempts] = None,
) -> Tuple[List[str], str]:
    """依次尝试各端点直到成功。

    Args:
        budget: 整体时间预算；预算不足时不再开始新的端点，期限耗尽后
            不发起任何新的生成请求。

    Returns:
        (图片数据列表, 最终错误信息)。成功时错误信息为空串；
        失败时列表为空，错误信息为最后一个端点的失败原因。
    """
    endpoint_list = [
        ep if isinstance(ep, Endpoint) else Endpoint.from_dict(ep) for ep in endpoints
    ]
    last_error = ""

    for i, endpoint in enumerate(endpoint_list):
        if budget is not None and not budget.can_start(
            reserve=POST_GENERATION_RESERVE_SECONDS, minimum=MIN_ENDPOINT_SECONDS
        ):
            logger.warning(
                f"时间预算不足，跳过剩余 {len(endpoint_list) - i} 个端点，不再发起新的生成请求"
            )
            if not last_error:
                last_error = BUDGET_EXHAUSTED_MESSAGE
            break

        if endpoint.protocol and normalize_protocol(endpoint.protocol) is None:
            logger.warning(
                f"端点 {endpoint.type} 配置的协议 '{endpoint.protocol}' 无法识别，改为自动识别。"
                f"可用 /渠道设置协议 重新设置。"
            )
        protocol, explicit = describe_protocol(endpoint)
        provider = resolve_provider(endpoint)
        if provider is None:
            logger.warning(
                f"无法识别的API地址格式: {redact_url(endpoint.url)}，跳过。"
                f"请检查配置，或用 /渠道设置协议 {endpoint.type.removeprefix('custom_')} <协议> 显式指定。"
            )
            continue

        logger.info(
            f"尝试第 {i + 1}/{len(endpoint_list)} 个端点: {endpoint.type} "
            f"({redact_url(endpoint.url)}) [协议: {protocol}{'' if explicit else '(自动)'}]"
        )

        if attempts is not None:
            attempts.count += 1
        state = GenerationState()
        log_token = set_request_secrets(endpoint.key)
        try:
            call = provider.build(endpoint, request)
            payload = call.json if call.json is not None else call.data
            logger.info(
                f"准备向 {endpoint.type} 端点发送请求。URL: {call.safe_url}, "
                f"Payload: {safe_json_dumps(payload)}"
            )

            timeout = call.timeout
            if budget is not None:
                timeout = budget.stage_timeout(
                    call.timeout, reserve=POST_GENERATION_RESERVE_SECONDS
                )

            async with httpx.AsyncClient(
                headers={"Accept-Encoding": "identity"},
                proxy=proxy if call.use_proxy else None,
                timeout=timeout,
                follow_redirects=True,
                event_hooks={"response": [state.observe]},
            ) as client:
                # 整个 fetch 的绝对上限：任务型协议（建任务 + 轮询）用自身的
                # 任务阶段上限，一次性/流式协议用单次请求的 timeout。单次
                # HTTPX timeout 只约束单次请求，流式响应持续有数据时可以被
                # 超过，所以两者都不能省。取消只终止本地等待，不能撤回上游
                # 已提交的任务，因此超时的处理与"提交结果不确定"一致：不换端点。
                fetch_cap: Optional[float] = provider.task_timeout or call.timeout
                if budget is not None:
                    fetch_cap = budget.stage_timeout(
                        fetch_cap if fetch_cap is not None else call.timeout,
                        reserve=POST_GENERATION_RESERVE_SECONDS,
                    )
                if fetch_cap is not None:
                    img_data = await asyncio.wait_for(
                        provider.fetch(call, client, logger, debug_mode),
                        timeout=fetch_cap,
                    )
                else:
                    img_data = await provider.fetch(call, client, logger, debug_mode)
                state.accepted = True

            if not img_data:
                raise SubmissionUncertainError("API响应未包含可提取的图片，生成结果不确定")

            _validate_result_size(img_data)
            # 结果已经生成：记账失败不能回到生成循环，也不能换端点重试（F7）。
            await _record(endpoint, True)
            logger.info(f"使用 {endpoint.type} 端点成功生成 {len(img_data)} 张图片")
            return img_data, ""

        except BudgetExpiredError as e:
            logger.warning(f"端点 {endpoint.type} 因时间预算耗尽中止: {e}")
            last_error = str(e)
            break
        except (asyncio.TimeoutError, httpx.ReadTimeout, httpx.WriteTimeout, SubmissionUncertainError) as e:
            if state.is_unconfirmed_timeout(e):
                # 请求已发出但没等到成功响应。上游可能仍在生成，换端点意味着
                # 可能重复出图与计费；但只试一个端点就失败同样不可接受，因此
                # 在总预算内继续轮换（已接收结果的异常不走这里）。
                last_error = f"端点 {endpoint.type} 等待响应超时，服务端结果尚未确认"
                if i + 1 >= len(endpoint_list):
                    last_error += "；没有更多可用端点"
                    break
                if budget is not None and not budget.can_start(
                    reserve=POST_GENERATION_RESERVE_SECONDS, minimum=MIN_ENDPOINT_SECONDS
                ):
                    last_error += "；剩余时间不足，未继续尝试"
                    break
                logger.warning(f"{last_error}；继续尝试下一个端点")
                await asyncio.sleep(RETRY_DELAY)
                continue
            if not isinstance(e, asyncio.TimeoutError):
                last_error = f"端点 {endpoint.type}: {describe_error(e)}{UNCERTAIN_SUBMISSION_SUFFIX}"
                logger.warning(last_error)
                break
            logger.warning(f"端点 {endpoint.type} 超出时间预算，已停止等待")
            last_error = (
                f"生成请求超出时间预算（端点 {endpoint.type}），已停止等待"
                f"{UNCERTAIN_SUBMISSION_SUFFIX}"
            )
            break
        except MediaTooLargeError as e:
            logger.warning(f"端点 {endpoint.type} 返回的图片数据过大: {e}")
            last_error = str(e)
            break
        except UpstreamHTTPError as e:
            if state.prevents_retry(e):
                logger.warning(
                    f"端点 {endpoint.type} 提交结果不确定: HTTP {e.status_code}，不再切换其他端点"
                )
                last_error = f"端点 {endpoint.type}: {e.user_message()}{UNCERTAIN_SUBMISSION_SUFFIX}"
                break
            if e.submission_state == SUBMISSION_UNCERTAIN:
                # HTTP 故障允许渠道回退，但不因此把 Key 记为失效。
                pass
            elif e.is_quota_exhausted:
                await _record(endpoint, False, force_disable=True)
            elif e.is_rate_limited:
                # 限流是可恢复冷却，不累计错误次数，避免误禁仍可用的 Key
                logger.warning(f"端点 {endpoint.type} 被限流 (HTTP 429)，不计入 Key 错误次数")
            else:
                await _record(endpoint, False)
            logger.warning(
                f"端点 {endpoint.type} 尝试失败: HTTP {e.status_code} (stage={e.stage}): {e.body[:300]}"
            )
            last_error = f"端点 {endpoint.type}: {e.user_message()}"
            await asyncio.sleep(RETRY_DELAY)
        except Exception as e:
            # 原始异常文本可能带查询串里的 key 或内网地址，日志与文案统一用脱敏结果。
            detail = describe_error(e)
            if state.prevents_retry(e) or submission_state_for(e) == SUBMISSION_UNCERTAIN:
                logger.warning(
                    f"端点 {endpoint.type} 提交结果不确定: {type(e).__name__}: {detail}，不再切换其他端点"
                )
                last_error = f"端点 {endpoint.type}: {detail}{UNCERTAIN_SUBMISSION_SUFFIX}"
                break
            logger.warning(f"端点 {endpoint.type} 尝试失败: {type(e).__name__}: {detail}")
            await _record(endpoint, False)
            last_error = f"端点 {endpoint.type}: {detail}"
            await asyncio.sleep(RETRY_DELAY)

        finally:
            reset_request_secrets(log_token)

    if not last_error:
        last_error = "没有可用的绘图端点（所有渠道均无法识别）"
    return [], last_error
