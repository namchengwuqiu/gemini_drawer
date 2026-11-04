"""大香蕉短名称、重名编号与 /+、/多图+ 命令的实际解析行为。"""
import json
import re
from unittest.mock import AsyncMock

import pytest

from gemini_drawer.commands import draw_commands
from gemini_drawer.commands.base_commands import BaseDrawCommand
from gemini_drawer.core.managers import DataManager
from gemini_drawer.plugin import CompatMessage


@pytest.fixture
def names(tmp_path):
    path = tmp_path / "data.json"
    path.write_text(json.dumps({"prompts": {}, "channels": {}}))
    return DataManager(path)


def banana(names, entries):
    names.save_banana_data({"prompts": entries})


def test_unique_short_title_and_full_name_both_resolve(names):
    key = "大香蕉/动漫 SFW/举猫猫"
    banana(names, {key: {"content": "hold a cat", "source_title": "举猫猫"}})
    assert names.get_prompt_matches("举猫猫") == {"举猫猫": "hold a cat"}
    assert names.get_prompt_matches(key) == {key: "hold a cat"}
    assert names.get_prompt_matches("举猫") == {}
    assert names.get_prompt_matches("举猫猫 再加一顶帽子") == {}


def test_duplicate_titles_all_get_numbers_and_base_lists_choices(names):
    banana(names, {
        "大香蕉/B/举猫猫": {"content": "second", "source_title": "举猫猫"},
        "大香蕉/A/举猫猫": {"content": "first", "source_title": "举猫猫"},
        "大香蕉/A/举猫猫#abc123": {"content": "duplicate", "source_title": "举猫猫"},
    })
    assert names.get_prompt_matches("举猫猫") == {
        "举猫猫1": "first", "举猫猫2": "duplicate", "举猫猫3": "second",
    }
    assert names.get_prompt_matches("举猫猫2") == {"举猫猫2": "duplicate"}
    # 别名不进入随机抽取池，避免词条被重复抽样。
    assert len(names.get_effective_prompts()) == 3


def test_numbering_skips_existing_titles_and_local_names(names):
    names.add_prompt("举猫猫2", "local")
    banana(names, {
        "大香蕉/A/举猫猫": {"content": "first", "source_title": "举猫猫"},
        "大香蕉/B/举猫猫": {"content": "second", "source_title": "举猫猫"},
        "大香蕉/A/举猫猫1": {"content": "real numbered title", "source_title": "举猫猫1"},
    })
    assert names.get_prompt_matches("举猫猫") == {"举猫猫3": "first", "举猫猫4": "second"}
    assert names.get_prompt_matches("举猫猫1") == {"举猫猫1": "real numbered title"}
    assert names.get_prompt_matches("举猫猫2") == {"举猫猫2": "local"}


def test_local_preset_wins_and_banana_gets_number(names):
    names.add_prompt("举猫猫", "local")
    banana(names, {"大香蕉/A/举猫猫": {"content": "remote", "source_title": "举猫猫"}})
    assert names.get_prompt_matches("举猫猫") == {"举猫猫": "local"}
    assert names.get_prompt_matches("举猫猫1") == {"举猫猫1": "remote"}


def test_full_key_collision_still_resolves_remote_short_name(names):
    key = "大香蕉/A/举猫猫"
    names.add_prompt(key, "local")
    names.add_prompt(key + "#banana", "another local")
    banana(names, {key: {"content": "remote", "source_title": "举猫猫"}})
    assert names.get_prompt_matches(key) == {key: "local"}
    assert names.get_prompt_matches("举猫猫") == {"举猫猫": "remote"}
    assert names.get_prompt_matches(key + "#banana2") == {key + "#banana2": "remote"}


