"""
Gemini Drawer 视频生成

视频侧按 API 协议分流。Agnes 的创建/轮询逻辑放在 agnes_video 中；
既有豆包、OpenAI 兼容与 Gemini 渠道保持原有分支。

失败响应与已接收状态共同决定是否换渠道，与图片链路保持一致：

- 尚未收到创建成功响应时，HTTP 错误（含 5xx/408）→ 尝试下一渠道；服务端故障与限流不记 Key 失效；
- 已收到创建成功响应 → 停止自动切换；尚未收到成功响应时，读写超时或单渠道
  等待超时也在总预算内轮换（可能重复生成）；
- 成片下载失败 → 只重试下载同一结果（先带认证头、再不带），绝不重新生成。

整体时间预算（core/budget.py）覆盖创建、轮询、下载与发送；期限耗尽后不再
开始新的渠道，单个渠道的创建/轮询/成片下载另有独立的绝对截止控制（F1）。
视频仅通过配置的 OneBot 正向 HTTP 端口发送，读取简短发送响应；
不调用 SDK 或 get_msg，也不在确认异常后重新生成或发送。
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from . import agnes_video
from .agnes_video import (
    generate_agnes_video,
    matches_agnes_video_endpoint,
)
from ..utils.log import set_request_secrets, reset_request_secrets
from .budget import BudgetExpiredError, MediaDeliveryUncertain, TimeBudget
from .channel_url import with_api_key
from ..utils.http import request_capped, read_body_capped, iter_sse_lines
from ..utils.net import same_origin, parse_http_url, DownloadNotAllowedError
from ..utils import limits
from .errors import (
    SUBMISSION_UNCERTAIN,
    GenerationState,
    GenerationRejectedError,
    SubmissionUncertainError,
    UpstreamHTTPError,
    describe_error,
    submission_state_for,
)
from .managers import key_manager
from .protocols import VIDEO_PROTOCOLS, normalize_protocol
from ..utils import (
    MediaTooLargeError,
    extract_http_error_reason,
    extract_video_data,
    logger,
    stream_download,
)
from ..utils.limits import MAX_SSE_ACCUMULATED_CHARS, MAX_VIDEO_BASE64_CHARS, MAX_VIDEO_BYTES

#: 生成阶段之后（成片下载 + 发送）需要保留的时间
POST_GENERATION_RESERVE_SECONDS = 60.0
#: 单个渠道至少需要这么多剩余时间才值得尝试
MIN_ENDPOINT_SECONDS = 5.0
#: 成片下载的尝试次数（第 1 次带认证头，第 2 次不带）
DOWNLOAD_ATTEMPTS = 2
#: 单个渠道生成阶段的安全上限（秒）。协议自身的截止控制（Agnes 的生成总超时、
#: 豆包的轮询预算检查）优先，这里只兜底"持续有数据、协议内检查覆盖不到"
#: 的长等待；仍会被剩余预算进一步收敛（F1）。
VIDEO_CHANNEL_SAFETY_SECONDS = 480.0

UNCERTAIN_SUBMISSION_SUFFIX = "；上游可能已接收本次任务，为避免重复生成，未自动切换其他渠道"
BUDGET_EXHAUSTED_MESSAGE = "本次视频生成已超出时间预算，剩余渠道未再尝试，也没有发送任何内容"


class VideoResultUnavailableError(RuntimeError):
    """成片已经生成、但结果不可用（下载失败等）：不得换渠道重新生成。"""


def detect_video_protocol(endpoint: Dict[str, Any]) -> Optional[str]:
    """按 URL / 模型名自动识别视频协议（忽略显式 protocol）；识别不到返回 None。

    判定顺序与重构前的 if/elif 链保持一致。
    """
    api_url = str(endpoint.get("url") or "")
    if matches_agnes_video_endpoint(endpoint):
        return "agnes_video"
    if "volces.com" in api_url or "/contents/generations/tasks" in api_url:
        return "seedance_video"
    if "/chat/completions" in api_url:
        return "openai_chat"
    if "generateContent" in api_url:
        return "gemini"
    return None


def describe_video_protocol(endpoint: Dict[str, Any]) -> Tuple[Optional[str], bool]:
    """返回 (视频协议 id, 是否显式指定)；显式值不合法时回退自动识别。"""
    explicit = normalize_protocol(endpoint.get("protocol"))
    if explicit in VIDEO_PROTOCOLS:
        return explicit, True
    return detect_video_protocol(endpoint), False


async def _record_key_success(api_key: str) -> None:
    """Key 记账会同步读写 keys.json，放到工作线程执行。

    记账失败只是统计问题，绝不能让异常冒泡回渠道循环造成重复提交（F7）。
    """
    try:
        await asyncio.to_thread(key_manager.record_key_usage, api_key, True)
    except Exception as e:
        logger.warning(f"[视频] Key 记账失败（不影响本次结果）: {type(e).__name__}")


async def _record_key_failure(api_key: str, *, force_disable: bool) -> None:
    try:
        await asyncio.to_thread(
            key_manager.record_key_usage, api_key, False, force_disable=force_disable
        )
    except Exception as e:
        logger.warning(f"[视频] Key 记账失败（不影响本次结果）: {type(e).__name__}")


async def _submit_request(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: Dict[str, str],
    json_payload: Dict[str, Any],
    stage: str = "创建视频任务",
) -> httpx.Response:
    """发送创建任务的请求，并按提交状态分类失败。

    Raises:
        SubmissionUncertainError: 请求已发出但结果未知，不得自动重复提交。
        UpstreamHTTPError: 上游明确返回了非 200 状态码。
    """
    try:
        response = await request_capped(client, "POST", url, json=json_payload, headers=headers)
    except httpx.HTTPError as exc:
        if submission_state_for(exc) == SUBMISSION_UNCERTAIN:
            raise SubmissionUncertainError(
                f"{stage}的提交结果不确定（{type(exc).__name__}），"
                "服务端可能已接收任务；为避免重复生成，不自动重试"
            ) from exc
        raise
    if response.status_code != 200:
        raise UpstreamHTTPError(
            response.status_code,
            stage="submit",
            body=response.text,
            reason=extract_http_error_reason(response),
        )
    return response


async def _download_video_result(
    video_url: str,
    *,
    api_url: str,
    api_key: str,
    proxy: Optional[str],
    logger: Any,
    budget: Optional[TimeBudget] = None,
) -> str:
    """下载成片并转 base64；失败时只重试下载同一结果，绝不重新生成。

    第 1 次尝试带认证头（仅当成片与创建接口同域），第 2 次不带，
    与旧实现的重试顺序一致。

    Raises:
        RuntimeError / MediaTooLargeError: 最终失败时抛出，调用方不得切换渠道。
    """
    headers: Dict[str, str] = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "video/mp4,video/*,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": video_url.split("?")[0],
    }
    if parse_http_url(api_url).scheme == "https" and parse_http_url(video_url).scheme != "https":
        raise DownloadNotAllowedError("拒绝从 HTTPS 视频接口下载 HTTP 明文结果")
    attempts: List[Dict[str, str]] = []
    if api_key and same_origin(api_url, video_url):
        attempts.append({**headers, "Authorization": f"Bearer {api_key}"})
    attempts.append(headers)

    last_error: Optional[BaseException] = None
    for attempt, attempt_headers in enumerate(attempts[:DOWNLOAD_ATTEMPTS], 1):
        timeout = 120.0
        if budget is not None:
            timeout = budget.stage_timeout(120.0, minimum=5.0)
        try:
            media = await stream_download(
                video_url,
                proxy=proxy,
                headers=attempt_headers,
                timeout=timeout,
                max_bytes=MAX_VIDEO_BYTES,
            )
        except Exception as e:
            last_error = e
            logger.warning(
                f"[视频] 成片下载失败（第 {attempt}/{min(len(attempts), DOWNLOAD_ATTEMPTS)} 次）: "
                f"{type(e).__name__}（已省略上游原文）"
            )
            if isinstance(e, MediaTooLargeError):
                break
            continue
        logger.info(f"[视频] 视频下载完成，大小: {len(media.data)} 字节")
        return base64.b64encode(media.data).decode("utf-8")

    raise RuntimeError(
        f"视频URL获取成功但下载失败（已重试 {min(len(attempts), DOWNLOAD_ATTEMPTS)} 次，未重新生成）: "
        f"{type(last_error).__name__}"
    )


async def process_video_generation(
    prompt: str,
    base64_img: Optional[str],
    mime_type: Optional[str],
    endpoints: List[Dict[str, Any]],
    proxy: Optional[str],
    logger,
    debug_mode: bool = False,
    budget: Optional[TimeBudget] = None,
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
        budget: 整体时间预算；预算耗尽后不再开始新的渠道
    """
    last_error = ""

    def stage_timeout(default: float) -> float:
        if budget is None:
            return default
        return budget.stage_timeout(default, reserve=POST_GENERATION_RESERVE_SECONDS)

    async def generate_with_protocol(
        endpoint: Dict[str, Any],
        protocol: Optional[str],
        api_url: str,
        api_key: str,
        state: GenerationState,
    ) -> Optional[str]:
        """按协议生成一次视频并下载成片，返回 base64 或 None。

        成片下载失败抛 VideoResultUnavailableError（调用方不得换渠道重新生成）；
        提交/轮询失败抛结构化异常，由外层按提交状态分类处理。
        """
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}"
        }
        video_data: Optional[str] = None

        # Agnes Videos API：独立任务协议，成片复用下方下载/发送链路。
        if protocol == "agnes_video":
            video_url = await generate_agnes_video(
                endpoint, prompt, base64_img, mime_type, proxy, logger,
                # 动态读取模块属性：Agnes 的生成总超时是它自己的截止控制，
                # 外层安全上限只做兜底，不能抢先于它触发。
                timeout=stage_timeout(agnes_video.GENERATION_TIMEOUT), state=state,
            )
            video_data = f"url:{video_url}"

        # 豆包 API (异步任务模式)
        elif protocol == "seedance_video":
            doubao_content = [{"type": "text", "text": prompt}]
            if base64_img:
                doubao_content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime_type};base64,{base64_img}"}
                })

            model_name = endpoint.get("model") or "doubao-seedance-1-5-pro-251215"
            doubao_payload = {
                "model": model_name,
                "content": doubao_content,
                "duration": -1,  # 自动时长: 模型在 4~12 秒范围内自主选择
                "resolution": "1080p"
            }

            async with httpx.AsyncClient(headers={"Accept-Encoding": "identity"}, proxy=proxy, timeout=stage_timeout(60.0), follow_redirects=True, event_hooks={"response": [state.observe]}) as client:
                response = await _submit_request(
                    client, api_url, headers=headers, json_payload=doubao_payload,
                )

                try:
                    created = response.json()
                except ValueError as exc:
                    raise SubmissionUncertainError(
                        "豆包视频任务提交后返回了无效 JSON，无法确认任务状态；"
                        "为避免重复生成，不自动切换渠道"
                    ) from exc
                task_id = created.get("id") if isinstance(created, dict) else None
                if not task_id:
                    # 生成请求已被接受（HTTP 200）却拿不到任务 ID：任务可能已在
                    # 服务端创建，无法恢复也无法证明未创建，不自动换渠道（F7）。
                    raise SubmissionUncertainError(
                        "未获取到豆包视频任务ID，无法确认任务是否已创建；"
                        "为避免重复生成，不自动切换渠道"
                    )

                logger.info("[视频] 豆包任务已创建，开始查询同一任务")

                # 轮询任务状态
                poll_url = f"{api_url}/{task_id}"
                for poll_count in range(120):  # 最多10分钟
                    if budget is not None and not budget.can_start(
                        reserve=POST_GENERATION_RESERVE_SECONDS, minimum=MIN_ENDPOINT_SECONDS
                    ):
                        raise BudgetExpiredError(
                            "视频轮询超出时间预算，已停止等待；上游任务可能仍在运行"
                        )
                    await asyncio.sleep(5)
                    try:
                        poll_resp = await request_capped(client, "GET", poll_url, headers=headers)
                    except httpx.HTTPError as exc:
                        # 任务已创建：查询阶段的网络错误只重试同一任务，绝不重新提交
                        logger.warning(f"[视频] 豆包查询暂时失败: {type(exc).__name__}，继续查询同一任务")
                        continue
                    if poll_resp.status_code != 200:
                        continue
                    try:
                        poll_data = poll_resp.json()
                    except ValueError:
                        continue
                    if not isinstance(poll_data, dict):
                        continue
                    status = poll_data.get("status")

                    if status == "succeeded":
                        content = poll_data.get("content", {})
                        video_url = None
                        if isinstance(content, list) and len(content) > 0 and isinstance(content[0], dict):
                            video_url = content[0].get("video_url") or content[0].get("url")
                        elif isinstance(content, dict):
                            video_url = content.get("video_url") or content.get("url")

                        if isinstance(video_url, str) and video_url:
                            video_data = f"url:{video_url}"
                            break
                        continue
                    elif status == "failed":
                        raise GenerationRejectedError("豆包视频任务明确报告生成失败")
                else:
                    raise SubmissionUncertainError("视频任务轮询超时，已停止等待同一任务")

        # OpenAI 格式
        elif protocol == "openai_chat":
            content_list = [{"type": "text", "text": prompt}]
            if base64_img:
                content_list.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime_type};base64,{base64_img}"}
                })

            openai_payload = {
                "model": endpoint.get("model") or "video-preview",
                "messages": [{"role": "user", "content": content_list}],
                "stream": endpoint.get("stream", False),
                "video_config": {
                    "video_length": 10,
                    "resolution_name": "720p"
                }
            }

            use_stream = endpoint.get("stream", False)

            async with httpx.AsyncClient(headers={"Accept-Encoding": "identity"}, proxy=proxy, timeout=stage_timeout(300.0), follow_redirects=True, event_hooks={"response": [state.observe]}) as client:
                if use_stream:
                    # 流式模式：累积所有 content，流结束后统一提取
                    accumulated_content = ""
                    try:
                        async with client.stream("POST", api_url, json=openai_payload, headers=headers) as response:
                            if response.status_code != 200:
                                raw_body = await read_body_capped(response, limits.MAX_ERROR_BODY_BYTES)
                                raise UpstreamHTTPError(
                                    response.status_code,
                                    stage="submit",
                                    body=raw_body.decode('utf-8', 'ignore'),
                                    reason="",
                                )

                            async for line in iter_sse_lines(response):
                                line = line.strip()
                                if not line or line.startswith(':'):
                                    continue
                                if line.startswith('data:'):
                                    data_str = line[5:].strip()
                                    if data_str in ["DONE", "[DONE]"]:
                                        break
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
                                                if len(accumulated_content) > MAX_SSE_ACCUMULATED_CHARS:
                                                    # 本地资源上限：不换渠道、不重新生成（F6）
                                                    raise MediaTooLargeError(
                                                        f"流式响应累积内容超过上限"
                                                        f"（{MAX_SSE_ACCUMULATED_CHARS} 字符），已中止"
                                                    )
                                    except json.JSONDecodeError:
                                        pass
                    except httpx.HTTPError as exc:
                        if submission_state_for(exc) == SUBMISSION_UNCERTAIN:
                            raise SubmissionUncertainError(
                                "创建视频任务的提交结果不确定"
                                f"（{type(exc).__name__}），服务端可能已接收任务；"
                                "为避免重复生成，不自动重试"
                            ) from exc
                        raise

                    # 流结束后：如果还没拿到 video_data，尝试从累积内容中提取
                    if not video_data and accumulated_content:
                        logger.info(f"[视频] 流式响应累积内容长度: {len(accumulated_content)}")
                        if debug_mode:
                            logger.warning("[调试模式] 已省略视频流原文，避免暴露凭据或媒体数据")
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
                    response = await _submit_request(
                        client, api_url, headers=headers, json_payload=openai_payload,
                    )
                    try:
                        data = response.json()
                    except ValueError as exc:
                        raise SubmissionUncertainError(
                            "视频接口返回了无效 JSON，无法确认生成结果；"
                            "为避免重复生成，不自动切换渠道"
                        ) from exc
                    video_data = await extract_video_data(data)
                    if not video_data and debug_mode:
                        logger.warning("[调试模式] 视频非流式响应未提取到数据，原始响应:")
                        logger.warning("[调试模式] 已省略上游原文，避免暴露凭据或媒体数据")

        # Gemini 格式
        elif protocol == "gemini":
            parts = [{"text": prompt}]
            if base64_img:
                parts.append({"inline_data": {"mime_type": mime_type, "data": base64_img}})

            gemini_payload = {"contents": [{"parts": parts}]}
            request_url = with_api_key(api_url, api_key)

            async with httpx.AsyncClient(headers={"Accept-Encoding": "identity"}, proxy=proxy, timeout=stage_timeout(300.0), follow_redirects=True, event_hooks={"response": [state.observe]}) as client:
                response = await _submit_request(
                    client, request_url,
                    headers={"Content-Type": "application/json"},
                    json_payload=gemini_payload,
                )
                try:
                    data = response.json()
                except ValueError as exc:
                    raise SubmissionUncertainError(
                        "Gemini 视频接口返回了无效 JSON，无法确认生成结果；"
                        "为避免重复生成，不自动切换渠道"
                    ) from exc
                video_data = await extract_video_data(data)

        # 内联 base64 视频同样受资源上限约束（F6）；超限不换渠道、不重新生成。
        if video_data and not video_data.startswith("url:") and len(video_data) > MAX_VIDEO_BASE64_CHARS:
            raise MediaTooLargeError(
                f"生成结果视频数据过大（{len(video_data)} 字符，上限 {MAX_VIDEO_BASE64_CHARS}），已拒绝处理"
            )

        # 如果提取到的是 URL，需要下载视频并转为 base64
        if video_data and video_data.startswith("url:"):
            video_url = video_data[4:]  # 去掉 "url:" 前缀
            logger.info("[视频] 正在下载已生成的视频")
            try:
                video_data = await _download_video_result(
                    video_url,
                    api_url=api_url,
                    api_key=api_key,
                    proxy=proxy,
                    logger=logger,
                    budget=budget,
                )
            except Exception as dl_err:
                # 成片已生成：只重试过下载，绝不换渠道重新生成。
                raise VideoResultUnavailableError(str(dl_err)) from dl_err

        return video_data

    for endpoint_index, endpoint in enumerate(endpoints):
        if budget is not None and not budget.can_start(
            reserve=POST_GENERATION_RESERVE_SECONDS, minimum=MIN_ENDPOINT_SECONDS
        ):
            logger.warning(
                f"[视频] 时间预算不足，跳过剩余 {len(endpoints) - endpoint_index} 个渠道"
            )
            if not last_error:
                last_error = BUDGET_EXHAUSTED_MESSAGE
            break

        api_url = endpoint["url"]
        api_key = endpoint["key"]
        endpoint_type = endpoint["type"]
        protocol, explicit = describe_video_protocol(endpoint)

        logger.info(
            f"[视频] 尝试端点: {endpoint_type} "
            f"[协议: {protocol or '未识别'}{'' if explicit else '(自动)'}]"
        )

        state = GenerationState()
        log_token = set_request_secrets(api_key)
        try:
            # 单个渠道的生成阶段（创建 → 轮询 → 成片下载）必须有绝对上限：
            # 持续有数据的 SSE 与轮询等待都能超过单次 HTTPX timeout。取消只
            # 终止本地等待，不能撤回上游已提交的任务，因此超时的处理与
            # "提交结果不确定"一致：不换渠道，也不把在途任务记成 Key 失效。
            video_data = await asyncio.wait_for(
                generate_with_protocol(endpoint, protocol, api_url, api_key, state),
                timeout=stage_timeout(VIDEO_CHANNEL_SAFETY_SECONDS),
            )

            if video_data:
                await _record_key_success(api_key)
                return video_data, ""
            else:
                # API 调用成功但未提取到视频数据
                error_msg = f"端点 {endpoint_type} 未返回有效视频数据"
                logger.warning(f"[视频] {error_msg}")
                last_error = error_msg
                if state.accepted:
                    return None, error_msg + UNCERTAIN_SUBMISSION_SUFFIX

        except BudgetExpiredError as e:
            logger.warning(f"[视频] 时间预算耗尽: {e}")
            last_error = str(e)
            break
        except (asyncio.TimeoutError, httpx.ReadTimeout, httpx.WriteTimeout, SubmissionUncertainError) as e:
            if state.is_unconfirmed_timeout(e):
                # 请求已发出但没等到成功响应：上游可能仍在生成，换渠道意味着可能
                # 重复生成；仍留一个渠道就不重试的做法代价更大，因此继续轮换。
                last_error = f"端点 {endpoint_type} 等待响应超时，服务端结果尚未确认"
                if endpoint_index + 1 >= len(endpoints):
                    last_error += "；没有更多可用端点"
                    break
                if budget is not None and not budget.can_start(
                    reserve=POST_GENERATION_RESERVE_SECONDS, minimum=MIN_ENDPOINT_SECONDS
                ):
                    last_error += "；剩余时间不足，未继续尝试"
                    break
                logger.warning(f"[视频] {last_error}；继续尝试下一个端点")
                await asyncio.sleep(1)
                continue
            if not isinstance(e, asyncio.TimeoutError):
                error = f"{describe_error(e)}{UNCERTAIN_SUBMISSION_SUFFIX}"
                logger.warning(f"[视频] {error}")
                return None, error
            error = (
                f"端点 {endpoint_type}: 视频生成超出单渠道时间上限，已停止等待"
                f"{UNCERTAIN_SUBMISSION_SUFFIX}"
            )
            logger.warning(f"[视频] {error}")
            return None, error
        except MediaTooLargeError as e:
            logger.warning(f"[视频] 成片过大: {e}")
            last_error = str(e)
            break
        except VideoResultUnavailableError as e:
            logger.error(f"[视频] {e}")
            return None, str(e)
        except UpstreamHTTPError as e:
            if state.prevents_retry(e):
                error = f"端点 {endpoint_type}: {e.user_message()}{UNCERTAIN_SUBMISSION_SUFFIX}"
                logger.warning(f"[视频] {error}")
                return None, error
            if e.submission_state == SUBMISSION_UNCERTAIN:
                # 与图片链路一致：HTTP 错误响应允许换端点，但不据此把 Key 记为失效。
                # 已接收任务由上面的 state 检查保护；传输超时仍走不确定状态分支。
                pass
            elif e.is_quota_exhausted:
                await _record_key_failure(api_key, force_disable=True)
            elif e.is_rate_limited:
                # 限流是可恢复冷却，不累计错误次数，避免误禁仍可用的 Key
                logger.warning(f"[视频] 端点 {endpoint_type} 被限流 (HTTP 429)，不计入 Key 错误次数")
            else:
                await _record_key_failure(api_key, force_disable=False)
            logger.warning(
                f"[视频] 端点 {endpoint_type} 失败: HTTP {e.status_code} (stage={e.stage}): {e.body[:300]}"
            )
            last_error = f"端点 {endpoint_type}: {e.user_message()}"
            await asyncio.sleep(1)
        except Exception as e:
            # 原始异常文本可能带查询串里的 key 或内网地址，日志与文案统一用脱敏结果。
            detail = describe_error(e)
            if state.prevents_retry(e) or submission_state_for(e) == SUBMISSION_UNCERTAIN:
                error = f"端点 {endpoint_type}: {detail}{UNCERTAIN_SUBMISSION_SUFFIX}"
                logger.warning(f"[视频] {error}")
                return None, error
            logger.warning(f"[视频] 端点 {endpoint_type} 失败: {type(e).__name__}: {detail}")
            await _record_key_failure(api_key, force_disable=False)
            last_error = f"端点 {endpoint_type}: {detail}"
            await asyncio.sleep(1)

        finally:
            reset_request_secrets(log_token)

    # 所有端点都失败了，记录最终错误
    if not last_error:
        last_error = "所有端点均未返回有效视频数据"
    logger.error(f"[视频] 生成失败: {last_error}")
    return None, last_error


