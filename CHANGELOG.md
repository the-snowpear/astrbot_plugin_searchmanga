# 更新日志 (Changelog)

本项目的所有显著变更均记录在此文件中。格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)。

---

## [0.3.0] - 2026-11-13

### 🚀 新增特性 (Added)
- **链接解析管线**（参考 `Zhalstar/astrbot_plugin_parser` 的 MIT 设计）：支持自动解析消息/QQ JSON 卡片中的 JMComic、E-Hentai、ExHentai、nHentai、Iwara（视频与图集）、Panda 链接与单独 JM 号，支持群聊白名单/黑名单、同会话防抖冷却（`parse_debounce_seconds`）与多 Bot 表情仲裁（`parse_arbiter`）。
- **新增指令与 LLM 工具**：`/解析`（别名 `/jx`、`/parse`，支持附带链接或引用含链接/转发的消息）、`/开启解析`、`/关闭解析` 与 LLM 工具 `parse_media_url`。
- **多格式输出与安全策略**：支持配置 `output_format`（`zip` / `pdf` / `merge`）、`preview_count`、`max_pages`（超页自动跳过全本）、全局默认 `nsfw_policy`（`send` / `blur` / `ignore`）+ 按群独立覆盖列表 `nsfw_group_settings`（同时作用于 `/搜本子` 搜索结果预览图与链接解析封面/预览），以及 Pillow 可视化信息卡片渲染（`render_parse_card`）。
- **模块化服务架构**：拆分为 `jm_service.py`、`ehentai_service.py`、`nhentai_service.py`、`iwara_service.py`、`packaging.py` 与统一分发 `downloaders.py`。

### 🐛 修复 (Fixed)
- **修复「下载第 N 个」引用失效与误触发**：`Comp.Reply.chain` 为空时通过 OneBot `get_msg` -> 递归查找 `forward` 段 -> `get_forward_msg` 还原合并转发条目；修复 `_mark_download_event_handled` 误传 `should_call_llm(True)` 导致的下载后重复响应；按 `(session, user)` 隔离最近搜索缓存。
- **修复 `soutubot` v2 结果归一化**：对齐 `same_image_group` / `path_segments` / `links` 优先级逻辑，保留 `alternatives` 并在下载失败时自动尝试备选源。
- **修复 `parse_download_intent` URL 劫持**：优先解析受支持的完整链接，防止其他网站路径偶然含 `jm\d+` 被当成 JM 号。
- **修复 JMComic 下载问题**：注入幂等 UTF-8 BOM 容错补丁（`\xef\xbb\xbf`）；区分 `/photo/<id>` 单话与 `/album/<id>` 全专辑；按 `(ep_idx, page_idx)` 严格排序解密避免多话文件互相覆盖。
- **修复 E-Hentai / ExHentai 下载问题**：接入 `gdata` / `showpage` API + `#gdt` / `?nw=1` HTML、动态分页、`509` / SadPanda 检测、`nl` 换源重试与图片字节校验；严格隔离站点 Cookie 不泄露给 `*.hath.network`；支持 `/s/` 阅读页反查画廊、免 GP 归档与 EH ↔ Ex 自动回退。
- **升级 nHentai 下载器**：优先使用 API v2（`/api/v2/galleries` + `/api/v2/cdn`），保留 v1 JSON / HTML 与 `curl_cffi` 回退。

## [0.2.2] - 2026-10-23

### 🐛 修复 (Fixed)
- **适配 soutubot v2 检索接口**：站点已取消首页 `m` 挑战参数与 `X-API-KEY` 鉴权，改为直接 `POST /api/search` 并在 `results` 中返回命中。旧实现会在抓取首页鉴权参数时报「无法解析 soutubot 首页鉴权参数 m」，现重写为 v2 调用，并把结果归一化为原有结构（按作品去重、按相似度降序）。
- 搜索失败提示改为 Cloudflare 挑战说明，不再误导为「对代理敏感，请确认未走代理」。

### 🚀 新增特性 (Added)
- **按来源单独配置代理 (`source_proxies`)**：可为 `soutubot` 与 `nhentai` / `ehentai` / `exhentai` / `panda` / `jmcomic` 分别指定代理。列表内配置优先于全局代理；`proxy_url` 留空或填 `direct` 表示该来源强制直连；未列出的来源回退全局代理设置；全部未配置时保持直连。

---

## [0.2.1] - 2026-10-22

### 🐛 修复 (Fixed)
- **Python 3.14 兼容**：`Node.uin` 现在显式转换为 `str`。AstrBot 在 Python 3.14 上使用 pydantic v2 直接校验消息组件，不再做 `int -> str` 隐式转换，原写法会在构造合并转发节点时抛出 `ValidationError`，导致搜索结果与下载提示无法发出（Python 3.12/3.13 因走 pydantic v1 兼容层而未暴露）。

### 🔧 变更 (Changed)
- 移除已废弃的 `@register(...)` 装饰器，改由 `metadata.yaml` 与 `Star` 基类自动注册，消除启动时的 `DeprecationWarning`。
- `metadata.yaml` 增加 `display_name`，管理面板展示更直观。

### 📝 文档 (Docs)
- 新增项目图标与 SVG 视觉素材，重排中英双语文档。

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
