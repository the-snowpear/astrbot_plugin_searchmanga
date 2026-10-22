<div align="center">
  <img src="docs/icon.png" width="128" alt="astrbot_plugin_searchmanga" />
</div>

<h1 align="center">astrbot_plugin_searchmanga</h1>

<p align="center">
  <b>Reverse image search for doujinshi</b> · search · forward · download
</p>

<p align="center">
  <a href="https://github.com/AstrBotDevs/AstrBot"><img src="https://img.shields.io/badge/AstrBot-4.x-1f6feb?style=flat-square" alt="AstrBot"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.12%20%7C%203.14-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python"></a>
  <a href="CHANGELOG.md"><img src="https://img.shields.io/badge/version-0.2.1-22c55e?style=flat-square" alt="Version"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-8b5cf6?style=flat-square" alt="License"></a>
  <a href="https://github.com/the-snowpear/astrbot_plugin_searchmanga"><img src="https://img.shields.io/badge/platform-aiocqhttp-f97316?style=flat-square" alt="Platform"></a>
  <a href="https://github.com/the-snowpear/astrbot_plugin_searchmanga/stargazers"><img src="https://img.shields.io/github/stars/the-snowpear/astrbot_plugin_searchmanga?style=flat-square&color=f97316&label=stars" alt="Stars"></a>
  <a href="https://github.com/the-snowpear/astrbot_plugin_searchmanga/commits/master"><img src="https://img.shields.io/github/last-commit/the-snowpear/astrbot_plugin_searchmanga?style=flat-square&color=64748b" alt="Last commit"></a>
</p>

<p align="center">
  <a href="README.md">简体中文</a> · <a href="README_EN.md">English</a>
</p>

<img src="docs/banner.en.svg" width="100%" alt="SearchManga banner" />

