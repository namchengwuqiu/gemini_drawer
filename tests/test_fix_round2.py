"""2026-10-15 复核 R1–R9：观察副作用次数、截止取消和实际连接边界。"""
import asyncio
import base64
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpcore
import httpx
import pytest

from gemini_drawer.core import pipeline, video, budget as budget_module
from gemini_drawer.core.budget import TimeBudget
from gemini_drawer.core.channel_url import split_url_and_model
from gemini_drawer.providers import DrawRequest, Endpoint
from gemini_drawer.providers.gemini import GeminiProvider
from gemini_drawer.utils import limits, net
from gemini_drawer.utils.http import iter_sse_lines
from gemini_drawer.utils.http import read_body_capped
from gemini_drawer.utils.transport import ValidatedBackend, ValidatedTransport

LOGGER = logging.getLogger(__name__)
IMAGE = base64.b64encode(b'image' * 200).decode()
VIDEO = base64.b64encode(b'video' * 20).decode()


def endpoint(protocol, host='first.example', **extra):
    return dict(type='custom_' + protocol, protocol=protocol, url=f'https://{host}/api', key='DUMMY-KEY', **extra)


def image_ok():
    return {'candidates': [{'content': {'parts': [{'inlineData': {'data': IMAGE}}]}}]}


def video_ok(**extra):
    return {'choices': [{'message': {'content': 'data:video/mp4;base64,' + VIDEO}}], **extra}


@pytest.fixture
def wire(monkeypatch):
    real_client, real_sleep = httpx.AsyncClient, asyncio.sleep
    state = SimpleNamespace(requests=[], handler=None, records=[])

    def dispatch(request):
        state.requests.append(request)
        return state.handler(request)

    def factory(**kwargs):
        kwargs.pop('proxy', None)
        kwargs.pop('transport', None)
        kwargs['trust_env'] = False
        return real_client(transport=httpx.MockTransport(dispatch), **kwargs)

    async def fast_sleep(seconds):
        await real_sleep(0)

    keys = SimpleNamespace(record_key_usage=lambda *a, **k: state.records.append((a, k)))
    monkeypatch.setattr(httpx, 'AsyncClient', factory)
    monkeypatch.setattr(asyncio, 'sleep', fast_sleep)
    monkeypatch.setattr(pipeline, 'key_manager', keys)
    monkeypatch.setattr(video, 'key_manager', keys)
    return state


@pytest.fixture(autouse=True)
def reset_policy():
    net.reset_policy()
    yield
    net.reset_policy()


@pytest.mark.parametrize('recover', [False, True])
async def test_completed_video_without_url_never_recreates(wire, recover):
    polls = 0

    def handler(request):
        nonlocal polls
        if request.method == 'POST':
            return httpx.Response(200, json={'id': 'task-1'})
        polls += 1
        url = {'video_url': 'https://8.8.8.8/result.mp4'} if recover and polls == 2 else {}
        if request.url.host == '8.8.8.8':
            return httpx.Response(200, content=b'movie')
        data = {'status': 'succeeded', 'content': url}
        return httpx.Response(200, json=data)

    wire.handler = handler
    result, error = await video.process_video_generation('p', None, None,
        [endpoint('seedance_video'), endpoint('openai_chat', 'second.example')], None, LOGGER)
    assert sum(r.method == 'POST' for r in wire.requests) == 1
    if recover:
        assert result == base64.b64encode(b'movie').decode() and not error
    else:
        assert result is None and '未自动切换' in error
        assert not wire.records


@pytest.mark.parametrize('case', ['invalid_json', 'wrong_result', 'wrong_data', 'wrong_top_level'])
async def test_image_parse_failure_preserves_accepted_state(wire, case):
    def handler(request):
        if case == 'invalid_json':
            return httpx.Response(200, content=b'{"truncated":')
        data = {'candidates': [{'content': {'parts': ['changed-schema']}}]}
        if case == 'wrong_data':
            data = {'candidates': ['changed-schema']}
        if case == 'wrong_top_level':
            data = ['changed-schema']
        return httpx.Response(200, json=data)
    wire.handler = handler
    result, error = await pipeline.run_drawing(DrawRequest('p'),
        [endpoint('gemini'), endpoint('gemini', 'second.example')], None, LOGGER)
    assert not result and '未自动切换' in error
    assert sum(r.method == 'POST' for r in wire.requests) == 1
    assert not wire.records


