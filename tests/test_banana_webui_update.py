"""大香蕉页面的后台更新、失败保护和文件归属验证。"""

import asyncio
import importlib.util
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
if "maibot_sdk" not in sys.modules:
    try:
        import maibot_sdk
    except ImportError:
        sdk_dir = REPO_ROOT / "maibot_sdk_tmp"
        if not sdk_dir.exists():
            pytest.skip("未安装 maibot_sdk，且没有本地 SDK 快照", allow_module_level=True)
        spec = importlib.util.spec_from_file_location("maibot_sdk", sdk_dir / "__init__.py",
                                                    submodule_search_locations=[str(sdk_dir)])
        module = importlib.util.module_from_spec(spec)
        sys.modules["maibot_sdk"] = module
        spec.loader.exec_module(module)

from gemini_drawer import plugin as plugin_module
from gemini_drawer.core.managers import DataManager
from gemini_drawer.plugin import GeminiDrawerPlugin


@pytest.fixture
def files(tmp_path, monkeypatch):
    manager = DataManager(tmp_path / "data.json")
    manager.data_file.write_text('{"prompts":{"本地":"保留"},"channels":{}}')
    config = tmp_path / "config.toml"
    config.write_text('[plugin]\nenabled = true\n')
    keys = tmp_path / "keys.json"
    keys.write_text('{"keys":[]}')
    manager.save_banana_data({"prompts": {"旧词条": "旧内容"}})
    monkeypatch.setattr(plugin_module, "data_manager", manager)
    return manager, [manager.data_file, config, keys]


def test_sync_only_writes_banana_and_keeps_restricted_metadata(files):
    manager, untouched = files
    before = [path.read_bytes() for path in untouched]
    response = httpx.Response(200, json={"lastUpdated": "2026-10-13", "sections": [
        {"id": "landscape", "title": "风景", "isRestricted": True,
         "prompts": [{"id": "1", "title": "山水", "content": " 水墨山水 "},
                     {"id": "2", "title": "山水", "content": "另一幅山水"}]}
    ]})
    success, message = GeminiDrawerPlugin._save_banana_response(response)
    saved = json.loads(manager.banana_file.read_text())
    assert success and "保存=2" in message
    assert len(saved["prompts"]) == 2 and all(entry["restricted"] for entry in saved["prompts"].values())
    assert saved["remote_last_updated"] == "2026-10-13" and saved["synced_at"]
    assert [path.read_bytes() for path in untouched] == before


@pytest.mark.parametrize("response", [httpx.Response(503), httpx.Response(200, content=b"invalid"),
                                      httpx.Response(200, json={"sections": []})])
def test_bad_download_preserves_existing_cache(files, response):
    manager, _ = files
    before = manager.banana_file.read_bytes()
    success, _ = GeminiDrawerPlugin._save_banana_response(response)
    assert not success and manager.banana_file.read_bytes() == before


@pytest.mark.asyncio
async def test_update_returns_immediately_and_coalesces_duplicate_clicks(monkeypatch):
    plugin = GeminiDrawerPlugin()
    ready = asyncio.Event()
    async def sync():
        await ready.wait()
        return True, "更新完成"
    mock = AsyncMock(side_effect=sync)
    monkeypatch.setattr(plugin, "sync_banana_website_prompts", mock)
    first = await asyncio.wait_for(plugin.drawer_update_banana(), .2)
    second = await plugin.drawer_update_banana()
    assert first["status"] == "running" and first["job_id"] == second["job_id"]
    ready.set()
    await plugin._banana_update_task
    state = await plugin.drawer_banana_update_status(first["job_id"])
    assert state["status"] == "success" and state["message"] == "更新完成"
    assert mock.await_count == 1


@pytest.mark.asyncio
async def test_update_failure_can_be_retried_and_old_job_is_invalid(monkeypatch):
    plugin = GeminiDrawerPlugin()
    monkeypatch.setattr(plugin, "sync_banana_website_prompts", AsyncMock(return_value=(False, "下载失败")))
    first = await plugin.drawer_update_banana()
    await plugin._banana_update_task
    assert (await plugin.drawer_banana_update_status(first["job_id"]))["status"] == "error"
    second = await plugin.drawer_update_banana()
    await plugin._banana_update_task
    assert second["job_id"] != first["job_id"]
    assert not (await plugin.drawer_banana_update_status(first["job_id"]))["ok"]


@pytest.mark.asyncio
async def test_unload_cancels_pending_update(monkeypatch):
    plugin = GeminiDrawerPlugin()
    plugin._ctx = SimpleNamespace(logger=logging.getLogger("test"))
    async def sync():
        await asyncio.Event().wait()
    monkeypatch.setattr(plugin, "sync_banana_website_prompts", sync)
    await plugin.drawer_update_banana()
    await asyncio.sleep(0)
    await plugin.on_unload()
    assert plugin._banana_update_task.cancelled()
    assert plugin._banana_update_state["status"] == "error"
