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

from typing import Any, Dict, List, Optional, Tuple

from .managers import data_manager, key_manager


def _channel_fields(channel_info: Any) -> Tuple[str, str, Optional[str], bool, bool, bool]:
    """把渠道条目normalize成 (url, key, model, enabled, is_video, stream)。

    早期版本允许渠道值是 "URL:KEY" 这样的字符串，这里一并兼容。
    """
    if isinstance(channel_info, dict):
        return (
            channel_info.get("url") or "",
            channel_info.get("key") or "",
            channel_info.get("model"),
            bool(channel_info.get("enabled", True)),
            bool(channel_info.get("is_video", False)),
            bool(channel_info.get("stream", False)),
        )

    if isinstance(channel_info, str) and ":" in channel_info:
        url, key = channel_info.rsplit(":", 1)
        return url, key, None, True, False, False

    return "", "", None, True, False, False


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


def _make_endpoint(name: str, url: str, key: str, model: Optional[str], stream: bool) -> Dict[str, Any]:
    return {"type": f"custom_{name}", "url": url, "key": key, "model": model, "stream": stream}


def build_drawing_endpoints() -> List[Dict[str, Any]]:
    """构建绘图端点列表（跳过标记为视频的渠道）。

    顺序即失败转移顺序，与重构前保持一致：
    先是所有渠道里内联的 Key（按渠道顺序），再是 KeyManager 里的 Key（按 keys.json 顺序）。
    """
    channels = data_manager.get_channels()
    endpoints: List[Dict[str, Any]] = []

    # 1. 渠道条目里内联的 Key（兼容旧数据格式）
    for name, channel_info in channels.items():
        url, key, model, enabled, is_video, stream = _channel_fields(channel_info)
        if is_video or not enabled or not url or not key:
            continue
        endpoints.append(_make_endpoint(name, url, key, model, stream))

    # 2. KeyManager 管理的 Key，按其所属渠道取 URL / 模型
    for key_info in key_manager.get_all_keys():
        if key_info.get("status") != "active":
            continue
        channel_name = _resolve_key_channel(key_info)
        if channel_name not in channels:
            continue

        url, _, model, enabled, is_video, stream = _channel_fields(channels[channel_name])
        if is_video or not enabled or not url:
            continue
        endpoints.append(_make_endpoint(channel_name, url, key_info["value"], model, stream))

    return endpoints


def build_video_endpoints(logger=None) -> List[Dict[str, Any]]:
    """构建视频生成端点列表（只取标记为视频的渠道）。"""
    endpoints: List[Dict[str, Any]] = []

    for name, channel_info in data_manager.get_channels().items():
        if not isinstance(channel_info, dict):
            continue
        url, key, model, enabled, is_video, stream = _channel_fields(channel_info)
        if not is_video or not enabled or not url:
            continue

        if key:
            endpoints.append(_make_endpoint(name, url, key, model, stream))

        managed_keys = _active_keys_for(name)
        for managed_key in managed_keys:
            endpoints.append(_make_endpoint(name, url, managed_key, model, stream))

        if not key and not managed_keys and logger:
            logger.warning(
                f"[视频] 渠道 '{name}' 已启用但未找到有效Key (检查了 key_manager 和 data.json)"
            )

    return endpoints


async def get_video_endpoints(config_getter=None, logger=None) -> List[Dict[str, Any]]:
    """异步包装，保留给现有调用方；config_getter 已不再使用。"""
    return build_video_endpoints(logger=logger)