async def test_unexpected_local_failure_after_acceptance_cannot_failover(wire, monkeypatch):
    wire.handler = lambda request: httpx.Response(200, json=image_ok())
    def broken_validation(data):
        raise ValueError('DUMMY-KEY at internal.local')
    monkeypatch.setattr(pipeline, '_validate_result_size', broken_validation)
    result, error = await pipeline.run_drawing(DrawRequest('p'),
        [endpoint('gemini'), endpoint('gemini', 'second.example')], None, LOGGER)
    assert not result and len(wire.requests) == 1 and not wire.records
    assert 'DUMMY-KEY' not in error and 'internal.local' not in error


@pytest.mark.parametrize('status', [400, 401, 429, 502])
async def test_user_error_and_logs_do_not_contain_upstream_credentials(wire, caplog, status):
    raw = "The provided credential 'DUMMY-KEY' is invalid; internal.local (10.1.2.3)"
    wire.handler = lambda request: httpx.Response(status, json={'error': {'message': raw}})
    with caplog.at_level(logging.DEBUG):
        _, error = await pipeline.run_drawing(DrawRequest('p'), [endpoint('gemini')], None, LOGGER, debug_mode=True)
    for secret in ('DUMMY-KEY', 'internal.local', '10.1.2.3'):
        assert secret not in error and secret not in caplog.text


async def test_debug_success_without_media_does_not_dump_response(wire, caplog):
    wire.handler = lambda request: httpx.Response(200, json={'choices': [{'message': {'content': 'DUMMY-KEY internal.local'}}]})
    with caplog.at_level(logging.DEBUG):
        _, image_error = await pipeline.run_drawing(DrawRequest('p'), [endpoint('openai_chat')], None, LOGGER, debug_mode=True)
        _, video_error = await video.process_video_generation('p', None, None, [endpoint('openai_chat')], None, LOGGER, debug_mode=True)
    assert 'DUMMY-KEY' not in caplog.text + image_error + video_error
    assert 'internal.local' not in caplog.text + image_error + video_error


@pytest.mark.parametrize('protocol', ['openai_chat', 'gemini', 'seedance_video', 'agnes_video'])
async def test_all_video_submit_bodies_are_capped(wire, monkeypatch, protocol):
    monkeypatch.setattr(limits, 'MAX_HTTP_RESPONSE_BYTES', 128)
    wire.handler = lambda request: httpx.Response(200, json=video_ok(padding='X' * 4096))
    first = endpoint(protocol)
    if protocol == 'agnes_video':
        first['url'] = 'https://first.example/v1/videos'
    result, error = await video.process_video_generation('p', None, None,
        [first, endpoint('openai_chat', 'second.example')], None, LOGGER)
    assert result is None and '上限' in error
    assert len(wire.requests) == 1 and not wire.records


async def test_seedance_poll_body_is_capped_without_recreation(wire, monkeypatch):
    monkeypatch.setattr(limits, 'MAX_HTTP_RESPONSE_BYTES', 128)
    wire.handler = lambda r: httpx.Response(200, json={'id': 'task-1'} if r.method == 'POST' else {'padding': 'X' * 4096})
    result, error = await video.process_video_generation('p', None, None,
        [endpoint('seedance_video'), endpoint('openai_chat', 'second.example')], None, LOGGER)
    assert not result and '上限' in error
    assert [r.method for r in wire.requests] == ['POST', 'GET'] and not wire.records


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


