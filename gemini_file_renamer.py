# --------------------------------------------------------------------------------
# gemini_file_renamer
#
# 功能:
# - ✅ 双模式运行: 支持高效批处理模式 (默认) 和简单的单文件处理模式 (--mode single)。
# - ✅ 高效批处理: 将多个文件打包进单次API请求，大幅提升处理速度，突破RPM限制。
# - ✅ 并发控制: 使用Semaphore精确控制并发任务数，保证程序稳定高效。
# - 智能分批: 根据Token上限自动将文件分批，确保每个文件作为原子单元处理。
# - 支持多个API密钥，在一个密钥的每日免费额度用尽时自动切换到下一个。
# - 主动追踪每个密钥的每日API使用情况（记录在 request_tracker.json）。
# - 同时提取文件开头和结尾的文本内容，以提高识别准确率。
# - 根据识别出的元数据生成规范化文件名。
# - 自动处理API速率限制（RPM/TPM）和每日请求数限制。
# - 支持强大的断点续传（按天、按密钥、按文件列表追踪）和API错误重试。
#
# 使用前请先安装必要的库:
# pip install google-generativeai pymupdf pathvalidate tqdm python-docx EbookLib beautifulsoup4
#
# 配置:
# 1. 将您的一个或多个 Google API 密钥设置为环境变量 `GOOGLE_API_KEY`，用逗号分隔。
# 2. 或者直接在程序提示时输入一个或多个密钥。
# 3. 将需要重命名的文件放入 `files_to_rename` 文件夹 (或在运行时指定其他文件夹)。
#
# 运行:
# 批处理模式 (默认): python gemini_renamer.py [可选的文件夹路径]
# 单文件模式: python gemini_renamer.py --mode single [可选的文件夹路径]
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
        _ = genai.get_model('models/gemini-2.5-flash')
        MODEL = genai.GenerativeModel('models/gemini-2.5-flash')
        MODEL.api_key = api_key # 自定义属性，用于日志记录
        logging.info(f"API 密钥 (前8位: {api_key[:8]}...) 配置成功。")
        return True
    except Exception as e:
        logging.error(f"API 密钥 (前8位: {api_key[:8]}...) 配置失败。错误: {e}")
        return False


# --- 2. 全局常量与模型定义 ---
RPM_LIMIT = 10
TPM_LIMIT = 250000
DAILY_REQUEST_LIMIT = 250
MAX_TOKENS_PER_REQUEST = 28000 # 单个请求（批处理或单文件）的安全Token上限
CONCURRENCY_LIMIT = 10 # 并发任务数
MAX_RETRIES = 3

SUPPORTED_EXTENSIONS = ['.pdf', '.epub', '.azw3', '.docx']
MODEL = None

# --- Prompts and JSON Schemas ---
API_PROMPT_INSTRUCTION_BATCH = """
Analyze the following text, which contains MULTIPLE documents concatenated together.
Each document starts with a "--- START OF FILE: [filename] ---" marker and ends with an "--- END OF FILE: [filename] ---" marker.
For EACH document provided, extract its metadata and create a corresponding JSON object.
Return a single JSON array (a list) containing all the extracted JSON objects.
The order of objects in the final list MUST match the order of the documents in the input text.
Do not add any commentary. Only return the JSON array.
"""

API_PROMPT_INSTRUCTION_SINGLE = """
Analyze the text from the following document to extract its metadata.
Based on the content, provide a JSON object with the following details.
Do not add any commentary. Only return the JSON object.
"""

SINGLE_OBJECT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "authors": {"type": "array", "items": {"type": "string"}},
        "translators": {"type": "string"},
        "editors": {"type": "string"},
        "publisher_or_journal": {"type": "string"},
        "journal_volume_issue": {"type": "string"},
        "publication_date": {"type": "string"},
        "start_page": {"type": "integer"}
    },
    "required": ["title", "authors"]
}

JSON_SCHEMA_BATCH = {
    "type": "array",
    "items": SINGLE_OBJECT_SCHEMA
}

