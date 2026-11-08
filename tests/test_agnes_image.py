"""Agnes Images API 的路由、请求格式与响应兼容性（不调用真实 API）。"""
import base64
import json
import logging

import httpx
import pytest

from gemini_drawer.providers import (
    AgnesImageProvider, DoubaoProvider, DrawRequest, Endpoint, REGISTRY, resolve_provider,
)

URL = "https://apihub.agnes-ai.com/v1/images/generations"
MODEL = "agnes-image-2.5-flash"
PNG = b"fake-png-image"
JPG = b"fake-jpeg-image"


def endpoint(url=URL, model=MODEL, stream=False, response_format=None):
    return Endpoint(
        type="custom_agnes", url=url, model=model, key="test-key", stream=stream,
        response_format=response_format,
    )


@pytest.mark.parametrize("url,model,expected", [
    (URL, MODEL, True),
    ("/v1/images/generations", MODEL, False),
    ("ftp://apihub.agnes-ai.com/v1/images/generations", MODEL, False),
    ("https://[invalid/v1/images/generations", MODEL, False),
    (URL + "/", MODEL, True),
    (URL + "?route=images", MODEL, True),
    (URL, None, True),
    (URL, "  " + MODEL + "  ", True),
    ("https://proxy.example/agnes/v1/images/generations", MODEL, True),
    ("https://proxy.example/v1/images/generations", None, False),
    ("https://apihub.agnes-ai.com.evil.example/v1/images/generations", None, False),
    (URL, "doubao-seedream-4-5", False),
    ("https://proxy.example/v1/chat/completions", MODEL, False),
    ("https://apihub.agnes-ai.com/v1/videos", MODEL, False),
])
def test_agnes_image_matching(url, model, expected):
    assert AgnesImageProvider.matches(endpoint(url, model)) is expected


def test_agnes_image_wins_over_generic_images_provider():
    assert REGISTRY.index(AgnesImageProvider) < REGISTRY.index(DoubaoProvider)
    assert isinstance(resolve_provider(endpoint()), AgnesImageProvider)
    assert isinstance(resolve_provider(endpoint(model="doubao-seedream-4-5")), DoubaoProvider)


def test_agnes_text_image_request_has_only_documented_fields():
    call = AgnesImageProvider().build(endpoint(), DrawRequest(prompt="一只猫"))
    assert call.url == URL
    assert call.headers == {"Content-Type": "application/json", "Authorization": "Bearer test-key"}
    assert call.json == {
        "model": MODEL, "prompt": "一只猫", "size": "2K", "ratio": "1:1",
        "extra_body": {"response_format": "url"},
    }
    assert call.timeout < 300  # 不能超过宿主图片组件的整体超时
    assert not call.stream


@pytest.mark.parametrize("images,mimes", [
    ([PNG], ["image/png"]),
    ([PNG, JPG], ["image/png", "image/jpeg"]),
])
def test_agnes_reference_images_are_nested_data_uri_arrays(images, mimes):
    call = AgnesImageProvider().build(
        endpoint(), DrawRequest(prompt="融合参考图", images=images, mime_types=mimes),
    )
    assert call.json["extra_body"] == {
        "response_format": "url",
        "image": [
            f"data:{mime};base64,{base64.b64encode(image).decode()}"
            for image, mime in zip(images, mimes)
        ],
    }
    assert "image" not in call.json
    assert "response_format" not in call.json
    assert call.files is None


def test_agnes_defaults_and_stream_setting():
    provider = AgnesImageProvider()
    call = provider.build(endpoint(model=None, stream=True), DrawRequest(prompt="p"))
    assert call.json["model"] == MODEL
    assert "stream" not in call.json
    assert not call.stream
    assert not provider.wants_stream(endpoint(stream=True))


@pytest.mark.asyncio
@pytest.mark.parametrize("response_format,data,result", [
    ("url", {"url": "https://cdn.example/image.png", "b64_json": None}, "https://cdn.example/image.png"),
    ("b64_json", {"url": None, "b64_json": "aW1hZ2U="}, "aW1hZ2U="),
])
@pytest.mark.parametrize("has_reference", [False, True])
async def test_agnes_image_requests_and_extracts_selected_format(response_format, data, result, has_reference):
    ep = endpoint(response_format=response_format)
    provider = resolve_provider(ep)
    request = DrawRequest(prompt="p", images=[PNG], mime_types=["image/png"]) if has_reference else DrawRequest(prompt="p")
    call = provider.build(ep, request)
    requests = []

    def handler(request):
        requests.append(request)
        body = json.loads(request.content)
        assert body["extra_body"]["response_format"] == response_format
        assert "response_format" not in body
        if response_format == "b64_json" and not has_reference:
            assert body["return_base64"] is True
        else:
            assert "return_base64" not in body
        return httpx.Response(200, json={"created": 1780000000, "data": [data]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await provider.fetch(call, client, logging.getLogger(__name__)) == [result]
    assert len(requests) == 1
