# --------------------------------------------------------------------------------
# gemini_file_renamer
#
# 功能:
# - 使用 Google Gemini 2.5 Flash (API) 批量重命名 PDF, EPUB, AZW3, DOCX 文件。
# - 同时提取文件开头和结尾的文本内容，以提高对书籍（版权页在后）和期刊（信息在前）的识别准确率。
# - 根据识别出的标题、作者、出版社等信息生成规范化文件名。
# - 自动处理API速率限制（RPM/TPM）和每日请求总数限制。
# - 支持断点续传（按天）和API错误重试。
#
# 使用前请先安装必要的库:
# pip install google-generativeai pymupdf pathvalidate tqdm python-docx EbookLib beautifulsoup4
#
# 配置:
# 1. 将您的 Google API 密钥设置为环境变量 `GOOGLE_API_KEY`。
# 2. 或者直接在程序提示时输入密钥。
# 3. 将需要重命名的文件放入 `files_to_rename` 文件夹 (或在运行时指定其他文件夹)。
#
# 运行:
# python gemini_file_renamer.py [可选的文件夹路径]
# --------------------------------------------------------------------------------

import os
import sys
import json
import time
import asyncio
import logging
import argparse
from pathlib import Path
from datetime import date
from collections import deque
from bs4 import BeautifulSoup

# 第三方库
import google.generativeai as genai
import pymupdf  # PyMuPDF for PDF
from docx import Document  # python-docx for DOCX
from ebooklib import epub, ITEM_DOCUMENT  # EbookLib for EPUB/AZW3
from pathvalidate import sanitize_filename
from tqdm.asyncio import tqdm


# --- 1. 初始化与配置 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def configure_api_key():
    """从环境变量或用户输入中获取并配置API密钥。"""
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        print("-----------------------------------------------------------------")
        print("未找到 GOOGLE_API_KEY 环境变量。")
        api_key = input("请输入您的 Google API 密钥 (输入后按回车): ").strip()
        print("-----------------------------------------------------------------")
        if not api_key:
            logging.error("错误：未提供 API 密钥，程序即将退出。")
            sys.exit(1)
    try:
        genai.configure(api_key=api_key)
        _ = genai.get_model('models/gemini-2.5-flash')
        logging.info("Google API 密钥配置成功。")
    except Exception as e:
        logging.error(f"API 密钥配置失败，请检查您的密钥是否正确。错误: {e}")
        sys.exit(1)


# --- 2. 全局常量与模型定义 ---
# 默认文件名模板
DEFAULT_FILENAME_TEMPLATE = "{title} - {authors} ({optional})"  # Change this line

# 可选字段及其在模板中的显示
OPTIONAL_FIELDS = {
    "translators": "译者：{translators}",
    "editors": "编者：{editors}",
    "publisher_or_journal": "{publisher_or_journal}",
    "publication_date": "{publication_date}"}

# 根据官方免费额度设定
RPM_LIMIT = 10  # 每分钟请求数 (保守设置)
TPM_LIMIT = 250000  # 每分钟令牌数 (保守设置)
DAILY_REQUEST_LIMIT = 245  # 每日请求数上限 (保守设置, 免费版为250)
MAX_TOKENS_PER_REQUEST = 30000 # 单次请求的安全Token上限
MAX_RETRIES = 3 # API请求失败后的最大重试次数

SUPPORTED_EXTENSIONS = ['.pdf', '.epub', '.azw3', '.docx']

MODEL = genai.GenerativeModel('models/gemini-2.5-flash')
JSON_SCHEMA = {
    "type": "object", "properties": { "title": {"type": "string"}, "authors": {"type": "array", "items": {"type": "string"}}, "translators": {"type": "string"}, "editors": {"type": "string"}, "publisher_or_journal": {"type": "string"}, "journal_volume_issue": {"type": "string"}, "publication_date": {"type": "string"}, "start_page": {"type": "integer"}}, "required": ["title", "authors"]
}
GENERATION_CONFIG = {"response_mime_type": "application/json", "response_schema": JSON_SCHEMA}
API_PROMPT_INSTRUCTION = """
Analyze the following document text, which may consist of excerpts from the beginning and end of the file.
Extract the metadata according to the provided JSON schema.
For fields like authors, editors, and translators, if none are found, return an empty array [].
For string fields like publisher_or_journal and publication_date, if not found, return an empty string "".
Do not add any commentary. Only return the JSON object.
"""


