# 更新日志 (Changelog)

本项目的所有显著变更均记录在此文件中。格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)。

---

## [0.2.0] - 2025-02-23

### 🚀 新增特性 (Added)
- **多平台画册自动下载**：
  - 支持 nHentai、E-Hentai、ExHentai、Panda 以及 JMComic (禁漫) 画册下载与自动打包为 ZIP。
  - 支持直接输入本子 URL 或纯数字 JM 号（如 `下载 JM123456`）触发下载。
  - 支持直接引用搜索结果的合并转发消息，回复「`下载第1个`」或「`下载 2`」自动解析上下文并执行下载。
- **OneBot 多级自适应文件传输链路**：
  - **Base64 直传**：小文件采用 `base64://` 协议秒传。
  - **NapCat 流式分片直传**：支持 NapCat 的 `upload_file_stream` 分块上传，天然解决 AstrBot 与 NapCat 跨机器、跨 Docker 容器部署时的文件共享问题。
  - **HTTP Token 校验回调**：基于 `file_token_service` 提供时效性回调下载，并具备 ZIP Magic Header（`PK`）自检降级机制，防止错误 HTML 保存为坏包。
- **LLM Function Calling 原生支持**：
  - 注册 `search_doujin_by_image`：支持大模型自主识图搜本。
  - 注册 `download_doujin`：支持大模型根据用户自然语言意图自主调用下载。
- **群聊细粒度独立配置 (`group_settings`)**：
  - 支持在 WebUI 中单独为指定 QQ 群开启/关闭封面预览图或下载功能。
- **代理策略扩展 (`proxy_policy`)**：
  - 支持 `disabled`、`all`、`search_only` 和 `download_only` 策略，推荐使用 `download_only` 避免触发 soutubot 代理拦截。
- **开源规范工程化**：
  - 补充中英双语独立文档 (`README.md`, `README_EN.md`)。
  - 补充 `LICENSE` (MIT), `CHANGELOG.md`, `CONTRIBUTING.md`, `SECURITY.md` 与 Issue 模板。

### 🔧 变更与优化 (Changed & Improved)
- 优化了图片缓存机制（延长至 10 分钟），提升发图后即刻搜图的容错率。
- 增强了文件名合法化清洗逻辑，防止 Windows / Linux 文件名非法字符报错。
- 针对 E-Hentai 提供优先归档与逐页抓图双模式降级。

---

## [0.1.0] - 2025-01-10

### 🚀 初始版本 (Initial Release)
- 基于 soutubot.moe API 实现以图搜本（同人志/漫画反向检索）。
- 支持 QQ 节点合并转发展示检索结果，包含相似度、来源及标题。
- 支持 `/搜本子`、`/搜本`、`/soutu` 等触发指令。
