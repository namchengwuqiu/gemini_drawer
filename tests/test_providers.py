"""Provider 适配层测试。

`build()` 的期望值全部逐字对照重构前的旧代码写出，作为等价性基线：
- 单图/文生图路径 → base_commands.py 旧 BaseDrawCommand.execute（副本 A）
- 多图路径        → base_commands.py 旧 BaseMultiImageDrawCommand.execute（副本 B）
- gpt-image 路径  → draw_logic.py 旧 process_drawing_api_request（副本 C）
"""
import base64

import pytest

from gemini_drawer.providers import (
    REGISTRY,
    DoubaoProvider,
    DrawRequest,
    Endpoint,
    GeminiProvider,
    GptImageProvider,
    OpenAICompatProvider,
    describe_protocol,
    resolve_provider,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"fake-png-bytes"
JPG = b"\xff\xd8" + b"fake-jpg-bytes"
PNG_B64 = base64.b64encode(PNG).decode()
JPG_B64 = base64.b64encode(JPG).decode()
PNG_URL = f"data:image/png;base64,{PNG_B64}"
JPG_URL = f"data:image/jpeg;base64,{JPG_B64}"

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/x:generateContent"
OPENAI_URL = "https://api.example.com/v1/chat/completions"
DOUBAO_URL = "https://ark.cn-beijing.volces.com/api/v3/images/generations"

SAFETY = [{"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"}]


def text_req(prompt="一只猫"):
    return DrawRequest(prompt=prompt)


def one_image_req(prompt="换成水彩风格"):
    return DrawRequest(prompt=prompt, images=[PNG], mime_types=["image/png"])


def two_image_req(prompt="融合这两张图"):
    return DrawRequest(
        prompt=prompt, images=[PNG, JPG], mime_types=["image/png", "image/jpeg"]
    )


# ── 注册表顺序 ────────────────────────────────────────────────


def test_gpt_image_wins_over_openai_on_chat_completions_url():
    """gpt-image 渠道的 URL 通常就是 /v1/chat/completions，
    若注册表顺序写反会被 OpenAI provider 抢走并发到错误端点。"""
    ep = Endpoint(type="custom_x", url=OPENAI_URL, key="sk-1", model="gpt-image-2")
    assert isinstance(resolve_provider(ep), GptImageProvider)


def test_registry_order_is_load_bearing():
    assert REGISTRY.index(GptImageProvider) < REGISTRY.index(OpenAICompatProvider)


@pytest.mark.parametrize(
    "endpoint,expected",
    [
        (Endpoint(type="custom_g", url=GEMINI_URL, key="k"), GeminiProvider),
        (Endpoint(type="custom_o", url=OPENAI_URL, key="k", model="gpt-4o"), OpenAICompatProvider),
        (Endpoint(type="lmarena", url="http://127.0.0.1:666/v1", key=""), OpenAICompatProvider),
        (Endpoint(type="custom_d", url=DOUBAO_URL, key="k", model="doubao-x"), DoubaoProvider),
        (Endpoint(type="custom_x", url=OPENAI_URL, key="k", model="GPT-Image-2"), GptImageProvider),
    ],
)
def test_resolve_provider(endpoint, expected):
    assert isinstance(resolve_provider(endpoint), expected)


def test_unknown_endpoint_resolves_to_none():
    assert resolve_provider(Endpoint(type="custom_x", url="https://a.b/unknown", key="k")) is None


def test_resolve_accepts_plain_dict():
    assert isinstance(resolve_provider({"type": "custom_g", "url": GEMINI_URL, "key": "k"}), GeminiProvider)


# ── Gemini ───────────────────────────────────────────────────


def test_gemini_text_only():
    ep = Endpoint(type="custom_g", url=GEMINI_URL, key="AIza-secret")
    call = GeminiProvider().build(ep, text_req())

    assert call.url == f"{GEMINI_URL}?key=AIza-secret"
    assert call.headers == {"Content-Type": "application/json"}
    assert call.json == {"contents": [{"parts": [{"text": "一只猫"}]}], "safetySettings": SAFETY}
    assert call.stream is False
    assert "AIza-secret" not in call.safe_url  # 日志脱敏


def test_gemini_single_image_puts_image_before_text():
    ep = Endpoint(type="custom_g", url=GEMINI_URL, key="k")
    call = GeminiProvider().build(ep, one_image_req())

    assert call.json["contents"][0]["parts"] == [
        {"inline_data": {"mime_type": "image/png", "data": PNG_B64}},
        {"text": "换成水彩风格"},
    ]


def test_gemini_multi_image_labels_each_image():
    ep = Endpoint(type="custom_g", url=GEMINI_URL, key="k")
    call = GeminiProvider().build(ep, two_image_req())

    assert call.json["contents"][0]["parts"] == [
        {"text": "Image 1:"},
        {"inline_data": {"mime_type": "image/png", "data": PNG_B64}},
        {"text": "Image 2:"},
        {"inline_data": {"mime_type": "image/jpeg", "data": JPG_B64}},
        {"text": "Prompt: 融合这两张图"},
    ]


def test_gemini_stream_flag_follows_channel():
    ep = Endpoint(type="custom_g", url=GEMINI_URL, key="k", stream=True)
    assert GeminiProvider().build(ep, text_req()).stream is True


# ── OpenAI 兼容 ──────────────────────────────────────────────


def test_openai_single_image():
    ep = Endpoint(type="custom_o", url=OPENAI_URL, key="sk-1", model="gemini-2.5-flash")
    call = OpenAICompatProvider().build(ep, one_image_req())

    assert call.url == OPENAI_URL
    assert call.headers["Authorization"] == "Bearer sk-1"
    assert call.json == {
        "model": "gemini-2.5-flash",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "换成水彩风格"},
                    {"type": "image_url", "image_url": {"url": PNG_URL}},
                ],
            }
        ],
        "stream": False,
    }
    assert call.use_proxy is True


