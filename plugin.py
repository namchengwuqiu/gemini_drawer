import asyncio
import json
import re
import base64
from pathlib import Path
from typing import List, Tuple, Type, Optional, Dict, Any
from datetime import datetime
from abc import ABC, abstractmethod

import httpx
from PIL import Image
import io

# --- 核心框架导入 ---
from src.plugin_system import (
    BasePlugin,
    register_plugin,
    ComponentInfo,
    ConfigField,
    BaseCommand,
)
from src.common.logger import get_logger

# 日志记录器
logger = get_logger("gemini_drawer")

# --- [新] 健壮的JSON解析函数 ---
async def extract_image_data(response_data: Dict[str, Any]) -> Optional[str]:
    """通过遍历所有部分来安全地从Gemini API响应中提取图像数据，并兼容LMArena的响应格式。"""
    try:
        # 尝试解析LMArena (OpenAI-like) 响应格式
        if "choices" in response_data and isinstance(response_data["choices"], list) and response_data["choices"]:
            message = response_data["choices"][0].get("message")
            if message and "content" in message and isinstance(message["content"], str):
                # 检查 content 字段中的Markdown格式图片 (URL)
                match_url = re.search(r"!\[.*?\]\((.*?)\)", message["content"])
                if match_url:
                    image_url = match_url.group(1)
                    log_url = image_url
                    if len(log_url) > 100 and "base64" in log_url:
                        log_url = log_url[:50] + "..." + log_url[-20:]
                    logger.info(f"从LMArena响应中提取到图片URL: {log_url}")
                    return image_url

                # 检查 content 字段中的Markdown格式图片 (Base64)
                match_b64 = re.search(r"data:image/\w+;base64,([a-zA-Z0-9+/=\n]+)", message["content"])
                if match_b64:
                    return match_b64.group(1)

        # 原始的Gemini API响应解析逻辑
        candidates = response_data.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            return None

        content = candidates[0].get("content")
        if not isinstance(content, dict):
            return None

        parts = content.get("parts")
        if not isinstance(parts, list) or not parts:
            return None

        # 遍历所有部分以查找图像数据
        for part in parts:
            if not isinstance(part, dict):
                continue

            # 检查 inlineData (以及兼容的 inline_data 写法)
            inline_data = part.get("inlineData") or part.get("inline_data")
            if isinstance(inline_data, dict):
                image_b64 = inline_data.get("data")
                if isinstance(image_b64, str):
                    return image_b64  # 找到了，立即返回

            # 新增：检查 text 字段中的Markdown格式图片
            text_content = part.get("text")
            if isinstance(text_content, str):
                match = re.search(r"data:image/\w+;base64,([a-zA-Z0-9+/=\n]+)", text_content)
                if match:
                    return match.group(1)

        # 如果循环完成仍未找到图像
        return None

    except Exception:
        return None

