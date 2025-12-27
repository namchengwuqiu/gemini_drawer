"""
Gemini Drawer 管理员命令模块

本模块包含所有需要管理员权限才能执行的命令，主要功能包括：

渠道管理命令：
- ChannelAddKeyCommand: 添加渠道 API Key (/渠道添加key)
- ChannelListKeysCommand: 查看各渠道 Key 状态 (/渠道key列表)
- ChannelResetKeyCommand: 重置 Key 状态 (/渠道重置key)
- ChannelSetKeyErrorLimitCommand: 设置 Key 错误禁用上限 (/渠道设置错误上限)
- ChannelUpdateModelCommand: 修改渠道模型 (/渠道修改模型)
- AddChannelCommand: 添加自定义 API 渠道 (/添加渠道)
- DeleteChannelCommand: 删除自定义 API 渠道 (/删除渠道)
- ToggleChannelCommand: 启用或禁用指定渠道 (/启用渠道, /禁用渠道)
- ListChannelsCommand: 查看所有渠道状态 (/渠道列表)
- ChannelSetStreamCommand: 设置渠道是否使用流式请求 (/渠道设置流式)

提示词管理命令：
- AddPromptCommand: 添加绘图提示词预设 (/添加提示词)
- DeletePromptCommand: 删除绘图提示词预设 (/删除提示词)
- ViewPromptCommand: 查看指定提示词内容 (/查看提示词)

所有命令均继承自 BaseAdminCommand，自动进行管理员权限验证。
"""
import re
from typing import Tuple, Optional
from pathlib import Path
from src.plugin_system import ReplyContentType
from .base_commands import BaseAdminCommand
from .managers import key_manager, data_manager
from .utils import logger, save_config_file

class ChannelAddKeyCommand(BaseAdminCommand):
    command_name: str = "gemini_channel_add_key"
    command_description: str = "添加渠道API Key (格式: /渠道添加key <渠道名称> <key1> [key2] ...)"
    command_pattern: str = r"^/渠道添加key"

    async def handle_admin_command(self) -> Tuple[bool, Optional[str], bool]:
        command_prefix = "/渠道添加key"
        content = self.message.raw_message.replace(command_prefix, "", 1).strip()
        parts = re.split(r"[\s,;，；\n\r]+", content)
        parts = [p for p in parts if p.strip()]

        if len(parts) < 2:
            await self.send_text("❌ 参数错误！\n格式：`/渠道添加key <渠道名称> <key1> [key2] ...`")
            return True, "参数不足", True

        channel_name = parts[0]
        new_keys = parts[1:]

        valid_channels = ['google']
        custom_channels = data_manager.get_channels()
        valid_channels.extend(custom_channels.keys())
        
        if channel_name not in valid_channels:
             await self.send_text(f"❌ 未知的渠道名称：`{channel_name}`\n可用渠道：{', '.join(valid_channels)}")
             return True, "未知渠道", True

        added, duplicates = key_manager.add_keys(new_keys, channel_name)
        msg = f"✅ 操作完成 (渠道: {channel_name})：\n- 成功添加: {added} 个\n"
        if duplicates > 0:
            msg += f"- 重复忽略: {duplicates} 个"
        await self.send_text(msg)
        return True, "添加成功", True

class ChannelListKeysCommand(BaseAdminCommand):
    command_name: str = "gemini_channel_list_keys"
    command_description: str = "查看各渠道Key状态"
    command_pattern: str = r"^/渠道key列表"

    async def handle_admin_command(self) -> Tuple[bool, Optional[str], bool]:
        all_keys = key_manager.get_all_keys()
        if not all_keys:
            await self.send_text("ℹ️ 当前未配置任何 API Key。")
            return True, "无Key", True

        grouped_keys = {}
        for k in all_keys:
            ctype = k.get('type', 'unknown')
            if ctype not in grouped_keys:
                grouped_keys[ctype] = []
            grouped_keys[ctype].append(k)

        msg_lines = ["📋 **渠道 Key 状态列表**", "--------------------"]
        for channel, keys in grouped_keys.items():
            active_count = sum(1 for k in keys if k['status'] == 'active')
            msg_lines.append(f"🔷 **{channel}** (可用: {active_count}/{len(keys)})")
            for i, k in enumerate(keys):
                status_icon = "✅" if k['status'] == 'active' else "❌"
                masked_key = k['value'][:8] + "..." + k['value'][-4:]
                err_info = f"(错误: {k.get('error_count', 0)})" if k.get('error_count', 0) > 0 else ""
                max_errors = k.get('max_errors', 5)
                limit_info = f" [上限: {'∞' if max_errors == -1 else max_errors}]"
                msg_lines.append(f"  {i+1}. {status_icon} `{masked_key}`{limit_info} {err_info}")
            msg_lines.append("")

        await self.send_text("\n".join(msg_lines))
        return True, "查询成功", True

