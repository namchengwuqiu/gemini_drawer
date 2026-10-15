"""从两个自拍 Tool 的真实入口验证渠道轮换、底图和单次发送。"""
import asyncio
import base64
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from gemini_drawer.commands.actions import _SelfieActionBase
from gemini_drawer.core import agnes_video, endpoints, pipeline, video
from gemini_drawer.core.budget import TimeBudget
from gemini_drawer.plugin import GeminiDrawerPlugin
from gemini_drawer.providers.gpt_image import GptImageProvider


REFERENCE = b'\x89PNG\r\n\x1a\n' + b'reference' * 100
REFERENCE_B64 = base64.b64encode(REFERENCE).decode()
RESULT = base64.b64encode(b'generated-media' * 100).decode()


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    reference = tmp_path / 'selfie.png'
    reference.write_bytes(REFERENCE)
    rig = SimpleNamespace(requests=[], handler=None, channels={}, records=Mock())
    send = SimpleNamespace(
        text=AsyncMock(return_value=True),
        image=AsyncMock(return_value={'sent': True, 'message_id': 'image-result'}),
        custom=AsyncMock(return_value={'sent': True, 'message_id': 'video-result'}),
    )
    plugin = GeminiDrawerPlugin()
    plugin._ctx = SimpleNamespace(plugin_id='namchengwuqiu.gemini_drawer', send=send,
                                  logger=logging.getLogger('test.selfie_failover'))
    monkeypatch.setattr(plugin, 'get_plugin_config_data', lambda: {
        'general': {'enable_gemini_drawer': True},
        'proxy': {'enable': False},
        'selfie': {'enable': True, 'polish_enable': False, 'base_prompt': '保持底图人物'},
    })
    monkeypatch.setattr(plugin, '_pick_wait_message', lambda kind: '正在准备')
    monkeypatch.setattr(plugin, '_media_cooldown_seconds', lambda: 90.0)
    monkeypatch.setattr(_SelfieActionBase, '_reference_image_path', lambda self: reference)
    monkeypatch.setattr(endpoints, 'data_manager', SimpleNamespace(get_channels=lambda: rig.channels))
    monkeypatch.setattr(endpoints, 'key_manager', SimpleNamespace(get_all_keys=lambda: []))
    for module in (pipeline, video):
        monkeypatch.setattr(module, 'key_manager', SimpleNamespace(record_key_usage=rig.records))

    real_client, real_sleep = httpx.AsyncClient, asyncio.sleep

    async def dispatch(request):
        rig.requests.append(request)
        assert plugin._media_is_pending('stream-1')
        return await rig.handler(request)

    def client(**kwargs):
        kwargs.pop('proxy', None)
        kwargs.pop('transport', None)
        return real_client(transport=httpx.MockTransport(dispatch), **kwargs)

    async def fast_sleep(seconds):
        await real_sleep(0)

    monkeypatch.setattr(httpx, 'AsyncClient', client)
    monkeypatch.setattr(asyncio, 'sleep', fast_sleep)
    rig.plugin, rig.send = plugin, send
    return rig


def configure(rig, kind, *, backup=True):
    is_video = kind != 'image'
    rig.channels = {
        'first': {
            'url': 'https://first.example/v1/videos' if kind == 'video_agnes' else 'https://first.example/v1/chat/completions',
            'protocol': 'agnes_video' if kind == 'video_agnes' else 'openai_chat' if is_video else 'gpt_image',
            'model': 'agnes-video-2.5-flash' if kind == 'video_agnes' else 'video-model' if is_video else 'gpt-image-2',
            'key': 'first-key', 'enabled': True, 'is_video': is_video,
        },
        'second': {
            'url': 'https://second.example/v1/chat/completions', 'protocol': 'openai_chat',
            'model': 'backup-model', 'key': 'second-key', 'enabled': backup, 'is_video': is_video,
        },
    }


async def invoke(rig, kind):
    handler = rig.plugin.handle_selfie if kind == 'image' else rig.plugin.handle_selfie_video
    return await handler(requested_action='在海边挥手', stream_id='stream-1', user_id='user-1', group_id='group-1')


def success_response(kind):
    mime = 'image/png' if kind == 'image' else 'video/mp4'
    return httpx.Response(200, json={'choices': [{'message': {'content': f'data:{mime};base64,{RESULT}'}}]})


