import sys
from pathlib import Path

# 让测试能以 `gemini_drawer.xxx` 形式导入插件（插件目录是 namespace package）
PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR.parent))
