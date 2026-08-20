from typing import Any, Tuple, Optional, Type
from pathlib import Path
import random
import httpx
from maibot_sdk import Command, HookHandler, MaiBotPlugin, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder
from maibot_sdk.context import PluginContext

from .config import GeminiDrawerConfig
from .core.managers import data_manager, key_manager

from .commands.help_command import HelpCommand
from .commands.draw_commands import (
    CustomDrawCommand, TextToImageCommand, UniversalPromptCommand,
    MultiImageDrawCommand, RandomPromptDrawCommand, VideoGenerateCommand,
    TextToVideoCommand
)
from .commands.admin_commands import (
    ChannelAddKeyCommand, ChannelListKeysCommand, ChannelResetKeyCommand,
    ChannelDeleteKeyCommand, ChannelSetKeyErrorLimitCommand, ChannelUpdateModelCommand,
    AddPromptCommand, DeletePromptCommand, ViewPromptCommand, ModifyPromptCommand,
    AddChannelCommand, DeleteChannelCommand, ToggleChannelCommand,
    ListChannelsCommand, ChannelSetStreamCommand, ChannelSetVideoCommand,
    SyncBananaPromptsCommand, ToggleBananaRestrictedCommand, BananaPromptSearchCommand
)
from .commands.actions import ImageGenerateAction, SelfieGenerateAction, SelfieVideoAction


