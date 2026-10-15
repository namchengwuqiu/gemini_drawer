"""下载地址策略与资源上限的回归测试（P1-7 / P1-8）。"""
import base64
import io

import httpx
import pytest
from PIL import Image

from gemini_drawer.core import image_source
from gemini_drawer.utils import limits, net
from gemini_drawer.utils.image import convert_if_gif, download_image


@pytest.fixture(autouse=True)
def reset_download_policy():
    net.reset_policy()
    yield
    net.reset_policy()


def patch_client(monkeypatch, handler):
    """把 httpx.AsyncClient 换成挂 MockTransport 的客户端（net 与 image 共用同一模块）。"""
    real_client = httpx.AsyncClient

    def factory(**kwargs):
        kwargs.pop("proxy", None)
        kwargs.pop("transport", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


# ── 地址策略 ────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8080/x.png",
    "http://10.0.0.5/x.png",
    "http://192.168.1.20/x.png",
    "http://169.254.169.254/latest/meta-data",
    "http://[::1]/x.png",
])
async def test_literal_private_addresses_are_rejected(url):
    with pytest.raises(net.DownloadNotAllowedError):
        await net.ensure_url_allowed(url)


@pytest.mark.asyncio
async def test_public_literal_address_passes():
    await net.ensure_url_allowed("https://8.8.8.8/x.png")


@pytest.mark.asyncio
async def test_non_http_scheme_is_rejected():
    with pytest.raises(net.DownloadNotAllowedError):
        await net.ensure_url_allowed("file:///etc/passwd")


@pytest.mark.asyncio
async def test_hostname_resolving_to_private_is_rejected(monkeypatch):
    monkeypatch.setattr(net, "_resolve_host", lambda host: ("192.168.1.10",))
    with pytest.raises(net.DownloadNotAllowedError):
        await net.ensure_url_allowed("https://internal.example/x.png")


@pytest.mark.asyncio
async def test_unresolvable_host_is_rejected(monkeypatch):
    """无法在本地解析就不能验证目的地址，不能默认当作安全（F9）。"""
    monkeypatch.setattr(net, "_resolve_host", lambda host: ())
    with pytest.raises(net.DownloadNotAllowedError):
        await net.ensure_url_allowed("https://not-resolvable.invalid/x.png")


@pytest.mark.asyncio
async def test_proxy_fake_ip_requires_explicit_allowlist(monkeypatch):
    """Clash/Surge fake-ip 占位地址只在确实配置了代理时放行（F9）。"""
    monkeypatch.setattr(net, "_resolve_host", lambda host: ("198.18.8.78",))
    with pytest.raises(net.DownloadNotAllowedError):
        await net.ensure_url_allowed("https://cdn.example/x.png", via_proxy=True)
    await net.ensure_url_allowed("https://cdn.example/x.png", via_proxy=True,
                                 policy=net.DownloadPolicy(host_allowlist=("cdn.example",)))
    with pytest.raises(net.DownloadNotAllowedError):
        await net.ensure_url_allowed("https://cdn.example/x.png")


@pytest.mark.asyncio
async def test_admin_config_can_allow_private_hosts():
    net.configure_policy(allow_private_hosts=True, host_allowlist=())
    await net.ensure_url_allowed("http://127.0.0.1:4399/x.png")


@pytest.mark.asyncio
async def test_allowlist_only_bypasses_private_check():
    net.configure_policy(allow_private_hosts=False, host_allowlist=["host.docker.internal"])
    await net.ensure_url_allowed("http://host.docker.internal:4399/x.png")
    with pytest.raises(net.DownloadNotAllowedError):
        await net.ensure_url_allowed("http://127.0.0.1:4399/x.png")


@pytest.mark.asyncio
async def test_redirect_hop_is_validated(monkeypatch):
    """重定向每一跳都要过地址策略（P1-7）。"""
    def handler(request):
        return httpx.Response(302, headers={"location": "http://127.0.0.1/secret.png"})

    patch_client(monkeypatch, handler)
    monkeypatch.setattr(net, "_resolve_host", lambda host: ("93.184.216.34",))

    with pytest.raises(net.DownloadNotAllowedError):
        await net.stream_download("https://public.example/x.png")


@pytest.mark.asyncio
async def test_redirect_to_other_origin_drops_auth_headers(monkeypatch):
    """跨 origin 重定向时认证头不能跟着发给其他主机（F4）。"""
    seen = []

    def handler(request):
        seen.append((str(request.url), dict(request.headers)))
        if request.url.host == "public.example":
            return httpx.Response(302, headers={"location": "https://other.example/out.png"})
        return httpx.Response(200, content=b"png-bytes", headers={"content-type": "image/png"})

    patch_client(monkeypatch, handler)
    monkeypatch.setattr(net, "_resolve_host", lambda host: ("93.184.216.34",))

    media = await net.stream_download(
        "https://public.example/x.png",
        headers={"Authorization": "Bearer DUMMY", "Cookie": "session=1", "Accept": "image/*"},
    )

    assert media.data == b"png-bytes"
    assert len(seen) == 2
    first_headers = {name.lower(): value for name, value in seen[0][1].items()}
    second_headers = {name.lower(): value for name, value in seen[1][1].items()}
    assert first_headers.get("authorization") == "Bearer DUMMY"
    assert "authorization" not in second_headers
    assert "cookie" not in second_headers
    assert second_headers.get("accept") == "image/*"