@pytest.mark.parametrize('prefix', [b': ', b'data: ', b'unknown: '])
async def test_sse_limits_raw_unterminated_lines(monkeypatch, prefix):
    monkeypatch.setattr(limits, 'MAX_SSE_LINE_BYTES', 32)
    response = httpx.Response(200, stream=Chunks([prefix, b'X' * 20, b'X' * 20]))
    with pytest.raises(net.MediaTooLargeError):
        async for _ in iter_sse_lines(response):
            pass


async def test_sse_total_counts_ignored_lines(monkeypatch):
    monkeypatch.setattr(limits, 'MAX_SSE_TOTAL_BYTES', 32)
    response = httpx.Response(200, stream=Chunks([b': ignored\n'] * 10))
    with pytest.raises(net.MediaTooLargeError):
        async for _ in iter_sse_lines(response):
            pass


async def test_sse_handles_split_crlf_and_utf8():
    body = 'data: 中文\r\n\rdata: next\n\n'.encode()
    response = httpx.Response(200, stream=Chunks([body[i:i+1] for i in range(len(body))]))
    assert [line async for line in iter_sse_lines(response)] == ['data: 中文', '', 'data: next', '']


@pytest.mark.parametrize('encoding', ['gzip', 'deflate'])
async def test_compressed_response_is_capped_after_bounded_decompression(encoding):
    import gzip
    import zlib
    payload = b'x' * 2_000_000
    compressed = gzip.compress(payload) if encoding == 'gzip' else zlib.compress(payload)
    response = httpx.Response(200, headers={'content-encoding': encoding}, stream=Chunks([compressed]))
    with pytest.raises(net.MediaTooLargeError):
        await read_body_capped(response, 128, enforce=True)


async def test_compressed_success_is_not_decoded_twice(wire):
    import gzip
    wire.handler = lambda r: httpx.Response(200, headers={'content-encoding': 'gzip'},
        stream=Chunks([gzip.compress(json.dumps(video_ok()).encode())]))
    result, error = await video.process_video_generation('p', None, None, [endpoint('openai_chat')], None, LOGGER)
    assert result == VIDEO and not error


async def test_slow_download_is_cancelled_by_total_deadline(monkeypatch):
    cancelled = asyncio.Event()
    class SlowBytes(httpx.AsyncByteStream):
        async def __aiter__(self):
            try:
                yield b'x'
                await asyncio.Event().wait()
            finally:
                cancelled.set()
    real_client = httpx.AsyncClient
    def factory(**kwargs):
        kwargs.pop('transport', None)
        return real_client(transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=SlowBytes())), **kwargs)
    monkeypatch.setattr(httpx, 'AsyncClient', factory)
    with pytest.raises(TimeoutError):
        await net.stream_download('https://8.8.8.8/image', timeout=.03)
    assert cancelled.is_set()


async def test_action_deadline_includes_wait_message():
    from gemini_drawer.commands.actions import ImageGenerateAction
    action = object.__new__(ImageGenerateAction)
    action.rpc_timeout_ms, action.budget_reserve_seconds = 30, 0
    action._precheck = lambda feature: None
    action.action_data = {'prompt': 'p'}
    action._draw_and_send = AsyncMock()
    async def wait_forever():
        await asyncio.Event().wait()
    action._send_wait_message = wait_forever
    result = await action.execute()
    assert not result[0] and '时间预算耗尽' in result[1]
    action._draw_and_send.assert_not_called()


async def test_confirmed_send_is_not_lost_when_cleanup_times_out():
    class Operation:
        @budget_module.budgeted_entry(30, reserve=0)
        async def execute(self):
            budget_module.current_budget().media_sent = True
            await asyncio.Event().wait()
    success, message = await Operation().execute()
    assert success and '已确认媒体发送' in message


async def test_download_request_logs_do_not_expose_signed_url(wire, caplog):
    wire.handler = lambda r: httpx.Response(200, content=b'png')
    url = 'https://8.8.8.8/private-signature-DUMMY-KEY.png?signature=SECRET'
    with caplog.at_level(logging.DEBUG):
        await net.stream_download(url)
    assert 'DUMMY-KEY' not in caplog.text and 'SECRET' not in caplog.text


