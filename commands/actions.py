import base64
import random
from pathlib import Path
from typing import Tuple, List, Dict, Optional, Any

from maibot_sdk.compat.base import BaseAction, ActionActivationType

from ..config import DEFAULT_SELFIE_BASE_PROMPT, DEFAULT_SELFIE_POLISH_TEMPLATE
from ..core.host_bridge import get_plugin_logger

from ..core.endpoints import build_drawing_endpoints
from ..core.image_source import extract_source_image
from ..core.guards import evaluate_access
from ..core.pipeline import run_drawing
from ..providers import DrawRequest
from ..utils import download_image, convert_if_gif, get_image_mime_type

logger = get_plugin_logger("plugin.gemini_drawer.action")

#: 这些前缀由专门的 Command 处理，Action 不应重复触发
COMMAND_PREFIXES = ("/绘图", "＃绘图", "/多图", "/bnn", "/文生视频", "/图生视频", "/+")


def is_command_message(message: Any) -> bool:
    """检查消息是否是特定绘图指令 (/绘图, /多图, /bnn)，忽略 @mention"""
    if not message:
        return False

    def check_text(text: str) -> bool:
        return bool(text) and text.strip().startswith(COMMAND_PREFIXES)

    try:
        # 1. 尝试基于 Segments 判断 (忽略 At 后的第一个文本段)
        if hasattr(message, 'message_segment'):
            from ..core.image_source import normalize_segments

            for seg in normalize_segments(message.message_segment):
                seg_type = getattr(seg, 'type', None)
                if seg_type == 'at':
                    continue
                if seg_type == 'text':
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


class _DrawActionMixin:
    """Action 侧共享的准入校验、绘图与发图逻辑。"""

    def _proxy(self) -> Optional[str]:
        return self.get_config("proxy.proxy_url") if self.get_config("proxy.enable") else None

    def _target_stream_id(self) -> str:
        return str(getattr(self, "_stream_id", "") or self.chat_id or "").strip()

    async def _send_text(self, text: str) -> bool:
        """统一走 ctx.send.text，避免混用已弃用的 BaseAction.send_text()。"""
        message = str(text or "").strip()
        stream_id = self._target_stream_id()
        if not message or not stream_id or not getattr(self, "ctx", None):
            return False
        try:
            await self.ctx.send.text(message, stream_id)
            return True
        except Exception as exc:
            logger.warning(f"发送{self.action_name}文本消息失败: {exc}")
            return False

    async def _send_wait_message(self) -> None:
        await self._send_text(getattr(self, "wait_message", ""))

    async def _send_image_data(self, image_b64: str) -> bool:
        """通过原生发送能力发图，并读取宿主返回的真实发送状态。"""
        stream_id = self._target_stream_id()
        if not stream_id or not getattr(self, "ctx", None):
            return False
        try:
            result = await self.ctx.send.image(
                image_b64,
                stream_id,
                return_details=True,
            )
        except Exception as exc:
            logger.error(f"图片发送失败: {exc}")
            return False

        if isinstance(result, dict):
            return bool(result.get("success") and result.get("sent", True))
        return bool(result)

    def _precheck(self, feature: str) -> Optional[Tuple[bool, str]]:
        """统一的 Action 准入校验；返回 None 表示放行。"""
        decision = evaluate_access(
            self.get_config, group_id=self.group_id, user_id=self.user_id
        )
        if not decision.allowed:
            logger.info(f"拒绝执行{feature} Action: {decision.reason}")
            return False, decision.reason

        if is_command_message(self.action_message):
            return False, "检测到指令前缀，忽略Action触发"

        return None

    async def _draw(self, prompt: str, images: List[bytes]) -> Tuple[List[str], str]:
        images = [convert_if_gif(img) for img in images]
        request = DrawRequest(
            prompt=prompt,
            images=images,
            mime_types=[get_image_mime_type(img) for img in images],
        )
        return await run_drawing(
            request=request,
            endpoints=build_drawing_endpoints(),
            proxy=self._proxy(),
            logger=logger,
            debug_mode=self.get_config("behavior.debug_mode", False),
        )

    async def _send_images(self, img_data: List[str]) -> int:
        """把 pipeline 返回的图片数据发出去，返回成功发送的张数。"""
        proxy = self._proxy()
        sent_count = 0
        for raw in img_data:
            if raw.startswith(('http://', 'https://')):
                downloaded = await download_image(raw, proxy)
                image_b64 = base64.b64encode(downloaded).decode('utf-8') if downloaded else None
            elif 'base64,' in raw:
                image_b64 = raw.split('base64,', 1)[1]
            else:
                image_b64 = raw

            if not image_b64:
                continue
            sent = await self._send_image_data(
                image_b64.replace('\n', '').replace('\r', '').replace(' ', '')
            )
            if sent:
                sent_count += 1
            else:
                logger.warning("图片发送接口返回失败，未计入已发送数量")
        return sent_count


