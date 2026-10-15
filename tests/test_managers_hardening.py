"""Key/data 文件结构校验、损坏保护与记账字段的回归测试（P1-3 / P2-11 / P2-13）。"""
import json

import pytest

from gemini_drawer.core.managers import ConfigFileCorruptedError, DataManager, KeyManager


def test_keys_file_missing_fields_get_defaults(tmp_path):
    keys_file = tmp_path / "keys.json"
    keys_file.write_text(json.dumps({"keys": [{"value": "sk-abc", "type": "google", "custom": 1}]}), encoding="utf-8")

    manager = KeyManager(keys_file)
    entry = manager.get_all_keys()[0]

    assert entry["status"] == "active"
    assert entry["error_count"] == 0
    assert entry["max_errors"] == 5
    assert entry["last_used"] is None
    assert entry["custom"] == 1  # 未知字段保留


def test_add_keys_works_when_keys_field_missing(tmp_path):
    keys_file = tmp_path / "keys.json"
    keys_file.write_text(json.dumps({"current_index": 0}), encoding="utf-8")

    manager = KeyManager(keys_file)
    added, duplicates = manager.add_keys(["sk-new"], "google")

    assert (added, duplicates) == (1, 0)
    assert json.loads(keys_file.read_text())["keys"][0]["value"] == "sk-new"


def test_corrupt_keys_file_is_backed_up_and_write_blocked(tmp_path):
    keys_file = tmp_path / "keys.json"
    keys_file.write_text(json.dumps({"keys": "not-a-list"}), encoding="utf-8")
    before = keys_file.read_bytes()

    manager = KeyManager(keys_file)

    assert manager.get_all_keys() == []
    assert list(tmp_path.glob("keys.json.corrupt-*")), "损坏文件必须留下备份"
    with pytest.raises(ConfigFileCorruptedError):
        manager.save_config({"keys": [], "current_index": 0})
    assert keys_file.read_bytes() == before


def test_corrupt_keys_file_recovers_after_manual_fix(tmp_path):
    keys_file = tmp_path / "keys.json"
    keys_file.write_text("{ not json", encoding="utf-8")
    manager = KeyManager(keys_file)

    keys_file.write_text(json.dumps({"keys": []}), encoding="utf-8")
    manager.add_keys(["sk-fixed"], "google")

    assert json.loads(keys_file.read_text())["keys"][0]["value"] == "sk-fixed"


def test_record_key_usage_updates_last_used(tmp_path):
    keys_file = tmp_path / "keys.json"
    keys_file.write_text(
        json.dumps({"keys": [{"value": "k1", "type": "google", "status": "active",
                              "error_count": 0, "max_errors": 5, "last_used": None}]}),
        encoding="utf-8",
    )
    manager = KeyManager(keys_file)

    manager.record_key_usage("k1", True)

    assert json.loads(keys_file.read_text())["keys"][0]["last_used"] is not None


def test_corrupt_data_json_is_backed_up_and_write_blocked(tmp_path):
    data_file = tmp_path / "data.json"
    data_file.write_text(json.dumps({"channels": [], "prompts": {}}), encoding="utf-8")
    before = data_file.read_bytes()

    manager = DataManager(data_file)

    assert manager.get_prompts() == {}
    assert list(tmp_path.glob("data.json.corrupt-*"))
    with pytest.raises(ConfigFileCorruptedError):
        manager.save_data()
    assert data_file.read_bytes() == before


def test_banana_entry_count_is_cached_until_file_changes(tmp_path):
    data_file = tmp_path / "data.json"
    data_file.write_text(json.dumps({"channels": {}, "prompts": {}}), encoding="utf-8")
    banana_file = tmp_path / "banana_prompts.json"
    banana_file.write_text(json.dumps({"prompts": {"a": "1", "b": "2"}}), encoding="utf-8")

    manager = DataManager(data_file)

    assert manager.count_banana_entries() == 2
    assert manager._banana_count_cache == 2
    # 命中缓存后重复调用不改变结果
    assert manager.count_banana_entries() == 2

    banana_file.write_text(json.dumps({"prompts": {"a": "1", "b": "2", "c": "3"}}), encoding="utf-8")
    assert manager.count_banana_entries() == 3


def test_banana_count_respects_restricted_filter(tmp_path):
    data_file = tmp_path / "data.json"
    data_file.write_text(json.dumps({"channels": {}, "prompts": {}}), encoding="utf-8")
    banana_file = tmp_path / "banana_prompts.json"
    banana_file.write_text(
        json.dumps({"prompts": {"a": {"content": "1"}, "b": {"content": "2", "restricted": True}}}),
        encoding="utf-8",
    )

    manager = DataManager(data_file)

    assert manager.count_banana_entries(show_restricted=False) == 1
    assert manager.count_banana_entries(show_restricted=True) == 2
