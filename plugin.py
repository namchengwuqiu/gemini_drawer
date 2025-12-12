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
    ReplyContentType,
)
from src.common.logger import get_logger

# 日志记录器
logger = get_logger("gemini_drawer")

# --- [新增] 配置文件修复工具 ---
def fix_broken_toml_config(file_path: Path):
    """
    读取配置文件原始文本，使用正则强制修复未加引号的中文键名。
    专门解决框架自动生成时 key 不带引号导致 Empty key 报错的问题。
    """
    if not file_path.exists():
        return

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        fixed_lines = []
        modified = False
        
        # 匹配规则：行首是非引号、非注释、非方括号的字符，且包含中文，后接等号
        # 简单来说就是匹配： 手办化 = "..." 这种格式
        pattern = re.compile(r'^([^#\n"\'\[]*[\u4e00-\u9fa5][^#\n"\'\[]*?)\s*=')
        
        for line in lines:
            match = pattern.match(line)
            if match:
                key = match.group(1).strip()
                # 构造修复后的行： "手办化" = ...
                # 保持原有的等号后的内容
                parts = line.split('=', 1)
                if len(parts) == 2:
                    new_line = f'"{key}" ={parts[1]}'
                    fixed_lines.append(new_line)
                    modified = True
                    # logger.info(f"自动修复配置文件格式: {key} -> \"{key}\"")
                else:
                    fixed_lines.append(line)
            else:
                fixed_lines.append(line)
        
        if modified:
            with open(file_path, 'w', encoding='utf-8') as f:
                f.writelines(fixed_lines)
            logger.info("配置文件格式已自动修复（添加了丢失的引号）。")
            
    except Exception as e:
        logger.error(f"尝试自动修复配置文件失败: {e}")

def save_config_file(config_path: Path, config_data: Dict[str, Any]):
    """
    统一的保存入口，保存前先转为字符串并二次处理，确保中文Key有引号。
    """
    try:
        import toml
        # 1. 先生成标准 TOML 字符串
        content = toml.dumps(config_data)
        
        # 2. 再次进行正则修复，确保万无一失
        lines = content.splitlines()
        final_lines = []
        for line in lines:
            stripped = line.strip()
            if '=' in stripped and not stripped.startswith('#') and not stripped.startswith('['):
                key_part, rest = stripped.split('=', 1)
                key_clean = key_part.strip()
                # 如果包含非ASCII且没引号
                if any(ord(c) > 127 for c in key_clean) and not (key_clean.startswith('"') or key_clean.startswith("'")):
                    line = f'"{key_clean}" ={rest}'
            final_lines.append(line)
            
        with open(config_path, 'w', encoding='utf-8') as f:
            f.write("\n".join(final_lines))
            
    except Exception as e:
        logger.error(f"保存配置文件失败: {e}")

