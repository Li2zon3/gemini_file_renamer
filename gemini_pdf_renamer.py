import os
import sys
import json
import time
import asyncio
import logging
import argparse
from pathlib import Path
from collections import deque
import google.generativeai as genai
import pymupdf # PyMuPDF
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
        # 使用 getpass 模块更安全，但为了简单起见，这里用 input
        api_key = input("请输入您的 Google API 密钥 (输入后按回车): ").strip()
        print("-----------------------------------------------------------------")
        if not api_key:
            print("错误：未提供 API 密钥，程序即将退出。")
            sys.exit(1)
    try:
        genai.configure(api_key=api_key)
        # 尝试获取模型以验证密钥是否有效
        _ = genai.get_model('gemini-2.5-flash')
        logging.info("Google API 密钥配置成功。")
    except Exception as e:
        logging.error(f"API 密钥配置失败，请检查您的密钥是否正确。错误: {e}")
        sys.exit(1)

# --- 2. 全局常量与模型定义 ---
# 根据官方免费额度设定
RPM_LIMIT = 10  # 每分钟请求数
TPM_LIMIT = 250000  # 每分钟令牌数
MAX_TOKENS_PER_REQUEST = 30000 # 单次请求的安全Token上限
MAX_RETRIES = 3 # API请求失败后的最大重试次数

MODEL = genai.GenerativeModel('gemini-2.5-flash')
JSON_SCHEMA = {
    "type": "array", "items": { "type": "object", "properties": { "original_filename": {"type": "string"}, "title": {"type": "string"}, "authors": {"type": "array", "items": {"type": "string"}}, "editors": {"type": "array", "items": {"type": "string"}}, "translators": {"type": "array", "items": {"type": "string"}}, "publisher_or_journal": {"type": "string"}, "publication_date": {"type": "string"}, }, "required": ["original_filename", "title", "authors", "editors", "translators", "publisher_or_journal", "publication_date"] }
}
GENERATION_CONFIG = {"response_mime_type": "application/json", "response_schema": JSON_SCHEMA}

# --- 3. 速率控制器 ---
class RateLimiter:
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
            if current_requests >= self.rpm and self.request_timestamps:
                wait_time = max(wait_time, (self.request_timestamps[0] + 60) - now)
            
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
                    wait_time = max(wait_time, (wait_until_ts + 60) - now)

            logging.warning(f"速率限制已达上限。等待 {wait_time:.2f} 秒...")
            await asyncio.sleep(max(0, wait_time))

