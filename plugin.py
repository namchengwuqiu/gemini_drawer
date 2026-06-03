from typing import Any, Tuple, Optional, Type
from pathlib import Path
import re
import base64
from maibot_sdk import Action, Command, MaiBotPlugin
from maibot_sdk.types import ActivationType
from maibot_sdk.context import PluginContext

from .config import GeminiDrawerConfig

from .help_command import HelpCommand
from .draw_commands import (
    CustomDrawCommand, TextToImageCommand, UniversalPromptCommand,
    MultiImageDrawCommand, RandomPromptDrawCommand, VideoGenerateCommand,
    TextToVideoCommand
)
from .admin_commands import (
    ChannelAddKeyCommand, ChannelListKeysCommand, ChannelResetKeyCommand,
    ChannelDeleteKeyCommand, ChannelSetKeyErrorLimitCommand, ChannelUpdateModelCommand,
    AddPromptCommand, DeletePromptCommand, ViewPromptCommand, ModifyPromptCommand,
    AddChannelCommand, DeleteChannelCommand, ToggleChannelCommand,
    ListChannelsCommand, ChannelSetStreamCommand, ChannelSetVideoCommand
)
from .actions import ImageGenerateAction, SelfieGenerateAction, SelfieVideoAction


DRAW_COMMAND_TIMEOUT_MS = 300_000
VIDEO_COMMAND_TIMEOUT_MS = 600_000


# ── Compatibility wrapper classes ──

class CompatUserInfo:
    def __init__(self, user_id: str, nickname: str, cardname: str = None):
        self.user_id = str(user_id) if user_id is not None else ""
        self.user_nickname = nickname or ""
        self.user_cardname = cardname

class CompatGroupInfo:
    def __init__(self, group_id: str, group_name: str):
        self.group_id = str(group_id) if group_id is not None else ""
        self.group_name = group_name or ""

class CompatMessageInfo:
    def __init__(self, user_info: CompatUserInfo, group_info: CompatGroupInfo = None, additional_config: dict = None):
        self.user_info = user_info
        self.group_info = group_info
        self.additional_config = additional_config or {}

class CompatMessageSegment:
    def __init__(self, seg_type: str, data: Any):
        self.type = seg_type
        self.data = data

class CompatChatStream:
    def __init__(self, stream_id: str, platform: str, user_info=None, group_info=None):
        self.stream_id = stream_id
        self.platform = platform
        self.user_info = user_info
        self.group_info = group_info

class CompatMessageString(str):
    @property
    def components(self):
        return []

