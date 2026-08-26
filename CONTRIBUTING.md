# 贡献指南 (Contributing Guide)

感谢你对 **astrbot_plugin_searchmanga** 感兴趣！我们欢迎各种形式的贡献，包括但不限于报告 Bug、提出新功能建议、改进文档或直接提交代码。

[English](#-contributing-in-english) | [简体中文](#-参与贡献简体中文)

---

## 🇨🇳 参与贡献（简体中文）

### 1. 提交 Issue
- **Bug 反馈**：请使用 GitHub 提供的 [Bug Report 模板](.github/ISSUE_TEMPLATE/bug_report.md)，尽可能提供：
  - AstrBot 版本与协议端类型（如 NapCat）。
  - 完整的报错日志（脱敏后的日志）。
  - 复现步骤及相关配置。
- **功能建议**：请使用 [Feature Request 模板](.github/ISSUE_TEMPLATE/feature_request.md) 说明你的需求场景及期望的效果。

### 2. 代码开发与 Pull Request 流程
1. **Fork 本仓库** 并克隆到你的本地开发环境。
2. **新建分支**：`git checkout -b feature/your-feature-name` 或 `git checkout -b fix/your-bug-fix`。
3. **编码规范**：
   - 保持代码清晰，并遵循 PEP 8 代码风格。
   - 所有新增/修改的文件**必须保存为 UTF-8 无 BOM** 编码（杜绝 Windows PowerShell 编码陷阱）。
   - 如果修改了配置项或指令，请同步更新 `_conf_schema.json`、`metadata.yaml` 与中英文档。
4. **测试**：在本地 AstrBot 实例中进行功能回归与单元测试。
5. **提交 PR**：向本仓库的 `main` 分支发起 Pull Request，简要描述你的改动。

---

## 🌐 Contributing (English)

### 1. Issues
- **Bug Reports**: Please use the [Bug Report template](.github/ISSUE_TEMPLATE/bug_report.md) with runtime environment, sanitized logs, and reproduction steps.
- **Feature Requests**: Please use the [Feature Request template](.github/ISSUE_TEMPLATE/feature_request.md) describing the context and expected behavior.

### 2. Pull Requests
1. Fork and create your feature branch: `git checkout -b feature/awesome-feature`.
2. Ensure all text files use **UTF-8 without BOM** encoding.
3. Keep `metadata.yaml`, `_conf_schema.json`, and bilingual READMEs synchronized when changing configuration or commands.
4. Open a Pull Request targeting the `main` branch.
