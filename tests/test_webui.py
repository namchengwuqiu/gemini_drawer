"""管理页面的真实 JSON 写回、并发统计和编辑冲突测试。"""

from concurrent.futures import ThreadPoolExecutor

import json

import pytest

from gemini_drawer.core.managers import DataManager, KeyManager
from gemini_drawer.core.webui import DrawerWebUI


@pytest.fixture
def store(tmp_path):
    data_file = tmp_path / "data.json"
    keys_file = tmp_path / "keys.json"
    data_file.write_text(json.dumps({"channels": {"google": {"url": "https://example.com/v1/images/generations",
                                                            "key": "legacy-secret", "custom": {"nested": True}}},
                                     "prompts": {"头像": "人物头像"}, "other": "preserve"}), encoding="utf-8")
    keys_file.write_text(json.dumps({"keys": [{"value": "test-secret-key-one", "type": "google", "status": "active",
                                              "error_count": 2, "last_used": "yesterday", "max_errors": 5,
                                              "custom": "preserve"}], "current_index": 7}), encoding="utf-8")
    config_file = tmp_path / "config.toml"
    config_file.write_text('[plugin]\nenabled = true\n', encoding="utf-8")
    data = DataManager(data_file)
    keys = KeyManager(keys_file)
    return DrawerWebUI(data, keys), data, keys, config_file


def test_prompt_crud_only_writes_data_json(store):
    web, data, keys, config = store
    old_keys, old_config = keys.keys_file.read_bytes(), config.read_bytes()
    web.change("prompts", "save", "风景", "", {"text": "山与海"})
    detail = web.detail("prompts", "风景")
    web.change("prompts", "save", "风景", detail["version"], {"text": "日落下的山与海"})
    assert data.get_prompts()["风景"] == "日落下的山与海"
    detail = web.detail("prompts", "风景")
    web.change("prompts", "delete", "风景", detail["version"], {})
    assert data.get_prompts() == {"头像": "人物头像"}
    assert json.loads(data.data_file.read_text())["other"] == "preserve"
    assert old_keys == keys.keys_file.read_bytes()
    assert old_config == config.read_bytes()


def test_channel_preserves_inline_key_and_unknown_fields(store):
    web, data, _, _ = store
    detail = web.detail("channels", "google")
    assert detail["has_key"] and detail["payload"]["key"] == ""
    assert "legacy-secret" not in json.dumps(detail)
    web.change("channels", "save", "google", detail["version"], {"url": "https://example.com/new", "key": "",
                                                               "model": "model-2", "protocol": "images_json"})
    channel = data.get_channels()["google"]
    assert channel["custom"] == {"nested": True}
    assert channel["key"] == "legacy-secret"
    assert channel["model"] == "model-2"
    detail = web.detail("channels", "google")
    web.change("channels", "save", "google", detail["version"], {"clear_key": True, "protocol": ""})
    assert "key" not in data.get_channels()["google"]
    assert "protocol" not in data.get_channels()["google"]


def test_listing_is_paginated_searches_full_prompt_and_masks_keys(store):
    web, data, _, _ = store
    data.data["prompts"] = {f"名称{i}": "前缀" * 100 + "全文匹配" for i in range(45)}
    data.save_data()
    result = web.list_records("prompts", "全文匹配", 2)
    assert result["total"] == 45 and len(result["rows"]) == 20 and result["pages"] == 3
    assert web.list_records("prompts", "名称1")["total"] == 11
    key_result = web.list_records("keys")
    detail = web.detail("keys", key_result["rows"][0]["id"])
    assert "test-secret-key-one" not in json.dumps(key_result)
    assert "test-secret-key-one" not in json.dumps(detail)
    assert detail["payload"]["value"] == ""


def test_edit_conflict_does_not_overwrite_new_content(store):
    web, data, _, _ = store
    detail = web.detail("prompts", "头像")
    data.update_prompt("头像", "聊天指令已修改")
    before = data.data_file.read_bytes()
    with pytest.raises(ValueError, match="其他操作"):
        web.change("prompts", "save", "头像", detail["version"], {"text": "页面旧内容"})
    assert data.data_file.read_bytes() == before
    with pytest.raises(ValueError, match="已存在"):
        web.change("prompts", "save", "头像", "", {"text": "重复新增"})