# --- 3. 速率控制器  ---
class RateLimiter:
    """控制对API的请求速率，避免超出限制。"""
    def __init__(self, rpm, tpm):
        self.rpm = rpm
        self.tpm = tpm
        self.request_timestamps = deque()
        self.token_timestamps = deque()

    async def wait_for_slot(self, tokens_needed):
        while True:
            now = time.time()
            
            while self.request_timestamps and self.request_timestamps[0] < now - 60:
                self.request_timestamps.popleft()
            while self.token_timestamps and self.token_timestamps[0][0] < now - 60:
                self.token_timestamps.popleft()

            current_requests = len(self.request_timestamps)
            current_tokens = sum(t[1] for t in self.token_timestamps)

            if current_requests < self.rpm and (current_tokens + tokens_needed) <= self.tpm:
                self.request_timestamps.append(now)
                self.token_timestamps.append((now, tokens_needed))
                break
            
            wait_time = 1.0
            rpm_wait = (self.request_timestamps[0] + 60) - now if current_requests >= self.rpm and self.request_timestamps else 0
            
            tpm_wait = 0
            if (current_tokens + tokens_needed) > self.tpm and self.token_timestamps:
                tokens_to_free = (current_tokens + tokens_needed) - self.tpm
                freed_tokens = 0
                wait_until_ts = 0
                for ts, tk in self.token_timestamps:
                    freed_tokens += tk
                    if freed_tokens >= tokens_to_free:
                        wait_until_ts = ts
                        break
                if wait_until_ts > 0:
                    tpm_wait = (wait_until_ts + 60) - now

            wait_time = max(wait_time, rpm_wait, tpm_wait)
            logging.warning(f"速率限制已达上限。等待 {wait_time:.2f} 秒...")
            await asyncio.sleep(max(0, wait_time))