class CompatMessage:
    def deepcopy(self):
        import copy
        return copy.deepcopy(self)

    def __init__(self, raw_data: Any):
        if raw_data is not None and not isinstance(raw_data, dict):
            if hasattr(raw_data, "model_dump"):
                try:
                    raw_data = raw_data.model_dump()
                except Exception:
                    pass
            elif hasattr(raw_data, "dict"):
                try:
                    raw_data = raw_data.dict()
                except Exception:
                    pass

        self._raw_data = raw_data

        def _get_val(keys, default=None):
            if not isinstance(raw_data, dict):
                return default
            for k in keys:
                if k in raw_data:
                    return raw_data[k]
            return default

        # 1. Basic text and ID properties
        self.message_id = str(_get_val(["message_id", "id", "session_id"], ""))
        self.session_id = str(_get_val(["session_id", "stream_id", "message_id"], ""))
        self.processed_plain_text = _get_val(["processed_plain_text", "plain_text", "display_message"], "")
        self.platform = _get_val(["platform"], "qq")

        ts_val = _get_val(["time", "timestamp"])
        if ts_val:
            try:
                from datetime import datetime
                self.timestamp = datetime.fromtimestamp(float(ts_val))
            except Exception:
                from datetime import datetime
                self.timestamp = datetime.now()
        else:
            from datetime import datetime
            self.timestamp = datetime.now()

        # 2. Re-construct message_info
        msg_info = _get_val(["message_info", "message_base_info"], {})
        if not isinstance(msg_info, dict):
            msg_info = {}

        u_info = msg_info.get("user_info", {})
        if not isinstance(u_info, dict):
            u_info = {}

        user_id = u_info.get("user_id") or msg_info.get("user_id") or _get_val(["user_id"], "")
        user_nickname = u_info.get("user_nickname") or msg_info.get("user_nickname") or _get_val(["user_nickname"], "")
        user_cardname = u_info.get("user_cardname") or msg_info.get("user_cardname") or _get_val(["user_cardname"], None)

        compat_user = CompatUserInfo(user_id, user_nickname, user_cardname)

        g_info = msg_info.get("group_info", None)
        if not isinstance(g_info, dict):
            g_info = {}

        group_id = None
        group_name = ""
        if g_info and g_info.get("group_id"):
            group_id = g_info.get("group_id")
            group_name = g_info.get("group_name", "")
        elif "group_id" in msg_info and msg_info.get("group_id"):
            group_id = msg_info.get("group_id")
            group_name = msg_info.get("group_name", "")
        elif _get_val(["group_id"]):
            group_id = _get_val(["group_id"])
            group_name = _get_val(["group_name"], "")

        compat_group = None
        if group_id:
            compat_group = CompatGroupInfo(group_id, group_name)

        additional_config = msg_info.get("additional_config", {})
        self.message_info = CompatMessageInfo(compat_user, compat_group, additional_config)

        # 3. chat_id and user_id attributes directly on message
        self.chat_id = group_id if group_id else user_id
        self.user_id = user_id
        self.message_type = "group" if group_id else "private"

        # 4. Message segments / raw_message
        raw_segments = _get_val(["message_segments", "raw_message", "message_segment"], [])
        if not isinstance(raw_segments, list):
            raw_segments = [raw_segments]

        compat_segments = []
        for seg in raw_segments:
            if not seg:
                continue
            if not isinstance(seg, dict):
                if hasattr(seg, "model_dump"):
                    try:
                        seg = seg.model_dump()
                    except Exception:
                        pass
                elif hasattr(seg, "dict"):
                    try:
                        seg = seg.dict()
                    except Exception:
                        pass
            if isinstance(seg, dict):
                s_type = seg.get("type", "text")
                s_data = seg.get("data", {})

                if isinstance(s_data, dict):
                    for key in ["url", "binary_data_base64", "hash"]:
                        if key in seg and key not in s_data:
                            s_data[key] = seg[key]

                base64_val = seg.get("binary_data_base64")
                if base64_val and (not s_data or (isinstance(s_data, dict) and not s_data.get("url"))):
                    s_data = base64_val

                compat_segments.append(CompatMessageSegment(s_type, s_data))

        if not compat_segments and self.processed_plain_text:
            compat_segments.append(CompatMessageSegment("text", self.processed_plain_text))

        self.raw_message = CompatMessageString(self.processed_plain_text or "")

        if len(compat_segments) == 1:
            self.message_segment = compat_segments[0]
        else:
            self.message_segment = CompatMessageSegment("seglist", compat_segments)

        # 5. chat_stream compat
        self.chat_stream = CompatChatStream(
            stream_id=self.session_id,
            platform=self.platform,
            user_info=compat_user,
            group_info=compat_group
        )

        # 6. reply message compat
        self.reply = None
        for seg in compat_segments:
            if seg.type == "reply":
                reply_data = seg.data
                if isinstance(reply_data, dict):
                    target_id = reply_data.get("target_message_id")
                    target_content = reply_data.get("target_message_content")
                    sender_id = reply_data.get("target_message_sender_id")
                    sender_nickname = reply_data.get("target_message_sender_nickname") or sender_id
                    sender_cardname = reply_data.get("target_message_sender_cardname")

                    mini_msg = {
                        "message_id": target_id,
                        "processed_plain_text": target_content,
                        "platform": self.platform,
                        "message_info": {
                            "user_info": {
                                "user_id": sender_id,
                                "user_nickname": sender_nickname,
                                "user_cardname": sender_cardname
                            }
                        }
                    }
                    self.reply = CompatMessage(mini_msg)
                break