# --- 3. 速率控制器 ---
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
            while self.request_timestamps and self.request_timestamps[0] < now - 60: self.request_timestamps.popleft()
            while self.token_timestamps and self.token_timestamps[0][0] < now - 60: self.token_timestamps.popleft()
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
                freed_tokens, wait_until_ts = 0, 0
                for ts, tk in self.token_timestamps:
                    freed_tokens += tk
                    if freed_tokens >= tokens_to_free: wait_until_ts = ts; break
                if wait_until_ts > 0: tpm_wait = (wait_until_ts + 60) - now
            wait_time = max(wait_time, rpm_wait, tpm_wait)
            logging.warning(f"速率限制已达上限。等待 {wait_time:.2f} 秒...")
            await asyncio.sleep(max(0, wait_time))

# --- 4. 核心异步功能函数 ---
async def process_batch(batch, batch_tokens, limiter, pbar, semaphore):
    """(批处理模式) 异步处理单个批次的文件，并使用Semaphore控制并发。"""
    async with semaphore:
        if not batch or MODEL is None:
            pbar.update(len(batch))
            return {"success": False, "failed_items": batch, "quota_exceeded": False}

        prompt_parts = [API_PROMPT_INSTRUCTION_BATCH]
        for item in batch:
            prompt_parts.append(f"\n\n--- START OF FILE: {item['path'].name} ---\n")
            prompt_parts.append(item['text'])
            prompt_parts.append(f"\n--- END OF FILE: {item['path'].name} ---")
        full_prompt = "".join(prompt_parts)
        generation_config = {"response_mime_type": "application/json", "response_schema": JSON_SCHEMA_BATCH}

        for attempt in range(MAX_RETRIES):
            try:
                await limiter.wait_for_slot(batch_tokens)
                response = await MODEL.generate_content_async(full_prompt, generation_config=generation_config)
                results = json.loads(response.text)

                if not isinstance(results, list) or len(results) != len(batch):
                    logging.error(f"批处理返回结果格式错误或数量不匹配。预期 {len(batch)} 个，得到 {len(results)} 个。跳过此批次。")
                    pbar.update(len(batch))
                    return {"success": False, "failed_items": batch, "quota_exceeded": False}

                for i, info in enumerate(results):
                    original_item = batch[i]
                    new_name = build_filename(info)
                    rename_file(original_item['path'], new_name)

                pbar.update(len(batch))
                return {"success": True, "failed_items": [], "quota_exceeded": False}

            except json.JSONDecodeError:
                logging.error(f"批处理JSON解析失败: API返回: {getattr(response, 'text', 'N/A')[:200]}...")
                break
            except Exception as e:
                logging.error(f"处理批次时出错 (尝试 {attempt + 1}/{MAX_RETRIES}): {e}")
                is_quota_error = "quota" in str(e).lower() or "exceeded" in str(e).lower() or "429" in str(e)
                if is_quota_error:
                    logging.warning(f"API密钥 (前8位: {MODEL.api_key[:8]}...) 配额可能已用尽。")
                    pbar.update(len(batch))
                    return {"success": False, "failed_items": batch, "quota_exceeded": True}
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(2 ** (attempt + 1))
                else:
                    logging.error("已达到最大重试次数，放弃此批次。")
                    break
        
        pbar.update(len(batch))
        return {"success": False, "failed_items": batch, "quota_exceeded": False}

async def process_single_file(file_item, limiter, pbar):
    """(单文件模式) 异步处理单个文件。"""
    if not file_item or MODEL is None:
        pbar.update(1)
        return {"success": False, "failed_item": file_item, "quota_exceeded": False}

    prompt_parts = [API_PROMPT_INSTRUCTION_SINGLE, "\n\n", file_item['text']]
    full_prompt = "".join(prompt_parts)
    generation_config = {"response_mime_type": "application/json", "response_schema": SINGLE_OBJECT_SCHEMA}

    for attempt in range(MAX_RETRIES):
        try:
            await limiter.wait_for_slot(file_item['tokens'])
            response = await MODEL.generate_content_async(full_prompt, generation_config=generation_config)
            info = json.loads(response.text)
            
            new_name = build_filename(info)
            rename_file(file_item['path'], new_name)
            
            pbar.update(1)
            return {"success": True, "failed_item": None, "quota_exceeded": False}

        except json.JSONDecodeError:
            logging.error(f"文件 {file_item['path'].name} 的JSON解析失败: API返回: {getattr(response, 'text', 'N/A')[:200]}...")
            break
        except Exception as e:
            logging.error(f"处理文件 {file_item['path'].name} 时出错 (尝试 {attempt + 1}/{MAX_RETRIES}): {e}")
            is_quota_error = "quota" in str(e).lower() or "exceeded" in str(e).lower() or "429" in str(e)
            if is_quota_error:
                logging.warning(f"API密钥 (前8位: {MODEL.api_key[:8]}...) 配额可能已用尽。")
                pbar.update(1)
                return {"success": False, "failed_item": file_item, "quota_exceeded": True}
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(2 ** (attempt + 1))
            else:
                logging.error(f"文件 {file_item['path'].name} 已达到最大重试次数，放弃。")
                break
    
    pbar.update(1)
    return {"success": False, "failed_item": file_item, "quota_exceeded": False}


