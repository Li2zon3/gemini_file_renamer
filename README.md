# Gemini File Renamer (CLI + GUI)

## Overview
- Batch-rename PDF/EPUB/AZW3/DOCX files using Google Gemini: extract title/authors/publisher/journal/date/keywords and generate a safe filename.
- Two entrypoints:
  - CLI: `gemini_file_renamer.py`
  - GUI: `gemini_file_renamer_gui.py`
- Features: rate limiting, retries, resume (`pending_files.txt`), optional metadata writing, and template-based filenames.

## Requirements
- Python 3.8+
- Install dependencies:
  ```bash
  pip install -r requirements.txt
  ```
- Set `GOOGLE_API_KEY` (supports multiple keys, comma-separated):
  ```bash
  export GOOGLE_API_KEY="key1,key2"
  ```

## CLI Quickstart
- Process a directory (default: `./files_to_rename`):
  ```bash
  python gemini_file_renamer.py "/path/to/your/documents"
  ```
- Processing mode:
  - `--mode batch` (default): pack multiple files into one request
  - `--mode single`: one file per request (concurrent)
  - `--mode auto`: choose single vs batch automatically (speed-first)

### Paid Tier (Gemini 3 Flash Preview)
- Enable paid tier (defaults: `$10/Key/month`, model `gemini-3-flash-preview`, context hard cap 200k):
  ```bash
  python gemini_file_renamer.py "/path/to/your/documents" --tier paid --mode auto
  ```
- Economy mode (cost-first): in `--mode auto`, prefer batching to reduce repeated prompt/thinking overhead:
  ```bash
  python gemini_file_renamer.py "/path/to/your/documents" --tier paid --mode auto --paid-economy
  ```
- When paid budget is exhausted, default behavior is to auto-downgrade to the free model. You can stop instead:
  ```bash
  python gemini_file_renamer.py "/path/to/your/documents" --tier paid --on-budget-exceeded stop
  ```
- Show current month budget usage (no raw keys are printed):
  ```bash
  python gemini_file_renamer.py --show-budget --budget-file ./budget_tracker.json
  ```

## GUI Quickstart
- Launch:
  ```bash
  python gemini_file_renamer_gui.py
  ```
- Processing mode: Auto / Batch / Single
- Paid mode (optional): Gemini 3 Flash + monthly budget ($/Key/month) + concurrency + optional economy mode

### GUI Note (Tkinter)
- The GUI requires a Python build with Tk support.
- If your venv cannot `import tkinter` (missing `_tkinter`), create a new venv using a Tk-enabled Python:
  ```bash
  /usr/bin/python3 -m venv .venv-tk
  source .venv-tk/bin/activate
  pip install -r requirements.txt
  python gemini_file_renamer_gui.py
  ```

## Runtime State Files
- `request_tracker.json`: daily request counter per key (stores only `key_id`, not raw keys)
- `budget_tracker.json`: monthly spend per key (stores only `key_id`, nanos USD integer)
- `pending_files.txt`: resume list when some files were not processed
- `config.json`: GUI settings

## Security & Privacy
- API keys are never written to disk.
- Trackers store only a short `key_id` (sha256 prefix), not raw keys.

---

# Gemini 文件重命名工具（CLI + GUI）

## 项目概览
- 使用 Google Gemini 对 PDF/EPUB/AZW3/DOCX 等文档批量重命名：提取标题/作者/出版社或期刊/日期/关键词，并生成安全文件名。
- 两个入口：
  - 命令行：`gemini_file_renamer.py`
  - 图形界面：`gemini_file_renamer_gui.py`
- 功能：速率限制、重试、断点续传（`pending_files.txt`）、可选写入元数据、文件名模板等。

## 环境要求
- Python 3.8+
- 安装依赖：
  ```bash
  pip install -r requirements.txt
  ```
- 设置 `GOOGLE_API_KEY`（支持多个 key，用逗号分隔）：
  ```bash
  export GOOGLE_API_KEY="key1,key2"
  ```

## CLI 快速开始
- 处理目录（默认：`./files_to_rename`）：
  ```bash
  python gemini_file_renamer.py "/path/to/your/documents"
  ```
- 处理模式：
  - `--mode batch`（默认）：批处理打包多个文件
  - `--mode single`：单文件请求（并发）
  - `--mode auto`：自动选择（速度优先：少量文件单文件并发，大量文件批处理）

### 付费模式（Gemini 3 Flash Preview）
- 开启付费模式（默认：`$10/Key/月`，模型 `gemini-3-flash-preview`，上下文硬上限 200k）：
  ```bash
  python gemini_file_renamer.py "/path/to/your/documents" --tier paid --mode auto
  ```
- 省钱模式（费用优先）：在 `--mode auto` 下尽量走批处理，减少重复 prompt/thinking 开销：
  ```bash
  python gemini_file_renamer.py "/path/to/your/documents" --tier paid --mode auto --paid-economy
  ```
- 付费预算耗尽后默认自动降级到免费模型继续处理；也可选择停止：
  ```bash
  python gemini_file_renamer.py "/path/to/your/documents" --tier paid --on-budget-exceeded stop
  ```
- 查看本月预算用量（不会显示明文 key）：
  ```bash
  python gemini_file_renamer.py --show-budget --budget-file ./budget_tracker.json
  ```

## GUI 快速开始
- 启动：
  ```bash
  python gemini_file_renamer_gui.py
  ```
- 处理模式：自动 / 批处理 / 单文件
- 付费模式：Gemini 3 Flash + 月预算（$/Key/月）+ 并发 + 可选省钱模式

### GUI 说明（Tkinter）
- GUI 需要带 Tk 支持的 Python。
- 如果当前 venv 无法 `import tkinter`（缺 `_tkinter`），请用带 Tk 的 Python 创建 venv：
  ```bash
  /usr/bin/python3 -m venv .venv-tk
  source .venv-tk/bin/activate
  pip install -r requirements.txt
  python gemini_file_renamer_gui.py
  ```

## 运行时状态文件
- `request_tracker.json`：按 key 记录每日请求次数（只保存 `key_id`，不保存明文 key）
- `budget_tracker.json`：按 key 记录本月预算用量（只保存 `key_id`，金额用 nanos USD 整数）
- `pending_files.txt`：未处理完成时的断点续传列表
- `config.json`：GUI 配置

## 安全与隐私
- 程序不会把 API key 写入磁盘。
- 各种 tracker 只保存 `key_id`（sha256 前缀），不保存明文 key。

