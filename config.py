from typing import List
from maibot_sdk import Field, PluginConfigBase

class PluginSectionConfig(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    name: str = Field(default="gemini_drawer", description="插件名称")
    version: str = Field(default="1.9.8", description="插件版本")
    config_version: str = Field(default="1.9.8", description="配置版本")
    enabled: bool = Field(default=True, description="是否启用插件")

class GeneralConfig(PluginConfigBase):
    __ui_label__ = "基本设置"
    __ui_icon__ = "settings"
    __ui_order__ = 1

    enable_gemini_drawer: bool = Field(default=True, description="是否启用Gemini绘图插件")
    admins: List[int] = Field(default_factory=list, description="可以管理本插件的管理员QQ号列表")
    blacklist_groups: List[int] = Field(default_factory=list, description="禁止使用本插件的群号黑名单列表")

class ProxyConfig(PluginConfigBase):
    __ui_label__ = "代理设置"
    __ui_icon__ = "globe"
    __ui_order__ = 2

    enable: bool = Field(default=False, description="是否为 Gemini API 请求启用代理")
    proxy_url: str = Field(default="http://127.0.0.1:7890", description="HTTP 代理地址")

class SelfieConfig(PluginConfigBase):
    __ui_label__ = "自拍功能"
    __ui_icon__ = "camera"
    __ui_order__ = 3

    enable: bool = Field(default=False, description="是否启用自拍功能")
    reference_image_path: str = Field(default="selfie_base.jpg", description="人设底图")
    base_prompt: str = Field(default="", description="人设基础Prompt (可选，可以不输入因为有人设图)")
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
        description="随机动作列表"
    )
    polish_enable: bool = Field(default=True, description="是否启用提示词润色")
    polish_model: str = Field(default="replyer", description="润色使用的文本模型名称(默认replyer不需要更改)")
    polish_template: str = Field(
        default="请将以下自拍主题润色为更适合AI图生图的提示词，保持原意但使描述更加细腻、生动、富有画面感。只输出润色后的提示词，不要输出其他内容。原始主题：'{original_prompt}'",
        description="润色提示词模板"
    )
    video_actions: List[str] = Field(
        default_factory=lambda: [
            "缓缓转头，露出微笑",
            "轻轻挥手打招呼",
            "眨眼并微微歪头",
            "点头微笑",
            "比耶手势"
        ],
        description="视频自拍随机动作列表"
    )
    video_polish_template: str = Field(
        default="请将以下视频动作描述润色为更适合AI图生视频生成的提示词时长5～10秒，让动作描述更加流畅、生动、有画面感。只输出润色后的提示词，不要输出其他内容。原始描述：'{original_prompt}'",
        description="视频提示词润色模板"
    )

class ApiConfig(PluginConfigBase):
    __ui_label__ = "API设置"
    __ui_icon__ = "key"
    __ui_order__ = 4

    enable_google: bool = Field(default=True, description="是否启用Google官方API")
    api_url: str = Field(
        default="https://generativelanguage.googleapis.com/v1beta/models/gemini-3-pro-image:generateContent",
        description="Google官方的Gemini API 端点"
    )
    enable_lmarena: bool = Field(default=False, description="是否启用第三方API")
    lmarena_api_url: str = Field(default="http://xxx:666/v1/chat/completions", description="第三方API的基础URL")
    lmarena_api_key: str = Field(default="", description="第三方API密钥 (可选, 使用Bearer Token)")
    lmarena_model_name: str = Field(default="gemini-3-pro-image-preview", description="第三方API 使用的模型名称")
    napcat_host: str = Field(default="napcat", description="NapCat HTTP服务器地址（Docker环境下设为'napcat'或容器名）")
    napcat_port: int = Field(default=3033, description="NapCat 正向HTTP端口，用于发送视频文件")

class BehaviorConfig(PluginConfigBase):
    __ui_label__ = "行为控制"
    __ui_icon__ = "user"
    __ui_order__ = 5

    debug_mode: bool = Field(default=False, description="调试模式：开启后当图片/视频提取失败时，会在终端输出原始API响应内容，帮助排查问题")
    admin_only_mode: bool = Field(default=False, description="管理员专用模式：开启后仅管理员可使用绘图功能，其他用户会收到'管理员已关闭功能'提示")
    auto_recall_status: bool = Field(default=True, description="是否自动撤回绘图过程中的状态提示消息（如'🎨 正在提交绘图指令…'）")
    success_notify_poke: bool = Field(default=True, description="生成成功后使用戳一戳通知用户（替代文字消息'✅ 生成完成'）")
    reply_with_image: bool = Field(default=True, description="以回复触发消息的方式发送图片（开启后自动跳过成功通知）")

class GeminiDrawerConfig(PluginConfigBase):
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    general: GeneralConfig = Field(default_factory=GeneralConfig)
    proxy: ProxyConfig = Field(default_factory=ProxyConfig)
    selfie: SelfieConfig = Field(default_factory=SelfieConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    behavior: BehaviorConfig = Field(default_factory=BehaviorConfig)
