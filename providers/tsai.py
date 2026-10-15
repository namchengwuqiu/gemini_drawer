"""TS-AI 图片生成：建任务 + 轮询任务状态的异步 API。"""
from __future__ import annotations

import asyncio
from typing import Any, List

import httpx

from ..core.errors import SubmissionUncertainError, UpstreamHTTPError, GenerationRejectedError
from ..core.host_bridge import get_plugin_logger

from ..utils import extract_http_error_reason, sanitize_upstream_text

from ..utils.http import request_capped

from .base import DrawRequest, Endpoint, HttpCall, Provider

logger = get_plugin_logger("plugin.gemini_drawer.providers.tsai")

DEFAULT_WORKFLOW = "rr3"
POLL_ATTEMPTS = 100
POLL_INTERVAL = 3.0

_URL_MARKERS = ("tavr.top", "tsart.lat", "endpoint=image")


class TsAiProvider(Provider):
    protocol = "tsai"
    name = "tsai"
    supports_stream = False
    #: 单次 HTTP 请求的超时（创建或一次轮询查询）
    timeout = 60.0
    #: 建任务 + 轮询的整个生成阶段上限；调用方用它做绝对截止控制（F8）
    task_timeout = 300.0

    @classmethod
    def matches(cls, endpoint: Endpoint) -> bool:
        if endpoint.type.startswith("custom_tsart"):
            return True
        url = endpoint.url.lower()
        return any(marker in url for marker in _URL_MARKERS)

    def build(self, endpoint: Endpoint, request: DrawRequest) -> HttpCall:
        base_url = endpoint.url.split("?")[0]
        workflow = endpoint.model_name or DEFAULT_WORKFLOW
        payload = {"prompt": request.prompt, "workflow": workflow, "seed": -1}

        if request.has_image:
            if len(request.images) > 1:
                logger.info(
                    f"TS-AI 多图暂仅使用第 1 张参考图，其余 {len(request.images) - 1} 张将被忽略。"
                )
            payload["image"] = request.data_url(0)
            action = "image_editing"
        else:
            action = "image_generation"

        return HttpCall(
            url=f"{base_url}?endpoint={action}",
            headers={
                "Content-Type": "application/json",
                "x-api-key": endpoint.key,
            },
            json=payload,
            timeout=self.timeout,
        )

    async def fetch(
        self,
        call: HttpCall,
        client: httpx.AsyncClient,
        logger: Any,
        debug_mode: bool = False,
    ) -> List[str]:
        response = await request_capped(client, "POST", call.url, json=call.json, headers=call.headers)
        if response.status_code != 200:
            raise UpstreamHTTPError(
                response.status_code,
                stage="submit",
                body=response.text,
                reason=extract_http_error_reason(response),
            )

        try:
            resp_json = response.json()
        except ValueError as exc:
            raise SubmissionUncertainError(
                "TS-AI 提交后返回了无效 JSON，无法确认任务状态；"
                "为避免重复生成，不自动切换端点"
            ) from exc
        created = resp_json.get("data") if isinstance(resp_json, dict) else None
        task_id = created.get("id") if isinstance(created, dict) else None
        if not task_id:
            # 生成请求已被接受（HTTP 200）却拿不到任务 ID：无法恢复同一任务，
            # 也不能证明请求未被处理，按"提交结果不确定"处理（F7）。
            raise SubmissionUncertainError(
                "未能获取TS-AI任务ID；"
                "无法确认任务是否已创建，为避免重复生成，不自动切换端点"
            )

        base_url = call.url.split("?")[0]
        poll_url = f"{base_url}?endpoint=task_status&task_id={task_id}"
        logger.info("[TS-AI] 任务已创建，开始查询同一任务")

        missing_result_logged = False
        for _ in range(POLL_ATTEMPTS):
            await asyncio.sleep(POLL_INTERVAL)
            try:
                poll_resp = await request_capped(client, "GET", poll_url, headers=call.headers)
            except httpx.HTTPError as exc:
                # 任务已创建：查询阶段的网络错误只重试同一任务，绝不重新提交
                logger.warning(f"[TS-AI] 查询暂时失败: {type(exc).__name__}，继续查询同一任务")
                continue
            if poll_resp.status_code != 200:
                continue
            try:
                poll_json = poll_resp.json()
            except ValueError:
                continue

            data = poll_json.get("data") if isinstance(poll_json, dict) else None
            if not isinstance(data, dict):
                continue
            status = data.get("status")
            if status == "completed":
                result = data.get("result")
                image_url = result.get("image_url") if isinstance(result, dict) else None
                if not isinstance(image_url, str) or not image_url:
                    # 任务已完成但结果地址暂不可用：继续查询同一任务，不换端点（F7）
                    if not missing_result_logged:
                        missing_result_logged = True
                        logger.warning("[TS-AI] 任务已完成但未返回图片地址，继续查询同一任务")
                    continue
                return [image_url]
            if status == "failed":
                raise GenerationRejectedError("TS-AI任务明确报告生成失败")

        raise SubmissionUncertainError(
            "TS-AI任务轮询超时，任务可能仍在服务端运行；为避免重复生成，不自动切换端点"
        )
