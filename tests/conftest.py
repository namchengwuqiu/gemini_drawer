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