def truncate_for_log(data: str, max_length: int = 100) -> str:
    """截断用于日志的数据，避免过长"""
    if len(data) <= max_length:
        return data
    return data[:max_length//2] + "...[truncated]..." + data[-max_length//2:]

def safe_json_dumps(obj: Any) -> str:
    """安全地序列化JSON对象，对base64数据进行截断"""
    def truncate_base64_values(o):
        if isinstance(o, dict):
            new_dict = {}
            for k, v in o.items():
                if isinstance(v, str) and ('base64' in v.lower() or len(v) > 500):
                    new_dict[k] = truncate_for_log(v)
                elif isinstance(v, (dict, list)):
                    new_dict[k] = truncate_base64_values(v)
                else:
                    new_dict[k] = v
            return new_dict
        elif isinstance(o, list):
            return [truncate_base64_values(item) for item in o]
        return o
    
    truncated_obj = truncate_base64_values(obj)
    return json.dumps(truncated_obj, ensure_ascii=False)

# --- 健壮的JSON解析函数 ---
async def extract_image_data(response_data: Dict[str, Any]) -> Optional[str]:
    try:
        if "choices" in response_data and isinstance(response_data["choices"], list) and response_data["choices"]:
            choice = response_data["choices"][0]
            content_text = None

            # Handle streaming response with 'delta'
            delta = choice.get("delta")
            if delta and "content" in delta and isinstance(delta["content"], str):
                content_text = delta["content"]
            
            # Handle non-streaming response with 'message'
            if not content_text:
                message = choice.get("message")
                if message and "content" in message and isinstance(message["content"], str):
                    content_text = message["content"]

            if content_text:
                match_url = re.search(r"!\[.*?\]\((.*?)\)", content_text)
                if match_url:
                    image_url = match_url.group(1)
                    log_url = image_url
                    if len(log_url) > 100 and "base64" in log_url:
                        log_url = log_url[:50] + "..." + log_url[-20:]
                    logger.info(f"从响应中提取到图片URL: {log_url}")
                    return image_url

                # 匹配裸露的HTTP/HTTPS URL
                match_plain_url = re.search(r"https?://[^\s]+", content_text)
                if match_plain_url:
                    image_url = match_plain_url.group(0)
                    logger.info(f"从响应中提取到裸图片URL: {image_url}")
                    return image_url

                match_b64 = re.search(r"data:image/\w+;base64,([a-zA-Z0-9+/=\n]+)", content_text)
                if match_b64:
                    return match_b64.group(1)

        candidates = response_data.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            return None

        content = candidates[0].get("content")
        if not isinstance(content, dict):
            return None

        parts = content.get("parts")
        if not isinstance(parts, list) or not parts:
            return None

        for part in parts:
            if not isinstance(part, dict):
                continue
            inline_data = part.get("inlineData") or part.get("inline_data")
            if isinstance(inline_data, dict):
                image_b64 = inline_data.get("data")
                if isinstance(image_b64, str):
                    return image_b64

            text_content = part.get("text")
            if isinstance(text_content, str):
                match = re.search(r"data:image/\w+;base64,([a-zA-Z0-9+/=\n]+)", text_content)
                if match:
                    return match.group(1)

        return None
    except Exception:
        return None

# --- API密钥管理器 ---
class KeyManager:
    def __init__(self, keys_file_path: Path = None):
        if keys_file_path is None:
            self.plugin_dir = Path(__file__).parent
            self.data_dir = self.plugin_dir / "data"
            self.data_dir.mkdir(exist_ok=True)
            self.keys_file = self.data_dir / "keys.json"
        else:
            self.keys_file = keys_file_path
            self.plugin_dir = self.keys_file.parent.parent 
            
        self.config = self._load_config()
        self._migrate_legacy_data()

    def _migrate_legacy_data(self):
        migrated = False
        old_keys_file = self.plugin_dir / "keys.json"
        if old_keys_file.exists() and old_keys_file != self.keys_file:
            try:
                with open(old_keys_file, 'r', encoding='utf-8') as f:
                    old_data = json.load(f)
                    old_keys = old_data.get('keys', [])
                    if old_keys:
                        current_keys = {k['value'] for k in self.config.get('keys', [])}
                        for k in old_keys:
                            if k['value'] not in current_keys:
                                if 'type' not in k:
                                    k['type'] = 'bailili' if k['value'].startswith('sk-') else 'google'
                                self.config['keys'].append(k)
                                migrated = True
                old_keys_file.rename(old_keys_file.with_suffix('.json.bak'))
                logger.info("已迁移旧的 keys.json 数据")
            except Exception as e:
                logger.error(f"迁移旧 keys.json 失败: {e}")

        config_path = self.plugin_dir / "config.toml"
        if config_path.exists():
            try:
                import toml
                # 尝试加载，如果这里加载失败（因为Empty key），应该直接跳过或先修复
                # 但我们在 __init__ 里已经修复了文件，所以这里应该能正常加载
                with open(config_path, 'r', encoding='utf-8') as f:
                    config_data = toml.load(f)
                
                channels = config_data.get("channels", {})
                config_changed = False
                
                for name, info in channels.items():
                    key_to_migrate = None
                    if isinstance(info, str):
                        if ":" in info:
                            possible_url, possible_key = info.rsplit(":", 1)
                            is_key = True
                            if '/' in possible_key: is_key = False
                            elif possible_key in ['generateContent', 'streamGenerateContent']: is_key = False
                            elif len(possible_key) < 10 and not possible_key.startswith('sk-'):
                                if possible_url.lower() in ['http', 'https']: is_key = False
                            
                            if is_key:
                                url = possible_url
                                key = possible_key
                                key_to_migrate = key
                                channels[name] = {"url": url, "enabled": True}
                                config_changed = True
                            else:
                                channels[name] = {"url": info, "enabled": True}
                                config_changed = True
                                
                    elif isinstance(info, dict):
                        if "key" in info:
                            key_to_migrate = info.pop("key")
                            config_changed = True
                    
                    if key_to_migrate:
                        self.add_keys([key_to_migrate], name)
                        migrated = True
                        logger.info(f"已迁移渠道 {name} 的 Key")

                if config_changed:
                    save_config_file(config_path, config_data) # 使用修复版保存
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
                key_obj = {"value": key_value, "type": key_type, "status": "active", "error_count": 0, "last_used": None, "max_errors": 5}
                self.config['keys'].append(key_obj)
                added_count += 1
        self.save_config(self.config)
        return added_count, duplicate_count

    def get_all_keys(self) -> List[Dict[str, Any]]:
        return self.config.get('keys', [])

    def get_next_api_key(self) -> Optional[Dict[str, str]]:
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
                    max_errors = key_obj.get('max_errors', 5)
                    # 当 max_errors 不为 -1 (无限) 时，才检查是否禁用
                    if max_errors != -1 and (force_disable or key_obj['error_count'] >= max_errors):
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
            if key_type and key_obj.get('type') != key_type:
                continue
            if key_obj.get('status') == 'disabled':
                key_obj['status'] = 'active'
                key_obj['error_count'] = 0
                reset_count += 1
        if reset_count > 0:
            self.save_config(self.config)
        return reset_count

    def reset_specific_key(self, key_type: str, index: int) -> bool:
        keys = self.config.get('keys', [])
        target_keys = []
        for i, key_obj in enumerate(keys):
            if key_obj.get('type') == key_type:
                target_keys.append((i, key_obj))
        
        if index < 1 or index > len(target_keys):
            return False
        real_index, key_obj = target_keys[index - 1]
        key_obj['status'] = 'active'
        key_obj['error_count'] = 0
        self.save_config(self.config)
        return True

# 初始化 KeyManager
key_manager = KeyManager()

# --- Data Manager ---
class DataManager:
    def __init__(self, data_file_path: Path = None):
        if data_file_path is None:
            self.plugin_dir = Path(__file__).parent
            self.data_dir = self.plugin_dir / "data"
            self.data_dir.mkdir(exist_ok=True)
            self.data_file = self.data_dir / "data.json"
        else:
            self.data_file = data_file_path
            self.plugin_dir = self.data_file.parent.parent
            
        self.data = self._load_data()
        self._migrate_from_toml()

    def _load_data(self) -> Dict[str, Any]:
        if not self.data_file.exists():
            return {"prompts": {}, "channels": {}}
        try:
            with open(self.data_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load data.json: {e}")
            return {"prompts": {}, "channels": {}}

    def save_data(self):
        try:
            with open(self.data_file, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, indent=4, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Failed to save data.json: {e}")

    def _migrate_from_toml(self):
        config_path = self.plugin_dir / "config.toml"
        if not config_path.exists():
            return

        try:
            import toml
            with open(config_path, 'r', encoding='utf-8') as f:
                config_data = toml.load(f)
            
            changed = False
            
            # Migrate Prompts
            if "prompts" in config_data:
                for name, prompt in config_data["prompts"].items():
                    if name not in self.data["prompts"]:
                        self.data["prompts"][name] = prompt
                        changed = True
                del config_data["prompts"]

            # Migrate Channels
            if "channels" in config_data:
                for name, info in config_data["channels"].items():
                    if name not in self.data["channels"]:
                        self.data["channels"][name] = info
                        changed = True
                del config_data["channels"]

            if changed:
                self.save_data()
                save_config_file(config_path, config_data)
                logger.info("Successfully migrated prompts and channels from config.toml to data/data.json")

        except Exception as e:
            logger.error(f"Migration from TOML failed: {e}")

    def get_prompts(self) -> Dict[str, str]:
        return self.data.get("prompts", {})

    def add_prompt(self, name: str, prompt: str):
        if "prompts" not in self.data:
            self.data["prompts"] = {}
        self.data["prompts"][name] = prompt
        self.save_data()

    def delete_prompt(self, name: str) -> bool:
        if name in self.data.get("prompts", {}):
            del self.data["prompts"][name]
            self.save_data()
            return True
        return False

    def get_channels(self) -> Dict[str, Any]:
        return self.data.get("channels", {})

    def add_channel(self, name: str, info: Dict[str, Any]):
        if "channels" not in self.data:
            self.data["channels"] = {}
        self.data["channels"][name] = info
        self.save_data()

    def delete_channel(self, name: str) -> bool:
        if name in self.data.get("channels", {}):
            del self.data["channels"][name]
            self.save_data()
            return True
        return False
        
    def update_channel(self, name: str, info: Dict[str, Any]):
         if "channels" not in self.data:
            self.data["channels"] = {}
         self.data["channels"][name] = info
         self.save_data()

data_manager = DataManager()

# --- 图像工具 ---
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

# --- 管理命令基类 ---
class BaseAdminCommand(BaseCommand, ABC):
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
        raise NotImplementedError

# --- 命令组件 (Key管理部分) ---
class ChannelAddKeyCommand(BaseAdminCommand):
    command_name: str = "gemini_channel_add_key"
    command_description: str = "添加渠道API Key (格式: /渠道添加key <渠道名称> <key1> [key2] ...)"
    command_pattern: str = r"^/渠道添加key"

    async def handle_admin_command(self) -> Tuple[bool, Optional[str], bool]:
        command_prefix = "/渠道添加key"
        content = self.message.raw_message.replace(command_prefix, "", 1).strip()
        import re
        parts = re.split(r"[\s,;，；\n\r]+", content)
        parts = [p for p in parts if p.strip()]

        if len(parts) < 2:
            await self.send_text("❌ 参数错误！\n格式：`/渠道添加key <渠道名称> <key1> [key2] ...`\n例如：`/渠道添加key google AIzaSy...` 或 `/渠道添加key PockGo sk-...`")
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
            if count > 0:
                await self.send_text(f"✅ 已成功重置所有渠道的 {count} 个失效 Key。")
            else:
                await self.send_text("ℹ️ 所有渠道均没有需要重置的 Key。")
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
            if count > 0:
                await self.send_text(f"✅ 已成功重置渠道 `{channel_name}` 的 {count} 个失效 Key。")
            else:
                await self.send_text(f"ℹ️ 渠道 `{channel_name}` 没有需要重置的 Key。")
        return True, "操作完成", True

class ChannelSetKeyErrorLimitCommand(BaseAdminCommand):
    command_name: str = "gemini_channel_set_key_error_limit"
    command_description: str = "设置Key的错误禁用上限 (格式: /渠道设置错误上限 <渠道> <序号> <次数> [-1为永不禁用])"
    command_pattern: str = r"^/渠道设置错误上限"

    async def handle_admin_command(self) -> Tuple[bool, Optional[str], bool]:
        command_prefix = "/渠道设置错误上限"
        content = self.message.raw_message.replace(command_prefix, "", 1).strip()
        parts = content.split()
        
        if len(parts) != 3:
            await self.send_text("❌ 参数错误！\n格式：`/渠道设置错误上限 <渠道名称> <序号> <次数>`\n例如：`/渠道设置错误上限 google 1 -1` (-1代表永不禁用)")
            return True, "参数不足", True

        channel_name, index_str, limit_str = parts
        
        try:
            index = int(index_str)
            limit = int(limit_str)
        except ValueError:
            await self.send_text("❌ 序号和次数必须是数字！")
            return True, "参数类型错误", True

        # Operate directly on the key_manager's config
        keys_list = key_manager.config.get('keys', [])
        target_keys_indices = [i for i, key in enumerate(keys_list) if key.get('type') == channel_name]

        if index < 1 or index > len(target_keys_indices):
            await self.send_text(f"❌ 渠道 `{channel_name}` 不存在第 `{index}` 个 Key。")
            return True, "序号无效", True
        
        real_index = target_keys_indices[index - 1]
        keys_list[real_index]['max_errors'] = limit
        
        key_manager.save_config(key_manager.config)

        limit_text = "永不禁用" if limit == -1 else f"{limit}次"
        await self.send_text(f"✅ 设置成功！\n渠道 `{channel_name}` 的第 `{index}` 个 Key 的错误上限已设置为: **{limit_text}**。")
        return True, "设置成功", True

# --- 管理命令 (Prompt管理) ---
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

        parts = re.split(r"[:：]", content, 1)
        name, prompt = parts[0].strip(), parts[1].strip()

        if not name or not prompt:
            await self.send_text("❌ 功能名称和提示词内容都不能为空！")
            return True, "参数不全", True

        try:
            if name in data_manager.get_prompts():
                await self.send_text(f"❌ 添加失败：功能名称 `{name}` 已存在，请使用其他名称。")
                return True, "名称重复", True

            data_manager.add_prompt(name, prompt)
            await self.send_text(f"✅ 提示词 `{name}` 添加成功！")
            return True, "添加成功", True
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
            if data_manager.delete_prompt(name):
                await self.send_text(f"✅ 提示词 `{name}` 删除成功！")
                return True, "删除成功", True
            else:
                await self.send_text(f"❌ 未在配置文件中找到名为 `{name}` 的提示词。")
                return True, "提示词不存在", True
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
            if ":" not in rest:
                await self.send_text(help_msg)
                return True, "格式错误", True

            name, rest_part = rest.split(':', 1)
            name = name.strip()
            url = ""
            model = None
            last_colon_index = rest_part.rfind(':')
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
                if rest_part.strip().endswith("/chat/completions"):
                     await self.send_text("❌ OpenAI 格式必须指定模型名称！\n例如：`/添加渠道 PockGo:https://.../chat/completions:gemini-1.5-pro`")
                     return True, "缺少模型", True
                if last_colon_index != -1:
                    possible_model = rest_part[last_colon_index+1:].strip()
                    possible_url = rest_part[:last_colon_index].strip()
                    if possible_url.endswith("/chat/completions"):
                        url = possible_url
                        model = possible_model
                    else:
                        await self.send_text("❌ 无法解析模型名称，请确保格式为 `URL:模型`")
                        return True, "解析失败", True
                else:
                     await self.send_text("❌ OpenAI 格式必须指定模型名称！")
                     return True, "缺少模型", True

            elif is_gemini:
                url = rest_part.strip()
                if not url.endswith(":generateContent") and "generateContent" not in url:
                     await self.send_text("❌ Gemini 格式 URL 应以 `:generateContent` 结尾！")
                     return True, "URL格式错误", True

            if not name or not url:
                await self.send_text("❌ 名称和API地址不能为空！")
                return True, "参数不全", True

            channel_info = {
                "url": url,
                "enabled": True
            }
            if model:
                channel_info["model"] = model

            data_manager.add_channel(name, channel_info)

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
        
        try:
            channels = data_manager.get_channels()
            if channel_name not in channels:
                await self.send_text(f"❌ 未找到渠道 `{channel_name}`！\n请先使用 `/添加渠道` 创建该渠道。")
                return True, "渠道不存在", True
            
            channel_info = channels[channel_name]
            old_model = channel_info.get("model", "未设置")
            url = channel_info.get("url", "")
            
            channel_info["model"] = new_model
            msg = f"✅ 渠道 `{channel_name}` 模型已更新！\n"
            msg += f"- 旧模型: `{old_model}`\n"
            msg += f"- 新模型: `{new_model}`\n"

            if "generateContent" in url and "/models/" in url:
                import re
                pattern = r"(/models/)([^:]+)(:generateContent)"
                if re.search(pattern, url):
                    new_url = re.sub(pattern, f"\\g<1>{new_model}\\g<3>", url)
                    if new_url != url:
                        channel_info["url"] = new_url
                        msg += f"- URL已自动更新: `{new_url}`\n"

            data_manager.update_channel(channel_name, channel_info)
                
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
            if data_manager.delete_channel(name):
                await self.send_text(f"✅ 渠道 `{name}` 删除成功！")
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
            channels = data_manager.get_channels()
            target_found = False
            
            # Global config handling
            if name.lower() in ['google', 'lmarena']:
                 import toml
                 config_path = Path(__file__).parent / "config.toml"
                 with open(config_path, 'r', encoding='utf-8') as f:
                     config_data = toml.load(f)
                 
                 if "api" not in config_data: config_data["api"] = {}
                 
                 if name.lower() == 'google':
                     config_data["api"]["enable_google"] = is_enable
                 else:
                     config_data["api"]["enable_lmarena"] = is_enable
                 
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
                await self.send_text(f"❌ 未找到名为 `{name}` 的渠道。")
                return True, "渠道不存在", True

            if target_found:
                action = "启用" if is_enable else "禁用"
                await self.send_text(f"✅ 渠道 `{name}` 已{action}！")
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
            channels_config = data_manager.get_channels()
            msg_lines = ["📋 **当前渠道状态列表**", "--------------------"]

            enable_google = api_config.get("enable_google", True)
            status_icon = "✅" if enable_google else "❌"
            msg_lines.append(f"{status_icon} **Google** (官方Key)")

            enable_lmarena = api_config.get("enable_lmarena", False)
            status_icon = "✅" if enable_lmarena else "❌"
            msg_lines.append(f"{status_icon} **LMArena** (免费接口)")

            if channels_config:
                msg_lines.append("--------------------")
                for name, info in channels_config.items():
                    enabled = True
                    if isinstance(info, dict):
                        enabled = info.get("enabled", True)
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

# --- 绘图命令基类 ---
class BaseDrawCommand(BaseCommand, ABC):
    permission: str = "user"

    async def get_source_image_bytes(self) -> Optional[bytes]:
        proxy = self.get_config("proxy.proxy_url") if self.get_config("proxy.enable") else None

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

        image_bytes = await _extract_image_from_segments(self.message.message_segment)
        if image_bytes:
            return image_bytes

        segments = self.message.message_segment
        if hasattr(segments, 'type') and segments.type == 'seglist':
            segments = segments.data
        if not isinstance(segments, list):
            segments = [segments]
        
        for seg in segments:
            if seg.type == 'text' and '@' in seg.data:
                match = re.search(r'(\d+)', seg.data)
                if match:
                    mentioned_user_id = match.group(1)
                    logger.info(f"在消息中找到@提及用户 {mentioned_user_id}，获取其头像。")
                    return await download_image(f"https://q1.qlogo.cn/g?b=qq&nk={mentioned_user_id}&s=640", proxy)

        # [修改] 如果允许纯文本且未找到显式图片，则直接返回 None，不使用头像回退
        if self.allow_text_only:
            logger.info("允许纯文本模式且未找到图片，跳过自动获取头像。")
            return None

        logger.info("未找到图片、Emoji或@提及，回退到发送者头像。")
        user_id = self.message.message_info.user_info.user_id
        return await download_image(f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640", proxy)

    @abstractmethod
    async def get_prompt(self) -> Optional[str]:
        raise NotImplementedError

    # 新增属性：是否允许仅文本输入
    allow_text_only: bool = False

    async def execute(self) -> Tuple[bool, Optional[str], bool]:
        if not self.get_config("general.enable_gemini_drawer", True):
            return True, "Plugin disabled", False
        start_time = datetime.now()

        prompt = await self.get_prompt()
        if not prompt:
            return True, "无效的Prompt", True

        await self.send_text("🎨 正在获取图片和指令…" if not self.allow_text_only else "🎨 正在提交绘图指令…")
        image_bytes = await self.get_source_image_bytes()
        
        if not image_bytes and not self.allow_text_only:
            await self.send_text("❌ 未找到可供处理的图片或图片处理失败。" )
            return True, "缺少图片或处理失败", True
        
        parts = []
        if image_bytes:
            image_bytes = convert_if_gif(image_bytes)
            base64_img = base64.b64encode(image_bytes).decode('utf-8')
            mime_type = get_image_mime_type(image_bytes)
            parts.append({"inline_data": {"mime_type": mime_type, "data": base64_img}})
        
        parts.append({"text": prompt})
        payload = {"contents": [{"parts": parts}]}

        await self.send_text("🤖 已提交至API…")

        endpoints_to_try = []

        if self.get_config("api.enable_lmarena", True):
            lmarena_url = self.get_config("api.lmarena_api_url", "https://chat.lmsys.org")
            lmarena_key = self.get_config("api.lmarena_api_key", "") 
            endpoints_to_try.append({
                "type": "lmarena",
                "url": lmarena_url,
                "key": lmarena_key
            })

        custom_channels = data_manager.get_channels()
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
                c_url, c_key = channel_info.rsplit(":", 1)
            
            if c_url and c_key and c_enabled:
                endpoints_to_try.append({
                    "type": f"custom_{name}",
                    "url": c_url,
                    "key": c_key,
                    "model": c_model
                })

        enable_google = self.get_config("api.enable_google", True)

        for key_info in key_manager.get_all_keys():
            if key_info.get('status') != 'active':
                continue
            
            key_type = key_info.get('type')
            if not key_type:
                key_type = 'bailili' if key_info['value'].startswith('sk-') else 'google'

            if key_type == 'google':
                if enable_google:
                    endpoints_to_try.append({
                        "type": "google",
                        "url": self.get_config("api.api_url"),
                        "key": key_info['value']
                    })
            
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

        for i, endpoint in enumerate(endpoints_to_try):
            api_url = endpoint["url"]
            api_key = endpoint["key"]
            endpoint_type = endpoint["type"]
            
            logger.info(f"尝试第 {i+1}/{len(endpoints_to_try)} 个端点: {endpoint_type} ({api_url})")

            headers = {"Content-Type": "application/json"}
            request_url = api_url

            try:
                current_payload = payload 
                client_proxy = proxy 
                
                is_openai = False
                
                if endpoint_type == 'lmarena':
                    is_openai = True
                    request_url = f"{api_url}" 
                    client_proxy = None 
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
                    if api_key:
                        headers["Authorization"] = f"Bearer {api_key}"
                    
                    user_text_prompt = ""
                    for p in parts:
                        if "text" in p:
                            user_text_prompt = p["text"]
                            break
                    
                    openai_messages = [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": user_text_prompt
                                }
                            ]
                        },
                    ]
                    
                    if image_bytes: # 只有存在图片时才添加图片部分
                        openai_messages[0]["content"].append({
                            "type": "image_url",
                            "image_url": { "url": f"data:{mime_type};base64,{base64_img}" }
                        })

                    model_name = endpoint.get("model")
                    if not model_name:
                        model_name = self.get_config("api.lmarena_model_name", "gemini-pro-vision") if endpoint_type != 'lmarena' else "gemini-3-pro-image-preview"

                    openai_payload = {
                        "model": model_name,
                        "messages": openai_messages,
                        "stream": endpoint_type == 'lmarena',
                    }
                    current_payload = openai_payload

                logger.info(f"准备向 {endpoint_type} 端点发送请求。URL: {request_url}, Payload: {safe_json_dumps(current_payload)}")
                
                img_data = None
                
                if endpoint_type == 'lmarena':
                    try:
                        async with httpx.AsyncClient(proxy=client_proxy, timeout=180.0) as client:
                            async with client.stream("POST", request_url, json=current_payload, headers=headers) as response:
                                if response.status_code != 200:
                                    raw_body = await response.aread()
                                    raise Exception(f"API请求失败, 状态码: {response.status_code} - {raw_body.decode('utf-8', 'ignore')}")

                                async for line in response.aiter_lines():
                                    line = line.strip()
                                    if not line:
                                        continue

                                    if line.startswith(':'):
                                        if 'keep-alive' in line:
                                            logger.info("Received SSE keep-alive.")
                                        else:
                                            logger.info(f"Received SSE comment: {line}")
                                        continue
                                    
                                    if line.startswith('data:'):
                                        data_str = line.replace('data:', '').strip()

                                        if data_str == "DONE" or data_str == "[DONE]":
                                            logger.info(f"LMArena SSE事件流结束 ({data_str})。")
                                            break
                                        
                                        try:
                                            response_data = json.loads(data_str)
                                            extracted_data = await extract_image_data(response_data)
                                            if extracted_data:
                                                img_data = extracted_data
                                                logger.info("从LMArena SSE流中成功提取图片数据。")
                                                break
                                        except json.JSONDecodeError:
                                            logger.warning(f"无法解析LMArena SSE data: '{data_str}', 已跳过。")
                    except httpx.RequestError as e:
                        logger.error(f"LMArena SSE 请求错误: {e}")
                        raise
                    except Exception as e:
                        logger.error(f"LMArena SSE 流处理失败: {e}")
                        raise
                
                else:
                    try:
                        async with httpx.AsyncClient(proxy=client_proxy, timeout=120.0) as client:
                            response = await client.post(request_url, json=current_payload, headers=headers)
                    except httpx.RequestError as e:
                        logger.error(f"httpx.RequestError for endpoint {endpoint_type} ({request_url}): {e}")
                        raise

                    if response.status_code == 200:
                        data = response.json()
                        img_data = await extract_image_data(data)
                        if not img_data:
                            logger.warning(f"API 响应成功但未提取到图片。响应: {json.dumps(data, indent=2, ensure_ascii=False)}")
                            raise Exception(f"API未返回图片, 原因: {data.get('candidates', [{}])[0].get('finishReason', '未知')}")
                    else:
                        raise Exception(f"API请求失败, 状态码: {response.status_code} - {response.text}")

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
                                image_bytes = await download_image(img_data, proxy)
                                if image_bytes:
                                    image_to_send_b64 = base64.b64encode(image_bytes).decode('utf-8')
                            elif 'base64,' in img_data:
                                image_to_send_b64 = img_data.split('base64,')[1]
                            else:
                                image_to_send_b64 = img_data
                            
                            if image_to_send_b64:
                                await send_api.image_to_stream(
                                    image_base64=image_to_send_b64,
                                    stream_id=stream_id,
                                    storage_message=False
                                )
                                await self.send_text(f"✅ 生成完成 ({elapsed:.2f}s)")
                            else:
                                raise Exception("图片下载或转换失败")
                        else:
                            raise Exception("无法从当前消息中确定stream_id")
                    except Exception as e:
                        logger.error(f"发送图片失败: {e}")
                        await self.send_text("❌ 图片发送失败。" )

                    return True, "绘图成功", True 

                if not img_data:
                    raise Exception("审核不通过，未能从API响应中获取图片数据")

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
    
class HelpCommand(BaseCommand):
    command_name: str = "gemini_help"
    command_description: str = "显示Gemini绘图插件的帮助信息和所有可用指令。"
    command_pattern: str = "/基咪绘图帮助"
    permission: str = "user"

    async def execute(self) -> Tuple[bool, Optional[str], bool]:
        prompts_config = data_manager.get_prompts()
        bot_name = "Gemini Drawer" # 发送人名称
        
        # 1. 头部信息
        header_text = "🎨 Gemini 绘图插件帮助 🎨\n"
        header_text += "本插件基于 Google Gemini 系列模型，提供强大的图片二次创作能力。\n"
        header_text += "--------------------\n"
        header_text += "Tip: 管理员可以使用 /添加提示词 可以动态添加新指令哦！"
        header_content = [(ReplyContentType.TEXT, header_text)]

        # 2. 用户指令与Prompts
        user_text = "✨ 用户指令 ✨\n--------------------\n"
        
        if prompts_config:
            user_text += "【预设风格】(点击指令即可复制)\n"
            sorted_prompts = sorted(prompts_config.keys())
            # 使用列表每行展示一个，清晰明了
            user_text += "\n".join([f"▪️ /{name}" for name in sorted_prompts])
            user_text += "\n\n"
        
        user_text += "【自定义风格】\n"
        user_text += "▪️ /绘图 {描述词}: 文生图，根据文字描述生成图片。\n"
        user_text += "▪️ /bnn {prompt}: 使用你的自定义prompt进行绘图。\n\n"

        user_text += "【使用方法】\n"
        user_text += "1. 回复图片 + 指令\n"
        user_text += "2. @用户 + 指令\n"
        user_text += "3. 发送图片 + 指令\n"
        user_text += "4. 直接发送指令 (使用自己头像)"
        
        user_content = [(ReplyContentType.TEXT, user_text)]
        
        nodes_to_send = [
            ("1", bot_name, header_content),
            ("1", bot_name, user_content)
        ]

        # 3. 管理员指令
        user_id_from_msg = getattr(self.message.message_info.user_info, 'user_id', None)
        admin_list = self.get_config("general.admins", [])
        str_admin_list = [str(admin) for admin in admin_list]

        if user_id_from_msg and str(user_id_from_msg) in str_admin_list:
            admin_text = "🔑 管理员指令 🔑\n--------------------\n"
            admin_text += "▪️ /渠道添加key: 添加渠道API Key\n"
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
            admin_text += "▪️ /渠道列表: 查看所有渠道状态"
            
            admin_content = [(ReplyContentType.TEXT, admin_text)]
            nodes_to_send.append(("1", bot_name, admin_content))

        await self.send_forward(nodes_to_send)
        return True, "帮助信息已发送", True

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
    # 匹配包含 " /绘图" 或以 "/绘图" 开头的消息，支持中间出现
    command_pattern: str = r".*(?:^|\s)/绘图.*"
    allow_text_only: bool = True # 允许仅文本输入

    async def get_prompt(self) -> Optional[str]:
        # 使用正则提取指令后的内容
        import re
        msg = self.message.raw_message
        
        # 查找 /绘图 及其后面的内容
        match = re.search(r"(?:^|\s)/绘图\s*(.*)", msg, re.DOTALL)
        if not match:
             return None
             
        prompt = match.group(1).strip()
        
        if not prompt:
            await self.send_text("❌ 请输入绘图描述！\n例如：`/绘图 一只可爱的小猫`")
            return None
            
        return prompt

class UniversalPromptCommand(BaseDrawCommand):
    command_name: str = "gemini_universal_prompt"
    command_description: str = "通用动态绘图指令"
    # 匹配包含 " /指令" 或以 "/指令" 开头的消息 (避免匹配 http://)
    command_pattern: str = r".*(?:^|\s)/[^/]+.*"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.current_prompt_content = None

    async def execute(self) -> Tuple[bool, Optional[str], bool]:
        # 匹配指令名称
        import re
        msg = self.message.raw_message
        logger.info(f"[Universal] 收到指令: {msg}")
        
        # 查找所有可能的指令 (必须是 /开头，前有空格或为首字符)
        potential_cmds = re.findall(r"(?:^|\s)/([^/\s]+)(?:$|\s)", msg)
        if not potential_cmds:
             return False, None, False
        
        prompts = data_manager.get_prompts()
        found_cmd = None
        
        # 遍历找到的指令，看哪个是有效的 Prompt
        for cmd in potential_cmds:
            if cmd in prompts:
                found_cmd = cmd
                break
        
        if not found_cmd:
            logger.info(f"[Universal] 未在消息中找到有效的 Prompt 指令。")
            return False, None, False
            
        # 是我的指令！
        logger.info(f"[Universal] 找到 Prompt: {found_cmd}，准备执行。")
        self.current_prompt_content = prompts[found_cmd]
        
        # 调用父类 execute (BaseDrawCommand)
        return await super().execute()

    async def get_prompt(self) -> Optional[str]:
        return self.current_prompt_content


# --- 插件注册 ---
@register_plugin
class GeminiDrawerPlugin(BasePlugin):
    plugin_name: str = "gemini_drawer"
    plugin_version: str = "1.2.0"
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
            "api_url": ConfigField(type=str, default="https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-image:generateContent", description="Google官方的Gemini API 端点"),
            "enable_lmarena": ConfigField(type=bool, default=False, description="是否启用LMArena API"),
            "lmarena_api_url": ConfigField(type=str, default="http://host.docker.internal:5102", description="LMArena API的基础URL"),
            "lmarena_api_key": ConfigField(type=str, default="", description="LMArena API密钥 (可选, 使用Bearer Token)"),
            "lmarena_model_name": ConfigField(type=str, default="gemini-2.5-flash-image-preview (nano-banana)", description="LMArena 使用的模型名称")
        }
    }

    def __init__(self, *args, **kwargs):
        # 0. 先尝试修复已存在的配置文件（如果是旧框架生成的坏文件）
        try:
            config_path = Path(__file__).parent / self.config_file_name
            if config_path.exists():
                fix_broken_toml_config(config_path)
        except Exception as e:
            pass

        # 1. 调用父类初始化（如果文件不存在，这里可能会创建它）
        super().__init__(*args, **kwargs)
        
        # 2. 再次尝试修复（如果上一步创建了坏文件，这里修复它）
        try:
            config_path = Path(__file__).parent / self.config_file_name
            if config_path.exists():
                fix_broken_toml_config(config_path)
        except Exception:
            pass

        # 3. 正常执行数据迁移
        self._migrate_config()

    def _migrate_config(self):
        try:
            import toml
        except ImportError:
            logger.error("Config Migration Failed: `toml` library not found.")
            return

        config_path = Path(__file__).parent / self.config_file_name
        
        if not config_path.exists():
            return

        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config_data = toml.load(f)

            original_config_str = toml.dumps(config_data)

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
                save_config_file(config_path, config_data) # 使用修复版保存
                logger.info("Config migration successful: config.toml has been updated.")

        except Exception as e:
            logger.error(f"Error during config migration: {e}")

    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        components: List[Tuple[ComponentInfo, Type]] = [
            (HelpCommand.get_command_info(), HelpCommand),
            (ChannelAddKeyCommand.get_command_info(), ChannelAddKeyCommand),
            (ChannelListKeysCommand.get_command_info(), ChannelListKeysCommand),
            (ChannelResetKeyCommand.get_command_info(), ChannelResetKeyCommand),
            (ChannelSetKeyErrorLimitCommand.get_command_info(), ChannelSetKeyErrorLimitCommand),
            (ChannelUpdateModelCommand.get_command_info(), ChannelUpdateModelCommand), 
            (AddPromptCommand.get_command_info(), AddPromptCommand),
            (DeletePromptCommand.get_command_info(), DeletePromptCommand),
            (AddChannelCommand.get_command_info(), AddChannelCommand),
            (DeleteChannelCommand.get_command_info(), DeleteChannelCommand),
            (ToggleChannelCommand.get_command_info(), ToggleChannelCommand),
            (ListChannelsCommand.get_command_info(), ListChannelsCommand),
            (CustomDrawCommand.get_command_info(), CustomDrawCommand),
            (TextToImageCommand.get_command_info(), TextToImageCommand),
            (UniversalPromptCommand.get_command_info(), UniversalPromptCommand),
        ]

        # prompts_config = data_manager.get_prompts()
        # 动态指令现已由 UniversalPromptCommand 统一接管，实现热重载支持
        
        return components