> An AstrBot plugin for reverse doujinshi / manga search powered by [soutubot.moe](https://soutubot.moe): send an image, get the source as a QQ forward message, and download the whole gallery in one step.

---

## 📖 Table of Contents

- [✨ Key Features](#-key-features)
- [📦 Installation Guide](#-installation-guide)
- [🎮 Usage & Interactions](#-usage--interactions)
  - [1. Command Triggers](#1-command-triggers)
  - [2. Natural Language & Quoted Reply Interactions](#2-natural-language--quoted-reply-interactions)
- [🌐 Supported Platforms & Gallery Sources](#-supported-platforms--gallery-sources)
- [🚀 OneBot File Delivery Architecture](#-onebot-file-delivery-architecture)
- [⚙️ Configuration Schema](#️-configuration-schema)
  - [Full Parameter Table](#full-parameter-table)
  - [Per-Group Configuration Template](#per-group-configuration-template)
  - [Proxy Strategy Selection](#proxy-strategy-selection)
- [🤖 LLM Function Calling](#-llm-function-calling)
- [🧩 Project Structure](#-project-structure)
- [✅ Compatibility](#-compatibility)
- [❓ Frequently Asked Questions (FAQ)](#-frequently-asked-questions-faq)
- [📄 License](#-license)

---

## ✨ Key Features

- 🔍 **Accurate Reverse Image Search**: Leverages [soutubot.moe](https://soutubot.moe) to accurately identify doujinshi/manga source works, with configurable search accuracy (`factor`).
- 📑 **Forward Message Node Bundling**: Displays results in QQ forward message nodes to prevent screen flooding, including thumbnails, similarity percentage, source platform, title, and direct links.
- 📥 **Context-Aware Interactive Downloads**:
  - Quote the search result message and reply `下载第1个` ("download the 1st one") or `下载 2` to fetch the archive.
  - Download directly using gallery URLs or JMComic IDs.
- 📦 **Multi-Source Parsing & Archiving**: Built-in concurrent page-fetching and archive downloaders for nHentai, E-Hentai, ExHentai, Panda, JMComic, etc., automatically validated and packed into standard ZIP files.
- 🚄 **Three-Tier OneBot File Delivery**:
  - **Base64 Direct Upload**: Instant transmission for small files using the `base64://` scheme.
  - **NapCat Chunked Stream Upload**: Seamlessly streams large files via `upload_file_stream`, perfectly solving cross-host/cross-container communication between AstrBot and NapCat.
  - **HTTP Token Fallback**: Auto-validates callback token endpoints to ensure valid ZIP files and prevent error pages from being saved as corrupted archives.
- 🛡️ **Granular Group Controls**: Toggle thumbnail preview images and download permissions for specific QQ groups in the WebUI.
- 🤖 **LLM Tool Ecosystem**: Registers `search_doujin_by_image` and `download_doujin` for autonomous LLM agent tool invocation.

---

## 📦 Installation Guide

### Option 1: AstrBot Marketplace (Recommended)

1. Open the AstrBot Web Management Panel.
2. Go to **Extensions / Marketplace**, search for `astrbot_plugin_searchmanga`.
3. Click **Install** and restart/reload the plugin.

### Option 2: Manual Git Clone

Navigate to the `data/plugins/` directory under your AstrBot root:

```bash
cd data/plugins
git clone https://github.com/the-snowpear/astrbot_plugin_searchmanga.git
```

### Install Dependencies

Install the required Python packages in your AstrBot environment:

```bash
pip install -r data/plugins/astrbot_plugin_searchmanga/requirements.txt
```

---

## 🎮 Usage & Interactions

### 1. Command Triggers

| Command | Aliases | Parameters | Description |
| :--- | :--- | :--- | :--- |
| `/搜本子` | `/搜本`, `/soutu` | Image (optional) | Send with an attached image, or send the command and reply to an image |
| `/下载本子` | `/下载`, `/dl`, `/下本` | `<Index / URL / JM ID>` | Download the specified search result index, gallery URL, or JM ID |

> **Tip**: The plugin features an image cache (retained for 10 minutes by default). If you send an image first, sending `/搜本子` in your next message will automatically capture that image.

### 2. Natural Language & Quoted Reply Interactions

- **Quoted Reply Download**: Quote/reply to the search result forward message sent by the bot:
  - `下载第1个` / `下载第一个` (Download the 1st one)
  - `下载 2` / `下本子 3` (Download 2nd / 3rd item)
  - `帮我下载第4本` (Download the 4th book)
- **Direct JM ID Download**:
  - `下载 JM123456`
  - `下载 jm 123456`
  - `JM123456 下载`
- **Direct URL Download**:
  - `下载 https://nhentai.net/g/xxxxxx/`
  - `下载 https://e-hentai.org/g/xxxxxx/xxxxxx/`

---

## 🌐 Supported Platforms & Gallery Sources

| Platform | Mode | Description |
| :--- | :--- | :--- |
| **nHentai** | Concurrent Page Fetching | Supports multiple domains (`nhentai.net`, `nhentai.xxx`), automatic retries, and proxy acceleration. |
| **E-Hentai** | Archive / Page Fetching | Attempts fast direct download via `archiver.php` first, falling back to concurrent page fetching; supports user Cookie. |
| **ExHentai** | Archive / Page Fetching | ExHentai galleries; requires configuring a valid `exhentai_cookie` (must include `igneous`, `ipb_member_id`, `ipb_pass_hash`, etc.). |
| **Panda** | Direct Archive Download | Fast archive downloader for Panda archive mirrors. |
| **JMComic** | Chapter Fetching / Decryption | Built on the `jmcomic` Python library; supports downloading directly by album ID (`JMxxxxxx`) or URL. |

---

## 🚀 OneBot File Delivery Architecture

To ensure reliable file delivery across diverse network topologies (especially when AstrBot and NapCat run on separate machines or in isolated Docker containers), this plugin implements a three-tier adaptive delivery system:

```
[Gallery Downloaded & Packed into ZIP]
                  │
                  ▼
[File Size <= base64_file_limit_mb ?] ──── Yes ───> [1. base64:// Direct Memory Upload]
                  │ No / Failed
                  ▼
[NapCat upload_file_stream Chunking] ─────────────> [2. Cross-Host Chunked Stream Upload]
                  │ Failed / Unsupported
                  ▼
[AstrBot Token Temporary HTTP File Server] ────────> [3. Remote HTTP Fetch by OneBot Client]
```

1. **Base64 Direct Upload (`base64://`)**: Small archives under the configured threshold (default 48MB) are encoded and sent directly without HTTP dependencies.
2. **Chunked Stream Upload (`upload_file_stream`)**: For larger files, the plugin chunks and uploads data to the protocol client via NapCat stream API, eliminating shared filesystem requirements.
3. **HTTP Token Fallback**: Serves files through AstrBot's temporary token service (1-hour expiry) with magic byte validation (`PK`) to prevent saving HTTP error pages as ZIP files.

---

## ⚙️ Configuration Schema

Configure these settings in the AstrBot WebUI under **Plugin Configuration**:

### Full Parameter Table

| Key | Type | Default | Description |
| :--- | :---: | :---: | :--- |
| `max_results` | `int` | `5` | Maximum number of search entries in forward messages (sorted by similarity). |
| `similarity_threshold` | `int` | `28` | Low similarity warning threshold (%); adds `⚠ 低相似度` if below. |
| `factor` | `float` | `1.2` | Search accuracy factor for soutubot (normal: 1.2, strict: 1.4). |
| `timeout` | `int` | `60` | HTTP request timeout for image search requests (seconds). |
| `node_name` | `string`| `"搜本子"` | Display name for forward message nodes. |
| `node_uin` | `int` | `10000` | Fallback QQ number for forward message nodes (uses bot self_id if available). |
| `send_preview_image` | `bool` | `true` | Global default: whether to include cover thumbnails in forward message nodes. |
| `allow_download` | `bool` | `true` | Global default: whether to allow users to trigger downloads. |
| `download_concurrency` | `int` | `4` | Maximum concurrent image download tasks when fetching pages. |
| `download_timeout` | `int` | `600` | Overall timeout for downloading and packaging a gallery (seconds). |
| `base64_file_limit_mb` | `int` | `48` | Max size for Base64 direct transfer (MB); set to 0 to disable. |
| `stream_chunk_kb` | `int` | `256` | Chunk size (KB) for NapCat stream upload. |
| `ehentai_cookie` | `string`| `""` | Optional login cookie for E-Hentai. |
| `exhentai_cookie` | `string`| `""` | Optional login cookie for ExHentai (required for ExHentai galleries). |
| `prefer_archive_download`| `bool` | `true` | Whether to prioritize direct archive downloads for E/ExHentai. |
| `proxy_enabled` | `bool` | `false` | Enable HTTP/SOCKS proxy. |
| `proxy_policy` | `string`| `"download_only"` | Proxy scope policy (see below). |
| `proxy_url` | `string`| `"http://127.0.0.1:7897"`| Proxy address (HTTP / HTTPS). |
| `source_proxies` | `list` | `[]` | Per-source proxy overrides; entries take precedence over the global proxy. |
| `file_callback_base` | `string`| `""` | Base URL for OneBot client to access AstrBot files (e.g., `http://192.168.1.100:6185`). |
| `group_settings` | `list` | `[]` | Per-group overrides for preview images and download permissions. |

### Per-Group Configuration Template

Override settings for specific QQ groups:
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

### Proxy Strategy Selection

- `disabled`: Never use proxy.
- `download_only` (**Recommended**): Only use proxy for downloading images from nHentai / E-Hentai. **Search requests bypass proxy** (avoids Cloudflare/IP blocks from soutubot).
- `search_only`: Only search requests use proxy.
- `all`: Both search and download use proxy.

### Per-Source Proxy Overrides

`soutubot` sits behind Cloudflare and frequently challenges datacenter IPs, while gallery sites are often reachable directly — the two need opposite proxy setups. `source_proxies` lets you route each source independently:

```json
[
  { "source": "soutubot", "proxy_url": "http://172.18.0.1:7890" },
  { "source": "nhentai",  "proxy_url": "direct" }
]
```

Precedence:

1. A matching entry in the list always wins and overrides the global proxy settings.
2. An empty `proxy_url` (or `direct`) forces that source to bypass the proxy, even when the global proxy is enabled.
3. Sources not listed fall back to `proxy_enabled` / `proxy_policy` / `proxy_url`.
4. With nothing configured, every request goes direct — same as before.

Valid `source` values: `soutubot` (search), `nhentai`, `ehentai`, `exhentai`, `panda`, `jmcomic` (download).

---

## 🤖 LLM Function Calling

Native support for AstrBot LLM tool calling:

### 1. `search_doujin_by_image`
- **Purpose**: Autonomous tool calling when users ask to identify where an image or character comes from.
- **Arguments**:
  - `image_url` (*string*): Image URL to search. Leave empty to automatically extract from recent user messages.

### 2. `download_doujin`
- **Purpose**: Autonomous tool calling when users request to download a gallery, specify an index from search results, or provide a URL/JM ID.
- **Arguments**:
  - `url` (*string*): Gallery URL (supports nHentai, E-Hentai, ExHentai, JMComic, etc.).
  - `index` (*number*): Index number from the latest search result list (e.g., `1`).
  - `jm_id` (*string*): JMComic numeric album ID (e.g., `123456`).

---

## 🧩 Project Structure

```text
astrbot_plugin_searchmanga/
├── main.py            # Plugin entry: commands, LLM tools, forward messages, download orchestration
├── soutubot.py        # soutubot.moe client (auth challenge, request wrapper)
├── downloaders.py     # Per-site gallery fetching and ZIP packaging
├── onebot.py          # OneBot protocol helpers (forward, reply, file upload)
├── parsing.py         # URL / text parsing and filename sanitising
├── models.py          # Data models (SearchEntry / DownloadIntent)
├── constants.py       # Proxy policy constants
├── _conf_schema.json  # WebUI configuration schema
├── metadata.yaml      # Plugin metadata
├── docs/              # Documentation assets (SVG / icon)
└── tests/             # Unit tests for parsing and URL generation
```

---

## ✅ Compatibility

| Item | Status |
| :--- | :--- |
| AstrBot | 4.x (APIs checked against **4.29.1**) |
| Python | 3.12 / 3.13 / **3.14** |
| Platform | `aiocqhttp` (OneBot v11) |
| Protocol backend | NapCat / Lagrange and other `send_group_forward_msg` implementations |

> **Fixed in v0.2.1**: AstrBot validates message components with pydantic v2 directly on Python 3.14, which no longer coerces `int -> str`. Passing an integer as `Node.uin` raised a `ValidationError` while building forward-message nodes, so results could not be delivered. The value is now converted to `str` explicitly.
>
> **Adapted in v0.2.2**: soutubot switched to its v2 search API (no homepage `m` challenge and no `X-API-KEY`; a plain `POST /api/search` returns `results`). The plugin was updated accordingly, so the old "cannot parse auth parameter m" error is gone. See [CHANGELOG](CHANGELOG.md).

---

## ❓ Frequently Asked Questions (FAQ)

#### Q1: Search fails with `soutubot 对代理敏感` (soutubot is sensitive to proxies)?
**Answer**: soutubot enforces strict Cloudflare protection and proxy IP blacklists. Set `proxy_policy` to `download_only` so that search queries connect directly to soutubot.

#### Q2: File delivery fails or downloaded ZIP is corrupted in a cross-server setup?
**Answer**:
1. Ensure NapCat is updated to a version supporting `upload_file_stream`.
2. If using HTTP fallback, configure `file_callback_base` with an IP and port accessible by NapCat (e.g., `http://192.168.1.50:6185`), instead of `127.0.0.1`.

#### Q3: ExHentai download returns 403 or fails to parse?
**Answer**: ExHentai requires valid login cookies. Set `exhentai_cookie` in the plugin configuration (must include `ipb_member_id`, `ipb_pass_hash`, `igneous`, etc.).

#### Q4: Forward messages stopped working after upgrading to Python 3.14?
**Answer**: This is a known issue in v0.2.0 caused by stricter pydantic v2 validation of `Node.uin`. Upgrade to **v0.2.1**.

---

## 📄 License

Distributed under the [MIT License](LICENSE).
