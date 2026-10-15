"""指令前缀统一与工具描述一致性的回归测试（P2-9）。"""
from types import SimpleNamespace

import pytest

from _components import component_metadata
from gemini_drawer.commands.actions import COMMAND_PREFIXES, is_command_message
from gemini_drawer.commands.draw_commands import DRAW_COMMAND_PREFIXES
from gemini_drawer.plugin import _COMMAND_HINT


def test_prefixes_are_defined_once():
    assert COMMAND_PREFIXES is DRAW_COMMAND_PREFIXES
    # 曾经缺失的两个随机绘图指令前缀
    assert "/随机" in DRAW_COMMAND_PREFIXES
    assert "/随机绘图" in DRAW_COMMAND_PREFIXES


def test_command_hint_matches_prefixes():
    assert _COMMAND_HINT == "、".join(DRAW_COMMAND_PREFIXES)


def test_tool_descriptions_list_every_excluded_prefix():
    """工具描述与 Action 排除集合共用同一份定义，不允许漂移（P2-9）。"""
    metadata = component_metadata("handle_generate_image")
    text = f"{metadata.get('description', '')}{metadata.get('detailed_description', '')}"
    for prefix in DRAW_COMMAND_PREFIXES:
        assert prefix in text, prefix


def _message(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        message_segment=SimpleNamespace(type="seglist", data=[SimpleNamespace(type="text", data=text)]),
        processed_plain_text=text,
    )


@pytest.mark.parametrize("text", ["/随机", "/随机绘图 猫", "＃绘图 猫", "/多图 a b", "/bnn x", "/+风格"])
def test_command_messages_are_excluded_from_actions(text):
    assert is_command_message(_message(text)) is True


def test_normal_message_is_not_excluded():
    assert is_command_message(_message("帮我画一只猫")) is False
