import sys
from pathlib import Path

# 让测试能以 `gemini_drawer.xxx` 形式导入插件（插件目录是 namespace package）
PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _sdk import ensure_sdk  # noqa: E402

# 在任何测试模块导入插件代码之前装载 SDK：优先已安装的 maibot-plugin-sdk
# （CI / 容器），其次仓库同级 maibot_sdk_tmp 快照；两者都没有时直接报错，
# 不用统一 skip 掩盖核心路径（P2-10）。
ensure_sdk()


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_manager_storage(tmp_path):
    """模拟宿主注入数据目录，测试不能依赖或读写真实部署的公共数据。"""
    from gemini_drawer.core.managers import data_manager, key_manager
    from gemini_drawer.core.storage import initialize_storage

    initialize_storage(
        tmp_path / "_host" / "data" / "plugins" / "drawer",
        tmp_path / "_host" / "plugins" / "drawer", key_manager, data_manager,
    )
