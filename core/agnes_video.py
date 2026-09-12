"""Agnes Videos API：创建一次任务，使用 video_id 轮询并返回成片 URL。

参考图直接使用 Data URI Base64 作为 keyframe 首帧。下载与发送继续由
core.video 负责；这里不上传图片，也不在轮询失败时重新创建任务。
"""
from __future__ import annotations

import asyncio
import math
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx

from ..utils import extract_text_failure_reason

DEFAULT_MODEL = "agnes-video-2.5-flash"
# 宿主视频组件上限为 600 秒；生成阶段最多 420 秒，为下载和发送留出时间。
GENERATION_TIMEOUT = 420.0
POLL_INTERVAL = 5.0
MAX_RETRY_DELAY = 30.0


class AgnesVideoPendingError(RuntimeError):
    """服务端可能仍有任务在运行，调用方不应切换 Key/渠道重复提交。"""


def _model_name(endpoint: Dict[str, Any]) -> str:
    return str(endpoint.get("model") or "").strip() or DEFAULT_MODEL


def matches_agnes_video_endpoint(endpoint: Dict[str, Any]) -> bool:
    try:
        url = urlsplit(endpoint.get("url") or "")
    except ValueError:
        return False
    if url.scheme not in ("https", "http") or not url.netloc:
        return False
    model = str(endpoint.get("model") or "").strip().lower()
    return url.path.rstrip("/").endswith("/v1/videos") and (
        model.startswith("agnes-video-")
        or (not model and url.hostname == "apihub.agnes-ai.com")
    )


def build_agnes_video_payload(
    endpoint: Dict[str, Any],
    prompt: str,
    base64_img: Optional[str],
    mime_type: Optional[str],
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "model": _model_name(endpoint),
        "prompt": prompt,
        "mode": "keyframe" if base64_img else "text",
        "seconds": "5",
        "size": "720P",
        "aspect_ratio": "16:9",
    }
    if base64_img:
        # 常规调用传裸 Base64；同时兼容已带 Data URI 前缀的输入。
        payload["first_frame"] = (
            base64_img if base64_img.startswith("data:image/")
            else f"data:{mime_type or 'image/png'};base64,{base64_img}"
        )
    return payload


def _query_url(api_url: str) -> str:
    url = urlsplit(api_url)
    path = url.path.rstrip("/")
    if not path.endswith("/v1/videos"):
        raise ValueError("Agnes 视频 API 地址必须以 /v1/videos 结尾")
    # 保留反向代理的路径前缀；/v1/videos 查询的是根路径 /agnesapi。
    path = path[:-len("/v1/videos")] + "/agnesapi"
    return urlunsplit((url.scheme, url.netloc, path, "", ""))


def _raise_for_api_error(response: httpx.Response, action: str) -> None:
    if response.is_success:
        return
    reason = response.reason_phrase
    try:
        body = response.json()
        if isinstance(body, dict):
            reason = extract_text_failure_reason(body) or reason
    except ValueError:
        pass
    # 只提取可读错误，不回显包含参考图 Base64 的整个响应/请求体。
    raise RuntimeError(f"Agnes {action}失败 (HTTP {response.status_code}): {reason}")


def _json_object(response: httpx.Response) -> Dict[str, Any]:
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError("Agnes 视频接口返回了无效 JSON") from exc
    if not isinstance(data, dict):
        raise RuntimeError("Agnes 视频接口响应必须是 JSON 对象")
    return data


def _task_result(data: Dict[str, Any]) -> Optional[str]:
    status = data.get("status")
    if status == "completed":
        metadata = data.get("metadata")
        # 文档使用 metadata.url；线上 Videos API 实际还会返回顶层 url。
        candidates = [metadata.get("url") if isinstance(metadata, dict) else None, data.get("url")]
        invalid_url = False
        for value in candidates:
            if not isinstance(value, str) or not value.strip():
                continue
            url = value.strip()
            try:
                parsed = urlsplit(url)
                if parsed.scheme in ("https", "http") and parsed.netloc:
                    return url
            except ValueError:
                pass
            invalid_url = True
        if invalid_url:
            # 服务端已报告完成，不能再切渠道生成一份来掩盖结果地址错误。
            raise AgnesVideoPendingError("Agnes 视频地址不是有效的 HTTP(S) 地址（metadata.url/url）")
        # completed 与结果地址发布可能不同步；让调用方继续查询同一任务。
        return None
    if status == "failed":
        reason = extract_text_failure_reason(data) or "未知错误"
        raise RuntimeError(f"Agnes 视频生成失败: {reason}")
    if status not in ("queued", "in_progress"):
        raise RuntimeError(f"Agnes 返回了未知视频任务状态: {status!r}")
    return None


