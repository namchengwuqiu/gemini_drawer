"""旧配置升级真实归一化流程、落盘失败保护与多实例数据隔离。"""

import json
from copy import deepcopy

import pytest
import toml

from gemini_drawer import plugin as plugin_module
from gemini_drawer.core.managers import DataManager, KeyManager
from gemini_drawer.core.storage import initialize_storage


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def upgrade(tmp_path, monkeypatch):
    plugin_dir = tmp_path / "plugins" / "gemini_drawer"
    plugin_dir.mkdir(parents=True)
    target = tmp_path / "data" / "plugins" / "namchengwuqiu.gemini_drawer"
    keys, data = KeyManager(), DataManager()
    monkeypatch.setattr(plugin_module, "key_manager", keys)
    monkeypatch.setattr(plugin_module, "data_manager", data)
    # 保留真实 SDK、配置模型和插件归一化入口，只将文件路径定向到临时目录。
    monkeypatch.setattr(plugin_module, "__file__", str(plugin_dir / "plugin.py"))
    plugin = plugin_module.GeminiDrawerPlugin()
    plugin._storage_root = target
    return plugin, keys, data, plugin_dir, target


def legacy_config():
    return {
        "plugin": {"config_version": "1.10.0", "enabled": False},
        "channels": {"custom": {"url": "https://custom.example/v1", "key": "sk-test-only", "enabled": False},
                     "string": "https://string.example/v1:sk-other-test"},
        "prompts": {"旧提示词": "custom prompt"},
        "api": {"api_url": "https://google.example/v1", "enable_google": False,
                "lmarena_api_url": "https://lmarena.example/v1", "lmarena_api_key": "sk-lmarena-test",
                "lmarena_model_name": "custom-model", "enable_lmarena": False,
                "napcat_host": "custom-host", "napcat_port": 3456},
        "selfie": {"enable": True, "base_prompt": "custom identity"},
        "behavior": {"media_host_allowlist": []},
    }


def test_migration_finishes_before_old_fields_are_pruned(upgrade):
    plugin, keys, data, plugin_dir, target = upgrade
    raw = legacy_config()
    original = deepcopy(raw)
    config_path = plugin_dir / "config.toml"
    config_path.write_text(toml.dumps(raw), encoding="utf-8")
    before = config_path.read_bytes()
    normalized, changed = plugin.normalize_plugin_config(raw)
    assert changed
    assert "channels" not in normalized and "prompts" not in normalized
    assert "api_url" not in normalized["api"]
    assert config_path.read_bytes() == before  # TOML 由宿主在迁移成功后清理
    assert config_path.with_name("config.toml.pre-migration.bak").read_bytes() == before
    saved = json.loads((target / "data.json").read_text())
    assert saved["prompts"]["旧提示词"] == "custom prompt"
    assert saved["channels"]["custom"]["enabled"] is False
    assert saved["channels"]["google"]["enabled"] is False
    assert saved["channels"]["lmarena"]["enabled"] is False
    assert saved["channels"]["lmarena"]["model"] == "custom-model"
    assert all("key" not in channel for channel in saved["channels"].values())
    assert {entry["value"] for entry in keys.get_all_keys()} == {
        "sk-test-only", "sk-other-test", "sk-lmarena-test",
    }
    assert normalized["plugin"]["enabled"] is False
    assert normalized["api"]["napcat_port"] == 3456
    assert normalized["selfie"]["base_prompt"] == "custom identity"
    assert normalized["behavior"]["media_host_allowlist"] == []
    assert raw == original
    stamps = {p.name: p.stat().st_mtime_ns for p in target.iterdir()}
    plugin.normalize_plugin_config(raw)
    assert stamps == {p.name: p.stat().st_mtime_ns for p in target.iterdir()}


def test_existing_channel_key_status_and_prompt_win(upgrade):
    plugin, keys, data, _, target = upgrade
    write_json(target / "data.json", {"channels": {"custom": {"url": "https://new.example", "enabled": False}},
                                      "prompts": {"旧提示词": "new prompt"}})
    write_json(target / "keys.json", {"keys": [{"value": "sk-other-test", "type": "new-channel",
                                               "status": "disabled", "error_count": 4}]})
    plugin.normalize_plugin_config(legacy_config())
    assert data.get_channels()["custom"] == {"url": "https://new.example", "enabled": False}
    assert data.get_prompts()["旧提示词"] == "new prompt"
    assert not any(entry["value"] == "sk-test-only" for entry in keys.get_all_keys())
    old = next(entry for entry in keys.get_all_keys() if entry["value"] == "sk-other-test")
    assert old["status"] == "disabled" and old["type"] == "new-channel" and old["error_count"] == 4


def test_write_failure_keeps_original_config_and_can_retry(upgrade, monkeypatch):
    plugin, keys, data, plugin_dir, target = upgrade
    raw = legacy_config()
    config_path = plugin_dir / "config.toml"
    config_path.write_text(toml.dumps(raw), encoding="utf-8")
    before = config_path.read_bytes()
    with monkeypatch.context() as patch:
        def fail():
            raise OSError("test disk full")
        patch.setattr(data, "save_data", fail)
        with pytest.raises(OSError, match="disk full"):
            plugin.normalize_plugin_config(raw)
    assert config_path.read_bytes() == before
    assert len(keys.get_all_keys()) == 3  # 第一份 JSON 成功、第二份失败，也必须可重试
    plugin.normalize_plugin_config(raw)
    assert json.loads((target / "data.json").read_text())["prompts"]["旧提示词"] == "custom prompt"
    assert len(keys.get_all_keys()) == 3


