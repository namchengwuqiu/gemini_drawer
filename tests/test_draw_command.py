"""BaseDrawCommand.execute() 的端到端测试。

这条路径重构幅度最大（原来两份各约 400 行的 execute 合并成一份），
所以在这里把完整链路跑一遍：准入 → 取提示词 → 取图 → 调管线 → 发图 → 撤回状态消息。
"""
import base64
import importlib.util
import sys
from pathlib import Path

import httpx
import pytest

# maibot_sdk_tmp 是 SDK 的本地快照，按真实包名挂上去后才能导入 base_commands
REPO_ROOT = Path(__file__).resolve().parents[4]
if "maibot_sdk" not in sys.modules:
    sdk_dir = REPO_ROOT / "maibot_sdk_tmp"
    if not sdk_dir.exists():
        pytest.skip("maibot_sdk 快照不存在，跳过命令层集成测试", allow_module_level=True)
    spec = importlib.util.spec_from_file_location(
        "maibot_sdk", sdk_dir / "__init__.py", submodule_search_locations=[str(sdk_dir)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["maibot_sdk"] = module
    spec.loader.exec_module(module)

from gemini_drawer.commands import base_commands  # noqa: E402
from gemini_drawer.core import pipeline  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"p" * 300
PNG_B64 = base64.b64encode(PNG).decode()
OUT_B64 = base64.b64encode(b"generated" * 300).decode()
GEMINI_URL = "https://g.example/v1beta/models/m:generateContent"


class Seg:
    def __init__(self, type_, data):
        self.type, self.data = type_, data


class SegList:
    type = "seglist"
    def __init__(self, segs):
        self.data = segs


class ChatStream:
    def __init__(self, group_id=None):
        self.stream_id = "stream-1"
        self.platform = "qq"
        self.group_info = type("G", (), {"group_id": group_id})() if group_id else None
        self.user_info = type("U", (), {"user_id": "456"})()


class Message:
    def __init__(self, segments, group_id="123"):
        self.message_segment = segments
        self.session_id = "stream-1"
        self.user_id = "456"
        self.chat_stream = ChatStream(group_id)
        self.message_info = type(
            "MI", (), {"user_info": type("U", (), {"user_id": "456"})(), "group_info": None}
        )()


class FakeCtx:
    """记录所有对外发送/调用，替代真实的插件上下文。"""

    def __init__(self):
        self.hybrid_calls = []
        self.api_calls = []
        outer = self

        class Send:
            async def hybrid(self, segments, stream_id):
                outer.hybrid_calls.append((segments, stream_id))
                return True

        class Api:
            async def call(self, name, **kw):
                outer.api_calls.append((name, kw))
                return {"success": True}

        class MessageCap:
            async def get_by_time_in_chat(self, **kw):
                return []

            async def get_by_id(self, *a, **kw):
                return None

        self.send, self.api, self.message = Send(), Api(), MessageCap()


class DrawCmd(base_commands.BaseDrawCommand):
    command_name = "test_draw"
    allow_text_only = True

    async def get_prompt(self):
        return "一只猫"


class MultiCmd(base_commands.BaseMultiImageDrawCommand):
    command_name = "test_multi"

    async def get_prompt(self):
        return "融合"


CONFIG = {
    "general": {"enable_gemini_drawer": True, "blacklist_groups": [], "admins": []},
    "behavior": {
        "admin_only_mode": False,
        "reply_with_image": True,
        "auto_recall_status": False,
        "success_notify_poke": True,
        "debug_mode": False,
    },
    "proxy": {"enable": False, "proxy_url": ""},
}


def make_cmd(cls=DrawCmd, segments=None, config=None, group_id="123"):
    msg = Message(segments if segments is not None else SegList([Seg("text", "/绘图 一只猫")]), group_id)
    cmd = cls(message=msg, plugin_config=config or CONFIG)
    cmd._stream_id = "stream-1"
    cmd.ctx = FakeCtx()
    cmd.sent_texts = []

    async def send_text(text, **kw):
        cmd.sent_texts.append(text)
        return True

    cmd.send_text = send_text
    return cmd


@pytest.fixture
def wire(monkeypatch):
    """接管端点来源、Key 记账与 HTTP。"""
    state = {"handler": lambda r: httpx.Response(200, json=_gemini_ok()), "requests": []}

    monkeypatch.setattr(
        base_commands, "build_drawing_endpoints",
        lambda: [{"type": "custom_g", "url": GEMINI_URL, "key": "k1", "model": None, "stream": False}],
    )
    monkeypatch.setattr(pipeline, "key_manager", type("K", (), {"record_key_usage": lambda *a, **k: None})())

    real_client = httpx.AsyncClient

    def factory(**kwargs):
        kwargs.pop("proxy", None)
        kwargs.pop("transport", None)
        def dispatch(request):
            state["requests"].append(request)
            return state["handler"](request)
        return real_client(transport=httpx.MockTransport(dispatch), **kwargs)

    monkeypatch.setattr(pipeline.httpx, "AsyncClient", factory)
    return state


def _gemini_ok():
    return {"candidates": [{"content": {"parts": [{"inlineData": {"data": OUT_B64}}]}}]}


# ── 主流程 ───────────────────────────────────────────────────


async def test_text_to_image_sends_hybrid_message(wire):
    cmd = make_cmd()
    ok, reason, stop = await cmd.execute()

    assert (ok, reason, stop) == (True, "绘图成功", True)
    segments, stream_id = cmd.ctx.hybrid_calls[0]
    assert stream_id == "stream-1"
    assert segments[0] == {"type": "at", "data": {"target_user_id": "456"}}
    assert segments[-1] == {"type": "image", "content": OUT_B64}


async def test_private_chat_omits_at_segment(wire):
    """QQ 私聊不支持 at 段，带上会导致整条消息被拒。"""
    cmd = make_cmd(group_id=None)
    await cmd.execute()

    segments, _ = cmd.ctx.hybrid_calls[0]
    assert all(s["type"] != "at" for s in segments)
    assert segments[-1]["type"] == "image"


async def test_caption_is_attached_to_first_image_only(wire):
    wire["handler"] = lambda r: httpx.Response(
        200,
        json={"candidates": [{"content": {"parts": [
            {"inlineData": {"data": OUT_B64}}, {"inlineData": {"data": OUT_B64}}
        ]}}]},
    )
    cmd = make_cmd()
    cmd.get_image_caption = lambda: "🎲 水彩风格"
    await cmd.execute()

    first, second = cmd.ctx.hybrid_calls[0][0], cmd.ctx.hybrid_calls[1][0]
    assert any(s.get("content", "").strip() == "🎲 水彩风格" for s in first if s["type"] == "text")
    assert all(s["type"] != "text" for s in second)


async def test_image_in_message_becomes_img2img(wire):
    cmd = make_cmd(segments=SegList([Seg("text", "/bnn 换风格"), Seg("image", PNG_B64)]))
    await cmd.execute()

    import json
    sent = json.loads(wire["requests"][0].content)
    parts = sent["contents"][0]["parts"]
    assert parts[0]["inline_data"]["data"] == PNG_B64


async def test_generation_failure_reports_reason_to_user(wire):
    wire["handler"] = lambda r: httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}})
    cmd = make_cmd()

    ok, reason, _ = await cmd.execute()

    assert reason == "所有尝试均失败"
    assert any("生成失败" in t and "SAFETY" in t for t in cmd.sent_texts)


