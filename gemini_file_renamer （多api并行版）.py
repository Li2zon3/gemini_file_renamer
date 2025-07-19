# --------------------------------------------------------------------------------
# gemini_file_renamer
#
# 功能:
# - 使用 Google Gemini 2.5 Flash (API) 批量重命名 PDF, EPUB, AZW3, DOCX 文件。
# - 支持多个API密钥，在一个密钥的每日免费额度用尽时自动切换到下一个。
# - 同时提取文件开头和结尾的文本内容，以提高对书籍（版权页在后）和期刊（信息在前）的识别准确率。
# - 根据识别出的标题、作者、出版社等信息生成规范化文件名。
# - 自动处理API速率限制（RPM/TPM）和每日请求总数限制。
# - 支持断点续传（按天，按密钥追踪）和API错误重试。
#
# 使用前请先安装必要的库:
# pip install google-generativeai pymupdf pathvalidate tqdm python-docx EbookLib beautifulsoup4
#
# 配置:
# 1. 将您的一个或多个 Google API 密钥设置为环境变量 `GOOGLE_API_KEY`，用逗号分隔。
#    例如: GOOGLE_API_KEY="key_one,key_two,key_three"
# 2. 或者直接在程序提示时输入一个或多个密钥。
# 3. 将需要重命名的文件放入 `files_to_rename` 文件夹 (或在运行时指定其他文件夹)。
#
# 运行:
# python test.py [可选的文件夹路径]
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

def configure_api_keys():
    """从环境变量或用户输入中获取并配置API密钥列表。"""
    keys_str = os.getenv("GOOGLE_API_KEY")
    if not keys_str:
        print("-----------------------------------------------------------------")
        print("未找到 GOOGLE_API_KEY 环境变量。")
        keys_str = input("请输入您的一个或多个 Google API 密钥 (若有多个，请用逗号','分隔):\n").strip()
        print("-----------------------------------------------------------------")
    
    if not keys_str:
        logging.error("错误：未提供任何 API 密钥，程序即将退出。")
        sys.exit(1)
        
    api_keys = [key.strip() for key in keys_str.split(',') if key.strip()]
    
    if not api_keys:
        logging.error("错误：提供的 API 密钥为空，程序即将退出。")
        sys.exit(1)
        
    logging.info(f"找到 {len(api_keys)} 个 API 密钥。将逐个尝试。")
    return api_keys

async def switch_and_configure_api(api_key):
    """配置并验证指定的API密钥。"""
    global MODEL
    try:
        genai.configure(api_key=api_key)
        # 尝试获取模型以验证密钥是否有效
        _ = genai.get_model('models/gemini-2.5-flash') 
        MODEL = genai.GenerativeModel('models/gemini-2.5-flash')
        # 为MODEL设置API密钥属性，以便在日志中引用
        MODEL.api_key = api_key
        logging.info(f"API 密钥 (前8位: {api_key[:8]}...) 配置成功。")
        return True
    except Exception as e:
        logging.error(f"API 密钥 (前8位: {api_key[:8]}...) 配置失败。错误: {e}")
        return False


# --- 2. 全局常量与模型定义 ---
# 默认文件名模板
DEFAULT_FILENAME_TEMPLATE = "{title} - {authors} ({optional})"

# 可选字段及其在模板中的显示
OPTIONAL_FIELDS = {
    "translators": "译者：{translators}",
    "editors": "编者：{editors}",
    "publisher_or_journal": "{publisher_or_journal}",
    "publication_date": "{publication_date}"}

# 根据官方免费额度设定
RPM_LIMIT = 10  # 每分钟请求数
TPM_LIMIT = 250000  # 每分钟令牌数
DAILY_REQUEST_LIMIT = 245  # 每日请求数上限 (保守设置, 免费版为250)
MAX_TOKENS_PER_REQUEST = 30000 # 单次请求的安全Token上限
MAX_RETRIES = 3 # API请求失败后的最大重试次数

SUPPORTED_EXTENSIONS = ['.pdf', '.epub', '.azw3', '.docx']

