# Gemini 绘图插件

> **Version:** 1.6.4

本插件基于 Google的Gemini 系列模型，提供强大的图片二次创作能力。它可以根据用户提供的图片和指定的风格指令，生成一张全新的图片，更新日志在CHANGELOG.md中查看。

## 主要特性

- **动态指令**：无需修改代码，仅通过修改配置文件即可轻松添加新的绘图风格和对应指令。
- **多样化图片源**：支持多种方式获取原始图片，极大提升了使用的便利性。
- **多 API Key 管理**：自动轮换并管理多个 API Key，保证服务稳定。
- **多后端支持**：支持 Google 官方、第三方兼容 API (如 Bailili)、LMArena 后端以及火山豆包 API。
- **自定义指令**：支持使用 `/bnn` 指令进行完全自定义的 prompt 绘图。
- **代理支持**：可为 API 请求配置 HTTP 代理。
- **回复图片模式** 🆕：生成的图片以回复触发消息的方式发送，更直观的用户反馈。
- **管理员专用模式** 🆕：可限制仅管理员使用绘图功能。
- **自然语言交互** 🆕：支持直接通过"帮我画个猫"等自然语言请求触发绘图。

---

## 安装

在使用此插件前，请确保已安装其所需的 Python 依赖。

在您的机器人项目环境的命令行中，进入本插件目录 (`plugins/gemini_drawer`)，然后运行以下指令：

```shell
pip install -r requirements.txt
```

---

---

## 自然语言交互 🆕

本插件现在包含一个 `ImageGenerateAction` 组件，允许 Bot 理解自然语言的绘图请求。

**触发示例**:
- "帮我画一只戴眼镜的猫"
- "生成一张赛博朋克风格的街道图片"
- "画个太阳"

**注意**: 此功能依赖于 Bot 的核心 LLM 具备意图识别（Function Calling 或 ReAct）能力。如果 Bot 认为用户的意图是绘图，它会自动调用本插件进行生成。

---

## 指令列表

### 用户指令

| 指令                  | 功能                                                     |
| :-------------------- | :------------------------------------------------------- |
| `/基咪绘图帮助`       | 显示本帮助菜单，列出所有可用指令。                       |
| `/绘图 {描述词}`      | **[新]** 纯文本生图，根据您的描述生成图片。              |
| `/+{风格指令}`        | 使用预设的风格指令进行绘图 (例如 `/+手办化`, `/+自拍`)。 |
| `/bnn {自定义prompt}` | 使用完全自定义的 prompt 进行绘图。                       |
| `/多图 {提示词}`      | **[新]** 融合至少2张图片进行绘图 (来源：回复/消息/@)。   |

**注**： `{风格指令}` 是指您在配置文件 `[prompts]` 部分定义的所有指令。

**通用使用方法**:
- 回复一张图片 + 指令
- @一位用户 + 指令 (使用对方头像)
- 发送一张图片 + 指令
- 直接发送指令 (使用自己头像)

### 管理员指令

| 指令                                     | 功能                                                                   |
| :--------------------------------------- | :--------------------------------------------------------------------- |
| `/渠道添加key {渠道} {key} ...`          | **[改]** 添加指定渠道的 API Key。                                      |
| `/渠道key列表`                           | **[改]** 查看各渠道 Key 的状态。                                       |
| `/渠道重置key [渠道] [序号]`             | **[新]** 重置指定渠道的特定 Key、全部 Key 或所有渠道的 Key的错误次数。 |
| `/渠道设置错误上限 {渠道} {序号} {次数}` | **[新]** 设置指定Key的错误禁用上限 (-1为永不禁用)。                    |
| `/添加提示词 {名称}:{prompt}`            | **[新]** 动态添加一个绘图指令，**即时生效**。                          |
| `/删除提示词 {名称}`                     | **[新]** 动态删除一个绘图指令，**即时生效**。                          |
| `/添加渠道 {名称}:{API地址}[:{模型}]`    | **[改]** 动态添加一个自定义 API 渠道。支持 OpenAI、Gemini 和豆包格式。 |
| `/渠道修改模型 {名称} {新模型}`          | **[新]** 修改指定渠道的模型名称，加载需重启。                          |
| `/删除渠道 {名称}`                       | **[新]** 动态删除一个自定义 API 渠道。                                 |
| `/启用渠道 {名称}`                       | **[新]** 启用指定渠道 (支持 google/lmarena)。                          |
| `/禁用渠道 {名称}`                       | **[新]** 禁用指定渠道。                                                |
| `/渠道设置流式 {名称} {true\|false}`     | **[新]** 设置渠道是否使用流式请求。                                    |
| `/渠道列表`                              | **[新]** 查看所有渠道的启用/禁用状态及流式设置。                       |
| `/渠道删除key {渠道} {序号}`             | **[新]** 删除指定渠道的指定 Key。                                      |

