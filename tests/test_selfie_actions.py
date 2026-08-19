"""自拍 Action 的角色一致性提示词回归测试。"""

import asyncio

from gemini_drawer.commands.actions import SelfieGenerateAction, _SelfieActionBase
from gemini_drawer.config import DEFAULT_SELFIE_BASE_PROMPT, DEFAULT_SELFIE_POLISH_TEMPLATE


class FakeLlm:
    def __init__(self):
        self.prompt = None

    async def get_available_models(self):
        return ["replyer"]

    async def generate(self, **kwargs):
        self.prompt = kwargs["prompt"]
        return {"success": True, "response": "在海边挥手，半身自拍构图"}


class DummySelfie:
    """只挂载提示词相关方法，避免启动完整宿主 Action。"""

    polish_template_key = SelfieGenerateAction.polish_template_key
    polish_template_default = SelfieGenerateAction.polish_template_default
    polish_prefix = SelfieGenerateAction.polish_prefix
    polish_request_type = SelfieGenerateAction.polish_request_type
    feature_name = SelfieGenerateAction.feature_name
    _identity_prompt = _SelfieActionBase._identity_prompt
    _compose_prompt = _SelfieActionBase._compose_prompt
    _polish_prompt = _SelfieActionBase._polish_prompt

    def __init__(self):
        self.values = {
            "selfie.base_prompt": "",
            "selfie.polish_enable": True,
            "selfie.polish_model": "replyer",
            "selfie.polish_template": "",
        }
        self.ctx = type("Ctx", (), {"llm": FakeLlm()})()

    def get_config(self, key, default=None):
        return self.values.get(key, default)


def test_legacy_blank_base_prompt_falls_back_to_identity_constraint():
    action = DummySelfie()

    prompt = action._compose_prompt("穿着蓝色外套", [], "looking at viewer")

    assert prompt.startswith(DEFAULT_SELFIE_BASE_PROMPT)
    assert "穿着蓝色外套" in prompt


def test_polish_keeps_identity_constraint_and_uses_default_template():
    async def run():
        action = DummySelfie()
        original = action._compose_prompt("在海边挥手", [], "looking at viewer")
        result = await action._polish_prompt(original)

        assert action.ctx.llm.prompt == DEFAULT_SELFIE_POLISH_TEMPLATE.format(
            original_prompt=original
        )
        assert result.startswith("根据图中人物按以下要求生成图片：")
        assert "身份与画风约束：" in result
        assert DEFAULT_SELFIE_BASE_PROMPT in result

    asyncio.run(run())
