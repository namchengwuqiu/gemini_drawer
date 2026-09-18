"""插件自身版本号。

版本号只在插件根目录的 ``_manifest.json`` 里维护一处。配置里的
``plugin.version``（``config.py`` 的 PluginSectionConfig）是宿主持久化的副本，
发版时容易忘记同步 —— 历史上它长期落后于实际版本，导致启动日志报的版本号
与实际加载的插件对不上，所以启动日志改为直接读清单文件。
"""
from __future__ import annotations

import json
from pathlib import Path

#: 清单文件固定在插件包根目录（与 plugin.py 同级）
_MANIFEST_PATH = Path(__file__).resolve().parent.parent / "_manifest.json"


def get_plugin_version(fallback: str = "") -> str:
    """读取 ``_manifest.json`` 的 ``version``。

    Args:
        fallback: 清单缺失、损坏或没有 version 字段时的兜底值。

    Returns:
        版本号字符串；无清单且无 fallback 时返回 ``"unknown"``。
    """
    try:
        data = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
        version = str(data.get("version") or "").strip()
    except Exception:
        version = ""
    return version or fallback or "unknown"
