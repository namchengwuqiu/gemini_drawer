import json
import logging
from types import SimpleNamespace

import httpx
import pytest

from gemini_drawer.core import endpoints, pipeline
from gemini_drawer.core.budget import TimeBudget
from gemini_drawer.providers import DrawRequest, Endpoint


@pytest.mark.parametrize('value', [None, '', True, 0, -1, 'nan', 'inf', 'invalid'])
def test_invalid_timeout_preserves_provider_default(value):
    assert Endpoint.from_dict({'type': 'custom_web2api', 'timeout': value}).timeout is None


def test_channel_timeout_is_preserved_for_inline_and_managed_keys(monkeypatch):
    monkeypatch.setattr(endpoints, 'data_manager', SimpleNamespace(get_channels=lambda: {
        'web2api': {'url': 'http://host.docker.internal:3088/v1/chat/completions', 'timeout': 240, 'stream': True, 'key': 'inline'}
    }))
    monkeypatch.setattr(endpoints, 'key_manager', SimpleNamespace(get_all_keys=lambda: [
        {'value': 'managed', 'type': 'web2api', 'status': 'active'}
    ]))
    result = endpoints.build_drawing_endpoints()
    assert len(result) == 2
    assert all(item['timeout'] == 240 and item['stream'] for item in result)


@pytest.mark.asyncio
@pytest.mark.parametrize('budget_seconds,expected_cap', [(280, 240), (100, 70)])
async def test_web2api_timeout_uses_override_within_total_budget(monkeypatch, budget_seconds, expected_cap):
    recorded_caps = []
    real_wait_for = pipeline.asyncio.wait_for
    real_client = httpx.AsyncClient

    async def wait_for(awaitable, timeout):
        recorded_caps.append(timeout)
        return await real_wait_for(awaitable, timeout)

    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, text='data: ' + json.dumps({
            'choices': [{'delta': {'content': 'data:image/png;base64,' + 'a' * 1200}}]
        }) + '\n\ndata: [DONE]\n\n', headers={'content-type': 'text/event-stream'})

    def client_factory(**kwargs):
        kwargs.pop('proxy', None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(pipeline.asyncio, 'wait_for', wait_for)
    monkeypatch.setattr(pipeline.httpx, 'AsyncClient', client_factory)
    monkeypatch.setattr(pipeline.key_manager, 'record_key_usage', lambda *args, **kwargs: None)
    images, error = await pipeline.run_drawing(DrawRequest(prompt='fixture'), [{
        'type': 'custom_web2api', 'url': 'http://host.docker.internal:3088/v1/chat/completions',
        'model': 'l0veyou/gpt-image-2-5-full', 'protocol': 'gpt_image', 'stream': True, 'timeout': 240
    }], None, logging.getLogger('test.channel-timeout'), budget=TimeBudget(budget_seconds, reserve_seconds=0))
    assert images and not error
    assert len(calls) == 1
    assert expected_cap - 1 <= recorded_caps[0] <= expected_cap


@pytest.mark.asyncio
async def test_each_channel_keeps_full_timeout_after_previous_channel_is_slow(monkeypatch):
    """首端点耗尽旧五分钟窗口后，后续端点仍取得完整额度。"""
    clock = [0.0]
    channels = [dict(type=f'custom_{i}', url='https://example.com/v1/chat/completions',
                     protocol='openai_chat', timeout=240) for i in range(3)]
    monkeypatch.setattr(endpoints, 'build_drawing_endpoints', lambda: channels)
    plan = TimeBudget.for_drawing()
    assert plan.remaining >= 3 * 240 + 470
    budget = TimeBudget(plan.remaining, reserve_seconds=0, clock=lambda: clock[0])
    budget.drawing_endpoints = plan.drawing_endpoints
    caps, requests = [], []
    real_wait_for = pipeline.asyncio.wait_for
    real_client = httpx.AsyncClient

    async def wait_for(operation, timeout):
        caps.append(timeout)
        return await real_wait_for(operation, timeout)

    def handler(request):
        requests.append(request)
        if len(requests) < 3:
            clock[0] += 240
            raise httpx.ReadTimeout('channel exhausted', request=request)
        return httpx.Response(200, json={'data': [{'b64_json': 'a' * 1200}]})

    def factory(**kwargs):
        kwargs.pop('proxy', None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(pipeline.asyncio, 'wait_for', wait_for)
    monkeypatch.setattr(pipeline.httpx, 'AsyncClient', factory)
    monkeypatch.setattr(pipeline, 'RETRY_DELAY', 0)
    monkeypatch.setattr(pipeline.key_manager, 'record_key_usage', lambda *a, **kw: None)
    images, error = await pipeline.run_drawing(DrawRequest(prompt='p'), channels, None,
                                              logging.getLogger('test.independent'), budget=budget)
    assert images and not error
    assert len(requests) == 3
    assert caps == [240, 240, 240]
