"""API 响应解析测试。

extract_all_image_data / extract_video_data / extract_text_failure_reason
要兼容 Gemini、OpenAI 兼容、豆包、以及各种"把图塞在文本里"的第三方中转，
是重构中最容易被无声改坏的部分。这里按各家真实响应形态逐一固定。
"""
import pytest

from gemini_drawer.utils import (
    extract_all_image_data,
    extract_text_failure_reason,
    extract_video_data,
    get_image_mime_type,
    redact_url,
    truncate_for_log,
)

B64 = "A" * 2000          # 足够长，不会被"疑似幻觉的短 base64"规则过滤
SHORT_B64 = "A" * 50


def openai(content):
    return {"choices": [{"message": {"content": content}}]}


# ── 图片提取 ─────────────────────────────────────────────────


async def test_gemini_inline_data():
    resp = {"candidates": [{"content": {"parts": [{"inlineData": {"data": B64}}]}}]}
    assert await extract_all_image_data(resp) == [B64]


async def test_gemini_snake_case_inline_data():
    resp = {"candidates": [{"content": {"parts": [{"inline_data": {"data": B64}}]}}]}
    assert await extract_all_image_data(resp) == [B64]


async def test_doubao_url_list():
    resp = {"data": [{"url": "https://cdn/a.png"}, {"url": "https://cdn/b.png"}]}
    assert await extract_all_image_data(resp) == ["https://cdn/a.png", "https://cdn/b.png"]


async def test_doubao_b64_json():
    assert await extract_all_image_data({"data": [{"b64_json": B64}]}) == [B64]


async def test_message_images_array():
    resp = {
        "choices": [
            {
                "message": {
                    "images": [
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{B64}"}},
                        {"url": "https://cdn/c.png"},
                    ]
                }
            }
        ]
    }
    assert await extract_all_image_data(resp) == [B64, "https://cdn/c.png"]


async def test_content_array_image_object():
    resp = openai(None)
    resp["choices"][0]["message"]["content"] = [{"type": "image", "image": {"data": B64}}]
    assert await extract_all_image_data(resp) == [B64]


async def test_markdown_multiple_images():
    resp = openai("here you go ![a](https://cdn/a.png) and ![b](https://cdn/b.png)")
    assert await extract_all_image_data(resp) == ["https://cdn/a.png", "https://cdn/b.png"]


async def test_markdown_data_url():
    assert await extract_all_image_data(openai(f"![x](data:image/png;base64,{B64})")) == [B64]


async def test_short_base64_in_markdown_is_ignored():
    """模型偶尔会编一段极短的 base64，不能当成真图发出去。"""
    assert await extract_all_image_data(openai(f"![x](data:image/png;base64,{SHORT_B64})")) == []


async def test_bare_url_with_image_suffix():
    resp = openai("图片在这里 https://cdn.example/out.jpeg?sig=abc 请查收")
    assert await extract_all_image_data(resp) == ["https://cdn.example/out.jpeg?sig=abc"]


async def test_bare_base64_without_data_prefix():
    """部分中转会丢掉 data: 前缀，只留 image/png;base64,..."""
    assert await extract_all_image_data(openai(f"image/png;base64,{B64}")) == [B64]


async def test_dashboard_url_is_skipped():
    """无后缀 URL 只能猜，至少要排掉明显是控制台/登录页的链接。"""
    assert await extract_all_image_data(openai("额度不足，请见 https://api.example/dashboard")) == []


async def test_delta_content_is_read():
    resp = {"choices": [{"delta": {"content": f"![x](data:image/png;base64,{B64})"}}]}
    assert await extract_all_image_data(resp) == [B64]


async def test_no_image_returns_empty_list():
    assert await extract_all_image_data(openai("抱歉，我无法生成这张图片。")) == []


async def test_malformed_response_does_not_raise():
    assert await extract_all_image_data({"choices": "not-a-list"}) == []


# ── 视频提取 ─────────────────────────────────────────────────


async def test_video_markdown_base64():
    assert await extract_video_data(openai(f"![v](data:video/mp4;base64,{B64})")) == B64


async def test_video_bare_mp4_url_marked_for_download():
    got = await extract_video_data(openai("https://cdn.example/clip.mp4?token=1"))
    assert got == "url:https://cdn.example/clip.mp4?token=1"