def _retry_delay(response: httpx.Response, previous: float) -> float:
    delay = min(previous * 2, MAX_RETRY_DELAY)
    value = response.headers.get("Retry-After", "")
    try:
        retry_after = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            retry_after = (retry_at - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return delay
    if math.isfinite(retry_after) and retry_after > 0:
        # Retry-After 是下限，不能因较小值缩短退避，也不能被 30 秒上限截短。
        # 服务器要求等待太久时，外层的生成总超时仍会终止本地等待。
        return max(delay, retry_after)
    return delay


async def generate_agnes_video(
    endpoint: Dict[str, Any],
    prompt: str,
    base64_img: Optional[str],
    mime_type: Optional[str],
    proxy: Optional[str],
    logger: Any,
) -> str:
    api_url = endpoint["url"]
    poll_url = _query_url(api_url)
    payload = build_agnes_video_payload(endpoint, prompt, base64_img, mime_type)
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {endpoint.get('key') or ''}",
    }
    video_id: Optional[str] = None
    waiting_for_url = False

    def note_missing_result_url(data: Dict[str, Any]) -> None:
        nonlocal waiting_for_url
        if data.get("status") != "completed" or waiting_for_url:
            return
        waiting_for_url = True
        # 只打印字段名用于定位协议变化，不打印提示词、参考图、URL 或密钥。
        fields = ", ".join(sorted(data))
        metadata = data.get("metadata")
        metadata_fields = ", ".join(sorted(metadata)) if isinstance(metadata, dict) else "无"
        logger.warning(
            f"[Agnes 视频] 任务已完成但视频地址尚不可用，继续查询同一任务；"
            f"响应字段: {fields}；metadata 字段: {metadata_fields}"
        )

    async with httpx.AsyncClient(
        proxy=proxy, timeout=60.0, follow_redirects=True,
    ) as client:
        async def submit_and_poll() -> str:
            nonlocal video_id
            # 创建请求不自动重试，避免网络超时时重复提交/计费。
            try:
                response = await client.post(api_url, json=payload, headers=headers)
            except (
                httpx.ReadTimeout, httpx.WriteTimeout,
                httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError,
            ) as exc:
                raise AgnesVideoPendingError(
                    "Agnes 视频任务提交结果不确定，服务端可能已接收任务；为避免重复生成，不自动重试"
                ) from exc
            _raise_for_api_error(response, "创建视频任务")
            data = _json_object(response)
            result = _task_result(data)
            if result:
                return result
            video_id = data.get("video_id")
            if not isinstance(video_id, str) or not video_id.strip():
                raise RuntimeError("Agnes 创建任务未返回 video_id，不能使用 id/task_id 代替查询")
            video_id = video_id.strip()
            logger.info(f"[Agnes 视频] 任务已创建: {video_id}")
            note_missing_result_url(data)
            params = {"video_id": video_id, "model_name": payload["model"]}
            delay = POLL_INTERVAL

            while True:
                await asyncio.sleep(delay)
                try:
                    response = await client.get(
                        poll_url, params=params, headers=headers, timeout=30.0,
                    )
                except httpx.RequestError as exc:
                    delay = min(delay * 2, MAX_RETRY_DELAY)
                    logger.warning(
                        f"[Agnes 视频] 查询暂时失败: {type(exc).__name__}，{delay:g} 秒后重试同一任务"
                    )
                    continue
                if response.status_code in (408, 429) or 500 <= response.status_code < 600:
                    delay = _retry_delay(response, delay)
                    logger.warning(f"[Agnes 视频] 查询返回 HTTP {response.status_code}，{delay:g} 秒后重试同一任务")
                    continue
                _raise_for_api_error(response, "查询视频任务")
                data = _json_object(response)
                result = _task_result(data)
                if result:
                    return result
                note_missing_result_url(data)
                # 成功查询后保留已经退避的间隔，避免恢复高频轮询后再次触发 429。

        try:
            return await asyncio.wait_for(submit_and_poll(), timeout=GENERATION_TIMEOUT)
        except asyncio.TimeoutError as exc:
            detail = f" (video_id={video_id})" if video_id else ""
            if waiting_for_url:
                raise AgnesVideoPendingError(
                    f"Agnes 服务端已报告完成，但一直未返回可用视频地址（metadata.url/url）{detail}"
                ) from exc
            raise AgnesVideoPendingError(
                f"Agnes 视频生成/轮询超时{detail}；已提交的任务可能仍在服务端运行"
            ) from exc