def shorten_first_timeout(monkeypatch, kind):
    if kind == 'image':
        monkeypatch.setattr(GptImageProvider, 'timeout', 0.02)
    elif kind == 'video_agnes':
        monkeypatch.setattr(agnes_video, 'GENERATION_TIMEOUT', 0.02)
    else:
        monkeypatch.setattr(video, 'VIDEO_CHANNEL_SAFETY_SECONDS', 0.02)


@pytest.mark.parametrize('kind', ['image', 'video_agnes', 'video_chat'])
@pytest.mark.parametrize('failure', ['http_503', 'read_timeout', 'write_timeout', 'deadline'])
async def test_selfie_tool_reaches_backup_and_sends_once(runtime, monkeypatch, kind, failure):
    configure(runtime, kind)
    if failure == 'deadline':
        shorten_first_timeout(monkeypatch, kind)

    async def handler(request):
        if request.url.host == 'first.example':
            if failure == 'http_503':
                return httpx.Response(503)
            if failure == 'deadline':
                await asyncio.Event().wait()
            error = httpx.ReadTimeout if failure == 'read_timeout' else httpx.WriteTimeout
            raise error('response not confirmed', request=request)
        return success_response(kind)

    runtime.handler = handler
    result = await invoke(runtime, kind)

    assert result['success'] is True, result
    assert [(r.method, r.url.host) for r in runtime.requests] == [
        ('POST', 'first.example'), ('POST', 'second.example'),
    ]
    payloads = [json.loads(r.content) for r in runtime.requests]
    for payload in payloads:
        if 'first_frame' in payload:
            assert payload['first_frame'] == 'data:image/png;base64,' + REFERENCE_B64
            assert payload['mode'] == 'keyframe'
        else:
            content = payload['messages'][0]['content']
            assert [p['image_url']['url'] for p in content if p['type'] == 'image_url'] == [
                'data:image/png;base64,' + REFERENCE_B64,
            ]
            assert content[0]['text'] == '保持底图人物, 在海边挥手'
    assert runtime.records.call_count == 1
    assert runtime.records.call_args.args[:2] == ('second-key', True)
    sent = runtime.send.image if kind == 'image' else runtime.send.custom
    sent.assert_awaited_once()
    assert not runtime.plugin._media_is_pending('stream-1')
    assert runtime.plugin._media_cooldown_remaining('stream-1') > 0


class WaitingBody(httpx.AsyncByteStream):
    async def __aiter__(self):
        await asyncio.Event().wait()
        yield b''


@pytest.mark.parametrize('kind', ['image', 'video_agnes', 'video_chat'])
async def test_selfie_does_not_recreate_after_success_response(runtime, monkeypatch, kind):
    configure(runtime, kind)
    shorten_first_timeout(monkeypatch, kind)

    async def handler(request):
        assert request.url.host == 'first.example'
        if kind == 'video_agnes':
            if request.method == 'POST':
                return httpx.Response(200, json={'video_id': 'created-task', 'status': 'queued'})
            await asyncio.Event().wait()
        return httpx.Response(200, stream=WaitingBody())

    runtime.handler = handler
    result = await invoke(runtime, kind)

    assert result['success'] is False
    assert sum(r.method == 'POST' for r in runtime.requests) == 1
    runtime.send.image.assert_not_awaited()
    runtime.send.custom.assert_not_awaited()
    runtime.records.assert_not_called()
    assert not runtime.plugin._media_is_pending('stream-1')
    assert runtime.plugin._media_cooldown_remaining('stream-1') == 0


@pytest.mark.parametrize('kind', ['image', 'video_agnes', 'video_chat'])
@pytest.mark.parametrize('no_backup', [True, False])
async def test_selfie_timeout_stops_without_backup_or_remaining_budget(runtime, monkeypatch, kind, no_backup):
    configure(runtime, kind, backup=not no_backup)
    clock = {'now': 0.0}
    monkeypatch.setattr(TimeBudget, 'from_rpc_timeout_ms', classmethod(
        lambda cls, timeout_ms, *, reserve_seconds=20: cls(timeout_ms / 1000, reserve_seconds=reserve_seconds,
                                                         clock=lambda: clock['now'])
    ))

    async def handler(request):
        if not no_backup:
            clock['now'] = 1000.0
        raise httpx.ReadTimeout('response not confirmed', request=request)

    runtime.handler = handler
    result = await invoke(runtime, kind)

    assert result['success'] is False
    assert len(runtime.requests) == 1
    runtime.send.image.assert_not_awaited()
    runtime.send.custom.assert_not_awaited()
    runtime.records.assert_not_called()
    assert not runtime.plugin._media_is_pending('stream-1')
