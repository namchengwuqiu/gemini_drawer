
import base64
import asyncio
from typing import Tuple, List, Dict, Optional, Any
from datetime import datetime

from src.plugin_system.apis import message_api
from src.plugin_system import BaseAction, ActionActivationType
from src.common.logger import get_logger

from .draw_logic import get_drawing_endpoints, process_drawing_api_request, extract_source_image
from .utils import download_image, convert_if_gif, get_image_mime_type
from .managers import key_manager

logger = get_logger("gemini_drawer_action")

def is_command_message(message: Any) -> bool:
    """检查消息是否是特定绘图指令 (/绘图, /多图, /bnn)，忽略 @mention"""
    if not message:
        return False
        
    target_commands = ["/绘图", "＃绘图", "/多图", "＃多图", "/bnn", "＃bnn"]
    
    def check_text(text: str) -> bool:
        if not text: return False
        t = text.strip()
        return any(t.startswith(cmd) for cmd in target_commands)

    try:
        # 1. 尝试基于 Segments 判断 (忽略 At 后的第一个文本段)
        if hasattr(message, 'message_segment'):
            segments = message.message_segment
            # 处理 SegList 包装
            if hasattr(segments, 'type') and segments.type == 'seglist':
                segments = segments.data
            if not isinstance(segments, list):
                segments = [segments]
            
            for seg in segments:
                if hasattr(seg, 'type') and seg.type == 'at':
                    continue
                if hasattr(seg, 'type') and seg.type == 'text':
                    data = getattr(seg, 'data', '')
                    if isinstance(data, str) and data.strip():
                        # 找到第一个非空文本段
                        return check_text(data)
    except Exception:
        pass

    # 2. 回退到基于 plain_text 判断
    try:
        msg_text = getattr(message, 'plain_text', '') or \
                   getattr(message, 'processed_plain_text', '') or \
                   getattr(message, 'display_message', '') or ''
        return check_text(msg_text)
    except Exception:
        return False

class ImageGenerateAction(BaseAction):
    """
    自然语言绘图 Action
    允许用户通过自然语言描述触发绘图功能
    """
    
    # === 基本信息 ===
    action_name = "gemini_generate_image"
    action_description = "根据用户的描述生成一张图片。当用户想要绘画、画图、生成图片时使用。"
    activation_type = ActionActivationType.ALWAYS
    
    # === 功能描述 ===
    action_parameters = {
        "prompt": "详细的图片描述，包括风格、内容、氛围等"
    }
    
    action_require = [
        "当用户明确表示想要绘画、画图、生成图片、修改图片时使用",
        "适用于'画一张xx'、'生成xx图片'、'帮我画xx'等请求",
        "不适用于用户只是在讨论某个事物，但没有明确表示想要图片的情况",
        "如果用户只是说'发张图'但没说发什么，可以尝试生成一张通用的美图"
    ]
    
    associated_types = ["image"]
    
    async def execute(self) -> Tuple[bool, str]:
        """执行绘图动作"""
        # 检查是否是指令触发
        if is_command_message(self.action_message):
             return False, "检测到指令前缀，忽略Action触发"

        prompt = self.action_data.get("prompt", "").strip()
        if not prompt:
            await self.send_text("你想画什么呢？说清楚一点嘛。")
            return False, "Prompt为空"
            
        logger.info(f"执行绘图 Action，Prompt: {prompt}")
        # await self.send_text("🎨 正在绘制中...")
        
        # 0. 尝试获取图片输入 (图生图支持)
        image_bytes = None
        mime_type = None
        proxy = self.get_config("proxy.proxy_url") if self.get_config("proxy.enable") else None

        try:
            if self.action_message:
                image_bytes = await extract_source_image(self.action_message, proxy, logger)
                if image_bytes:
                    logger.info("Action 检测到图片输入，将执行图生图模式。")
                    image_bytes = convert_if_gif(image_bytes)
                    mime_type = get_image_mime_type(image_bytes)
        except Exception as e:
            logger.warning(f"尝试提取图片输入失败: {e}")

        # 1. 准备参数
        try:
            endpoints = await get_drawing_endpoints(self.get_config)
            
            # 使用 Gemini 格式构建 payload
            parts = []
            if image_bytes:
                base64_img = base64.b64encode(image_bytes).decode('utf-8')
                parts.append({"inline_data": {"mime_type": mime_type, "data": base64_img}})
            
            parts.append({"text": prompt})

            payload = {
                "contents": [{"parts": parts}],
                "safetySettings": [
                    {
                        "category": "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                        "threshold": "BLOCK_NONE"
                    }
                ]
            }
            
            # 2. 调用核心绘图逻辑
            img_data, error = await process_drawing_api_request(
                payload=payload,
                endpoints=endpoints,
                image_bytes=image_bytes,
                mime_type=mime_type,
                proxy=proxy,
                logger=logger,
                config_getter=self.get_config
            )
            
            if img_data:
                # 3. 处理并发送图片
                image_to_send_b64 = None
                
                if img_data.startswith(('http://', 'https')):
                    # 下载 URL 图片
                    image_bytes = await download_image(img_data, proxy)
                    if image_bytes:
                        image_to_send_b64 = base64.b64encode(image_bytes).decode('utf-8')
                elif 'base64,' in img_data:
                    # 提取 Base64
                    image_to_send_b64 = img_data.split('base64,')[1]
                else:
                    # 假定是纯 Base64
                    image_to_send_b64 = img_data
                
                if image_to_send_b64:
                    await self.send_image(image_to_send_b64)
                    return True, f"成功生成并发送了关于'{prompt}'的图片"
                else:
                    await self.send_text("图片生成成功，但处理失败。")
                    return False, "图片数据处理失败"
            else:
                await self.send_text(f"绘图失败了...\n错误: {error}")
                return False, f"绘图失败: {error}"
                
        except Exception as e:
            logger.error(f"Action 绘图异常: {e}")
            await self.send_text(f"绘图过程中发生了错误: {e}")
            return False, f"异常: {e}"