# MODEL 将在 switch_and_configure_api 中被动态赋值
MODEL = None 
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
    if not item or MODEL is None: return False

    prompt = f"{API_PROMPT_INSTRUCTION}\n\n--- DOCUMENT TEXT FROM: {item['path'].name} ---\n{item['text']}"

    for attempt in range(MAX_RETRIES):
        try:
            await limiter.wait_for_slot(item['tokens'])
            
            response = await MODEL.generate_content_async(prompt, generation_config=GENERATION_CONFIG)
            
            info = json.loads(response.text)
            new_name = build_filename(info)
            rename_file(item['path'], new_name)
            
            pbar.update(1)
            return True # 成功
        except json.JSONDecodeError:
            logging.error(f"JSON解析失败: {item['path'].name}. API返回: {getattr(response, 'text', 'N/A')[:200]}...")
            break # 不重试格式错误的JSON
        except Exception as e:
            logging.error(f"处理文件 {item['path'].name} 时出错 (尝试 {attempt + 1}/{MAX_RETRIES}): {e}")
            # 如果是配额错误，立即返回False，由主循环处理密钥切换
            if "quota" in str(e).lower() or "exceeded" in str(e).lower() or "429" in str(e):  # 配额错误检查
                 logging.warning(f"API密钥 (前8位: {MODEL.api_key[:8]}...) 配额可能已用尽。")
                 return False # 返回失败信号，以便主循环用下一个密钥重试
            if attempt < MAX_RETRIES - 1:
                wait_time = 2 ** (attempt + 1)
                logging.info(f"将在 {wait_time} 秒后重试...")
                await asyncio.sleep(wait_time)
            else:
                logging.error(f"已达到最大重试次数，放弃文件: {item['path'].name}")
                break # 达到最大重试次数
    
    pbar.update(1) # 无论成功失败，都更新进度条，表示已尝试处理
    return False # 返回处理失败

# --- 5. 辅助函数 ---

# --- 5a. 每日请求跟踪模块 ---
PENDING_FILES_LOG = Path("./pending_files.txt")
TRACKER_FILE = Path("./request_tracker.json")

def load_request_tracker():
    """加载每日请求计数器。如果日期是新的，则重置所有计数器。"""
    today_str = date.today().isoformat()
    default_tracker = {"date": today_str, "usage": {}}
    if not TRACKER_FILE.exists():
        return default_tracker
    try:
        with open(TRACKER_FILE, 'r', encoding='utf-8') as f:
            tracker = json.load(f)
        if tracker.get("date") != today_str:
            logging.info("新的一天，重置所有API密钥的每日请求计数器。")
            return default_tracker
        # 确保 usage 键存在
        if "usage" not in tracker:
            tracker["usage"] = {}
        return tracker
    except (json.JSONDecodeError, IOError) as e:
        logging.warning(f"读取请求跟踪文件失败，将重新开始计数。错误: {e}")
        return default_tracker

def save_request_tracker(tracker_data):
    """保存每日请求计数器。"""
    try:
        with open(TRACKER_FILE, 'w', encoding='utf-8') as f:
            json.dump(tracker_data, f, indent=4, ensure_ascii=False)
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
            logging.error(f"{error_msg}. 文件可能已损坏或不是有效的 EPUB/AZW3 文件。")
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

    template = os.getenv("FILENAME_TEMPLATE", DEFAULT_FILENAME_TEMPLATE)

    optional_str = _build_optional_parts_string(info, OPTIONAL_FIELDS)

    fields = {
        "title": info.get("title", "无标题").strip(),
        "authors": "、".join(info.get("authors", [])).strip() or "作者不详",
        "optional": optional_str
    }

    try:
        filename = template.format(**fields)
    except KeyError as e:
        logging.error(f"文件名模板中使用了 API 输出中不存在的字段：{e}。请检查您的模板或 API 架构。")
        return None
    return filename.strip().replace(" ()", "") if optional_str == "" else filename.strip()

def _build_optional_parts_string(info, optional_fields):
    """构建可选字段字符串，如果字段值为空，则跳过。"""
    parts = []
    if info.get("translators"):
        parts.append(f"{info.get('translators')} 译")
    if info.get("editors"):
        if info.get("publisher_or_journal") and not any(x in info.get("publisher_or_journal").lower() for x in ["journal", "periodical", "review", "quarterly", "magazine", "bulletin", "transactions", "proceedings", "gazette", "record", "series", "report", "annals", "yearbook", "newsletter", "forum", "advances", "studies", "letters", "notes"]):
            parts.append(f"{info.get('editors')} 编")
        else:
            parts.append(f"{info.get('editors')} 编")
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

