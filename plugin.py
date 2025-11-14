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

# --- 全局常量 ---
PLUGIN_DATA_DIR = Path(f"data/gemini_drawer")
KEYS_FILE = PLUGIN_DATA_DIR / "keys.json"

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
                    logger.info(f"从LMArena响应中提取到图片URL: {image_url}")
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
    def __init__(self, keys_file_path: Path):
        self.keys_file = keys_file_path
        self.config = self._load_config()

    def _load_config(self) -> Dict[str, Any]:
        try:
            if not self.keys_file.exists():
                self.keys_file.parent.mkdir(parents=True, exist_ok=True)
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

    def add_keys(self, new_keys: List[str]) -> Tuple[int, int]:
        existing_keys = {key['value'] for key in self.config.get('keys', [])}
        added_count = 0
        duplicate_count = 0
        for key_value in new_keys:
            if key_value in existing_keys:
                duplicate_count += 1
            else:
                key_type = 'bailili' if key_value.startswith('sk-') else 'google'
                key_obj = {"value": key_value, "type": key_type, "status": "active", "error_count": 0, "last_used": None}
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
                key_type = key_obj.get('type', 'bailili' if key_obj['value'].startswith('sk-') else 'google')
                return {"value": key_obj['value'], "type": key_type}
        return None

    def record_key_usage(self, key_value: str, success: bool):
        keys = self.config.get('keys', [])
        for key_obj in keys:
            if key_obj['value'] == key_value:
                if success:
                    key_obj['error_count'] = 0
                else:
                    key_obj['error_count'] = key_obj.get('error_count', 0) + 1
                    if key_obj['error_count'] >= 5:
                        key_obj['status'] = 'disabled'
                        logger.warning(f"API Key {key_value[:8]}... 已被自动禁用")
                self.save_config(self.config)
                return

    def manual_reset_keys(self) -> int:
        keys = self.config.get('keys', [])
        reset_count = 0
        for key_obj in keys:
            if key_obj.get('status') == 'disabled':
                key_obj['status'] = 'active'
                key_obj['error_count'] = 0
                reset_count += 1
        if reset_count > 0:
            self.save_config(self.config)
        return reset_count

key_manager = KeyManager(KEYS_FILE)

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
class AddKeysCommand(BaseAdminCommand):
    command_name: str = "gemini_add_keys"
    command_description: str = "添加一个或多个Gemini API Key"
    command_pattern: str = "/手办化添加key"

    async def handle_admin_command(self) -> Tuple[bool, Optional[str], bool]:
        command_prefix = "/手办化添加key"
        raw_keys = self.message.raw_message.replace(command_prefix, "", 1)

        raw_keys = raw_keys.strip()
        if not raw_keys:
            await self.send_text("❌ 请提供API密钥\n\n📝 使用方法：\n/手办化添加key <密钥1> [密钥2]...")
            return True, "缺少参数", True

        keys = re.split(r"[\s,;，；\n\r]+", raw_keys)
        valid_keys = [k for k in keys if k and k.strip()]

        if not valid_keys:
            await self.send_text("❌ 未检测到有效的API密钥。")
            return True, "无效参数", True

        added, duplicate = key_manager.add_keys(valid_keys)
        reply = f"✅ 操作完成:\n- 成功添加 {added} 个新密钥。\n- 跳过 {duplicate} 个重复密钥。"
        await self.send_text(reply)
        return True, "添加密钥成功", True

class ListKeysCommand(BaseAdminCommand):
    command_name: str = "gemini_list_keys"
    command_description: str = "查看已添加的API Key列表"
    command_pattern: str = "/手办化key列表"

    async def handle_admin_command(self) -> Tuple[bool, Optional[str], bool]:
        all_keys = key_manager.get_all_keys()
        if not all_keys:
            await self.send_text("📝 当前没有配置任何API密钥。")
            return True, "列表为空", True

        reply_lines = ["📝 API密钥列表:"]
        for i, key in enumerate(all_keys):
            key_type = key.get('type', 'bailili' if key['value'].startswith('sk-') else 'google')
            masked_key = key['value'][:8] + '...' 
            status_icon = '✅' if key['status'] == 'active' else '❌'
            reply_lines.append(f"{i+1}. {masked_key} ({key_type}) | 状态: {status_icon} | 连续错误: {key['error_count']}")
        
        await self.send_text("\n".join(reply_lines))
        return True, "获取列表成功", True