async def test_redirect_drops_response_cookies_and_rejects_later_downgrade(wire):
    def handler(r):
        if r.url.scheme == 'http':
            return httpx.Response(302, headers={'location': 'https://8.8.8.8/second', 'set-cookie': 'credential=SECRET'})
        assert 'cookie' not in r.headers
        return httpx.Response(302, headers={'location': 'http://8.8.8.8/third'})
    wire.handler = handler
    with pytest.raises(net.DownloadNotAllowedError):
        await net.stream_download('http://8.8.8.8/first')
    assert len(wire.requests) == 2


@pytest.mark.parametrize('suffix', ['?alt=sse', '#section', '?tag=a:b#c:d', '?key=old&tag=x'])
def test_gemini_key_reaches_actual_request_query(suffix):
    url = 'https://gemini.example/v1beta/models/m:generateContent' + suffix
    assert split_url_and_model(url) == (url, None)
    call = GeminiProvider().build(Endpoint(type='custom_g', url=url, key='a+b&c'), DrawRequest('p'))
    request = httpx.Request('POST', call.url)
    assert request.url.params.get_list('key') == ['a+b&c']
    assert not request.url.fragment
    if 'tag=a:b' in suffix:
        assert request.url.params['tag'] == 'a:b'


@pytest.mark.parametrize('url,blocked,has_auth', [
    ('https://8.8.8.8:443/out.mp4', False, True),
    ('https://8.8.8.8:8443/out.mp4', False, False),
    ('http://8.8.8.8/out.mp4', True, False),
])
async def test_initial_video_origin_protects_credentials(wire, url, blocked, has_auth):
    wire.handler = lambda r: httpx.Response(200, content=b'movie')
    async def download():
        return await video._download_video_result(url, api_url='https://8.8.8.8/v1/videos', api_key='DUMMY', proxy=None, logger=LOGGER)
    if blocked:
        with pytest.raises(net.DownloadNotAllowedError):
            await download()
        assert not wire.requests
    else:
        await download()
        assert ('authorization' in wire.requests[0].headers) == has_auth


@pytest.mark.parametrize('policy', [net.DownloadPolicy(allow_private_hosts=True), net.DownloadPolicy(host_allowlist=('127.0.0.1',))])
async def test_explicit_private_policy_works_through_real_transport(policy):
    received = []
    async def serve(reader, writer):
        received.append(await reader.readuntil(b'\r\n\r\n'))
        writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 3\r\nConnection: close\r\n\r\npng')
        await writer.drain()
        writer.close()
        await writer.wait_closed()
    async with await asyncio.start_server(serve, '127.0.0.1', 0) as server:
        port = server.sockets[0].getsockname()[1]
        result = await net.stream_download(f'http://127.0.0.1:{port}/image', policy=policy)
    assert result.data == b'png' and received[0].startswith(b'GET /image ')


class FakeNetworkStream(httpcore.AsyncNetworkStream):
    def __init__(self, ip='8.8.8.8'):
        self.ip, self.writes, self.sni, self.closed = ip, [], None, False
        self.response = b'HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\npng'
    async def read(self, max_bytes, timeout=None):
        response, self.response = self.response, b''
        return response
    async def write(self, buffer, timeout=None):
        self.writes.append(buffer)
    async def aclose(self):
        self.closed = True
    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self.sni = server_hostname
        return self
    def get_extra_info(self, name):
        return (self.ip, 443) if name == 'server_addr' else None


async def test_ip_is_pinned_while_host_and_tls_name_are_preserved(monkeypatch):
    stream = FakeNetworkStream()
    backend = SimpleNamespace(connect_tcp=AsyncMock(return_value=stream))
    monkeypatch.setattr(net, '_resolve_host', lambda host: ('8.8.8.8',))
    async with httpx.AsyncClient(transport=ValidatedTransport(net.DownloadPolicy(), backend=backend), trust_env=False) as client:
        response = await client.get('https://media.example/image')
    assert response.content == b'png'
    assert backend.connect_tcp.call_args.args[:2] == ('8.8.8.8', 443)
    assert stream.sni == 'media.example'
    assert b'Host: media.example' in b''.join(stream.writes)


