"""
Gemini Drawer 绘图命令模块

本模块包含所有用户可用的绘图命令，均继承自 BaseDrawCommand：

CustomDrawCommand (/bnn):
    自定义 Prompt 绘图命令
    - 命令格式：/bnn <自定义提示词>
    - 允许用户直接输入任意 prompt 进行绘图
    - 需要配合图片使用（回复/发送/头像）

TextToImageCommand (/绘图):
    文生图命令
    - 命令格式：/绘图 <描述词>
    - 根据文字描述直接生成图片
    - 不需要源图片（allow_text_only=True）

UniversalPromptCommand (/+ 指令名):
    通用动态绘图指令
    - 命令格式：/+ <预设名称>
    - 使用管理员预先配置的提示词预设进行绘图
    - 支持动态添加新的绘图风格

每个命令通过重写 get_prompt() 方法来定义如何解析用户输入并生成提示词。
"""
import re
import random
from typing import Tuple, Optional
from .base_commands import BaseDrawCommand, BaseMultiImageDrawCommand
from .managers import data_manager
from .utils import logger

class CustomDrawCommand(BaseDrawCommand):
    command_name: str = "gemini_custom_draw"
    command_description: str = "使用自定义Prompt进行AI绘图"
    command_pattern: str = r".*/bnn.*"
    
    async def get_prompt(self) -> Optional[str]:
        cleaned_message = re.sub(r'\[CQ:.*?\]', '', self.message.raw_message).strip()
        command_pattern = "/bnn"
        command_pos = cleaned_message.find(command_pattern)
        
        if command_pos == -1:
            await self.send_text("❌ 未找到 /bnn 指令。")
            return None
            
        prompt_text = cleaned_message[command_pos + len(command_pattern):].strip()
        if not prompt_text:
            await self.send_text("❌ 自定义指令(/bnn)内容不能为空。")
            return None
            
        return prompt_text

class TextToImageCommand(BaseDrawCommand):
    command_name: str = "gemini_text_draw"
    command_description: str = "文生图：根据文字描述生成图片 (格式: /绘图 描述词)"
    command_pattern: str = r".*(?:^|\s)/绘图.*"
    allow_text_only: bool = True

    async def get_prompt(self) -> Optional[str]:
        msg = self.message.raw_message
        match = re.search(r"(?:^|\s)/绘图\s*(.*)", msg, re.DOTALL)
        if not match: return None
        prompt = match.group(1).strip()
        
        if not prompt:
            await self.send_text("❌ 请输入绘图描述！\n例如：`/绘图 一只可爱的小猫`")
            return None
        return prompt

class UniversalPromptCommand(BaseDrawCommand):
    command_name: str = "gemini_universal_prompt"
    command_description: str = "通用动态绘图指令 (格式: /+ 指令名)"
    command_pattern: str = r".*(?:^|[\s\]])/[+].*"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.current_prompt_content = None

    async def execute(self) -> Tuple[bool, Optional[str], bool]:
        msg = self.message.raw_message
        logger.info(f"[Universal] 收到指令: {msg}")
        
        match = re.search(r"(?:^|[\s\]])/\+\s*([^/\s]+)(?:$|[\s\[])", msg)
        if not match: return False, None, False
        
        cmd_name = match.group(1)
        prompts = data_manager.get_prompts()
        
        if cmd_name not in prompts:
            logger.info(f"[Universal] 未找到 Prompt: {cmd_name}")
            await self.send_text(f"❌ 未找到指令: {cmd_name}\n请使用 `/添加提示词 {cmd_name}:内容` 添加。")
            return True, f"未找到指令: {cmd_name}", False
            
        logger.info(f"[Universal] 找到 Prompt: {cmd_name}，准备执行。")
        self.current_prompt_content = prompts[cmd_name]
        return await super().execute()

    async def get_prompt(self) -> Optional[str]:
        return self.current_prompt_content

class MultiImageDrawCommand(BaseMultiImageDrawCommand):
    command_name: str = "gemini_multi_image_draw"
    command_description: str = "多图生图：根据至少2张图片和提示词生成图片"
    command_pattern: str = r".*/多图.*"

    async def get_prompt(self) -> Optional[str]:
        # 移除 CQ 码以获取纯文本
        cleaned_message = re.sub(r'\[CQ:.*?\]', '', self.message.raw_message).strip()
        command_pattern = "/多图"
        command_pos = cleaned_message.find(command_pattern)
        
        if command_pos == -1:
            await self.send_text("❌ 未找到 /多图 指令。")
            return None
            
        prompt_text = cleaned_message[command_pos + len(command_pattern):].strip()
        
        if not prompt_text:
            await self.send_text("❌ 请输入提示词！\n例如：`/多图 融合这两张图`")
            return None
            
        return prompt_text


class RandomPromptDrawCommand(BaseDrawCommand):
    """随机绘图命令 - 从预设中随机选择一个提示词进行绘图"""
    command_name: str = "gemini_random_draw"
    command_description: str = "随机绘图：从预设风格中随机抽取一个进行绘图"
    command_pattern: str = r".*(?:^|\s)/随机(?:绘图)?(?:$|\s).*"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.selected_prompt_name = None
        self.selected_prompt_content = None

    async def execute(self) -> Tuple[bool, Optional[str], bool]:
        prompts = data_manager.get_prompts()
        
        if not prompts:
            await self.send_text("❌ 当前没有任何预设提示词！\n请先使用 `/添加提示词` 添加。")
            return True, "无预设", True
        
        # 随机选择一个提示词
        self.selected_prompt_name = random.choice(list(prompts.keys()))
        self.selected_prompt_content = prompts[self.selected_prompt_name]
        
        logger.info(f"[Random] 随机选中提示词: {self.selected_prompt_name}")
        
        return await super().execute()

    async def get_prompt(self) -> Optional[str]:
        return self.selected_prompt_content

    def get_image_caption(self) -> Optional[str]:
        """返回要与图片一起发送的风格名称"""
        if self.selected_prompt_name:
            return f"🎲 {self.selected_prompt_name}"
        return None