class ResetKeysCommand(BaseAdminCommand):
    command_name: str = "gemini_reset_keys"
    command_description: str = "手动重置所有失效的API Key"
    command_pattern: str = "/手办化手动重置key"

    async def handle_admin_command(self) -> Tuple[bool, Optional[str], bool]:
        reset_count = key_manager.manual_reset_keys()
        if reset_count > 0:
            await self.send_text(f"✅ 操作完成：已手动重置 {reset_count} 个失效的密钥。")
        else:
            await self.send_text("ℹ️ 没有检测到状态为“禁用”的密钥，无需重置。")
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
        enable_lmarena = self.get_config("api.enable_lmarena", True)
        lmarena_url = self.get_config("api.lmarena_api_url")
        lmarena_key = self.get_config("api.lmarena_api_key")

        # 首先添加特殊的 lmarena 端点
        if enable_lmarena and lmarena_url:
            endpoints_to_try.append({
                "type": "lmarena",
                "url": lmarena_url,
                "key": lmarena_key
            })

        # 然后添加所有从 key_manager 获取的常规密钥
        for key_info in key_manager.get_all_keys():
            if key_info.get('status') == 'active':
                key_type = key_info.get('type', 'bailili' if key_info['value'].startswith('sk-') else 'google')
                if key_type == 'google':
                    api_url = self.get_config("api.api_url")
                else: # bailili
                    api_url = self.get_config("api.bailili_api_url")
                
                endpoints_to_try.append({
                    "type": key_type,
                    "url": api_url,
                    "key": key_info['value']
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

                if endpoint_type == 'lmarena':
                    request_url = f"{api_url}/v1/chat/completions"
                    if api_key: # 只有存在key时才添加Authorization头
                        headers["Authorization"] = f"Bearer {api_key}"
                    headers["Content-Type"] = "application/json" # 确保Content-Type为application/json
                    
                    # 构造LMArena特定的payload
                    lmarena_messages = []
                    for part in parts:
                        if "inline_data" in part:
                            lmarena_messages.append({"role": "user", "content": [{"type": "image_url", "image_url": {"url": f"data:{part["inline_data"]["mime_type"]};base64,{part["inline_data"]["data"]}"}}]})
                        elif "text" in part:
                            lmarena_messages.append({"role": "user", "content": part["text"]})
                    
                    lmarena_payload = {
                        "model": self.get_config("api.lmarena_model_name", "gemini-2.5-flash-image-preview (nano-banana)"),
                        "messages": lmarena_messages,
                        "n": 1
                    }
                    current_payload = lmarena_payload
                    client_proxy = None # Disable proxy for local lmarena connection
                else: # 对于 google 和 bailili，将key作为查询参数
                    request_url = f"{api_url}?key={api_key}"

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
                    key_manager.record_key_usage(api_key, False)
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

        reply_lines.append("\n--------------------")
        reply_lines.append("🔑 管理员指令 🔑")
        reply_lines.append("  - `/手办化添加key`: 添加API Key")
        reply_lines.append("  - `/手办化key列表`: 查看所有Key的状态")
        reply_lines.append("  - `/手办化手动重置key`: 重置所有失效的Key")
        reply_lines.append("  - `/添加提示词`: 添加自定义绘图风格")
        reply_lines.append("  - `/删除提示词`: 删除自定义绘图风格")
        
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
            "api_url": ConfigField(type=str, default="https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-image:generateContent", description="Google官方的Gemini API 端点"),
            "bailili_api_url": ConfigField(type=str, default="https://newapi.sisuo.de/v1beta/models/gemini-2.5-flash-image-preview-free:generateContent", description="Bailili等第三方兼容API端点"),
            "enable_lmarena": ConfigField(type=bool, default=True, description="是否启用LMArena API"),
            "lmarena_api_url": ConfigField(type=str, default="http://host.docker.internal:5102", description="LMArena API的基础URL (例如: http://host.docker.internal:5102, 如果在Docker中运行)"),
            "lmarena_api_key": ConfigField(type=str, default="", description="[新增]特殊的LMArena API密钥 (可选, 使用Bearer Token)"),
            "lmarena_model_name": ConfigField(type=str, default="gemini-2.5-flash-image-preview (nano-banana)", description="LMArena 使用的模型名称")
        },
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
            (AddKeysCommand.get_command_info(), AddKeysCommand),
            (ListKeysCommand.get_command_info(), ListKeysCommand),
            (ResetKeysCommand.get_command_info(), ResetKeysCommand),
            (AddPromptCommand.get_command_info(), AddPromptCommand),
            (DeletePromptCommand.get_command_info(), DeletePromptCommand),
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