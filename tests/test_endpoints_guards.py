"""端点构建与准入校验测试。

端点顺序即失败转移顺序，重构时必须与旧实现保持一致，故在此固定。
"""
import pytest

from gemini_drawer.core import endpoints as ep_mod
from gemini_drawer.core.guards import ADMIN_ONLY_NOTICE, evaluate_access, is_admin, message_user_id

GEMINI = "https://g.example/v1beta/models/m:generateContent"
OPENAI = "https://o.example/v1/chat/completions"
VIDEO = "https://ark.example/api/v3/contents/generations/tasks"


class FakeData:
    def __init__(self, channels):
        self._channels = channels

    def get_channels(self):
        return self._channels


class FakeKeys:
    def __init__(self, keys):
        self._keys = keys

    def get_all_keys(self):
        return self._keys


@pytest.fixture
def wire(monkeypatch):
    def _wire(channels, keys=()):
        monkeypatch.setattr(ep_mod, "data_manager", FakeData(channels))
        monkeypatch.setattr(ep_mod, "key_manager", FakeKeys(list(keys)))
    return _wire


def key(value, type_, status="active"):
    return {"value": value, "type": type_, "status": status}


# ── 绘图端点 ─────────────────────────────────────────────────


def test_managed_keys_expand_to_one_endpoint_each(wire):
    wire({"google": {"url": GEMINI, "enabled": True}},
         [key("k1", "google"), key("k2", "google")])

    got = ep_mod.build_drawing_endpoints()
    assert [e["key"] for e in got] == ["k1", "k2"]
    assert all(e["type"] == "custom_google" and e["url"] == GEMINI for e in got)


def test_disabled_keys_are_excluded(wire):
    wire({"google": {"url": GEMINI}}, [key("ok", "google"), key("dead", "google", status="disabled")])
    assert [e["key"] for e in ep_mod.build_drawing_endpoints()] == ["ok"]


def test_disabled_channel_is_excluded(wire):
    wire({"google": {"url": GEMINI, "enabled": False}}, [key("k1", "google")])
    assert ep_mod.build_drawing_endpoints() == []


def test_video_channel_excluded_from_drawing(wire):
    wire({"veo": {"url": VIDEO, "is_video": True}}, [key("k1", "veo")])
    assert ep_mod.build_drawing_endpoints() == []


def test_key_for_unknown_channel_is_ignored(wire):
    wire({"google": {"url": GEMINI}}, [key("orphan", "已删除的渠道")])
    assert ep_mod.build_drawing_endpoints() == []


def test_inline_keys_come_before_managed_keys(wire):
    """失败转移顺序：先渠道内联 Key，再 KeyManager 的 Key（与重构前一致）。"""
    wire(
        {
            "a": {"url": GEMINI, "key": "inline-a"},
            "b": {"url": OPENAI, "key": "inline-b", "model": "m"},
        },
        [key("managed-a", "a"), key("managed-b", "b")],
    )
    assert [e["key"] for e in ep_mod.build_drawing_endpoints()] == [
        "inline-a", "inline-b", "managed-a", "managed-b",
    ]


def test_legacy_url_colon_key_string_channel(wire):
    wire({"old": "https://legacy.example/v1/chat/completions:sk-legacy"})
    got = ep_mod.build_drawing_endpoints()
    assert got == [{
        "type": "custom_old",
        "url": "https://legacy.example/v1/chat/completions",
        "key": "sk-legacy",
        "model": None,
        "stream": False,
    }]


def test_untyped_key_is_attributed_by_prefix(wire):
    """早期 keys.json 没有 type 字段，按 sk- 前缀猜归属。"""
    wire({"bailili": {"url": OPENAI, "model": "m"}, "google": {"url": GEMINI}},
         [{"value": "sk-abc", "status": "active"}, {"value": "AIzaXyz", "status": "active"}])

    got = ep_mod.build_drawing_endpoints()
    assert [(e["type"], e["key"]) for e in got] == [
        ("custom_bailili", "sk-abc"),
        ("custom_google", "AIzaXyz"),
    ]


