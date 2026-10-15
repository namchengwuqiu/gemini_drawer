# Gemini 绘图插件

> **Version:** 1.15.2

本插件基于 Google的Gemini 系列模型，提供强大的图片二次创作能力。它可以根据用户提供的图片和指定的风格指令，生成一张全新的图片，更新日志在[CHANGELOG.md](https://github.com/namchengwuqiu/gemini_drawer/blob/main/CHANGELOG.md)中查看。

## 主要特性

- **动态指令**：无需修改代码，即可通过管理指令添加绘图渠道、API Key 和绘图风格。
- **WebUI 管理页面（v1.14.0）**：渠道与 Key 联合展示，通过弹框管理渠道、提示词和密钥，数据仍保存到各自的 JSON 文件。
- **多样化图片源**：支持多种方式获取原始图片，极大提升了使用的便利性。
- **多 API Key 管理**：自动轮换并管理多个 API Key，保证服务稳定。
- **多后端支持**：支持 Google 官方、OpenAI 兼容格式、第三方兼容 API、LMArena 后端、火山豆包 API 以及 Agnes 图片/视频 API，渠道统一通过指令管理。
- **自定义指令**：支持使用 `/bnn` 指令进行完全自定义的 prompt 绘图。
- **代理支持**：可为 API 请求配置 HTTP 代理。
- **回复图片模式** 🆕：生成的图片以回复触发消息的方式发送，更直观的用户反馈。
- **管理员专用模式** 🆕：可限制仅管理员使用绘图功能。
- **自然语言交互** 🆕：支持直接通过"帮我画个猫"等自然语言请求触发绘图，并在媒体真正发送后才报告完成。
- **视频生成功能** 🎬：支持图生视频和文生视频，根据图片和描述生成视频，还有自然语言生成自拍视频。

---

## 安装

### 从插件市场安装

在 MaiBot WebUI 的插件市场搜索「Gemini 绘图」，进入详情后，在「安装版本」中选择兼容的发布版本进行安装或更新。

版本列表来自插件仓库已发布且已被市场收录的 GitHub Release。仅更新本仓库的版本号或创建 Tag 不会生成市场的可选版本；发布后还需要等待市场同步和审核收录。

### 手动安装依赖

需要 Python 3.11 或以上版本。在使用此插件前，请确保已安装其所需的 Python 依赖。

在您的机器人项目环境的命令行中，进入本插件目录 (`plugins/gemini_drawer`)，然后运行以下指令：

```shell
pip install -r requirements.txt
```

### 开发者发布版本

1. 更新 `_manifest.json` 的 `version`、`config.py` 中的 `version` / `config_version` 默认值以及 README 顶部版本号，并在 `CHANGELOG.md` 添加对应的 `## vX.Y.Z` 更新说明。保持插件 ID `namchengwuqiu.gemini_drawer` 不变，按实际兼容情况声明 MaiBot 与 SDK 版本范围。
2. 提交并推送代码，然后创建、推送与 manifest 版本一致的 Tag。例如发布当前 `1.15.2`：

   ```shell
   git push origin main
   git tag v1.15.2
   git push origin v1.15.2
   ```

3. GitHub Actions 的 **Publish plugin release** 工作流会校验 Tag、插件 ID、manifest 版本及必需文件，从更新日志提取该版本说明，并创建正式 GitHub Release。使用 GitHub 自动生成的源码包即可，无需额外上传附件。
4. 等待插件市场同步、审核和收录。市场收录完成后，详情页会显示该版本供用户选择。

如果 Tag 已推送但 Release 尚未创建，可在仓库 Actions 页面手动运行 **Publish plugin release**，填写已有 Tag。历史版本需在其实际对应的提交上创建 Tag 和 Release；不要把多个版本号都指向当前代码。

已经收录的 Tag 与提交应保持不变；后续修复请使用新版本号发布。完整要求见[插件市场贡献指南](https://github.com/Mai-with-u/plugin-repo/blob/main/CONTRIBUTING.md)和[多版本说明](https://github.com/Mai-with-u/plugin-repo/blob/main/VERSIONING.md)。

---

---

## 自然语言交互 🆕

本插件提供 `gemini_generate_image`、`gemini_selfie`、`gemini_selfie_video` 三个原生 Tool，允许 Bot 理解自然语言的绘图、自拍和自拍视频请求。

**触发示例**:
- "帮我画一只戴眼镜的猫"
- "生成一张赛博朋克风格的街道图片"
- "画个太阳"

**注意**: 此功能依赖于 Bot 的核心 LLM 具备意图识别（Function Calling 或 ReAct）能力。如果 Bot 认为用户的意图是绘图，它会自动调用本插件进行生成。Tool 会先发送一条等待提示（文案见 `[wait_notice]` 配置），并持续等待生成及发送完成；同一轮里 Planner 即使同时规划了 `reply`，插件也会移除该回复，防止图片还没发出就说"已经发给你了"。生成期间其他插件若触发 Replyer，也会被约束为只能使用"正在准备"的进行时表述。

> 代价说明：媒体 Tool 是同步执行的，生成期间该会话的对话循环会被阻塞（宿主 RPC 上限图片 300 秒、视频 600 秒；插件会扣除返回余量并主动收尾，实际不会跑满上限）。这是为换取"绝不提前宣称已发送"而有意接受的取舍。

---

## 指令列表

### 用户指令

| 指令                  | 功能                                                                          |
| :-------------------- | :---------------------------------------------------------------------------- |
| `/基咪绘图帮助`       | 显示本帮助菜单，列出所有可用指令，推荐使用指令添加api渠道不在配置文件中添加。 |
| `/绘图 {描述词}`      | **[新]** 纯文本生图，根据您的描述生成图片。                                   |
| `/+{风格指令}`        | 使用预设的风格指令进行绘图 (例如 `/+手办化`, `/+自拍`)。                      |
| `/bnn {自定义prompt}` | 使用完全自定义的 prompt 进行绘图。                                            |
| `/多图 {提示词}`      | **[新]** 融合至少2张图片进行绘图 (来源：回复/消息/@)。                        |
| `/图生视频 {描述词}`  | **[新]** 图生视频，根据图片和描述生成视频。                                   |
| `/文生视频 {描述词}`  | **[新]** 文生视频，纯文字描述生成视频。                                       |

**注**： `{风格指令}` 是指通过 `/添加提示词` 或管理页面维护的预设指令；它们保存在插件数据目录的 `data.json` 里，不在 `config.toml` 中。

**通用使用方法**:

- 回复一张图片 + 指令
- @一位用户 + 指令 (使用对方头像)
- 发送一张图片 + 指令
- 在同一条消息中发送 `/多图` 指令和两张或更多图片
- 直接发送指令 (使用自己头像)

指令提示词只读取用户发送的文字段，主程序生成的图片、表情和引用描述不会加入提示词。
例如发送 `/多图两个人贴贴` 并附上两张图片，请求会分别携带“两个人贴贴”和两张原图；
图片已缓存但当前消息尚未入库时，也能正常取图。

### 管理员指令

| 指令                                     | 功能                                                                                        |
| :--------------------------------------- | :------------------------------------------------------------------------------------------ |
| `/渠道添加key {渠道} {key} ...`          | **[改]** 添加指定渠道的 API Key。                                                           |
| `/渠道key列表`                           | **[改]** 查看各渠道 Key 的状态。                                                            |
| `/渠道重置key [渠道] [序号]`             | **[新]** 重置指定渠道的特定 Key、全部 Key 或所有渠道的 Key的错误次数。                      |
| `/渠道设置错误上限 {渠道} {序号} {次数}` | **[新]** 设置指定Key的错误禁用上限 (-1为永不禁用)。                                         |
| `/添加提示词 {名称}:{prompt}`            | **[新]** 动态添加一个绘图指令，**即时生效**。                                               |
| `/修改提示词 {名称}:{新prompt}`          | **[新]** 修改已存在的绘图指令，**即时生效**。                                               |
| `/删除提示词 {名称}`                     | **[新]** 动态删除一个绘图指令，**即时生效**。                                               |
| `/添加渠道 {名称}:{API地址}[:{模型}]`    | **[改]** 动态添加一个 API 渠道。支持 OpenAI 兼容、Gemini 官方、豆包和 Agnes 图片/视频格式。 |
| `/渠道修改模型 {名称} {新模型}`          | **[新]** 修改指定渠道的模型名称，加载需重启。                                               |
| `/删除渠道 {名称}`                       | **[新]** 动态删除一个自定义 API 渠道。                                                      |
| `/启用渠道 {名称}`                       | **[新]** 启用指定渠道。                                                                     |
| `/禁用渠道 {名称}`                       | **[新]** 禁用指定渠道。                                                                     |
| `/渠道设置流式 {名称} {true\|false}`     | **[新]** 设置渠道是否使用流式请求。                                                         |
| `/渠道列表`                              | **[新]** 查看所有渠道的启用/禁用状态及流式设置。                                            |
| `/渠道删除key {渠道} {序号}`             | **[新]** 删除指定渠道的指定 Key。                                                           |
| `/渠道设置视频 {名称} {true\|false}`     | **[新]** 设置渠道是否用于视频生成。                                                         |
| `/渠道设置协议 {名称} {协议\|自动}`      | **[新]** 显式指定渠道的请求协议，不再靠 URL / 模型名猜；`/渠道列表` 会显示每个渠道实际走的协议。 |
| `/渠道设置输出格式 {名称} {url\|b64_json\|默认}` | **[新]** 为 `/images/generations` 形态渠道设置 `response_format` 覆盖。               |
| `/查看提示词 {名称}`                     | 查看指定提示词的完整内容。                                                                  |
| `/大香蕉提示词 {关键词}`                 | 搜索大香蕉扩展词库中的提示词，搜索结果可用 `/+ 完整名称` 调用。                              |
| `/渠道同步大香蕉`                        | 手动同步大香蕉扩展词库到 `banana_prompts.json`。                                            |
| `/渠道设置猎奇 开启\|关闭`              | 显示或隐藏大香蕉词库中标记为限制级的提示词。                                                |

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

- `channel_setup_guide` (字符串, 只读说明): 提示如何使用 `/添加渠道`、`/渠道添加key`、`/渠道列表`、`/启用渠道`、`/禁用渠道` 管理绘图 API 渠道。绘图渠道统一通过管理员指令写入外部数据文件，不在 `config.toml` 中填写 API 地址或 Key，避免密钥暴露在 WebUI 可编辑的配置里。

  绘图渠道添加示例：

  ```text
  /添加渠道 google:https://generativelanguage.googleapis.com/v1beta/models/gemini-3-pro-image-preview:generateContent
  /添加渠道 openai渠道:https://api.example.com/v1/chat/completions:gpt-4
  /渠道添加key google your-api-key
  /渠道列表
  ```

- `enable_gemini_drawer` (布尔值, 默认 `true`): 是否启用本插件。关闭后插件仍会加载，但**绘图指令与绘图 Action 都不再响应**。
- `admins` (数组, 默认 `[]`): 管理员 QQ 号列表，只有在此列表中的用户才能使用管理员指令。
  - 示例: `admins = [123456, 789012]`
- `blacklist_groups` (数组, 默认 `[]`): 群黑名单。名单内的群无法使用绘图、自拍与视频生成功能。
  - 示例: `blacklist_groups = [16095807]`

### `[proxy]` - 代理设置

- `enable` (布尔值, 默认 `false`): 是否为 API 请求启用代理。
- `proxy_url` (字符串, 默认 `"http://127.0.0.1:7890"`): 你的 HTTP 代理地址。

### `[behavior]` - 行为设置 🆕

- `admin_only_mode` (布尔值, 默认 `false`): 管理员专用模式，开启后仅管理员可使用绘图功能（**指令与 Action 同时受限**）。
- `auto_recall_status` (布尔值, 默认 `true`): 是否自动撤回绘图过程中的状态提示消息。
- `success_notify_poke` (布尔值, 默认 `true`): 生成成功后使用戳一戳通知用户。
- `reply_with_image` (布尔值, 默认 `true`): 以回复触发消息的方式发送图片（开启后自动跳过成功通知）。
- `debug_mode` (布尔值, 默认 `false`): 开启额外排查日志；图片/视频响应原文和敏感凭据不会直接回显。
- `enable_banana_prompts` (布尔值, 默认 `true`): 是否启用「大香蕉」只读扩展词库，供 `/+`、`/随机绘图` 使用。
- `show_restricted` (布尔值, 默认 `false`): 是否显示大香蕉词库中标记为猎奇/限制级的提示词。
- `banana_sync_on_load` (布尔值, 默认 `false`): 插件加载时是否自动同步大香蕉词库。关闭后仅在 `/渠道同步大香蕉` 时手动同步。
- `media_cooldown_seconds` (整数, 默认 `90`): 自然语言媒体生成的防连发冷却秒数。媒体发出后对话循环不会暂停，下一轮 Planner 常常还在处理同一条用户消息而再发一张，因此需要硬性间隔。冷却只在媒体**真正发送成功**后起算（生成失败可立刻重试），按会话隔离，设为 `0` 关闭。只作用于 Tool 调用，`/绘图`、`/+` 等指令不受影响。
- `media_host_allowlist` (字符串列表, 默认 `["q1.qlogo.cn"]`): 显式允许下载的媒体域名或 IP，不含协议、端口和路径。默认包含 QQ 头像域名以兼容 fake-IP；升级保留已有列表，包括空列表。
- `allow_private_media_hosts` (布尔值, 默认 `false`): 全局允许内网媒体下载及通过代理下载任意媒体主机；仅需少数主机时使用 `media_host_allowlist`。

### `[wait_notice]` - 等待提示 🆕

媒体 Tool 会同步等待生成和发送完成，期间先发一条等待提示告诉用户"在做了"。这条提示由插件直接发出，**不经过 Replyer 和人格化插件**，所以固定文案会明显出戏——每类各配多条候选，运行时随机取一条。

- `image_messages` (字符串列表): 绘图等待提示候选。
- `selfie_messages` (字符串列表): 自拍等待提示候选。
- `video_messages` (字符串列表): 视频等待提示候选。

建议按自己 Bot 的人设改写这几组文案。把某一组清空即表示该类型不发送等待提示（媒体生成完成前用户不会收到任何消息）。

### `[api]` - 视频发送

此部分只配置发送视频所需的 NapCat 连接信息，绘图渠道说明见 `[general]` 的 `channel_setup_guide`。

- `napcat_host` (字符串, 默认 `napcat`): NapCat HTTP 服务器地址。Docker 环境下通常为 `napcat`，本地运行可设为 `localhost`。
- `napcat_port` (整数, 默认 `3033`): NapCat 正向 HTTP 端口。

旧版本 `config.toml` 中的 `api_url`、`enable_google`、`lmarena_api_*` 会在插件加载时自动迁移到渠道数据，并从配置文件中移除。

### NapCat HTTP 服务器配置 🆕

视频发送功能需要通过 NapCat 的 HTTP API 来发送视频文件。请按以下步骤配置：

1. **在 NapCat 网络配置中新建一个 HTTP 服务器**
2. **配置参数**：
   - **Port**: 与 `config.toml` 中 `api.napcat_port` 保持一致（如 `3033`）
   - **Host**: 
     - 本地运行：`localhost` 或 `127.0.0.1`
     - Docker 环境：`0.0.0.0`（不推荐在公网服务器使用）
3. **测试连通性**：确保从 MaiBot 所在环境能访问 `http://{napcat_host}:{napcat_port}`

> ⚠️ **安全提示**: 如果在公网服务器部署，不建议将 Host 设为 `0.0.0.0`，请使用 Docker 网络内部通信或设置防火墙规则。

*   `selfie.enable` (布尔值, 默认 `false`): 是否启用自拍功能 (需手动开启)。
*   `selfie.reference_image_path` (字符串, 默认 `"selfie_base.jpg"`): 人设底图文件名，实际读取位置为插件的 `assets/` 目录。
*   `selfie.base_prompt` (字符串): 人设身份与画风约束。默认要求严格保持底图人物的脸、五官、发色、发型、瞳色、体型特征和原始画风，只改变用户指定的动作、服装、表情、镜头与场景。
*   `selfie.random_actions` (数组): 随机场景/动作列表。
*   `selfie.polish_enable` (布尔值, 默认 `true`): 是否启用提示词润色 🆕。
*   `selfie.polish_model` (字符串, 默认 `"replyer"`): 润色使用的模型任务配置名（如 `replyer`、`utils`）🆕。
*   `selfie.polish_template` (字符串): 润色提示词模板。必须保留 `{original_prompt}`；默认模板禁止润色模型改变人物身份或在动漫与写实风格之间转换。

## 📸 自拍与照片生成
插件支持通过自然语言请求 Bot 发送“自拍”。
1. **启用功能**: 在配置中设置 `selfie.enable = true` 并重启插件。
2. **准备底图**: 将人设图放入插件目录下的 `assets/` 文件夹（插件启动后会自动创建此文件夹）。
3. **配置文件名**: 确保 `reference_image_path` 与图片文件名完全一致。目录中有多张底图时，只有这里指定的文件会被使用。
4. **触发**: 直接对 Bot 说“发张自拍”、“看看你的照片”。

自拍和自拍视频在 HTTP 错误后会继续尝试其他已启用的同类端点；尚未收到成功响应时，
读写超时或单端点等待超时也会在总预算内继续轮换，不把超时计入 Key 错误。
原服务仍可能在处理超时请求，因此换渠道可能重复生成；已经收到成功响应或取得视频任务 ID 时，
继续处理原任务，不重新提交。只有一个可用端点或总预算不足时停止轮换。
未确认超时的继续轮换仅用于自拍和自拍视频，普通绘图与视频指令仍在读写超时后停止自动重提。

生成时，底图会作为图生图参考发送给绘图端点。为减少角色漂移，建议保留默认的 `base_prompt` 和 `polish_template` 身份约束；如需补充人物设定，请追加确定的外观信息，不要删除“保持同一人物”和“保持原始画风”等约束。底图清晰、正脸、遮挡少时，人物一致性通常更稳定，但最终效果仍取决于所用图像模型的参考图遵循能力。

### 自定义渠道配置
自定义渠道的配置数据存储于 `gemini_drawer/data.json` 中，建议使用管理员指令进行管理。

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

4.  **gpt-image 系列** (按 URL 自动选择形态，详见下节):
    *   Model: **必须指定**且需包含 `gpt-image`（支持带命名空间前缀，如 `l0veyou/gpt-image-2-16-9`）

### gpt-image 系列（两种形态）🆕

模型名含 `gpt-image` 的渠道由同一个协议接管，具体发往哪里**由你填的 URL 决定**，无需额外开关：

| 渠道 URL | 形态 | 请求 |
| --- | --- | --- |
| 含 `/chat/completions` | chat | 按 OpenAI chat 协议原样发到该地址，参考图以 Data URI 放在 `messages` 里 |
| 其他（图片端点或 API base） | images | 文生图发 `/v1/images/generations`；图生图走 multipart 上传 `/v1/images/edits` |

```text
# 只暴露 chat 端点的中转站 / 本地 web2api 桥
/添加渠道 gpt_image2:http://host.docker.internal:4399/v1/chat/completions:gpt-image-2

# OpenAI 官方或支持图片端点的中转：直接填图片端点
/添加渠道 gpt官方:https://api.openai.com/v1/images/generations:gpt-image-2
/渠道添加key gpt官方 sk-xxxx
```

- **chat 形态**：适用于只暴露 `/chat/completions` 的中转站与本地 web2api 桥（这类服务通常没有 `/v1/images/generations`，旧版本会把地址改写成图片端点因而必然失败）。请求体与 OpenAI 兼容协议完全一致，图片从响应的文本、Markdown 图片或 `b64_json` 中提取。默认非流式；需要 SSE 时用 `/渠道设置流式 <渠道名> true` 单独开启。
- **images 形态**：文生图用 JSON，图生图用 `multipart/form-data` 上传原图。URL 会先归一成 base 再拼接，所以直接填 `/v1/images/generations`、填 `/v1/images/edits`、填 API base（`https://api.example.com` 或 `https://api.example.com/v1`）都能得到正确端点，不会出现 `/v1/images/generations/v1/images/generations`。

### Agnes Image / Video 2.5 Flash

通过管理员指令添加两个独立渠道，然后分别添加 Agnes API Key：

```text
/添加渠道 agnes图片:https://apihub.agnes-ai.com/v1/images/generations:agnes-image-2.5-flash
/渠道添加key agnes图片 YOUR_AGNES_API_KEY

/添加渠道 agnes视频:https://apihub.agnes-ai.com/v1/videos:agnes-video-2.5-flash
/渠道添加key agnes视频 YOUR_AGNES_API_KEY
```

- **图片**：支持文生图、单图编辑和多图合成。默认输出 `2K`、`1:1`；参考图以 Data URI Base64 数组放在 `extra_body.image`，输出格式使用 `extra_body.response_format: "url"`，也能解析服务返回的 `b64_json`。
- **视频**：默认 `720P`、`5` 秒、`16:9`。文生视频使用 `mode=text`；图生视频和视频自拍使用 `mode=keyframe`，直接把参考图的 Data URI Base64 放入 `first_frame`，无需额外图床或上传步骤。目前沿用插件的单张参考图入口，不新增尾帧、多图视频或音频参考命令。
- **异步任务**：创建后使用响应中的 `video_id` 和对应 `model_name` 查询 `/agnesapi`；只有 `status=completed` 时才从 `metadata.url`（文档格式）或顶层 `url`（线上返回格式）取地址下载和发送。若任务完成但地址暂不可用，会继续查询同一任务，而不是立即判定生成失败。正常每 5 秒查询一次；临时网络错误、429 和 5xx 会退避重试，遵守 `Retry-After`，成功查询后保留退避间隔，不重新提交任务。生成阶段总上限为 420 秒，为宿主 600 秒的视频超时留出下载和发送时间。已创建任务的轮询超时会停止等待，不会换 Key/渠道重建，也不误计 Key 失效；本地超时不代表服务端任务已取消。协议明确报告 `status=failed` 时，可按渠道轮换规则继续尝试。
- **渠道识别**：Agnes 图片通过 `agnes-image-` 模型前缀选择专用协议，不会误用豆包的请求字段；视频使用完整的 `/v1/videos` 地址及 `agnes-video-` 模型。反向代理可保留前置路径，例如 `/agnes/v1/videos` 对应查询 `/agnes/agnesapi`。
- **视频标记**：新添加的 Agnes 视频渠道会自动设置为视频渠道。已有渠道若未标记，可执行 `/渠道设置视频 agnes视频 true`。这两个 API 不走 SSE，无需开启流式。
- **失败换渠道**：尚未收到创建成功响应时，HTTP 错误（包括 500/502/503/504/408）会继续尝试下一个已启用且有有效 Key 的端点；服务端故障和限流不累计 Key 错误。普通绘图及视频命令的读写超时仍停止自动重提；自拍和自拍视频可在未收到成功响应时继续轮换超时端点。成功响应后的解析失败、在途任务的查询故障或成片下载失败不会重新提交。仅启用一个视频端点时没有备用可切换。
- **使用**：`/绘图 描述`、回复图片后 `/bnn 描述`、`/多图 描述`、`/文生视频 描述`、回复图片后 `/图生视频 描述`；自然语言自拍/视频自拍沿用原有入口。若同时启用多个同类渠道，插件仍按既有顺序尝试；单独验证 Agnes 时可临时禁用其他同类渠道。

协议参考：[图片文档](https://www.agnes-ai.com/zh-Hans/docs/agnes-image-25-flash)、[视频文档](https://www.agnes-ai.com/zh-Hans/docs/agnes-video-25-flash)。

### 渠道协议（protocol）🆕

每个渠道用哪种请求格式，由 `protocol` 字段决定。可选值固定为市面上常见的几种：

| 协议 id | 适用 | 说明 |
| :--- | :--- | :--- |
| `openai_chat` | 绘图 / 视频 | OpenAI `/chat/completions` 兼容，参考图以 Data URI 放进 messages（lmarena、web2api 等中转） |
| `gpt_image` | 绘图 | OpenAI gpt-image 系列，按 URL 自动选 `/images/*` 或 `/chat/completions` 形态 |
| `images_json` | 绘图 | 豆包 seedream 及同协议中转：`/images/generations`，JSON 带 `image` / `response_format` |
| `gemini` | 绘图 / 视频 | Google Gemini 原生 `generateContent` |
| `agnes_image` | 绘图 | Agnes Image，参数放在 `extra_body` |
| `tsai` | 绘图 | TS-AI 建任务 + 轮询 |
| `seedance_video` | 视频 | 豆包 seedance：`/contents/generations/tasks` 建任务 + 轮询 |
| `agnes_video` | 视频 | Agnes Video：`/v1/videos` 建任务 + 轮询 |
| `tsai_video` | 视频 | TS-AI 视频：`?endpoint=video_generation` |

```text
/渠道设置协议 gpt_image2 images_json   # 显式指定
/渠道设置协议 gpt_image2 自动          # 清除，回到自动识别
/渠道列表                              # 每个渠道下方显示「协议: xxx」或「协议: xxx(自动)」
```

- **没写 `protocol` 的老渠道**照旧按 URL / 模型名自动识别，行为与之前完全一致；`/渠道列表` 会把识别结果标成 `(自动)`。识别不到的渠道会提示用 `/渠道设置协议` 指定。
- **显式指定后不再猜**：例如把 `grok-imagine-image-2.0` 配在 `/v1/images/generations` 上，以前只能靠"模型名不含 gpt-image 且不是 agnes"才落到豆包协议；现在直接写 `images_json` 即可，改模型名也不会串到别的协议。
- 常见别名会自动归一：`doubao` / `seedream` → `images_json`，`openai` → `openai_chat`，`gpt-image` → `gpt_image`，`seedance` → `seedance_video`，`agnes` → `agnes_image`。
- 协议必须与渠道类型匹配：绘图渠道只能设绘图协议，视频渠道（`/渠道设置视频 ... true`）只能设视频协议；`openai_chat` 和 `gemini` 两边通用。
- 也可以直接在 `data.json` 的渠道条目里加 `"protocol": "images_json"`；非法值会被当作未设置并在日志里提醒。
- `/添加渠道` 语法不变，仍按 URL 形态做基本校验；添加后如需固定协议再执行一次 `/渠道设置协议`。

### 图片渠道输出格式（response_format）

`/images/generations` 形态的图片渠道（豆包及同协议的中转）默认请求 `response_format: "url"`。有些中转只允许 `b64_json`，否则返回 `400 private_media_required`（典型场景：把 `grok-imagine-image-2.0` 之类的模型配在 `/v1/images/generations` 地址上）。可以按渠道单独覆盖：

```text
/渠道设置输出格式 gpt_image2 b64_json
/渠道设置输出格式 gpt_image2 默认      # 清除覆盖，回到协议默认值
```

- 只对走豆包协议的 `/images/generations` 渠道生效；`gpt-image` 系列和 Agnes 图片渠道有各自的协议，不读取该项。
- 设置后 `/渠道列表` 会显示 `[b64_json]` 标签；`b64_json` 响应会按原有的 `data[].b64_json` 解析并直接发图。
- 也可以直接在 `data.json` 的渠道条目里加 `"response_format": "b64_json"`。

### 流式请求配置

某些 API 端点需要使用流式请求（SSE）来获取响应。插件支持为每个自定义渠道单独配置是否使用流式请求。

- **默认行为**: 新添加的自定义渠道默认**不使用**流式请求（`stream: false`）。
- **LMArena**: 内置的 LMArena 渠道默认**使用**流式请求。
- **设置方法**: 使用 `/渠道设置流式 <渠道名称> <true|false>` 命令切换。
  - 例如: `/渠道设置流式 PockGo true` 为 PockGo 渠道启用流式请求。
- **查看状态**: 在 `/渠道列表` 中，启用了流式请求的渠道会显示 `[流式]` 标签。

- `/绘图 <描述>`: 根据描述生成图片（文生图）。
- `/+ <指令名>`: 调用已添加的提示词进行绘图（如 `/+赛博朋克`）。
- `/添加提示词 <指令名>:<提示词>`: 添加一个新的绘图指令。
- `/删除提示词 <指令名>`: 删除一个绘图指令。
- `/添加渠道 <名称>:<URL>[:模型]`: 添加一个新的 API 渠道；类型按 URL 自动识别，模型在图片/视频渠道上必填。
- `/删除渠道 <名称>`: 删除一个渠道。
- `/渠道列表`: 查看所有渠道及其状态。
- `/渠道修改模型 <名称> <新模型>`: 修改渠道使用的模型。
- `/查看提示词 <名称>`: 查看指定提示词的完整内容。

### 4. 数据存储

插件将所有提示词（prompts）和渠道（channels）配置存储在插件数据目录的 `data.json` 文件中
（Docker 部署为 `/MaiMBot/data/gemini_drawer/data.json`；直接部署为 `data/MaiMBot/gemini_drawer/data.json`）。
这个文件在首次运行时会自动创建，如果之前有 `config.toml` 配置，也会自动迁移数据。

**下载地址策略**：图片与视频的下载目标会先校验 scheme、主机与 DNS 解析结果，并逐跳校验重定向；
默认拒绝内网、回环与链路本地地址（防 SSRF）。重定向到其他站点时会移除请求里的认证头，
HTTPS→HTTP 的降级跳转直接拒绝；直连会在发送请求前校验并固定连接 IP，同时保留原始
Host 与 TLS 名称。本地绘图渠道需要下载本地地址时，推荐在 `behavior.media_host_allowlist`
填写可信主机；`behavior.allow_private_media_hosts` 则是允许所有主机的全局开关。

全新安装时，`behavior.media_host_allowlist` 默认包含 QQ 头像域名 `q1.qlogo.cn`，
用于兼容代理 fake-IP 环境下的头像下载。已有配置中显式填写的允许列表（包括空列表）会保留。

通过代理下载媒体时，默认必须在 `behavior.media_host_allowlist` 列出可信主机，包含重定向后的
媒体主机；填写域名或 IP，不包含协议、端口和路径。例如 `["q1.qlogo.cn", "cdn.example.com", "host.docker.internal"]`。
代理侧解析无法本地验证，因此本地公网 DNS 结果、fake-IP 或仅开启代理都不会自动获得放行。
生成接口的代理设置不受此媒体下载策略影响。

等待提示、生成、下载和发送共用一个绝对期限。SDK 发送结果不确定时不会自动改走 NapCat 再发一次；
只有 SDK 发送方法缺失，或明确返回未发送且没有消息 ID 时，才使用回退发送。

**注意**: 你不需要手动创建 `data/data.json`，插件会自动管理。

**预设指令（prompts）说明**

**这是插件最核心的配置部分。** 每个键值对都会自动成为一个新的绘图指令（`/+ 指令名`）。
预设保存在插件数据目录的 `data.json` 中，可用 `/添加提示词`、`/修改提示词`、`/删除提示词`
或管理页面维护；大香蕉扩展词库另存于同目录的 `banana_prompts.json`（只读，用 `/渠道同步大香蕉` 更新）。
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

## 代码结构（开发者向）

自 v1.10.0 起，绘图请求的处理拆成了三层：

| 模块           | 职责                                                                |
| -------------- | ------------------------------------------------------------------- |
| `endpoints.py` | 把「渠道配置 + 渠道 Key」组合成一批待尝试的端点，顺序即失败转移顺序 |
| `providers/`   | 各家 API 的协议差异：判定该端点归谁处理、构造请求体、解析响应       |
| `pipeline.py`  | 唯一的编排循环：遍历端点 → 发请求 → 取图 → Key 记账 → 失败转移      |

其余支撑模块：`image_source.py`（从消息中取图，回复 > 当前消息 > @用户头像）、
`guards.py`（总开关 / 群黑名单 / 管理员模式三重校验）、`video.py`（视频生成与 NapCat 发送）、
`host_bridge.py`（唯一允许 `import src.*` 的地方，所有宿主依赖均带降级）。

### 如何新增一种渠道类型？

只需加一个文件并登记两行，不需要改动命令或 Action：

1. 在 `providers/` 下新建模块，继承 `Provider` 并实现两个方法：

   ```python
   class MyProvider(Provider):
       protocol = "my_api"              # 规范协议 id，必须同时登记到 core/protocols.py
       name = "my_api"
       supports_stream = False          # 该协议是否走本插件的 SSE 解析路径

       @classmethod
       def matches(cls, endpoint) -> bool:
           return "/my/endpoint" in endpoint.url

       def build(self, endpoint, request) -> HttpCall:
           return HttpCall(
               url=endpoint.url,
               headers={"Authorization": f"Bearer {endpoint.key}"},
               json={"prompt": request.prompt, "images": request.data_urls()},
           )
   ```

   若该 API 是「建任务 + 轮询」式的异步接口，再覆写 `async def fetch(...)`（参考 `providers/tsai.py`）。

2. 在 `providers/__init__.py` 的 `REGISTRY` 中登记。**顺序有语义**：`resolve_provider()`
   返回首个 `matches()` 命中的 provider，判定条件越具体的越要靠前。例如 gpt-image 必须排在
   OpenAI 兼容之前——它要按渠道 URL 自行决定走 images 端点还是 chat 端点，顺序写反会被
   一律当成 chat 处理，images 形态的渠道就拿不到 multipart 图生图。

3. 在 `core/protocols.py` 的 `IMAGE_PROTOCOLS`（视频则 `VIDEO_PROTOCOLS`）登记同名协议 id 和一句说明，
   需要的话在 `PROTOCOL_ALIASES` 加别名。`providers/__init__.py` 导入时会断言 provider 的 `protocol`
   与 `IMAGE_PROTOCOLS` 完全一致，漏登记会直接报错。用户之后即可用 `/渠道设置协议 <渠道> my_api`
   显式指定，不依赖 `matches()` 的启发式。

4. 在 `tests/test_providers.py` 中补上 `matches()` 与 `build()` 的断言。

### 测试

```bash
pip install -r requirements.txt pytest pytest-asyncio maibot-plugin-sdk
python -m pytest
```

`tests/` 覆盖响应解析、provider 请求构造、端点顺序、准入校验、取图优先级、
管线失败转移、绘图命令完整链路，以及媒体 Tool 与 Replyer 的时序保护
（`test_tool_coordination.py`：同轮 `reply` 过滤、同轮重复媒体调用过滤、
pending 会话隔离、等待提示随机取值）。测试不参与插件运行时加载，宿主只会导入
`_manifest.json` 指向的入口模块及其依赖。

- 测试依赖 `pytest`、`pytest-asyncio` 与 `maibot-plugin-sdk`；优先使用已安装的 SDK，
  未安装时回退到仓库同级目录的 `maibot_sdk_tmp` 快照（本地开发用，来源为容器内 SDK 2.5.2）。
  两者都没有时会直接报错，不会把核心路径静默跳过。
- 插件声明支持的 SDK 范围为 `_manifest.json` 的 `sdk.min_version`–`max_version`（当前 2.5.2–2.99.99）。

### 发版

发版时同步修改 `_manifest.json` 的 `version`、`config.py` 的 `version` /
`config_version` 默认值、README 顶部版本号和带版本号的更新日志。配置默认值用于 WebUI 与配置版本升级；
启动日志的版本号由 `utils/version.py` 直接读清单文件得到，不会因为运行时
`config.toml` 里的旧副本而报错版本。`tests/test_version.py` 会校验三处
（清单 / schema 默认值 / README 顶部）一致，改漏了会直接失败。

---

## API Key 及后端说明

### WebUI 数据管理（v1.14.0）

支持自定义插件页面的 MaiBot WebUI 会在侧栏“插件扩展”下显示 **Gemini 绘图管理**。也可以从插件列表底部“管理插件页面”进入。

- **渠道与 Key**：渠道信息和所属 Key 在同一列表展示，可就地添加、编辑、删除或重置 Key；未匹配渠道的历史 Key 在页面底部保留管理入口。
- **渠道**：通过弹框添加、编辑渠道，配置 API 地址、模型、图片/视频类型、协议、返回格式和启用状态，也可删除渠道。
- **提示词**：按名称或全文搜索、分页浏览，添加、修改和删除本地提示词。
- **Key**：通过弹框添加或替换密钥，调整所属渠道、状态及错误禁用上限；重置可启用密钥并清零错误次数。渠道下添加时自动填写所属渠道，Key 较多时支持按渠道分页。

页面直接读取插件当前使用的外部数据目录，渠道和提示词保存到 `data.json`，Key 保存到 `keys.json`。这些操作不迁移数据，也不会把业务数据写入 `config.toml`。Docker 部署下，数据仍位于 `/MaiMBot/data/gemini_drawer/`（本项目宿主机对应 `data/MaiMBot/gemini_drawer/`）。

Key 默认遮盖且不回显明文，编辑时留空表示保留旧密钥。编辑保存会保留最新使用统计和未修改的额外字段；记录发生编辑冲突时页面保留输入并提示重新打开。删除渠道会保留其 Key，便于之后恢复渠道或重新分配。渠道和提示词的名称作为引用标识，编辑时保持原名称。

**大香蕉词库（v1.14.1）**：独立的 `banana_prompts.json` 通过“大香蕉词库”标签页只读展示，支持按名称、分类或全文搜索、分页和弹框查看全文，并显示限制级标记、本地同步时间与云端更新时间。不提供新增、编辑或删除词条的入口，后端也拒绝这些写操作。

点击“更新词库”会复用 `/渠道同步大香蕉` 的云端同步逻辑，后台获取和校验词库后只写回 `banana_prompts.json`；页面显示进度与结果，更新失败保留已有缓存，重复点击不会重复创建同步任务。运行时的限制级过滤配置保持原样。原有聊天管理指令仍可使用，修改后点击页面“刷新”即可看到最新数据。

本页面使用当前项目宿主支持的 WebUI v2 自定义页面和受限 API 桥接，需要 dashboard 支持 `custom_pages`。在线 [WebUI 页面文档](https://docs.mai-mai.org/plugin/webui-pages) 当前介绍的是 v1 声明式组件；本实现沿用页面声明与本插件静态 API 绑定机制，页面模式以当前宿主协议为准。

---

本插件支持三种类型的后端，并会自动轮询尝试：

1.  **LMArena (本地/自部署)**
    - **特点**: 免费，需要在本地或服务器上自行部署 [WebAI2API](https://github.com/foxhui/WebAI2API)。
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
