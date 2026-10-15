"""生成接口共用的受限响应读取；包含错误体、轮询结果和 SSE 原始行。"""
import re
import zlib
from typing import AsyncIterator

import httpx

from . import limits
from .limits import MediaTooLargeError

_CHUNK_BYTES = 64 * 1024


async def _decoded_chunks(response: httpx.Response) -> AsyncIterator[bytes]:
    # 兼容已缓冲的响应（包括 MockTransport）；真实请求始终使用 stream=True。
    if response.is_stream_consumed:
        async for chunk in response.aiter_bytes(chunk_size=_CHUNK_BYTES):
            yield chunk
        return
    encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if encoding not in ("", "identity", "gzip", "deflate"):
        raise MediaTooLargeError("上游响应压缩方式不支持受限解码，已停止处理")
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS) if encoding in ("gzip", "deflate") else None
    try:
        async for chunk in response.aiter_raw(chunk_size=_CHUNK_BYTES):
            if decoder is None:
                yield chunk
                continue
            while chunk:
                decoded = decoder.decompress(chunk, _CHUNK_BYTES)
                chunk = decoder.unconsumed_tail
                if decoded:
                    yield decoded
                if decoder.unused_data:
                    raise httpx.DecodingError("压缩响应包含多余数据")
        if decoder is not None and not decoder.eof:
            raise httpx.DecodingError("压缩响应被截断")
    except zlib.error as exc:
        raise httpx.DecodingError("压缩响应无效") from exc


async def read_body_capped(response: httpx.Response, max_bytes: int, *, enforce: bool = False) -> bytes:
    data = bytearray()
    async for chunk in _decoded_chunks(response):
        available = max_bytes - len(data)
        if len(chunk) > available:
            if enforce:
                raise MediaTooLargeError(f"上游响应超过大小上限（{max_bytes} 字节），已中止读取")
            data.extend(chunk[:available])
            break
        data.extend(chunk)
    return bytes(data)


async def request_capped(client: httpx.AsyncClient, method: str, url: str, **kwargs) -> httpx.Response:
    async with client.stream(method, url, **kwargs) as response:
        maximum = limits.MAX_HTTP_RESPONSE_BYTES if response.is_success else limits.MAX_ERROR_BODY_BYTES
        body = await read_body_capped(response, maximum, enforce=response.is_success)
        # aiter_bytes 已解压；重新构造可 json()/text 的响应时不得再次解压。
        headers = [(k, v) for k, v in response.headers.multi_items()
                   if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")]
        return httpx.Response(response.status_code, headers=headers, content=body,
                              request=response.request, extensions=response.extensions)


async def iter_sse_lines(response: httpx.Response) -> AsyncIterator[str]:
    """在按行解析之前限制所有字节，包括注释、无关字段和未终止的行。"""
    pending = bytearray()
    total = 0
    skip_lf = False
    async for chunk in _decoded_chunks(response):
        total += len(chunk)
        if total > limits.MAX_SSE_TOTAL_BYTES:
            raise MediaTooLargeError("SSE 总响应超过大小上限，已中止读取")
        # 每次只扫描当前块；长行不会被反复从头扫描。
        start = 0
        for match in re.finditer(rb"[\r\n]", chunk):
            index = match.start()
            value = chunk[index]
            part = chunk[start:index]
            if len(pending) + len(part) > limits.MAX_SSE_LINE_BYTES:
                raise MediaTooLargeError("SSE 单行超过大小上限，已中止读取")
            pending.extend(part)
            if not (skip_lf and value == 10 and not pending):
                yield pending.decode("utf-8", "replace")
            pending.clear()
            skip_lf = value == 13
            start = index + 1
        tail = chunk[start:]
        if tail:
            skip_lf = False
        if len(pending) + len(tail) > limits.MAX_SSE_LINE_BYTES:
            raise MediaTooLargeError("SSE 单行超过大小上限，已中止读取")
        pending.extend(tail)
    if pending:
        yield pending.decode("utf-8", "replace")
