"""
Gemini Drawer 端点构建

把"渠道配置（data.json 的 channels）"与"渠道 Key（keys.json）"组合成
一批可依次尝试的端点。绘图与视频各有一份构建规则，区别只在于取
is_video=False 还是 is_video=True 的渠道。

Key 有两个来源，都要兼容：
1. 渠道条目里直接写的 key（早期版本的数据格式）
2. KeyManager 管理的、按渠道名归类的 Key（当前推荐用法，支持多 Key 轮换与健康度统计）
"""
from __future__ import annotations

from typing import Any, Dict, List, NamedTuple, Optional

from .managers import data_manager, key_manager
from .protocols import normalize_protocol
from ..providers.base import Endpoint


class ChannelFields(NamedTuple):
    """data.json 里一个渠道条目归一化后的字段。"""
    url: str
    key: str
    model: Optional[str]
    enabled: bool
    is_video: bool
    stream: bool
    response_format: Optional[str]
    protocol: Optional[str]
    timeout: Optional[float] = None


def _optional_str(value: Any) -> Optional[str]:
    return (str(value).strip() or None) if value else None


def _channel_fields(channel_info: Any) -> ChannelFields:
    """把渠道条目 normalize 成 ChannelFields。

    早期版本允许渠道值是 "URL:KEY" 这样的字符串，这里一并兼容。
    protocol 在这里就归一成规范 id，非法值当作未设置（回退自动识别）。
    """
    if isinstance(channel_info, dict):
        return ChannelFields(
            url=channel_info.get("url") or "",
            key=channel_info.get("key") or "",
            model=channel_info.get("model"),
            enabled=bool(channel_info.get("enabled", True)),
            is_video=bool(channel_info.get("is_video", False)),
            stream=bool(channel_info.get("stream", False)),
            response_format=_optional_str(channel_info.get("response_format")),
            protocol=normalize_protocol(channel_info.get("protocol")),
            timeout=Endpoint.normalize_timeout(channel_info.get("timeout")),
        )

    if isinstance(channel_info, str) and ":" in channel_info:
        url, key = channel_info.rsplit(":", 1)
        return ChannelFields(url, key, None, True, False, False, None, None)

    return ChannelFields("", "", None, True, False, False, None, None)


def _resolve_key_channel(key_info: Dict[str, Any]) -> str:
    """Key 归属的渠道名。早期数据没有 type，按 Key 前缀猜测。"""
    key_type = key_info.get("type")
    if key_type:
        return key_type
    return "bailili" if str(key_info.get("value", "")).startswith("sk-") else "google"


def _active_keys_for(channel_name: str) -> List[str]:
    """KeyManager 中归属于该渠道且状态正常的 Key。"""
    return [
        key_info["value"]
        for key_info in key_manager.get_all_keys()
        if key_info.get("status") == "active" and _resolve_key_channel(key_info) == channel_name
    ]


def _make_endpoint(name: str, key: str, fields: ChannelFields) -> Dict[str, Any]:
    endpoint: Dict[str, Any] = {
        "type": f"custom_{name}", "url": fields.url, "key": key,
        "model": fields.model, "stream": fields.stream,
    }
    # 可选字段只在渠道显式配置时带上，未配置时保持旧的 dict 形态
    if fields.response_format:
        endpoint["response_format"] = fields.response_format
    if fields.protocol:
        endpoint["protocol"] = fields.protocol
    if fields.timeout is not None:
        endpoint["timeout"] = fields.timeout
    return endpoint


def _order_endpoints(
    endpoints: List[Dict[str, Any]], channels: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """显式顺序优先；稳定排序保留同值、未设置渠道及其 Key 的原顺序。"""
    def order(endpoint):
        channel = channels.get(endpoint["type"][len("custom_"):])
        value = channel.get("request_order") if isinstance(channel, dict) else None
        if type(value) is int and 1 <= value <= 2147483647:
            return (0, value)
        return (1, 0)

    return sorted(endpoints, key=order)


def build_drawing_endpoints() -> List[Dict[str, Any]]:
    """构建绘图端点列表（跳过标记为视频的渠道）。

    未设置请求顺序时，失败转移顺序与重构前保持一致：
    先是所有渠道里内联的 Key（按渠道顺序），再是 KeyManager 里的 Key（按 keys.json 顺序）。
    """
    channels = data_manager.get_channels()
    endpoints: List[Dict[str, Any]] = []

    # 1. 渠道条目里内联的 Key（兼容旧数据格式）
    for name, channel_info in channels.items():
        fields = _channel_fields(channel_info)
        if fields.is_video or not fields.enabled or not fields.url or not fields.key:
            continue
        endpoints.append(_make_endpoint(name, fields.key, fields))

    # 2. KeyManager 管理的 Key，按其所属渠道取 URL / 模型
    for key_info in key_manager.get_all_keys():
        if key_info.get("status") != "active":
            continue
        channel_name = _resolve_key_channel(key_info)
        if channel_name not in channels:
            continue

        fields = _channel_fields(channels[channel_name])
        if fields.is_video or not fields.enabled or not fields.url:
            continue
        endpoints.append(_make_endpoint(channel_name, key_info["value"], fields))

    return _order_endpoints(endpoints, channels)


def build_video_endpoints(logger=None) -> List[Dict[str, Any]]:
    """构建视频生成端点列表（只取标记为视频的渠道）。"""
    endpoints: List[Dict[str, Any]] = []

    channels = data_manager.get_channels()
    for name, channel_info in channels.items():
        if not isinstance(channel_info, dict):
            continue
        fields = _channel_fields(channel_info)
        if not fields.is_video or not fields.enabled or not fields.url:
            continue

        if fields.key:
            endpoints.append(_make_endpoint(name, fields.key, fields))

        managed_keys = _active_keys_for(name)
        for managed_key in managed_keys:
            endpoints.append(_make_endpoint(name, managed_key, fields))

        if not fields.key and not managed_keys and logger:
            logger.warning(
                f"[视频] 渠道 '{name}' 已启用但未找到有效Key (检查了 key_manager 和 data.json)"
            )

    return _order_endpoints(endpoints, channels)