# --- 4. 核心异步功能函数 ---
async def process_batch_concurrently(batch, limiter, pbar):
    """异步处理一个批次的请求，并遵守速率限制和重试机制。"""
    if not batch: return

    total_tokens = sum(item['tokens'] for item in batch)
    prompt_parts = [f"\n\n--- DOCUMENT: {item['path'].name} ---\n{item['text']}" for item in batch]
    full_prompt = "".join(prompt_parts)

    for attempt in range(MAX_RETRIES):
        try:
            await limiter.wait_for_slot(total_tokens)
            logging.info(f"--- 提交一个包含 {len(batch)} 个文件，共 {total_tokens} tokens 的批次 (尝试 {attempt + 1}/{MAX_RETRIES}) ---")

            response = await MODEL.generate_content_async(full_prompt, generation_config=GENERATION_CONFIG)
            results = json.loads(response.text)
            
            results_map = {res.get('original_filename'): res for res in results}

            for item in batch:
                res_info = results_map.get(item['path'].name)
                if res_info:
                    new_name = build_filename(res_info)
                    rename_file(item['path'], new_name)
                else:
                    logging.error(f"API返回结果中未找到文件: {item['path'].name}")
            pbar.update(len(batch))
            return
        except json.JSONDecodeError as e:
            logging.error(f"处理批次时JSON解析失败: {e}. API返回内容: {getattr(response, 'text', 'N/A')[:200]}...")
            pbar.update(len(batch))
            break
        except Exception as e:
            logging.error(f"处理批次时发生错误 (尝试 {attempt + 1}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES - 1:
                wait_time = 2 ** (attempt + 1)
                logging.info(f"将在 {wait_time} 秒后重试...")
                await asyncio.sleep(wait_time)
            else:
                logging.error(f"已达到最大重试次数，放弃此批次。涉及文件: {[item['path'].name for item in batch]}")
                pbar.update(len(batch))

# --- 5. 辅助函数 ---
def extract_text_from_first_pages(pdf_path, num_pages=3):
    try:
        with pymupdf.open(pdf_path) as doc:
            return "".join(page.get_text() for i, page in enumerate(doc) if i < num_pages)
    except Exception as e:
        logging.error(f"提取文本时出错: {pdf_path.name}, 错误: {e}")
        return None

def build_filename(info):
    if not info or not info.get('title'): return None
    title = info.get('title', '无标题').strip()
    authors = "、".join(info.get('authors',)).strip() or "作者不详"
    optional_parts = []
    if info.get('editors'): optional_parts.append("编者：" + "、".join(info.get('editors')))
    if info.get('translators'): optional_parts.append("译者：" + "、".join(info.get('translators')))
    if info.get('publisher_or_journal'): optional_parts.append(info.get('publisher_or_journal'))
    if info.get('publication_date'): optional_parts.append(info.get('publication_date'))
    filename = f"{title}-{authors}"
    filtered_parts = [part for part in optional_parts if part]
    if filtered_parts: filename += f"（{'-'.join(filtered_parts)}）"
    return filename

def rename_file(original_path, new_base_name):
    if not new_base_name:
        logging.warning(f"无法为 {original_path.name} 构建有效文件名，跳过。")
        return
    safe_base_name = sanitize_filename(new_base_name)
    new_path = original_path.with_name(f"{safe_base_name}.pdf")
    counter = 1
    while new_path.exists() and new_path != original_path:
        new_path = original_path.with_name(f"{safe_base_name}_{counter}.pdf")
        counter += 1
    if new_path == original_path:
        logging.info(f"文件名 '{original_path.name}' 已符合格式，无需重命名。")
    else:
        try:
            original_path.rename(new_path)
            logging.info(f"成功: '{original_path.name}' -> '{new_path.name}'")
        except OSError as e:
            logging.error(f"重命名文件时出错: {original_path.name} -> {new_path.name}, 错误: {e}")

def get_args():
    """解析命令行参数，使其更具可复用性"""
    parser = argparse.ArgumentParser(description="使用Gemini API批量智能重命名PDF文件。")
    parser.add_argument(
        "directory",
        nargs='?',
        default="./pdfs_to_rename",
        help="包含PDF文件的目录路径 (默认为: ./pdfs_to_rename)"
    )
    return parser.parse_args()

# --- 6. 主执行逻辑 ---
async def main():
    configure_api_key()
    start_time = time.time()
    args = get_args()
    target_directory = Path(args.directory)
    if not target_directory.is_dir():
        logging.info(f"目标目录不存在，已创建: {target_directory}。请将PDF文件放入后重新运行。")
        target_directory.mkdir(exist_ok=True)
        return

    pdf_files = list(target_directory.glob("*.pdf"))
    if not pdf_files:
        logging.info(f"在目录 {target_directory} 中未找到PDF文件。")
        return

    print(f"找到 {len(pdf_files)} 个PDF文件。开始提取元数据...")
    
    limiter = RateLimiter(RPM_LIMIT, TPM_LIMIT)
    
    all_file_data = []
    for pdf_path in pdf_files:
        text = extract_text_from_first_pages(pdf_path, num_pages=3)
        if not text: continue
        try:
            tokens = MODEL.count_tokens(text).total_tokens
            all_file_data.append({'path': pdf_path, 'text': text, 'tokens': tokens})
        except Exception as e:
            logging.error(f"计算Token时出错 ({pdf_path.name}): {e}")

    print("元数据提取完毕，开始提交API进行重命名...")
    tasks = []
    with tqdm(total=len(all_file_data), desc="重命名进度") as pbar:
        batch, current_tokens = [], 0
        for item in all_file_data:
            if item['tokens'] > MAX_TOKENS_PER_REQUEST:
                if batch:
                    tasks.append(process_batch_concurrently(batch, limiter, pbar))
                    batch, current_tokens = [], 0
                tasks.append(process_batch_concurrently([item], limiter, pbar))
                continue
            
            if current_tokens + item['tokens'] > MAX_TOKENS_PER_REQUEST:
                tasks.append(process_batch_concurrently(batch, limiter, pbar))
                batch, current_tokens = [], 0
            
            batch.append(item)
            current_tokens += item['tokens']
        if batch:
            tasks.append(process_batch_concurrently(batch, limiter, pbar))
        
        await asyncio.gather(*tasks)
        
    print(f"\n所有文件处理完毕。总耗时: {time.time() - start_time:.2f} 秒。")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n程序被用户中断。")
        sys.exit(0)