def test_key_edit_keeps_latest_usage_state_order_and_unknown_fields(store):
    web, data, keys, config = store
    keys.add_keys(["test-secret-key-two"], "google")
    detail = web.detail("keys", web.list_records("keys")["rows"][0]["id"])
    old_data, old_config = data.data_file.read_bytes(), config.read_bytes()
    keys.record_key_usage("test-secret-key-one", False, force_disable=True)
    # 记账会更新 last_used；页面编辑必须保留这次记账后的最新值
    latest_last_used = json.loads(keys.keys_file.read_text())["keys"][0]["last_used"]
    assert latest_last_used != "yesterday"
    web.change("keys", "save", detail["id"], detail["version"], {"value": "", "max_errors": 9, "type": "google"})
    saved = json.loads(keys.keys_file.read_text())
    assert [key["value"] for key in saved["keys"]] == ["test-secret-key-one", "test-secret-key-two"]
    key = saved["keys"][0]
    assert key["error_count"] == 3 and key["status"] == "disabled"
    assert key["last_used"] == latest_last_used and key["custom"] == "preserve" and key["max_errors"] == 9
    assert saved["current_index"] == 7
    assert data.data_file.read_bytes() == old_data and config.read_bytes() == old_config
    detail = web.detail("keys", detail["id"])
    web.change("keys", "reset", detail["id"], detail["version"], {})
    assert keys.get_all_keys()[0]["status"] == "active" and keys.get_all_keys()[0]["error_count"] == 0


def test_key_add_replace_delete_and_duplicate_validation(store):
    web, _, keys, _ = store
    result = web.change("keys", "save", "", "", {"value": "new-test-key", "type": "google"})
    detail = web.detail("keys", result["id"])
    with pytest.raises(ValueError, match="已存在"):
        web.change("keys", "save", "", "", {"value": "new-test-key", "type": "google"})
    result = web.change("keys", "save", detail["id"], detail["version"], {"value": "replacement-test-key"})
    assert keys.get_all_keys()[1]["value"] == "replacement-test-key"
    detail = web.detail("keys", result["id"])
    web.change("keys", "delete", detail["id"], detail["version"], {})
    assert len(keys.get_all_keys()) == 1


def test_corrupt_file_is_not_overwritten(store):
    web, data, _, _ = store
    data.data_file.write_text('{"channels":', encoding="utf-8")
    before = data.data_file.read_bytes()
    with pytest.raises(ValueError, match="JSON 格式"):
        web.change("prompts", "save", "新记录", "", {"text": "新内容"})
    assert data.data_file.read_bytes() == before


def test_write_error_is_reported(store, monkeypatch):
    web, data, _, _ = store
    before = data.data_file.read_bytes()
    def fail(*args, **kwargs):
        raise PermissionError("read only")
    monkeypatch.setattr(data, "save_data", fail)
    with pytest.raises(PermissionError):
        web.change("prompts", "save", "新记录", "", {"text": "新内容"})
    assert data.data_file.read_bytes() == before


@pytest.mark.parametrize("payload", [
    {"url": "file:///etc/passwd"}, {"url": "https://example.com", "protocol": "seedance_video"},
    {"url": "https://example.com", "enabled": "yes"},
])
def test_invalid_channel_is_not_saved(store, payload):
    web, data, _, _ = store
    before = data.data_file.read_bytes()
    with pytest.raises(ValueError):
        web.change("channels", "save", "bad", "", payload)
    assert data.data_file.read_bytes() == before


def test_unknown_channel_url_requires_explicit_protocol(store):
    """与 /添加渠道 一致：无法识别类型的 URL 必须显式指定协议（F10）。"""
    web, data, _, _ = store
    before = data.data_file.read_bytes()
    with pytest.raises(ValueError, match="显式指定"):
        web.change("channels", "save", "newbad", "", {"url": "https://example.com/weird-path"})
    assert data.data_file.read_bytes() == before

    web.change("channels", "save", "newbad", "",
               {"url": "https://example.com/weird-path", "protocol": "images_json"})
    assert data.get_channels()["newbad"]["protocol"] == "images_json"

    # 仅清除协议、不重新提交 URL 的存量编辑仍可保存（不追溯卡死旧记录）。
    detail = web.detail("channels", "newbad")
    web.change("channels", "save", "newbad", detail["version"], {"protocol": ""})
    assert "protocol" not in data.get_channels()["newbad"]


def test_concurrent_usage_updates_and_new_records_are_not_lost(store):
    web, data, keys, _ = store
    def record_usage(_):
        keys.record_key_usage("test-secret-key-one", False)
    def add_prompt(index):
        web.change("prompts", "save", f"并发{index}", "", {"text": "提示词"})
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(record_usage, range(20)))
        list(pool.map(add_prompt, range(20)))
    assert keys.get_all_keys()[0]["error_count"] == 22
    assert len(data.get_prompts()) == 21