**注**：管理员指令在 `/基咪绘图帮助` 中仅对管理员可见。

### Key 错误禁用管理

为了防止因临时的网络问题或 API 波动导致某个 Key 被永久禁用，插件现在支持为每个 Key 单独设置错误次数上限。

- 使用 `/渠道设置错误上限 {渠道} {序号} {次数}` 指令来设置。
- `{次数}` 是一个数字，代表该 Key 在连续出错多少次后会被自动禁用。
- **将 `{次数}` 设置为 `-1`，即可让该 Key 永不因错误次数过多而被禁用**，这对于某些你信任的、稳定的渠道非常有用。
- 使用 `/渠道key列表` 可以查看每个 Key 当前的错误上限设置 (`∞` 代表永不禁用)。

---

## 配置文件说明

插件的配置文件位于插件目录下的 `config.toml`。

### `[general]` - 通用设置

- `enable_gemini_drawer` (布尔值, 默认 `true`): 是否启用本插件。
- `admins` (数组, 默认 `[]`): 管理员 QQ 号列表，只有在此列表中的用户才能使用管理员指令。
  - 示例: `admins = [123456, 789012]`

### `[proxy]` - 代理设置

- `enable` (布尔值, 默认 `false`): 是否为 API 请求启用代理。
- `proxy_url` (字符串, 默认 `"http://127.00.1:7890"`): 你的 HTTP 代理地址。

### `[behavior]` - 行为设置 🆕

- `admin_only_mode` (布尔值, 默认 `false`): 管理员专用模式，开启后仅管理员可使用绘图功能。
- `auto_recall_status` (布尔值, 默认 `true`): 是否自动撤回绘图过程中的状态提示消息。
- `success_notify_poke` (布尔值, 默认 `true`): 生成成功后使用戳一戳通知用户。
- `reply_with_image` (布尔值, 默认 `true`): 以回复触发消息的方式发送图片（开启后自动跳过成功通知）。

### `[api]` - API 端点设置

此部分用于配置插件可以使用的不同后端 API。

- `enable_google` (布尔值, 默认 `true`): 是否启用 Google 官方 API。
- `api_url` (字符串): Google 官方 API 的端点地址。
- `enable_lmarena` (布尔值, 默认 `false`): 是否启用 LMArena API。插件会自动根据 Key 的格式（是否以 `sk-` 开头）选择合适的端点。
- `lmarena_api_url` (字符串, 默认 `http://host.docker.internal:5102`): **[新增]** LMArena API 的基础 URL。如果你在 Docker 中运行，并且 LMArena 也在 Docker 网络中，这个地址通常是正确的。
- `lmarena_api_key` (字符串, 默认 `""`): **[新增]** LMArena API 的密钥 (可选, 使用 Bearer Token)。
- `lmarena_model_name` (字符串, 默认 `gemini-2.5-flash-image-preview (nano-banana)`): **[新增]** LMArena 使用的模型名称。
*   `selfie.enable` (布尔值, 默认 `false`): 是否启用自拍功能 (需手动开启)。
*   `selfie.reference_image_path` (字符串, 默认 `"selfie_base.jpg"`): 人设底图文件名 (放入插件自动生成的 images 目录)。
*   `selfie.base_prompt` (字符串, 默认 `""`): 人设基础描述词 (可选)。
*   `selfie.random_actions` (数组): 随机场景/动作列表。

## 📸 自拍与照片生成
插件支持通过自然语言请求 Bot 发送“自拍”。
1. **启用功能**: 在配置中设置 `selfie.enable = true` 并重启插件。
2. **准备底图**: 将人设图放入插件目录下的 `images/` 文件夹（插件启动后会自动创建此文件夹）。
3. **配置文件名**: 确保 `reference_image_path` 与您的图片文件名一致。
4. **触发**: 直接对 Bot 说“发张自拍”、“看看你的照片”。

### 自定义渠道配置
自定义渠道的配置数据存储于 `data/data.json` 中，建议使用管理员指令进行管理。

**支持的 URL 格式 (严格校验)**：

1.  **OpenAI 格式** (必须包含 `/chat/completions`):
    *   URL: `https://api.example.com/v1/chat/completions`
    *   Model: **必须指定** (例如 `gemini-1.5-pro`)
    *   指令示例: `/添加渠道 MyOpenAI:https://api.example.com/v1/chat/completions:gemini-1.5-pro`

2.  **Gemini 格式** (必须包含 `:generateContent`):
    *   URL: `https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent`
    *   Model: 包含在 URL 中，无需额外指定。
    *   指令示例: `/添加渠道 MyGemini:https://.../models/gemini-1.5-flash:generateContent`

