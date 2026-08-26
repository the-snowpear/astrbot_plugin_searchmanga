# astrbot_plugin_searchmanga (搜本子)

<div align="center">

[![AstrBot](https://img.shields.io/badge/AstrBot-v3.x+-blue.svg)](https://github.com/AstrBot-Framework/AstrBot)
[![Version](https://img.shields.io/badge/version-v0.2.0-green.svg)](https://github.com/the-snowpear/astrbot_plugin_searchmanga)
[![Platform](https://img.shields.io/badge/platform-OneBot%20v11%20%2F%20aiocqhttp-orange.svg)](https://github.com/the-snowpear/astrbot_plugin_searchmanga)
[![License](https://img.shields.io/badge/license-MIT-informational.svg)](LICENSE)

*基于 [soutubot.moe](https://soutubot.moe) API 的以图搜漫画/同人志 AstrBot 插件。*  
*支持 QQ 节点合并转发呈现、全自动多平台画册下载（ZIP 打包）与 OneBot 跨机器分片直传。*

[简体中文 (README.md)](README.md) | [English (README_EN.md)](README_EN.md)

</div>

---

## 📖 目录

- [✨ 核心特性](#-核心特性)
- [📦 安装指南](#-安装指南)
- [🎮 使用方法与交互指令](#-使用方法与交互指令)
  - [1. 基础指令](#1-基础指令)
  - [2. 自然语言与引用交互](#2-自然语言与引用交互)
- [🌐 支持的平台与画册源](#-支持的平台与画册源)
- [🚀 OneBot 文件传输架构](#-onebot-文件传输架构)
- [⚙️ 配置项详解](#️-配置项详解)
  - [完整配置参数表](#完整配置参数表)
  - [群聊个性化配置模板](#群聊个性化配置模板)
  - [网络代理策略选择](#网络代理策略选择)
- [🤖 LLM Function Calling (智能体工具调用)](#-llm-function-calling-智能体工具调用)
- [❓ 常见问题与排错指南 (FAQ)](#-常见问题与排错指南-faq)
- [📄 开源许可](#-开源许可)

---

## ✨ 核心特性

- 🔍 **精准以图搜本**：借助 [soutubot.moe](https://soutubot.moe) 引擎，精准反向检索图片出自的同人志/漫画作品，支持配置搜索精度（`factor`）。
- 📑 **合并转发节点呈现**：搜索结果采用 QQ 消息节点合并转发展示，清晰呈现封面图、相似度百分比、平台来源、作品标题与详情链接，拒绝刷屏。
- 📥 **智能上下文下载**：
  - 引用搜索结果消息直接回复「`下载第1个`」或「`下载 2`」即可自动获取。
  - 支持直接输入本子链接或 JM 号一键下载。
- 📦 **多源画册解析打包**：内置针对 nHentai、E-Hentai、ExHentai、Panda、JMComic 等站点的多线程图片抓取与归档下载，自动校验并压缩为 ZIP 文件。
- 🚄 **三级 OneBot 文件传输保障**：
  - **Base64 直传**：小文件采用 `base64://` 协议秒传。
  - **NapCat 流式分片直传**：大文件通过 `upload_file_stream` 切片传输，无缝支持 AstrBot 与 NapCat 分离部署（跨物理机/跨容器）。
  - **HTTP Token 校验回调**：自适应校验回调 URL，防止错误响应被保存为坏包。
- 🛡️ **群聊细粒度权限管控**：可在管理面板中单独为不同群配置是否展示封面预览图、是否允许下载。
- 🤖 **LLM 工具生态集成**：注册了 `search_doujin_by_image` 与 `download_doujin` 两个工具，大模型可自主感知意图并调用。

---

## 📦 安装指南

### 方式一：AstrBot 插件市场一键安装（推荐）

1. 打开 AstrBot Web 管理面板。
2. 进入「扩展/插件市场」，搜索 `astrbot_plugin_searchmanga`。
3. 点击「安装」并重启或重新加载插件。

### 方式二：手动 Git 克隆

进入 AstrBot 根目录下的 `data/plugins/` 目录：

```bash
cd data/plugins
git clone https://github.com/the-snowpear/astrbot_plugin_searchmanga.git
```

### 安装 Python 依赖

在 AstrBot 的 Python 环境中安装依赖：

```bash
pip install -r data/plugins/astrbot_plugin_searchmanga/requirements.txt
```

---

## 🎮 使用方法与交互指令

### 1. 基础指令

| 指令 | 别名 | 附带参数 | 说明 |
| :--- | :--- | :--- | :--- |
| `/搜本子` | `/搜本`, `/soutu` | 可选附带图片 | 发送带有图片的指令，或发送指令后回复目标图片 |
| `/下载本子` | `/下载`, `/dl`, `/下本` | `<序号 / URL / JM号>` | 下载搜索列表中的指定序号、目标画册 URL 或 JM 号 |

> **提示**：插件拥有图片缓存机制（默认保留 10 分钟）。你在群里发图后，直接在下一条消息发送 `/搜本子` 也能自动捕获刚才发送的图片。

### 2. 自然语言与引用交互

- **引用回复下载**：长按或引用机器人返回的搜索结果合并转发消息，直接回复：
  - `下载第1个` / `下载第一个`
  - `下载 2` / `下本子 3`
  - `帮我下载第4本`
- **JM 号直接下载**：
  - `下载 JM123456`
  - `下载 jm 123456`
  - `JM123456 下载`
- **链接直接下载**：
  - `下载 https://nhentai.net/g/xxxxxx/`
  - `下载 https://e-hentai.org/g/xxxxxx/xxxxxx/`

---

## 🌐 支持的平台与画册源

| 平台名称 | 模式 | 说明 |
| :--- | :--- | :--- |
| **nHentai** | 逐页高清抓图 | 支持多域名（`nhentai.net`, `nhentai.xxx` 等），支持自动重试与代理加速 |
| **E-Hentai** | 归档 / 逐页抓取 | 优先尝试 `archiver.php` 归档直下，失败自动回退到逐页抓取；支持配置 Cookie |
| **ExHentai** | 归档 / 逐页抓取 | 里站画册，需在配置项中填写有效 `exhentai_cookie`（需包含 `igneous`, `ipb_member_id`, `ipb_pass_hash` 等） |
| **Panda** | 归档直下 | 针对熊猫归档站点的快速打包下载 |
| **JMComic (禁漫)** | 章节抓取 / 解密 | 基于 `jmcomic` 官方 SDK，支持输入 `JMxxxxxx` 纯数字 ID 或画册链接直接下载 |

---

## 🚀 OneBot 文件传输架构

为了保证在不同网络拓扑与部署环境（尤其是 AstrBot 与 QQ 协议端如 NapCat 部署在不同机器或不同 Docker 容器中）下的文件分发成功率，本插件设计了三级自适应传输链路：

```
[画册下载并打包为 ZIP]
         │
         ▼
[文件大小 <= base64_file_limit_mb ?] ──── 是 ───> 【1. base64:// 内存直传 OneBot】
         │ 否 / 发送失败
         ▼
[NapCat upload_file_stream 流式切片直传] ────────> 【2. 跨机器分片上传至协议端】
         │ 失败 / 协议端未支持
         ▼
[AstrBot Token 临时 HTTP 文件下载服务] ─────────> 【3. 协议端远程 HTTP 拉取】
```

1. **Base64 直传 (`base64://`)**：对于体积小于阈值（默认 48MB）的压缩包，直接编码传输，无网络回调依赖。
2. **流式分片上传 (`upload_file_stream`)**：针对大文件，自动切片并通过 NapCat 的流式 API 逐块上传至协议端本地，天然解决跨主机文件共享问题。
3. **HTTP 校验降级**：通过 AstrBot `file_token_service` 提供具备时效性（1小时）的下载接口，传输前自动校验 Magic Header（`PK`），防止协议端将错误页面当成 ZIP 下载。

---

## ⚙️ 配置项详解

在 AstrBot WebUI 面板「插件配置」中可按需调整：

### 完整配置参数表

| 配置项 | 类型 | 默认值 | 提示说明 |
| :--- | :---: | :---: | :--- |
| `max_results` | `int` | `5` | 合并转发中展示的最多条目数（按相似度降序排序） |
| `similarity_threshold` | `int` | `28` | 低相似度警告阈值（%），低于此数值的条目会标注 `⚠ 低相似度` |
| `factor` | `float` | `1.2` | soutubot 搜索精度参数（普通搜索 1.2，严格模式 1.4） |
| `timeout` | `int` | `60` | soutubot 搜图 HTTP 请求超时时间（秒） |
| `node_name` | `string`| `"搜本子"` | 合并转发节点的外显昵称 |
| `node_uin` | `int` | `10000` | 合并转发节点的默认 QQ 兜底号（会自动优先使用机器人自身 QQ 号） |
| `send_preview_image` | `bool` | `true` | 全局默认：是否在合并转发节点中附带封面预览图 |
| `allow_download` | `bool` | `true` | 全局默认：是否允许群员使用下载功能 |
| `download_concurrency` | `int` | `4` | 画册逐页抓图时的最大并发数（避免过高导致站点限流封禁） |
| `download_timeout` | `int` | `600` | 画册下载与压缩的总超时限制（秒） |
| `base64_file_limit_mb` | `int` | `48` | Base64 直传大小上限（MB），填 0 表示禁用并直接走流式或回调 |
| `stream_chunk_kb` | `int` | `256` | NapCat 流式上传单片分块大小（KB） |
| `ehentai_cookie` | `string`| `""` | 可选。E-Hentai 登录 Cookie（用于归档下载或限制内容访问） |
| `exhentai_cookie` | `string`| `""` | 可选。ExHentai 里站 Cookie（必须包含有效身份凭证） |
| `prefer_archive_download`| `bool` | `true` | 是否优先尝试归档直下（加快 E/ExHentai 下载速度） |
| `proxy_enabled` | `bool` | `false` | 是否开启网络代理 |
| `proxy_policy` | `string`| `"download_only"` | 代理作用范围策略（详见下文） |
| `proxy_url` | `string`| `"http://127.0.0.1:7897"`| 代理服务器地址（支持 HTTP/HTTPS/SOCKS5） |
| `file_callback_base` | `string`| `""` | 协议端访问 AstrBot 文件的基础 URL（如 `http://192.168.1.100:6185`） |
| `group_settings` | `list` | `[]` | 群聊个性化独立设置列表 |

### 群聊个性化配置模板

支持针对特定 QQ 群设置不同策略：
```json
[
  {
    "group_id": "123456789",
    "send_preview_image": false,
    "allow_download": true
  },
  {
    "group_id": "987654321",
    "send_preview_image": true,
    "allow_download": false
  }
]
```

### 网络代理策略选择

- `disabled`：完全不走代理。
- `download_only` (**推荐**)：仅在下载 nHentai / E-Hentai 图片时使用代理，**搜图请求不走代理**（避免 soutubot 对代理 IP 拦截触发 403/Cloudflare 校验）。
- `search_only`：仅搜图请求走代理。
- `all`：搜图与下载全部走代理。

---

## 🤖 LLM Function Calling (智能体工具调用)

插件原生接入了 AstrBot 大模型函数调用（Tool Calling / Function Calling）：

### 1. `search_doujin_by_image`
- **用途**：当用户意图为查询图片出处、搜索同人志/漫画时由大模型自主调用。
- **参数**：
  - `image_url` (*string*): 图片 URL。留空时自动提取用户最近发送的图片。

### 2. `download_doujin`
- **用途**：当用户要求下载漫画、下载搜索列表某一项、或提供链接/JM号时调用。
- **参数**：
  - `url` (*string*): 本子链接（支持 nHentai / E-Hentai / ExHentai / JMComic 等）。
  - `index` (*number*): 最近一次搜索结果中的条目序号（如 `1`）。
  - `jm_id` (*string*): JMComic 本子纯数字 ID（如 `123456`）。

---

## ❓ 常见问题与排错指南 (FAQ)

#### Q1: 搜本子提示 `搜索失败: ... 提示: soutubot 对代理敏感`？
**答**：soutubot 部署了严格的 Cloudflare 与代理检测机制。请将插件配置项 `proxy_policy` 设置为 `download_only`，保证搜图请求直连 soutubot。

#### Q2: 跨机器部署时，群文件发送失败或下载到的 ZIP 损坏？
**答**：
1. 确保已开启 NapCat 并更新至较新版本（支持 `upload_file_stream`）。
2. 若采用 HTTP 回调下载，请在 `file_callback_base` 中填写 NapCat 所在机器能够连通的 AstrBot IP 与端口（例如 `http://192.168.1.50:6185`），不要填写 `127.0.0.1`。

#### Q3: ExHentai 下载返回 403 或无法解析？
**答**：里站需要配额有效的 Cookie。请在配置项 `exhentai_cookie` 中填入你的 Cookie 字符串（必须包含 `ipb_member_id`、`ipb_pass_hash`、`igneous` 等字段）。

---

## 📄 开源许可

本项目基于 [MIT License](LICENSE) 协议开源。