def test_two_managers_reload_before_chat_mutation(store):
    web, data, keys, _ = store
    other_data = DataManager(data.data_file)
    other_keys = KeyManager(keys.keys_file)
    web.change("prompts", "save", "页面新增", "", {"text": "保留"})
    other_data.add_prompt("聊天新增", "保留")
    assert set(data.get_prompts()) == {"头像", "页面新增", "聊天新增"}
    keys.add_keys(["another-test-key"], "google")
    other_keys.set_key_error_limit("google", 1, 10)
    assert len(keys.get_all_keys()) == 2
    assert keys.get_all_keys()[0]["max_errors"] == 10


def test_reused_key_and_historical_duplicates_remain_individual_records(store):
    web, data, keys, _ = store
    data.add_channel("other", {"url": "https://example.com/v1/images/generations"})
    first = dict(keys.get_all_keys()[0])
    keys.config["keys"] += [{**first, "type": "other"}, dict(first)]
    keys.save_config(keys.config)
    rows = web.list_records("keys")["rows"]
    assert len(rows) == 3 and len({row["id"] for row in rows}) == 3
    detail = web.detail("keys", rows[0]["id"])
    web.change("keys", "save", detail["id"], detail["version"], {"max_errors": 10})
    saved = keys.get_all_keys()
    assert [key["max_errors"] for key in saved] == [10, 5, 5]
    detail = web.detail("keys", rows[2]["id"])
    web.change("keys", "delete", detail["id"], detail["version"], {})
    assert [key["type"] for key in keys.get_all_keys()] == ["google", "other"]


def test_same_key_can_be_added_for_another_channel(store):
    web, data, keys, _ = store
    data.add_channel("other", {"url": "https://example.com/v1/images/generations"})
    web.change("keys", "save", "", "", {"value": "test-secret-key-one", "type": "other"})
    assert len(keys.get_all_keys()) == 2


def test_channel_view_joins_keys_without_modifying_either_file(store):
    web, data, keys, config = store
    data.add_channel("other", {"url": "https://example.com/other"})
    keys.add_keys([f"extra-test-key-{index}" for index in range(24)], "google")
    keys.add_keys(["other-channel-test-key"], "other")
    before = [file.read_bytes() for file in (data.data_file, keys.keys_file, config)]
    result = web.list_records("channels")
    google, other = result["rows"]
    assert google["key_count"] == 25 and len(google["keys"]) == 5
    assert google["has_inline_key"] is True
    assert other["key_count"] == 1 and other["keys"][0]["channel"] == "other"
    second_page = web.list_records("keys", channel="google", page=2)
    assert second_page["total"] == 25 and len(second_page["rows"]) == 5
    assert all(row["channel"] == "google" for row in second_page["rows"])
    assert "test-secret-key-one" not in json.dumps(result)
    assert [file.read_bytes() for file in (data.data_file, keys.keys_file, config)] == before


def test_orphan_keys_are_visible_after_channel_deletion(store):
    web, data, keys, _ = store
    data.delete_channel("google")
    joined = web.list_records("channels")
    assert joined["rows"] == []
    assert joined["unassigned"]["total"] == 1
    detail = web.detail("keys", joined["unassigned"]["rows"][0]["id"])
    web.change("keys", "save", detail["id"], detail["version"], {"type": "google", "max_errors": 10})
    assert keys.get_all_keys()[0]["max_errors"] == 10


def test_channel_search_finds_keys_and_legacy_key_ownership(store):
    web, _, keys, _ = store
    keys.config["keys"][0].pop("type")
    keys.config["keys"][0]["status"] = "disabled"
    keys.save_config(keys.config)
    joined = web.list_records("channels", search="disabled")
    assert joined["total"] == 1 and joined["rows"][0]["keys"][0]["channel"] == "google"
    assert web.list_records("keys", channel="google")["total"] == 1


def test_banana_readonly_listing_search_and_full_detail(store):
    web, data, keys, config = store
    banana = {"synced_at": "2026-10-13T18:00:00+08:00", "remote_last_updated": "2026-10-12",
              "prompts": {f"大香蕉/风景/词条{i}": {"content": "前缀" * 100 + "全文匹配",
                                                 "section_title": "风景", "source_title": f"词条{i}",
                                                 "restricted": i % 2 == 0} for i in range(25)}}
    data.save_banana_data(banana)
    before = [path.read_bytes() for path in (data.data_file, data.banana_file, keys.keys_file, config)]
    result = web.list_records("banana", search="全文匹配", page=2)
    assert result["total"] == 25 and len(result["rows"]) == 5
    assert result["banana"]["readonly"] and result["banana"]["remote_last_updated"] == "2026-10-12"
    assert result["counts"]["banana"] == 25
    assert web.list_records("banana", search="风景")["total"] == 25
    detail = web.detail("banana", result["rows"][0]["id"])
    assert detail["readonly"] and detail["payload"]["text"].endswith("全文匹配")
    assert [path.read_bytes() for path in (data.data_file, data.banana_file, keys.keys_file, config)] == before


