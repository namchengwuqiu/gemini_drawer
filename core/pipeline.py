"""
Gemini Drawer 绘图管线

整个插件唯一的"遍历端点 → 发请求 → 取图 → 记账 → 失败换下一个"循环。
命令（/绘图 /bnn /+ /随机 /多图）与 Action（自然语言绘图、自拍）共用此入口。

协议差异全部下沉到 providers/，本模块只负责编排与容错。
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

import httpx

from .managers import key_manager
from ..providers import DrawRequest, Endpoint, describe_protocol, resolve_provider
from .protocols import normalize_protocol
from ..utils import redact_url, safe_json_dumps

#: lmarena 是无 Key 的中转，不参与 Key 健康度记账
KEYLESS_ENDPOINT_TYPES = {"lmarena"}
#: 端点之间的退避间隔
RETRY_DELAY = 1.0


def _record(endpoint: Endpoint, success: bool, force_disable: bool = False) -> None:
    if endpoint.type in KEYLESS_ENDPOINT_TYPES:
        return
    key_manager.record_key_usage(endpoint.key, success, force_disable=force_disable)


async def run_drawing(
    request: DrawRequest,
    endpoints: Iterable[Union[Endpoint, Dict[str, Any]]],
    proxy: Optional[str],
    logger: Any,
    debug_mode: bool = False,
) -> Tuple[List[str], str]:
    """依次尝试各端点直到成功。

    Returns:
        (图片数据列表, 最终错误信息)。成功时错误信息为空串；
        失败时列表为空，错误信息为最后一个端点的失败原因。
    """
    endpoint_list = [
        ep if isinstance(ep, Endpoint) else Endpoint.from_dict(ep) for ep in endpoints
    ]
    last_error = ""

    for i, endpoint in enumerate(endpoint_list):
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

        try:
            call = provider.build(endpoint, request)
            payload = call.json if call.json is not None else call.data
            logger.info(
                f"准备向 {endpoint.type} 端点发送请求。URL: {call.safe_url}, "
                f"Payload: {safe_json_dumps(payload)}"
            )

            async with httpx.AsyncClient(
                proxy=proxy if call.use_proxy else None,
                timeout=call.timeout,
                follow_redirects=True,
            ) as client:
                img_data = await provider.fetch(call, client, logger, debug_mode)

            if not img_data:
                raise RuntimeError("API未返回图片，响应中没有可提取的图片数据")

            _record(endpoint, True)
            logger.info(f"使用 {endpoint.type} 端点成功生成 {len(img_data)} 张图片")
            return img_data, ""

        except Exception as e:
            logger.warning(f"端点 {endpoint.type} 尝试失败: {type(e).__name__}: {e}")
            _record(endpoint, False, force_disable="429" in str(e))
            last_error = str(e)
            await asyncio.sleep(RETRY_DELAY)

    if not last_error:
        last_error = "没有可用的绘图端点（所有渠道均无法识别）"
    return [], last_error