async def test_unconfirmed_timeout_rotates_to_next_endpoint(wire, monkeypatch):
    """命令侧同样适用超时轮换：首个端点没等到结果就换下一个端点出图。"""
    attempts = []
    second_url = "https://second.example/v1/chat/completions"
    monkeypatch.setattr(pipeline, "RETRY_DELAY", 0.0)
    monkeypatch.setattr(
        base_commands, "build_drawing_endpoints",
        lambda: [
            {"type": "custom_first", "url": GEMINI_URL, "key": "k1", "model": None, "stream": False},
            {"type": "custom_second", "url": second_url, "key": "k2", "model": None, "stream": False},
        ],
    )

    def handler(request):
        attempts.append(str(request.url))
        if str(request.url).startswith(GEMINI_URL):
            raise httpx.ReadTimeout("response not confirmed", request=request)
        return httpx.Response(200, json=_gemini_ok())

    wire["handler"] = handler
    cmd = make_cmd()

    ok, reason, stop = await cmd.execute()

    assert (ok, reason, stop) == (True, "绘图成功", True)
    # Gemini 渠道会把 Key 拼进查询串，比较主机与顺序即可。
    assert len(attempts) == 2
    assert attempts[0].startswith(GEMINI_URL)
    assert attempts[1] == second_url
    assert len(cmd.ctx.hybrid_calls) == 1