async def test_dns_drift_is_rejected_before_connect(monkeypatch):
    addresses = iter([('8.8.8.8',), ('127.0.0.1',)])
    monkeypatch.setattr(net, '_resolve_host', lambda host: next(addresses))
    await net.ensure_url_allowed('http://media.example/image')
    backend = SimpleNamespace(connect_tcp=AsyncMock())
    with pytest.raises(net.DownloadNotAllowedError):
        await ValidatedBackend(net.DownloadPolicy(), backend).connect_tcp('media.example', 80)
    backend.connect_tcp.assert_not_called()


async def test_unexpected_peer_is_closed_before_http_or_tls(monkeypatch):
    stream = FakeNetworkStream('127.0.0.1')
    backend = SimpleNamespace(connect_tcp=AsyncMock(return_value=stream))
    monkeypatch.setattr(net, '_resolve_host', lambda host: ('8.8.8.8',))
    async with httpx.AsyncClient(transport=ValidatedTransport(net.DownloadPolicy(), backend=backend), trust_env=False) as client:
        with pytest.raises(net.DownloadNotAllowedError):
            await client.get('https://media.example/image')
    assert stream.closed and not stream.writes and stream.sni is None


async def test_proxy_requires_allowlist_before_any_get(wire):
    wire.handler = lambda r: httpx.Response(200, content=b'png')
    with pytest.raises(net.DownloadNotAllowedError):
        await net.stream_download('https://media.example/image', proxy='http://proxy.example:8080')
    assert not wire.requests
    result = await net.stream_download('https://media.example/image', proxy='http://proxy.example:8080',
        policy=net.DownloadPolicy(host_allowlist=('media.example',)))
    assert result.data == b'png'


async def test_budget_does_not_raise_timeout_above_remaining_time():
    budget = TimeBudget(.02, reserve_seconds=0)
    assert 0 < budget.stage_timeout(60, minimum=5) <= .02
    cancelled = asyncio.Event()
    async def wait_forever():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    with pytest.raises(TimeoutError):
        await budget.run(wait_forever)
    assert cancelled.is_set()


async def test_command_deadline_includes_start_notification(monkeypatch):
    from test_draw_command import make_cmd
    cmd = make_cmd()
    cmd.rpc_timeout_ms, cmd.budget_reserve_seconds = 30, 0
    cancelled = asyncio.Event()
    async def wait_forever():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    cmd._notify_start = wait_forever
    cmd.collect_images = AsyncMock()
    ok, reason, stop = await cmd.execute()
    assert not ok and stop and '时间预算耗尽' in reason and cancelled.is_set()
    cmd.collect_images.assert_not_called()


@pytest.mark.parametrize('outcome', ['timeout', 'disconnect', 'unknown_result'])
async def test_uncertain_http_video_send_is_never_repeated(monkeypatch, outcome):
    from test_video_send import _send, FakeSend, mock_http
    import httpx
    error = (httpx.ReadTimeout('ack lost') if outcome == 'timeout' else
             httpx.RemoteProtocolError('ack lost') if outcome == 'disconnect' else None)
    calls = mock_http(monkeypatch, payload={}, error=error)
    send = FakeSend(error=AssertionError('不得调用 SDK'))
    ok, message = await _send(send)
    assert not ok and '状态不确定' in message
    assert len(calls) == 1 and send.calls == []


async def test_http_video_send_wait_is_cancelled_at_absolute_budget(monkeypatch):
    from test_video_send import _send, FakeSend
    class SmallBudget(TimeBudget):
        def can_start(self, **kwargs):
            return not self.expired()
    cancelled = asyncio.Event()
    async def request(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    monkeypatch.setattr(video, 'request_capped', request)
    send = FakeSend(error=AssertionError('不得调用 SDK'))
    ok, message = await _send(send, budget=SmallBudget(.03, reserve_seconds=0))
    assert not ok and '状态不确定' in message and cancelled.is_set()
    assert send.calls == []
