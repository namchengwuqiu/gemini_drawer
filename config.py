from typing import Any, List
from maibot_sdk import Field, PluginConfigBase


def ui(label: str, hint: str | None = None, **extra: Any) -> dict[str, Any]:
    data = {"label": label, **extra}
    if hint:
        data["hint"] = hint
    return data


class PluginSectionConfig(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    name: str = Field(default="gemini_drawer", description="插件名称",
                      json_schema_extra=ui("插件名称", disabled=True))
    version: str = Field(default="1.9.15", description="插件版本", json_schema_extra=ui("插件版本", disabled=True))
    config_version: str = Field(default="1.9.15", description="配置版本",
                                json_schema_extra=ui("配置版本", disabled=True))
    enabled: bool = Field(default=True, description="是否启用插件", json_schema_extra=ui("启用插件"))


class GeneralConfig(PluginConfigBase):
    __ui_label__ = "基本设置"
    __ui_icon__ = "settings"
    __ui_order__ = 1

    enable_gemini_drawer: bool = Field(
        default=True,
        description="是否启用Gemini绘图插件",
        json_schema_extra=ui("启用绘图功能", "关闭后插件仍加载，但绘图命令和绘图 Action 不再响应。"),
    )
    admins: List[int] = Field(
        default_factory=list,
        description="可以管理本插件的管理员QQ号列表",
        json_schema_extra=ui("管理员 QQ 列表", "可使用插件管理命令的 QQ 号。留空时通常只受全局权限控制。"),
    )
    blacklist_groups: List[int] = Field(
        default_factory=list,
        description="禁止使用本插件的群号黑名单列表",
        json_schema_extra=ui("群黑名单", "这些群内的绘图、自拍、视频生成功能会被拒绝。"),
    )


class ProxyConfig(PluginConfigBase):
    __ui_label__ = "代理设置"
    __ui_icon__ = "globe"
    __ui_order__ = 2

    enable: bool = Field(
        default=False,
        description="是否为 Gemini API 请求启用代理",
        json_schema_extra=ui("启用代理", "访问 Google 官方 API 或第三方接口需要代理时开启。"),
    )
    proxy_url: str = Field(
        default="http://127.0.0.1:7890",
        description="HTTP 代理地址",
        json_schema_extra=ui("代理地址", "例如 http://127.0.0.1:7890。仅在启用代理后生效。"),
    )


class SelfieConfig(PluginConfigBase):
    __ui_label__ = "自拍功能"
    __ui_icon__ = "camera"
    __ui_order__ = 3

    enable: bool = Field(
        default=False,
        description="是否启用自拍功能",
        json_schema_extra=ui("启用自拍", "开启后用户可请求机器人基于人设底图生成自拍图片或视频。"),
    )
    reference_image_path: str = Field(
        default="selfie_base.jpg",
        description="人设底图",
        json_schema_extra=ui("人设底图文件", "相对于插件 images 目录的图片文件名，例如 selfie_base.jpg。"),
    )
    base_prompt: str = Field(
        default="",
        description="人设基础Prompt (可选，可以不输入因为有人设图)",
        json_schema_extra=ui(
            "人设基础提示词",
            "用于补充底图无法表达的人设信息，可留空。",
            **{"x-widget": "textarea", "rows": 3},
        ),
    )
    random_actions: List[str] = Field(
        default_factory=lambda: [
            "向观众眨眼，面带俏皮的微笑",
            "在公园里吃冰淇淋",
            "用手指做和平手势",
            "拿着珍珠奶茶",
            "戴着太阳镜在海滩上",
            "调整头发，看起来害羞",
            "穿着睡衣，抱着枕头",
            "随机生成符合图片人物的自拍动作"
        ],
        description="随机动作列表",
        json_schema_extra=ui("自拍随机动作", "用户没有指定自拍动作时，会从这里随机选择一条。"),
    )
    polish_enable: bool = Field(
        default=True,
        description="是否启用提示词润色",
        json_schema_extra=ui("启用自拍提示词润色", "开启后会先用文本模型把用户的自拍要求改写得更适合图生图。"),
    )
    polish_model: str = Field(
        default="replyer",
        description="润色使用的文本模型名称(默认replyer不需要更改)",
        json_schema_extra=ui("润色模型", "使用 MaiBot 中已配置的文本模型名称，默认 replyer 通常无需修改。"),
    )
    polish_template: str = Field(
        default="请将以下自拍主题润色为更适合AI图生图的提示词，保持原意但使描述更加细腻、生动、富有画面感。只输出润色后的提示词，不要输出其他内容。原始主题：'{original_prompt}'",
        description="润色提示词模板",
        json_schema_extra=ui(
            "自拍润色模板",
            "必须保留 {original_prompt}，运行时会替换为用户原始要求。",
            **{"x-widget": "textarea", "rows": 4},
        ),
    )
    video_actions: List[str] = Field(
        default_factory=lambda: [
            "缓缓转头，露出微笑",
            "轻轻挥手打招呼",
            "眨眼并微微歪头",
            "点头微笑",
            "比耶手势"
        ],
        description="视频自拍随机动作列表",
        json_schema_extra=ui("视频自拍随机动作", "用户没有指定视频动作时，会从这里随机选择一条。"),
    )
    video_polish_template: str = Field(
        default="请将以下视频动作描述润色为更适合AI图生视频生成的提示词时长5～10秒，让动作描述更加流畅、生动、有画面感。只输出润色后的提示词，不要输出其他内容。原始描述：'{original_prompt}'",
        description="视频提示词润色模板",
        json_schema_extra=ui(
            "视频润色模板",
            "必须保留 {original_prompt}，运行时会替换为用户原始视频动作描述。",
            **{"x-widget": "textarea", "rows": 4},
        ),
    )


class ApiConfig(PluginConfigBase):
    __ui_label__ = "视频发送"
    __ui_icon__ = "key"
    __ui_order__ = 4

    channel_setup_guide: str = Field(
        default=(
            "绘图 API 渠道不在 config.toml 里填写，请在聊天中使用指令管理：\n"
            "1. 添加 Google 官方渠道：/添加渠道 google:https://generativelanguage.googleapis.com/v1beta/models/gemini-3-pro-image-preview:generateContent\n"
            "2. 添加第三方 OpenAI 兼容渠道：/添加渠道 渠道名:https://api.example.com/v1/chat/completions:模型名\n"
            "3. 添加渠道 Key：/渠道添加key 渠道名 your-api-key\n"
            "4. 查看或启停渠道：/渠道列表、/启用渠道 渠道名、/禁用渠道 渠道名"
        ),
        description="绘图API渠道配置说明",
        json_schema_extra=ui(
            "绘图渠道配置说明",
            "只读说明。绘图 API 地址和 Key 请通过聊天指令写入渠道数据，不再放在 config.toml。",
            disabled=True,
            **{"x-widget": "textarea", "rows": 6},
        ),
    )
    napcat_host: str = Field(
        default="napcat",
        description="NapCat HTTP服务器地址（Docker环境下设为'napcat'或容器名）",
        json_schema_extra=ui(
            "NapCat 主机",
            "本页只配置视频发送所需 NapCat。绘图 API 渠道请在聊天中使用 `/添加渠道` 与 `/渠道添加key` 管理；Docker 环境通常填 napcat 或容器名。",
        ),
    )
    napcat_port: int = Field(
        default=3033,
        description="NapCat 正向HTTP端口，用于发送视频文件",
        json_schema_extra=ui(
            "NapCat HTTP 端口",
            "NapCat 正向 HTTP 服务端口，用于发送视频文件。绘图 API 渠道请使用 `/添加渠道 <名称>:<URL>[:模型名]` 添加。",
        ),
    )


class BehaviorConfig(PluginConfigBase):
    __ui_label__ = "行为控制"
    __ui_icon__ = "user"
    __ui_order__ = 5

    debug_mode: bool = Field(
        default=False,
        description="调试模式：开启后当图片/视频提取失败时，会在终端输出原始API响应内容，帮助排查问题",
        json_schema_extra=ui("调试模式", "排查接口返回异常时开启；可能输出较多原始响应内容。"),
    )
    admin_only_mode: bool = Field(
        default=False,
        description="管理员专用模式：开启后仅管理员可使用绘图功能，其他用户会收到'管理员已关闭功能'提示",
        json_schema_extra=ui("仅管理员可用", "开启后只有管理员 QQ 列表中的用户能使用绘图功能。"),
    )
    auto_recall_status: bool = Field(
        default=True,
        description="是否自动撤回绘图过程中的状态提示消息（如'🎨 正在提交绘图指令…'）",
        json_schema_extra=ui("自动撤回状态提示", "生成过程中发送的等待提示会在完成后自动撤回。"),
    )
    success_notify_poke: bool = Field(
        default=True,
        description="生成成功后使用戳一戳通知用户（替代文字消息'✅ 生成完成'）",
        json_schema_extra=ui("完成后戳一戳", "生成成功后用戳一戳提醒用户，减少文字提示刷屏。"),
    )
    reply_with_image: bool = Field(
        default=True,
        description="以回复触发消息的方式发送图片（开启后自动跳过成功通知）",
        json_schema_extra=ui("回复原消息发图", "开启后图片会回复触发消息发送，并自动跳过成功文字通知。"),
    )
    enable_banana_prompts: bool = Field(
        default=True,
        description="是否启用大香蕉网站提示词作为只读扩展词库",
        json_schema_extra=ui("启用大香蕉提示词", "把大香蕉网站提示词作为只读扩展词库，用于 /+ 等提示词命令。"),
    )
    show_restricted: bool = Field(
        default=False,
        description="是否显示大香蕉网站中标记为猎奇/重口/限制级的提示词",
        json_schema_extra=ui("显示限制级提示词", "开启后会显示被标记为猎奇、重口或限制级的提示词。请按使用场景谨慎开启。"),
    )
    banana_sync_on_load: bool = Field(
        default=False,
        description="插件加载时是否自动同步大香蕉提示词，关闭后仅手动同步",
        json_schema_extra=ui("加载时同步提示词", "开启后插件每次加载都会同步大香蕉提示词；关闭后只在手动命令触发时同步。"),
    )


class GeminiDrawerConfig(PluginConfigBase):
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    general: GeneralConfig = Field(default_factory=GeneralConfig)
    proxy: ProxyConfig = Field(default_factory=ProxyConfig)
    selfie: SelfieConfig = Field(default_factory=SelfieConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    behavior: BehaviorConfig = Field(default_factory=BehaviorConfig)