# --- API密钥管理器 (代码已修改) ---
class KeyManager:
    def __init__(self, keys_file_path: Path = None):
        if keys_file_path is None:
            self.plugin_dir = Path(__file__).parent
            self.data_dir = self.plugin_dir / "data"
            self.data_dir.mkdir(exist_ok=True)
            self.keys_file = self.data_dir / "keys.json"
        else:
            self.keys_file = keys_file_path
            self.plugin_dir = self.keys_file.parent.parent # Assumption for legacy test support
            
        self.config = self._load_config()
        self._migrate_legacy_data()

    def _migrate_legacy_data(self):
        """迁移旧数据到新的存储位置"""
        migrated = False
        
        # 1. 迁移旧的 keys.json (如果在插件根目录)
        # 优先处理旧数据，确保它们被保留
        old_keys_file = self.plugin_dir / "keys.json"
        if old_keys_file.exists() and old_keys_file != self.keys_file:
            try:
                with open(old_keys_file, 'r', encoding='utf-8') as f:
                    old_data = json.load(f)
                    old_keys = old_data.get('keys', [])
                    if old_keys:
                        # 合并到现有配置
                        current_keys = {k['value'] for k in self.config.get('keys', [])}
                        for k in old_keys:
                            if k['value'] not in current_keys:
                                # 旧数据通常没有 type，默认为 google 或根据前缀判断
                                if 'type' not in k:
                                    k['type'] = 'bailili' if k['value'].startswith('sk-') else 'google'
                                self.config['keys'].append(k)
                                migrated = True
                # 备份旧文件
                old_keys_file.rename(old_keys_file.with_suffix('.json.bak'))
                logger.info("已迁移旧的 keys.json 数据")
            except Exception as e:
                logger.error(f"迁移旧 keys.json 失败: {e}")

        # 2. 迁移 config.toml 中的自定义渠道 Key
        config_path = self.plugin_dir / "config.toml"
        if config_path.exists():
            try:
                import toml
                with open(config_path, 'r', encoding='utf-8') as f:
                    config_data = toml.load(f)
                
                channels = config_data.get("channels", {})
                config_changed = False
                
                for name, info in channels.items():
                    key_to_migrate = None
                    if isinstance(info, str):
                        # 旧格式 "url:key"
                        # 更加智能的分割：只有当冒号后的部分看起来像 Key 时才分割
                        if ":" in info:
                            # 尝试从右边分割
                            possible_url, possible_key = info.rsplit(":", 1)
                            
                            # 验证 possible_key 是否像一个 Key
                            # 1. 不包含 / (URL路径)
                            # 2. 长度通常较长 (虽然有些 key 很短，但 URL 后缀通常是单词)
                            # 3. 不包含 . (除了 base64 字符)
                            # 4. 排除常见的 URL 结尾，如 generateContent
                            
                            is_key = True
                            if '/' in possible_key:
                                is_key = False
                            elif possible_key in ['generateContent', 'streamGenerateContent']:
                                is_key = False
                            elif len(possible_key) < 10 and not possible_key.startswith('sk-'):
                                # 极短的字符串可能不是 Key，除非是 sk- 开头
                                # 但这里保守一点，如果太短且不像 key，就认为是 URL 的一部分
                                # 实际上，如果用户真的把 key 写在后面，我们应该信任。
                                # 主要问题是 URL 中包含冒号。
                                # 如果 possible_url 是 http 或 https 结尾，说明冒号是协议分隔符
                                if possible_url.lower() in ['http', 'https']:
                                    is_key = False
                            
                            if is_key:
                                url = possible_url
                                key = possible_key
                                key_to_migrate = key
                                # 更新为新格式
                                channels[name] = {"url": url, "enabled": True}
                                config_changed = True
                            else:
                                # 整个字符串都是 URL
                                channels[name] = {"url": info, "enabled": True}
                                config_changed = True
                                
                    elif isinstance(info, dict):
                        if "key" in info:
                            key_to_migrate = info.pop("key")
                            config_changed = True
                    
                    if key_to_migrate:
                        # 添加到 KeyManager
                        # 注意：如果 Key 已经存在（例如从 keys.json 迁移过来的），add_keys 会忽略它
                        # 这符合“优先考虑旧版数据”的要求，如果旧版数据已经有了这个 Key，我们就不覆盖它的属性
                        self.add_keys([key_to_migrate], name)
                        migrated = True
                        logger.info(f"已迁移渠道 {name} 的 Key")

                if config_changed:
                    with open(config_path, 'w', encoding='utf-8') as f:
                        toml.dump(config_data, f)
                    logger.info("已从 config.toml 移除 Key")

            except Exception as e:
                logger.error(f"迁移 config.toml 数据失败: {e}")

        if migrated:
            self.save_config(self.config)

    def _load_config(self) -> Dict[str, Any]:
        try:
            if not self.keys_file.exists():
                default_config = {"keys": [], "current_index": 0}
                self.save_config(default_config)
                return default_config
            with open(self.keys_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (IOError, json.JSONDecodeError) as e:
            logger.error(f"读取密钥配置失败: {e}")
            return {"keys": [], "current_index": 0}

    def save_config(self, config_data: Dict[str, Any]):
        try:
            with open(self.keys_file, 'w', encoding='utf-8') as f:
                json.dump(config_data, f, indent=4, ensure_ascii=False)
        except IOError as e:
            logger.error(f"保存密钥配置失败: {e}")

    def add_keys(self, new_keys: List[str], key_type: str) -> Tuple[int, int]:
        existing_keys = {key['value'] for key in self.config.get('keys', [])}
        added_count = 0
        duplicate_count = 0
        for key_value in new_keys:
            if key_value in existing_keys:
                duplicate_count += 1
            else:
                # key_type 由外部传入
                key_obj = {"value": key_value, "type": key_type, "status": "active", "error_count": 0, "last_used": None}
                self.config['keys'].append(key_obj)
                added_count += 1
        self.save_config(self.config)
        return added_count, duplicate_count

    def get_all_keys(self) -> List[Dict[str, Any]]:
        return self.config.get('keys', [])

    def get_next_api_key(self) -> Optional[Dict[str, str]]:
        # 注意：这个方法主要用于旧逻辑或默认逻辑，新的 BaseDrawCommand 可能会自己筛选 Key
        keys = self.config.get('keys', [])
        active_keys = [key for key in keys if key.get('status') == 'active']
        if not active_keys:
            return None
        current_index = self.config.get('current_index', 0)
        if current_index >= len(keys):
            current_index = 0
        for i in range(len(keys)):
            next_index = (current_index + i) % len(keys)
            key_obj = keys[next_index]
            if key_obj.get('status') == 'active':
                self.config['current_index'] = (next_index + 1) % len(keys)
                key_obj['last_used'] = datetime.now().isoformat()
                self.save_config(self.config)
                return {"value": key_obj['value'], "type": key_obj.get('type', 'google')}
        return None

    def record_key_usage(self, key_value: str, success: bool, force_disable: bool = False):
        keys = self.config.get('keys', [])
        for key_obj in keys:
            if key_obj['value'] == key_value:
                if success:
                    key_obj['error_count'] = 0
                else:
                    key_obj['error_count'] = key_obj.get('error_count', 0) + 1
                    if force_disable or key_obj['error_count'] >= 5:
                        if key_obj['status'] == 'active':
                            key_obj['status'] = 'disabled'
                            reason = "配额耗尽" if force_disable else "错误次数过多"
                            logger.warning(f"API Key {key_value[:8]}... 已因“{reason}”被自动禁用。")
                self.save_config(self.config)
                return

    def manual_reset_keys(self, key_type: Optional[str] = None) -> int:
        keys = self.config.get('keys', [])
        reset_count = 0
        for key_obj in keys:
            # 如果指定了 key_type，则只重置该类型的 key
            if key_type and key_obj.get('type') != key_type:
                continue
                
            if key_obj.get('status') == 'disabled':
                key_obj['status'] = 'active'
                key_obj['error_count'] = 0
                reset_count += 1
        if reset_count > 0:
            self.save_config(self.config)
        return reset_count

# 初始化 KeyManager
key_manager = KeyManager()

# --- 图像工具 (代码无变化) ---
async def download_image(url: str, proxy: Optional[str]) -> Optional[bytes]:
    try:
        async with httpx.AsyncClient(proxy=proxy, timeout=30.0) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.content
    except httpx.RequestError as e:
        logger.error(f"下载图片失败: {url}, 错误: {e}")
        return None

def get_image_mime_type(image_bytes: bytes) -> str:
    if image_bytes.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if image_bytes.startswith(b'\xff\xd8'):
        return 'image/jpeg'
    if image_bytes.startswith(b'GIF8'):
        return 'image/gif'
    if image_bytes.startswith(b'RIFF') and image_bytes[8:12] == b'WEBP':
        return 'image/webp'
    return 'application/octet-stream'

def convert_if_gif(image_bytes: bytes) -> bytes:
    mime = get_image_mime_type(image_bytes)
    if mime == 'image/gif':
        logger.info("检测到GIF图片，正在转换为PNG...")
        try:
            with Image.open(io.BytesIO(image_bytes)) as img:
                img.seek(0)
                output = io.BytesIO()
                img.save(output, format='PNG')
                return output.getvalue()
        except Exception as e:
            logger.error(f"GIF转PNG失败: {e}")
            return image_bytes
    return image_bytes

# --- [新] 管理命令基类 ---
class BaseAdminCommand(BaseCommand, ABC):
    """封装了管理员权限检查的基类"""
    permission: str = "owner"

    async def execute(self) -> Tuple[bool, Optional[str], bool]:
        if not self.get_config("general.enable_gemini_drawer", True):
            return True, "Plugin disabled", False
        
        user_id_from_msg = getattr(self.message.message_info.user_info, 'user_id', None)
        if not user_id_from_msg:
            logger.warning("无法从 self.message.message_info.user_info 中获取 user_id")
            await self.send_text("无法获取用户信息，操作失败。")
            return False, "无法获取用户信息", True

        str_user_id = str(user_id_from_msg)
        admin_list = self.get_config("general.admins", [])
        str_admin_list = [str(admin) for admin in admin_list]
        
        if str_user_id not in str_admin_list:
            await self.send_text("❌ 仅管理员可用")
            return True, "无权限访问", True
        
        return await self.handle_admin_command()

    @abstractmethod
    async def handle_admin_command(self) -> Tuple[bool, Optional[str], bool]:
        """由子类实现的核心命令逻辑"""
        raise NotImplementedError

# --- 命令组件 (Key管理部分) ---
class ChannelAddKeyCommand(BaseAdminCommand):
    command_name: str = "gemini_channel_add_key"
    command_description: str = "添加渠道API Key (格式: /渠道添加key <渠道名称> <key1> [key2] ...)"
    command_pattern: str = r"^/渠道添加key"

    async def handle_admin_command(self) -> Tuple[bool, Optional[str], bool]:
        command_prefix = "/渠道添加key"
        content = self.message.raw_message.replace(command_prefix, "", 1).strip()
        
        # 使用正则分割，支持空格、逗号、换行等
        import re
        parts = re.split(r"[\s,;，；\n\r]+", content)
        # 过滤空字符串
        parts = [p for p in parts if p.strip()]

        if len(parts) < 2:
            await self.send_text("❌ 参数错误！\n格式：`/渠道添加key <渠道名称> <key1> [key2] ...`\n例如：`/渠道添加key google AIzaSy...` 或 `/渠道添加key PockGo sk-...`")
            return True, "参数不足", True

        channel_name = parts[0]
        new_keys = parts[1:]

        # 验证渠道名称
        valid_channels = ['google']
        custom_channels = self.get_config("channels", {})
        valid_channels.extend(custom_channels.keys())
        
        if channel_name not in valid_channels:
             await self.send_text(f"❌ 未知的渠道名称：`{channel_name}`\n可用渠道：{', '.join(valid_channels)}")
             return True, "未知渠道", True

        added, duplicates = key_manager.add_keys(new_keys, channel_name)
        
        msg = f"✅ 操作完成 (渠道: {channel_name})：\n"
        msg += f"- 成功添加: {added} 个\n"
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

        # 按渠道分组
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
                msg_lines.append(f"  {i+1}. {status_icon} `{masked_key}` {err_info}")
            msg_lines.append("") # 空行分隔

        await self.send_text("\n".join(msg_lines))
        return True, "查询成功", True

class ChannelResetKeysCommand(BaseAdminCommand):
    command_name: str = "gemini_channel_reset_keys"
    command_description: str = "重置渠道Key状态 (格式: /渠道手动重置key [渠道名称])"
    command_pattern: str = r"^/渠道手动重置key"

    async def handle_admin_command(self) -> Tuple[bool, Optional[str], bool]:
        command_prefix = "/渠道手动重置key"
        channel_name = self.message.raw_message.replace(command_prefix, "", 1).strip()
        
        if not channel_name:
            channel_name = None # 重置所有
        
        count = key_manager.manual_reset_keys(channel_name)
        
        target = f"渠道 `{channel_name}`" if channel_name else "所有渠道"
        if count > 0:
            await self.send_text(f"✅ 已成功重置 {target} 的 {count} 个失效 Key。")
        else:
            await self.send_text(f"ℹ️ {target} 没有需要重置的 Key。")
        return True, "重置成功", True

# --- [新] 管理命令 (Prompt管理) ---
class AddPromptCommand(BaseAdminCommand):
    command_name: str = "gemini_add_prompt"
    command_description: str = "添加一个新的绘图提示词预设"
    command_pattern: str = "/添加提示词"

    async def handle_admin_command(self) -> Tuple[bool, Optional[str], bool]:
        command_prefix = "/添加提示词"
        content = self.message.raw_message.replace(command_prefix, "", 1).strip()
        
        if ":" not in content and "：" not in content:
            await self.send_text("❌ 格式错误！\n\n正确格式：`/添加提示词 功能名称:具体提示词`")
            return True, "格式错误", True

        # 同时处理中英文冒号
        parts = re.split(r"[:：]", content, 1)
        name, prompt = parts[0].strip(), parts[1].strip()

        if not name or not prompt:
            await self.send_text("❌ 功能名称和提示词内容都不能为空！")
            return True, "参数不全", True

        try:
            import toml
            config_path = Path(__file__).parent / "config.toml"
            
            with open(config_path, 'r', encoding='utf-8') as f:
                config_data = toml.load(f)
            
            if "prompts" not in config_data:
                config_data["prompts"] = {}
            
            if name in config_data["prompts"]:
                await self.send_text(f"❌ 添加失败：功能名称 `{name}` 已存在，请使用其他名称。")
                return True, "名称重复", True

            config_data["prompts"][name] = prompt
            
            with open(config_path, 'w', encoding='utf-8') as f:
                toml.dump(config_data, f)
            
            await self.send_text(f"✅ 提示词 `{name}` 添加成功！\n请手动重启程序以应用更改。")
            return True, "添加成功", True

        except ImportError:
            await self.send_text("❌ 错误：`toml` 库未安装，无法修改配置文件。")
            return False, "缺少toml库", True
        except Exception as e:
            logger.error(f"添加提示词失败: {e}")
            await self.send_text(f"❌ 操作失败，发生内部错误：{e}")
            return False, str(e), True

class DeletePromptCommand(BaseAdminCommand):
    command_name: str = "gemini_delete_prompt"
    command_description: str = "删除一个绘图提示词预设"
    command_pattern: str = "/删除提示词"

    async def handle_admin_command(self) -> Tuple[bool, Optional[str], bool]:
        command_prefix = "/删除提示词"
        name = self.message.raw_message.replace(command_prefix, "", 1).strip()

        if not name:
            await self.send_text("❌ 请提供要删除的功能名称！\n\n正确格式：`/删除提示词 功能名称`")
            return True, "缺少参数", True

        try:
            import toml
            config_path = Path(__file__).parent / "config.toml"

            if not config_path.exists():
                await self.send_text("❌ 配置文件 `config.toml` 不存在。")
                return True, "配置文件不存在", True

            with open(config_path, 'r', encoding='utf-8') as f:
                config_data = toml.load(f)

            if "prompts" in config_data and name in config_data["prompts"]:
                del config_data["prompts"][name]
                
                with open(config_path, 'w', encoding='utf-8') as f:
                    toml.dump(config_data, f)
                
                await self.send_text(f"✅ 提示词 `{name}` 删除成功！\n请手动重启程序以应用更改。")
                return True, "删除成功", True
            else:
                await self.send_text(f"❌ 未在配置文件中找到名为 `{name}` 的提示词。")
                return True, "提示词不存在", True

        except ImportError:
            await self.send_text("❌ 错误：`toml` 库未安装，无法修改配置文件。")
            return False, "缺少toml库", True
        except Exception as e:
            logger.error(f"删除提示词失败: {e}")
            await self.send_text(f"❌ 操作失败，发生内部错误：{e}")
            return False, str(e), True

class AddChannelCommand(BaseAdminCommand):
    command_name: str = "gemini_add_channel"
    command_description: str = "添加自定义API渠道"
    command_pattern: str = r"^/添加渠道"

    async def handle_admin_command(self) -> Tuple[bool, Optional[str], bool]:
        command_prefix = "/添加渠道"
        rest = self.message.raw_message.replace(command_prefix, "", 1).strip()
        
        help_msg = (
            "❌ 请提供正确的渠道信息！\n"
            "支持两种格式：\n"
            "1. **OpenAI格式** (必须指定模型)：\n"
            "   `/添加渠道 名称:https://.../v1/chat/completions:模型名称`\n"
            "2. **Gemini格式** (模型在URL中)：\n"
            "   `/添加渠道 名称:https://.../models/模型名称:generateContent`"
        )

        if not rest:
            await self.send_text(help_msg)
            return True, "缺少参数", True

        try:
            # 格式: 名称:API地址[:模型]
            if ":" not in rest:
                await self.send_text(help_msg)
                return True, "格式错误", True

            name, rest_part = rest.split(':', 1)
            name = name.strip()
            
            url = ""
            model = None
            
            # 尝试分割 URL 和 Model
            # 逻辑：
            # 1. 如果 URL 包含 /chat/completions，则必须有 Model
            # 2. 如果 URL 包含 :generateContent，则 Model 通常在 URL 中，不需要额外指定
            
            # 先尝试按最后一个冒号分割，看看是不是 Model
            last_colon_index = rest_part.rfind(':')
            
            # 预判 URL 类型
            is_openai = "/chat/completions" in rest_part
            is_gemini = "generateContent" in rest_part
            
            if not is_openai and not is_gemini:
                await self.send_text(
                    "❌ URL 格式不正确！\n"
                    "请检查 API 地址是否正确：\n"
                    "- OpenAI 格式应包含 `/chat/completions`\n"
                    "- Gemini 格式应包含 `:generateContent`"
                )
                return True, "URL格式错误", True

            if is_openai:
                # OpenAI 格式，必须有 Model
                # 检查是否提供了 Model (即是否存在冒号分隔)
                # 注意：URL 本身可能包含端口号 (http://localhost:1234/...)
                # 如果 rest_part 结尾是 /chat/completions，说明没有提供 Model
                if rest_part.strip().endswith("/chat/completions"):
                     await self.send_text("❌ OpenAI 格式必须指定模型名称！\n例如：`/添加渠道 PockGo:https://.../chat/completions:gemini-1.5-pro`")
                     return True, "缺少模型", True
                
                # 尝试分割
                if last_colon_index != -1:
                    possible_model = rest_part[last_colon_index+1:].strip()
                    possible_url = rest_part[:last_colon_index].strip()
                    
                    # 再次验证 URL
                    if possible_url.endswith("/chat/completions"):
                        url = possible_url
                        model = possible_model
                    else:
                        # 可能是端口号？但我们要求必须有 Model
                        # 如果分割出来的 url 不对，那可能是用户没加 Model，而冒号是端口号的一部分
                        await self.send_text("❌ 无法解析模型名称，请确保格式为 `URL:模型`")
                        return True, "解析失败", True
                else:
                     await self.send_text("❌ OpenAI 格式必须指定模型名称！")
                     return True, "缺少模型", True

            elif is_gemini:
                # Gemini 格式，Model 在 URL 中
                # 通常不需要额外指定 Model，但如果用户指定了，我们也可以接受（虽然可能用不上，或者用于覆盖？）
                # 现阶段逻辑：Gemini 格式不需要 Model 参数
                url = rest_part.strip()
                # 简单的验证
                if not url.endswith(":generateContent") and "generateContent" not in url:
                     await self.send_text("❌ Gemini 格式 URL 应以 `:generateContent` 结尾！")
                     return True, "URL格式错误", True

            if not name or not url:
                await self.send_text("❌ 名称和API地址不能为空！")
                return True, "参数不全", True

            # 保存到 config.toml
            import toml
            config_path = Path(__file__).parent / "config.toml"
            
            with open(config_path, 'r', encoding='utf-8') as f:
                config_data = toml.load(f)

            if "channels" not in config_data:
                config_data["channels"] = {}

            channel_info = {
                "url": url,
                "enabled": True
            }
            if model:
                channel_info["model"] = model

            config_data["channels"][name] = channel_info

            with open(config_path, 'w', encoding='utf-8') as f:
                toml.dump(config_data, f)

            msg = f"✅ 自定义渠道 `{name}` 添加成功！\n"
            msg += f"- 类型: {'OpenAI' if is_openai else 'Gemini'}\n"
            msg += f"- URL: `{url}`\n"
            if model:
                msg += f"- Model: `{model}`\n"
            msg += f"\n⚠️ **注意**：请**重启Bot**以应用更改！\n重启后使用 `/渠道添加key {name} <key>` 添加密钥。"
            
            await self.send_text(msg)
            return True, "添加成功", True

        except Exception as e:
            logger.error(f"添加渠道失败: {e}")
            await self.send_text(f"❌ 添加失败：{e}")
            return False, str(e), True

class ChannelUpdateModelCommand(BaseAdminCommand):
    command_name: str = "gemini_channel_update_model"
    command_description: str = "修改渠道模型 (格式: /渠道修改模型 <渠道名称> <新模型名称>)"
    command_pattern: str = r"^/渠道修改模型"

    async def handle_admin_command(self) -> Tuple[bool, Optional[str], bool]:
        command_prefix = "/渠道修改模型"
        content = self.message.raw_message.replace(command_prefix, "", 1).strip()
        
        parts = content.split()
        if len(parts) < 2:
            await self.send_text("❌ 参数错误！\n格式：`/渠道修改模型 <渠道名称> <新模型名称>`\n例如：`/渠道修改模型 PockGo gemini-1.5-pro`")
            return True, "参数不足", True

        channel_name = parts[0]
        new_model = parts[1]

        # 读取 config.toml
        import toml
        config_path = Path(__file__).parent / "config.toml"
        
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config_data = toml.load(f)
            
            channels = config_data.get("channels", {})
            
            if channel_name not in channels:
                await self.send_text(f"❌ 未找到渠道 `{channel_name}`！\n请先使用 `/添加渠道` 创建该渠道。")
                return True, "渠道不存在", True
            
            channel_info = channels[channel_name]
            old_model = channel_info.get("model", "未设置")
            url = channel_info.get("url", "")
            
            # 更新模型字段
            channel_info["model"] = new_model
            
            msg = f"✅ 渠道 `{channel_name}` 模型已更新！\n"
            msg += f"- 旧模型: `{old_model}`\n"
            msg += f"- 新模型: `{new_model}`\n"

            # 特殊处理 Gemini 格式 URL: 尝试替换 URL 中的模型部分
            # 格式: .../models/<model_name>:generateContent
            if "generateContent" in url and "/models/" in url:
                import re
                # 匹配 /models/ 之后，:generateContent 之前的部分
                pattern = r"(/models/)([^:]+)(:generateContent)"
                if re.search(pattern, url):
                    new_url = re.sub(pattern, f"\\g<1>{new_model}\\g<3>", url)
                    if new_url != url:
                        channel_info["url"] = new_url
                        msg += f"- URL已自动更新: `{new_url}`\n"

            channels[channel_name] = channel_info
            config_data["channels"] = channels
            
            with open(config_path, 'w', encoding='utf-8') as f:
                toml.dump(config_data, f)
                
            msg += "\n⚠️ **注意**：请**重启Bot**以应用更改！"
            
            await self.send_text(msg)
            return True, "更新成功", True

        except Exception as e:
            logger.error(f"更新渠道模型失败: {e}")
            await self.send_text(f"❌ 更新失败：{e}")
            return False, str(e), True

class DeleteChannelCommand(BaseAdminCommand):
    command_name: str = "gemini_delete_channel"
    command_description: str = "删除自定义API渠道"
    command_pattern: str = "/删除渠道"

    async def handle_admin_command(self) -> Tuple[bool, Optional[str], bool]:
        command_prefix = "/删除渠道"
        name = self.message.raw_message.replace(command_prefix, "", 1).strip()

        if not name:
            await self.send_text("❌ 请提供要删除的渠道名称！")
            return True, "缺少参数", True

        try:
            import toml
            config_path = Path(__file__).parent / "config.toml"
            
            with open(config_path, 'r', encoding='utf-8') as f:
                config_data = toml.load(f)

            if "channels" in config_data and name in config_data["channels"]:
                del config_data["channels"][name]
                with open(config_path, 'w', encoding='utf-8') as f:
                    toml.dump(config_data, f)
                await self.send_text(f"✅ 渠道 `{name}` 删除成功！\n请手动重启程序以应用更改。")
                return True, "删除成功", True
            else:
                await self.send_text(f"❌ 未找到名为 `{name}` 的渠道。")
                return True, "渠道不存在", True

        except Exception as e:
            logger.error(f"删除渠道失败: {e}")
            await self.send_text(f"❌ 操作失败：{e}")
            return False, str(e), True

class ToggleChannelCommand(BaseAdminCommand):
    command_name: str = "gemini_toggle_channel"
    command_description: str = "启用或禁用指定渠道"
    command_pattern: str = r"^/(启用|禁用)渠道"

    async def handle_admin_command(self) -> Tuple[bool, Optional[str], bool]:
        msg = self.message.raw_message.strip()
        is_enable = msg.startswith("/启用渠道")
        name = msg.replace("/启用渠道" if is_enable else "/禁用渠道", "", 1).strip()

        if not name:
            await self.send_text("❌ 请指定要操作的渠道名称！\n例如：`/启用渠道 google` 或 `/禁用渠道 PockGo`")
            return True, "缺少参数", True

        try:
            import toml
            config_path = Path(__file__).parent / "config.toml"
            
            with open(config_path, 'r', encoding='utf-8') as f:
                config_data = toml.load(f)

            # 确保 api 和 channels 节存在
            if "api" not in config_data: config_data["api"] = {}
            if "channels" not in config_data: config_data["channels"] = {}

            target_found = False
            
            # 处理内置渠道
            if name.lower() == 'google':
                config_data["api"]["enable_google"] = is_enable
                target_found = True
            elif name.lower() == 'lmarena':
                config_data["api"]["enable_lmarena"] = is_enable
                target_found = True
            # 处理自定义渠道
            elif name in config_data["channels"]:
                channel_info = config_data["channels"][name]
                # 如果是旧格式字符串，转为字典
                if isinstance(channel_info, str):
                    url, key = channel_info.rsplit(":", 1)
                    channel_info = {"url": url, "key": key}
                
                channel_info["enabled"] = is_enable
                config_data["channels"][name] = channel_info
                target_found = True
            else:
                await self.send_text(f"❌ 未找到名为 `{name}` 的渠道。")
                return True, "渠道不存在", True

            if target_found:
                with open(config_path, 'w', encoding='utf-8') as f:
                    toml.dump(config_data, f)
                
                action = "启用" if is_enable else "禁用"
                await self.send_text(f"✅ 渠道 `{name}` 已{action}！\n请手动重启程序以应用更改。")
                return True, "操作成功", True

        except Exception as e:
            logger.error(f"切换渠道状态失败: {e}")
            await self.send_text(f"❌ 操作失败：{e}")
            return False, str(e), True

class ListChannelsCommand(BaseAdminCommand):
    command_name: str = "gemini_list_channels"
    command_description: str = "查看所有渠道状态"
    command_pattern: str = "/渠道列表"

    async def handle_admin_command(self) -> Tuple[bool, Optional[str], bool]:
        try:
            import toml
            config_path = Path(__file__).parent / "config.toml"
            
            with open(config_path, 'r', encoding='utf-8') as f:
                config_data = toml.load(f)

            api_config = config_data.get("api", {})
            channels_config = config_data.get("channels", {})

            msg_lines = ["📋 **当前渠道状态列表**", "--------------------"]

            # 1. Google 官方
            enable_google = api_config.get("enable_google", True)
            status_icon = "✅" if enable_google else "❌"
            msg_lines.append(f"{status_icon} **Google** (官方Key)")

            # 2. LMArena
            enable_lmarena = api_config.get("enable_lmarena", False)
            status_icon = "✅" if enable_lmarena else "❌"
            msg_lines.append(f"{status_icon} **LMArena** (免费接口)")

            # 自定义渠道
            if channels_config:
                msg_lines.append("--------------------")
                for name, info in channels_config.items():
                    enabled = True
                    if isinstance(info, dict):
                        enabled = info.get("enabled", True)
                    # 字符串格式默认为启用
                    
                    icon = "✅" if enabled else "❌"
                    model_info = ""
                    if isinstance(info, dict) and info.get("model"):
                        model_info = f" ({info['model']})"
                    
                    msg_lines.append(f"{icon} **{name}**{model_info}")
            
            await self.send_text("\n".join(msg_lines))
            return True, "查询成功", True

        except Exception as e:
            logger.error(f"查询渠道列表失败: {e}")
            await self.send_text(f"❌ 查询失败：{e}")
            return False, str(e), True

# --- [新] 绘图命令基类 (代码已修改) ---
class BaseDrawCommand(BaseCommand, ABC):
    """
    所有绘图命令的抽象基类. 
    封装了图片下载、API调用、重试和结果发送的通用逻辑.
    """
    permission: str = "user"

    async def get_source_image_bytes(self) -> Optional[bytes]:
        """
        按以下顺序在消息中查找源图片:
        1. 消息中直接发送的图片或被QQ标记为'emoji'的回复图片。
        2. 消息文本中 @提及 的用户头像。
        3. 发送指令用户的头像 (作为最终回退)。
        """
        proxy = self.get_config("proxy.proxy_url") if self.get_config("proxy.enable") else None

        # 内部函数，用于从消息段中提取和处理图片
        async def _extract_image_from_segments(segments) -> Optional[bytes]:
            if not segments:
                return None
            if hasattr(segments, 'type') and segments.type == 'seglist':
                segments = segments.data
            if not isinstance(segments, list):
                segments = [segments]
            for seg in segments:
                if seg.type == 'image' or seg.type == 'emoji':
                    if isinstance(seg.data, dict) and seg.data.get('url'):
                        logger.info(f"在消息段中找到URL图片 (类型: {seg.type})。")
                        return await download_image(seg.data.get('url'), proxy)
                    elif isinstance(seg.data, str) and len(seg.data) > 200:
                        try:
                            logger.info(f"在消息段中找到Base64图片 (类型: {seg.type})。")
                            return base64.b64decode(seg.data)
                        except Exception:
                            logger.warning(f"无法将类型为 '{seg.type}' 的段解码为图片，已跳过。")
                            continue
            return None

        # 1. 查找消息中的图片或Emoji
        image_bytes = await _extract_image_from_segments(self.message.message_segment)
        if image_bytes:
            return image_bytes

        # 2. 如果没有图片，查找 @提及 的用户
        segments = self.message.message_segment
        if hasattr(segments, 'type') and segments.type == 'seglist':
            segments = segments.data
        if not isinstance(segments, list):
            segments = [segments]
        
        for seg in segments:
            if seg.type == 'text' and '@' in seg.data:
                # 从包含@的文本中，直接提取其中的数字ID
                match = re.search(r'(\d+)', seg.data)
                if match:
                    mentioned_user_id = match.group(1)
                    logger.info(f"在消息中找到@提及用户 {mentioned_user_id}，获取其头像。")
                    return await download_image(f"https://q1.qlogo.cn/g?b=qq&nk={mentioned_user_id}&s=640", proxy)

        # 3. 回退到发送者自己的头像
        logger.info("未找到图片、Emoji或@提及，回退到发送者头像。")
        user_id = self.message.message_info.user_info.user_id
        return await download_image(f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640", proxy)

    @abstractmethod
    async def get_prompt(self) -> Optional[str]:
        """
        获取用于API请求的prompt. 必须由子类实现.
        """
        raise NotImplementedError

    async def execute(self) -> Tuple[bool, Optional[str], bool]:
        if not self.get_config("general.enable_gemini_drawer", True):
            return True, "Plugin disabled", False
        start_time = datetime.now()

        prompt = await self.get_prompt()
        if not prompt:
            return True, "无效的Prompt", True

        await self.send_text("🎨 正在获取图片和指令…")
        image_bytes = await self.get_source_image_bytes()
        if not image_bytes:
            await self.send_text("❌ 未找到可供处理的图片或图片处理失败。" )
            return True, "缺少图片或处理失败", True
        
        image_bytes = convert_if_gif(image_bytes)
        base64_img = base64.b64encode(image_bytes).decode('utf-8')
        mime_type = get_image_mime_type(image_bytes)
        parts = [{"inline_data": {"mime_type": mime_type, "data": base64_img}}, {"text": prompt}]
        payload = {"contents": [{"parts": parts}]}

        await self.send_text("🤖 已提交至API…")

        # 1. 准备要尝试的API端点列表
        endpoints_to_try = []

        # 首先添加 LMArena (如果启用)
        if self.get_config("api.enable_lmarena", True):
            lmarena_url = self.get_config("api.lmarena_api_url", "https://chat.lmsys.org")
            lmarena_key = self.get_config("api.lmarena_api_key", "") # Placeholder
            endpoints_to_try.append({
                "type": "lmarena",
                "url": lmarena_url,
                "key": lmarena_key
            })

        # 添加自定义渠道 (如果启用)
        custom_channels = self.get_config("channels", {})
        for name, channel_info in custom_channels.items():
            c_url = ""
            c_key = ""
            c_model = None
            c_enabled = True
            
            if isinstance(channel_info, dict):
                c_url = channel_info.get("url")
                c_key = channel_info.get("key")
                c_model = channel_info.get("model")
                c_enabled = channel_info.get("enabled", True)
            elif isinstance(channel_info, str) and ":" in channel_info:
                # 兼容 "url:key" 字符串格式
                c_url, c_key = channel_info.rsplit(":", 1)
            
            if c_url and c_key and c_enabled:
                endpoints_to_try.append({
                    "type": f"custom_{name}",
                    "url": c_url,
                    "key": c_key,
                    "model": c_model
                })

        # 然后添加所有从 key_manager 获取的密钥 (包括内置和自定义渠道的)
        enable_google = self.get_config("api.enable_google", True)
        # enable_bailili 已移除，bailili 现在作为自定义渠道处理

        for key_info in key_manager.get_all_keys():
            if key_info.get('status') != 'active':
                continue
            
            key_type = key_info.get('type')
            # 兼容旧数据：如果没有 type，根据 value 前缀判断
            if not key_type:
                key_type = 'bailili' if key_info['value'].startswith('sk-') else 'google'

            # 1. Google 官方渠道
            if key_type == 'google':
                if enable_google:
                    endpoints_to_try.append({
                        "type": "google",
                        "url": self.get_config("api.api_url"),
                        "key": key_info['value']
                    })
            
            # 2. 自定义渠道 (包括 bailili)
            elif key_type in custom_channels:
                channel_info = custom_channels[key_type]
                c_enabled = True
                c_url = ""
                c_model = None
                
                if isinstance(channel_info, dict):
                    c_url = channel_info.get("url")
                    c_model = channel_info.get("model")
                    c_enabled = channel_info.get("enabled", True)
                
                if c_enabled and c_url:
                    endpoints_to_try.append({
                        "type": f"custom_{key_type}",
                        "url": c_url,
                        "key": key_info['value'],
                        "model": c_model
                    })

        if not endpoints_to_try:
            await self.send_text("❌ 未配置任何API密钥或端点。" )
            return True, "无可用密钥或端点", True

        last_error = ""
        proxy = self.get_config("proxy.proxy_url") if self.get_config("proxy.enable") else None

        # 2. 轮询所有端点
        for i, endpoint in enumerate(endpoints_to_try):
            api_url = endpoint["url"]
            api_key = endpoint["key"]
            endpoint_type = endpoint["type"]
            
            logger.info(f"尝试第 {i+1}/{len(endpoints_to_try)} 个端点: {endpoint_type} ({api_url})")

            headers = {"Content-Type": "application/json"}
            request_url = api_url

            try:
                # 3. 根据端点类型准备请求
                current_payload = payload # Default payload
                client_proxy = proxy # Use global proxy by default
                
                is_openai = False
                
                # 严格根据 URL 判断模式
                if endpoint_type == 'lmarena':
                    # LMArena 特殊处理
                    is_openai = True
                    request_url = f"{api_url}/v1/chat/completions"
                    client_proxy = None # Disable proxy for local lmarena connection
                elif "/chat/completions" in api_url:
                    is_openai = True
                    request_url = api_url
                elif "generateContent" in api_url:
                    is_openai = False
                    request_url = f"{api_url}?key={api_key}"
                else:
                    logger.warning(f"无法识别的API地址格式: {api_url}，跳过。请检查配置。")
                    continue

                if is_openai:
                    if api_key: # 只有存在key时才添加Authorization头
                        headers["Authorization"] = f"Bearer {api_key}"
                    headers["Content-Type"] = "application/json" # 确保Content-Type为application/json
                    
                    # 构造 OpenAI/LMArena 特定的 payload
                    openai_messages = []
                    for part in parts:
                        if "inline_data" in part:
                            openai_messages.append({"role": "user", "content": [{"type": "image_url", "image_url": {"url": f"data:{part['inline_data']['mime_type']};base64,{part['inline_data']['data']}"}}]})
                        elif "text" in part:
                            openai_messages.append({"role": "user", "content": part["text"]})
                    
                    # 确定模型名称
                    model_name = endpoint.get("model")
                    if not model_name:
                        model_name = self.get_config("api.lmarena_model_name", "gemini-2.5-flash-image-preview (nano-banana)")
                    
                    openai_payload = {
                        "model": model_name,
                        "messages": openai_messages,
                        "n": 1
                    }
                    current_payload = openai_payload

                # logger.info(f"准备向 {endpoint_type} 端点发送请求。URL: {request_url}, Payload: {json.dumps(current_payload, ensure_ascii=False)}")

                try:
                    async with httpx.AsyncClient(proxy=client_proxy, timeout=120.0) as client:
                        response = await client.post(request_url, json=current_payload, headers=headers)
                except httpx.RequestError as e:
                    logger.error(f"httpx.RequestError for endpoint {endpoint_type} ({request_url}): {e}")
                    raise # Re-raise to be caught by the outer except block

                if response.status_code == 200:
                    data = response.json()
                    img_data = await extract_image_data(data)
                    
                    if img_data:
                        if endpoint_type != 'lmarena':
                            key_manager.record_key_usage(api_key, True)
                        
                        elapsed = (datetime.now() - start_time).total_seconds()
                        logger.info(f"使用 {endpoint_type} 端点成功生成图片，耗时 {elapsed:.2f}s")
                        
                        try:
                            from src.plugin_system.apis import send_api, chat_api
                            stream_id = None
                            if hasattr(self.message, 'chat_stream') and self.message.chat_stream:
                                stream_info = chat_api.get_stream_info(self.message.chat_stream)
                                stream_id = stream_info.get('stream_id')

                            if stream_id:
                                image_to_send_b64 = None
                                if img_data.startswith(('http://', 'https')):
                                    logger.info("开始下载图片...")
                                    download_start_time = datetime.now()
                                    image_bytes = await download_image(img_data, proxy)
                                    download_elapsed = (datetime.now() - download_start_time).total_seconds()
                                    logger.info(f"图片下载完成，耗时 {download_elapsed:.2f}s")

                                    if image_bytes:
                                        logger.info("开始进行Base64编码...")
                                        encode_start_time = datetime.now()
                                        image_to_send_b64 = base64.b64encode(image_bytes).decode('utf-8')
                                        encode_elapsed = (datetime.now() - encode_start_time).total_seconds()
                                        logger.info(f"Base64编码完成，耗时 {encode_elapsed:.2f}s")
                                elif img_data.startswith('data:image'):
                                    # 处理 data URI 格式
                                    if 'base64,' in img_data:
                                        image_to_send_b64 = img_data.split('base64,')[1]
                                    else:
                                        # 可能是其他编码，暂不支持
                                        logger.warning("不支持的 data URI 格式")
                                        image_to_send_b64 = None
                                else:
                                    image_to_send_b64 = img_data
                                
                                if image_to_send_b64:
                                    logger.info("开始发送图片...")
                                    send_start_time = datetime.now()
                                    await send_api.image_to_stream(
                                        image_base64=image_to_send_b64,
                                        stream_id=stream_id,
                                        storage_message=False
                                    )
                                    send_elapsed = (datetime.now() - send_start_time).total_seconds()
                                    logger.info(f"图片发送完成，耗时 {send_elapsed:.2f}s")
                                    await self.send_text(f"✅ 生成完成 ({elapsed:.2f}s)")
                                else:
                                    raise Exception("图片下载或转换失败")
                            else:
                                raise Exception("无法从当前消息中确定stream_id")
                        except Exception as e:
                            logger.error(f"发送图片失败: {e}")
                            await self.send_text("❌ 图片发送失败。" )

                        return True, "绘图成功", True
                    else:
                        response_file = PLUGIN_DATA_DIR / f"{endpoint_type}_response.json"
                        with open(response_file, 'w', encoding='utf-8') as f:
                            json.dump(data, f, indent=4, ensure_ascii=False)
                        logger.info(f"API响应内容已保存至: {response_file}")
                        raise Exception(f"API未返回图片, 原因: {data.get('candidates', [{}])[0].get('finishReason', '未知')}")
                else:
                    raise Exception(f"API请求失败, 状态码: {response.status_code} - {response.text}")

            except Exception as e:
                logger.warning(f"端点 {endpoint_type} 尝试失败: {e}")
                if endpoint_type != 'lmarena':
                    is_quota_error = "429" in str(e)
                    key_manager.record_key_usage(api_key, False, force_disable=is_quota_error)
                last_error = str(e)
                await asyncio.sleep(1)

        elapsed = (datetime.now() - start_time).total_seconds()
        await self.send_text(f"❌ 生成失败 ({elapsed:.2f}s, {len(endpoints_to_try)}次尝试)\n最终错误: {last_error}")
        return True, "所有尝试均失败", True
    
# --- [新] 具体的绘图命令 ---
class HelpCommand(BaseCommand):
    command_name: str = "gemini_help"
    command_description: str = "显示Gemini绘图插件的帮助信息和所有可用指令。"
    command_pattern: str = "/基咪绘图帮助"
    permission: str = "user"

    async def execute(self) -> Tuple[bool, Optional[str], bool]:
        prompts_config = self.get_config("prompts", {})
        
        reply_lines = ["🎨 Gemini 绘图插件帮助 🎨"]
        reply_lines.append("--------------------")
        reply_lines.append("✨ 用户指令 ✨")
        
        if prompts_config:
            reply_lines.append("【预设风格】")
            preset_commands = [f"  - `/{name}`" for name in prompts_config.keys()]
            reply_lines.extend(preset_commands)
        
        reply_lines.append("\n【自定义风格】")
        reply_lines.append(f"  - `/bnn {{prompt}}`: 使用你的自定义prompt进行绘图。")

        reply_lines.append("\\n【使用方法】")
        reply_lines.append("  - 回复图片 + 指令")
        reply_lines.append("  - @用户 + 指令")
        reply_lines.append("  - 发送图片 + 指令")
        reply_lines.append("  - 直接发送指令 (使用自己头像)")

        # Check for admin permission
        user_id_from_msg = getattr(self.message.message_info.user_info, 'user_id', None)
        admin_list = self.get_config("general.admins", [])
        str_admin_list = [str(admin) for admin in admin_list]

        if user_id_from_msg and str(user_id_from_msg) in str_admin_list:
            reply_lines.append("\n--------------------")
            reply_lines.append("🔑 管理员指令 🔑")
            reply_lines.append("  - `/渠道添加key`: 添加渠道API Key")
            reply_lines.append("  - `/渠道key列表`: 查看各渠道Key状态")
            reply_lines.append("  - `/渠道手动重置key`: 重置渠道Key状态")
            reply_lines.append("  - `/添加提示词`: 添加自定义绘图风格")
            reply_lines.append("  - `/删除提示词`: 删除自定义绘图风格")
            reply_lines.append("  - `/添加渠道`: 添加自定义API渠道")
            reply_lines.append("  - `/删除渠道`: 删除自定义API渠道")
            reply_lines.append("  - `/渠道修改模型`: 修改渠道模型")
            reply_lines.append("  - `/启用渠道`: 启用指定渠道")
            reply_lines.append("  - `/禁用渠道`: 禁用指定渠道")
            reply_lines.append("  - `/渠道列表`: 查看所有渠道状态")
            
        await self.send_text("\n".join(reply_lines))
        return True, "帮助信息已发送", True

# --- [新] 具体的绘图命令 ---
class CustomDrawCommand(BaseDrawCommand):
    command_name: str = "gemini_custom_draw"
    command_description: str = "使用自定义Prompt进行AI绘图"
    command_pattern: str = r".*/bnn.*"
    async def get_prompt(self) -> Optional[str]:
        command_prefix = "/bnn"
        prompt_text = self.message.raw_message.replace(command_prefix, "", 1).strip()
        if not prompt_text:
            await self.send_text("❌ 自定义指令(/bnn)内容不能为空。" )
            return None
        return prompt_text

# --- 插件注册 (代码已修改) ---
@register_plugin
class GeminiDrawerPlugin(BasePlugin):
    plugin_name: str = "gemini_drawer"
    plugin_version: str = "1.1.0"
    enable_plugin: bool = True
    dependencies: List[str] = []
    python_dependencies: List[str] = ["httpx", "Pillow", "toml"]
    config_file_name: str = "config.toml"

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
            "api_url": ConfigField(type=str, default="https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-image:generateContent", description="Google官方的Gemini API 端点"),
            "enable_lmarena": ConfigField(type=bool, default=False, description="是否启用LMArena API"),
            "lmarena_api_url": ConfigField(type=str, default="http://host.docker.internal:5102", description="LMArena API的基础URL (例如: http://host.docker.internal:5102, 如果在Docker中运行)"),
            "lmarena_api_key": ConfigField(type=str, default="", description="[新增]特殊的LMArena API密钥 (可选, 使用Bearer Token)"),
            "lmarena_model_name": ConfigField(type=str, default="gemini-2.5-flash-image-preview (nano-banana)", description="LMArena 使用的模型名称")
        },
        "channels": {},
        "prompts": {
            "手办化": ConfigField(type=str, default="Please accurately transform the main subject in this photo into a realistic, masterpiece-like 1/7 scale PVC statue...", description="默认的手办化prompt"),
            "手办化2": ConfigField(type=str, default="Use the nano-banana model to create a 1/7 scale commercialized figure...", description="手办化prompt版本2"),
            "手办化3": ConfigField(type=str, default="Your primary mission is to accurately convert the subject from the user's photo into a photorealistic...", description="手办化prompt版本3"),
            "手办化4": ConfigField(type=str, default="Please accurately transform the main subject in this photo into a realistic, masterpiece-like 1/7 scale PVC statue...", description="手办化prompt版本4"),
            "手办化5": ConfigField(type=str, default="Realistic PVC figure based on the game screenshot character...", description="手办化prompt版本5"),
            "Q版化": ConfigField(type=str, default="((chibi style)), ((super-deformed)), ((head-to-body ratio 1:2))...", description="Q版化prompt"),
            "cos化": ConfigField(type=str, default="Generate a highly detailed photo of a girl cosplaying this illustration, at Comiket...", description="Cosplay prompt"),
            "ntr化": ConfigField(type=str, default="A scene in a bright, modern restaurant at night, created to replicate the original image provided...", description="NTR prompt"),
            "自拍": ConfigField(type=str, default="selfie, best quality, from front", description="自拍 prompt"),
        }
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Manually trigger config migration on plugin initialization
        self._migrate_config()

    def _migrate_config(self):
        """
        Compares the config.toml with the schema and adds missing fields 
        without overwriting existing user values.
        """
        try:
            import toml
        except ImportError:
            logger.error("Config Migration Failed: `toml` library not found. Please install it via `pip install toml` to enable automatic config updates.")
            return

        config_path = Path(__file__).parent / self.config_file_name
        
        if not config_path.exists():
            # If the file doesn't exist, the framework will create it with defaults.
            # No migration needed.
            return

        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config_data = toml.load(f)

            # Flag to track if changes were made
            original_config_str = toml.dumps(config_data)

            # Helper function to recursively check and update
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

            new_config_str = toml.dumps(config_data)

            if original_config_str != new_config_str:
                with open(config_path, 'w', encoding='utf-8') as f:
                    toml.dump(config_data, f)
                logger.info("Config migration successful: config.toml has been updated with new fields.")

        except Exception as e:
            logger.error(f"Error during config migration: {e}")

    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        """动态注册所有命令组件"""
        components: List[Tuple[ComponentInfo, Type]] = [
            # 帮助命令
            (HelpCommand.get_command_info(), HelpCommand),
            # Key管理命令
            (ChannelAddKeyCommand.get_command_info(), ChannelAddKeyCommand),
            (ChannelListKeysCommand.get_command_info(), ChannelListKeysCommand),
            (ChannelResetKeysCommand.get_command_info(), ChannelResetKeysCommand),
            (ChannelUpdateModelCommand.get_command_info(), ChannelUpdateModelCommand), # 新增
            # Prompt管理命令
            (AddPromptCommand.get_command_info(), AddPromptCommand),
            (DeletePromptCommand.get_command_info(), DeletePromptCommand),
            # 渠道管理命令
            (AddChannelCommand.get_command_info(), AddChannelCommand),
            (DeleteChannelCommand.get_command_info(), DeleteChannelCommand),
            (ToggleChannelCommand.get_command_info(), ToggleChannelCommand),
            (ListChannelsCommand.get_command_info(), ListChannelsCommand),
            # 自定义绘图命令
            (CustomDrawCommand.get_command_info(), CustomDrawCommand),
        ]

        # 从已加载的配置中动态创建绘图命令，而不是从静态的schema
        prompts_config = self.get_config("prompts", {})
        for prompt_name, _ in prompts_config.items():
            # 使用闭包来捕获正确的 prompt_name
            def create_get_prompt(p_name):
                async def get_prompt(self_command) -> Optional[str]:
                    return self_command.get_config(f"prompts.{p_name}")
                return get_prompt

            # 动态创建命令类
            CommandClass = type(
                f"Dynamic{prompt_name}Command",
                (BaseDrawCommand,),
                {
                    "command_name": f"gemini_{prompt_name}",
                    "command_description": f"将图片{prompt_name}",
                    "command_pattern": f".*/{prompt_name}",
                    "get_prompt": create_get_prompt(prompt_name)
                }
            )
            
            components.append((CommandClass.get_command_info(), CommandClass))

        return components