class ImageGenerateAction(_DrawActionMixin, BaseAction):
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
        "不适用于用户要求生成文字内容（如人设描述、角色设定、故事、文案等），只适用于生成视觉图像",
        "当用户说'生成人设'、'写个人设'、'来个人设'时，通常是指文字角色设定，不是图片，除非明确提到'画'或'图'",
        "用户让别人或AI去做某事（如'叫ai给你生成xx'）属于建议或讨论，不是对本bot的绘图指令，不应触发",
        "如果用户只是说'发张图'但没说发什么，可以尝试生成一张通用的美图",
        "注意：如果遇到/绘图、/bnn、/多图、/+，这种带斜杠的指令消息，不要再调用此Action",
        "注意：不要连续触发，如果刚刚已经发送过图片或正在生成中，就不要再次触发此动作，除非用户再次主动要求"
    ]

    associated_types = ["image"]

    async def execute(self) -> Tuple[bool, str]:
        """执行绘图动作"""
        blocked = self._precheck("绘图")
        if blocked:
            return blocked

        # 运行时参数在 action_data 中；action_parameters 是类级 schema 描述，不可用于取值
        prompt = (self.action_data.get('prompt') or '').strip()
        if not prompt:
            return False, "没有提供绘图提示词"

        await self._send_wait_message()
        return await self._draw_and_send(prompt)

    async def _draw_and_send(self, prompt: str) -> Tuple[bool, str]:
        logger.info(f"执行绘图 Action，Prompt: {prompt}")

        try:
            # 有图则走图生图
            images = []
            if self.action_message:
                try:
                    source = await extract_source_image(
                        self.action_message, self._proxy(), logger, getattr(self, 'ctx', None)
                    )
                    if source:
                        logger.info("Action 检测到图片输入，将执行图生图模式。")
                        images.append(source)
                except Exception as e:
                    logger.warning(f"尝试提取图片输入失败: {e}")

            img_data, error = await self._draw(prompt, images)

            if not img_data:
                await self._send_text(f"绘图失败了...\n错误: {error}")
                return False, f"绘图失败: {error}"

            if await self._send_images(img_data) == 0:
                await self._send_text("图片生成成功，但处理失败。")
                return False, "图片生成成功，但发送失败"

            return True, "图片已经生成并发送"

        except Exception as e:
            logger.error(f"Action 绘图异常: {e}")
            await self._send_text(f"绘图过程中发生了错误: {e}")
            return False, f"绘图过程中发生错误: {e}"