def to_compat_message(message: Any) -> Any:
    if message is None:
        return None
    if isinstance(message, CompatMessage):
        return message
    return CompatMessage(message)


class GeminiDrawerPlugin(MaiBotPlugin):
    config_model = GeminiDrawerConfig

    def _set_context(self, ctx: PluginContext) -> None:
        super()._set_context(ctx)
        # 将上下文注入到全局兼容层持有者中，确保 legacy api 能顺利获取上下文
        from maibot_sdk.compat import _context_holder
        _context_holder.set_context(ctx)

    async def on_load(self) -> None:
        # 初始化自拍目录
        try:
            if self.config.selfie.enable:
                image_filename = self.config.selfie.reference_image_path
                plugin_dir = Path(__file__).parent
                images_dir = plugin_dir / "images"
                if not images_dir.exists():
                    images_dir.mkdir(parents=True, exist_ok=True)
                    self.ctx.logger.info(f"[GeminiDrawer] Auto-created images directory at: {images_dir}")
        except Exception as e:
            self.ctx.logger.warning(f"[GeminiDrawer] Failed to initialize selfie directory: {e}")

        # 同步配置缓存到兼容层 config_api
        try:
            from maibot_sdk.compat.apis import config_api
            config_api.set_config_cache(
                global_cfg={},
                plugin_cfg=self.get_plugin_config_data()
            )
        except Exception:
            pass

        self.ctx.logger.info("Gemini Drawer 插件 v1.9.9 已成功以原生 v1.0 架构加载！")

    async def on_unload(self) -> None:
        self.ctx.logger.info("Gemini Drawer 插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        self.ctx.logger.info("Gemini Drawer 配置热更新: scope=%s version=%s", scope, version)
        try:
            from maibot_sdk.compat.apis import config_api
            config_api.set_config_cache(
                global_cfg={},
                plugin_cfg=self.get_plugin_config_data()
            )
        except Exception:
            pass

    # ── 命令/动作运行桥接函数 ──

    async def _run_command(self, cmd_cls: Type[Any], stream_id: str, message: Any, matched_groups: Any) -> Tuple[bool, Optional[str], bool]:
        from maibot_sdk.compat import _context_holder
        token = _context_holder.activate_plugin(self.ctx.plugin_id)
        try:
            compat_msg = to_compat_message(message)
            instance = cmd_cls(message=compat_msg, plugin_config=self.get_plugin_config_data())
            instance._stream_id = stream_id
            instance.ctx = self.ctx  # 注入上下文，允许使用跨插件 API
            if matched_groups:
                instance.set_matched_groups(matched_groups)
            res = await instance.execute()
            if isinstance(res, tuple):
                success = res[0]
                reply = res[1] if len(res) > 1 else None
                stop = res[2] if len(res) > 2 else True
                if isinstance(stop, int):
                    stop = bool(stop)
                return success, reply, stop
            return True, None, True
        finally:
            _context_holder.deactivate_plugin(token)

    async def _run_action(self, action_cls: Type[Any], stream_id: str, **kwargs: Any) -> Tuple[bool, str]:
        from maibot_sdk.compat import _context_holder
        token = _context_holder.activate_plugin(self.ctx.plugin_id)
        try:
            instance = action_cls()
            instance.action_data = kwargs.get("action_data", {})
            instance.action_reasoning = kwargs.get("action_reasoning", "")
            instance.cycle_timers = kwargs.get("cycle_timers", {})
            instance.thinking_id = kwargs.get("thinking_id", "")
            instance.chat_stream = kwargs.get("chat_stream", None)
            instance.plugin_config = self.get_plugin_config_data()

            raw_action_msg = kwargs.get("action_message", None)
            instance.action_message = to_compat_message(raw_action_msg)

            instance._stream_id = stream_id
            for attr in (
                "chat_id",
                "user_id",
                "message",
                "message_id",
                "platform",
                "group_id",
                "group_name",
                "user_nickname",
                "is_group",
                "target_id",
            ):
                if attr in kwargs:
                    val = kwargs[attr]
                    if attr == "message" and val is not None:
                        val = to_compat_message(val)
                    setattr(instance, attr, val)
            
            instance.ctx = self.ctx
            return await instance.execute()
        finally:
            _context_holder.deactivate_plugin(token)

    # ── 用户与绘图指令 ──

    @Command("gemini_custom_draw", description=CustomDrawCommand.command_description, pattern=CustomDrawCommand.command_pattern, timeout_ms=DRAW_COMMAND_TIMEOUT_MS)
    async def handle_custom_draw(self, stream_id: str = "", message: Any = None, **kwargs: Any):
        return await self._run_command(CustomDrawCommand, stream_id, message, kwargs.get("matched_groups"))

    @Command("gemini_text_draw", description=TextToImageCommand.command_description, pattern=TextToImageCommand.command_pattern, timeout_ms=DRAW_COMMAND_TIMEOUT_MS)
    async def handle_text_draw(self, stream_id: str = "", message: Any = None, **kwargs: Any):
        return await self._run_command(TextToImageCommand, stream_id, message, kwargs.get("matched_groups"))

    @Command("gemini_universal_prompt", description=UniversalPromptCommand.command_description, pattern=UniversalPromptCommand.command_pattern, timeout_ms=DRAW_COMMAND_TIMEOUT_MS)
    async def handle_universal_prompt(self, stream_id: str = "", message: Any = None, **kwargs: Any):
        return await self._run_command(UniversalPromptCommand, stream_id, message, kwargs.get("matched_groups"))

    @Command("gemini_multi_image_draw", description=MultiImageDrawCommand.command_description, pattern=MultiImageDrawCommand.command_pattern, timeout_ms=DRAW_COMMAND_TIMEOUT_MS)
    async def handle_multi_image_draw(self, stream_id: str = "", message: Any = None, **kwargs: Any):
        return await self._run_command(MultiImageDrawCommand, stream_id, message, kwargs.get("matched_groups"))

    @Command("gemini_random_draw", description=RandomPromptDrawCommand.command_description, pattern=RandomPromptDrawCommand.command_pattern, timeout_ms=DRAW_COMMAND_TIMEOUT_MS)
    async def handle_random_draw(self, stream_id: str = "", message: Any = None, **kwargs: Any):
        return await self._run_command(RandomPromptDrawCommand, stream_id, message, kwargs.get("matched_groups"))

    @Command("gemini_video_generate", description=VideoGenerateCommand.command_description, pattern=VideoGenerateCommand.command_pattern, timeout_ms=VIDEO_COMMAND_TIMEOUT_MS)
    async def handle_video_generate(self, stream_id: str = "", message: Any = None, **kwargs: Any):
        return await self._run_command(VideoGenerateCommand, stream_id, message, kwargs.get("matched_groups"))

    @Command("gemini_text_to_video", description=TextToVideoCommand.command_description, pattern=TextToVideoCommand.command_pattern, timeout_ms=VIDEO_COMMAND_TIMEOUT_MS)
    async def handle_text_to_video(self, stream_id: str = "", message: Any = None, **kwargs: Any):
        return await self._run_command(TextToVideoCommand, stream_id, message, kwargs.get("matched_groups"))

    @Command("gemini_help", description=HelpCommand.command_description, pattern=HelpCommand.command_pattern)
    async def handle_help(self, stream_id: str = "", message: Any = None, **kwargs: Any):
        return await self._run_command(HelpCommand, stream_id, message, kwargs.get("matched_groups"))

    # ── 管理员指令 ──

    @Command("gemini_channel_add_key", description=ChannelAddKeyCommand.command_description, pattern=ChannelAddKeyCommand.command_pattern)
    async def handle_channel_add_key(self, stream_id: str = "", message: Any = None, **kwargs: Any):
        return await self._run_command(ChannelAddKeyCommand, stream_id, message, kwargs.get("matched_groups"))

    @Command("gemini_channel_list_keys", description=ChannelListKeysCommand.command_description, pattern=ChannelListKeysCommand.command_pattern)
    async def handle_channel_list_keys(self, stream_id: str = "", message: Any = None, **kwargs: Any):
        return await self._run_command(ChannelListKeysCommand, stream_id, message, kwargs.get("matched_groups"))

    @Command("gemini_channel_reset_key", description=ChannelResetKeyCommand.command_description, pattern=ChannelResetKeyCommand.command_pattern)
    async def handle_channel_reset_key(self, stream_id: str = "", message: Any = None, **kwargs: Any):
        return await self._run_command(ChannelResetKeyCommand, stream_id, message, kwargs.get("matched_groups"))

    @Command("gemini_channel_delete_key", description=ChannelDeleteKeyCommand.command_description, pattern=ChannelDeleteKeyCommand.command_pattern)
    async def handle_channel_delete_key(self, stream_id: str = "", message: Any = None, **kwargs: Any):
        return await self._run_command(ChannelDeleteKeyCommand, stream_id, message, kwargs.get("matched_groups"))

    @Command("gemini_channel_set_key_error_limit", description=ChannelSetKeyErrorLimitCommand.command_description, pattern=ChannelSetKeyErrorLimitCommand.command_pattern)
    async def handle_channel_set_key_error_limit(self, stream_id: str = "", message: Any = None, **kwargs: Any):
        return await self._run_command(ChannelSetKeyErrorLimitCommand, stream_id, message, kwargs.get("matched_groups"))

    @Command("gemini_channel_update_model", description=ChannelUpdateModelCommand.command_description, pattern=ChannelUpdateModelCommand.command_pattern)
    async def handle_channel_update_model(self, stream_id: str = "", message: Any = None, **kwargs: Any):
        return await self._run_command(ChannelUpdateModelCommand, stream_id, message, kwargs.get("matched_groups"))

    @Command("gemini_add_prompt", description=AddPromptCommand.command_description, pattern=AddPromptCommand.command_pattern)
    async def handle_add_prompt(self, stream_id: str = "", message: Any = None, **kwargs: Any):
        return await self._run_command(AddPromptCommand, stream_id, message, kwargs.get("matched_groups"))

    @Command("gemini_delete_prompt", description=DeletePromptCommand.command_description, pattern=DeletePromptCommand.command_pattern)
    async def handle_delete_prompt(self, stream_id: str = "", message: Any = None, **kwargs: Any):
        return await self._run_command(DeletePromptCommand, stream_id, message, kwargs.get("matched_groups"))

    @Command("gemini_view_prompt", description=ViewPromptCommand.command_description, pattern=ViewPromptCommand.command_pattern)
    async def handle_view_prompt(self, stream_id: str = "", message: Any = None, **kwargs: Any):
        return await self._run_command(ViewPromptCommand, stream_id, message, kwargs.get("matched_groups"))

    @Command("gemini_modify_prompt", description=ModifyPromptCommand.command_description, pattern=ModifyPromptCommand.command_pattern)
    async def handle_modify_prompt(self, stream_id: str = "", message: Any = None, **kwargs: Any):
        return await self._run_command(ModifyPromptCommand, stream_id, message, kwargs.get("matched_groups"))

    @Command("gemini_add_channel", description=AddChannelCommand.command_description, pattern=AddChannelCommand.command_pattern)
    async def handle_add_channel(self, stream_id: str = "", message: Any = None, **kwargs: Any):
        return await self._run_command(AddChannelCommand, stream_id, message, kwargs.get("matched_groups"))

    @Command("gemini_delete_channel", description=DeleteChannelCommand.command_description, pattern=DeleteChannelCommand.command_pattern)
    async def handle_delete_channel(self, stream_id: str = "", message: Any = None, **kwargs: Any):
        return await self._run_command(DeleteChannelCommand, stream_id, message, kwargs.get("matched_groups"))

    @Command("gemini_toggle_channel", description=ToggleChannelCommand.command_description, pattern=ToggleChannelCommand.command_pattern)
    async def handle_toggle_channel(self, stream_id: str = "", message: Any = None, **kwargs: Any):
        return await self._run_command(ToggleChannelCommand, stream_id, message, kwargs.get("matched_groups"))

    @Command("gemini_list_channels", description=ListChannelsCommand.command_description, pattern=ListChannelsCommand.command_pattern)
    async def handle_list_channels(self, stream_id: str = "", message: Any = None, **kwargs: Any):
        return await self._run_command(ListChannelsCommand, stream_id, message, kwargs.get("matched_groups"))

    @Command("gemini_channel_set_stream", description=ChannelSetStreamCommand.command_description, pattern=ChannelSetStreamCommand.command_pattern)
    async def handle_channel_set_stream(self, stream_id: str = "", message: Any = None, **kwargs: Any):
        return await self._run_command(ChannelSetStreamCommand, stream_id, message, kwargs.get("matched_groups"))

    @Command("gemini_channel_set_video", description=ChannelSetVideoCommand.command_description, pattern=ChannelSetVideoCommand.command_pattern)
    async def handle_channel_set_video(self, stream_id: str = "", message: Any = None, **kwargs: Any):
        return await self._run_command(ChannelSetVideoCommand, stream_id, message, kwargs.get("matched_groups"))

    # ── Actions ──

    @Action(
        "gemini_generate_image",
        description="根据用户的描述生成一张图片。当用户想要绘画、画图、生成图片时使用。",
        activation_type=ActivationType.ALWAYS,
        action_parameters={"prompt": "详细的图片描述，包括风格、内容、氛围等"},
        associated_types=["image"],
        action_require=[
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
    )
    async def handle_generate_image(self, stream_id: str = "", **kwargs: Any) -> Tuple[bool, str]:
        return await self._run_action(ImageGenerateAction, stream_id, **kwargs)

    @Action(
        "gemini_selfie",
        description="发送一张自己的自拍照片",
        activation_type=ActivationType.ALWAYS,
        action_parameters={
            "requested_action": "用户请求的完整场景描述（包括服装、动作、姿势、场景等），如'穿女仆装比心'、'戴眼镜做鬼脸'、'在海边挥手'等。需要完整提取用户的要求，不要只提取单个动作词。如果用户没有指定具体场景，返回空字符串。"
        },
        action_require=[
            "当用户明确要求看我的照片、自拍、长什么样时使用",
            "看看你的照片", "发张自拍",
            "注意：不要连续发，如果刚刚已经发送过自拍或正在生成中，就不要再次触发此动作"
        ]
    )
    async def handle_selfie(self, stream_id: str = "", **kwargs: Any) -> Tuple[bool, str]:
        return await self._run_action(SelfieGenerateAction, stream_id, **kwargs)

    @Action(
        "gemini_selfie_video",
        description="发送一段自己的视频",
        activation_type=ActivationType.ALWAYS,
        action_parameters={
            "requested_action": "用户请求的完整视频场景描述（包括服装、动作、场景等），如'穿女仆装跳舞'、'在海边挥手'、'穿JK转圈'、'做鬼脸眨眼'等。需要完整提取用户的要求，不要只提取单个动作词。如果用户没有明确指定场景，返回空字符串。"
        },
        action_require=[
            "当用户明确要求看我的视频、动态、动作时使用",
            "发个视频看看", "想看你跳舞", "来段视频",
            "注意：不要连续发，如果刚刚已经发送过视频或正在生成中，就不要再次触发此动作"
        ]
    )
    async def handle_selfie_video(self, stream_id: str = "", **kwargs: Any) -> Tuple[bool, str]:
        return await self._run_action(SelfieVideoAction, stream_id, **kwargs)


def create_plugin() -> GeminiDrawerPlugin:
    return GeminiDrawerPlugin()