def test_numbering_is_stable_across_reordering_and_restricted_filter(names):
    entries = {
        "大香蕉/A/举猫猫": {"content": "hidden", "source_title": "举猫猫", "restricted": True},
        "大香蕉/B/举猫猫": {"content": "visible", "source_title": "举猫猫"},
    }
    banana(names, entries)
    aliases = names.get_banana_prompt_aliases(show_restricted=True)
    assert aliases == {"大香蕉/A/举猫猫": "举猫猫1", "大香蕉/B/举猫猫": "举猫猫2"}
    assert names.get_banana_prompt_aliases() == {"大香蕉/B/举猫猫": "举猫猫2"}
    assert names.get_prompt_matches("举猫猫1") == {}
    assert names.get_prompt_matches("大香蕉/A/举猫猫") == {}
    assert names.get_prompt_matches("举猫猫2") == {"举猫猫2": "visible"}
    banana(names, dict(reversed(list(entries.items()))))
    assert names.get_banana_prompt_aliases(show_restricted=True) == aliases


def test_disabled_banana_cannot_resolve_short_or_full_names(names):
    key = "大香蕉/A/举猫猫"
    banana(names, {key: {"content": "remote", "source_title": "举猫猫"}})
    names.add_prompt("本地", "local")
    assert names.get_prompt_matches("举猫猫", include_banana=False) == {}
    assert names.get_prompt_matches(key, include_banana=False) == {}
    assert names.get_prompt_matches("本地", include_banana=False) == {"本地": "local"}


def test_legacy_string_entry_uses_last_path_component(names):
    banana(names, {"大香蕉/旧分类/举猫猫": "legacy"})
    assert names.get_prompt_matches("举猫猫") == {"举猫猫": "legacy"}


@pytest.mark.parametrize("message", ["/+举猫猫", "/+ 举猫猫", "/+大香蕉/动漫 SFW/举猫猫"])
async def test_command_resolves_short_name_before_drawing(names, monkeypatch, message):
    banana(names, {"大香蕉/动漫 SFW/举猫猫": {"content": "hold a cat", "source_title": "举猫猫"}})
    monkeypatch.setattr(draw_commands, "data_manager", names)
    drawing = AsyncMock(return_value=(True, "绘图成功", True))
    monkeypatch.setattr(BaseDrawCommand, "execute", drawing)
    command = draw_commands.UniversalPromptCommand(
        message=CompatMessage({"raw_message": [{"type": "text", "data": message}]}), plugin_config={},
    )
    assert re.search(command.command_pattern, message)
    assert await command.execute() == (True, "绘图成功", True)
    assert await command.get_prompt() == "hold a cat"
    drawing.assert_awaited_once()


def multi_command(text, config=None):
    command = draw_commands.MultiImageDrawCommand(
        message=CompatMessage({
            'session_id': 'stream-1', 'user_id': '456',
            'processed_plain_text': text + ' [图片：自动描述]',
            'raw_message': [
                {'type': 'text', 'data': text},
                {'type': 'image', 'data': '[图片：自动描述]', 'hash': 'a' * 64},
            ],
        }),
        plugin_config=config or {},
    )
    command.send_text = AsyncMock(return_value=True)
    return command


@pytest.mark.parametrize('text', [
    '/多图+贴贴', '/多图+ 贴贴', '/多图 +贴贴', '/多图 + 贴贴',
    '/多图+大香蕉/动漫 SFW/贴贴', '[CQ:at,qq=123] /多图+贴贴 [CQ:image,file=a]',
])
async def test_multi_command_resolves_banana_names_without_image_descriptions(names, monkeypatch, text):
    banana(names, {'大香蕉/动漫 SFW/贴贴': {'content': 'two people together', 'source_title': '贴贴'}})
    monkeypatch.setattr(draw_commands, 'data_manager', names)
    command = multi_command(text)

    assert re.search(command.command_pattern, text)
    assert await command.get_prompt() == 'two people together'
    command.send_text.assert_not_awaited()


