# Gemini 绘图插件

> **Version:** 1.0.0

本插件基于 gemini-2.5-flash-image-preview 模型，提供强大的图片二次创作能力。它可以根据用户提供的图片和指定的风格指令，生成一张全新的图片。

## 主要特性

- **动态指令**：无需修改代码，仅通过修改配置文件即可轻松添加新的绘图风格和对应指令。
- **多样化图片源**：支持多种方式获取原始图片，极大提升了使用的便利性。
- **多 API Key 管理**：自动轮换并管理多个 API Key，保证服务稳定。
- **自定义指令**：支持使用 `/bnn` 指令进行完全自定义的 prompt 绘图。
- **代理支持**：可为 API 请求配置 HTTP 代理。

---

## 安装

在使用此插件前，请确保已安装其所需的 Python 依赖。

在您的机器人项目环境的命令行中，进入本插件目录 (`plugins/gemini_drawer`)，然后运行以下指令：

```shell
pip install -r requirements.txt
```

---

## 指令列表

### 用户指令

| 指令                  | 功能                                                   |
| :-------------------- | :----------------------------------------------------- |
| `/基咪绘图帮助`       | 显示本帮助菜单，列出所有可用指令。                     |
| `/{风格指令}`         | 使用预设的风格指令进行绘图 (例如 `/手办化`, `/自拍`)。 |
| `/bnn {自定义prompt}` | 使用完全自定义的 prompt 进行绘图。                     |

**注**： `{风格指令}` 是指您在配置文件 `[prompts]` 部分定义的所有指令。

**通用使用方法**:
- 回复一张图片 + 指令
- @一位用户 + 指令 (使用对方头像)
- 发送一张图片 + 指令
- 直接发送指令 (使用自己头像)

### 管理员指令

| 指令                               | 功能                                               |
| :--------------------------------- | :------------------------------------------------- |
| `/手办化添加key {key1} [key2] ...` | 添加一个或多个 API Key，支持空格、逗号、换行分隔。 |
| `/手办化key列表`                   | 查看当前所有已配置 Key 的状态、类型和错误次数。    |
| `/手办化手动重置key`               | 将所有因失败次数过多而被禁用的 Key 重新激活。      |

---

## 配置文件说明

插件的配置文件位于插件目录下的 `config.toml`。

### `[general]` - 通用设置

- `enable_gemini_drawer` (布尔值, 默认 `true`): 是否启用本插件。
- `admins` (数组, 默认 `[]`): 管理员 QQ 号列表，只有在此列表中的用户才能使用管理员指令。
  - 示例: `admins = [123456, 789012]`

### `[proxy]` - 代理设置

- `enable` (布尔值, 默认 `false`): 是否为 API 请求启用代理。
- `proxy_url` (字符串, 默认 `"http://127.0.0.1:7890"`): 你的 HTTP 代理地址。

### `[api]` - API 端点设置

- `api_url` (字符串): Google 官方的 Gemini API 端点。
- `bailili_api_url` (字符串): 第三方兼容 API 端点。插件会自动根据 Key 的格式（是否以 `sk-` 开头）选择合适的端点。

### `[prompts]` - 核心：动态指令配置

**这是插件最核心的配置部分。** 你在这里定义的每一个键值对，都会自动成为一个新的绘图指令，仓库中prompts.text文件已经定义了一些常用的指令，你可以根据需要自行复制添加。

- **键（Key）**: 将作为机器人的指令名。例如，键为 `手办化`，对应的指令就是 `/手办化`。
- **值（Value）**: 调用该指令时，发送给 Gemini API 的 prompt 文本。

**示例**：
```toml
[prompts]
手办化 = "Please accurately transform the main subject in this photo into a realistic, masterpiece-like 1/7 scale PVC statue..."
Q版化 = "((chibi style)), ((super-deformed)), ((head-to-body ratio 1:2))..."
cos化 = "Generate a highly detailed photo of a girl cosplaying this illustration, at Comiket..."
自拍 = "selfie, best quality, from front"
```

#### **如何新增一个指令？**

非常简单，只需在此部分添加一行即可。例如，你想新增一个 `/水彩画` 的指令：

1.  打开 `config.toml` 文件。
2.  在 `[prompts]` 部分下，添加一行：
    ```toml
    水彩画 = "watercolor painting style, vibrant colors, masterpiece"
    ```
3.  重启机器人。
4.  现在你就可以使用 `/水彩画` 指令了！

---

## API Key 获取

本插件支持两种类型的 API Key，并会自动识别：

1.  **Google 官方 Key**:
    -   **获取地址**: [https://aistudio.google.com/api-keys](https://aistudio.google.com/api-keys)
    -   **特点**: Google 官方提供，需要有相应的 Google 账号和访问权限。

2.  **第三方兼容 Key**:
    -   **特点**: 通常以 `sk-` 开头，由第三方服务商提供，可能在国内网络环境下有更好的访问性。
    -   **一些已知的服务商地址**:
        -   Bailili API: [https://api.bailili.top/console/token](https://api.bailili.top/console/token)
        -   VC-AI: [https://newapi.sisuo.de/console/token](https://newapi.sisuo.de/console/token)
        -   *(请注意，第三方服务可能随时变更)*

获取 Key 后，请使用管理员指令 `/手办化添加key {您的KEY}` 将其添加至插件。
