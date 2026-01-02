+# Gemini File Renamer Project Guide (English)
+
+## Overview
+- Uses the Google Gemini API to batch-rename PDF, EPUB, AZW3, DOCX, and similar documents by extracting title, authors, publisher/journal, publication date, and related metadata.
+- Supports both the CLI script `gemini_file_renamer.py` and the GUI `gemini_file_renamer_gui.py`, each with bilingual labels.
+- Includes rate limiting, retries, resume support, and filename templating to keep names consistent.
+
+## Requirements
+- Python 3.8+
+- Install dependencies:
+  ```bash
+  pip install google-generativeai pymupdf pathvalidate tqdm python-docx EbookLib beautifulsoup4
+  ```
+- Ensure access to the Google Gemini API and prepare `GOOGLE_API_KEY`.
+
+## Obtain & Configure API Key
+1. Visit Google AI Studio and create an API key.
+2. Set the key as an environment variable:
+   - macOS/Linux:
+     ```bash
+     export GOOGLE_API_KEY="<your_api_key>"
+     ```
+   - Windows (PowerShell):
+     ```powershell
+     $env:GOOGLE_API_KEY="<your_api_key>"
+     ```
+3. If the variable is missing, the script prompts for the key at runtime.
+
+## CLI Usage
+1. Place files to rename in `files_to_rename` or pass a custom directory at runtime.
+2. Run with the default directory:
+   ```bash
+   python gemini_file_renamer.py
+   ```
+3. Specify a directory explicitly:
+   ```bash
+   python gemini_file_renamer.py "/path/to/your/documents"
+   ```
+4. Files are safely renamed in place; conflicts automatically get suffixes.
+
+## GUI Usage
+- Launch after installing dependencies:
+  ```bash
+  python gemini_file_renamer_gui.py
+  ```
+- Features:
+  - Bilingual interface with drag-and-drop folder selection.
+  - Switch between batch and single-file modes.
+  - Real-time progress/log display and optional metadata writing.
+  - Multiple API key rotation and resume support.
+
+## Filename Template
+- Control file naming with the `FILENAME_TEMPLATE` environment variable; default: `"{title} - {authors} ({optional})"`.
+- `{optional}` combines translators, editors, publisher/journal, volume/issue, publication year, and start page when present.
+- Example customization:
+  ```bash
+  export FILENAME_TEMPLATE="{authors} - {title} ({publication_date})"
+  python gemini_file_renamer.py
+  ```
+
+## Quotas & Rate Limiting
+- Default RPM/TPM checks run before each call, and daily totals are tracked in `request_tracker.json`.
+- Automatic retries with exponential backoff handle quota or API failures.
+
+## Text Extraction
+- Defaults: PDF first 4 and last 3 pages; DOCX first 20 and last 15 paragraphs; EPUB/AZW3 first 5 and last 4 chapters.
+- Adjust these limits in the script to suit specific documents.
+
+## Tips
+- For better quality, switch from `gemini-2.5-flash` to `gemini-2.5-pro` at the cost of speed.
+- Back up important files and ensure write permissions before batch runs.
+
+---
+
+# Gemini 文件重命名工具说明（中文）
+
+## 项目概览
+- 利用 Google Gemini API 批量重命名 PDF、EPUB、AZW3、DOCX 等文档，自动提取标题、作者、出版社/期刊、出版日期等元数据。
+- 支持命令行脚本 `gemini_file_renamer.py` 与图形界面 `gemini_file_renamer_gui.py`，界面提供中英文标签。
+- 内置速率限制、重试、断点续传与文件名模板化，帮助生成一致规范的文件名。
+
+## 环境要求
+- Python 3.8+
+- 安装依赖：
+  ```bash
+  pip install google-generativeai pymupdf pathvalidate tqdm python-docx EbookLib beautifulsoup4
+  ```
+- 请确保可访问 Google Gemini API，并准备好 `GOOGLE_API_KEY`。
+
+## 获取与配置 API 密钥
+1. 登录 Google AI Studio，创建 API key。
+2. 将密钥设置为环境变量：
+   - macOS/Linux:
+     ```bash
+     export GOOGLE_API_KEY="<your_api_key>"
+     ```
+   - Windows (PowerShell):
+     ```powershell
+     $env:GOOGLE_API_KEY="<your_api_key>"
+     ```
+3. 若未设置环境变量，脚本会在运行时提示输入。
+
+## 命令行用法
+1. 将待重命名文件放入 `files_to_rename` 目录，或在运行时指定自定义目录。
+2. 运行默认目录：
+   ```bash
+   python gemini_file_renamer.py
+   ```
+3. 指定目录：
+   ```bash
+   python gemini_file_renamer.py "/path/to/your/documents"
+   ```
+4. 处理完成后，文件会在原目录内被安全重命名；若重名则自动添加后缀。
+
+## 图形界面用法
+- 启动（需已安装依赖）：
+  ```bash
+  python gemini_file_renamer_gui.py
+  ```
+- 功能：
+  - 中英文界面与拖拽选择文件夹。
+  - 批处理与单文件模式切换。
+  - 实时进度、日志展示与可选的元数据写入。
+  - 支持多 API 密钥轮换与断点续传。
+
+## 自定义文件名模板
+- 通过环境变量 `FILENAME_TEMPLATE` 控制文件名格式，默认模板：`"{title} - {authors} ({optional})"`。
+- `{optional}` 会自动组合译者、编者、出版社/期刊、卷期、出版年份及起始页码等非空字段。
+- 自定义示例：
+  ```bash
+  export FILENAME_TEMPLATE="{authors} - {title} ({publication_date})"
+  python gemini_file_renamer.py
+  ```
+
+## 配额与速率限制
+- 默认每分钟请求数 (RPM) 与令牌数 (TPM) 会在调用前检查；每日总请求记录在 `request_tracker.json`。
+- 发生超额或 API 失败时会自动重试并指数退避。
+
+## 文本提取范围
+- 默认：PDF 提取前 4 页和后 3 页；DOCX 提取前 20 段与后 15 段；EPUB/AZW3 提取前 5 章与后 4 章。
+- 可在脚本中调整这些数量以适配具体文档。
+
+## 常见提示
+- 如需更高质量，可将模型从 `gemini-2.5-flash` 替换为更高精度的 `gemini-2.5-pro`（速度较慢）。
+- 批处理前建议备份重要文件，并确保输出目录有足够的写入权限。
