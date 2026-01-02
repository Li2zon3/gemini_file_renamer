# -*- coding: utf-8 -*-
"""
Gemini File Renamer - GUI 版本
使用 Gemini API 批量智能重命名文件并写入元数据
"""

import os
import sys
import json
import time
import asyncio
import threading
import queue
import platform
from pathlib import Path
from datetime import date
from collections import deque
from urllib.request import getproxies
from bs4 import BeautifulSoup
import customtkinter as ctk
from tkinter import filedialog, messagebox

# --- 依赖库导入 ---
try:
    import google.generativeai as genai
    import pymupdf
    from docx import Document
    from ebooklib import epub, ITEM_DOCUMENT
    from pathvalidate import sanitize_filename
except ImportError as e:
    class ErrorApp(ctk.CTk):
        def __init__(self, error_message):
            super().__init__()
            self.withdraw()
            messagebox.showerror("依赖库缺失", f"错误：缺少必要的库: {error_message}\n\n请在终端运行 'pip install google-generativeai pymupdf pathvalidate python-docx EbookLib beautifulsoup4 customtkinter' 进行安装。")
            self.after(100, self.destroy)
    app = ErrorApp(e)
    app.mainloop()
    sys.exit(1)


# =======================================================================================
# SECTION 0: 代理检测模块
# =======================================================================================

class ProxyDetector:
    """系统代理检测器"""
    
    @staticmethod
    def get_windows_proxy():
        """从 Windows 注册表获取系统代理设置"""
        if platform.system() != 'Windows':
            return None
        
        try:
            import winreg
            
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r'Software\Microsoft\Windows\CurrentVersion\Internet Settings',
                0,
                winreg.KEY_READ
            )
            
            try:
                proxy_enable, _ = winreg.QueryValueEx(key, 'ProxyEnable')
                if not proxy_enable:
                    return None
                
                proxy_server, _ = winreg.QueryValueEx(key, 'ProxyServer')
                
                if proxy_server:
                    if '=' in proxy_server:
                        for part in proxy_server.split(';'):
                            if part.startswith('http=') or part.startswith('https='):
                                addr = part.split('=', 1)[1]
                                if not addr.startswith('http'):
                                    addr = f'http://{addr}'
                                return addr
                    else:
                        if not proxy_server.startswith('http'):
                            proxy_server = f'http://{proxy_server}'
                        return proxy_server
                        
            finally:
                winreg.CloseKey(key)
                
        except (ImportError, OSError, FileNotFoundError):
            pass
        
        return None
    
    @staticmethod
    def get_macos_proxy():
        """从 macOS 系统偏好设置获取代理"""
        if platform.system() != 'Darwin':
            return None
        
        try:
            import subprocess
            
            for service in ['Wi-Fi', 'Ethernet', 'USB 10/100/1000 LAN']:
                try:
                    result = subprocess.run(
                        ['networksetup', '-getwebproxy', service],
                        capture_output=True,
                        text=True,
                        timeout=5
                    )
                    
                    if result.returncode == 0:
                        lines = result.stdout.strip().split('\n')
                        enabled = False
                        server = None
                        port = None
                        
                        for line in lines:
                            if 'Enabled: Yes' in line:
                                enabled = True
                            elif line.startswith('Server:'):
                                server = line.split(':', 1)[1].strip()
                            elif line.startswith('Port:'):
                                port = line.split(':', 1)[1].strip()
                        
                        if enabled and server and port:
                            return f'http://{server}:{port}'
                            
                except subprocess.TimeoutExpired:
                    continue
                    
        except Exception:
            pass
        
        return None
    
    @staticmethod
    def get_env_proxy():
        """从环境变量获取代理设置"""
        proxy_vars = [
            'HTTPS_PROXY', 'https_proxy',
            'HTTP_PROXY', 'http_proxy',
            'ALL_PROXY', 'all_proxy',
        ]
        
        for var in proxy_vars:
            proxy = os.environ.get(var)
            if proxy:
                return proxy
        
        return None
    
    @staticmethod
    def get_urllib_proxy():
        """使用 urllib 获取系统代理"""
        proxies = getproxies()
        
        if 'https' in proxies:
            return proxies['https']
        if 'http' in proxies:
            return proxies['http']
        
        return None
    
    @classmethod
    def detect(cls):
        """自动检测系统代理设置"""
        # 1. 环境变量
        proxy = cls.get_env_proxy()
        if proxy:
            return proxy
        
        # 2. 操作系统特定检测
        system = platform.system()
        
        if system == 'Windows':
            proxy = cls.get_windows_proxy()
            if proxy:
                return proxy
        
        elif system == 'Darwin':
            proxy = cls.get_macos_proxy()
            if proxy:
                return proxy
        
        # 3. urllib 通用方法
        proxy = cls.get_urllib_proxy()
        if proxy:
            return proxy
        
        return None
    
    @classmethod
    def apply(cls, proxy=None, auto_detect=True):
        """将代理设置应用到环境变量"""
        result = {'proxy': None, 'applied': False, 'message': ''}
        
        if proxy:
            proxy_to_use = proxy
            result['message'] = f"使用手动指定代理: {proxy}"
        elif auto_detect:
            proxy_to_use = cls.detect()
            if proxy_to_use:
                result['message'] = f"自动检测到代理: {proxy_to_use}"
            else:
                result['message'] = "未检测到系统代理"
                return result
        else:
            result['message'] = "代理功能已禁用"
            return result
        
        result['proxy'] = proxy_to_use
        
        # 设置环境变量
        os.environ['HTTP_PROXY'] = proxy_to_use
        os.environ['HTTPS_PROXY'] = proxy_to_use
        os.environ['http_proxy'] = proxy_to_use
        os.environ['https_proxy'] = proxy_to_use
        os.environ['GRPC_PROXY'] = proxy_to_use
        
        result['applied'] = True
        return result
    
    @classmethod
    def clear(cls):
        """清除代理环境变量"""
        proxy_vars = [
            'HTTP_PROXY', 'HTTPS_PROXY', 'GRPC_PROXY',
            'http_proxy', 'https_proxy', 'grpc_proxy',
            'ALL_PROXY', 'all_proxy'
        ]
        
        for var in proxy_vars:
            if var in os.environ:
                del os.environ[var]


