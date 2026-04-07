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
from .utils import fix_broken_toml_config, save_config_file, logger

from .help_command import HelpCommand
from .draw_commands import CustomDrawCommand, TextToImageCommand, UniversalPromptCommand, MultiImageDrawCommand, RandomPromptDrawCommand, VideoGenerateCommand, TextToVideoCommand
from .admin_commands import (
    ChannelAddKeyCommand, ChannelListKeysCommand, ChannelResetKeyCommand,
    ChannelDeleteKeyCommand, ChannelSetKeyErrorLimitCommand, ChannelUpdateModelCommand,
    AddPromptCommand, DeletePromptCommand, ViewPromptCommand, ModifyPromptCommand,
    AddChannelCommand, DeleteChannelCommand, ToggleChannelCommand,
    ListChannelsCommand, ChannelSetStreamCommand, ChannelSetVideoCommand
)

from .actions import ImageGenerateAction, SelfieGenerateAction, SelfieVideoAction

@register_plugin
class GeminiDrawerPlugin(BasePlugin):
    plugin_name: str = "gemini_drawer"
    plugin_version: str = "1.9.6"
    enable_plugin: bool = True
    dependencies: List[str] = []
    python_dependencies: List[str] = ["httpx", "Pillow", "toml"]
    config_file_name: str = "config.toml"

    config_section_descriptions = {
        "general": "插件启用配置，其他动态配置请向bot发送/基咪绘图帮助 设置其余配置",
        "proxy": "代理配置",
        "api": "API配置",
        "selfie": "自拍功能配置",
    }

    config_schema: dict = {
        "general": {
            "enable_gemini_drawer": ConfigField(type=bool, default=True, description="是否启用Gemini绘图插件"),
            "admins": ConfigField(type=list, default=[], description="可以管理本插件的管理员QQ号列表"),
            "blacklist_groups": ConfigField(type=list, default=[], description="禁止使用本插件的群号黑名单列表")
        },
        "proxy": {
            "enable": ConfigField(type=bool, default=False, description="是否为 Gemini API 请求启用代理"),
            "proxy_url": ConfigField(type=str, default="http://127.0.0.1:7890", description="HTTP 代理地址"),
        },
        "selfie": {
            "enable": ConfigField(type=bool, default=False, description="是否启用自拍功能"),
            "reference_image_path": ConfigField(type=str, default="selfie_base.jpg", description="人设底图"),
            "base_prompt": ConfigField(type=str, default="", description="人设基础Prompt (可选，可以不输入因为有人设图)"),
            "random_actions": ConfigField(type=list, default=[
                "向观众眨眼，面带俏皮的微笑",
                "在公园里吃冰淇淋",
                "用手指做和平手势",
                "拿着珍珠奶茶",
                "戴着太阳镜在海滩上",
                "调整头发，看起来害羞",
                "穿着睡衣，抱着枕头",
                "随机生成符合图片人物的自拍动作"
            ], description="随机动作列表"),
            "polish_enable": ConfigField(type=bool, default=True, description="是否启用提示词润色"),
            "polish_model": ConfigField(type=str, default="replyer", description="润色使用的文本模型名称(默认replyer不需要更改)"),
            "polish_template": ConfigField(type=str, default="请将以下自拍主题润色为更适合AI图生图的提示词，保持原意但使描述更加细腻、生动、富有画面感。只输出润色后的提示词，不要输出其他内容。原始主题：'{original_prompt}'", description="润色提示词模板"),
            "video_actions": ConfigField(type=list, default=[
                "缓缓转头，露出微笑",
                "轻轻挥手打招呼",
                "眨眼并微微歪头",
                "点头微笑",
                "比耶手势"
            ], description="视频自拍随机动作列表"),
            "video_polish_template": ConfigField(type=str, default="请将以下视频动作描述润色为更适合AI图生视频生成的提示词时长5～10秒，让动作描述更加流畅、生动、有画面感。只输出润色后的提示词，不要输出其他内容。原始描述：'{original_prompt}'", description="视频提示词润色模板")
        },
        "api": {
            "enable_google": ConfigField(type=bool, default=True, description="是否启用Google官方API"),
            "api_url": ConfigField(type=str, default="https://generativelanguage.googleapis.com/v1beta/models/gemini-3-pro-image:generateContent", description="Google官方的Gemini API 端点"),
            "enable_lmarena": ConfigField(type=bool, default=False, description="是否启用第三方API"),
            "lmarena_api_url": ConfigField(type=str, default="http://xxx:666/v1/chat/completions", description="第三方API的基础URL"),
            "lmarena_api_key": ConfigField(type=str, default="", description="第三方API密钥 (可选, 使用Bearer Token)"),
            "lmarena_model_name": ConfigField(type=str, default="gemini-3-pro-image-preview", description="第三方API 使用的模型名称"),
            "napcat_host": ConfigField(type=str, default="napcat", description="NapCat HTTP服务器地址（Docker环境下设为'napcat'或容器名）"),
            "napcat_port": ConfigField(type=int, default=3033, description="NapCat 正向HTTP端口，用于发送视频文件")
        },
        "behavior": {
            "debug_mode": ConfigField(type=bool, default=False, description="调试模式：开启后当图片/视频提取失败时，会在终端输出原始API响应内容，帮助排查问题"),
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

        # 初始化自拍目录
        try:
            if self.get_config("selfie.enable"):
                image_filename = self.get_config("selfie.reference_image_path")
                # 总是基于插件目录下的 images 文件夹
                plugin_dir = Path(__file__).parent
                images_dir = plugin_dir / "images"
                
                if not images_dir.exists():
                    images_dir.mkdir(parents=True, exist_ok=True)
                    logger.info(f"[GeminiDrawer] Auto-created images directory at: {images_dir}")
                
        except Exception as e:
            logger.warning(f"[GeminiDrawer] Failed to initialize selfie directory: {e}")

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
            
            # 保存更新后的配置
            save_config_file(config_path, config_data)
        except Exception as e:
            logger.error(f"Config migration failed: {e}")

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
            (ModifyPromptCommand.get_command_info(), ModifyPromptCommand),
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
            (MultiImageDrawCommand.get_command_info(), MultiImageDrawCommand),
            (RandomPromptDrawCommand.get_command_info(), RandomPromptDrawCommand),
            (VideoGenerateCommand.get_command_info(), VideoGenerateCommand),
            (TextToVideoCommand.get_command_info(), TextToVideoCommand),
            (ChannelSetVideoCommand.get_command_info(), ChannelSetVideoCommand),
            (ImageGenerateAction.get_action_info(), ImageGenerateAction),
            (SelfieGenerateAction.get_action_info(), SelfieGenerateAction),
            (SelfieVideoAction.get_action_info(), SelfieVideoAction),
        ]