"""取图模块测试：消息段解析、@提及提取、以及取图的优先级顺序。"""
import base64

import pytest

from gemini_drawer.core import image_source as src

PNG = b"\x89PNG\r\n\x1a\n" + b"p" * 300
JPG = b"\xff\xd8" + b"j" * 300
PNG_B64 = base64.b64encode(PNG).decode()
JPG_B64 = base64.b64encode(JPG).decode()


class Seg:
    """模拟 CompatMessageSegment。"""
    def __init__(self, type_, data, binary_data_base64=None):
        self.type = type_
        self.data = data
        if binary_data_base64 is not None:
            self.binary_data_base64 = binary_data_base64


class SegList:
    type = "seglist"
    def __init__(self, segs):
        self.data = segs


class Msg:
    def __init__(self, **kw):
        self.__dict__.update(kw)


@pytest.fixture
def downloads(monkeypatch):
    """拦截图片下载，记录被请求的 URL。"""
    calls = []
    table = {}

    async def fake_download(url, proxy=None):
        calls.append(url)
        return table.get(url)

    monkeypatch.setattr(src, "download_image", fake_download)
    return {"calls": calls, "table": table}


# ── normalize_segments ───────────────────────────────────────


def test_normalize_unwraps_seglist():
    a, b = Seg("text", "x"), Seg("image", {})
    assert src.normalize_segments(SegList([a, b])) == [a, b]


def test_normalize_wraps_single_segment():
    a = Seg("text", "x")
    assert src.normalize_segments(a) == [a]


@pytest.mark.parametrize("value", [None, [], ""])
def test_normalize_handles_empty(value):
    assert src.normalize_segments(value) == []


def test_normalize_drops_falsy_members():
    a = Seg("text", "x")
    assert src.normalize_segments([a, None]) == [a]


# ── 图片提取 ─────────────────────────────────────────────────


async def test_extracts_inline_base64(downloads):
    segs = SegList([Seg("image", PNG_B64)])
    assert await src.extract_images_from_segments(segs) == [PNG]


async def test_extracts_binary_data_base64_field(downloads):
    segs = SegList([Seg("image", {}, binary_data_base64=PNG_B64)])
    assert await src.extract_images_from_segments(segs) == [PNG]


async def test_extracts_from_url(downloads):
    downloads["table"]["https://cdn/a.png"] = PNG
    segs = SegList([Seg("image", {"url": "https://cdn/a.png"})])
    assert await src.extract_images_from_segments(segs) == [PNG]


async def test_extracts_emoji_segments_too(downloads):
    segs = SegList([Seg("emoji", PNG_B64)])
    assert await src.extract_images_from_segments(segs) == [PNG]


async def test_extracts_dict_shaped_segments(downloads):
    segs = [{"type": "image", "data": PNG_B64}]
    assert await src.extract_images_from_segments(segs) == [PNG]


async def test_extracts_multiple_images_in_order(downloads):
    downloads["table"]["https://cdn/b.jpg"] = JPG
    segs = SegList([
        Seg("text", "看这两张"),
        Seg("image", PNG_B64),
        Seg("image", {"url": "https://cdn/b.jpg"}),
    ])
    assert await src.extract_images_from_segments(segs) == [PNG, JPG]


async def test_failed_download_does_not_abort_remaining(downloads):
    downloads["table"]["https://cdn/ok.png"] = PNG
    segs = SegList([
        Seg("image", {"url": "https://cdn/dead.png"}),   # 返回 None
        Seg("image", {"url": "https://cdn/ok.png"}),
    ])
    assert await src.extract_images_from_segments(segs) == [PNG]


async def test_short_string_is_not_treated_as_base64(downloads):
    assert await src.extract_images_from_segments(SegList([Seg("image", "abc")])) == []


async def test_undecodable_base64_is_skipped(downloads):
    segs = SegList([Seg("image", "!" * 300)])
    assert await src.extract_images_from_segments(segs) == []


async def test_first_image_helper_stops_at_first(downloads):
    segs = SegList([Seg("image", PNG_B64), Seg("image", JPG_B64)])
    assert await src.extract_first_image_from_segments(segs) == PNG


# ── @提及提取 ────────────────────────────────────────────────


@pytest.mark.parametrize("field", ["qq", "user_id", "id", "target_user_id"])
def test_at_segment_key_variants(field):
    assert src.extract_mentioned_user_ids(SegList([Seg("at", {field: 12345})])) == ["12345"]


def test_at_segment_plain_string():
    assert src.extract_mentioned_user_ids(SegList([Seg("at", "12345")])) == ["12345"]


def test_at_all_is_ignored():
    assert src.extract_mentioned_user_ids(SegList([Seg("at", "all")])) == []


def test_mentions_in_text_nickname_form():
    segs = SegList([Seg("text", "@<小明:12345> 画一张")])
    assert src.extract_mentioned_user_ids(segs) == ["12345"]


