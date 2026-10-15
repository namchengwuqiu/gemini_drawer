"""绘图指令读取用户文字，不能把宿主的图片描述当作用户提示词。"""
from unittest.mock import AsyncMock

import pytest

from gemini_drawer.commands.draw_commands import (
    CustomDrawCommand, MultiImageDrawCommand, TextToImageCommand,
    TextToVideoCommand, VideoGenerateCommand,
)
from gemini_drawer.plugin import CompatMessage


COMMANDS = [
    (CustomDrawCommand, '/bnn'),
    (MultiImageDrawCommand, '/多图'),
    (TextToImageCommand, '/绘图'),
    (VideoGenerateCommand, '/图生视频'),
    (TextToVideoCommand, '/文生视频'),
]


def mixed_message(command_text):
    return CompatMessage({
        'processed_plain_text': command_text + '\n [图片：宿主生成的图片描述] [表情包：宿主生成的表情描述]',
        'raw_message': [
            {'type': 'text', 'data': command_text},
            {'type': 'image', 'data': '[图片：宿主生成的图片描述]', 'hash': 'a' * 64},
            {'type': 'emoji', 'data': '[表情包：宿主生成的表情描述]', 'hash': 'b' * 64},
        ],
    })


@pytest.mark.parametrize('command_class,prefix', COMMANDS)
async def test_prompt_comes_from_user_text_only(command_class, prefix):
    message = mixed_message(prefix + ' 两个人贴贴')
    command = command_class(message=message, plugin_config={})

    assert await command.get_prompt() == '两个人贴贴'
    assert '宿主生成的图片描述' in message.processed_plain_text
    assert [segment.hash for segment in message.message_segment.data[1:]] == ['a' * 64, 'b' * 64]


@pytest.mark.parametrize('command_class,prefix', COMMANDS)
async def test_image_description_cannot_fill_an_empty_prompt(command_class, prefix):
    command = command_class(message=mixed_message(prefix), plugin_config={})
    command.send_text = AsyncMock(return_value=True)

    assert await command.get_prompt() is None
    command.send_text.assert_awaited_once()


async def test_user_brackets_and_text_on_both_sides_of_images_are_preserved():
    message = CompatMessage({
        'processed_plain_text': '/多图 已混入描述的展示文本',
        'raw_message': [
            {'type': 'text', 'data': '/多'},
            {'type': 'text', 'data': '图 第一行：[图片：用户自己输入的文字]\n'},
            {'type': 'image', 'data': '[图片：自动描述含有[嵌套括号]]'},
            {'type': 'text', 'data': '第二行：保留[左侧[细节]]'},
            {'type': 'emoji', 'data': '[表情包：自动描述]'},
        ],
    })
    command = MultiImageDrawCommand(message=message, plugin_config={})

    assert await command.get_prompt() == '第一行：[图片：用户自己输入的文字]\n第二行：保留[左侧[细节]]'


def test_non_text_segments_never_supply_command_text():
    message = CompatMessage({
        'processed_plain_text': '[图片：上方写着 /多图 两个人贴贴]',
        'raw_message': [{'type': 'image', 'data': '[图片：上方写着 /多图 两个人贴贴]'}],
    })

    assert message.raw_message == ''


def test_reply_and_mentions_are_not_added_to_command_text():
    message = CompatMessage({
        'processed_plain_text': '[回复：被引用的描述] @别人 /多图 两个人贴贴 [图片：自动描述]',
        'raw_message': [
            {'type': 'reply', 'data': {'target_message_id': 'reply-id', 'target_message_content': '被引用的描述'}},
            {'type': 'at', 'data': {'target_user_id': '12345'}},
            {'type': 'text', 'data': '/多图 两个人贴贴'},
            {'type': 'image', 'data': '[图片：自动描述]'},
        ],
    })

    assert message.raw_message == '/多图 两个人贴贴'
    assert message.reply.message_id == 'reply-id'


def test_legacy_text_only_payload_remains_readable():
    assert CompatMessage({'processed_plain_text': '/多图 两个人贴贴'}).raw_message == '/多图 两个人贴贴'


def test_user_text_is_available_without_processed_text():
    message = CompatMessage({'raw_message': [{'type': 'text', 'data': '/多图 两个人贴贴'}]})
    assert message.raw_message == '/多图 两个人贴贴'