# =======================================================================================
# SECTION 1: 后端核心逻辑
# =======================================================================================

class Backend:
    """封装所有后台文件处理和API交互逻辑"""

    def __init__(self, gui_queue):
        self.gui_queue = gui_queue
        self.model = None
        self.stop_event = None
        
        # --- 全局常量与路径定义 ---
        self.RPM_LIMIT = 10
        self.TPM_LIMIT = 250000
        self.DAILY_REQUEST_LIMIT = 250
        self.MAX_TOKENS_PER_BATCH = 28000
        self.CONCURRENCY_LIMIT = 10
        self.MAX_RETRIES = 3
        self.SUPPORTED_EXTENSIONS = ['.pdf', '.epub', '.azw3', '.docx']
        
        self.PENDING_FILES_LOG = Path("./pending_files.txt")
        self.TRACKER_FILE = Path("./request_tracker.json")
        
        self.API_PROMPT_INSTRUCTION_BATCH = """
        Analyze the following text, which contains MULTIPLE documents concatenated together.
        Each document starts with a "--- START OF FILE: [filename] ---" marker and ends with an "--- END OF FILE: [filename] ---" marker.
        For EACH document provided, extract its metadata and create a corresponding JSON object.
        Return a single JSON array (a list) containing all the extracted JSON objects.
        The order of objects in the final list MUST match the order of the documents in the input text.
        Do not add any commentary. Only return the JSON array.
        """
        self.API_PROMPT_INSTRUCTION_SINGLE = """
        Analyze the following text from a single document.
        Extract its metadata and create a corresponding JSON object.
        Return only the single JSON object. Do not add any commentary.
        """
        self.SINGLE_OBJECT_SCHEMA = {
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
        self.JSON_SCHEMA_BATCH = {"type": "array", "items": self.SINGLE_OBJECT_SCHEMA}
        self.JOURNAL_KEYWORDS = ["journal", "review", "proceedings", "transactions", "quarterly", "annals", "bulletin", "magazine", "advances", "letters", "studies"]

    def log_to_gui(self, message, level="INFO"):
        self.gui_queue.put(f"[{level}] {message}")

    def load_request_tracker(self, silent=False):
        today_str = date.today().isoformat()
        default_tracker = {"date": today_str, "usage": {}}
        if not self.TRACKER_FILE.exists():
            return default_tracker
        try:
            with open(self.TRACKER_FILE, 'r', encoding='utf-8') as f:
                tracker = json.load(f)
            if not silent and tracker.get("date") != today_str:
                self.log_to_gui("新的一天，重置所有API密钥的每日请求计数器。")
                return default_tracker
            if "usage" not in tracker:
                tracker["usage"] = {}
            return tracker
        except (json.JSONDecodeError, IOError) as e:
            self.log_to_gui(f"读取请求跟踪文件失败，将重新开始计数。错误: {e}", "WARNING")
            return default_tracker

    def save_request_tracker(self, tracker_data):
        try:
            with open(self.TRACKER_FILE, 'w', encoding='utf-8') as f:
                json.dump(tracker_data, f, indent=4, ensure_ascii=False)
            if threading.current_thread() is not threading.main_thread():
                 self.log_to_gui("API用量信息已更新。", "DEBUG")
        except IOError as e:
            self.log_to_gui(f"保存请求跟踪文件失败: {e}", "ERROR")

    def load_pending_files(self):
        if not self.PENDING_FILES_LOG.exists():
            return []
        try:
            with open(self.PENDING_FILES_LOG, 'r', encoding='utf-8') as f:
                return [Path(line.strip()) for line in f if line.strip() and Path(line.strip()).exists()]
        except IOError:
            return []

    def save_pending_files(self, file_paths):
        try:
            with open(self.PENDING_FILES_LOG, 'w', encoding='utf-8') as f:
                for path in file_paths:
                    f.write(f"{path}\n")
        except IOError as e:
            self.log_to_gui(f"无法写入待处理文件日志: {e}", "ERROR")

    def clear_pending_files_log(self):
        if self.PENDING_FILES_LOG.exists():
            try:
                self.PENDING_FILES_LOG.unlink()
                self.log_to_gui("所有任务完成，待处理文件日志已清空。")
            except OSError as e:
                self.log_to_gui(f"无法清空待处理文件日志: {e}", "ERROR")

    class RateLimiter:
        def __init__(self, rpm, tpm, logger):
            self.rpm, self.tpm, self.logger = rpm, tpm, logger
            self.request_timestamps, self.token_timestamps = deque(), deque()

        async def wait_for_slot(self, tokens_needed):
            while True:
                now = time.time()
                while self.request_timestamps and self.request_timestamps[0] < now - 60:
                    self.request_timestamps.popleft()
                current_requests = len(self.request_timestamps)
                if current_requests < self.rpm:
                    self.request_timestamps.append(now)
                    break
                wait_time = max(1.0, (self.request_timestamps[0] + 60) - now)
                self.logger(f"速率限制已达上限 (RPM)。等待 {wait_time:.2f} 秒...", "WARNING")
                await asyncio.sleep(wait_time)

    async def switch_and_configure_api(self, api_key):
        try:
            genai.configure(api_key=api_key)
            self.model = genai.GenerativeModel('gemini-2.5-flash')
            return True
        except Exception as e:
            self.log_to_gui(f"API 密钥 (前8位: {api_key[:8]}...) 配置失败。错误: {e}", "ERROR")
            return False

    def _extract_from_pdf(self, pdf_path):
        text_content = []
        try:
            with pymupdf.open(pdf_path) as doc:
                total_pages = doc.page_count
                start_page_nums = list(range(min(4, total_pages)))
                for i in start_page_nums:
                    text_content.append(doc[i].get_text())
                end_page_start_index = max(len(start_page_nums), total_pages - 3)
                if end_page_start_index < total_pages:
                    text_content.append("\n\n--- DOCUMENT END CONTENT ---\n\n")
                    for i in range(end_page_start_index, total_pages):
                        text_content.append(doc[i].get_text())
        except Exception as e:
            self.log_to_gui(f"提取PDF文本时出错: {pdf_path.name}, 错误: {e}", "ERROR")
            return ""
        return "".join(text_content)

    def _extract_from_epub(self, epub_path):
        text_content = []
        try:
            book = epub.read_epub(epub_path)
            doc_items = list(book.get_items_of_type(ITEM_DOCUMENT))
            total_chapters = len(doc_items)
            items_to_process = []
            if total_chapters <= 5 + 4:
                items_to_process = doc_items
            else:
                items_to_process.extend(doc_items[:5])
                items_to_process.append(None)
                items_to_process.extend(doc_items[-4:])
            for item in items_to_process:
                if item is None:
                    text_content.append("\n\n--- DOCUMENT END CONTENT ---\n\n")
                    continue
                soup = BeautifulSoup(item.get_body_content(), 'html.parser')
                text_content.append(soup.get_text("\n", strip=True))
        except Exception as e:
            self.log_to_gui(f"提取 EPUB/AZW3 文本时出错: {epub_path.name}, 错误: {e}", "ERROR")
            return ""
        return "\n\n".join(text_content)

    def _extract_from_docx(self, docx_path):
        text_content = []
        try:
            doc = Document(docx_path)
            all_paras = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
            total_paras = len(all_paras)
            if total_paras <= 20 + 15:
                text_content = all_paras
            else:
                text_content.extend(all_paras[:20])
                text_content.append("\n\n--- DOCUMENT END CONTENT ---\n\n")
                text_content.extend(all_paras[-15:])
        except Exception as e:
            self.log_to_gui(f"提取 DOCX 文本时出错: {docx_path.name}, 错误: {e}", "ERROR")
            return ""
        return "\n".join(text_content)

    def extract_text_from_file(self, file_path):
        extension = file_path.suffix.lower()
        text_to_extract = ""
        try:
            if extension == '.pdf':
                text_to_extract = self._extract_from_pdf(file_path)
            elif extension in ['.epub', '.azw3']:
                text_to_extract = self._extract_from_epub(file_path)
            elif extension == '.docx':
                text_to_extract = self._extract_from_docx(file_path)
            else:
                return None
            return text_to_extract[:int(self.MAX_TOKENS_PER_BATCH * 0.9)]
        except Exception as e:
            self.log_to_gui(f"提取文本时发生未知错误: {file_path.name}, {e}", "ERROR")
            return None

    def build_filename(self, info):
        if not info or not info.get('title'):
            return None
        template = "{title} - {authors} ({optional})"
        parts = []
        if info.get("translators") and info["translators"].lower() != 'null':
            parts.append(f"{info['translators']} 译")
        if info.get("editors") and info["editors"].lower() != 'null':
            if not any(k in info.get("publisher_or_journal", "").lower() for k in self.JOURNAL_KEYWORDS):
                parts.append(f"{info['editors']} 编")
        if info.get("publisher_or_journal") and info["publisher_or_journal"].lower() != 'null':
            parts.append(info["publisher_or_journal"])
        if info.get("journal_volume_issue") and info["journal_volume_issue"].lower() != 'null':
            parts.append(info["journal_volume_issue"])
        if info.get("publication_date") and info["publication_date"].lower() != 'null':
            parts.append(f"({info['publication_date']})")
        if info.get("start_page"):
            parts.append(f"p{info['start_page']}")
        optional_str = ", ".join(part for part in parts if part)
        fields = {"title": info.get("title", "无标题").strip(), "authors": "、".join(info.get("authors", [])).strip() or "作者不详", "optional": optional_str}
        filename = template.format(**fields)
        return filename.replace(" ()", "").strip() if not optional_str else filename.strip()

    def rename_file(self, original_path, new_base_name):
        if not new_base_name:
            self.log_to_gui(f"无法为 {original_path.name} 构建有效文件名，跳过。", "WARNING")
            return
        safe_name = sanitize_filename(new_base_name)
        new_path = original_path.with_name(f"{safe_name}{original_path.suffix}")
        counter = 1
        while new_path.exists() and new_path != original_path:
            new_path = original_path.with_name(f"{safe_name}_{counter}{original_path.suffix}")
            counter += 1
        if new_path != original_path:
            try:
                original_path.rename(new_path)
                self.log_to_gui(f"成功: '{original_path.name}' -> '{new_path.name}'", "SUCCESS")
            except OSError as e:
                self.log_to_gui(f"重命名文件时出错: {e}", "ERROR")

    async def _process_single_file(self, file_item, limiter):
        """处理单个文件"""
        if self.stop_event.is_set(): return False

        prompt_parts = [
            self.API_PROMPT_INSTRUCTION_SINGLE,
            f"\n\n--- START OF FILE: {file_item['path'].name} ---\n{file_item['text']}\n--- END OF FILE: {file_item['path'].name} ---"
        ]
        single_file_config = {"response_mime_type": "application/json", "response_schema": self.SINGLE_OBJECT_SCHEMA}

        for attempt in range(self.MAX_RETRIES):
            if self.stop_event.is_set(): return False
            try:
                await limiter.wait_for_slot(file_item['tokens'])
                response = await self.model.generate_content_async("".join(prompt_parts), generation_config=single_file_config)
                
                if not response.parts:
                    self.log_to_gui(f"文件 '{file_item['path'].name}' 因内容安全策略被过滤，已跳过。", "WARNING")
                    self.gui_queue.put(("progress_update", 1))
                    return False

                info = json.loads(response.text)
                self.rename_file(file_item['path'], self.build_filename(info))
                self.gui_queue.put(("progress_update", 1))
                return True
            except Exception as e:
                self.log_to_gui(f"处理单个文件 '{file_item['path'].name}' 时出错 (尝试 {attempt + 1}/{self.MAX_RETRIES}): {e}", "ERROR")
                if "quota" in str(e).lower() or "429" in str(e):
                    raise e
                if attempt < self.MAX_RETRIES - 1:
                    await asyncio.sleep(2 ** attempt)
        
        self.log_to_gui(f"文件 '{file_item['path'].name}' 处理失败，已跳过。", "ERROR")
        self.gui_queue.put(("progress_update", 1))
        return False
    
    async def process_batch(self, batch, limiter, semaphore):
        """处理一个批次"""
        if self.stop_event.is_set(): return False
        async with semaphore:
            if self.stop_event.is_set(): return False
            if not batch: return True
            
            batch_tokens = sum(item['tokens'] for item in batch)
            prompt_parts = [self.API_PROMPT_INSTRUCTION_BATCH]
            for item in batch:
                prompt_parts.append(f"\n\n--- START OF FILE: {item['path'].name} ---\n{item['text']}\n--- END OF FILE: {item['path'].name} ---")

            for attempt in range(self.MAX_RETRIES):
                if self.stop_event.is_set(): return False
                try:
                    await limiter.wait_for_slot(batch_tokens)
                    batch_config = {"response_mime_type": "application/json", "response_schema": self.JSON_SCHEMA_BATCH}
                    response = await self.model.generate_content_async("".join(prompt_parts), generation_config=batch_config)
                    results = json.loads(response.text)

                    if not isinstance(results, list) or len(results) != len(batch):
                        self.log_to_gui(f"批处理返回结果数量({len(results)}/{len(batch)})或格式错误。", "WARNING")
                        return False
                    
                    for i, info in enumerate(results):
                        self.rename_file(batch[i]['path'], self.build_filename(info))
                    
                    self.gui_queue.put(("progress_update", len(batch)))
                    return True
                except Exception as e:
                    self.log_to_gui(f"处理批次时出错 (尝试 {attempt + 1}/{self.MAX_RETRIES}): {e}", "ERROR")
                    if "quota" in str(e).lower() or "429" in str(e):
                        raise e 
                    if attempt < self.MAX_RETRIES - 1:
                        await asyncio.sleep(2 ** attempt)
            
            self.log_to_gui(f"批次处理失败，已达到最大重试次数。", "ERROR")
            return False

    async def run_processing(self, api_keys_str, target_dir_str, excluded_folder_paths, use_single_mode=False, stop_event=None, proxy_settings=None):
        self.stop_event = stop_event
        all_remaining_paths = []
        
        try:
            # ===== 应用代理设置 =====
            if proxy_settings:
                auto_proxy = proxy_settings.get('auto', True)
                manual_proxy = proxy_settings.get('manual', '').strip()
                
                if manual_proxy:
                    result = ProxyDetector.apply(proxy=manual_proxy, auto_detect=False)
                    self.log_to_gui(result['message'])
                elif auto_proxy:
                    result = ProxyDetector.apply(auto_detect=True)
                    self.log_to_gui(result['message'])
                else:
                    self.log_to_gui("代理功能已禁用，将直接连接")
            # =========================
            
            self.log_to_gui("开始处理...")
            if self.stop_event.is_set(): return
            if not api_keys_str or not target_dir_str:
                self.log_to_gui("错误: API密钥和目标目录为必填项。", "ERROR")
                return
            api_keys = [key.strip() for key in api_keys_str.split(',') if key.strip()]
            target_directory = Path(target_dir_str)
            if not target_directory.is_dir():
                self.log_to_gui(f"错误: 目录不存在: {target_dir_str}", "ERROR")
                return

            pending_paths = self.load_pending_files()
            if self.stop_event.is_set(): return

            if pending_paths:
                self.log_to_gui(f"检测到断点日志，将只处理上次未完成的 {len(pending_paths)} 个文件。", "INFO")
                files_to_process_paths = pending_paths
            else:
                self.log_to_gui("未检测到断点日志，将扫描整个目录进行新任务。")
                all_found_files = list(set([p for ext in self.SUPPORTED_EXTENSIONS for p in target_directory.glob(f"**/*{ext}")]))
                if excluded_folder_paths:
                    resolved_excluded_paths = {p.resolve() for p in excluded_folder_paths}
                    files_to_process_paths = [
                        p for p in all_found_files
                        if not any(p.resolve().is_relative_to(ex_p_res) for ex_p_res in resolved_excluded_paths)
                    ]
                else:
                    files_to_process_paths = all_found_files
            
            if not files_to_process_paths:
                self.log_to_gui("没有需要处理的文件。", "INFO")
                self.clear_pending_files_log()
                return

            all_remaining_paths = list(files_to_process_paths)

            if self.stop_event.is_set(): return
            self.log_to_gui(f"找到 {len(files_to_process_paths)} 个文件待处理。")
            self.gui_queue.put(("set_progress_max", len(files_to_process_paths)))
            
            self.log_to_gui("正在提取文件文本...")
            all_file_data_map = {
                path: {'path': path, 'text': self.extract_text_from_file(path) or "", 'tokens': 0}
                for path in files_to_process_paths
            }
            for path, data in all_file_data_map.items():
                data['tokens'] = len(data['text']) // 4
            
            limiter = self.RateLimiter(self.RPM_LIMIT, self.TPM_LIMIT, self.log_to_gui)

            for key_index, api_key in enumerate(api_keys):
                if self.stop_event.is_set() or not all_remaining_paths: break
                
                self.log_to_gui(f"\n--- 正在尝试使用 API 密钥 #{key_index + 1} ---")
                if not await self.switch_and_configure_api(api_key):
                    continue

                successfully_processed_paths = set()
                current_files_data = [all_file_data_map[path] for path in all_remaining_paths]

                try:
                    if use_single_mode:
                        self.log_to_gui("--- 用户选择单文件处理模式 ---", "INFO")
                        for item in current_files_data:
                            if self.stop_event.is_set(): break
                            if await self._process_single_file(item, limiter):
                                successfully_processed_paths.add(item['path'])
                    else:
                        self.log_to_gui("--- 启动批处理模式 ---", "INFO")
                        batches_queue = deque()
                        batch, tokens = [], 0
                        for item in sorted(current_files_data, key=lambda x: x['tokens']):
                            if item['tokens'] > self.MAX_TOKENS_PER_BATCH:
                                self.gui_queue.put(("progress_update", 1))
                                successfully_processed_paths.add(item['path'])
                                continue
                            if batch and tokens + item['tokens'] > self.MAX_TOKENS_PER_BATCH:
                                batches_queue.append(list(batch))
                                batch, tokens = [], 0
                            batch.append(item)
                            tokens += item['tokens']
                        if batch:
                            batches_queue.append(list(batch))

                        if not batches_queue:
                            self.log_to_gui("根据剩余文件未能创建任何处理批次。")
                            break
                        
                        self.log_to_gui(f"使用当前密钥处理 {len(batches_queue)} 个批次...")
                        semaphore = asyncio.Semaphore(self.CONCURRENCY_LIMIT)
                        tasks = [self.process_batch(b, limiter, semaphore) for b in batches_queue]
                        results = await asyncio.gather(*tasks)

                        for i, success in enumerate(results):
                            if success:
                                for item in batches_queue[i]:
                                    successfully_processed_paths.add(item['path'])
                except Exception as e:
                    if "quota" in str(e).lower() or "429" in str(e):
                        self.log_to_gui(f"API密钥配额已用尽或速率过快，将尝试下一个密钥。", "WARNING")
                    else:
                        raise e

                all_remaining_paths = [path for path in all_remaining_paths if path not in successfully_processed_paths]

            if self.stop_event.is_set():
                self.log_to_gui("任务被用户终止。")
            
            if all_remaining_paths:
                self.log_to_gui(f"处理完成，仍有 {len(all_remaining_paths)} 个文件未处理，已保存到断点日志。", "WARNING")
                self.save_pending_files(all_remaining_paths)
            else:
                self.log_to_gui("所有文件已成功处理！")
                self.clear_pending_files_log()

        except Exception as e:
            self.log_to_gui(f"发生严重错误: {e}", "CRITICAL")
            import traceback
            self.log_to_gui(traceback.format_exc(), "DEBUG")
        finally:
            if self.stop_event and self.stop_event.is_set():
                 self.log_to_gui("处理线程已安全退出。")
            elif all_remaining_paths:
                 self.save_pending_files(all_remaining_paths)
            self.gui_queue.put(("processing_finished", None))


# =======================================================================================
# SECTION 2: GUI 用户界面
# =======================================================================================
CONFIG_FILE = "config.json"

class ExclusionDialog(ctk.CTkToplevel):
    """选择要排除的子文件夹的对话框"""
    def __init__(self, parent, target_directory):
        super().__init__(parent)
        self.parent_app = parent
        self.title("选择要排除的文件夹")
        self.geometry("450x350")
        self.transient(parent)
        self.grab_set()

        self.checkbox_vars = {}
        self.initial_excluded_str = {str(p.resolve()) for p in self.parent_app.excluded_folders}

        label = ctk.CTkLabel(self, text="请勾选您想要排除的子文件夹:")
        label.pack(padx=20, pady=(20, 10))

        scrollable_frame = ctk.CTkScrollableFrame(self)
        scrollable_frame.pack(expand=True, fill="both", padx=20, pady=10)

        try:
            target_dir_path = Path(target_directory).resolve()
            subfolders = [p for p in target_dir_path.iterdir() if p.is_dir()]
            if not subfolders:
                ctk.CTkLabel(scrollable_frame, text="未找到子文件夹。").pack(pady=10)
            else:
                previously_excluded_str = self.initial_excluded_str
                
                for folder in sorted(subfolders):
                    folder_path_str = str(folder.resolve())
                    initial_value = folder_path_str if folder_path_str in previously_excluded_str else ""
                    var = ctk.StringVar(value=initial_value)
                    cb = ctk.CTkCheckBox(scrollable_frame,
                                         text=folder.name,
                                         variable=var,
                                         onvalue=folder_path_str,
                                         offvalue="")
                    cb.pack(anchor="w", padx=10, pady=5)
                    self.checkbox_vars[folder_path_str] = var

        except Exception as e:
            ctk.CTkLabel(scrollable_frame, text=f"读取目录时出错:\n{e}", text_color="red").pack()

        self.status_label = ctk.CTkLabel(self, text="", text_color="green")
        self.status_label.pack(padx=20, pady=(0, 5))

        button_frame = ctk.CTkFrame(self, fg_color="transparent")
        button_frame.pack(padx=20, pady=(5, 20), fill="x")
        button_frame.grid_columnconfigure(0, weight=1)
        
        self.close_button = ctk.CTkButton(button_frame, text="关闭", command=self.close_dialog)
        self.close_button.grid(row=0, column=2, padx=(10, 0))

        self.save_button = ctk.CTkButton(button_frame, text="保存更改", command=self.save_changes)
        self.save_button.grid(row=0, column=1)

    def get_current_selection_set(self):
        return {Path(var.get()).resolve() for var in self.checkbox_vars.values() if var.get()}

    def save_changes(self):
        current_selection_paths = list(self.get_current_selection_set())
        self.parent_app.update_exclusions_from_dialog(current_selection_paths)
        
        self.initial_excluded_str = {str(p) for p in current_selection_paths}
        
        self.status_label.configure(text="更改已保存！")
        self.after(3000, lambda: self.status_label.configure(text=""))

    def close_dialog(self):
        current_selection_str = {str(p) for p in self.get_current_selection_set()}
        
        if current_selection_str != self.initial_excluded_str:
            if messagebox.askyesno("未保存的更改", "您有未保存的更改。确定要关闭吗？", parent=self):
                self.destroy()
        else:
            self.destroy()


class UsageDialog(ctk.CTkToplevel):
    """查看和编辑API用量信息的对话框"""
    def __init__(self, parent, backend):
        super().__init__(parent)
        self.backend = backend
        self.tracker_data = self.backend.load_request_tracker(silent=True)

        self.title("查看/编辑API用量")
        self.geometry("500x400")
        self.transient(parent)
        self.grab_set()

        self.date_entry = None
        self.usage_entries = {}

        current_keys_in_main_window = [k.strip() for k in parent.api_keys_entry.get().split(',') if k.strip()]
        for key in current_keys_in_main_window:
            if key not in self.tracker_data['usage']:
                self.tracker_data['usage'][key] = 0

        main_frame = ctk.CTkFrame(self)
        main_frame.pack(expand=True, fill="both", padx=10, pady=10)
        
        date_frame = ctk.CTkFrame(main_frame)
        date_frame.pack(fill="x", padx=10, pady=5)
        ctk.CTkLabel(date_frame, text="记录日期 (YYYY-MM-DD):").pack(side="left", padx=5)
        self.date_entry = ctk.CTkEntry(date_frame)
        self.date_entry.insert(0, self.tracker_data.get("date", ""))
        self.date_entry.pack(side="left", expand=True, fill="x", padx=5)

        ctk.CTkLabel(main_frame, text="各API密钥已用请求数:").pack(anchor="w", padx=10, pady=(10,0))
        scroll_frame = ctk.CTkScrollableFrame(main_frame)
        scroll_frame.pack(expand=True, fill="both", padx=10, pady=5)

        usage = self.tracker_data.get("usage", {})
        if not usage:
            ctk.CTkLabel(scroll_frame, text="暂无用量记录。").pack(pady=10)
        else:
            for key, count in usage.items():
                key_frame = ctk.CTkFrame(scroll_frame)
                key_frame.pack(fill="x", pady=2)
                masked_key = f"{key[:8]}...{key[-4:]}" if len(key) > 12 else key
                ctk.CTkLabel(key_frame, text=masked_key, width=200, anchor="w").pack(side="left", padx=5)
                entry = ctk.CTkEntry(key_frame)
                entry.insert(0, str(count))
                entry.pack(side="left", expand=True, fill="x", padx=5)
                self.usage_entries[key] = entry
        
        button_frame = ctk.CTkFrame(self, fg_color="transparent")
        button_frame.pack(fill="x", padx=10, pady=10)
        ctk.CTkButton(button_frame, text="保存", command=self.save_changes).pack(side="right", padx=5)
        ctk.CTkButton(button_frame, text="取消", command=self.destroy).pack(side="right", padx=5)

    def save_changes(self):
        new_data = {"date": self.date_entry.get(), "usage": {}}
        try:
            time.strptime(new_data["date"], '%Y-%m-%d')
            for key, entry in self.usage_entries.items():
                new_data["usage"][key] = int(entry.get())
            
            self.backend.save_request_tracker(new_data)
            self.backend.log_to_gui("API用量信息已由用户手动更新。", "INFO")
            messagebox.showinfo("成功", "API用量信息已更新。", parent=self)
            self.destroy()
        except ValueError:
            messagebox.showerror("输入错误", "日期格式应为 YYYY-MM-DD，且用量必须为整数。", parent=self)
        except Exception as e:
            messagebox.showerror("保存失败", f"发生未知错误: {e}", parent=self)


class App(ctk.CTk):
    """主应用程序类"""
    def __init__(self, backend_logic):
        super().__init__()
        self.backend = backend_logic
        self.excluded_folders = []
        self.progress_max = 0
        self.progress_current = 0
        self.single_mode_var = ctk.BooleanVar(value=False)
        self.stop_event = threading.Event()
        
        # ===== 代理相关变量 =====
        self.auto_proxy_var = ctk.BooleanVar(value=True)
        self.manual_proxy_var = ctk.StringVar(value="")
        # =========================

        self.title("Gemini 智能文件重命名工具")
        self.geometry("900x750")
        ctk.set_appearance_mode("System")
        ctk.set_default_color_theme("blue")

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self.create_widgets()
        self.load_config()
        self.check_queue_periodically()

    def create_widgets(self):
        settings_frame = ctk.CTkFrame(self)
        settings_frame.grid(row=0, column=0, padx=10, pady=10, sticky="ew")
        settings_frame.grid_columnconfigure(1, weight=1)

        # API Keys
        ctk.CTkLabel(settings_frame, text="Google API Keys (逗号分隔):").grid(row=0, column=0, padx=10, pady=5, sticky="w")
        self.api_keys_entry = ctk.CTkEntry(settings_frame, placeholder_text="key1,key2,...")
        self.api_keys_entry.grid(row=0, column=1, columnspan=2, padx=10, pady=5, sticky="ew")
        
        # 目标文件夹
        ctk.CTkLabel(settings_frame, text="目标文件夹:").grid(row=1, column=0, padx=10, pady=5, sticky="w")
        self.dir_entry = ctk.CTkEntry(settings_frame, placeholder_text="尚未选择文件夹")
        self.dir_entry.grid(row=1, column=1, padx=10, pady=5, sticky="ew")
        self.browse_button = ctk.CTkButton(settings_frame, text="浏览...", command=self.browse_directory, width=100)
        self.browse_button.grid(row=1, column=2, padx=(5,10), pady=5)
        
        # 功能操作
        ctk.CTkLabel(settings_frame, text="功能操作:").grid(row=2, column=0, padx=10, pady=5, sticky="w")
        button_group_frame = ctk.CTkFrame(settings_frame, fg_color="transparent")
        button_group_frame.grid(row=2, column=1, columnspan=2, pady=5, sticky="w")
        
        self.exclusion_button = ctk.CTkButton(button_group_frame, text="选择排除文件夹...", command=self.open_exclusion_dialog)
        self.exclusion_button.pack(side="left", padx=(0,5))
        
        self.usage_button = ctk.CTkButton(button_group_frame, text="查看/编辑API用量", command=self.open_usage_dialog)
        self.usage_button.pack(side="left", padx=5)

        self.single_mode_checkbox = ctk.CTkCheckBox(button_group_frame, text="以单文件模式处理 (较慢)", variable=self.single_mode_var)
        self.single_mode_checkbox.pack(side="left", padx=15)

        self.exclusion_status_label = ctk.CTkLabel(settings_frame, text="当前未排除任何文件夹。", text_color="gray")
        self.exclusion_status_label.grid(row=3, column=1, columnspan=2, padx=10, pady=(0, 5), sticky="w")

        # ===== 代理设置 UI =====
        ctk.CTkLabel(settings_frame, text="代理设置:").grid(row=4, column=0, padx=10, pady=5, sticky="w")
        
        proxy_frame = ctk.CTkFrame(settings_frame, fg_color="transparent")
        proxy_frame.grid(row=4, column=1, columnspan=2, pady=5, sticky="w")
        
        self.auto_proxy_checkbox = ctk.CTkCheckBox(
            proxy_frame, 
            text="自动检测系统代理", 
            variable=self.auto_proxy_var,
            command=self.on_proxy_mode_change
        )
        self.auto_proxy_checkbox.pack(side="left", padx=(0, 15))
        
        ctk.CTkLabel(proxy_frame, text="手动代理:").pack(side="left", padx=(0, 5))
        self.proxy_entry = ctk.CTkEntry(proxy_frame, placeholder_text="如 http://127.0.0.1:7890", width=200, textvariable=self.manual_proxy_var)
        self.proxy_entry.pack(side="left", padx=(0, 10))
        
        self.detect_proxy_button = ctk.CTkButton(
            proxy_frame, 
            text="检测代理", 
            command=self.detect_and_show_proxy,
            width=80
        )
        self.detect_proxy_button.pack(side="left")
        
        # 代理状态标签
        self.proxy_status_label = ctk.CTkLabel(settings_frame, text="代理状态: 未检测", text_color="gray")
        self.proxy_status_label.grid(row=5, column=1, columnspan=2, padx=10, pady=(0, 5), sticky="w")
        # =========================
        
        # 日志区域
        log_frame = ctk.CTkFrame(self)
        log_frame.grid(row=1, column=0, padx=10, pady=(0, 10), sticky="nsew")
        log_frame.grid_rowconfigure(0, weight=1)
        log_frame.grid_columnconfigure(0, weight=1)
        self.log_textbox = ctk.CTkTextbox(log_frame, state="disabled", wrap="word")
        self.log_textbox.grid(row=0, column=0, padx=10, pady=10, sticky="nsew")

        # 控制区域
        control_frame = ctk.CTkFrame(self)
        control_frame.grid(row=2, column=0, padx=10, pady=(0,10), sticky="ew")
        control_frame.grid_columnconfigure(0, weight=1)
        control_frame.grid_columnconfigure(1, weight=1)
        
        self.progressbar = ctk.CTkProgressBar(control_frame)
        self.progressbar.grid(row=0, column=0, columnspan=2, padx=10, pady=5, sticky="ew")
        self.progressbar.set(0)
        
        self.start_button = ctk.CTkButton(control_frame, text="开始重命名", command=self.start_processing_thread, height=35)
        self.start_button.grid(row=1, column=0, padx=10, pady=10, sticky="ew")

        self.stop_button = ctk.CTkButton(control_frame, text="终止", command=self.stop_processing, height=35, fg_color="red", hover_color="darkred", state="disabled")
        self.stop_button.grid(row=1, column=1, padx=10, pady=10, sticky="ew")

    # ===== 代理相关方法 =====
    def on_proxy_mode_change(self):
        """当代理模式切换时调用"""
        if self.auto_proxy_var.get():
            self.proxy_entry.configure(state="disabled")
            self.detect_and_show_proxy()
        else:
            self.proxy_entry.configure(state="normal")
            self.proxy_status_label.configure(text="代理状态: 使用手动配置", text_color="orange")

    def detect_and_show_proxy(self):
        """检测并显示代理状态"""
        detected = ProxyDetector.detect()
        if detected:
            self.proxy_status_label.configure(
                text=f"代理状态: 已检测到 {detected}", 
                text_color="green"
            )
            if self.auto_proxy_var.get():
                self.manual_proxy_var.set(detected)
            self.log(f"检测到系统代理: {detected}")
        else:
            self.proxy_status_label.configure(
                text="代理状态: 未检测到系统代理", 
                text_color="gray"
            )
            self.log("未检测到系统代理，将直接连接")

    def get_effective_proxy(self):
        """获取当前生效的代理地址"""
        if self.auto_proxy_var.get():
            return ProxyDetector.detect() or ""
        else:
            return self.manual_proxy_var.get().strip()

    def get_proxy_settings(self):
        """获取代理设置字典"""
        return {
            'auto': self.auto_proxy_var.get(),
            'manual': self.manual_proxy_var.get().strip()
        }
    # =========================

    def open_usage_dialog(self):
        UsageDialog(self, self.backend)

    def browse_directory(self):
        current_dir = self.dir_entry.get()
        dir_path = filedialog.askdirectory(title="请选择包含文件的文件夹")

        if dir_path and dir_path != current_dir:
            self.dir_entry.delete(0, "end")
            self.dir_entry.insert(0, dir_path)
            self.excluded_folders = []
            self.update_exclusion_status_label()
            self.log(f"已选择新文件夹: {dir_path}")
            self.log("排除列表已因此重置。")

    def open_exclusion_dialog(self):
        target_dir = self.dir_entry.get()
        if not target_dir or not Path(target_dir).is_dir():
            messagebox.showerror("错误", "请先选择一个有效的目标文件夹。")
            return
        ExclusionDialog(self, target_dir)

    def update_exclusions_from_dialog(self, excluded_list):
        self.excluded_folders = excluded_list
        self.update_exclusion_status_label()
        self.save_config()
        self.log("排除列表已更新。")
            
    def update_exclusion_status_label(self):
        if not self.excluded_folders:
            self.exclusion_status_label.configure(text="当前未排除任何文件夹。", text_color="gray")
        else:
            count = len(self.excluded_folders)
            folder_names = ", ".join(f.name for f in self.excluded_folders)
            self.exclusion_status_label.configure(text=f"已排除 {count} 个文件夹: {folder_names}", text_color=("black", "white"))

    def load_config(self):
        try:
            if Path(CONFIG_FILE).exists():
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                
                api_keys_from_config = config.get("api_keys", "")
                if api_keys_from_config:
                    cleaned_api_keys = api_keys_from_config.strip()
                    self.api_keys_entry.insert(0, cleaned_api_keys)
                
                target_dir = config.get("target_directory", "")
                if target_dir:
                    self.dir_entry.insert(0, target_dir)
                
                excluded_paths_str = config.get("excluded_folders", [])
                if excluded_paths_str:
                    self.excluded_folders = [Path(p) for p in excluded_paths_str]
                    self.update_exclusion_status_label()

                # ===== 加载代理配置 =====
                self.auto_proxy_var.set(config.get("auto_proxy", True))
                manual_proxy = config.get("manual_proxy", "")
                if manual_proxy:
                    self.manual_proxy_var.set(manual_proxy)
                
                # 根据模式更新UI状态
                if self.auto_proxy_var.get():
                    self.proxy_entry.configure(state="disabled")
                    self.after(500, self.detect_and_show_proxy)
                else:
                    self.proxy_entry.configure(state="normal")
                    self.proxy_status_label.configure(text="代理状态: 使用手动配置", text_color="orange")
                # =========================

                self.log("已从 config.json 加载保存的配置。")
        except Exception as e:
            self.log(f"无法加载配置文件: {e}", "ERROR")

    def save_config(self):
        try:
            config_data = {
                "api_keys": self.api_keys_entry.get(),
                "target_directory": self.dir_entry.get(),
                "excluded_folders": [str(p.resolve()) for p in self.excluded_folders],
                # ===== 保存代理配置 =====
                "auto_proxy": self.auto_proxy_var.get(),
                "manual_proxy": self.manual_proxy_var.get().strip(),
                # =========================
            }
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(config_data, f, indent=4)
        except Exception as e:
            self.log(f"无法保存配置文件: {e}", "ERROR")

    def log(self, message, level="INFO"):
        self.log_textbox.configure(state="normal")
        self.log_textbox.insert("end", f"{message}\n")
        self.log_textbox.see("end")
        self.log_textbox.configure(state="disabled")

    def start_processing_thread(self):
        self.save_config()
        self.stop_event.clear()
        
        # ===== 获取代理设置 =====
        proxy_settings = self.get_proxy_settings()
        proxy = self.get_effective_proxy()
        if proxy:
            self.log(f"将使用代理: {proxy}")
        else:
            self.log("未配置代理，将直接连接 Google API")
        # =========================
        
        self.start_button.configure(state="disabled", text="正在处理中...")
        self.stop_button.configure(state="normal")
        self.progressbar.set(0)
        self.progress_max = 0
        self.progress_current = 0
        self.log_textbox.configure(state="normal")
        self.log_textbox.delete("1.0", "end")
        self.log_textbox.configure(state="disabled")
        
        threading.Thread(
            target=lambda: asyncio.run(self.backend.run_processing(
                self.api_keys_entry.get(),
                self.dir_entry.get(),
                self.excluded_folders,
                self.single_mode_var.get(),
                self.stop_event,
                proxy_settings  # 传递代理设置
            )),
            daemon=True
        ).start()
    
    def stop_processing(self):
        self.log("用户请求终止...将在当前操作完成后停止。", "WARNING")
        self.stop_event.set()
        self.stop_button.configure(state="disabled", text="正在终止...")

    def check_queue_periodically(self):
        try:
            while True:
                message = self.backend.gui_queue.get_nowait()
                if isinstance(message, tuple):
                    command, value = message
                    if command == "set_progress_max":
                        self.progress_max = value if value > 0 else 1
                        self.progress_current = 0
                        self.progressbar.set(0)
                    elif command == "progress_update":
                        self.progress_current += value
                        if self.progress_max > 0:
                            self.progressbar.set(self.progress_current / self.progress_max)
                    elif command == "processing_finished":
                        self.start_button.configure(state="normal", text="开始重命名")
                        self.stop_button.configure(state="disabled", text="终止")
                        if self.progressbar.get() < 1.0:
                             self.progressbar.set(1.0)
                else:
                    self.log(message)
        except queue.Empty:
            pass
        finally:
            self.after(100, self.check_queue_periodically)
            
    def on_closing(self):
        self.save_config()
        self.destroy()


if __name__ == "__main__":
    def handle_exception(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        
        import traceback
        error_details = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
        print("Unhandled exception caught:\n" + error_details)
        
        try:
            messagebox.showerror("未捕获的异常", f"发生了一个严重错误:\n\n{exc_type.__name__}: {exc_value}\n\n详细信息已打印到控制台。")
        except Exception:
            pass

    sys.excepthook = handle_exception
    
    backend = Backend(gui_queue=queue.Queue())
    app = App(backend)
    app.protocol("WM_DELETE_WINDOW", app.on_closing)
    app.mainloop()
