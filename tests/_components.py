"""通过公开接口读取插件组件注册信息的测试辅助（P2-10）。

使用 ``MaiBotPlugin.get_components()`` 而不是 SDK 私有属性，快照与已安装的
maibot-plugin-sdk 都可用。
"""
from __future__ import annotations

from gemini_drawer.plugin import GeminiDrawerPlugin


def plugin_components() -> list:
    return GeminiDrawerPlugin().get_components()


def component_metadata(handler_name: str) -> dict:
    for component in plugin_components():
        metadata = component.get("metadata") or {}
        if metadata.get("handler_name") == handler_name:
            return metadata
    raise AssertionError(f"未找到组件 {handler_name}")