def test_openai_multi_image_interleaves_labels():
    ep = Endpoint(type="custom_o", url=OPENAI_URL, key="sk-1", model="m")
    content = OpenAICompatProvider().build(ep, two_image_req()).json["messages"][0]["content"]

    assert content == [
        {"type": "text", "text": "Prompt: 融合这两张图"},
        {"type": "text", "text": "Image 1:"},
        {"type": "image_url", "image_url": {"url": PNG_URL}},
        {"type": "text", "text": "Image 2:"},
        {"type": "image_url", "image_url": {"url": JPG_URL}},
    ]


def test_openai_falls_back_to_default_model():
    ep = Endpoint(type="custom_o", url=OPENAI_URL, key="sk-1")
    assert OpenAICompatProvider().build(ep, text_req()).json["model"] == "gemini-pro-vision"


def test_lmarena_bypasses_proxy_and_omits_auth_when_keyless():
    ep = Endpoint(type="lmarena", url="http://127.0.0.1:666/v1", key="")
    call = OpenAICompatProvider().build(ep, text_req())

    assert call.use_proxy is False
    assert "Authorization" not in call.headers


# ── 豆包 ─────────────────────────────────────────────────────


def test_doubao_single_image_sends_string():
    ep = Endpoint(type="custom_d", url=DOUBAO_URL, key="ark-1", model="doubao-seedream-4-5-251128")
    call = DoubaoProvider().build(ep, one_image_req())

    assert call.headers["Authorization"] == "Bearer ark-1"
    assert call.json == {
        "model": "doubao-seedream-4-5-251128",
        "prompt": "换成水彩风格",
        "response_format": "url",
        "size": "2k",
        "stream": False,
        "watermark": False,
        "image": PNG_URL,
    }


def test_doubao_multi_image_sends_list():
    ep = Endpoint(type="custom_d", url=DOUBAO_URL, key="ark-1")
    assert DoubaoProvider().build(ep, two_image_req()).json["image"] == [PNG_URL, JPG_URL]


def test_doubao_text_only_omits_image():
    ep = Endpoint(type="custom_d", url=DOUBAO_URL, key="ark-1")
    assert "image" not in DoubaoProvider().build(ep, text_req()).json


def test_doubao_ignores_stream_flag():
    """豆包图片接口不支持本插件的 SSE 解析路径，渠道开了流式也必须忽略。"""
    ep = Endpoint(type="custom_d", url=DOUBAO_URL, key="ark-1", stream=True)
    assert DoubaoProvider().build(ep, text_req()).stream is False


def test_doubao_response_format_defaults_to_url():
    ep = Endpoint(type="custom_d", url=DOUBAO_URL, key="ark-1")
    assert DoubaoProvider().build(ep, text_req()).json["response_format"] == "url"


def test_doubao_response_format_can_be_overridden_per_channel():
    """非豆包的 /images/generations 中转（如 grok-imagine）只接受 b64_json，
    否则 400 private_media_required；渠道级覆盖必须原样进 payload。"""
    ep = Endpoint(
        type="custom_gpt_image2",
        url="https://welfare.example/v1/images/generations",
        key="sk-1",
        model="grok-imagine-image-2.0",
        response_format="b64_json",
    )
    assert isinstance(resolve_provider(ep), DoubaoProvider)
    call = DoubaoProvider().build(ep, one_image_req())
    assert call.json["response_format"] == "b64_json"
    assert call.json["model"] == "grok-imagine-image-2.0"
    assert call.json["image"] == PNG_URL