def test_mentions_in_text_bare_number():
    assert src.extract_mentioned_user_ids(SegList([Seg("text", "@1960408665 来")])) == ["1960408665"]


def test_mentions_are_deduped_preserving_order():
    segs = SegList([
        Seg("at", {"qq": "111111"}),
        Seg("text", "@222222 @111111"),
    ])
    assert src.extract_mentioned_user_ids(segs) == ["111111", "222222"]


def test_text_without_at_is_cheap_noop():
    assert src.extract_mentioned_user_ids(SegList([Seg("text", "普通消息")])) == []


def test_mentions_from_plain_text_helper():
    assert src.extract_mentioned_ids_from_text("@<甲:111111> 和 @222222") == ["111111", "222222"]
    assert src.extract_mentioned_ids_from_text("") == []


# ── extract_source_image 优先级 ──────────────────────────────


async def test_current_message_image_preferred_over_at_avatar(downloads):
    downloads["table"]["https://q1.qlogo.cn/g?b=qq&nk=12345&s=640"] = JPG
    msg = Msg(message_segment=SegList([Seg("at", {"qq": "12345"}), Seg("image", PNG_B64)]))

    assert await src.extract_source_image(msg) == PNG
    assert downloads["calls"] == []   # 没必要去下头像


async def test_falls_back_to_at_user_avatar(downloads):
    avatar_url = "https://q1.qlogo.cn/g?b=qq&nk=12345&s=640"
    downloads["table"][avatar_url] = JPG
    msg = Msg(message_segment=SegList([Seg("at", {"qq": "12345"}), Seg("text", "画一张")]))

    assert await src.extract_source_image(msg) == JPG
    assert downloads["calls"] == [avatar_url]


async def test_reply_image_wins_over_current_message(downloads):
    reply = Msg(message_segment=SegList([Seg("image", JPG_B64)]))
    msg = Msg(message_segment=SegList([Seg("image", PNG_B64)]), reply=reply)

    assert await src.extract_source_image(msg) == JPG


async def test_returns_none_when_nothing_available(downloads):
    msg = Msg(message_segment=SegList([Seg("text", "随便聊聊")]))
    assert await src.extract_source_image(msg) is None


async def test_avatar_fallback_from_plain_text_mention(downloads):
    """数据库来的消息没有结构化消息段，只能从纯文本里找 @。"""
    avatar_url = "https://q1.qlogo.cn/g?b=qq&nk=999999&s=640"
    downloads["table"][avatar_url] = PNG
    msg = Msg(processed_plain_text="@<某人:999999> 画一张")

    assert await src.extract_source_image(msg) == PNG


async def test_broken_message_object_does_not_raise(downloads):
    class Exploding:
        @property
        def message_segment(self):
            raise RuntimeError("boom")

    assert await src.extract_source_image(Exploding()) is None


async def test_download_avatar_builds_expected_url(downloads):
    downloads["table"]["https://q1.qlogo.cn/g?b=qq&nk=42&s=640"] = PNG
    assert await src.download_avatar(42) == PNG


# ── 回复链场景（回归） ───────────────────────────────────────
#
# 真实场景：娜娜 发了「@sakura桜花 [图片]」，用户回复这条消息并发
# 「@娜娜 /bnn 亲吻在一起」。想要的是娜娜发的那张图。
#
# CompatMessage 构造 reply 时只带 target_message_content 这段纯文本
# （渲染成 "@<sakura桜花:...> [图片]"），没有消息段。所以递归查 reply
# 时若允许头像兜底，就会命中文本里的 @，误取 sakura桜花 的头像，
# 并且短路掉真正能拿到原图的 message capability 查询。


NANA_ID = "111111111"
SAKURA_ID = "222222222"
NANA_AVATAR = "https://q1.qlogo.cn/g?b=qq&nk=111111111&s=640"
SAKURA_AVATAR = "https://q1.qlogo.cn/g?b=qq&nk=222222222&s=640"
TARGET_MSG_ID = "msg-nana-with-image"

WANTED = b"\x89PNG\r\n\x1a\n" + b"the-image-nana-sent" * 20
WANTED_B64 = base64.b64encode(WANTED).decode()


class CapabilityCtx:
    """模拟 ctx.message.get_by_id：按 message_id 返回带图的真实消息。"""

    def __init__(self, table):
        self.table = table
        self.queried = []
        outer = self

        class MessageCap:
            async def get_by_id(self, message_id, stream_id=None, include_binary_data=False):
                outer.queried.append(message_id)
                return outer.table.get(message_id)

        self.message = MessageCap()


