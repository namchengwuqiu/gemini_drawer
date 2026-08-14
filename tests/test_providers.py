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
    TsAiProvider,
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
TSAI_URL = "https://api.tavr.top/?endpoint=image"

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
    assert REGISTRY.index(TsAiProvider) == len(REGISTRY) - 1


@pytest.mark.parametrize(
    "endpoint,expected",
    [
        (Endpoint(type="custom_g", url=GEMINI_URL, key="k"), GeminiProvider),
        (Endpoint(type="custom_o", url=OPENAI_URL, key="k", model="gpt-4o"), OpenAICompatProvider),
        (Endpoint(type="lmarena", url="http://127.0.0.1:666/v1", key=""), OpenAICompatProvider),
        (Endpoint(type="custom_d", url=DOUBAO_URL, key="k", model="doubao-x"), DoubaoProvider),
        (Endpoint(type="custom_t", url=TSAI_URL, key="k"), TsAiProvider),
        (Endpoint(type="custom_tsart_a", url="https://whatever.example/api", key="k"), TsAiProvider),
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


# ── gpt-image ────────────────────────────────────────────────


def test_gpt_image_text_uses_generations_endpoint():
    ep = Endpoint(type="custom_x", url=OPENAI_URL, key="sk-1", model="gpt-image-2")
    call = GptImageProvider().build(ep, text_req())

    assert call.url == "https://api.example.com/v1/images/generations"
    assert call.json == {"model": "gpt-image-2", "prompt": "一只猫", "size": "auto"}
    assert call.files is None


def test_gpt_image_with_image_uses_multipart_edits():
    ep = Endpoint(type="custom_x", url=OPENAI_URL, key="sk-1", model="gpt-image-2")
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
    ep = Endpoint(type="custom_x", url=OPENAI_URL, key="sk-1", model="gpt-image-2")
    req = DrawRequest(prompt="p", images=[JPG], mime_types=["image/jpeg"])
    assert GptImageProvider().build(ep, req).files["image"][0] == "input.jpg"


def test_gpt_image_strips_chat_completions_suffix():
    ep = Endpoint(type="custom_x", url="https://a.b/chat/completions", key="k", model="gpt-image-2")
    assert GptImageProvider().build(ep, text_req()).url == "https://a.b/v1/images/generations"


# ── TS-AI ────────────────────────────────────────────────────


def test_tsai_text_only():
    ep = Endpoint(type="custom_tsart", url=TSAI_URL, key="ts-1")
    call = TsAiProvider().build(ep, text_req())

    assert call.url == "https://api.tavr.top/?endpoint=image_generation"
    assert call.headers["x-api-key"] == "ts-1"
    assert call.json == {"prompt": "一只猫", "workflow": "rr3", "seed": -1}


def test_tsai_with_image_switches_to_editing():
    ep = Endpoint(type="custom_tsart", url=TSAI_URL, key="ts-1", model="rr4")
    call = TsAiProvider().build(ep, one_image_req())

    assert call.url == "https://api.tavr.top/?endpoint=image_editing"
    assert call.json == {
        "prompt": "换成水彩风格",
        "workflow": "rr4",
        "seed": -1,
        "image": PNG_URL,
    }


def test_tsai_multi_image_uses_first_only():
    ep = Endpoint(type="custom_tsart", url=TSAI_URL, key="ts-1")
    assert TsAiProvider().build(ep, two_image_req()).json["image"] == PNG_URL


# ── DrawRequest 契约 ─────────────────────────────────────────


def test_draw_request_rejects_mismatched_mime_list():
    with pytest.raises(ValueError):
        DrawRequest(prompt="p", images=[PNG], mime_types=[])