class ChannelResetKeyCommand(BaseAdminCommand):
    command_name: str = "gemini_channel_reset_key"
    command_description: str = "重置Key状态 (格式: /渠道重置key [渠道] [序号])"
    command_pattern: str = r"^/渠道重置key"

    async def handle_admin_command(self) -> Tuple[bool, Optional[str], bool]:
        command_prefix = "/渠道重置key"
        content = self.message.raw_message.replace(command_prefix, "", 1).strip()
        parts = content.split()
        
        if not parts:
            count = key_manager.manual_reset_keys(None)
            await self.send_text(f"✅ 已成功重置所有渠道的 {count} 个失效 Key。")
            return True, "重置所有成功", True
            
        channel_name = parts[0]
        if len(parts) >= 2:
            try:
                index = int(parts[1])
                if key_manager.reset_specific_key(channel_name, index):
                    await self.send_text(f"✅ 已成功重置渠道 `{channel_name}` 的第 {index} 个 Key。")
                else:
                    await self.send_text(f"❌ 重置失败：渠道 `{channel_name}` 不存在第 {index} 个 Key。")
            except ValueError:
                await self.send_text("❌ 序号必须是数字！")
        else:
            count = key_manager.manual_reset_keys(channel_name)
            await self.send_text(f"✅ 已成功重置渠道 `{channel_name}` 的 {count} 个失效 Key。")
        return True, "操作完成", True

class ChannelSetKeyErrorLimitCommand(BaseAdminCommand):
    command_name: str = "gemini_channel_set_key_error_limit"
    command_description: str = "设置Key的错误禁用上限 (格式: /渠道设置错误上限 <渠道> <序号> <次数>)"
    command_pattern: str = r"^/渠道设置错误上限"

    async def handle_admin_command(self) -> Tuple[bool, Optional[str], bool]:
        command_prefix = "/渠道设置错误上限"
        content = self.message.raw_message.replace(command_prefix, "", 1).strip()
        parts = content.split()
        
        if len(parts) != 3:
            await self.send_text("❌ 参数错误！\n格式：`/渠道设置错误上限 <渠道名称> <序号> <次数>`")
            return True, "参数不足", True

        channel_name, index_str, limit_str = parts
        try:
            index = int(index_str)
            limit = int(limit_str)
        except ValueError:
            await self.send_text("❌ 序号和次数必须是数字！")
            return True, "参数类型错误", True

        keys_list = key_manager.config.get('keys', [])
        target_keys_indices = [i for i, key in enumerate(keys_list) if key.get('type') == channel_name]

        if index < 1 or index > len(target_keys_indices):
            await self.send_text(f"❌ 渠道 `{channel_name}` 不存在第 `{index}` 个 Key。")
            return True, "序号无效", True
        
        real_index = target_keys_indices[index - 1]
        keys_list[real_index]['max_errors'] = limit
        key_manager.save_config(key_manager.config)

        limit_text = "永不禁用" if limit == -1 else f"{limit}次"
        await self.send_text(f"✅ 设置成功！\n渠道 `{channel_name}` Key {index} 错误上限: **{limit_text}**。")
        return True, "设置成功", True

