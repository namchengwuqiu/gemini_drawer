"""HTTPX 下载 transport：在 TCP 连接前验证 DNS，固定 IP，保留原始 Host/TLS SNI。"""
import asyncio
import ipaddress
import ssl
from contextlib import contextmanager

import httpcore
import httpx


@contextmanager
def _http_errors():
    try:
        yield
    except Exception as exc:
        for name in ("ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout",
                     "ConnectError", "ReadError", "WriteError", "RemoteProtocolError",
                     "LocalProtocolError", "ProxyError", "UnsupportedProtocol"):
            if isinstance(exc, getattr(httpcore, name)):
                raise getattr(httpx, name)(str(exc)) from exc
        raise


class ValidatedBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, policy, backend=None):
        self.policy = policy
        self.backend = backend or httpcore.AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        from .net import DownloadNotAllowedError, resolve_addresses, _is_private_address

        allowed = self.policy.allow_private_hosts or self.policy.allows_host(host)
        # 显式允许的主机也解析一次再连接 IP，TLS SNI 仍由 httpcore 使用原始 host。
        async with asyncio.timeout(timeout):
            addresses = await resolve_addresses(host)
            if not allowed and any(_is_private_address(ip) for ip in addresses):
                raise DownloadNotAllowedError("媒体主机解析到受限地址，未建立下载连接")
            last_error = None
            for address in addresses:
                try:
                    stream = await self.backend.connect_tcp(
                        address, port, timeout=timeout, local_address=local_address,
                        socket_options=socket_options,
                    )
                except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                    last_error = exc
                    continue
                try:
                    peer = stream.get_extra_info("server_addr")
                    if not allowed and (not peer or _is_private_address(str(peer[0]))
                                        or ipaddress.ip_address(str(peer[0])) != ipaddress.ip_address(address)):
                        raise DownloadNotAllowedError("下载连接地址未通过验证，未发送 HTTP 请求")
                except BaseException:
                    await stream.aclose()
                    raise
                return stream
            raise last_error or DownloadNotAllowedError("媒体主机没有可连接的地址")


class _ResponseStream(httpx.AsyncByteStream):
    def __init__(self, stream):
        self.stream = stream

    async def __aiter__(self):
        with _http_errors():
            async for chunk in self.stream:
                yield chunk

    async def aclose(self):
        await self.stream.aclose()


class ValidatedTransport(httpx.AsyncBaseTransport):
    def __init__(self, policy, *, backend=None):
        self.pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl.create_default_context(),
            network_backend=ValidatedBackend(policy, backend), retries=0,
        )

    async def handle_async_request(self, request):
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(scheme=request.url.raw_scheme, host=request.url.raw_host,
                             port=request.url.port, target=request.url.raw_path),
            headers=request.headers.raw, content=request.stream, extensions=request.extensions,
        )
        with _http_errors():
            response = await self.pool.handle_async_request(core_request)
        return httpx.Response(response.status, headers=response.headers,
                              stream=_ResponseStream(response.stream), extensions=response.extensions)

    async def aclose(self):
        await self.pool.aclose()