# ── 显式协议 ──────────────────────────────────────────────────


def test_explicit_protocol_overrides_detection():
    """写了 protocol 就不再猜：/images/generations 地址也能强制走 gpt_image；chat 地址也能强制走豆包协议。"""
    ep = Endpoint(type="custom_x", url=DOUBAO_URL, key="k", model="whatever", protocol="gpt_image")
    assert isinstance(resolve_provider(ep), GptImageProvider)
    ep = Endpoint(type="custom_x", url=OPENAI_URL, key="k", model="whatever", protocol="images_json")
    assert isinstance(resolve_provider(ep), DoubaoProvider)


def test_explicit_protocol_accepts_aliases():
    ep = Endpoint(type="custom_x", url="https://a.b/anything", key="k", protocol="Doubao")
    assert isinstance(resolve_provider(ep), DoubaoProvider)
    ep = Endpoint(type="custom_x", url="https://a.b/anything", key="k", protocol="openai")
    assert isinstance(resolve_provider(ep), OpenAICompatProvider)


def test_unknown_protocol_falls_back_to_detection():
    ep = Endpoint(type="custom_x", url=OPENAI_URL, key="k", model="gpt-image-2", protocol="nonsense")
    assert isinstance(resolve_provider(ep), GptImageProvider)
    assert describe_protocol(ep) == ("gpt_image", False)


def test_describe_protocol_reports_source():
    assert describe_protocol(Endpoint(type="custom_x", url=DOUBAO_URL, key="k")) == ("images_json", False)
    assert describe_protocol(Endpoint(type="custom_x", url=DOUBAO_URL, key="k", protocol="images_json")) == ("images_json", True)
    assert describe_protocol(Endpoint(type="custom_x", url="https://a.b/unknown", key="k")) == (None, False)
    assert describe_protocol({"type": "custom_x", "url": "https://a.b/unknown", "protocol": "gemini"}) == ("gemini", True)


def test_every_provider_declares_a_known_protocol():
    from gemini_drawer.core.protocols import IMAGE_PROTOCOLS
    assert {cls.protocol for cls in REGISTRY} == set(IMAGE_PROTOCOLS)
    assert len({cls.protocol for cls in REGISTRY}) == len(REGISTRY)


def test_endpoint_from_dict_reads_response_format():
    assert Endpoint.from_dict({"type": "custom_x", "url": DOUBAO_URL}).response_format is None
    assert Endpoint.from_dict({"type": "custom_x", "url": DOUBAO_URL, "response_format": ""}).response_format is None
    assert Endpoint.from_dict(
        {"type": "custom_x", "url": DOUBAO_URL, "response_format": "b64_json"}
    ).response_format == "b64_json"


# ── gpt-image ────────────────────────────────────────────────
#
# 同一协议两种形态，按渠道 URL 分流：带 /chat/completions 走 chat，
# 其余（图片端点/base）走 images。

GPT_IMAGE_IMAGES_URL = "https://api.example.com/v1/images/generations"


def test_gpt_image_images_url_text_uses_generations_endpoint():
    """直接给图片端点也要能拼对，不能再补一遍 /v1/images/generations。"""
    ep = Endpoint(type="custom_x", url=GPT_IMAGE_IMAGES_URL, key="sk-1", model="gpt-image-2")
    call = GptImageProvider().build(ep, text_req())

    assert call.url == "https://api.example.com/v1/images/generations"
    assert call.json == {"model": "gpt-image-2", "prompt": "一只猫", "size": "auto"}
    assert call.files is None


def test_gpt_image_images_url_with_image_uses_multipart_edits():
    ep = Endpoint(type="custom_x", url=GPT_IMAGE_IMAGES_URL, key="sk-1", model="gpt-image-2")
    call = GptImageProvider().build(ep, one_image_req())

    assert call.url == "https://api.example.com/v1/images/edits"
    assert call.json is None
    assert call.data == {"model": "gpt-image-2", "prompt": "换成水彩风格", "size": "auto"}
    filename, fileobj, mime = call.files["image"]
    assert (filename, mime) == ("input.png", "image/png")
    assert fileobj.read() == PNG
    # multipart 必须让 httpx 自己生成 boundary
    assert "Content-Type" not in call.headers