def reply_chain_message():
    """还原截图里的消息结构。"""
    reply = Msg(
        # 被回复消息只剩渲染后的纯文本，没有图片段
        message_segment=SegList([Seg("text", f"@<sakura桜花:{SAKURA_ID}> [图片]")]),
        message_id=TARGET_MSG_ID,
        processed_plain_text=f"@<sakura桜花:{SAKURA_ID}> [图片]",
    )
    return Msg(
        message_segment=SegList([
            Seg("at", {"qq": NANA_ID}),
            Seg("text", " /bnn 亲吻在一起"),
        ]),
        reply=reply,
        session_id="stream-1",
    )


async def test_reply_chain_fetches_original_image_not_an_avatar(downloads):
    downloads["table"][NANA_AVATAR] = b"nana-avatar"
    downloads["table"][SAKURA_AVATAR] = b"sakura-avatar"

    ctx = CapabilityCtx({
        TARGET_MSG_ID: {"message_segments": [{"type": "image", "data": WANTED_B64}]}
    })

    got = await src.extract_source_image(reply_chain_message(), ctx=ctx)

    assert got == WANTED, "应取回娜娜发的那张图"
    assert ctx.queried == [TARGET_MSG_ID]
    assert downloads["calls"] == [], "不应该去下任何人的头像"


async def test_reply_text_mention_never_yields_avatar(downloads):
    """即使 capability 拿不到消息，也不能退化成"取被回复内容里 @ 的人的头像"。"""
    downloads["table"][SAKURA_AVATAR] = b"sakura-avatar"
    downloads["table"][NANA_AVATAR] = b"nana-avatar"

    ctx = CapabilityCtx({})   # 查不到

    got = await src.extract_source_image(reply_chain_message(), ctx=ctx)

    # 兜底只允许落到当前消息 @ 的人（娜娜），绝不能是被回复正文里的 sakura桜花
    assert got == b"nana-avatar"
    assert SAKURA_AVATAR not in downloads["calls"]


async def test_reply_with_real_image_segments_still_works(downloads):
    """若适配器确实给了完整消息段，A 路径应直接命中，不必查 capability。"""
    reply = Msg(message_segment=SegList([
        Seg("text", f"@<sakura桜花:{SAKURA_ID}> 看这个"),
        Seg("image", WANTED_B64),
    ]), message_id=TARGET_MSG_ID)
    msg = Msg(message_segment=SegList([Seg("at", {"qq": NANA_ID})]), reply=reply)
    ctx = CapabilityCtx({})

    assert await src.extract_source_image(msg, ctx=ctx) == WANTED
    assert ctx.queried == []
    assert downloads["calls"] == []


async def test_avatar_fallback_can_be_disabled_explicitly(downloads):
    downloads["table"][NANA_AVATAR] = b"nana-avatar"
    msg = Msg(message_segment=SegList([Seg("at", {"qq": NANA_ID})]))

    assert await src.extract_source_image(msg, allow_avatar_fallback=False) is None
    assert downloads["calls"] == []


# ── 多图的回复解析 ───────────────────────────────────────────


IMG_A = b"\x89PNG\r\n\x1a\n" + b"a" * 300
IMG_B = b"\xff\xd8" + b"b" * 300


async def test_reply_images_come_from_capability_when_segments_are_text_only(downloads):
    """/多图 此前只读 reply 的消息段，回复带图时一张都取不到。"""
    ctx = CapabilityCtx({
        TARGET_MSG_ID: {"message_segments": [
            {"type": "text", "data": "两张图"},
            {"type": "image", "data": base64.b64encode(IMG_A).decode()},
            {"type": "image", "data": base64.b64encode(IMG_B).decode()},
        ]}
    })

    got = await src.resolve_reply_images(reply_chain_message(), ctx=ctx)

    assert got == [IMG_A, IMG_B]
    assert downloads["calls"] == []


async def test_reply_images_prefer_real_segments_over_capability(downloads):
    reply = Msg(
        message_segment=SegList([Seg("image", base64.b64encode(IMG_A).decode())]),
        message_id=TARGET_MSG_ID,
    )
    ctx = CapabilityCtx({TARGET_MSG_ID: {"message_segments": [
        {"type": "image", "data": base64.b64encode(IMG_B).decode()}
    ]}})

    got = await src.resolve_reply_images(Msg(message_segment=SegList([]), reply=reply), ctx=ctx)

    assert got == [IMG_A]
    assert ctx.queried == []


async def test_reply_images_never_fall_back_to_avatar(downloads):
    downloads["table"][SAKURA_AVATAR] = b"sakura-avatar"
    downloads["table"][NANA_AVATAR] = b"nana-avatar"

    got = await src.resolve_reply_images(reply_chain_message(), ctx=CapabilityCtx({}))

    assert got == []
    assert downloads["calls"] == []


async def test_reply_images_empty_without_reply(downloads):
    msg = Msg(message_segment=SegList([Seg("text", "/多图 融合")]))
    assert await src.resolve_reply_images(msg, ctx=CapabilityCtx({})) == []