async def test_no_endpoints_configured(wire, monkeypatch):
    monkeypatch.setattr(base_commands, "build_drawing_endpoints", list)
    cmd = make_cmd()

    ok, reason, _ = await cmd.execute()

    assert reason == "无可用密钥或端点"
    assert any("未配置任何API密钥或端点" in t for t in cmd.sent_texts)


async def test_empty_prompt_short_circuits(wire):
    cmd = make_cmd()
    cmd.get_prompt = lambda: _none()
    ok, reason, _ = await cmd.execute()
    assert reason == "无效的Prompt"
    assert wire["requests"] == []


async def _none():
    return None


# ── 准入 ─────────────────────────────────────────────────────


async def test_blacklisted_group_is_rejected_silently(wire):
    config = {**CONFIG, "general": {**CONFIG["general"], "blacklist_groups": [123]}}
    cmd = make_cmd(config=config)

    ok, reason, stop = await cmd.execute()

    assert (reason, stop) == ("群黑名单", False)
    assert cmd.sent_texts == []
    assert wire["requests"] == []


async def test_master_switch_off_is_rejected(wire):
    config = {**CONFIG, "general": {**CONFIG["general"], "enable_gemini_drawer": False}}
    cmd = make_cmd(config=config)

    ok, reason, stop = await cmd.execute()

    assert (reason, stop) == ("Plugin disabled", False)
    assert wire["requests"] == []


async def test_admin_only_mode_notifies_non_admin(wire):
    config = {
        **CONFIG,
        "general": {**CONFIG["general"], "admins": [999]},
        "behavior": {**CONFIG["behavior"], "admin_only_mode": True},
    }
    cmd = make_cmd(config=config)

    ok, reason, stop = await cmd.execute()

    assert (reason, stop) == ("管理员专用模式", True)
    assert cmd.sent_texts == ["⚠️ 管理员已关闭绘图功能"]


# ── 多图 ─────────────────────────────────────────────────────


async def test_multi_image_requires_two_images(wire):
    cmd = make_cmd(MultiCmd, segments=SegList([Seg("image", PNG_B64)]))

    ok, reason, _ = await cmd.execute()

    assert reason == "参考图不满足要求"
    assert any("至少提供2张图片" in t for t in cmd.sent_texts)
    assert wire["requests"] == []


async def test_multi_image_succeeds_and_uses_hybrid_send(wire):
    """重构后 /多图 也走 hybrid，从而和单图一样支持 @提及与 caption。"""
    cmd = make_cmd(MultiCmd, segments=SegList([Seg("image", PNG_B64), Seg("image", PNG_B64)]))

    ok, reason, _ = await cmd.execute()

    assert reason == "绘图成功"
    segments, _ = cmd.ctx.hybrid_calls[0]
    assert segments[0]["type"] == "at"

    import json
    parts = json.loads(wire["requests"][0].content)["contents"][0]["parts"]
    assert [p.get("text") for p in parts if "text" in p] == ["Image 1:", "Image 2:", "Prompt: 融合"]


async def test_real_multi_command_sends_only_typed_prompt_and_both_images(wire, monkeypatch):
    """使用真实 /多图 解析器，检查实际 chat 请求中的提示词与两张参考图。"""
    import json
    from unittest.mock import AsyncMock
    from gemini_drawer.commands.draw_commands import MultiImageDrawCommand
    from gemini_drawer.core import host_bridge
    from gemini_drawer.plugin import CompatMessage

    jpeg = b'\xff\xd8' + b'j' * 300
    command = make_cmd(MultiImageDrawCommand)
    command.message = CompatMessage({
        'message_id': 'current-command', 'session_id': 'stream-1', 'user_id': '456',
        'processed_plain_text': '/多图两个人贴贴\n [图片：蓝发角色的自动描述] [图片：黑白发角色的自动描述]',
        'raw_message': [
            {'type': 'text', 'data': '/多图两个人贴贴\n'},
            {'type': 'image', 'data': '[图片：蓝发角色的自动描述]', 'hash': 'a' * 64},
            {'type': 'image', 'data': '[图片：黑白发角色的自动描述]', 'hash': 'b' * 64},
        ],
    })
    monkeypatch.setattr(host_bridge, 'image_from_hash', AsyncMock(
        side_effect=lambda digest, kind: {'a' * 64: PNG, 'b' * 64: jpeg}.get(digest)
    ))
    monkeypatch.setattr(base_commands, 'build_drawing_endpoints', lambda: [{
        'type': 'custom_chat', 'url': 'https://example.com/v1/chat/completions',
        'key': 'test-key', 'model': 'gpt-image-2', 'protocol': 'gpt_image', 'stream': False,
    }])

    _, reason, _ = await command.execute()

    assert reason == '绘图成功'
    assert len(wire['requests']) == 1
    content = json.loads(wire['requests'][0].content)['messages'][0]['content']
    assert [part['text'] for part in content if part['type'] == 'text'] == [
        'Prompt: 两个人贴贴', 'Image 1:', 'Image 2:',
    ]
    assert [part['image_url']['url'] for part in content if part['type'] == 'image_url'] == [
        'data:image/png;base64,' + PNG_B64,
        'data:image/jpeg;base64,' + base64.b64encode(jpeg).decode(),
    ]