# --- 5. 辅助函数 ---
# --- 5a. 每日请求跟踪模块 ---
PENDING_FILES_LOG = Path("./pending_files.txt")
TRACKER_FILE = Path("./request_tracker.json")

def load_request_tracker():
    today_str = date.today().isoformat()
    default_tracker = {"date": today_str, "usage": {}}
    if not TRACKER_FILE.exists(): return default_tracker
    try:
        with open(TRACKER_FILE, 'r', encoding='utf-8') as f: tracker = json.load(f)
        if tracker.get("date") != today_str:
            logging.info("新的一天，重置所有API密钥的每日请求计数器。")
            return default_tracker
        if "usage" not in tracker: tracker["usage"] = {}
        return tracker
    except (json.JSONDecodeError, IOError) as e:
        logging.warning(f"读取请求跟踪文件失败，将重新开始计数。错误: {e}")
        return default_tracker

def save_request_tracker(tracker_data):
    try:
        with open(TRACKER_FILE, 'w', encoding='utf-8') as f: json.dump(tracker_data, f, indent=4, ensure_ascii=False)
    except IOError as e: logging.error(f"保存请求跟踪文件失败: {e}")

# --- 5b. 文本提取模块 ---
def _extract_from_pdf(pdf_path, num_pages_start=4, num_pages_end=3):
    text_content = []
    try:
        with pymupdf.open(pdf_path) as doc:
            total_pages = doc.page_count
            start_page_nums = list(range(min(num_pages_start, total_pages)))
            for i in start_page_nums: text_content.append(doc[i].get_text())
            end_page_start_index = max(len(start_page_nums), total_pages - num_pages_end)
            if end_page_start_index < total_pages:
                text_content.append("\n\n--- DOCUMENT END CONTENT ---\n\n")
                for i in range(end_page_start_index, total_pages): text_content.append(doc[i].get_text())
    except Exception as e: logging.error(f"提取PDF文本时出错: {pdf_path.name}, 错误: {e}"); return ""
    return "".join(text_content)

def _extract_from_epub(epub_path, num_chapters_start=5, num_chapters_end=4):
    text_content = []
    try:
        book = epub.read_epub(epub_path)
        doc_items = list(book.get_items_of_type(ITEM_DOCUMENT))
        total_chapters = len(doc_items)
        items_to_process = []
        if total_chapters <= num_chapters_start + num_chapters_end: items_to_process = doc_items
        else:
            items_to_process.extend(doc_items[:num_chapters_start])
            items_to_process.append(None)
            items_to_process.extend(doc_items[-num_chapters_end:])
        for item in items_to_process:
            if item is None: text_content.append("\n\n--- DOCUMENT END CONTENT ---\n\n"); continue
            soup = BeautifulSoup(item.get_body_content(), 'html.parser')
            text_content.append(soup.get_text("\n", strip=True))
    except Exception as e: logging.error(f"提取 EPUB/AZW3 文本时出错: {epub_path.name}, 错误: {e}"); return ""
    return "\n\n".join(text_content)

def _extract_from_docx(docx_path, num_paras_start=20, num_paras_end=15):
    text_content = []
    try:
        doc = Document(docx_path)
        all_paras = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
        total_paras = len(all_paras)
        if total_paras <= num_paras_start + num_paras_end: text_content = all_paras
        else:
            text_content.extend(all_paras[:num_paras_start])
            text_content.append("\n\n--- DOCUMENT END CONTENT ---\n\n")
            text_content.extend(all_paras[-num_paras_end:])
    except Exception as e: logging.error(f"提取 DOCX 文本时出错: {docx_path.name}, 错误: {e}"); return ""
    return "\n".join(text_content)

