# gemini_file_renamer

利用免费的 Google Gemini 2.5 Flash (API) 批量智能重命名您的本地文档（PDF, EPUB, AZW3, DOCX）。它能自动从文档中提取标题、作者、出版社、出版日期等元数据，并按照您喜欢的方式生成规范、统一的文件名。

This is a powerful Python script that leverages the Google Gemini API to intelligently batch-rename your local documents (PDF, EPUB, AZW3, DOCX). It automatically extracts metadata such as title, authors, publisher, and publication date from your documents and generates clean, consistent filenames according to your preferences.

## ✨ 主要功能 (Features)

-   **🤖 AI驱动的元数据提取**: 使用先进的 Gemini 模型准确识别文件名所需的关键信息（标题、作者、译者、编者、出版社/期刊、卷/期、出版日期、起始页码等）。
-   **📚 支持多种格式**: 轻松处理 PDF, EPUB, AZW3, 和 DOCX 文件。
-   **💡 智能文本分析**: 同时提取文件首部和尾部内容，优化对书籍（版权页在后）和期刊（信息在前）的识别效果。
-   **📝 高度可定制的文件名**: 支持用户通过环境变量自定义文件名模板，并提供一个合理的默认模板。
-   **⚙️ 自动API管理**:
    -   内置速率控制器，严格遵守 Gemini API 的每分钟请求数（RPM）和令牌数（TPM）限制。
    -   自动跟踪每日请求总数，避免超出免费额度，支持跨天断点续传。
-   **🔁 稳健的错误处理**: 包含API请求失败后的自动重试与指数退避机制。
-   **📊 清晰的进度显示**: 使用 `tqdm` 库提供实时进度条，直观展示处理进程。
-   **🔒 安全的密钥管理**: 优先从环境变量读取API密钥，避免硬编码带来的安全风险。
-   **跨平台兼容**: 可在 Windows, macOS, 和 Linux 上运行。

## 🔧 环境要求 (Requirements)

-   Python 3.8+
-   安装必要的依赖库。打开终端或命令行，运行以下命令：
    ```bash
    pip install google-generativeai pymupdf pathvalidate tqdm python-docx EbookLib beautifulsoup4
    ```

## 🚀 快速开始 (Getting Started)

### 1. 获取 Google API 密钥

-   访问 Google AI Studio。
-   点击 "Create API key" 创建一个新的API密钥。
-   复制生成的密钥。

### 2. 配置 API 密钥

为了安全，强烈建议使用环境变量来配置您的API密钥。

-   **在 macOS / Linux 上:**
    ```bash
    export GOOGLE_API_KEY='你的API密钥'
    ```
    *(要使其永久生效, 可以将此行添加到 `~/.bashrc`, `~/.zshrc`, 或 `~/.profile` 文件中)*

-   **在 Windows 上:**
    ```powershell
    $env:GOOGLE_API_KEY="你的API密钥"
    ```
    *(这只在当前 PowerShell 会话中有效。要永久设置，请在系统属性中设置环境变量。)*

如果未设置环境变量，脚本在运行时也会提示您直接输入密钥。

### 3. 准备文件

-   将您需要重命名的所有文件（PDF, EPUB, AZW3, DOCX）放入脚本所在目录下的 `files_to_rename` 文件夹中。
-   如果该文件夹不存在，首次运行脚本时会自动创建。
-   您也可以在运行时通过命令行参数指定任何其他文件夹。

## ▶️ 如何运行 (Usage)

1.  **对于默认目录 (`./files_to_rename`):**
    在终端中，导航到脚本所在的目录，然后运行：
    ```bash
    python gemini_file_renamer.py
    ```

2.  **对于指定目录:**
    ```bash
    python "gemini_file_renamer.py" "/path/to/your/documents"
    ```
    *(请将 `"/path/to/your/documents"` 替换为您的实际文件路径)*

脚本将开始处理文件，并显示进度条。重命名后的文件将保留在原文件夹内。

## 🎨 自定义文件名模板 (Filename Template)

您可以通过设置 `FILENAME_TEMPLATE` 环境变量来完全控制文件名的格式。

**默认模板:** `"{title} - {authors} ({optional})"`

-   **示例输出:** `表见代理中的被代理人可归责性 - 朱虎 (法学研究,2017年第2期,(2017),p58).pdf`

**可用占位符:**

-   `{title}`: 文档标题
-   `{authors}`: 文档作者，多个作者用 "、" 分隔
-   `{optional}`: 一个智能组合的字段，包含以下非空信息：
    -   译者 (`X 译`)
    -   编者 (`Y 编`，会根据是否为期刊决定是否显示“编者：”标签)
    -   出版社/期刊名
    -   期刊卷/期
    -   出版日期 (格式: `(YYYY)`)
    -   起始页码 (格式: `pXXX`, 仅期刊)
    -   这些字段将用 ", " 连接。

**自定义示例:**

假设您想要 `作者 - 标题 (出版日期).pdf` 这样的格式。

-   **在 macOS / Linux 上:**
    ```bash
    export FILENAME_TEMPLATE="{authors} - {title} ({publication_date})"
    python gemini_file_renamer.py
    ```
-   **注意:** 在自定义模板中，像 `publication_date` 这样的字段如果不存在于API返回的元数据中，会导致文件名生成失败。默认模板中的 `{optional}` 占位符则会自动处理空字段。

## 🛠️ 工作原理 (How It Works)

1.  **扫描文件**: 脚本首先扫描指定目录下的所有受支持文件。
2.  **提取文本**: 对每个文件，它会提取文件开头和结尾部分的关键文本。这种策略对于不同类型的文档（如书籍和学术文章）都能有效捕获元数据。
3.  **调用 API**: 将提取的文本发送给 Gemini API，并要求其根据预设的 JSON 结构返回元数据。
4.  **速率与配额管理**: 在每次 API 调用前，`RateLimiter` 类会检查是否超出了每分钟的请求和令牌限制。同时，脚本会读取 `request_tracker.json` 文件，确保不会超出每日的总请求限制。
5.  **构建文件名**: 根据 API 返回的元数据和文件名模板，构建新的文件名。
6.  **安全重命名**: 使用 `pathvalidate` 库清理文件名中的非法字符，并安全地重命名文件，如果新文件名已存在，则会自动添加后缀（如 `_1`, `_2`）。

## 注意事项

gemini 2.5 flash虽然速度快，但有时不甚智能，重命名的信息未必完整、准确。可采用更智能但速度更慢的gemini 2.5 pro，该模型同样可免费调用api。

脚本默认提取pdf的前4页和后3页，docx文档的前20段和后15段，epub、azw3文档的前5章和后4章。一般足以提取出出版信息。您可自行更改提取数量。

