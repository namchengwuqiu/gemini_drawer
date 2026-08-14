"""提示词与渠道数据管理测试。

重点是三件容易出错的事：
- 限制级（restricted）提示词的过滤
- 本地词库与只读大香蕉词库合并时的重名消歧
- 大文件（data.json ~700KB / banana_prompts.json ~2.6MB）的指纹缓存与失效
"""
import json

import pytest

from gemini_drawer.core.managers import DataManager


@pytest.fixture
def dm(tmp_path):
    """用临时目录构造 DataManager，不触碰真实数据目录。"""
    data_file = tmp_path / "data" / "data.json"
    data_file.parent.mkdir(parents=True)
    data_file.write_text(json.dumps({"prompts": {}, "channels": {}}), encoding="utf-8")
    return DataManager(data_file)


def write_banana(dm, prompts):
    dm.banana_file.write_text(
        json.dumps({"schema_version": 1, "prompts": prompts}), encoding="utf-8"
    )
    # 清掉指纹缓存，模拟"另一个进程刚写完"
    dm._banana_stamp = None


# ── 本地词库 CRUD ────────────────────────────────────────────


def test_add_and_get_prompt(dm):
    dm.add_prompt("水彩", "watercolor style")
    assert dm.get_prompts() == {"水彩": "watercolor style"}


def test_update_prompt_only_touches_existing(dm):
    dm.add_prompt("水彩", "v1")
    assert dm.update_prompt("水彩", "v2") is True
    assert dm.update_prompt("不存在", "v") is False
    assert dm.get_prompts()["水彩"] == "v2"


def test_delete_prompt(dm):
    dm.add_prompt("水彩", "v1")
    assert dm.delete_prompt("水彩") is True
    assert dm.delete_prompt("水彩") is False
    assert dm.get_prompts() == {}


def test_channel_crud(dm):
    dm.add_channel("google", {"url": "https://g/x:generateContent", "enabled": True})
    assert "google" in dm.get_channels()
    dm.update_channel("google", {"url": "https://g/y:generateContent", "enabled": False})
    assert dm.get_channels()["google"]["enabled"] is False
    assert dm.delete_channel("google") is True
    assert dm.delete_channel("google") is False


def test_data_survives_reload_from_disk(dm):
    dm.add_prompt("水彩", "v1")
    dm.add_channel("google", {"url": "u"})
    reloaded = DataManager(dm.data_file)
    assert reloaded.get_prompts() == {"水彩": "v1"}
    assert reloaded.get_channels() == {"google": {"url": "u"}}


# ── 大香蕉只读词库 ───────────────────────────────────────────


def test_restricted_prompts_hidden_by_default(dm):
    write_banana(dm, {
        "大香蕉/人像/清新": {"content": "fresh portrait", "restricted": False},
        "大香蕉/猎奇/重口": {"content": "gore", "restricted": True},
    })
    assert set(dm.get_banana_prompts()) == {"大香蕉/人像/清新"}


def test_restricted_prompts_shown_when_enabled(dm):
    write_banana(dm, {
        "大香蕉/人像/清新": {"content": "fresh portrait", "restricted": False},
        "大香蕉/猎奇/重口": {"content": "gore", "restricted": True},
    })
    assert len(dm.get_banana_prompts(show_restricted=True)) == 2


def test_legacy_string_entries_are_supported(dm):
    write_banana(dm, {"大香蕉/旧/条目": "plain string content"})
    assert dm.get_banana_prompts() == {"大香蕉/旧/条目": "plain string content"}


def test_blank_and_malformed_entries_are_skipped(dm):
    write_banana(dm, {
        "空内容": {"content": "   "},
        "错类型": 12345,
        "正常": {"content": "ok"},
    })
    assert dm.get_banana_prompts() == {"正常": "ok"}


def test_missing_banana_file_is_not_fatal(dm):
    assert dm.get_banana_prompts() == {}
    assert dm.load_banana_data()["prompts"] == {}


def test_corrupt_banana_file_is_ignored(dm):
    dm.banana_file.write_text("{ not json", encoding="utf-8")
    dm._banana_stamp = None
    assert dm.get_banana_prompts() == {}


def test_entries_keep_metadata_for_search(dm):
    write_banana(dm, {
        "大香蕉/人像/清新": {"content": "c", "restricted": False, "section_title": "人像"}
    })
    entry = dm.get_banana_prompt_entries()["大香蕉/人像/清新"]
    assert entry["section_title"] == "人像"


# ── 合并视图 ─────────────────────────────────────────────────


def test_effective_prompts_merges_local_and_banana(dm):
    dm.add_prompt("本地风格", "local")
    write_banana(dm, {"大香蕉/x/y": {"content": "remote"}})
    assert dm.get_effective_prompts() == {"本地风格": "local", "大香蕉/x/y": "remote"}


def test_local_prompt_wins_and_banana_gets_suffix_on_collision(dm):
    """同名时本地词库优先，大香蕉条目改名后仍然可用，不能直接丢弃。"""
    dm.add_prompt("撞名", "local-version")
    write_banana(dm, {"撞名": {"content": "banana-version"}})

    merged = dm.get_effective_prompts()
    assert merged["撞名"] == "local-version"
    assert merged["撞名#banana"] == "banana-version"


def test_banana_can_be_disabled(dm):
    dm.add_prompt("本地风格", "local")
    write_banana(dm, {"大香蕉/x/y": {"content": "remote"}})
    assert dm.get_effective_prompts(include_banana=False) == {"本地风格": "local"}


def test_effective_prompts_respects_restricted_flag(dm):
    write_banana(dm, {"限制级": {"content": "x", "restricted": True}})
    assert dm.get_effective_prompts() == {}
    assert dm.get_effective_prompts(show_restricted=True) == {"限制级": "x"}


# ── 指纹缓存 ─────────────────────────────────────────────────


def test_repeated_reads_do_not_reparse(dm, monkeypatch):
    dm.add_channel("google", {"url": "u"})
    write_banana(dm, {"a": {"content": "c"}})
    dm.get_banana_prompts()  # 预热

    calls = []
    real_load = json.load
    monkeypatch.setattr(json, "load", lambda fp, *a, **k: (calls.append(1), real_load(fp, *a, **k))[1])

    for _ in range(5):
        dm.get_channels()
        dm.get_prompts()
        dm.get_banana_prompts()

    assert calls == []


def test_external_write_invalidates_cache(dm):
    dm.add_channel("google", {"url": "u"})
    assert len(dm.get_channels()) == 1

    # 模拟另一个进程（如 WebUI）改了文件
    payload = {"prompts": {"新增": "p"}, "channels": {"google": {"url": "u"}, "openai": {"url": "v"}}}
    dm.data_file.write_text(json.dumps(payload), encoding="utf-8")
    dm._data_stamp = None

    assert len(dm.get_channels()) == 2
    assert dm.get_prompts() == {"新增": "p"}


def test_save_refreshes_cache_stamp(dm):
    dm.add_prompt("a", "1")
    stamp_after_save = dm._data_stamp
    assert stamp_after_save is not None
    # 紧接着读一次不应触发重新解析
    assert dm.get_prompts() == {"a": "1"}
    assert dm._data_stamp == stamp_after_save


def test_save_data_is_atomic(dm):
    """写入过程不应留下 .tmp 残留。"""
    dm.add_prompt("a", "1")
    assert list(dm.data_file.parent.glob("*.tmp")) == []
    assert json.loads(dm.data_file.read_text(encoding="utf-8"))["prompts"] == {"a": "1"}