3.  **火山豆包格式** 🆕 (必须包含 `/images/generations`):
    *   URL: `https://ark.cn-beijing.volces.com/api/v3/images/generations`
    *   Model: **必须指定** (例如 `doubao-seedream-4-5-251128`)
    *   指令示例: `/添加渠道 doubao:https://ark.cn-beijing.volces.com/api/v3/images/generations:doubao-seedream-4-5-251128`
    *   获取 API Key: [火山引擎控制台](https://console.volcengine.com/)

### 流式请求配置

某些 API 端点需要使用流式请求（SSE）来获取响应。插件支持为每个自定义渠道单独配置是否使用流式请求。

- **默认行为**: 新添加的自定义渠道默认**不使用**流式请求（`stream: false`）。
- **LMArena**: 内置的 LMArena 渠道默认**使用**流式请求。
- **设置方法**: 使用 `/渠道设置流式 <渠道名称> <true|false>` 命令切换。
  - 例如: `/渠道设置流式 PockGo true` 为 PockGo 渠道启用流式请求。
- **查看状态**: 在 `/渠道列表` 中，启用了流式请求的渠道会显示 `[流式]` 标签。

- `/绘图 <描述>`: 根据描述生成图片（文生图）。
- `/+ <指令名>`: 调用已添加的提示词进行绘图（如 `/+ 黄游`）。
- `/添加提示词 <指令名>:<提示词>`: 添加一个新的绘图指令。
- `/删除提示词 <指令名>`: 删除一个绘图指令。
- `/添加渠道 <名称>:<URL>:<模型>:<类型>`: 添加一个新的 API 渠道。
- `/删除渠道 <名称>`: 删除一个渠道。
- `/渠道列表`: 查看所有渠道及其状态。
- `/渠道模型 <名称>:<新模型>`: 修改渠道使用的模型。
- `/查看提示词 <名称>`: 查看指定提示词的完整内容。

### 4. 数据存储

插件将所有提示词（prompts）和渠道（channels）配置存储在 `data/data.json` 文件中。
这个文件在首次运行时会自动创建，如果之前有 `config.toml` 配置，也会自动迁移数据。

**注意**: 你不需要手动创建 `data/data.json`，插件会自动管理。

**[prompts] 配置详解**

**这是插件最核心的配置部分。** 这里定义的每一个键值对，都会自动成为一个新的绘图指令。数据存储于 `data/data.json` 中。
仓库中 `prompts.text` 文件已经定义了一些常用的指令，建议直接使用管理员指令 `/添加提示词` 进行添加。推荐直接访问网站[大香蕉](https://nanobanana-website.vercel.app)查看更多提示词

- **指令名**: 将作为机器人的指令名。例如，为 `手办化`，对应的指令就是 `/+手办化`。
- **Prompt**: 调用该指令时，发送给 Gemini API 的 prompt 文本。

**示例** (data.json):

```json
{
  "prompts": {
    "手办化": "将图片中的人物变成手办风格..."
  }
}
```

#### **如何新增一个指令？**

推荐使用管理员指令进行操作：
- **新增**: `/添加提示词 水彩画:watercolor painting style, vibrant colors, masterpiece` (即时生效)
- **删除**: `/删除提示词 水彩画` (即时生效)
- **使用**: `/+水彩画`

如果需要手动批量添加，可以编辑 `data/data.json` 。

---

## API Key 及后端说明

本插件支持三种类型的后端，并会自动轮询尝试：

1.  **LMArena (本地/自部署)**
    - **特点**: 免费，需要在本地或服务器上自行部署 [LMArenaImagenAutomator](https://github.com/foxhui/LMArenaImagenAutomator)。
    - **配置**: 在 `config.toml` 的 `[api]` 部分填入 `lmarena_api_url`。

2.  **Google 官方 Key**
    - **获取地址**: [https://aistudio.google.com/api-keys](https://aistudio.google.com/api-keys)
    - **特点**: Google 官方提供，但可能需要代理才能访问。

3.  **第三方兼容 Key**
    - **特点**: 通常以 `sk-` 开头，由第三方服务商提供，可能在国内网络环境下有更好的访问性。
    - **一些已知的服务商地址**:
        -   Bailili API: [https://api.bailili.top/register?aff=oPYw](https://api.bailili.top/register?aff=oPYw)
        -   VC-AI: [https://newapi.sisuo.de/register?aff=ugef](https://newapi.sisuo.de/register?aff=ugef)
        -   *(请注意，第三方服务可能随时变更)*

获取 Key 后，请使用管理员指令 `/渠道添加key {渠道} {key}` 将其添加至插件。插件会根据 Key 的格式自动判断其类型 (Google or 第三方)。