class _SelfieActionBase(_DrawActionMixin, BaseAction):
    """自拍类工具的共享逻辑：底图定位、提示词润色与同步执行。"""

    #: 润色模板的配置键
    polish_template_key: str = "selfie.polish_template"
    polish_template_default: str = ""
    #: 润色结果的图片引导前缀
    polish_prefix: str = "根据图中人物按以下要求生成图片"
    #: LLM 请求类型标签
    polish_request_type: str = "gemini_drawer.selfie_polish"
    #: 功能名（日志与提示用）
    feature_name: str = "自拍"

    def _identity_prompt(self) -> str:
        """Return the configured identity constraint, falling back for legacy blank configs."""
        configured = self.get_config("selfie.base_prompt", "")
        return str(configured or DEFAULT_SELFIE_BASE_PROMPT).strip()

    async def _polish_prompt(self, original_prompt: str) -> str:
        """使用 LLM 模型润色提示词，失败时原样返回。"""
        if not self.get_config("selfie.polish_enable", False):
            return original_prompt

        try:
            if not getattr(self, 'ctx', None):
                logger.warning("当前上下文不支持模型调用，使用原始提示词")
                return original_prompt

            model_name = self.get_config("selfie.polish_model", "replyer")
            available_models = await self.ctx.llm.get_available_models()
            if model_name not in available_models:
                logger.warning(f"润色模型 '{model_name}' 不存在，使用原始提示词")
                return original_prompt

            template = self.get_config(self.polish_template_key, "") or self.polish_template_default
            logger.info(f"正在润色{self.feature_name}提示词: {original_prompt}")
            result = await self.ctx.llm.generate(
                prompt=template.format(original_prompt=original_prompt),
                model=model_name,
                request_type=self.polish_request_type,
                temperature=0.5,
                max_tokens=512,
            )

            if result.get("success") and result.get("response"):
                final_prompt = f"{self.polish_prefix}：{result['response'].strip()}"
                identity_prompt = self._identity_prompt()
                if identity_prompt and identity_prompt not in final_prompt:
                    final_prompt = f"{final_prompt}\n\n身份与画风约束：{identity_prompt}"
                logger.debug(f"润色完成: {original_prompt} -> {final_prompt}")
                return final_prompt

            logger.warning("润色失败，使用原始提示词")
            return original_prompt

        except Exception as e:
            logger.error(f"润色提示词时出错: {e}")
            return original_prompt

    def _reference_image_path(self) -> Optional[Path]:
        """定位人设底图；不存在时返回 None。"""
        image_filename = self.get_config("selfie.reference_image_path")
        path = Path(__file__).parent.parent / "assets" / image_filename
        return path if path.exists() else None

    def _compose_prompt(self, user_action: str, random_pool: List[str], fallback: str) -> str:
        if user_action:
            action = user_action
            logger.info(f"使用用户指定的动作: {action}")
        else:
            action = random.choice(random_pool) if random_pool else fallback
            logger.info(f"随机选择动作: {action}")

        base_prompt = self._identity_prompt()
        return f"{base_prompt}, {action}" if base_prompt else action

    async def _run_generation(self, coro_factory) -> Tuple[bool, str]:
        """统一执行前置校验，并等待媒体生成和发送完成。"""
        blocked = self._precheck(self.feature_name)
        if blocked:
            return blocked

        if not self.get_config("selfie.enable"):
            await self._send_text("虽然很想发，但是管理员没有开启自拍功能哦。")
            return True, "自拍功能未启用"

        ref_image_path = self._reference_image_path()
        if ref_image_path is None:
            await self._send_text("糟糕，我找不到我的底图了，可能被管理员删掉了。")
            logger.warning(
                f"Selfie reference image not found: {self.get_config('selfie.reference_image_path')}"
            )
            return False, "未找到人设底图"

        try:
            user_action = (self.action_data.get("requested_action") or "").strip()
            await self._send_wait_message()
            return await coro_factory(ref_image_path, user_action)
        except Exception as e:
            logger.error(f"{self.feature_name} Tool Error: {e}")
            await self._send_text(f"处理{self.feature_name}时发生了错误: {e}")
            return False, str(e)


class SelfieGenerateAction(_SelfieActionBase):
    action_name: str = "gemini_selfie"
    action_description: str = "发送一张自己的自拍照片"

    # 只需要简单的触发词监测，这里描述触发条件，Planner会进行判断
    action_require: List[str] = [
        "当用户明确要求看我的照片、自拍、长什么样时使用",
        "看看你的照片", "发张自拍",
        "注意：不要连续发，如果刚刚已经发送过自拍或正在生成中，就不要再次触发此动作"
    ]
    activation_type: ActionActivationType = ActionActivationType.ALWAYS

    # 参数定义：让 Planner 从用户消息中提取完整场景描述
    action_parameters: Dict[str, Any] = {
        "requested_action": "用户请求的完整场景描述（包括服装、动作、姿势、场景等），如'穿女仆装比心'、'戴眼镜做鬼脸'、'在海边挥手'等。需要完整提取用户的要求，不要只提取单个动作词。如果用户没有指定具体场景，返回空字符串。"
    }

    feature_name = "自拍"
    polish_template_key = "selfie.polish_template"
    polish_template_default = DEFAULT_SELFIE_POLISH_TEMPLATE
    polish_prefix = "根据图中人物按以下要求生成图片"
    polish_request_type = "gemini_drawer.selfie_polish"

    async def execute(self) -> Tuple[bool, str]:
        return await self._run_generation(self._do_selfie)

    async def _do_selfie(self, ref_image_path: Path, user_action: str) -> Tuple[bool, str]:
        try:
            image_bytes = ref_image_path.read_bytes()

            full_prompt = self._compose_prompt(
                user_action,
                self.get_config("selfie.random_actions") or [],
                "looking at viewer",
            )
            full_prompt = await self._polish_prompt(full_prompt)

            logger.info(f"Generating selfie with prompt: {full_prompt}")
            img_data, error = await self._draw(full_prompt, [image_bytes])

            if not img_data:
                await self._send_text(f"自拍生成失败了: {error}")
                return False, f"自拍生成失败: {error}"

            if await self._send_images(img_data) == 0:
                await self._send_text("自拍生成了，但是处理出错了。")
                return False, "自拍生成成功，但发送失败"

            return True, "自拍已经生成并发送"

        except Exception as e:
            logger.error(f"Selfie Tool Error: {e}")
            await self._send_text(f"处理自拍时发生了错误: {e}")
            return False, f"处理自拍时发生错误: {e}"