def test_gpt_image_jpeg_extension():
    ep = Endpoint(type="custom_x", url=GPT_IMAGE_IMAGES_URL, key="sk-1", model="gpt-image-2")
    req = DrawRequest(prompt="p", images=[JPG], mime_types=["image/jpeg"])
    assert GptImageProvider().build(ep, req).files["image"][0] == "input.jpg"


def test_gpt_image_chat_url_stays_on_chat_completions():
    """本地 web2api 桥只暴露 chat 端点：地址带 /chat/completions 时必须原样发过去，
    不能再改写成 /v1/images/generations（那正是这类桥没有的端点）。"""
    ep = Endpoint(
        type="custom_x",
        url="http://host.docker.internal:4399/v1/chat/completions",
        key="sk-1",
        model="gpt-image-2",
    )
    call = GptImageProvider().build(ep, text_req())

    assert call.url == "http://host.docker.internal:4399/v1/chat/completions"
    assert call.files is None
    assert call.json["model"] == "gpt-image-2"
    assert call.json["messages"] == [{"role": "user", "content": [{"type": "text", "text": "一只猫"}]}]
    assert call.json["stream"] is False


def test_gpt_image_chat_url_sends_image_as_data_uri():
    ep = Endpoint(type="custom_x", url=OPENAI_URL, key="sk-1", model="gpt-image-2")
    call = GptImageProvider().build(ep, one_image_req())

    assert call.url == OPENAI_URL
    assert call.files is None
    content = call.json["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "换成水彩风格"}
    assert content[1] == {"type": "image_url", "image_url": {"url": PNG_URL}}


def test_gpt_image_chat_url_multi_image_matches_openai_compat_payload():
    """chat 形态与 OpenAI 兼容协议必须发出逐字一致的请求体。"""
    ep = Endpoint(type="custom_x", url=OPENAI_URL, key="sk-1", model="gpt-image-2")
    req = two_image_req()

    gpt_call = GptImageProvider().build(ep, req)
    openai_call = OpenAICompatProvider().build(ep, req)

    assert gpt_call.url == openai_call.url
    assert gpt_call.json == openai_call.json
    labels = [part["text"] for part in gpt_call.json["messages"][0]["content"] if part["type"] == "text"]
    assert labels == ["Prompt: 融合这两张图", "Image 1:", "Image 2:"]


def test_gpt_image_chat_url_preserves_relay_prefix():
    """反向代理保留前置路径（/relay/v1/chat/completions）时不能被剥掉。"""
    ep = Endpoint(type="custom_x", url="https://a.b/relay/v1/chat/completions", key="k", model="gpt-image-2")
    assert GptImageProvider().build(ep, text_req()).url == "https://a.b/relay/v1/chat/completions"


def test_gpt_image_chat_url_honours_stream_flag():
    ep = Endpoint(type="custom_x", url=OPENAI_URL, key="k", model="gpt-image-2", stream=True)
    call = GptImageProvider().build(ep, text_req())

    assert call.stream is True
    assert call.json["stream"] is True


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://a.b", "https://a.b/v1/images/generations"),
        ("https://a.b/", "https://a.b/v1/images/generations"),
        ("https://a.b/v1", "https://a.b/v1/images/generations"),
        ("https://a.b/v1/images/generations", "https://a.b/v1/images/generations"),
        ("https://a.b/images/generations", "https://a.b/v1/images/generations"),
    ],
)
def test_gpt_image_images_base_normalization(url, expected):
    ep = Endpoint(type="custom_x", url=url, key="k", model="gpt-image-2")
    assert GptImageProvider().build(ep, text_req()).url == expected


def test_gpt_image_images_edits_url_maps_to_edits():
    ep = Endpoint(type="custom_x", url="https://a.b/v1/images/edits", key="k", model="gpt-image-2")
    assert GptImageProvider().build(ep, one_image_req()).url == "https://a.b/v1/images/edits"


def test_gpt_image_model_match_is_case_insensitive_and_prefix_agnostic():
    """中转站常给模型加命名空间前缀，例如 l0veyou/gpt-image-2-16-9。"""
    ep = Endpoint(type="custom_x", url=OPENAI_URL, key="k", model="l0veyou/GPT-Image-2-16-9")
    assert isinstance(resolve_provider(ep), GptImageProvider)
    assert GptImageProvider().build(ep, text_req()).json["model"] == "l0veyou/GPT-Image-2-16-9"


# ── DrawRequest 契约 ─────────────────────────────────────────


def test_draw_request_rejects_mismatched_mime_list():
    with pytest.raises(ValueError):
        DrawRequest(prompt="p", images=[PNG], mime_types=[])