DRAW_COMMAND_TIMEOUT_MS = 300_000
VIDEO_COMMAND_TIMEOUT_MS = 600_000
BANANA_PROMPTS_URL = "https://raw.githubusercontent.com/unknowlei/nanobanana-website/main/public/data.json"


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

    _MEDIA_TOOLS = frozenset({"gemini_generate_image", "gemini_selfie", "gemini_selfie_video"})
    _REPLY_GUARD_TEXT = (
        "媒体仍在生成或发送中。禁止声称图片/视频已经发送、已经拍好或让用户查收；"
        "只能说明正在准备并请对方稍等。只有在工具明确返回发送成功后，才可以使用完成时态。"
    )

    def __init__(self) -> None:
        super().__init__()
        # 按 stream_id 计数而不是布尔值。当前媒体 Tool 同步执行，同会话不会真的并发，
        # 但计数保证了任何一条路径提前结束都不会误清掉其他任务的保护状态。
        self._media_pending: dict[str, int] = {}

    def _begin_media_task(self, stream_id: str) -> None:
        key = str(stream_id or "").strip()
        if key:
            self._media_pending[key] = self._media_pending.get(key, 0) + 1

    def _end_media_task(self, stream_id: str) -> None:
        key = str(stream_id or "").strip()
        if not key:
            return
        remaining = self._media_pending.get(key, 0) - 1
        if remaining > 0:
            self._media_pending[key] = remaining
        else:
            self._media_pending.pop(key, None)

    def _media_is_pending(self, stream_id: str) -> bool:
        return self._media_pending.get(str(stream_id or "").strip(), 0) > 0

    def get_components(self) -> list[dict[str, Any]]:
        components = super().get_components()
        for component in components:
            metadata = component.get("metadata")
            if not isinstance(metadata, dict):
                continue

            nested_metadata = metadata.get("metadata")
            if not isinstance(nested_metadata, dict):
                continue

            for key, value in nested_metadata.items():
                metadata.setdefault(key, value)
        return components

    def _set_context(self, ctx: PluginContext) -> None:
        super()._set_context(ctx)
        # 将上下文注入到全局兼容层持有者中，确保 legacy api 能顺利获取上下文
        from maibot_sdk.compat import _context_holder
        _context_holder.set_context(ctx)

    async def on_load(self) -> None:
        # 历史数据迁移（旧版 keys.json / data.json / config.toml → 外部数据目录）
        try:
            key_manager.ensure_migrated()
            data_manager.ensure_migrated()
        except Exception as e:
            self.ctx.logger.warning(f"[GeminiDrawer] 历史数据迁移失败: {e}")

        # 初始化自拍目录
        try:
            if self.config.selfie.enable:
                plugin_dir = Path(__file__).parent
                assets_dir = plugin_dir / "assets"
                if not assets_dir.exists():
                    assets_dir.mkdir(parents=True, exist_ok=True)
                    self.ctx.logger.info(f"[GeminiDrawer] Auto-created assets directory at: {assets_dir}")
        except Exception as e:
            self.ctx.logger.warning(f"[GeminiDrawer] Failed to initialize selfie directory: {e}")

        if self.config.behavior.banana_sync_on_load:
            self.ctx.logger.info("[GeminiDrawer] 正在从大香蕉云端同步扩展词库...")
            success, msg = await self.sync_banana_website_prompts()
            if success:
                self.ctx.logger.info(f"[GeminiDrawer] {msg}")
            else:
                self.ctx.logger.warning(f"[GeminiDrawer] 大香蕉扩展词库自动同步失败: {msg}")
        else:
            self.ctx.logger.info("[GeminiDrawer] 已跳过大香蕉扩展词库自动同步，可使用 /渠道同步大香蕉 手动同步")

        # 同步配置缓存到兼容层 config_api
        try:
            from maibot_sdk.compat.apis import config_api
            config_api.set_config_cache(
                global_cfg={},
                plugin_cfg=self.get_plugin_config_data()
            )
        except Exception:
            pass

        self.ctx.logger.info(f"Gemini Drawer 插件 v{self.config.plugin.version} 已成功以原生 v1.0 架构加载！")

    async def sync_banana_website_prompts(self) -> Tuple[bool, str]:
        """同步大香蕉提示词到独立 banana_prompts.json，不修改 data.json。"""
        proxy = self.config.proxy.proxy_url if self.config.proxy.enable else None
        try:
            client_kwargs = {
                "timeout": 15.0,
                "follow_redirects": True,
            }
            if proxy:
                client_kwargs["proxy"] = proxy

            async with httpx.AsyncClient(**client_kwargs) as client:
                response = await client.get(BANANA_PROMPTS_URL)

            if response.status_code != 200:
                return False, f"下载云端词库失败，HTTP 状态码: {response.status_code}"

            try:
                web_data = response.json()
            except ValueError as e:
                return False, f"云端词库不是有效 JSON: {e}"

            if not isinstance(web_data, dict):
                return False, "云端词库格式错误：顶层不是对象"

            sections = web_data.get("sections")
            if not isinstance(sections, list):
                return False, "云端词库格式错误：sections 不是列表"

            prompts = {}
            seen_keys = set()
            total_prompt_count = 0
            restricted_count = 0
            duplicate_count = 0
            skipped_count = 0

            for section in sections:
                if not isinstance(section, dict):
                    continue
                section_id = str(section.get("id") or "").strip()
                section_title = str(section.get("title") or section_id or "未分类").strip()
                is_restricted = bool(section.get("isRestricted", False))
                section_prompts = section.get("prompts", [])
                if not isinstance(section_prompts, list):
                    continue

                for prompt in section_prompts:
                    total_prompt_count += 1
                    if not isinstance(prompt, dict):
                        skipped_count += 1
                        continue

                    prompt_id = str(prompt.get("id") or "").strip()
                    title = str(prompt.get("title") or "").strip()
                    content = prompt.get("content")
                    if not isinstance(content, str):
                        skipped_count += 1
                        continue
                    content = content.strip()
                    if not title or not content:
                        skipped_count += 1
                        continue

                    key = f"大香蕉/{section_title}/{title}"
                    if key in seen_keys:
                        duplicate_count += 1
                        short_id = prompt_id[-6:] if prompt_id else str(duplicate_count)
                        key = f"{key}#{short_id}"
                        while key in seen_keys:
                            duplicate_count += 1
                            key = f"大香蕉/{section_title}/{title}#{short_id}-{duplicate_count}"
                    seen_keys.add(key)

                    if is_restricted:
                        restricted_count += 1

                    prompts[key] = {
                        "content": content,
                        "prompt_id": prompt_id,
                        "source_title": title,
                        "section_id": section_id,
                        "section_title": section_title,
                        "restricted": is_restricted,
                    }

            if not prompts:
                return False, f"同步失败：未解析到有效提示词，跳过 {skipped_count} 条"

            from datetime import datetime, timezone
            banana_data = {
                "schema_version": 1,
                "source_url": BANANA_PROMPTS_URL,
                "synced_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                "remote_last_updated": web_data.get("lastUpdated"),
                "prompts": prompts,
            }
            data_manager.save_banana_data(banana_data)

            return (
                True,
                "大香蕉词库同步完成："
                f"sections={len(sections)}，远端prompts={total_prompt_count}，"
                f"保存={len(prompts)}，restricted={restricted_count}，"
                f"重名处理={duplicate_count}，跳过={skipped_count}，"
                f"remote_last_updated={web_data.get('lastUpdated') or '未知'}"
            )
        except httpx.TimeoutException:
            return False, "同步超时：访问大香蕉 GitHub raw 超过 15 秒"
        except httpx.HTTPError as e:
            return False, f"同步网络异常: {e}"
        except Exception as e:
            return False, f"同步发生未知异常: {e}"

    async def on_unload(self) -> None:
        self._media_pending.clear()
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

    @staticmethod
    def _item_tool_name(item: Any) -> str:
        """读取序列化 Item 中的工具名，非工具调用返回空串。"""
        if not isinstance(item, dict) or item.get("item_type") != "FunctionCallItem":
            return ""
        tool_call = item.get("tool_call")
        if not isinstance(tool_call, dict):
            return ""
        return str(tool_call.get("func_name") or "").strip()

    @HookHandler(
        "maisaka.planner.after_response",
        name="gemini_drawer_suppress_parallel_reply",
        description="媒体工具与 reply 同轮出现时移除 reply 及重复媒体调用，避免媒体发送前提前宣称完成",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def suppress_parallel_media_reply(self, **kwargs: Any) -> dict[str, Any]:
        # 宿主对 Hook 失败只记进 dispatch_result.errors，调用方不会打日志。
        # 一旦 output_items 的结构发生变化，这里必须自己喊出来，否则会静默退回旧行为。
        try:
            output_items = kwargs.get("output_items")
            if not isinstance(output_items, list):
                return {"action": "continue"}

            tool_name = self._item_tool_name
            if not any(tool_name(item) in self._MEDIA_TOOLS for item in output_items):
                return {"action": "continue"}

            filtered_items: list[Any] = []
            removed_reply = 0
            removed_duplicate = 0
            seen_media = False
            for item in output_items:
                name = tool_name(item)
                if name == "reply":
                    # 媒体工具会同步执行到发送完成，这一轮的 reply 必然早于图片落地。
                    removed_reply += 1
                    continue
                if name in self._MEDIA_TOOLS:
                    # 串行执行会在第一次结束时清掉 pending，同轮的第二次调用
                    # 靠 _run_media_tool 的去重拦不住，只能在这里掐掉。
                    if seen_media:
                        removed_duplicate += 1
                        continue
                    seen_media = True
                filtered_items.append(item)

            if not removed_reply and not removed_duplicate:
                return {"action": "continue"}

            kwargs["output_items"] = filtered_items
            self._get_logger().warning(
                f"[GeminiDrawer] 已过滤与媒体工具同轮的调用：reply {removed_reply} 个、"
                f"重复媒体调用 {removed_duplicate} 个，等待媒体发送完成"
            )
            return {"action": "continue", "modified_kwargs": kwargs}
        except Exception as exc:
            self._get_logger().error(
                f"[GeminiDrawer] 同轮 reply 过滤失败，本轮回退为宿主默认行为: {exc}",
                exc_info=True,
            )
            return {"action": "continue"}

    @HookHandler(
        "maisaka.replyer.before_request",
        name="gemini_drawer_pending_media_guard",
        description="媒体生成期间约束 Replyer 使用进行时，禁止提前声称已经发送",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def guard_pending_media_reply(self, **kwargs: Any) -> dict[str, Any]:
        # 兜底防线：正常路径下 suppress_parallel_media_reply 已经摘掉同轮 reply，
        # 且媒体工具同步阻塞期间该会话不会再跑 replyer，所以这里通常不触发。
        # 保留它是为了在上面那个 Hook 失效时仍有约束，不要因为"没见它生效"就删掉。
        try:
            stream_id = str(kwargs.get("session_id") or kwargs.get("stream_id") or "").strip()
            if not self._media_is_pending(stream_id):
                return {"action": "continue"}

            existing = str(kwargs.get("extra_prompt") or "").strip()
            kwargs["extra_prompt"] = (
                f"{existing}\n\n{self._REPLY_GUARD_TEXT}".strip()
                if existing
                else self._REPLY_GUARD_TEXT
            )
            self._get_logger().warning(
                "[GeminiDrawer] 媒体生成期间仍触发了 Replyer，已注入禁止提前宣称发送的约束"
            )
            return {"action": "continue", "modified_kwargs": kwargs}
        except Exception as exc:
            self._get_logger().error(
                f"[GeminiDrawer] 注入媒体等待约束失败: {exc}",
                exc_info=True,
            )
            return {"action": "continue"}

    # ── 命令/动作运行桥接函数 ──

    async def _run_command(self, cmd_cls: Type[Any], stream_id: str, message: Any, matched_groups: Any) -> Tuple[bool, Optional[str], bool]:
        from maibot_sdk.compat import _context_holder
        token = _context_holder.activate_plugin(self.ctx.plugin_id)
        try:
            compat_msg = to_compat_message(message)
            instance = cmd_cls(message=compat_msg, plugin_config=self.get_plugin_config_data())
            instance._stream_id = stream_id
            instance.ctx = self.ctx  # 注入上下文，允许使用跨插件 API
            instance.plugin = self  # 注入插件实例，供原生命令调用插件级辅助方法
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
            action_data = kwargs.get("action_data")
            if not isinstance(action_data, dict):
                action_data = {
                    key: value
                    for key, value in kwargs.items()
                    if key in {"prompt", "requested_action"}
                }
            instance.action_data = action_data
            instance.wait_message = kwargs.get("wait_message", "")
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

    def _pick_wait_message(self, kind: str) -> str:
        """从配置里随机取一条等待提示；该提示绕过 replyer，固定文案会出戏。"""
        try:
            candidates = getattr(self.config.wait_notice, f"{kind}_messages", None) or []
        except Exception as exc:
            self._get_logger().warning(f"[GeminiDrawer] 读取{kind}等待提示配置失败: {exc}")
            return ""
        normalized = [str(item).strip() for item in candidates if str(item).strip()]
        return random.choice(normalized) if normalized else ""

    async def _run_media_tool(
        self,
        tool_name: str,
        action_cls: Type[Any],
        stream_id: str,
        wait_kind: str,
        action_data: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        stream_id = str(stream_id or kwargs.get("chat_id") or "").strip()
        if not stream_id:
            return {
                "name": tool_name,
                "success": False,
                "content": "无法确定目标会话，媒体任务未执行",
                "error": "缺少 stream_id",
            }

        if self._media_is_pending(stream_id):
            # 必须返回失败：宿主只看 success 字段，返回成功会让模型顺着说"已经发了"，
            # 正好复现这次要修的问题。
            reason = "当前会话已有媒体任务正在生成，本次未提交，也没有任何内容被发送。"
            return {
                "name": tool_name,
                "success": False,
                "content": reason,
                "error": reason,
            }

        self._begin_media_task(stream_id)
        try:
            success, message = await self._run_action(
                action_cls,
                stream_id,
                action_data=action_data,
                wait_message=self._pick_wait_message(wait_kind),
                **kwargs,
            )
            result = {
                "name": tool_name,
                "success": bool(success),
                "content": str(message or ""),
            }
            if not success:
                result["error"] = str(message or "媒体任务执行失败")
            return result
        finally:
            self._end_media_task(stream_id)

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

    @Command("gemini_sync_banana", description=SyncBananaPromptsCommand.command_description, pattern=SyncBananaPromptsCommand.command_pattern)
    async def handle_sync_banana(self, stream_id: str = "", message: Any = None, **kwargs: Any):
        return await self._run_command(SyncBananaPromptsCommand, stream_id, message, kwargs.get("matched_groups"))

    @Command("gemini_toggle_banana_restricted", description=ToggleBananaRestrictedCommand.command_description, pattern=ToggleBananaRestrictedCommand.command_pattern)
    async def handle_toggle_banana_restricted(self, stream_id: str = "", message: Any = None, **kwargs: Any):
        return await self._run_command(ToggleBananaRestrictedCommand, stream_id, message, kwargs.get("matched_groups"))

    @Command("gemini_search_banana_prompts", description=BananaPromptSearchCommand.command_description, pattern=BananaPromptSearchCommand.command_pattern)
    async def handle_search_banana_prompts(self, stream_id: str = "", message: Any = None, **kwargs: Any):
        return await self._run_command(BananaPromptSearchCommand, stream_id, message, kwargs.get("matched_groups"))

    # ── AI Tools ──

    @Tool(
        "gemini_generate_image",
        description=(
            "根据用户的明确要求生成并自行发送图片。适用于‘画一张’‘生成图片’‘帮我画’等视觉请求，"
            "不适用于文字人设、故事或仅讨论图片。遇到 /绘图、/bnn、/多图、/+ 等命令时不要调用。"
            "工具会先发送等待提示，然后一直等待到图片真正发送成功或明确失败才返回；调用本工具时不要在同一轮"
            "同时调用 reply，也不要在工具成功返回前声称图片已经发送或让用户查收。"
        ),
        parameters={
            "prompt": {
                "type": "string",
                "description": "详细的图片描述，包括主体、风格、内容、构图和氛围",
                "required": True,
            }
        },
        timeout_ms=DRAW_COMMAND_TIMEOUT_MS
    )
    async def handle_generate_image(
        self, prompt: str = "", stream_id: str = "", **kwargs: Any
    ) -> dict[str, Any]:
        return await self._run_media_tool(
            "gemini_generate_image",
            ImageGenerateAction,
            stream_id,
            "image",
            {"prompt": prompt},
            **kwargs,
        )

    @Tool(
        "gemini_selfie",
        description=(
            "基于已配置的人设底图生成并自行发送角色自拍。当用户明确要求看你的照片、自拍或长什么样时使用。"
            "工具会先发送正在准备的提示，并等待自拍图片真正发送成功或明确失败才返回。requested_action 要完整保留"
            "用户要求的服装、动作、姿势和场景；未指定时可留空。调用本工具时不要同轮调用 reply，也不要提前说"
            "‘已经发了’‘拍好了’或让用户查收。"
        ),
        parameters={
            "requested_action": {
                "type": "string",
                "description": (
                    "完整自拍场景描述，包括服装、动作、姿势、表情和场景；"
                    "如‘穿女仆装比心’‘戴眼镜做鬼脸’。没有具体要求时传空字符串"
                ),
                "required": False,
            }
        },
        timeout_ms=DRAW_COMMAND_TIMEOUT_MS
    )
    async def handle_selfie(
        self, requested_action: str = "", stream_id: str = "", **kwargs: Any
    ) -> dict[str, Any]:
        return await self._run_media_tool(
            "gemini_selfie",
            SelfieGenerateAction,
            stream_id,
            "selfie",
            {"requested_action": requested_action},
            **kwargs,
        )

    @Tool(
        "gemini_selfie_video",
        description=(
            "基于已配置的人设底图生成并自行发送角色视频。当用户明确要求看你的视频、动态或动作时使用。"
            "工具会先发送正在准备的提示，并等待视频真正发送成功或明确失败才返回。requested_action 要完整保留"
            "服装、动作和场景；未指定时可留空。调用本工具时不要同轮调用 reply，也不要在成功返回前声称视频"
            "已经发送或让用户查收。"
        ),
        parameters={
            "requested_action": {
                "type": "string",
                "description": (
                    "完整视频场景描述，包括服装、动作、表情和场景；"
                    "如‘穿女仆装跳舞’‘在海边挥手’。没有具体要求时传空字符串"
                ),
                "required": False,
            }
        },
        timeout_ms=VIDEO_COMMAND_TIMEOUT_MS
    )
    async def handle_selfie_video(
        self, requested_action: str = "", stream_id: str = "", **kwargs: Any
    ) -> dict[str, Any]:
        return await self._run_media_tool(
            "gemini_selfie_video",
            SelfieVideoAction,
            stream_id,
            "video",
            {"requested_action": requested_action},
            **kwargs,
        )


def create_plugin() -> GeminiDrawerPlugin:
    return GeminiDrawerPlugin()