class SelfieVideoAction(_SelfieActionBase):
    """
    发送自己的视频动作
    类似自拍功能，但生成视频而非图片
    """
    action_name: str = "gemini_selfie_video"
    action_description: str = "发送一段自己的视频"

    action_require: List[str] = [
        "当用户明确要求看我的视频、动态、动作时使用",
        "发个视频看看", "想看你跳舞", "来段视频",
        "注意：不要连续发，如果刚刚已经发送过视频或正在生成中，就不要再次触发此动作"
    ]
    activation_type: ActionActivationType = ActionActivationType.ALWAYS

    action_parameters: Dict[str, Any] = {
        "requested_action": "用户请求的完整视频场景描述（包括服装、动作、场景等），如'穿女仆装跳舞'、'在海边挥手'、'穿JK转圈'、'做鬼脸眨眼'等。需要完整提取用户的要求，不要只提取单个动作词。如果用户没有明确指定场景，返回空字符串。"
    }

    feature_name = "自拍视频"
    polish_template_key = "selfie.video_polish_template"
    polish_template_default = (
        "请将以下视频动作描述润色为更适合AI视频生成的提示词，让动作描述更加流畅、生动、有画面感。"
        "只输出润色后的一份提示词，不要输出其他内容。原始描述：'{original_prompt}'"
    )
    polish_prefix = "根据图中人物按以下要求生成视频"
    polish_request_type = "gemini_drawer.selfie_video_polish"

    DEFAULT_VIDEO_ACTIONS = [
        "缓缓转头，露出微笑",
        "轻轻挥手打招呼",
        "眨眼并微微歪头",
        "点头微笑",
        "比耶手势",
    ]

    async def execute(self) -> Tuple[bool, str]:
        return await self._run_generation(self._do_video)

    async def _do_video(self, ref_image_path: Path, user_action: str) -> Tuple[bool, str]:
        try:
            from ..core.endpoints import build_video_endpoints
            from ..core.video import process_video_generation, send_video_via_napcat

            image_bytes = ref_image_path.read_bytes()

            full_prompt = self._compose_prompt(
                user_action,
                self.get_config("selfie.video_actions", self.DEFAULT_VIDEO_ACTIONS),
                "looking at camera",
            )
            full_prompt = await self._polish_prompt(full_prompt)
            logger.info(f"Generating selfie video with prompt: {full_prompt}")

            endpoints = build_video_endpoints(logger=logger)
            if not endpoints:
                await self._send_text("❌ 没有配置视频生成渠道，无法录制视频。")
                return False, "没有配置视频生成渠道"

            video_data, error = await process_video_generation(
                prompt=full_prompt,
                base64_img=base64.b64encode(image_bytes).decode('utf-8'),
                mime_type=get_image_mime_type(image_bytes),
                endpoints=endpoints,
                proxy=self._proxy(),
                logger=logger,
                debug_mode=self.get_config("behavior.debug_mode", False),
            )

            if not video_data:
                await self._send_text(f"视频生成失败了: {error}")
                return False, f"视频生成失败: {error}"

            success, send_error = await send_video_via_napcat(
                video_base64=video_data,
                group_id=self.group_id,
                user_id=self.user_id,
                napcat_host=self.get_config("api.napcat_host", "napcat"),
                napcat_port=self.get_config("api.napcat_port", 3033),
                logger=logger,
            )

            if success:
                await self._send_text("当当当！专门为你拍的视频来啦，快夸夸我！(≧▽≦)✨")
                return True, "自拍视频已经生成并发送"
            else:
                await self._send_text(f"❌ 视频发送失败: {send_error}")
                return False, f"视频发送失败: {send_error}"

        except Exception as e:
            logger.error(f"Selfie Video Tool Error: {e}")
            await self._send_text(f"录制视频时发生了错误: {e}")
            return False, f"录制视频时发生错误: {e}"