# --- 4. 核心异步功能函数 ---
async def process_file(item, limiter, pbar):
    """异步处理单个文件，并遵守速率限制和重试机制。"""
    if not item: return

    prompt = f"{API_PROMPT_INSTRUCTION}\n\n--- DOCUMENT TEXT FROM: {item['path'].name} ---\n{item['text']}"

    for attempt in range(MAX_RETRIES):
        try:
            await limiter.wait_for_slot(item['tokens'])
            
            response = await MODEL.generate_content_async(prompt, generation_config=GENERATION_CONFIG)
            
            info = json.loads(response.text)
            new_name = build_filename(info)
            rename_file(item['path'], new_name)
            
            pbar.update(1)
            return # Success, exit loop
        except json.JSONDecodeError:
            logging.error(f"JSON解析失败: {item['path'].name}. API返回: {getattr(response, 'text', 'N/A')[:200]}...")
            break # Don't retry on malformed JSON
        except Exception as e:
            logging.error(f"处理文件 {item['path'].name} 时出错 (尝试 {attempt + 1}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES - 1:
                wait_time = 2 ** (attempt + 1)
                logging.info(f"将在 {wait_time} 秒后重试...")
                await asyncio.sleep(wait_time)
            else:
                logging.error(f"已达到最大重试次数，放弃文件: {item['path'].name}")
                break # Max retries reached
    
    pbar.update(1) # Still update progress bar for failed files

# --- 5. 辅助函数 ---

# --- 5a. 每日请求跟踪模块 ---
TRACKER_FILE = Path("./request_tracker.json")

def load_request_tracker():
    """加载每日请求计数器。如果日期是新的，则重置计数器。"""
    today_str = date.today().isoformat()
    if not TRACKER_FILE.exists():
        return {"date": today_str, "count": 0}
    try:
        with open(TRACKER_FILE, 'r') as f:
            tracker = json.load(f)
        if tracker.get("date") != today_str:
            logging.info("新的一天，重置每日API请求计数器。")
            return {"date": today_str, "count": 0}
        return tracker
    except (json.JSONDecodeError, IOError) as e:
        logging.warning(f"读取请求跟踪文件失败，将重新开始计数。错误: {e}")
        return {"date": today_str, "count": 0}

def save_request_tracker(tracker_data):
    """保存每日请求计数器。"""
    try:
        with open(TRACKER_FILE, 'w') as f:
            json.dump(tracker_data, f, indent=4)
    except IOError as e:
        logging.error(f"保存请求跟踪文件失败: {e}")

# --- 5b. 文本提取模块 ---
def _extract_from_pdf(pdf_path, num_pages_start=4, num_pages_end=3):
    """从PDF文件提取开头和结尾几页的文本。"""
    text_content = []
    try:
        with pymupdf.open(pdf_path) as doc:
            total_pages = doc.page_count
            
            start_page_nums = list(range(min(num_pages_start, total_pages)))
            for i in start_page_nums:
                text_content.append(doc[i].get_text())

            end_page_start_index = max(len(start_page_nums), total_pages - num_pages_end)
            if end_page_start_index < total_pages:
                text_content.append("\n\n--- DOCUMENT END CONTENT ---\n\n")
                for i in range(end_page_start_index, total_pages):
                    text_content.append(doc[i].get_text())
    except Exception as e:
        logging.error(f"提取PDF文本时出错: {pdf_path.name}, 错误: {e}")
        return ""
    return "".join(text_content)

def _extract_from_epub(epub_path, num_chapters_start=5, num_chapters_end=4, max_chars=25000):
    """从EPUB或无DRM的AZW3文件提取开头和结尾几章的文本。"""
    text_content = []
    try:
        book = epub.read_epub(epub_path)
        doc_items = list(book.get_items_of_type(ITEM_DOCUMENT))
        total_chapters = len(doc_items)
        
        items_to_process = []
        if total_chapters <= num_chapters_start + num_chapters_end:
            items_to_process = doc_items
        else:
            items_to_process.extend(doc_items[:num_chapters_start])
            items_to_process.append(None) # Separator
            items_to_process.extend(doc_items[-num_chapters_end:])

        for item in items_to_process:
            if item is None:
                text_content.append("\n\n--- DOCUMENT END CONTENT ---\n\n")
                continue
            soup = BeautifulSoup(item.get_body_content(), 'html.parser')
            text_content.append(soup.get_text("\n", strip=True))
    except Exception as e:
        error_msg = f"提取 EPUB/AZW3 文本时出错: {epub_path.name}, 错误: {e}"
        if "Bad Zip file" in str(e):
            logging.error(f"{error_msg}. The file might be corrupted or not a valid AZW3/EPUB.")
        else:
            logging.error(error_msg)
        return ""
        
    full_text = "\n\n".join(text_content)
    return full_text[:max_chars]

def _extract_from_docx(docx_path, num_paras_start=20, num_paras_end=15):
    """从DOCX文件提取开头和结尾几个段落的文本。"""
    text_content = []
    try:
        doc = Document(docx_path)
        all_paras = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
        total_paras = len(all_paras)

        if total_paras <= num_paras_start + num_paras_end:
            text_content = all_paras
        else:
            text_content.extend(all_paras[:num_paras_start])
            text_content.append("\n\n--- DOCUMENT END CONTENT ---\n\n")
            text_content.extend(all_paras[-num_paras_end:])
    except Exception as e:
        logging.error(f"提取 DOCX 文本时出错: {docx_path.name}, 错误: {e}")
        return ""
        
    return "\n".join(text_content)

def extract_text_from_file(file_path):
    """根据文件类型分派到相应的文本提取函数。"""
    extension = file_path.suffix.lower()
    try:
        if extension == '.pdf':
            return _extract_from_pdf(file_path)
        elif extension in ['.epub', '.azw3']:
            return _extract_from_epub(file_path)
        elif extension == '.docx':
            return _extract_from_docx(file_path)
        else:
            logging.warning(f"不支持的文件类型: {file_path.name}")
            return None
    except Exception as e:
        if extension in ['.epub', '.azw3'] and "DRM" in str(e):
             logging.error(f"提取文本失败: {file_path.name}. 文件可能受DRM保护。")
        else:
            logging.error(f"提取文本时出错: {file_path.name}, 错误: {e}")
        return None

# --- 5c. 文件名构建与重命名模块 ---
def build_filename(info):
    """根据API返回的元数据和用户定义的模板构建规范化文件名。"""
    if not info or not info.get('title'):
        return None

    # 使用户可以自定义模板
    template = os.getenv("FILENAME_TEMPLATE", DEFAULT_FILENAME_TEMPLATE)

    # 准备可选字段字符串
    optional_str = _build_optional_parts_string(info, OPTIONAL_FIELDS)

    # 构建字段字典，处理缺失值
    fields = {
        "title": info.get("title", "无标题").strip(),
        "authors": "、".join(info.get("authors", [])).strip() or "作者不详",
        "optional": optional_str
    }

    # 应用模板，处理 KeyError
    try:
        filename = template.format(**fields)
    except KeyError as e:
        logging.error(f"文件名模板中使用了 API 输出中不存在的字段：{e}。请检查您的模板或 API 架构。")
        return None
    # 移除末尾可能产生的空格
    return filename.strip().replace(" ()", "") if optional_str == "" else filename.strip()

def _build_optional_parts_string(info, optional_fields):
    """构建可选字段字符串，如果字段值为空，则跳过。"""
    parts = []
    if info.get("translators"):
        parts.append(f"{info.get('translators')} 译")
    if info.get("editors"):
        if info.get("publisher_or_journal") and not any(x in info.get("publisher_or_journal").lower() for x in ["journal", "periodical", "review", "quarterly", "magazine", "bulletin", "transactions", "proceedings", "gazette", "record", "series", "report", "annals", "yearbook", "newsletter", "forum", "advances", "studies", "letters", "notes"]):
            parts.append(f"{info.get('editors')} 编")
        else: # 如果是期刊，保留编辑，但不加“编者”标签
            parts.append(f"{info.get('editors')} 编") # Still include editors without label if it seems like a journal
    if info.get("publisher_or_journal"):
        parts.append(info.get("publisher_or_journal"))
    if info.get("journal_volume_issue"):
        parts.append(info.get("journal_volume_issue")) 
    if info.get("publication_date"):
        parts.append(f"({info.get('publication_date')})")
    if info.get("start_page") and info.get("publisher_or_journal") and any(x in info.get("publisher_or_journal").lower() for x in ["journal", "periodical", "review", "quarterly", "magazine", "bulletin", "transactions", "proceedings", "gazette", "record", "series", "report", "annals", "yearbook", "newsletter", "forum", "advances", "studies", "letters", "notes"]):
        parts.append(f"p{info.get('start_page')}")
    return ", ".join(part for part in parts if part) 


def rename_file(original_path, new_base_name):
    """安全地重命名文件，保留原始扩展名并处理文件名冲突。"""
    if not new_base_name:
        logging.warning(f"无法为 {original_path.name} 构建有效文件名，跳过。")
        return
        
    safe_base_name = sanitize_filename(new_base_name)
    new_path = original_path.with_name(f"{safe_base_name}{original_path.suffix}")
    
    counter = 1
    while new_path.exists() and new_path != original_path:
        new_path = original_path.with_name(f"{safe_base_name}_{counter}{original_path.suffix}")
        counter += 1
        
    if new_path == original_path:
        logging.info(f"文件名 '{original_path.name}' 已符合格式，无需重命名。")
    else:
        try:
            original_path.rename(new_path)
            logging.info(f"成功: '{original_path.name}' -> '{new_path.name}'")
        except OSError as e:
            logging.error(f"重命名文件时出错: {original_path.name} -> {new_path.name}, 错误: {e}")

# --- 5d. 命令行参数解析 ---
def get_args():
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        description="使用Gemini API批量智能重命名PDF、EPUB、AZW3、DOCX等格式的文件。",
        formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        "directory",
        nargs='?',
        default="./files_to_rename",
        help="包含待处理文件的目录路径 (默认为: ./files_to_rename)"
    )
    return parser.parse_args()