def _stage_shared_video(video_base64: str, shared_dir: str) -> Path:
    """将视频写入双端同路径挂载的目录；仅清理本插件超过一天的遗留文件。"""
    directory = Path(shared_dir).expanduser()
    if not directory.is_absolute():
        raise ValueError("视频共享目录必须是绝对路径")
    directory.mkdir(parents=True, exist_ok=True)
    for old in directory.glob("gemini-video-*.mp4"):
        try:
            if old.is_file() and time.time() - old.stat().st_mtime > 86400:
                old.unlink()
        except OSError:
            pass  # 遗留文件清理失败不影响当前视频，不能重新生成。
    if len(video_base64) > MAX_VIDEO_BASE64_CHARS:
        raise ValueError("视频超过大小上限")
    data = base64.b64decode(video_base64.replace("\n", "").replace("\r", ""), validate=True)
    if not data or len(data) > MAX_VIDEO_BYTES:
        raise ValueError("视频数据为空或超过大小上限")
    with tempfile.NamedTemporaryFile(dir=directory, prefix="gemini-video-", suffix=".mp4", delete=False) as file:
        try:
            file.write(data)
            # SnowLuma 与宿主可能以不同容器 UID 运行，需要读取共享文件。
            os.fchmod(file.fileno(), 0o644)
        except BaseException:
            Path(file.name).unlink(missing_ok=True)
            raise
    return Path(file.name)