@pytest.mark.parametrize('cached', [False, True])
async def test_multi_image_command_hydrates_current_runtime_references(wire, monkeypatch, cached):
    """覆盖未入库命令的 hash 缓存取图，以及已入库消息的官方能力取图。"""
    import json
    from unittest.mock import AsyncMock
    from gemini_drawer.plugin import CompatMessage
    from gemini_drawer.core import host_bridge

    jpg_b64 = base64.b64encode(b"\xff\xd8" + b"j" * 300).decode()
    raw = {
        "message_id": "current-images",
        "session_id": "stream-1",
        "user_id": "456",
        "raw_message": [
            {"type": "text", "data": "/多图两个人贴贴\n"},
            {"type": "image", "data": "", "hash": "a" * 64},
            {"type": "image", "data": "", "hash": "b" * 64},
        ],
    }
    cmd = make_cmd(MultiCmd)
    cmd.message = CompatMessage(raw)
    monkeypatch.setattr(host_bridge, 'image_from_hash', AsyncMock(
        side_effect=lambda digest, kind: {'a' * 64: PNG, 'b' * 64: base64.b64decode(jpg_b64)}.get(digest) if cached else None
    ))
    cmd.ctx.message.get_by_id = AsyncMock(return_value=None if cached else {
        **raw,
        "raw_message": [
            raw["raw_message"][0],
            {**raw["raw_message"][1], "binary_data_base64": PNG_B64},
            {**raw["raw_message"][2], "binary_data_base64": jpg_b64},
        ],
    })

    _, reason, _ = await cmd.execute()

    assert reason == "绘图成功"
    if cached:
        cmd.ctx.message.get_by_id.assert_not_awaited()
    else:
        cmd.ctx.message.get_by_id.assert_awaited_once_with(
            "current-images", stream_id="stream-1", include_binary_data=True
        )
    parts = json.loads(wire["requests"][0].content)["contents"][0]["parts"]
    assert [p["inline_data"]["data"] for p in parts if "inline_data" in p] == [PNG_B64, jpg_b64]
    assert len(wire["requests"]) == 1


async def test_images_are_not_sent_after_budget_expires_during_download(wire, monkeypatch):
    """下载完成后预算已耗尽：不得再发送（F1，复现宿主超时后仍发图的事故）。"""
    clock_state = {"now": 1000.0}
    real_budget_cls = base_commands.TimeBudget

    class ClockedBudget(real_budget_cls):
        def __init__(self, total_seconds, *, reserve_seconds=0.0, clock=None):
            super().__init__(
                total_seconds,
                reserve_seconds=reserve_seconds,
                clock=lambda: clock_state["now"],
            )

    from gemini_drawer.core import budget as budget_module
    monkeypatch.setattr(budget_module, "TimeBudget", ClockedBudget)
    monkeypatch.setattr(base_commands, "TimeBudget", ClockedBudget)
    wire["handler"] = lambda r: httpx.Response(
        200, json={"data": [{"url": "https://cdn.example/out.png"}]}
    )

    async def slow_download(url, proxy, *, timeout=60.0, max_bytes=None):
        clock_state["now"] += 300.0  # 下载期间宿主 RPC 已超时
        return PNG

    monkeypatch.setattr(base_commands, "download_image", slow_download)
    cmd = make_cmd()

    ok, reason, stop = await cmd.execute()

    assert not ok and stop and "时间预算耗尽" in reason
    assert cmd.ctx.hybrid_calls == []          # 图片一张都没有发出