@pytest.mark.asyncio
async def test_https_to_http_downgrade_redirect_is_rejected(monkeypatch):
    """HTTPS → HTTP 的降级跳转直接拒绝（F4）。"""
    def handler(request):
        return httpx.Response(302, headers={"location": "http://plain.example/x.png"})

    patch_client(monkeypatch, handler)
    monkeypatch.setattr(net, "_resolve_host", lambda host: ("93.184.216.34",))

    with pytest.raises(net.DownloadNotAllowedError):
        await net.stream_download("https://public.example/x.png")


@pytest.mark.asyncio
async def test_peer_private_address_is_rejected_for_direct_downloads(monkeypatch):
    """解析结果与连接地址不一致（DNS 重绑定）时拒绝读取内容（F9）。"""
    class FakeStream:
        def get_extra_info(self, name):
            return ("192.168.1.10", 443) if name == "server_addr" else None

    real_client = httpx.AsyncClient

    def factory(**kwargs):
        kwargs.pop("proxy", None)
        kwargs.pop("transport", None)
        client = real_client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, content=b"png", headers={"content-type": "image/png"}
                )
            ),
            **kwargs,
        )
        original_stream = client.stream

        def patched_stream(*args, **kw):
            ctx = original_stream(*args, **kw)

            class Wrapper:
                async def __aenter__(self):
                    response = await ctx.__aenter__()
                    response.extensions["network_stream"] = FakeStream()
                    return response

                async def __aexit__(self, *exc):
                    return await ctx.__aexit__(*exc)

            return Wrapper()

        client.stream = patched_stream
        return client

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    monkeypatch.setattr(net, "_resolve_host", lambda host: ("93.184.216.34",))

    with pytest.raises(net.DownloadNotAllowedError):
        await net.stream_download("https://public.example/x.png")


@pytest.mark.asyncio
async def test_download_size_cap_counts_actual_bytes(monkeypatch):
    """大小限制按实际累计字节数生效，不信任 Content-Length（P1-8）。"""
    def handler(request):
        return httpx.Response(
            200, content=b"x" * 4096,
            headers={"content-type": "image/png", "content-length": "10"},
        )

    patch_client(monkeypatch, handler)
    monkeypatch.setattr(net, "_resolve_host", lambda host: ("93.184.216.34",))

    with pytest.raises(net.MediaTooLargeError):
        await net.stream_download("https://public.example/x.png", max_bytes=1024)


@pytest.mark.asyncio
async def test_download_image_rejects_private_and_returns_none():
    assert await download_image("http://127.0.0.1/x.png", None) is None


@pytest.mark.asyncio
async def test_download_image_returns_bytes_for_public_host(monkeypatch):
    png = b"\x89PNG\r\n\x1a\n" + b"p" * 32

    def handler(request):
        return httpx.Response(200, content=png, headers={"content-type": "image/png"})

    patch_client(monkeypatch, handler)
    monkeypatch.setattr(net, "_resolve_host", lambda host: ("93.184.216.34",))

    assert await download_image("https://public.example/x.png", None) == png


# ── 解码与数量上限 ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_oversized_inline_base64_is_rejected():
    seg = {"type": "image", "data": "", "binary_data_base64": "A" * (limits.MAX_INLINE_BASE64_CHARS + 1)}
    assert await image_source.extract_first_image_from_segments([seg], None, None) is None


@pytest.mark.asyncio
async def test_message_image_count_is_capped():
    payload = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"p" * 300).decode()
    segments = [{"type": "image", "data": payload} for _ in range(limits.MAX_MESSAGE_IMAGES + 5)]

    images = await image_source.extract_images_from_segments(segments, None, None)

    assert len(images) == limits.MAX_MESSAGE_IMAGES


def _tiny_gif() -> bytes:
    buffer = io.BytesIO()
    Image.new("P", (10, 10)).save(buffer, format="GIF")
    return buffer.getvalue()


def test_gif_conversion_skips_oversized_images(monkeypatch):
    gif = _tiny_gif()
    monkeypatch.setattr("gemini_drawer.utils.image.MAX_GIF_PIXELS", 50)

    # 超过像素上限时原样返回，不进行解码/转换
    assert convert_if_gif(gif) == gif


def test_gif_conversion_works_within_limit():
    gif = _tiny_gif()
    converted = convert_if_gif(gif)
    assert converted != gif
    assert converted.startswith(b"\x89PNG\r\n\x1a\n")