class AddPromptCommand(BaseAdminCommand):
    command_name: str = "gemini_add_prompt"
    command_description: str = "添加一个新的绘图提示词预设"
    command_pattern: str = "/添加提示词"

    async def handle_admin_command(self) -> Tuple[bool, Optional[str], bool]:
        command_prefix = "/添加提示词"
        content = self.message.raw_message.replace(command_prefix, "", 1).strip()
        if ":" not in content and "：" not in content:
            await self.send_text("❌ 格式错误！\n正确格式：`/添加提示词 功能名称:具体提示词`")
            return True, "格式错误", True

        parts = re.split(r"[:：]", content, 1)
        name, prompt = parts[0].strip(), parts[1].strip()

        if not name or not prompt:
            await self.send_text("❌ 内容不能为空！")
            return True, "参数不全", True

        if name in data_manager.get_prompts():
            await self.send_text(f"❌ 名称 `{name}` 已存在。")
            return True, "名称重复", True

        data_manager.add_prompt(name, prompt)
        await self.send_text(f"✅ 提示词 `{name}` 添加成功！")
        return True, "添加成功", True

class DeletePromptCommand(BaseAdminCommand):
    command_name: str = "gemini_delete_prompt"
    command_description: str = "删除一个绘图提示词预设"
    command_pattern: str = "/删除提示词"

    async def handle_admin_command(self) -> Tuple[bool, Optional[str], bool]:
        name = self.message.raw_message.replace("/删除提示词", "", 1).strip()
        if not name:
            await self.send_text("❌ 请提供名称！")
            return True, "缺少参数", True

        if data_manager.delete_prompt(name):
            await self.send_text(f"✅ 提示词 `{name}` 删除成功！")
        else:
            await self.send_text(f"❌ 未找到提示词 `{name}`。")
        return True, "删除操作", True

class ViewPromptCommand(BaseAdminCommand):
    command_name: str = "gemini_view_prompt"
    command_description: str = "查看指定提示词的内容"
    command_pattern: str = r"^/查看提示词"

    async def handle_admin_command(self) -> Tuple[bool, Optional[str], bool]:
        name = self.message.raw_message.replace("/查看提示词", "", 1).strip()
        if not name:
            await self.send_text("❌ 请提供名称！")
            return True, "缺少参数", True

        prompts = data_manager.get_prompts()
        if name in prompts:
            bot_name = self.get_config("general.bot_name", "Gemini绘图助手")
            nodes_to_send = [
                ("1", bot_name, [(ReplyContentType.TEXT, f"📝 提示词: {name}")]),
                ("1", bot_name, [(ReplyContentType.TEXT, prompts[name])])
            ]
            await self.send_forward(nodes_to_send)
        else:
            await self.send_text(f"❌ 未找到提示词 `{name}`。")
        return True, "查看成功", True

class AddChannelCommand(BaseAdminCommand):
    command_name: str = "gemini_add_channel"
    command_description: str = "添加自定义API渠道"
    command_pattern: str = r"^/添加渠道"

    async def handle_admin_command(self) -> Tuple[bool, Optional[str], bool]:
        rest = self.message.raw_message.replace("/添加渠道", "", 1).strip()
        help_msg = """❌ 格式错误！请使用正确格式：
📌 OpenAI格式: /添加渠道 名称:URL:模型名
📌 Gemini格式: /添加渠道 名称:URL
📌 豆包格式: /添加渠道 名称:URL:模型名

示例:
/添加渠道 openai渠道:https://api.example.com/v1/chat/completions:gpt-4
/添加渠道 gemini渠道:https://xxx/models/gemini-pro:generateContent
/添加渠道 doubao:https://ark.cn-beijing.volces.com/api/v3/images/generations:doubao-seedream-4-5-251128"""
        
        if not rest or ":" not in rest:
            await self.send_text(help_msg)
            return True, "格式错误", True

        try:
            name, rest_part = rest.split(':', 1)
            name = name.strip()
            url = ""
            model = None
            last_colon_index = rest_part.rfind(':')
            is_openai = "/chat/completions" in rest_part
            is_gemini = "generateContent" in rest_part
            is_doubao = "/images/generations" in rest_part
            
            if not is_openai and not is_gemini and not is_doubao:
                await self.send_text("❌ URL 格式不正确！\n支持的格式：\n- OpenAI: 包含 /chat/completions\n- Gemini: 包含 generateContent\n- 豆包: 包含 /images/generations")
                return True, "URL格式错误", True

            if is_openai:
                if rest_part.strip().endswith("/chat/completions"):
                     await self.send_text("❌ OpenAI 格式必须指定模型名称！")
                     return True, "缺少模型", True
                if last_colon_index != -1:
                    possible_model = rest_part[last_colon_index+1:].strip()
                    possible_url = rest_part[:last_colon_index].strip()
                    if possible_url.endswith("/chat/completions"):
                        url = possible_url
                        model = possible_model
                    else:
                        await self.send_text("❌ 无法解析模型名称")
                        return True, "解析失败", True
                else:
                     await self.send_text("❌ OpenAI 格式必须指定模型名称！")
                     return True, "缺少模型", True

            elif is_doubao:
                # 豆包格式: URL:模型名
                if rest_part.strip().endswith("/images/generations"):
                     await self.send_text("❌ 豆包格式必须指定模型名称！\n例如: https://ark.cn-beijing.volces.com/api/v3/images/generations:doubao-seedream-4-5-251128")
                     return True, "缺少模型", True
                if last_colon_index != -1:
                    possible_model = rest_part[last_colon_index+1:].strip()
                    possible_url = rest_part[:last_colon_index].strip()
                    if "/images/generations" in possible_url:
                        url = possible_url
                        model = possible_model
                    else:
                        await self.send_text("❌ 无法解析豆包模型名称")
                        return True, "解析失败", True
                else:
                     await self.send_text("❌ 豆包格式必须指定模型名称！")
                     return True, "缺少模型", True

            elif is_gemini:
                url = rest_part.strip()
                if not url.endswith(":generateContent") and "generateContent" not in url:
                     await self.send_text("❌ Gemini 格式 URL 应以 `:generateContent` 结尾！")
                     return True, "URL格式错误", True

            channel_info = {"url": url, "enabled": True, "stream": False}
            if model: channel_info["model"] = model
            data_manager.add_channel(name, channel_info)

            api_type = "豆包" if is_doubao else ("OpenAI" if is_openai else "Gemini")
            msg = f"✅ 自定义渠道 `{name}` 添加成功！\n类型: {api_type}\n请使用 `/渠道添加key {name} <your-api-key>` 添加密钥。"
            await self.send_text(msg)
            return True, "添加成功", True

        except Exception as e:
            await self.send_text(f"❌ 添加失败：{e}")
            return False, str(e), True