async def test_multi_command_local_preset_wins_and_preserves_multiline_content(names, monkeypatch):
    names.add_prompt('贴贴', '第一张图的人物\n和第二张图的人物一起合影[保留细节]')
    banana(names, {'大香蕉/A/贴贴': {'content': 'remote', 'source_title': '贴贴'}})
    monkeypatch.setattr(draw_commands, 'data_manager', names)

    assert await multi_command('/多图+贴贴').get_prompt() == '第一张图的人物\n和第二张图的人物一起合影[保留细节]'
    assert await multi_command('/多图+贴贴1').get_prompt() == 'remote'


async def test_multi_command_lists_numbered_choices_and_resolves_selection(names, monkeypatch):
    banana(names, {
        '大香蕉/A/贴贴': {'content': 'first', 'source_title': '贴贴'},
        '大香蕉/B/贴贴': {'content': 'second', 'source_title': '贴贴'},
    })
    monkeypatch.setattr(draw_commands, 'data_manager', names)
    command = multi_command('/多图+贴贴')
    command.collect_images = AsyncMock()

    assert await command.execute() == (True, '无效的Prompt', True)
    notice = command.send_text.await_args.args[0]
    assert '/多图+贴贴1' in notice and '/多图+贴贴2' in notice
    command.collect_images.assert_not_awaited()
    assert await multi_command('/多图+贴贴2').get_prompt() == 'second'


@pytest.mark.parametrize('text,error', [
    ('/多图+', '请输入预设名称'),
    ('/多图 +  ', '请输入预设名称'),
    ('/多图+不存在', '未找到提示词: 不存在'),
    ('/多图+贴贴 再加一顶帽子', '未找到提示词: 贴贴 再加一顶帽子'),
])
async def test_invalid_multi_preset_stops_before_collecting_images(names, monkeypatch, text, error):
    names.add_prompt('贴贴', 'local')
    monkeypatch.setattr(draw_commands, 'data_manager', names)
    command = multi_command(text)
    command.collect_images = AsyncMock()

    assert await command.execute() == (True, '无效的Prompt', True)
    command.send_text.assert_awaited_once()
    assert error in command.send_text.await_args.args[0]
    command.collect_images.assert_not_awaited()


@pytest.mark.parametrize('behavior,name,expected', [
    ({'enable_banana_prompts': False}, '贴贴', None),
    ({'enable_banana_prompts': False}, '本地', 'local'),
    ({'show_restricted': False}, '贴贴', None),
    ({'show_restricted': True}, '贴贴', 'remote'),
])
async def test_multi_preset_obeys_banana_and_restricted_settings(names, monkeypatch, behavior, name, expected):
    names.add_prompt('本地', 'local')
    banana(names, {'大香蕉/A/贴贴': {'content': 'remote', 'source_title': '贴贴', 'restricted': True}})
    monkeypatch.setattr(draw_commands, 'data_manager', names)
    command = multi_command('/多图+' + name, {'behavior': behavior})

    assert await command.get_prompt() == expected


async def test_command_lists_numbered_choices_and_uses_selected_number(names, monkeypatch):
    banana(names, {
        "大香蕉/A/举猫猫": {"content": "first", "source_title": "举猫猫"},
        "大香蕉/B/举猫猫": {"content": "second", "source_title": "举猫猫"},
    })
    monkeypatch.setattr(draw_commands, "data_manager", names)
    drawing = AsyncMock(return_value=(True, "绘图成功", True))
    monkeypatch.setattr(BaseDrawCommand, "execute", drawing)
    command = draw_commands.UniversalPromptCommand(
        message=CompatMessage({"raw_message": [{"type": "text", "data": "/+举猫猫"}]}), plugin_config={},
    )
    command.send_text = AsyncMock(return_value=True)
    assert await command.execute() == (True, "提示词同名，请选择编号", True)
    notice = command.send_text.await_args.args[0]
    assert "/+举猫猫1" in notice and "/+举猫猫2" in notice
    drawing.assert_not_awaited()
    command.message = CompatMessage({"raw_message": [{"type": "text", "data": "/+举猫猫2"}]})
    assert await command.execute() == (True, "绘图成功", True)
    assert await command.get_prompt() == "second"
    drawing.assert_awaited_once()
