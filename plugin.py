"""
Gemini Drawer 插件主入口模块

本模块是 Gemini Drawer 插件的核心入口文件，负责：
1. 定义插件的元信息（名称、版本、依赖等）
2. 配置插件的 schema 定义（包括 general、proxy、api、behavior 等配置项）
3. 注册所有可用的命令组件（用户命令和管理员命令）
4. 处理配置文件的迁移和修复

插件结构：
- plugin.py: 插件主入口（本文件）
- base_commands.py: 基础命令类定义
- draw_commands.py: 绘图相关命令
- admin_commands.py: 管理员命令
- help_command.py: 帮助命令
- managers.py: 数据管理器（Key管理、配置管理）
- utils.py: 工具函数

作者：sakura桜花
"""
from typing import List, Tuple, Type
from pathlib import Path

from src.plugin_system import BasePlugin, register_plugin, ComponentInfo, ConfigField
from .utils import fix_broken_toml_config, logger

from .help_command import HelpCommand
from .draw_commands import CustomDrawCommand, TextToImageCommand, UniversalPromptCommand
from .admin_commands import (
    ChannelAddKeyCommand, ChannelListKeysCommand, ChannelResetKeyCommand,
    ChannelDeleteKeyCommand, ChannelSetKeyErrorLimitCommand, ChannelUpdateModelCommand,
    AddPromptCommand, DeletePromptCommand, ViewPromptCommand,
    AddChannelCommand, DeleteChannelCommand, ToggleChannelCommand,
    ListChannelsCommand, ChannelSetStreamCommand
)

@register_plugin
class GeminiDrawerPlugin(BasePlugin):
    plugin_name: str = "gemini_drawer"
    plugin_version: str = "1.5.1"
    enable_plugin: bool = True
    dependencies: List[str] = []
    python_dependencies: List[str] = ["httpx", "Pillow", "toml"]
    config_file_name: str = "config.toml"

    config_section_descriptions = {
        "general": "插件启用配置，其他动态配置请向bot发送/基咪绘图帮助 设置其余配置",
        "proxy": "代理配置",
        "api": "API配置",
    }

    config_schema: dict = {
        "general": {
            "enable_gemini_drawer": ConfigField(type=bool, default=True, description="是否启用Gemini绘图插件"),
            "admins": ConfigField(type=list, default=[], description="可以管理本插件的管理员QQ号列表")
        },
        "proxy": {
            "enable": ConfigField(type=bool, default=False, description="是否为 Gemini API 请求启用代理"),
            "proxy_url": ConfigField(type=str, default="http://127.0.0.1:7890", description="HTTP 代理地址"),
        },
        "api": {
            "enable_google": ConfigField(type=bool, default=True, description="是否启用Google官方API"),
            "api_url": ConfigField(type=str, default="https://generativelanguage.googleapis.com/v1beta/models/gemini-3-pro-image:generateContent", description="Google官方的Gemini API 端点"),
            "enable_lmarena": ConfigField(type=bool, default=False, description="是否启用LMArena API"),
            "lmarena_api_url": ConfigField(type=str, default="http://xxx:666/v1/chat/completions", description="LMArena API的基础URL"),
            "lmarena_api_key": ConfigField(type=str, default="", description="LMArena API密钥 (可选, 使用Bearer Token)"),
            "lmarena_model_name": ConfigField(type=str, default="gemini-3-pro-image-preview", description="LMArena 使用的模型名称")
        },
        "behavior": {
            "admin_only_mode": ConfigField(type=bool, default=False, description="管理员专用模式：开启后仅管理员可使用绘图功能，其他用户会收到'管理员已关闭功能'提示"),
            "auto_recall_status": ConfigField(type=bool, default=True, description="是否自动撤回绘图过程中的状态提示消息（如'🎨 正在提交绘图指令…'）"),
            "success_notify_poke": ConfigField(type=bool, default=True, description="生成成功后使用戳一戳通知用户（替代文字消息'✅ 生成完成'）"),
            "reply_with_image": ConfigField(type=bool, default=True, description="以回复触发消息的方式发送图片（开启后自动跳过成功通知）"),
        }
    }

    def __init__(self, *args, **kwargs):
        try:
            config_path = Path(__file__).parent / self.config_file_name
            if config_path.exists():
                fix_broken_toml_config(config_path)
        except Exception:
            pass

        super().__init__(*args, **kwargs)
        
        try:
            config_path = Path(__file__).parent / self.config_file_name
            if config_path.exists():
                fix_broken_toml_config(config_path)
        except Exception:
            pass
        self._migrate_config()

    def _migrate_config(self):
        try:
            import toml
            config_path = Path(__file__).parent / self.config_file_name
            if not config_path.exists(): return
            
            with open(config_path, 'r', encoding='utf-8') as f:
                config_data = toml.load(f)

            # 简单的Schema检查与迁移逻辑
            def check_and_update(schema_level, config_level):
                for key, field in schema_level.items():
                    if isinstance(field, ConfigField):
                        if key not in config_level:
                            config_level[key] = field.default
                    elif isinstance(field, dict):
                        if key not in config_level:
                            config_level[key] = {}
                        check_and_update(field, config_level[key])

            check_and_update(self.config_schema, config_data)
        except Exception:
            logger.error("Config migration skipped.")

    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        return [
            (HelpCommand.get_command_info(), HelpCommand),
            (ChannelAddKeyCommand.get_command_info(), ChannelAddKeyCommand),
            (ChannelListKeysCommand.get_command_info(), ChannelListKeysCommand),
            (ChannelResetKeyCommand.get_command_info(), ChannelResetKeyCommand),
            (ChannelDeleteKeyCommand.get_command_info(), ChannelDeleteKeyCommand),
            (ChannelSetKeyErrorLimitCommand.get_command_info(), ChannelSetKeyErrorLimitCommand),
            (ChannelUpdateModelCommand.get_command_info(), ChannelUpdateModelCommand), 
            (AddPromptCommand.get_command_info(), AddPromptCommand),
            (DeletePromptCommand.get_command_info(), DeletePromptCommand),
            (ViewPromptCommand.get_command_info(), ViewPromptCommand),
            (AddChannelCommand.get_command_info(), AddChannelCommand),
            (DeleteChannelCommand.get_command_info(), DeleteChannelCommand),
            (ToggleChannelCommand.get_command_info(), ToggleChannelCommand),
            (ListChannelsCommand.get_command_info(), ListChannelsCommand),
            (ChannelSetStreamCommand.get_command_info(), ChannelSetStreamCommand),
            (CustomDrawCommand.get_command_info(), CustomDrawCommand),
            (TextToImageCommand.get_command_info(), TextToImageCommand),
            (UniversalPromptCommand.get_command_info(), UniversalPromptCommand),
        ]