class ChannelUpdateModelCommand(BaseAdminCommand):
    command_name: str = "gemini_channel_update_model"
    command_description: str = "修改渠道模型"
    command_pattern: str = r"^/渠道修改模型"

    async def handle_admin_command(self) -> Tuple[bool, Optional[str], bool]:
        content = self.message.raw_message.replace("/渠道修改模型", "", 1).strip()
        parts = content.split()
        if len(parts) < 2:
            await self.send_text("❌ 参数错误！")
            return True, "参数不足", True

        channel_name, new_model = parts[0], parts[1]
        channels = data_manager.get_channels()
        if channel_name not in channels:
            await self.send_text(f"❌ 未找到渠道 `{channel_name}`！")
            return True, "渠道不存在", True
        
        channel_info = channels[channel_name]
        url = channel_info.get("url", "")
        channel_info["model"] = new_model
        
        if "generateContent" in url and "/models/" in url:
            pattern = r"(/models/)([^:]+)(:generateContent)"
            if re.search(pattern, url):
                new_url = re.sub(pattern, f"\\g<1>{new_model}\\g<3>", url)
                if new_url != url: channel_info["url"] = new_url

        data_manager.update_channel(channel_name, channel_info)
        await self.send_text(f"✅ 渠道 `{channel_name}` 模型已更新！请重启Bot。")
        return True, "更新成功", True

class DeleteChannelCommand(BaseAdminCommand):
    command_name: str = "gemini_delete_channel"
    command_description: str = "删除自定义API渠道"
    command_pattern: str = "/删除渠道"

    async def handle_admin_command(self) -> Tuple[bool, Optional[str], bool]:
        name = self.message.raw_message.replace("/删除渠道", "", 1).strip()
        if not name:
            await self.send_text("❌ 请提供名称！")
            return True, "缺少参数", True
        if data_manager.delete_channel(name):
            await self.send_text(f"✅ 渠道 `{name}` 删除成功！")
        else:
            await self.send_text(f"❌ 未找到渠道 `{name}`。")
        return True, "删除操作", True

