"""TOML 原子保存与损坏保护的回归测试（P1-2）。"""
import toml
import pytest

from gemini_drawer.utils.config_file import fix_broken_toml_config, save_config_file


def test_save_config_file_writes_content_atomically(tmp_path):
    path = tmp_path / "config.toml"
    save_config_file(path, {"plugin": {"version": "1.15.0"}, "提示词": "x"})

    assert toml.load(path)["plugin"]["version"] == "1.15.0"
    # 中文 Key 必须带引号，且不留下临时文件
    assert '"提示词" =' in path.read_text(encoding="utf-8")
    assert list(tmp_path.glob("*.tmp")) == []


def test_save_failure_propagates_and_keeps_old_file(tmp_path, monkeypatch):
    """写入失败必须抛给调用者，且不破坏旧文件（P1-2）。"""
    path = tmp_path / "config.toml"
    save_config_file(path, {"plugin": {"version": "1.0.0"}})
    before = path.read_bytes()

    import gemini_drawer.utils.config_file as config_file

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(config_file.tempfile, "mkstemp", boom)

    with pytest.raises(OSError):
        save_config_file(path, {"plugin": {"version": "2.0.0"}})

    assert path.read_bytes() == before
    assert list(tmp_path.glob("*.tmp")) == []


def test_fix_broken_toml_config_quotes_chinese_keys(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("提示词 = \"x\"\n[plugin]\nenabled = true\n", encoding="utf-8")

    fix_broken_toml_config(path)

    data = toml.load(path)
    assert data["提示词"] == "x"
    assert data["plugin"]["enabled"] is True
    assert list(tmp_path.glob("*.tmp")) == []