def load_pending_files():
    """加载待处理文件日志。"""
    if not PENDING_FILES_LOG.exists():
        return []
    try:
        with open(PENDING_FILES_LOG, 'r', encoding='utf-8') as f:
            return [Path(line.strip()) for line in f if line.strip()]
    except IOError as e:
        logging.warning(f"读取待处理文件日志失败: {e}。将扫描所有文件。")
        return []

def save_pending_files(file_paths):
    """保存待处理文件日志。"""
    try:
        with open(PENDING_FILES_LOG, 'w', encoding='utf-8') as f:
            for path in file_paths:
                f.write(f"{path}\n")
    except IOError as e:
        logging.error(f"无法写入待处理文件日志: {e}")

def clear_pending_files_log():
    """清空待处理文件日志。"""
    try:
        with open(PENDING_FILES_LOG, 'w', encoding='utf-8') as f:
            f.write("")  # 写入空字符串
        logging.info("待处理文件日志已清空。")
    except IOError as e:
        logging.error(f"无法清空待处理文件日志: {e}")

def delete_pending_files_log():
    """删除待处理文件日志文件"""
    try:
        if PENDING_FILES_LOG.exists():
            PENDING_FILES_LOG.unlink()
            logging.info("待处理文件日志已删除。")
        else:
            logging.info("待处理文件日志文件不存在，无需删除。")
    except OSError as e:
            logging.error(f"删除待处理文件日志文件 {PENDING_FILES_LOG} 时出错: {e}")

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
    api_keys = configure_api_keys()
    start_time = time.time()
    
    request_tracker = load_request_tracker()
    
    args = get_args()
    target_directory = Path(args.directory)

    if not target_directory.is_dir():
        logging.info(f"目标目录不存在，已创建: {target_directory}。请将文件放入后重新运行。")
        target_directory.mkdir(exist_ok=True)
        return

    # 启用递归搜索：使用 'glob(f"**/*{ext}")' 来查找子目录中的文件
    # 使用 set 来自动处理重复的文件（如果一个文件有多个支持的扩展名，虽然不太可能）
    print(f"正在目录 {target_directory} 及其所有子目录中递归搜索文件...")
    all_files = list(set([p for ext in SUPPORTED_EXTENSIONS for p in target_directory.glob(f"**/*{ext}")]))
    
    if not all_files:
        logging.info(f"在目录 {target_directory} 中未找到任何支持的文件 {SUPPORTED_EXTENSIONS}。")
        return

    print(f"找到 {len(all_files)} 个支持的文件。开始提取文本内容...")
    
    # 1. 提取所有文件的文本内容
    all_file_data_pre = []
    for file_path in tqdm(all_files, desc="提取文本进度"):
        text = extract_text_from_file(file_path)
        if not text: continue
        text = text[:MAX_TOKENS_PER_REQUEST]
        all_file_data_pre.append({'path': file_path, 'text': text, 'tokens': 0})

    if not all_file_data_pre:
        logging.warning("所有文件的文本内容都未能成功提取，程序退出。")
        return

    # 2. 寻找第一个可用密钥来计算Token
    logging.info("正在寻找可用API密钥以计算Token...")
    first_usable_key_found = False
    for api_key in api_keys:
        if await switch_and_configure_api(api_key):
            first_usable_key_found = True
            break
    
    if not first_usable_key_found:
        logging.error("所有提供的API密钥均无效，无法计算Token，程序退出。")
        return
        
    # 3. 使用有效密钥计算所有文件的Token
    logging.info("使用有效密钥计算所有文件的Token...")
    all_file_data_final = []
    for item in tqdm(all_file_data_pre, desc="计算Token进度"):
        try:
            # 现在 MODEL 已经用一个有效密钥配置好了
            tokens = await MODEL.count_tokens_async(item['text'])
            item['tokens'] = tokens.total_tokens
            all_file_data_final.append(item)
        except Exception as e:
            logging.error(f"使用密钥 {getattr(MODEL, 'api_key', 'N/A')[:8]}... 计算Token时出错 ({item['path'].name}): {e}")

    if not all_file_data_final:
        logging.warning("未能成功为任何文件计算Token，程序退出。")
        return

    # 4. 加载待处理文件日志
    pending_files = load_pending_files()

    # 5. 根据日志筛选待处理文件
    if pending_files:
        logging.info(f"找到待处理文件日志，将只处理 {len(pending_files)} 个文件。")
        files_to_process_initial = [item for item in all_file_data_final if item['path'] in pending_files]
        
        # 如果日志中的文件在本次扫描中不存在，发出警告
        missing_from_scan = set(pending_files) - {item['path'] for item in files_to_process_initial}
        if missing_from_scan:
            logging.warning(f"待处理文件日志中 {len(missing_from_scan)} 个文件在本次扫描中未找到：")
            for missing_path in missing_from_scan:
                logging.warning(f"  - {missing_path}")
    else:
        logging.info("未找到待处理文件日志，将处理所有扫描到的文件。")
        files_to_process_initial = all_file_data_final

    # 6. 开始主处理循环
    files_to_process_queue = deque(all_file_data_final)
    total_processed_this_session = 0
    limiter = RateLimiter(RPM_LIMIT, TPM_LIMIT)

    for key_index, api_key in enumerate(api_keys):
        if not files_to_process_queue:
            logging.info("队列中的所有文件均已处理完毕。")
            break

        logging.info(f"\n--- 正在尝试使用 API 密钥 #{key_index + 1} (前8位: {api_key[:8]}...) ---")
        
        if not await switch_and_configure_api(api_key):
            continue # 如果密钥无效，则尝试下一个

        requests_made_today = request_tracker["usage"].get(api_key, 0)
        requests_left_today = DAILY_REQUEST_LIMIT - requests_made_today

        if requests_left_today <= 0:
            logging.warning(f"此密钥今日已达到每日API请求限额 ({requests_made_today}/{DAILY_REQUEST_LIMIT})。")
            continue # 如果配额用尽，则尝试下一个

        num_to_process_with_this_key = min(len(files_to_process_queue), requests_left_today)
        
        logging.info(f"此密钥今日剩余配额可处理 {requests_left_today} 个文件。队列中还剩 {len(files_to_process_queue)} 个文件。")
        logging.info(f"本次将使用此密钥尝试处理 {num_to_process_with_this_key} 个文件。")

        batch_to_process = [files_to_process_queue.popleft() for _ in range(num_to_process_with_this_key)]
        
        tasks = []
        with tqdm(total=len(batch_to_process), desc=f"密钥 #{key_index+1} 进度") as pbar:
            for item in batch_to_process:
                task = asyncio.create_task(process_file(item, limiter, pbar))
                tasks.append(task)
            
            results = await asyncio.gather(*tasks)
            
            # *** 将失败的任务重新放回队列 ***
            successful_count = 0
            failed_items = []
            for i, success in enumerate(results):
                if success:
                    successful_count += 1
                else:
                    # 如果处理失败 (例如因为配额耗尽)，则将其加回队列
                    failed_items.append(batch_to_process[i])

            if failed_items:
                logging.warning(f"{len(failed_items)} 个文件处理失败，将它们放回队列以供下一个密钥尝试。")
                # 以相反的顺序加回去，以保持原始顺序
                for item in reversed(failed_items):
                    files_to_process_queue.appendleft(item)
            
            total_processed_this_session += successful_count

        # 根据本次 *尝试* 的数量更新此密钥的用量
        attempts_with_this_key = len(batch_to_process)
        request_tracker["usage"][api_key] = requests_made_today + attempts_with_this_key
        save_request_tracker(request_tracker)
        logging.info(f"密钥 (前8位: {api_key[:8]}...) 处理完成。成功 {successful_count} 个。此密钥今日累计尝试: {request_tracker['usage'][api_key]}/{DAILY_REQUEST_LIMIT}。")

    # 7. 处理完成后，判断是否需要记录剩余文件
    if files_to_process_queue:
        logging.warning(f"仍有 {len(files_to_process_queue)} 个文件未处理，将记录到待处理文件日志。")
        save_pending_files([item['path'] for item in files_to_process_queue])
    else:
        logging.info("所有文件已成功处理，清空待处理文件日志。")
        clear_pending_files_log()

    print("\n-----------------------------------------------------------------")
    if not files_to_process_queue:
        print(f"所有任务完成！本次运行共成功处理了 {total_processed_this_session} 个文件。")
        print(f"本次运行共成功处理了 {total_processed_this_session} 个文件。")
        print(f"仍有 {len(files_to_process_queue)} 个文件在队列中未处理。请明天再试或添加新密钥。")

    print(f"总耗时: {time.time() - start_time:.2f} 秒。")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n程序被用户中断。")
        sys.exit(0)
