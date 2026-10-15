"""测试用的 SDK 装载辅助。

优先使用已安装的 maibot-plugin-sdk（pip install maibot-plugin-sdk）；
未安装时回退到仓库同级目录的 maibot_sdk_tmp 快照（本地开发）。

两者都没有时直接抛 ImportError，而不是统一 skip——核心路径的测试不允许
在 CI 里静默变绿（P2-10）。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

#: 本地快照目录（插件仓库同级；快照来源与版本见 README「测试」一节）
SNAPSHOT_DIR = Path(__file__).resolve().parents[4] / "maibot_sdk_tmp"


def ensure_sdk() -> None:
    """确保 ``maibot_sdk`` 可导入；已安装时直接返回。"""
    if "maibot_sdk" in sys.modules:
        return
    try:
        import maibot_sdk  # noqa: F401
        return
    except ImportError:
        pass

    init_file = SNAPSHOT_DIR / "__init__.py"
    if not init_file.is_file():
        raise ImportError(
            "未找到 maibot_sdk：请安装 maibot-plugin-sdk（pip install maibot-plugin-sdk），"
            f"或把 SDK 快照放到 {SNAPSHOT_DIR}"
        )
    spec = importlib.util.spec_from_file_location(
        "maibot_sdk", init_file, submodule_search_locations=[str(SNAPSHOT_DIR)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["maibot_sdk"] = module
    spec.loader.exec_module(module)