def test_invalid_regular_config_does_not_start_migration(upgrade):
    plugin, _, _, plugin_dir, target = upgrade
    raw = legacy_config()
    raw["api"]["napcat_port"] = "invalid"
    config_path = plugin_dir / "config.toml"
    config_path.write_text(toml.dumps(raw), encoding="utf-8")
    before = config_path.read_bytes()
    with pytest.raises(ValueError):
        plugin.normalize_plugin_config(raw)
    assert config_path.read_bytes() == before
    assert not target.exists()


def test_legacy_data_copy_preserves_protection_records_and_originals(tmp_path):
    plugin_dir = tmp_path / "plugins" / "gemini_drawer"
    target = tmp_path / "data" / "plugins" / "drawer"
    legacy = tmp_path / "data" / "gemini_drawer"
    values = {"keys.json": {"keys": []}, "data.json": {"channels": {}, "prompts": {}},
              "banana_prompts.json": {"prompts": {"test": "content"}},
              "video_retry_guard.json": {"version": 1, "failed": {"test": 123}},
              "autonomous_quota.json": {"test": "quota"}}
    for name, value in values.items():
        write_json(legacy / name, value)
    keys, data = KeyManager(), DataManager()
    initialize_storage(target, plugin_dir, keys, data)
    assert keys.data_dir == data.data_dir == target
    for name in values:
        assert (target / name).read_bytes() == (legacy / name).read_bytes()
    # 新位置已有设置，旧目录变化不能反向覆盖。
    write_json(legacy / "keys.json", {"keys": [{"value": "stale", "type": "test"}]})
    initialize_storage(target, plugin_dir, keys, data)
    assert keys.get_all_keys() == []
    (target / "keys.json").unlink()
    initialize_storage(target, plugin_dir, keys, data)
    assert keys.get_all_keys() == []  # 清空新目录后不能再次导入旧 Key


def test_other_bot_does_not_import_main_bot_or_shared_source_data(tmp_path):
    plugin_dir = tmp_path / "plugins" / "gemini_drawer"
    for root in [tmp_path / "data" / "gemini_drawer", plugin_dir / "data"]:
        write_json(root / "data.json", {"channels": {"main-only": {}}, "prompts": {}})
    target = tmp_path / "bots" / "second" / "data" / "plugins" / "drawer"
    keys, data = KeyManager(), DataManager()
    initialize_storage(target, plugin_dir, keys, data)
    assert data.get_channels() == {}
    assert not (target / "data.json").exists()
    write_json(target.parent.parent / "gemini_drawer" / "data.json",
               {"channels": {"second-only": {}}, "prompts": {}})
    (target / ".legacy-storage-migrated.json").unlink()  # 模拟该 Bot 首次迁移已有的独立旧目录
    initialize_storage(target, plugin_dir, keys, data)
    assert set(data.get_channels()) == {"second-only"}


def test_invalid_destination_does_not_overwrite_or_clean_legacy_config(upgrade):
    plugin, _, _, plugin_dir, target = upgrade
    write_json(target / "keys.json", {"keys": "invalid"})
    before = (target / "keys.json").read_bytes()
    with pytest.raises(ValueError, match="数据无效"):
        plugin.normalize_plugin_config(legacy_config())
    assert (target / "keys.json").read_bytes() == before
    assert not (plugin_dir / "config.toml.pre-migration.bak").exists()


def test_managers_do_not_access_storage_before_context():
    keys, data = KeyManager(), DataManager()
    assert keys.data_dir is None and data.data_dir is None
    with pytest.raises(RuntimeError, match="尚未"):
        keys.get_all_keys()
    with pytest.raises(RuntimeError, match="尚未"):
        data.get_channels()


@pytest.mark.parametrize("suffix", ["generateContent", "streamGenerateContent", "streamGenerateContent?alt=sse"])
def test_gemini_endpoint_suffix_is_not_mistaken_for_key(upgrade, suffix):
    plugin, keys, data, _, _ = upgrade
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3:" + suffix
    raw = {"plugin": {"config_version": "1.10.0"}, "channels": {"google": url}}
    plugin.normalize_plugin_config(raw)
    assert data.get_channels()["google"]["url"] == url
    assert keys.get_all_keys() == []


def test_direct_deployment_old_root_directory_is_migrated(tmp_path):
    plugin_dir = tmp_path / "plugins" / "gemini_drawer"
    target = tmp_path / "data" / "plugins" / "drawer"
    legacy = tmp_path / "gemini_drawer"
    write_json(legacy / "data.json", {"channels": {"direct": {"url": "https://test.example"}}, "prompts": {}})
    write_json(legacy / "video_retry_guard.json", {"version": 1, "failed": {"test": 123}})
    keys, data = KeyManager(), DataManager()
    initialize_storage(target, plugin_dir, keys, data)
    assert set(data.get_channels()) == {"direct"}
    assert (target / "video_retry_guard.json").read_bytes() == (legacy / "video_retry_guard.json").read_bytes()