class SelfieGenerateAction(BaseAction):
    action_name: str = "gemini_selfie"
    action_description: str = "发送一张自己的自拍照片"
    
    # 只需要简单的触发词监测，这里描述触发条件，Planner会进行判断
    action_require: List[str] = ["当用户明确要求看我的照片、自拍、长什么样时使用", "看看你的照片", "发张自拍"]
    activation_type: ActionActivationType = ActionActivationType.ALWAYS
    
    # 无需特定的参数提取，只需要触发即可
    action_parameters: Dict[str, Any] = {}

    async def execute(self) -> Tuple[bool, str]:
        # 检查是否是指令触发
        if is_command_message(self.action_message):
             return False, "检测到指令前缀，忽略Action触发"

        if not self.get_config("selfie.enable"):
             await self.send_text("虽然很想发，但是管理员没有开启自拍功能哦。")
             return True, "自拍功能未启用"

        image_filename = self.get_config("selfie.reference_image_path")
        # 自动定位到插件目录下的 images 文件夹
        from pathlib import Path
        plugin_dir = Path(__file__).parent
        ref_image_path = plugin_dir / "images" / image_filename
        
        if not ref_image_path.exists():
            await self.send_text("糟糕，我找不到我的底图了，可能被管理员删掉了。")
            logger.warning(f"Selfie reference image not found at: {ref_image_path}")
            return False, "未找到人设底图"

        try:
            with open(ref_image_path, "rb") as f:
                image_bytes = f.read()

            base_prompt = self.get_config("selfie.base_prompt")
            random_actions = self.get_config("selfie.random_actions")
            
            # 随机选择一个动作
            import random
            action = random.choice(random_actions) if random_actions else "looking at viewer"
            
            if base_prompt:
                full_prompt = f"{base_prompt}, {action}"
            else:
                full_prompt = action
            
            # 使用 process_drawing_api_request 进行绘图 (图生图模式)
            logger.info(f"Generating selfie with prompt: {full_prompt}")
            
            # 获取 endpoints
            from .draw_logic import get_drawing_endpoints
            endpoints = await get_drawing_endpoints(self.get_config)
            
            # 构建 payload (Gemini 格式)
            mime_type = get_image_mime_type(image_bytes)
            b64_img = base64.b64encode(image_bytes).decode('utf-8')
            
            payload = {
                "contents": [{
                    "parts": [
                        {"text": full_prompt},
                        {
                            "inline_data": {
                                "mime_type": mime_type,
                                "data": b64_img
                            }
                        }
                    ]
                }]
            }
            
            # 获取 proxy
            proxy = self.get_config("proxy.proxy_url") if self.get_config("proxy.enable") else None
            
            await self.send_text("好吧，你这么想看那我就给你一张，等一下...")
            
            # 调用绘图逻辑
            img_data, error = await process_drawing_api_request(
                payload=payload,
                endpoints=endpoints,
                image_bytes=image_bytes,
                mime_type=mime_type,
                proxy=proxy,
                logger=logger,
                config_getter=self.get_config
            )
            
            if img_data:
                image_to_send_b64 = None
                
                # 处理不同格式的图片数据
                if img_data.startswith(('http://', 'https')):
                    # 下载 URL 图片
                    image_bytes = await download_image(img_data, proxy)
                    if image_bytes:
                        image_to_send_b64 = base64.b64encode(image_bytes).decode('utf-8')
                elif img_data.startswith('data:image') and 'base64,' in img_data:
                    # 提取 data URL 中的 Base64 部分
                    image_to_send_b64 = img_data.split('base64,')[1]
                else:
                    image_to_send_b64 = img_data
                
                if image_to_send_b64:
                    await self.send_image(image_to_send_b64)
                    return True, "成功发送自拍"
                else:
                    await self.send_text("自拍生成了，但是处理出错了。")
                    return False, "数据处理失败"
            else:
                await self.send_text(f"自拍生成失败了: {error}")
                return False, f"生成失败: {error}"

        except Exception as e:
            logger.error(f"Selfie Action Error: {e}")
            await self.send_text(f"处理自拍时发生了错误: {e}")
            return False, str(e)