def extract_text_from_file(file_path):
    extension = file_path.suffix.lower()
    text_to_extract = ""
    try:
        if extension == '.pdf': text_to_extract = _extract_from_pdf(file_path)
        elif extension in ['.epub', '.azw3']: text_to_extract = _extract_from_epub(file_path)
        elif extension == '.docx': text_to_extract = _extract_from_docx(file_path)
        else: logging.warning(f"不支持的文件类型: {file_path.name}"); return None
        # 粗略截断，避免单个文件过大。更精细的控制在分批时进行。
        return text_to_extract[:int(MAX_TOKENS_PER_REQUEST * 0.9)]
    except Exception as e: logging.error(f"提取文本时出错: {file_path.name}, 错误: {e}"); return None

# --- 5c. 文件名构建与重命名模块 ---
JOURNAL_KEYWORDS = [
    "journal", "review", "proceedings", "transactions", "quarterly",
    "annals", "bulletin", "magazine", "advances", "letters", "studies"
]

def build_filename(info):
    """根据API返回的元数据构建规范化、更智能的文件名。"""
    if not info or not info.get('title'):
        return None
    
    template = os.getenv("FILENAME_TEMPLATE", "{title} - {authors} ({optional})")
    parts = []
    
    translator_str = info.get("translators", "").strip()
    if translator_str and translator_str.lower() != 'null': parts.append(f"{translator_str} 译")

    editor_str = info.get("editors", "").strip()
    if editor_str and editor_str.lower() != 'null':
        publisher_str = info.get("publisher_or_journal", "").lower()
        is_journal = any(keyword in publisher_str for keyword in JOURNAL_KEYWORDS)
        if not is_journal: parts.append(f"{editor_str} 编")

    publisher_or_journal_str = info.get("publisher_or_journal", "").strip()
    if publisher_or_journal_str and publisher_or_journal_str.lower() != 'null': parts.append(publisher_or_journal_str)

    journal_volume_issue_str = info.get("journal_volume_issue", "").strip()
    if journal_volume_issue_str and journal_volume_issue_str.lower() != 'null': parts.append(journal_volume_issue_str)

    publication_date_str = info.get("publication_date", "").strip()
    if publication_date_str and publication_date_str.lower() != 'null': parts.append(f"({publication_date_str})")

    if info.get("start_page"): parts.append(f"p{info.get('start_page')}")
        
    optional_str = ", ".join(part for part in parts if part)
    
    fields = {
        "title": info.get("title", "无标题").strip(),
        "authors": "、".join(info.get("authors", [])).strip() or "作者不详",
        "optional": optional_str
    }
    
    try:
        filename = template.format(**fields)
    except KeyError as e:
        logging.error(f"文件名模板格式错误: {e}")
        return None
        
    if not optional_str:
        return filename.replace(" ()", "").strip()
    else:
        return filename.strip()

def rename_file(original_path, new_base_name):
    if not new_base_name:
        logging.warning(f"无法为 {original_path.name} 构建有效文件名，跳过。")
        return
        
    safe_base_name = sanitize_filename(new_base_name)
    new_path = original_path.with_name(f"{safe_base_name}{original_path.suffix}")
    
    counter = 1
    while new_path.exists() and new_path != original_path:
        new_path = original_path.with_name(f"{safe_base_name}_{counter}{original_path.suffix}")
        counter += 1
        
    if new_path != original_path:
        try:
            original_path.rename(new_path)
            logging.info(f"成功: '{original_path.name}' -> '{new_path.name}'")
        except OSError as e:
            logging.error(f"重命名文件时出错: {original_path.name} -> {new_path.name}, 错误: {e}")

# --- 5d. 断点续传/待处理文件日志 ---
def load_pending_files():
    if not PENDING_FILES_LOG.exists(): return []
    try:
        with open(PENDING_FILES_LOG, 'r', encoding='utf-8') as f:
            return [Path(line.strip()) for line in f if line.strip()]
    except IOError: return []

def save_pending_files(file_paths):
    try:
        with open(PENDING_FILES_LOG, 'w', encoding='utf-8') as f:
            for path in file_paths: f.write(f"{path}\n")
    except IOError as e: logging.error(f"无法写入待处理文件日志: {e}")

def clear_pending_files_log():
    if PENDING_FILES_LOG.exists():
        try: PENDING_FILES_LOG.unlink(); logging.info("待处理文件日志已清空。")
        except OSError as e: logging.error(f"无法清空待处理文件日志: {e}")