# --- 6. 主执行逻辑 ---
async def main():
    """主函数，协调整个重命名流程。"""
    configure_api_key()
    start_time = time.time()
    
    request_tracker = load_request_tracker()
    requests_made_today = request_tracker.get("count", 0)
    
    requests_left_today = DAILY_REQUEST_LIMIT - requests_made_today
    if requests_left_today <= 0:
        logging.error(f"今日已达到每日API请求限额 ({requests_made_today}/{DAILY_REQUEST_LIMIT})。请明天再试。")
        return

    args = get_args()
    target_directory = Path(args.directory)

    if not target_directory.is_dir():
        logging.info(f"目标目录不存在，已创建: {target_directory}。请将文件放入后重新运行。")
        target_directory.mkdir(exist_ok=True)
        return

    all_files = [p for ext in SUPPORTED_EXTENSIONS for p in target_directory.glob(f"*{ext}")]
    
    if not all_files:
        logging.info(f"在目录 {target_directory} 中未找到任何支持的文件 {SUPPORTED_EXTENSIONS}。")
        return

    print(f"找到 {len(all_files)} 个支持的文件。开始提取文本内容...")
    
    all_file_data = []
    for file_path in tqdm(all_files, desc="提取文本进度"):
        text = extract_text_from_file(file_path)
        if not text: continue
        
        text = text[:MAX_TOKENS_PER_REQUEST]
        
        try:
            tokens = await MODEL.count_tokens_async(text)
            all_file_data.append({'path': file_path, 'text': text, 'tokens': tokens.total_tokens})
        except Exception as e:
            logging.error(f"计算Token时出错 ({file_path.name}): {e}")

    if not all_file_data:
        logging.warning("所有文件的文本内容都未能成功提取，程序退出。")
        return

    files_to_process_count = len(all_file_data)
    num_to_process_this_run = min(files_to_process_count, requests_left_today)
    
    if num_to_process_this_run <= 0:
        logging.info("今日已无剩余API请求配额可处理新文件。")
        return

    if num_to_process_this_run < files_to_process_count:
        logging.warning(f"文件总数 ({files_to_process_count}) 超过今日剩余配额 ({requests_left_today})。")
        logging.warning(f"本次运行将只处理前 {num_to_process_this_run} 个文件。")
        files_to_process = all_file_data[:num_to_process_this_run]
    else:
        files_to_process = all_file_data

    print(f"文本提取完毕。本次将处理 {len(files_to_process)} 个文件。开始提交API进行重命名...")
    limiter = RateLimiter(RPM_LIMIT, TPM_LIMIT)
    
    tasks = []
    with tqdm(total=len(files_to_process), desc="重命名进度") as pbar:
        for item in files_to_process:
            task = asyncio.create_task(process_file(item, limiter, pbar))
            tasks.append(task)
        
        await asyncio.gather(*tasks)
        
    processed_count = len(files_to_process)
    request_tracker["count"] += processed_count
    save_request_tracker(request_tracker)
    logging.info(f"本次运行处理了 {processed_count} 个文件。今日已用配额: {request_tracker['count']}/{DAILY_REQUEST_LIMIT}。")
    
    print(f"\n所有文件处理完毕。总耗时: {time.time() - start_time:.2f} 秒。")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n程序被用户中断。")
        sys.exit(0)