class ToggleChannelCommand(BaseAdminCommand):
    command_name: str = "gemini_toggle_channel"
    command_description: str = "启用或禁用指定渠道"
    command_pattern: str = r"^/(启用|禁用)渠道"

    async def handle_admin_command(self) -> Tuple[bool, Optional[str], bool]:
        msg = self.message.raw_message.strip()
        is_enable = msg.startswith("/启用渠道")
        name = msg.replace("/启用渠道" if is_enable else "/禁用渠道", "", 1).strip()

        if not name:
            await self.send_text("❌ 请指定渠道名称！")
            return True, "缺少参数", True

        channels = data_manager.get_channels()
        target_found = False
        
        if name.lower() in ['google', 'lmarena']:
             import toml
             config_path = Path(__file__).parent / "config.toml"
             with open(config_path, 'r', encoding='utf-8') as f:
                 config_data = toml.load(f)
             if "api" not in config_data: config_data["api"] = {}
             if name.lower() == 'google': config_data["api"]["enable_google"] = is_enable
             else: config_data["api"]["enable_lmarena"] = is_enable
             save_config_file(config_path, config_data)
             target_found = True
        elif name in channels:
            channel_info = channels[name]
            if isinstance(channel_info, str):
                url, key = channel_info.rsplit(":", 1)
                channel_info = {"url": url, "key": key}
            channel_info["enabled"] = is_enable
            data_manager.update_channel(name, channel_info)
            target_found = True
        else:
            await self.send_text(f"❌ 未找到渠道 `{name}`。")
            return True, "渠道不存在", True

        action = "启用" if is_enable else "禁用"
        await self.send_text(f"✅ 渠道 `{name}` 已{action}！")
        return True, "操作成功", True

class ListChannelsCommand(BaseAdminCommand):
    command_name: str = "gemini_list_channels"
    command_description: str = "查看所有渠道状态"
    command_pattern: str = "/渠道列表"

    async def handle_admin_command(self) -> Tuple[bool, Optional[str], bool]:
        import toml
        config_path = Path(__file__).parent / "config.toml"
        with open(config_path, 'r', encoding='utf-8') as f:
            config_data = toml.load(f)

        api_config = config_data.get("api", {})
        channels_config = data_manager.get_channels()
        msg_lines = ["📋 **当前渠道状态列表**", "--------------------"]

        msg_lines.append(f"{'✅' if api_config.get('enable_google', True) else '❌'} **Google** (官方Key)")
        msg_lines.append(f"{'✅' if api_config.get('enable_lmarena', False) else '❌'} **LMArena** (免费接口)")

        if channels_config:
            msg_lines.append("--------------------")
            for name, info in channels_config.items():
                enabled = info.get("enabled", True) if isinstance(info, dict) else True
                stream = info.get("stream", False) if isinstance(info, dict) else False
                stream_info = " [流式]" if stream else ""
                model_info = f" ({info['model']})" if isinstance(info, dict) and info.get("model") else ""
                msg_lines.append(f"{'✅' if enabled else '❌'} **{name}**{model_info}{stream_info}")
        
        await self.send_text("\n".join(msg_lines))
        return True, "查询成功", True

class ChannelSetStreamCommand(BaseAdminCommand):
    command_name: str = "gemini_channel_set_stream"
    command_description: str = "设置渠道是否使用流式请求"
    command_pattern: str = r"^/渠道设置流式"

    async def handle_admin_command(self) -> Tuple[bool, Optional[str], bool]:
        content = self.message.raw_message.replace("/渠道设置流式", "", 1).strip()
        parts = content.split()
        if len(parts) != 2:
            await self.send_text("❌ 参数错误！格式：`/渠道设置流式 <渠道> <true|false>`")
            return True, "参数不足", True

        channel_name, stream_str = parts
        stream_value = stream_str.lower() in ['true', '1', 'yes', '是', '开启', '启用']
        
        channels = data_manager.get_channels()
        if channel_name not in channels:
            await self.send_text(f"❌ 未找到渠道 `{channel_name}`。")
            return True, "渠道不存在", True
        
        channel_info = channels[channel_name]
        if isinstance(channel_info, str):
            url, key = channel_info.rsplit(":", 1)
            channel_info = {"url": url, "key": key}
        
        channel_info["stream"] = stream_value
        data_manager.update_channel(channel_name, channel_info)
        await self.send_text(f"✅ 渠道 `{channel_name}` 流式请求已{'启用' if stream_value else '禁用'}！")
        return True, "设置成功", True