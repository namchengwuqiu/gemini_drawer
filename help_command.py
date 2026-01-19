"""
Gemini Drawer 帮助命令模块

本模块包含帮助信息显示命令：

HelpCommand (/基咪绘图帮助):
    显示插件的完整帮助信息，包括：
    - 插件介绍和使用说明
    - 所有可用的预设绘图风格列表
    - 用户可用的绘图指令说明
    - 管理员专用命令（仅对管理员显示）

帮助信息通过转发消息的方式发送，分为多个节点展示不同类别的内容。
管理员命令部分会根据发送者是否在管理员列表中动态显示。
"""
from typing import Tuple, Optional
from src.plugin_system import BaseCommand, ReplyContentType
from .managers import data_manager

class HelpCommand(BaseCommand):
    command_name: str = "gemini_help"
    command_description: str = "显示Gemini绘图插件的帮助信息和所有可用指令。"
    command_pattern: str = "/基咪绘图帮助"
    permission: str = "user"

    async def execute(self) -> Tuple[bool, Optional[str], bool]:
        prompts_config = data_manager.get_prompts()
        bot_name = "Gemini Drawer"
        
        header_text = "🎨 Gemini 绘图插件帮助 🎨\n"
        header_text += "本插件基于 Google Gemini 系列模型，提供强大的图片二次创作能力。\n"
        header_text += "--------------------\n"
        header_text += "Tip: 管理员可以使用 /添加提示词 可以动态添加新指令哦！"
        header_content = [(ReplyContentType.TEXT, header_text)]

        user_text = "✨ 用户指令 ✨\n--------------------\n"
        if prompts_config:
            user_text += "【预设风格】(点击指令即可复制)\n"
            sorted_prompts = sorted(prompts_config.keys())
            user_text += "\n".join([f"▪️ /+ {name}" for name in sorted_prompts])
            user_text += "\n\n"
        
        user_text += "【自定义风格】\n"
        user_text += "▪️ /绘图 {描述词}: 文生图，根据文字描述生成图片。\n"
        user_text += "▪️ /bnn {prompt}: 使用你的自定义prompt进行绘图。\n"
        user_text += "▪️ /多图 {prompt}: 多图生图，需配合至少2张图片使用。\n"
        user_text += "▪️ /随机 或 /随机绘图: 随机抽取预设风格进行绘图。\n"
        user_text += "▪️ /视频 {描述词}: 图生视频，需配合图片使用。\n\n"
        user_text += "▪️ /查看提示词 {名称}: 查看指定提示词的完整内容。\n\n"
        user_text += "【使用方法】\n1. 回复图片 + 指令\n2. @用户 + 指令\n3. 发送图片 + 指令\n4. 直接发送指令 (使用自己头像)"
        
        user_content = [(ReplyContentType.TEXT, user_text)]
        nodes_to_send = [("1", bot_name, header_content), ("1", bot_name, user_content)]

        user_id_from_msg = getattr(self.message.message_info.user_info, 'user_id', None)
        admin_list = self.get_config("general.admins", [])
        str_admin_list = [str(admin) for admin in admin_list]

        if user_id_from_msg and str(user_id_from_msg) in str_admin_list:
            admin_text = "🔑 管理员指令 🔑\n--------------------\n"
            admin_text += "▪️ /渠道添加key: 添加渠道API Key\n"
            admin_text += "▪️ /渠道删除key: 删除渠道API Key\n"
            admin_text += "▪️ /渠道key列表: 查看各渠道Key状态\n"
            admin_text += "▪️ /渠道重置key: 重置指定渠道的Key\n"
            admin_text += "▪️ /渠道设置错误上限: 设置Key的错误禁用上限\n"
            admin_text += "▪️ /添加提示词 {名称}:{prompt}: 动态添加绘图风格\n"
            admin_text += "▪️ /删除提示词 {名称}: 删除绘图风格\n"
            admin_text += "▪️ /添加渠道: 添加自定义API渠道\n"
            admin_text += "▪️ /删除渠道: 删除自定义API渠道\n"
            admin_text += "▪️ /渠道修改模型: 修改渠道模型\n"
            admin_text += "▪️ /启用渠道: 启用指定渠道\n"
            admin_text += "▪️ /禁用渠道: 禁用指定渠道\n"
            admin_text += "▪️ /渠道设置流式 {名称} {true|false}: 设置渠道是否使用流式请求\n"
            admin_text += "▪️ /渠道设置视频 {名称} {true|false}: 设置渠道是否用于视频生成\n"
            admin_text += "▪️ /渠道列表: 查看所有渠道状态"
            
            nodes_to_send.append(("1", bot_name, [(ReplyContentType.TEXT, admin_text)]))

        await self.send_forward(nodes_to_send)
        return True, "帮助信息已发送", True