async def send_video_media(
    video_base64: str,
    *,
    ctx: Any,
    stream_id: str,
    group_id: Optional[str],
    user_id: Optional[str],
    napcat_host: str,
    napcat_port: int,
    logger,
    budget: Optional[TimeBudget] = None,
    shared_video_dir: str = "",
) -> Tuple[bool, str]:
    """仅通过配置的 OneBot HTTP 端口发送视频，不查询完整消息回执。

    ctx / stream_id 保留为现有调用方的兼容参数；视频不进入 SDK 发送链路。
    发送接口确认成功即结束；超时或未知响应保留投递未确认状态，绝不重发。
    """
    if budget is not None and not budget.can_start(minimum=5.0):
        logger.warning("[视频] 时间预算耗尽，不再开始 HTTP 发送")
        return False, "时间预算耗尽，未尝试发送视频"

    napcat_timeout = 300.0
    if budget is not None:
        napcat_timeout = budget.stage_timeout(300.0, minimum=5.0)

    if budget:
        budget.media_delivery_pending = True
    try:
        result = await asyncio.wait_for(send_video_via_napcat(
            video_base64=video_base64,
            group_id=group_id,
            user_id=user_id,
            napcat_host=napcat_host,
            napcat_port=napcat_port,
            logger=logger,
            timeout=napcat_timeout,
            shared_video_dir=shared_video_dir,
        ), timeout=napcat_timeout)
    except TimeoutError:
        return False, MediaDeliveryUncertain()
    if budget:
        budget.media_delivery_pending = isinstance(result[1], MediaDeliveryUncertain)
        if result[0]:
            budget.media_sent = True
    return result


