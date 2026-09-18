"""版本号测试。

版本号只在 _manifest.json 维护一处，启动日志必须读它而不是配置里的副本 ——
历史上 config.toml 的 plugin.version 长期落后于实际版本（v1.11.2 时代日志还
在报 v1.10.3），所以这里把「清单可用」「与配置默认值一致」「清单坏了要兜底」
三件事都固定下来。
"""
import ast
import json
import pathlib

import pytest

from gemini_drawer.utils.version import _MANIFEST_PATH, get_plugin_version

PLUGIN_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _config_defaults():
    """用 AST 取 config.py 里 PluginSectionConfig 的 version/config_version 默认值。

    本地测试环境装不上 maibot_sdk，不能直接 import config.py，所以这里读源码。
    """
    tree = ast.parse((PLUGIN_ROOT / "config.py").read_text(encoding="utf-8"))
    found = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != "PluginSectionConfig":
            continue
        for stmt in node.body:
            if not isinstance(stmt, ast.AnnAssign) or not isinstance(stmt.target, ast.Name):
                continue
            if stmt.target.id not in ("version", "config_version"):
                continue
            call = stmt.value
            for kw in getattr(call, "keywords", []):
                if kw.arg == "default":
                    found[stmt.target.id] = ast.literal_eval(kw.value)
    return found


def test_manifest_file_lives_next_to_plugin_py():
    assert _MANIFEST_PATH == PLUGIN_ROOT / "_manifest.json"
    assert (PLUGIN_ROOT / "plugin.py").is_file()


def test_get_plugin_version_reads_manifest():
    manifest = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    assert get_plugin_version() == manifest["version"]


def test_manifest_version_matches_config_default():
    """两处版本号必须一起改，避免 WebUI 显示与实际加载版本不一致。"""
    defaults = _config_defaults()
    assert set(defaults) == {"version", "config_version"}, defaults
    assert get_plugin_version() == defaults["version"]
    assert get_plugin_version() == defaults["config_version"]


def test_readme_version_matches_manifest():
    readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
    assert f"> **Version:** {get_plugin_version()}" in readme


def test_missing_manifest_falls_back(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "gemini_drawer.utils.version._MANIFEST_PATH", tmp_path / "nope.json"
    )
    assert get_plugin_version("1.2.3") == "1.2.3"
    assert get_plugin_version() == "unknown"


def test_corrupt_manifest_falls_back(monkeypatch, tmp_path):
    bad = tmp_path / "_manifest.json"
    bad.write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr("gemini_drawer.utils.version._MANIFEST_PATH", bad)
    assert get_plugin_version("9.9.9") == "9.9.9"


def test_manifest_without_version_falls_back(monkeypatch, tmp_path):
    empty = tmp_path / "_manifest.json"
    empty.write_text(json.dumps({"name": "x"}), encoding="utf-8")
    monkeypatch.setattr("gemini_drawer.utils.version._MANIFEST_PATH", empty)
    assert get_plugin_version("9.9.9") == "9.9.9"


@pytest.mark.parametrize("value,expected", [("  1.2.3  ", "1.2.3"), ("", "fallback")])
def test_version_is_trimmed(monkeypatch, tmp_path, value, expected):
    f = tmp_path / "_manifest.json"
    f.write_text(json.dumps({"version": value}), encoding="utf-8")
    monkeypatch.setattr("gemini_drawer.utils.version._MANIFEST_PATH", f)
    assert get_plugin_version("fallback") == expected
