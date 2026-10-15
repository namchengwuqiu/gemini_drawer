"""下载目标地址策略与受限下载。

下载来源（消息段 URL、上游返回的结果 URL）都可能指向内网、回环或链路本地
地址；这里统一校验 scheme、主机与 DNS 解析结果，并在重定向的每一跳重复
校验，下载过程按实际累计字节数限制大小。

管理员可以通过 ``behavior.allow_private_media_hosts`` 与
``behavior.media_host_allowlist`` 显式放行内网媒体主机（本地绘图渠道需要）。

重定向每一跳都按完整 origin 校验：跨 origin 时移除 Authorization/Cookie
等敏感头（凭据不能发给其他主机），HTTPS→HTTP 的降级跳转直接拒绝。

直连在连接前重新校验解析结果，固定已验证的 IP，并保留原始 Host/TLS SNI。
代理侧的实际目的地址无法本地验证，必须显式允许该媒体主机；仅配置代理或
解析为 fake-IP 不构成放行依据。allow_private_media_hosts 是管理员的全局放行。
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from typing import Dict, Iterable, NamedTuple, Optional, Tuple
from urllib.parse import SplitResult, urlsplit

import httpx

from .limits import MAX_IMAGE_BYTES, MediaTooLargeError as MediaTooLargeError
from .http import read_body_capped
from .log import logger, redact_request

#: 本地 DNS 解析的等待上限（秒）；超时按"无法解析"处理，避免拖住下载流程
DNS_RESOLVE_TIMEOUT_SECONDS = 5.0

#: 重定向跨 origin 时必须移除的敏感请求头（凭据不能发给其他主机）
_SENSITIVE_HEADER_NAMES = frozenset(
    {"authorization", "proxy-authorization", "cookie", "x-api-key", "referer"}
)


def _origin(parts: SplitResult) -> Tuple[str, str, int]:
    """URL 的完整 origin（scheme、host、port），用于判断重定向是否跨站。"""
    scheme = parts.scheme.lower()
    default_port = 443 if scheme == "https" else 80
    return (scheme, (parts.hostname or "").lower(), parts.port or default_port)


def _strip_sensitive_headers(headers: Dict[str, str]) -> Dict[str, str]:
    """移除认证头等敏感请求头，供跨 origin 重定向时使用。"""
    return {
        name: value
        for name, value in headers.items()
        if name.lower() not in _SENSITIVE_HEADER_NAMES
    }


class DownloadNotAllowedError(RuntimeError):
    """下载目标不符合地址策略。"""


class DownloadFailedError(RuntimeError):
    """下载失败（HTTP 状态异常、空内容或重定向过多）。"""


class DownloadedMedia(NamedTuple):
    """一次成功下载的结果。"""

    data: bytes
    content_type: str
    url: str


@dataclass(frozen=True)
class DownloadPolicy:
    """下载地址策略。"""

    #: 是否允许下载解析到内网/回环/链路本地地址的媒体
    allow_private_hosts: bool = False
    #: 显式允许的主机名或 IP（不受内网限制约束）
    host_allowlist: Tuple[str, ...] = ()

    def allows_host(self, host: str) -> bool:
        normalized = str(host or "").strip().lower()
        if not normalized:
            return False
        return normalized in {item.lower() for item in self.host_allowlist}


DEFAULT_POLICY = DownloadPolicy()
_active_policy: DownloadPolicy = DEFAULT_POLICY


def configure_policy(
    *, allow_private_hosts: bool, host_allowlist: Iterable[str] = ()
) -> DownloadPolicy:
    """由插件在加载/配置热更新时调用，更新全局下载策略。"""
    global _active_policy
    _active_policy = DownloadPolicy(
        allow_private_hosts=bool(allow_private_hosts),
        host_allowlist=tuple(
            str(item).strip() for item in (host_allowlist or ()) if str(item).strip()
        ),
    )
    return _active_policy


def current_policy() -> DownloadPolicy:
    return _active_policy


def reset_policy() -> None:
    """恢复默认策略（测试用）。"""
    global _active_policy
    _active_policy = DEFAULT_POLICY


def parse_http_url(url: str) -> SplitResult:
    """解析并校验 http(s) URL；失败抛出 DownloadNotAllowedError。"""
    try:
        parts = urlsplit(str(url or "").strip())
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname or parts.username or parts.password:
            raise ValueError("Invalid media URL")
        _ = parts.port
    except ValueError as exc:
        raise DownloadNotAllowedError("不是有效的无凭据 HTTP(S) 媒体地址") from exc
    return parts


def same_origin(first: str, second: str) -> bool:
    return _origin(parse_http_url(first)) == _origin(parse_http_url(second))


def _is_ip_literal(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def _is_private_address(ip_text: str) -> bool:
    try:
        address = ipaddress.ip_address(ip_text)
    except ValueError:
        return True  # 解析结果不可信，按受限处理
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return not address.is_global or address.is_multicast


def _resolve_host(host: str) -> Tuple[str, ...]:
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError):
        return ()
    return tuple({info[4][0] for info in infos})


async def resolve_addresses(host: str) -> Tuple[str, ...]:
    if _is_ip_literal(host):
        return (host,)
    try:
        addresses = await asyncio.wait_for(asyncio.to_thread(_resolve_host, host), DNS_RESOLVE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        addresses = ()
    if not addresses:
        raise DownloadNotAllowedError("媒体主机无法解析，不能验证下载目的地址")
    return addresses


async def ensure_url_allowed(
    url: str,
    *,
    policy: Optional[DownloadPolicy] = None,
    via_proxy: bool = False,
) -> SplitResult:
    """校验下载目标；不通过时抛出 DownloadNotAllowedError，通过时返回解析结果。

    Args:
        via_proxy: 本次下载是否经由代理发起。代理会自行解析 DNS，本地校验
            覆盖不到真实目的地址，因此代理场景只允许管理员显式授权的主机；fake-IP 本身不构成授权。
    """
    active = policy or _active_policy
    parts = parse_http_url(url)
    host = parts.hostname or ""
    if active.allows_host(host) or active.allow_private_hosts:
        return parts
    if via_proxy:
        raise DownloadNotAllowedError("代理媒体下载需要显式配置 media_host_allowlist；无法本地验证代理目的地址")
    addresses = await resolve_addresses(host)
    if any(_is_private_address(address) for address in addresses):
        raise DownloadNotAllowedError("媒体主机解析到非公网地址，已按策略拒绝下载")
    return parts


def _peer_address(response: httpx.Response) -> Optional[str]:
    """尽力读取实际连接的对端 IP；底层不提供该信息时返回 None。"""
    try:
        stream = (response.extensions or {}).get("network_stream")
        if stream is None:
            return None
        address = stream.get_extra_info("server_addr")
        if isinstance(address, tuple) and address:
            return str(address[0])
    except Exception:
        return None
    return None


async def stream_download(
    url: str,
    *,
    proxy: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 60.0,
    max_bytes: int = MAX_IMAGE_BYTES,
    policy: Optional[DownloadPolicy] = None,
    max_redirects: int = 5,
) -> DownloadedMedia:
    """带地址校验、逐跳重定向校验与累计大小限制的下载。

    重定向每一跳都按完整 origin（scheme、host、port）判断：跨 origin 时移除
    Authorization/Cookie 等敏感头，HTTPS→HTTP 的降级跳转直接拒绝（F4）。
    直连的 transport 在发送 HTTP 请求前验证并固定连接地址（F9）。

    Raises:
        DownloadNotAllowedError: 任一跳的目标地址不符合策略，或出现跨站降级。
        MediaTooLargeError: 实际累计字节数超过 ``max_bytes``。
        DownloadFailedError: HTTP 状态异常、空内容或重定向过多。
    """
    current = str(url or "").strip()
    via_proxy = bool(proxy)
    base_origin: Optional[Tuple[str, str, int]] = None
    previous_origin: Optional[Tuple[str, str, int]] = None
    active = policy or _active_policy
    current_headers: Dict[str, str] = dict(headers or {})
    stripped_origin_notice = False
    from .transport import ValidatedTransport
    transport = None if via_proxy else ValidatedTransport(active)
    async with asyncio.timeout(timeout), httpx.AsyncClient(
        proxy=proxy, transport=transport, timeout=timeout, follow_redirects=False, trust_env=False,
        headers={"Accept-Encoding": "identity"},
    ) as client:
        for _ in range(max_redirects + 1):
            parts = await ensure_url_allowed(current, policy=policy, via_proxy=via_proxy)
            origin = _origin(parts)
            if previous_origin and previous_origin[0] == "https" and origin[0] != "https":
                raise DownloadNotAllowedError("拒绝 HTTPS 到 HTTP 的媒体降级跳转")
            previous_origin = origin
            if base_origin is None:
                base_origin = origin
            elif origin != base_origin:
                if base_origin[0] == "https" and origin[0] != "https":
                    raise DownloadNotAllowedError(
                        "重定向到非 HTTPS 地址，已按策略拒绝"
                    )
                if not stripped_origin_notice:
                    stripped_origin_notice = True
                    logger.warning(
                        f"重定向到其他站点（{origin[1]}），已移除请求中的认证头等敏感信息"
                    )
                current_headers = _strip_sensitive_headers(current_headers)
            # 不让上一跳 Set-Cookie 绕过显式请求头的移除策略。
            client.cookies.clear()
            secrets = [value for name, value in current_headers.items() if name.lower() in _SENSITIVE_HEADER_NAMES]
            async with redact_request(current, *secrets), client.stream("GET", current, headers=current_headers) as response:
                if not via_proxy and not (active.allow_private_hosts or active.allows_host(parts.hostname or "")):
                    peer = _peer_address(response)
                    if peer and _is_private_address(peer):
                        raise DownloadNotAllowedError("下载连接的实际对端属于受限地址")
                if response.is_redirect:
                    location = response.headers.get("location", "")
                    if not location:
                        raise DownloadFailedError("重定向响应缺少 Location 头")
                    current = str(response.url.join(location))
                    continue
                if response.status_code != 200:
                    raise DownloadFailedError(f"HTTP 状态码 {response.status_code}")
                content_type = (
                    response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                )
                data = await read_body_capped(response, max_bytes, enforce=True)
                if not data:
                    raise DownloadFailedError("响应内容为空")
                return DownloadedMedia(data=data, content_type=content_type, url=current)
        raise DownloadFailedError(f"重定向次数超过上限（{max_redirects}）")