async def send_video_via_napcat(
    video_base64: str,
    group_id: Optional[str],
    user_id: Optional[str],
    napcat_host: str,
    napcat_port: int,
    logger,
    timeout: float = 300.0,
    shared_video_dir: str = "",
) -> Tuple[bool, str]:
    """
    通过 SnowLuma / NapCat 的 OneBot HTTP API 发送一次视频

    Returns:
        (success, error_message)
    """
    if group_id:
        api_url = f"http://{napcat_host}:{napcat_port}/send_group_msg"
        request_data = {"group_id": group_id, "message": [{"type": "video", "data": {"file": ""}}]}
    elif user_id:
        api_url = f"http://{napcat_host}:{napcat_port}/send_private_msg"
        request_data = {"user_id": user_id, "message": [{"type": "video", "data": {"file": ""}}]}
    else:
        return False, "无法确定发送目标"

    staged_file = None
    if shared_video_dir:
        try:
            staged_file = await asyncio.to_thread(_stage_shared_video, video_base64, shared_video_dir)
        except (OSError, ValueError) as exc:
            logger.error(f"[视频] 共享文件准备失败: {type(exc).__name__}，未调用发送接口")
            return False, "视频共享文件准备失败，未尝试发送"
        request_data["message"][0]["data"]["file"] = staged_file.as_uri()
        logger.info("[视频] 使用共享文件发送，HTTP 请求仅包含文件路径")
    else:
        request_data["message"][0]["data"]["file"] = f"base64://{video_base64}"

    try:
        async with asyncio.timeout(timeout), httpx.AsyncClient(headers={"Accept-Encoding": "identity"}, timeout=timeout, trust_env=False) as client:
            response = await request_capped(client, "POST", api_url, json=request_data)
            if response.status_code != 200:
                if 400 <= response.status_code < 500 and response.status_code != 408:
                    return False, f"视频发送接口拒绝请求: HTTP {response.status_code}"
                return False, MediaDeliveryUncertain()

            result = response.json()
            if not isinstance(result, dict):
                return False, MediaDeliveryUncertain()
            status = result.get("status")
            retcode = result.get("retcode")
            data = result.get("data")
            message_id = data.get("message_id") if isinstance(data, dict) else None
            valid_message_id = (
                not isinstance(message_id, bool)
                and isinstance(message_id, (int, str))
                and str(message_id).removeprefix("-").isdigit()
                and int(message_id) != 0
            )
            # 标准成功响应必须一致，且包含消息 ID；不再额外调用 get_msg。
            if status == "ok" and type(retcode) is int and retcode == 0 and valid_message_id:
                logger.info("[视频] HTTP 发送接口已确认成功")
                if staged_file is not None:
                    try:
                        await asyncio.to_thread(staged_file.unlink, missing_ok=True)
                    except OSError:
                        logger.warning("[视频] 已发送，共享临时文件清理失败，将在后续发送时清理")
                return True, ""
            if status == "failed" and type(retcode) is int and retcode != 0:
                return False, "视频发送接口明确报告失败"
            return False, MediaDeliveryUncertain()
    except Exception as e:
        logger.error(f"[视频] 发送确认失败: {type(e).__name__}")
        return False, MediaDeliveryUncertain()