def test_banana_page_shows_callable_numbered_names(store):
    web, data, _, _ = store
    data.save_banana_data({"prompts": {
        "大香蕉/B/举猫猫": {"content": "second", "source_title": "举猫猫"},
        "大香蕉/A/举猫猫": {"content": "first", "source_title": "举猫猫"},
    }})
    before = data.banana_file.read_bytes()
    result = web.list_records("banana", search="举猫猫2")
    assert result["total"] == 1
    row = result["rows"][0]
    assert row["name"] == "举猫猫2" and row["id"] == "大香蕉/B/举猫猫"
    detail = web.detail("banana", row["id"])
    assert detail["payload"]["command"] == "/+举猫猫2"
    assert data.get_prompt_matches(row["name"]) == {row["name"]: "second"}
    assert data.banana_file.read_bytes() == before


@pytest.mark.parametrize("operation", ["save", "delete", "reset"])
def test_banana_mutations_are_rejected_without_changing_files(store, operation):
    web, data, keys, config = store
    data.save_banana_data({"prompts": {"旧格式": "只读词条"}})
    before = [path.read_bytes() for path in (data.data_file, data.banana_file, keys.keys_file, config)]
    with pytest.raises(ValueError, match="只读"):
        web.change("banana", operation, "旧格式", "", {"text": "覆盖"})
    assert web.detail("banana", "旧格式")["payload"]["text"] == "只读词条"
    assert [path.read_bytes() for path in (data.data_file, data.banana_file, keys.keys_file, config)] == before


def test_banana_missing_and_corrupt_file_are_distinguished(store):
    web, data, _, _ = store
    assert web.list_records("banana")["total"] == 0
    data.banana_file.write_text('{"prompts":', encoding="utf-8")
    with pytest.raises(ValueError, match="JSON 格式"):
        web.list_records("banana")
    assert data.banana_file.read_text() == '{"prompts":'


def test_channel_request_order_save_list_clear_and_runtime(store, monkeypatch):
    from gemini_drawer.core import endpoints
    web, data, keys, _ = store
    monkeypatch.setattr(endpoints, "data_manager", data)
    monkeypatch.setattr(endpoints, "key_manager", keys)
    web.change("channels", "save", "preferred", "", {
        "url": "https://example.com/v1/images/generations", "key": "preferred-key", "request_order": 1})
    assert endpoints.build_drawing_endpoints()[0]["type"] == "custom_preferred"
    assert next(row for row in web.list_records("channels")["rows"] if row["id"] == "preferred")["request_order"] == 1
    assert json.loads(data.data_file.read_text())["channels"]["preferred"]["request_order"] == 1
    detail = web.detail("channels", "preferred")
    web.change("channels", "save", "preferred", detail["version"], {"request_order": None})
    assert "request_order" not in data.get_channels()["preferred"]
    assert endpoints.build_drawing_endpoints()[0]["type"] == "custom_google"


@pytest.mark.parametrize("invalid", [True, 0, -1, 1.5, "1", {}, [], 2147483648])
def test_channel_request_order_invalid_does_not_write(store, invalid):
    web, data, _, _ = store
    before = data.data_file.read_bytes()
    detail = web.detail("channels", "google")
    with pytest.raises(ValueError, match="请求顺序"):
        web.change("channels", "save", "google", detail["version"], {"request_order": invalid})
    assert data.data_file.read_bytes() == before


def test_channel_media_filter_before_pagination_and_with_search(store):
    web, data, _, _ = store
    data.data["channels"] = {
        **{f"image-{i}": {"url": "https://example.com/v1/images/generations"} for i in range(25)},
        **{f"video-{i}": {"url": "https://example.com/video", "is_video": True} for i in range(23)},
        "legacy": "https://example.com/v1/images/generations:sk-legacy",
    }
    data.save_data()
    images = web.list_records("channels", media_type="image", page=2)
    assert images["total"] == 26 and len(images["rows"]) == 6
    assert all(not row["is_video"] for row in images["rows"])
    videos = web.list_records("channels", media_type="video", page=2)
    assert videos["total"] == 23 and len(videos["rows"]) == 3
    assert all(row["is_video"] for row in videos["rows"])
    assert web.list_records("channels", media_type="video", search="image")["total"] == 0
    assert web.list_records("channels")["total"] == 49
    assert web.list_records("prompts", media_type="video")["total"] == 1
    with pytest.raises(ValueError, match="渠道类型"):
        web.list_records("channels", media_type="invalid")
