"""TS-AI 图片生成：建任务 + 轮询任务状态的异步 API。"""
from __future__ import annotations

import asyncio
from typing import Any, List

import httpx

from ..core.host_bridge import get_plugin_logger

from .base import DrawRequest, Endpoint, HttpCall, Provider

logger = get_plugin_logger("plugin.gemini_drawer.providers.tsai")

DEFAULT_WORKFLOW = "rr3"
POLL_ATTEMPTS = 100
POLL_INTERVAL = 3.0

_URL_MARKERS = ("tavr.top", "tsart.lat", "endpoint=image")


class TsAiProvider(Provider):
    name = "tsai"
    supports_stream = False
    timeout = 60.0

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
        response = await client.post(call.url, json=call.json, headers=call.headers)
        if response.status_code != 200:
            raise RuntimeError(f"创建任务失败: {response.status_code} - {response.text}")

        resp_json = response.json()
        task_id = (resp_json.get("data") or {}).get("id")
        if not task_id:
            raise RuntimeError(f"未能获取TS-AI任务ID: {resp_json}")

        base_url = call.url.split("?")[0]
        poll_url = f"{base_url}?endpoint=task_status&task_id={task_id}"
        logger.info(f"[TS-AI] 任务已创建: {task_id}")

        for _ in range(POLL_ATTEMPTS):
            await asyncio.sleep(POLL_INTERVAL)
            poll_resp = await client.get(poll_url, headers=call.headers)
            if poll_resp.status_code != 200:
                continue

            data = (poll_resp.json().get("data") or {})
            status = data.get("status")
            if status == "completed":
                image_url = (data.get("result") or {}).get("image_url")
                if not image_url:
                    raise RuntimeError(f"TS-AI任务完成但未返回图片地址: {data}")
                return [image_url]
            if status == "failed":
                raise RuntimeError(f"TS-AI生成失败: {data.get('error', '未知错误')}")

        raise RuntimeError("TS-AI任务轮询超时")