async def test_video_html_source_tag():
    resp = openai('<video><source src="https://cdn/clip.mp4" type="video/mp4"></video>')
    assert await extract_video_data(resp) == "url:https://cdn/clip.mp4"


async def test_video_gemini_inline_data():
    resp = {
        "candidates": [
            {"content": {"parts": [{"inlineData": {"mimeType": "video/mp4", "data": B64}}]}}
        ]
    }
    assert await extract_video_data(resp) == B64


async def test_video_ignores_image_inline_data():
    resp = {
        "candidates": [
            {"content": {"parts": [{"inlineData": {"mimeType": "image/png", "data": B64}}]}}
        ]
    }
    assert await extract_video_data(resp) is None


# ── 失败原因提取 ─────────────────────────────────────────────


def test_reason_from_gemini_prompt_feedback():
    reason = extract_text_failure_reason({"promptFeedback": {"blockReason": "SAFETY"}})
    assert "安全策略拦截" in reason and "SAFETY" in reason


def test_reason_from_openai_error_object():
    reason = extract_text_failure_reason({"error": {"message": "insufficient balance"}})
    assert reason == "insufficient balance"


def test_reason_from_model_refusal_text():
    resp = {"choices": [{"message": {"refusal": "I can't create that image."}}]}
    assert extract_text_failure_reason(resp) == "I can't create that image."


def test_reason_from_finish_reason():
    resp = {"choices": [{"message": {"content": ""}, "finish_reason": "content_filter"}]}
    assert "content_filter" in extract_text_failure_reason(resp)


def test_successful_finish_reason_is_not_reported_as_failure():
    resp = {"choices": [{"message": {"content": ""}, "finish_reason": "STOP"}]}
    assert extract_text_failure_reason(resp) == ""


def test_reason_from_content_filter_results():
    resp = {
        "choices": [
            {
                "message": {"content": ""},
                "content_filter_results": {"sexual": {"filtered": True, "severity": "high"}},
            }
        ]
    }
    reason = extract_text_failure_reason(resp)
    assert "内容过滤" in reason and "sexual" in reason


def test_reason_from_gemini_safety_ratings():
    resp = {
        "candidates": [
            {
                "finishReason": "SAFETY",
                "safetyRatings": [{"category": "HARM_CATEGORY_HATE", "probability": "HIGH"}],
            }
        ]
    }
    reason = extract_text_failure_reason(resp)
    assert "安全评级" in reason and "HARM_CATEGORY_HATE" in reason


def test_reason_falls_back_to_model_prose():
    resp = openai("我不太方便画这个内容哦。")
    assert extract_text_failure_reason(resp) == "我不太方便画这个内容哦。"


def test_reason_is_empty_when_nothing_to_report():
    assert extract_text_failure_reason({}) == ""


def test_reason_survives_garbage_input():
    assert extract_text_failure_reason({"error": object()}) == ""


# ── 小工具 ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        (b"\x89PNG\r\n\x1a\n....", "image/png"),
        (b"\xff\xd8\xff\xe0....", "image/jpeg"),
        (b"GIF89a....", "image/gif"),
        (b"RIFF????WEBPVP8 ", "image/webp"),
        (b"not-an-image", "application/octet-stream"),
    ],
)
def test_mime_sniffing(raw, expected):
    assert get_image_mime_type(raw) == expected


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://a.b/c:generateContent?key=AIzaSecret", "https://a.b/c:generateContent?key=***"),
        ("https://a.b/c?api_key=sk-1&size=2k", "https://a.b/c?api_key=***&size=2k"),
        ("https://a.b/c?endpoint=task_status&task_id=9", "https://a.b/c?endpoint=task_status&task_id=9"),
        ("https://a.b/chat/completions", "https://a.b/chat/completions"),
        ("", ""),
    ],
)
def test_redact_url(url, expected):
    assert redact_url(url) == expected


def test_truncate_for_log_keeps_short_strings():
    assert truncate_for_log("short") == "short"


def test_truncate_for_log_elides_long_strings():
    out = truncate_for_log("x" * 500, max_length=100)
    assert "truncated" in out and len(out) < 200