def test_model_and_stream_propagate(wire):
    wire({"o": {"url": OPENAI, "model": "gpt-image-2", "stream": True}}, [key("k", "o")])
    got = ep_mod.build_drawing_endpoints()[0]
    assert got["model"] == "gpt-image-2" and got["stream"] is True


# ── 视频端点 ─────────────────────────────────────────────────


def test_video_endpoints_only_include_video_channels(wire):
    wire({"google": {"url": GEMINI}, "veo": {"url": VIDEO, "is_video": True}},
         [key("k-draw", "google"), key("k-video", "veo")])

    got = ep_mod.build_video_endpoints()
    assert [e["key"] for e in got] == ["k-video"]


def test_video_channel_without_key_warns(wire):
    wire({"veo": {"url": VIDEO, "is_video": True}}, [])

    warnings = []
    class L:
        def warning(self, msg): warnings.append(msg)

    assert ep_mod.build_video_endpoints(logger=L()) == []
    assert len(warnings) == 1 and "veo" in warnings[0]


async def test_async_wrapper_matches_sync(wire):
    wire({"veo": {"url": VIDEO, "is_video": True, "key": "k"}})
    assert await ep_mod.get_video_endpoints(None) == ep_mod.build_video_endpoints()


# ── 准入校验 ─────────────────────────────────────────────────


def cfg(**overrides):
    values = {
        "general.enable_gemini_drawer": True,
        "general.blacklist_groups": [],
        "general.admins": [],
        "behavior.admin_only_mode": False,
    }
    values.update(overrides)
    return lambda key, default=None: values.get(key, default)


def test_access_allowed_by_default():
    assert evaluate_access(cfg(), group_id="123", user_id="456").allowed


def test_master_switch_blocks_silently():
    d = evaluate_access(cfg(**{"general.enable_gemini_drawer": False}), "123", "456")
    assert not d.allowed and d.message is None and d.should_stop is False


def test_blacklisted_group_blocks_silently():
    d = evaluate_access(cfg(**{"general.blacklist_groups": [123]}), group_id="123", user_id="456")
    assert not d.allowed and d.reason == "群黑名单" and d.message is None


def test_blacklist_compares_as_string():
    """配置里是 int，运行时拿到的是 str，必须能对上。"""
    assert not evaluate_access(cfg(**{"general.blacklist_groups": [16095807]}), "16095807", "1").allowed


def test_blacklist_does_not_affect_private_chat():
    assert evaluate_access(cfg(**{"general.blacklist_groups": [123]}), group_id=None, user_id="456").allowed


def test_admin_only_blocks_non_admin_with_notice():
    d = evaluate_access(
        cfg(**{"behavior.admin_only_mode": True, "general.admins": [999]}), "1", "456"
    )
    assert not d.allowed and d.message == ADMIN_ONLY_NOTICE and d.should_stop is True


def test_admin_only_allows_admin():
    d = evaluate_access(
        cfg(**{"behavior.admin_only_mode": True, "general.admins": [456]}), "1", "456"
    )
    assert d.allowed


def test_admin_only_does_not_block_when_user_unknown():
    """取不到 user_id 时放行，与重构前的行为保持一致。"""
    d = evaluate_access(cfg(**{"behavior.admin_only_mode": True, "general.admins": [999]}), "1", None)
    assert d.allowed


def test_is_admin_compares_as_string():
    assert is_admin(cfg(**{"general.admins": [1960408665]}), "1960408665")
    assert not is_admin(cfg(**{"general.admins": [1960408665]}), "1")
    assert not is_admin(cfg(**{"general.admins": [1]}), None)


def test_message_user_id_extraction():
    class UserInfo: user_id = 12345
    class MsgInfo: user_info = UserInfo()
    class Msg: message_info = MsgInfo()

    assert message_user_id(Msg()) == "12345"
    assert message_user_id(object()) is None
    assert message_user_id(None) is None