# --- 5e. 命令行参数解析 ---
def get_args():
    parser = argparse.ArgumentParser(description="使用Gemini API批量智能重命名文件。", formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("directory", nargs='?', default="./files_to_rename", help="包含待处理文件的目录路径 (默认为: ./files_to_rename)")
    parser.add_argument("--mode", choices=['batch', 'single'], default='batch', help="选择处理模式:\n'batch': 高效批处理模式 (默认)\n'single': 逐个文件处理模式")
    return parser.parse_args()


# --- 6. 主执行逻辑  ---
async def main():
    total_start_time = time.time()
    api_keys = configure_api_keys()
    request_tracker = load_request_tracker()
    args = get_args()
    target_directory = Path(args.directory)
    
    logging.info(f"运行模式: {'批处理 (Batch)' if args.mode == 'batch' else '单文件 (Single)'}")

    if not target_directory.is_dir():
        target_directory.mkdir(exist_ok=True)
        logging.info(f"目标目录不存在，已创建: {target_directory}。请将文件放入后重新运行。")
        return

    # --- 1. 确定文件处理范围 ---
    pending_paths = load_pending_files()
    if pending_paths:
        logging.info(f"检测到断点日志，将只处理上次未完成的 {len(pending_paths)} 个文件。")
        files_to_process_paths = [p for p in pending_paths if p.exists()]
        if len(files_to_process_paths) != len(pending_paths): logging.warning("部分日志中的文件已不存在，将跳过。")
    else:
        logging.info("未检测到断点日志，将扫描整个目录进行新任务。")
        files_to_process_paths = list(set([p for ext in SUPPORTED_EXTENSIONS for p in target_directory.glob(f"**/*{ext}")]))

    if not files_to_process_paths:
        logging.info("待处理文件列表为空，程序结束。")
        if pending_paths: clear_pending_files_log()
        return

    # --- 2. 文本提取与Token计算 ---
    prep_start_time = time.time()
    print(f"准备处理 {len(files_to_process_paths)} 个文件。开始提取文本并计算Token...")
    first_usable_key_found = False
    for api_key in api_keys:
        if await switch_and_configure_api(api_key):
            first_usable_key_found = True
            break
    if not first_usable_key_found:
        logging.error("所有API密钥均无效，无法计算Token，程序退出。")
        return
    
    all_file_data = []
    for file_path in tqdm(files_to_process_paths, desc="提取并计算Token"):
        text = extract_text_from_file(file_path)
        if text:
            try:
                tokens = await MODEL.count_tokens_async(text)
                if tokens.total_tokens > MAX_TOKENS_PER_REQUEST:
                     logging.warning(f"文件 {file_path.name} 的Token数({tokens.total_tokens})过大，已跳过。")
                     continue
                all_file_data.append({'path': file_path, 'text': text, 'tokens': tokens.total_tokens})
            except Exception as e:
                logging.error(f"计算Token时出错 ({file_path.name}): {e}")

    if not all_file_data:
        logging.warning("未能为任何文件成功提取文本和计算Token。")
        return

    processing_queue = deque(all_file_data)
    total_files_to_process = len(processing_queue)
    total_files_processed_this_session = 0
    limiter = RateLimiter(RPM_LIMIT, TPM_LIMIT)
    
    prep_time = time.time() - prep_start_time
    api_processing_start_time = time.time()
    
    # --- 3. 按密钥逐个/逐批处理 ---
    for key_index, api_key in enumerate(api_keys):
        if not processing_queue: break
        
        logging.info(f"\n--- 正在尝试使用 API 密钥 #{key_index + 1} (前8位: {api_key[:8]}...) ---")
        if not await switch_and_configure_api(api_key): continue
        
        requests_made_today = request_tracker["usage"].get(api_key, 0)
        requests_left_today = DAILY_REQUEST_LIMIT - requests_made_today
        if requests_left_today <= 0:
            logging.warning(f"此密钥今日配额已用尽。")
            continue
        
        logging.info(f"此密钥今日剩余请求配额: {requests_left_today} 次。")

        with tqdm(total=total_files_to_process, desc=f"密钥 #{key_index+1} 进度") as pbar:
            pbar.update(total_files_to_process - len(processing_queue))
            
            if args.mode == 'batch':
                # --- BATCH MODE LOGIC ---
                batches_to_process_queue = deque()
                current_batch, current_batch_tokens = [], 0
                temp_queue = processing_queue.copy() # 使用副本进行分批计算，不影响主队列
                
                while temp_queue:
                    file_item = temp_queue.popleft()
                    if current_batch and current_batch_tokens + file_item['tokens'] > MAX_TOKENS_PER_REQUEST:
                        batches_to_process_queue.append((current_batch, current_batch_tokens))
                        current_batch, current_batch_tokens = [file_item], file_item['tokens']
                    else:
                        current_batch.append(file_item)
                        current_batch_tokens += file_item['tokens']
                if current_batch: batches_to_process_queue.append((current_batch, current_batch_tokens))

                logging.info(f"待处理文件已分装成 {len(batches_to_process_queue)} 个批次。")
                
                num_batches_to_process = min(len(batches_to_process_queue), requests_left_today)
                if num_batches_to_process == 0 and len(batches_to_process_queue) > 0: continue

                # 从主队列中获取文件以创建实际任务
                batches_for_this_key = []
                for _ in range(num_batches_to_process):
                    batch_info, batch_tokens = batches_to_process_queue.popleft()
                    current_task_batch = [processing_queue.popleft() for _ in range(len(batch_info))]
                    batches_for_this_key.append((current_task_batch, batch_tokens))

                semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)
                tasks = [asyncio.create_task(process_batch(b, t, limiter, pbar, semaphore)) for b, t in batches_for_this_key]
                results = await asyncio.gather(*tasks)

                requests_attempted = 0
                for i, res in enumerate(results):
                    requests_attempted += 1
                    if res["success"]:
                        total_files_processed_this_session += len(batches_for_this_key[i][0])
                    else:
                        for failed_item in reversed(res["failed_items"]): processing_queue.appendleft(failed_item)
                    if res["quota_exceeded"]:
                        logging.warning("因配额耗尽，提前中止使用当前密钥。")
                        break
                request_tracker["usage"][api_key] = requests_made_today + requests_attempted

            else:
                # --- SINGLE MODE LOGIC ---
                requests_attempted = 0
                while requests_left_today > 0 and processing_queue:
                    file_item = processing_queue.popleft()
                    requests_attempted += 1
                    requests_left_today -= 1
                    
                    res = await process_single_file(file_item, limiter, pbar)
                    
                    if res["success"]:
                        total_files_processed_this_session += 1
                    else:
                        processing_queue.appendleft(res["failed_item"])
                    
                    if res["quota_exceeded"]:
                        logging.warning("因配额耗尽，提前中止使用当前密钥。")
                        break
                request_tracker["usage"][api_key] = requests_made_today + requests_attempted

        save_request_tracker(request_tracker)
        logging.info(f"密钥 (前8位: {api_key[:8]}...) 处理完成。今日累计尝试请求: {request_tracker['usage'][api_key]}/{DAILY_REQUEST_LIMIT}。")


    api_processing_time = time.time() - api_processing_start_time

    # --- 4. 结束后保存未完成的文件 ---
    remaining_files = [item['path'] for item in processing_queue]
    if remaining_files:
        logging.warning(f"所有密钥均已尝试，仍有 {len(remaining_files)} 个文件未处理。")
        save_pending_files(remaining_files)
    else:
        logging.info("所有文件已成功处理，清空待处理文件日志。")
        clear_pending_files_log()
    
    # --- 5. 最终结果输出 ---
    total_run_time = time.time() - total_start_time
    average_rate = total_files_processed_this_session / api_processing_time if api_processing_time > 0 else 0
    
    print("\n-----------------------------------------------------------------")
    print("运行结束！")
    print(f"本次运行共成功处理了 {total_files_processed_this_session} 个文件。")
    if remaining_files: print(f"仍有 {len(remaining_files)} 个文件在队列中未处理。")
    
    print("\n--- 耗时分析 ---")
    print(f"准备阶段 (文本提取、Token计算) 耗时: {prep_time:.2f} 秒")
    print(f"API处理阶段 (网络请求、文件重命名) 耗时: {api_processing_time:.2f} 秒")
    print(f"总运行耗时: {total_run_time:.2f} 秒")
    print(f"平均处理速率: {average_rate:.2f} 文件/秒")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n程序被用户中断。建议重新运行以使用断点续传功能。")
        sys.exit(0)

