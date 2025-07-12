# gemini_pdf_renamer
运用免费的gemini-2.5-flash自动化批量重命名pdf文献

# 智能 PDF 批量重命名工具

这是一个功能强大的 Python 脚本，它利用 Google Gemini 2.5 Flash 的能力，自动读取 PDF 文献的内容，并根据其标题、作者、出版社、出版日期等元信息，将其智能地重命名为规范、统一的格式。

## ✨ 主要功能

-   **智能识别**：通过调用免费的 Gemini API，精准提取 PDF 的核心元信息。
-   **批量处理**：一次性处理指定目录下的所有 PDF 文件。
-   **异步高效**：利用 `asyncio` 并发处理文件，最大化处理效率。
-   **智能批处理**：自动将多个小文件合并为一个请求，以节约 API 调用次数。
-   **速率控制**：内置速率限制器，严格遵守 API 的免费使用额度（10 RPM, 250,000 TPM）。
-   **自动重试**：当遇到网络波动或 API 临时错误时，脚本会自动进行指数退避重试。
-   **安全文件名**：生成跨平台安全的文件名，并能自动处理重名文件。
-   **交互友好**：提供清晰的进度条，并能通过交互方式安全地输入 API 密钥。

## 🚀 安装与使用

### 1. 克隆仓库

```bash
git clone <你的仓库URL>
cd <你的仓库目录>
```

### 2. 安装依赖

确保你已经安装了 Python 3.8+。然后运行以下命令安装所有必需的库：

```bash
pip install -r requirements.txt
```

### 3. 配置 API 密钥

你需要一个 Google AI Studio 的 API 密钥才能使用此脚本。

你有两种方式配置密钥：

**方式一 (推荐): 设置环境变量**

这是最安全的方式，你的密钥不会出现在任何代码或命令行历史中。

-   **macOS / Linux**:
    ```bash
    export GOOGLE_API_KEY="你的API密钥"
    ```
-   **Windows (CMD)**:
    ```bash
    set GOOGLE_API_KEY="你的API密钥"
    ```
-   **Windows (PowerShell)**:
    ```bash
    $env:GOOGLE_API_KEY="你的API密钥"
    ```

**方式二: 交互式输入**

如果你不想设置环境变量，可以直接运行脚本。脚本在启动时会检测是否存在环境变量，如果不存在，会提示你输入 API 密钥。

### 4. 运行脚本

将所有需要重命名的 PDF 文件放入一个文件夹（例如，项目根目录下的 `pdfs_to_rename` 文件夹）。

然后运行脚本，并指定该文件夹的路径：

```bash
python gemini_pdf_renamer.py /path/to/your/pdfs
```

如果你不提供路径，脚本会默认处理当前目录下的 `pdfs_to_rename` 文件夹。

```bash
# 默认处理 ./pdfs_to_rename 文件夹
python gemini_pdf_renamer.py
```

脚本会开始处理文件，并显示一个实时更新的进度条。

## 📝 注意事项

-   脚本默认提取每个 PDF 的前 3 页内容进行分析，这对于大多数学术论文和书籍来说已经足够。你可以在代码中修改 `extract_text_from_first_pages` 函数的 `num_pages` 参数来调整。
-   请遵守 Google Gemini API 的使用政策。


