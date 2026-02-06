# -*- coding: utf-8 -*-
"""
Gemini File Renamer - GUI 版本（带代理支持）
使用 Gemini API 批量智能重命名文件并写入元数据

修复版 - 修复了以下问题：
- asyncio.gather 增加 return_exceptions=True，防止单个失败导致索引错乱
- RateLimiter 补充 TPM 限制（原版只有 RPM）
- 过滤空文本文件，不再发送给 API
- process_batch 增加 response.parts 空值检查
- 配置文件保存/加载处理模式（批处理/单文件/自动）
- 修复自动代理覆盖手动输入的问题
- 增加元数据写入功能（与 CLI 版一致）
- 改进 build_filename 的 null/none 等值过滤
- 批处理失败后自动降级为单文件重试
- 修正 finally 块中重复保存 pending files 的问题
"""

import os
import sys
import json
import time
import asyncio
import threading
import queue
import platform
import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from datetime import date, datetime, timezone
from collections import deque
from urllib.request import getproxies
from typing import Any, Dict, Optional

# =============================================================================
# Dependency checks (fail fast with a friendly message)
# =============================================================================

_KEY_ID_LEN = 16  # Short stable id for tracker usage keys (sha256 hex prefix).


def _make_key_id(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:_KEY_ID_LEN]


def _looks_like_key_id(value: object) -> bool:
    if not isinstance(value, str) or len(value) != _KEY_ID_LEN:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _to_int(value: object) -> int:
    try:
        return int(value)
    except Exception:
        return 0


_BUDGET_TRACKER_VERSION = 1
_IN_NANOS_PER_TOKEN = 500
_OUT_NANOS_PER_TOKEN = 3000


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class BudgetManager:
    """
    Per-key monthly budget tracker (nanos USD) stored in budget_tracker.json.
    Never stores raw API keys on disk (key_id only).
    """

    def __init__(
        self,
        path: Path,
        *,
        in_nanos_per_token: int = _IN_NANOS_PER_TOKEN,
        out_nanos_per_token: int = _OUT_NANOS_PER_TOKEN,
    ):
        self._path = path
        self._in_nanos_per_token = int(in_nanos_per_token)
        self._out_nanos_per_token = int(out_nanos_per_token)
        self._lock = asyncio.Lock()
        self._data = self._load()

    @staticmethod
    def month_key(today: Optional[date] = None) -> str:
        d = today or date.today()
        return d.strftime("%Y-%m")

    def _load(self) -> dict:
        default = {"version": _BUDGET_TRACKER_VERSION, "months": {}}
        if not self._path.exists():
            return default
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return default
            if data.get("version") != _BUDGET_TRACKER_VERSION:
                data["version"] = _BUDGET_TRACKER_VERSION
            if not isinstance(data.get("months"), dict):
                data["months"] = {}
            return data
        except Exception:
            return default

    def _atomic_save(self) -> None:
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)
        tmp.replace(self._path)

    def _get_entry(self, month: str, key_id: str) -> dict:
        months = self._data.setdefault("months", {})
        if not isinstance(months, dict):
            months = {}
            self._data["months"] = months
        month_bucket = months.setdefault(month, {})
        if not isinstance(month_bucket, dict):
            month_bucket = {}
            months[month] = month_bucket
        entry = month_bucket.setdefault(key_id, {})
        if not isinstance(entry, dict):
            entry = {}
            month_bucket[key_id] = entry

        entry.setdefault("spent_nanos_usd", 0)
        entry.setdefault("input_tokens", 0)
        entry.setdefault("output_tokens", 0)
        entry.setdefault("requests", 0)
        entry.setdefault("updated_at", "")
        return entry

    def read_snapshot(self) -> dict:
        # Best-effort read without locking (for UI display).
        return self._load()

    async def try_reserve(
        self,
        *,
        key_id: str,
        budget_nanos_usd: int,
        estimated_input_tokens: int,
        max_output_tokens: int,
        month: Optional[str] = None,
        input_safety_factor: float = 1.10,
    ) -> Optional[Dict[str, Any]]:
        if budget_nanos_usd <= 0:
            return None

        m = month or self.month_key()
        safe_in = int(max(0, estimated_input_tokens) * input_safety_factor + 0.999)
        safe_out = int(max(0, max_output_tokens))
        reserved = safe_in * self._in_nanos_per_token + safe_out * self._out_nanos_per_token

        async with self._lock:
            entry = self._get_entry(m, key_id)
            spent = _to_int(entry.get("spent_nanos_usd", 0))
            if spent + reserved > budget_nanos_usd:
                return None

            entry["spent_nanos_usd"] = spent + reserved
            entry["requests"] = _to_int(entry.get("requests", 0)) + 1
            entry["updated_at"] = _utc_now_iso()
            self._atomic_save()

        return {
            "key_id": key_id,
            "month": m,
            "reserved_nanos_usd": reserved,
        }

    async def commit(
        self,
        *,
        reservation: Dict[str, Any],
        actual_input_tokens: int,
        actual_output_tokens: int,
    ) -> None:
        key_id = str(reservation.get("key_id", ""))
        month = str(reservation.get("month", self.month_key()))
        reserved = _to_int(reservation.get("reserved_nanos_usd", 0))

        in_tk = int(max(0, actual_input_tokens))
        out_tk = int(max(0, actual_output_tokens))
        actual = in_tk * self._in_nanos_per_token + out_tk * self._out_nanos_per_token

        async with self._lock:
            entry = self._get_entry(month, key_id)
            spent = _to_int(entry.get("spent_nanos_usd", 0))

            entry["spent_nanos_usd"] = max(0, spent - reserved + actual)
            entry["input_tokens"] = _to_int(entry.get("input_tokens", 0)) + in_tk
            entry["output_tokens"] = _to_int(entry.get("output_tokens", 0)) + out_tk
            entry["updated_at"] = _utc_now_iso()
            self._atomic_save()

    async def rollback(self, *, reservation: Dict[str, Any]) -> None:
        """
        Undo a reservation when the corresponding API call did not complete.
        This prevents budget leakage under retries/errors.
        """
        key_id = str(reservation.get("key_id", ""))
        month = str(reservation.get("month", self.month_key()))
        reserved = _to_int(reservation.get("reserved_nanos_usd", 0))

        async with self._lock:
            entry = self._get_entry(month, key_id)
            spent = _to_int(entry.get("spent_nanos_usd", 0))
            entry["spent_nanos_usd"] = max(0, spent - reserved)

            req = _to_int(entry.get("requests", 0))
            entry["requests"] = max(0, req - 1)

            entry["updated_at"] = _utc_now_iso()
            self._atomic_save()


def _path_is_relative_to(child: Path, parent: Path) -> bool:
    """Python 3.8 compatible alternative to Path.is_relative_to()."""
    try:
        child.relative_to(parent)
        return True
    except Exception:
        return False


def _show_dependency_error(error_message: str, extra_hint: str = "") -> None:
    details = f"{error_message}"
    if extra_hint:
        details = f"{details}\n\n{extra_hint}"
    try:
        import tkinter as tk  # noqa: F401
        from tkinter import messagebox as _messagebox

        root = tk.Tk()
        root.withdraw()
        _messagebox.showerror("依赖/环境错误", details)
        root.destroy()
    except Exception:
        print("依赖/环境错误:", details, file=sys.stderr)


try:
    import tkinter as tk  # noqa: F401
    from tkinter import filedialog, messagebox
except Exception as e:
    _show_dependency_error(
        f"Tkinter 不可用：{e}",
        "你的 Python 解释器可能缺少 Tk 支持（例如缺 _tkinter），GUI 版本无法运行。\n\n"
        "建议：\n"
        "1) 使用系统自带的 /usr/bin/python3 创建 venv\n"
        "2) 或安装 python.org 的 Python（包含 Tk），然后重建 venv 并安装依赖",
    )
    sys.exit(1)

try:
    import customtkinter as ctk
except ImportError as e:
    _show_dependency_error(
        f"缺少依赖：{e}",
        "请在终端运行：pip install customtkinter\n或：pip install -r requirements.txt",
    )
    sys.exit(1)

try:
    from bs4 import BeautifulSoup
    import google.generativeai as genai
    import pymupdf
    from docx import Document
    from ebooklib import epub, ITEM_DOCUMENT
    from pathvalidate import sanitize_filename
except ImportError as e:
    _show_dependency_error(
        f"缺少依赖：{e}",
        "请在终端运行：pip install -r requirements.txt",
    )
    sys.exit(1)


# =======================================================================================
# SECTION 0: 代理检测模块
# =======================================================================================

class ProxyDetector:
    """系统代理检测器"""

    @staticmethod
    def get_windows_proxy():
        if platform.system() != 'Windows':
            return None
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r'Software\Microsoft\Windows\CurrentVersion\Internet Settings',
                0, winreg.KEY_READ
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
        if platform.system() != 'Darwin':
            return None
        try:
            import subprocess
            for service in ['Wi-Fi', 'Ethernet', 'USB 10/100/1000 LAN']:
                try:
                    result = subprocess.run(
                        ['networksetup', '-getwebproxy', service],
                        capture_output=True, text=True, timeout=5
                    )
                    if result.returncode == 0:
                        lines = result.stdout.strip().split('\n')
                        enabled, server, port = False, None, None
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
        for var in ['HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy',
                     'ALL_PROXY', 'all_proxy']:
            proxy = os.environ.get(var)
            if proxy:
                return proxy
        return None

    @staticmethod
    def get_urllib_proxy():
        proxies = getproxies()
        return proxies.get('https') or proxies.get('http')

    @classmethod
    def detect(cls):
        proxy = cls.get_env_proxy()
        if proxy:
            return proxy
        system = platform.system()
        if system == 'Windows':
            proxy = cls.get_windows_proxy()
            if proxy:
                return proxy
        elif system == 'Darwin':
            proxy = cls.get_macos_proxy()
            if proxy:
                return proxy
        return cls.get_urllib_proxy()

    @classmethod
    def apply(cls, proxy=None, auto_detect=True):
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
        for var_name in ['HTTP_PROXY', 'HTTPS_PROXY', 'GRPC_PROXY',
                         'http_proxy', 'https_proxy', 'grpc_proxy']:
            os.environ[var_name] = proxy_to_use
        result['applied'] = True
        return result

    @classmethod
    def clear(cls):
        for var in ['HTTP_PROXY', 'HTTPS_PROXY', 'GRPC_PROXY',
                     'http_proxy', 'https_proxy', 'grpc_proxy',
                     'ALL_PROXY', 'all_proxy']:
            os.environ.pop(var, None)


# =======================================================================================
# SECTION 1: 后端核心逻辑
# =======================================================================================

# --- 文本规范化工具（与 CLI 版一致）---
JOURNAL_KEYWORDS = frozenset([
    "journal", "review", "proceedings", "transactions", "quarterly",
    "annals", "bulletin", "magazine", "advances", "letters", "studies",
    "science", "research", "technology", "medicine", "report", "archives",
    "学报", "法学", "研究", "评论", "科学", "技术", "杂志", "动态",
    "报告", "医学", "经济", "哲学", "历史", "通讯", "汇刊", "纪要"
])
ROLE_INVALID_TOKENS = frozenset(["null", "none", "n/a", "unknown", "不详", "未知"])
ROLE_INVALID_SUBSTRINGS = frozenset(["无法提取", "不明确", "系统返回null", "系统返回 null"])


def _normalize(value):
    """规范化单个值，过滤 null/none 等无效值"""
    if value is None:
        return ""
    text = str(value).strip()
    if not text or text.lower() in ("null", "none", "n/a"):
        return ""
    return text


# --- 占位标题检测（避免误命名为 "Metadata Extraction Task" 等泛化标题） ---
_PLACEHOLDER_TITLE_EXACT = "metadata extraction task"
_PLACEHOLDER_TITLE_MAX_LEN = 80


def is_placeholder_title(title):
    if not isinstance(title, str):
        return False
    value = title.strip()
    if not value:
        return False
    lowered = value.casefold()
    if lowered == _PLACEHOLDER_TITLE_EXACT:
        return True
    if (
        len(lowered) <= _PLACEHOLDER_TITLE_MAX_LEN
        and "metadata extraction" in lowered
        and "task" in lowered
    ):
        return True
    return False


def _normalize_role(value):
    """规范化角色字段（译者、编者等）"""
    normalized = _normalize(value)
    if not normalized:
        return ""
    lower = normalized.lower()
    if lower in ROLE_INVALID_TOKENS:
        return ""
    compact = normalized.replace(" ", "")
    for marker in ROLE_INVALID_SUBSTRINGS:
        if marker in normalized or marker in compact:
            return ""
    return normalized


def _normalize_authors(values):
    """规范化作者列表"""
    if not values:
        return []
    result = []
    for v in values:
        n = _normalize(v)
        if n and n not in ("作者不详",):
            result.append(n)
    return result


# --- 元数据写入器 ---

class MetadataWriters:
    """元数据写入工具集"""

    @staticmethod
    def _build_details(info):
        """构建详细信息字符串"""
        details = []
        mappings = [
            ("出版/期刊", _normalize(info.get("publisher_or_journal"))),
            ("卷期", _normalize(info.get("journal_volume_issue"))),
            ("日期", _normalize(info.get("publication_date"))),
            ("编者", _normalize_role(info.get("editors"))),
            ("译者", _normalize_role(info.get("translators"))),
            ("页码", _normalize(info.get("start_page"))),
        ]
        for label, value in mappings:
            if value:
                details.append(f"{label}: {value}")
        return " | ".join(details)

    @staticmethod
    def _get_keywords_str(info):
        kws = info.get("keywords", [])
        if not kws:
            return ""
        return ", ".join(_normalize(k) for k in kws if _normalize(k))

    @staticmethod
    def write_pdf(path, info, logger_fn):
        try:
            with pymupdf.open(path) as doc:
                metadata = doc.metadata
                metadata['title'] = _normalize(info.get('title'))
                metadata['author'] = "、".join(_normalize_authors(info.get('authors')))
                metadata['subject'] = MetadataWriters._build_details(info)
                metadata['keywords'] = MetadataWriters._get_keywords_str(info)
                doc.set_metadata(metadata)
                doc.save(doc.name, incremental=True, encryption=pymupdf.PDF_ENCRYPT_KEEP)
            logger_fn(f"PDF 元数据写入成功: {path.name}")
        except Exception as e:
            logger_fn(f"PDF 元数据写入失败 {path.name}: {e}", "WARNING")

    @staticmethod
    def write_docx(path, info, logger_fn):
        try:
            doc = Document(path)
            cp = doc.core_properties
            cp.title = _normalize(info.get('title'))
            cp.author = "、".join(_normalize_authors(info.get('authors')))
            cp.subject = MetadataWriters._build_details(info)
            cp.keywords = MetadataWriters._get_keywords_str(info)
            cp.comments = "Metadata updated by Gemini File Renamer"
            doc.save(path)
            logger_fn(f"DOCX 元数据写入成功: {path.name}")
        except Exception as e:
            logger_fn(f"DOCX 元数据写入失败 {path.name}: {e}", "WARNING")

    @staticmethod
    def write_epub(path, info, logger_fn):
        try:
            book = epub.read_epub(path)
            title = _normalize(info.get('title'))
            if title:
                book.set_title(title)
            # 清除旧作者
            namespace = "http://purl.org/dc/elements/1.1/"
            try:
                meta = book.metadata.get(namespace)
                if isinstance(meta, dict):
                    meta.pop("creator", None)
                elif isinstance(meta, list):
                    book.metadata[namespace] = [
                        item for item in meta
                        if not (isinstance(item, (tuple, list)) and len(item) > 0 and item[0] == "creator")
                    ]
            except Exception:
                pass

            for author in _normalize_authors(info.get('authors')):
                book.add_author(author)

            details = MetadataWriters._build_details(info)
            kws = MetadataWriters._get_keywords_str(info)
            desc_parts = []
            if details:
                desc_parts.append(details)
            if kws:
                desc_parts.append(f"Keywords: {kws}")
            if desc_parts:
                book.add_metadata('DC', 'description', "\n".join(desc_parts))
            epub.write_epub(path, book)
            logger_fn(f"EPUB 元数据写入成功: {path.name}")
        except Exception as e:
            logger_fn(f"EPUB 元数据写入失败 {path.name}: {e}", "WARNING")

    @classmethod
    def write(cls, path, info, logger_fn):
        """根据文件类型写入元数据"""
        ext = path.suffix.lower()
        if ext == '.pdf':
            cls.write_pdf(path, info, logger_fn)
        elif ext == '.docx':
            cls.write_docx(path, info, logger_fn)
        elif ext in ('.epub', '.azw3'):
            cls.write_epub(path, info, logger_fn)


class Backend:
    """封装所有后台文件处理和API交互逻辑"""

    def __init__(self, gui_queue):
        self.gui_queue = gui_queue
        self.model = None
        self.stop_event = None

        # --- 全局常量 ---
        self.RPM_LIMIT = 10
        self.TPM_LIMIT = 250000
        self.DAILY_REQUEST_LIMIT = 250
        self.MAX_TOKENS_PER_BATCH = 28000
        self.CONCURRENCY_LIMIT = 5  # [FIX] 降低默认并发，减少 429
        self.MAX_RETRIES = 3
        self.CHARS_PER_TOKEN = 3.5
        self.SUPPORTED_EXTENSIONS = ['.pdf', '.epub', '.azw3', '.docx']

        self.PENDING_FILES_LOG = Path("./pending_files.txt")
        self.TRACKER_FILE = Path("./request_tracker.json")
        self.BUDGET_FILE = Path("./budget_tracker.json")

        # Tier defaults
        self.FREE_MODEL_NAME = "gemini-2.5-flash"
        self.PAID_MODEL_NAME = "gemini-3-flash-preview"
        self.PAID_MAX_CONTEXT_TOKENS = 200_000
        self.PAID_MAX_OUTPUT_TOKENS = 8_192
        self.PAID_MAX_REQUEST_TOKENS = 100_000
        self.PAID_MAX_ITEMS_PER_BATCH = 40
        self.PAID_CONCURRENCY_DEFAULT = 20
        # Paid-tier limits (best-effort throttling; adjust if you see frequent 429s).
        self.PAID_RPM_LIMIT = 1_000
        self.PAID_TPM_LIMIT = 1_000_000
        self.PAID_DAILY_REQUEST_LIMIT = 10_000
        self.BUDGET_SAFETY_MARGIN_TOKENS = 8_000
        self.PROMPT_OVERHEAD_TOKENS = 2_000

        self.API_PROMPT_INSTRUCTION_BATCH = (
            "Analyze the following text, which contains MULTIPLE documents concatenated together.\n"
            "Each document starts with a \"--- START OF FILE: [filename] ---\" marker and ends with "
            "an \"--- END OF FILE: [filename] ---\" marker.\n"
            "For EACH document provided, extract its metadata and create a corresponding JSON object.\n"
            "Also extract a list of 3-5 relevant keywords from each document's content.\n"
            "Do not use generic placeholder titles like \"Metadata Extraction Task\". If uncertain, return an empty title string.\n"
            "Return a single JSON array (a list) containing all the extracted JSON objects.\n"
            "The order of objects in the final list MUST match the order of the documents in the input text.\n"
            "Do not add any commentary. Only return the JSON array."
        )
        self.API_PROMPT_INSTRUCTION_SINGLE = (
            "Analyze the following text from a single document.\n"
            "Extract its metadata and create a corresponding JSON object.\n"
            "Also extract a list of 3-5 relevant keywords.\n"
            "Do not use generic placeholder titles like \"Metadata Extraction Task\". If uncertain, return an empty title string.\n"
            "Return only the single JSON object. Do not add any commentary."
        )
        self.SINGLE_OBJECT_SCHEMA = {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "authors": {"type": "array", "items": {"type": "string"}},
                "keywords": {"type": "array", "items": {"type": "string"}},
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
            if not isinstance(tracker, dict):
                return default_tracker
            if not silent and tracker.get("date") != today_str:
                self.log_to_gui("新的一天，重置所有API密钥的每日请求计数器。")
                return default_tracker
            usage = tracker.get("usage")
            if not isinstance(usage, dict):
                usage = {}
            # Migration: legacy tracker stored raw API keys as dict keys. Migrate to key_id.
            migrated = {}
            for k, v in usage.items():
                if k is None:
                    continue
                k_str = str(k)
                key_id = k_str if _looks_like_key_id(k_str) else _make_key_id(k_str)
                migrated[key_id] = _to_int(migrated.get(key_id, 0)) + _to_int(v)
            tracker["usage"] = migrated
            return tracker
        except (json.JSONDecodeError, IOError) as e:
            self.log_to_gui(f"读取请求跟踪文件失败，将重新开始计数。错误: {e}", "WARNING")
            return default_tracker

    def save_request_tracker(self, tracker_data):
        try:
            # Privacy: never persist raw API keys as usage dict keys.
            usage = tracker_data.get("usage", {})
            if not isinstance(usage, dict):
                usage = {}
            sanitized = dict(tracker_data)
            sanitized["usage"] = {
                str(k): _to_int(v) for k, v in usage.items() if _looks_like_key_id(str(k))
            }
            with open(self.TRACKER_FILE, 'w', encoding='utf-8') as f:
                json.dump(sanitized, f, indent=4, ensure_ascii=False)
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

    # ===================================================================
    # [FIX] 重写 RateLimiter，同时追踪 RPM 和 TPM
    # ===================================================================
    class RateLimiter:
        def __init__(self, rpm, tpm, logger):
            self.rpm = rpm
            self.tpm = tpm
            self.logger = logger
            self.request_timestamps = deque()
            self.token_records = deque()  # (timestamp, token_count)
            self.token_total = 0
            self._lock = asyncio.Lock()

        async def wait_for_slot(self, tokens_needed):
            async with self._lock:
                while True:
                    now = time.time()
                    cutoff = now - 60

                    # 清理旧记录
                    while self.request_timestamps and self.request_timestamps[0] < cutoff:
                        self.request_timestamps.popleft()
                    while self.token_records and self.token_records[0][0] < cutoff:
                        _, old_tokens = self.token_records.popleft()
                        self.token_total -= old_tokens

                    can_request = len(self.request_timestamps) < self.rpm
                    can_tokens = (self.token_total + tokens_needed) <= self.tpm

                    if can_request and can_tokens:
                        self.request_timestamps.append(now)
                        self.token_records.append((now, tokens_needed))
                        self.token_total += tokens_needed
                        break

                    # 计算等待时间
                    rpm_wait = 0.0
                    tpm_wait = 0.0

                    if not can_request and self.request_timestamps:
                        rpm_wait = (self.request_timestamps[0] + 60) - now

                    if not can_tokens and self.token_records:
                        tokens_to_free = (self.token_total + tokens_needed) - self.tpm
                        freed = 0
                        for ts, tk in self.token_records:
                            freed += tk
                            if freed >= tokens_to_free:
                                tpm_wait = (ts + 60) - now
                                break

                    wait_time = max(0.1, rpm_wait, tpm_wait)
                    self.logger(
                        f"速率限制。等待 {wait_time:.2f} 秒... "
                        f"(RPM: {len(self.request_timestamps)}/{self.rpm}, "
                        f"TPM: {self.token_total}/{self.tpm})",
                        "WARNING",
                    )
                    await asyncio.sleep(wait_time)

    async def switch_and_configure_api(self, api_key, model_name=None):
        try:
            genai.configure(api_key=api_key)
            model_to_use = model_name or self.FREE_MODEL_NAME
            self.model = genai.GenerativeModel(model_to_use)
            self.log_to_gui(
                f"API 密钥 (id: {_make_key_id(api_key)}) 配置成功。模型: {model_to_use}",
                "INFO",
            )
            return True
        except Exception as e:
            self.log_to_gui(
                f"API 密钥 (id: {_make_key_id(api_key)}) 配置失败。错误: {e}",
                "ERROR",
            )
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

    def extract_text_from_file(self, file_path, max_tokens=None):
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
            max_tokens_to_use = self.MAX_TOKENS_PER_BATCH if max_tokens is None else int(max_tokens)
            max_chars = int(max_tokens_to_use * self.CHARS_PER_TOKEN * 0.9)
            return text_to_extract[:max_chars] if text_to_extract else None
        except Exception as e:
            self.log_to_gui(f"提取文本时发生未知错误: {file_path.name}, {e}", "ERROR")
            return None

    def build_filename(self, info):
        """
        [FIX] 改进的文件名构建，使用 _normalize 过滤无效值。
        """
        if not info or not _normalize(info.get('title')):
            return None

        title = _normalize(info.get('title', '')).strip()
        if is_placeholder_title(title):
            return None
        authors = _normalize_authors(info.get('authors'))
        authors_str = "、".join(authors).strip() or "作者不详"
        translators = _normalize_role(info.get('translators'))
        editors = _normalize_role(info.get('editors'))
        publisher = _normalize(info.get('publisher_or_journal'))

        template = "{title} - {authors} ({optional})"
        parts = []

        if translators:
            parts.append(f"{translators} 译")
        if editors:
            pub_lower = publisher.lower() if publisher else ""
            if not any(k in pub_lower for k in JOURNAL_KEYWORDS):
                parts.append(f"{editors} 编")
        if publisher:
            parts.append(publisher)

        jvi = _normalize(info.get('journal_volume_issue'))
        if jvi:
            parts.append(jvi)

        pub_date = _normalize(info.get('publication_date'))
        if pub_date:
            parts.append(f"({pub_date})")

        start_page = info.get('start_page')
        if start_page and str(start_page).strip() and str(start_page).lower() not in ('null', 'none', '0'):
            parts.append(f"p{start_page}")

        optional_str = ", ".join(part for part in parts if part)
        fields = {"title": title, "authors": authors_str, "optional": optional_str}
        filename = template.format(**fields)
        return filename.replace(" ()", "").strip() if not optional_str else filename.strip()

    def rename_file(self, original_path, new_base_name, info=None, write_metadata=True):
        """
        [FIX] 增加元数据写入支持。
        """
        if not new_base_name:
            self.log_to_gui(f"无法为 {original_path.name} 构建有效文件名，跳过。", "WARNING")
            return original_path

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
                return original_path
        else:
            new_path = original_path

        # [FIX] 写入元数据
        if write_metadata and info:
            try:
                MetadataWriters.write(new_path, info, self.log_to_gui)
            except Exception as e:
                self.log_to_gui(f"写入元数据时出错: {e}", "WARNING")

        return new_path

    async def _process_single_file(
        self,
        file_item,
        limiter,
        tracker_data,
        key_id,
        usage_lock,
        write_metadata=True,
        paid_ctx=None,
    ):
        """处理单个文件（支持付费预算预扣与 200k context 上限）"""
        if self.stop_event.is_set():
            return {"success": False}

        prompt_parts = [
            self.API_PROMPT_INSTRUCTION_SINGLE,
            f"\n\n--- START OF FILE: {file_item['path'].name} ---\n"
            f"{file_item['text']}\n"
            f"--- END OF FILE: {file_item['path'].name} ---",
        ]
        prompt = "".join(prompt_parts)
        prompt_tokens_est = max(1, int(len(prompt) / self.CHARS_PER_TOKEN))

        max_output_tokens = None
        if paid_ctx is not None:
            max_output_tokens = int(paid_ctx.get("max_output_tokens", self.PAID_MAX_OUTPUT_TOKENS))
            max_context_tokens = int(paid_ctx.get("max_context_tokens", self.PAID_MAX_CONTEXT_TOKENS))
            margin = int(paid_ctx.get("safety_margin_tokens", self.BUDGET_SAFETY_MARGIN_TOKENS))
            if prompt_tokens_est + max_output_tokens + margin > max_context_tokens:
                self.log_to_gui(
                    f"文件 '{file_item['path'].name}' 输入过大，可能超过 {max_context_tokens} tokens 上限，跳过。",
                    "WARNING",
                )
                # 不更新 progress：该文件会在后续阶段继续尝试
                return {"success": False}

        single_file_config = {
            "response_mime_type": "application/json",
            "response_schema": self.SINGLE_OBJECT_SCHEMA,
        }
        if max_output_tokens is not None:
            single_file_config["max_output_tokens"] = int(max_output_tokens)

        usage_incremented = False

        def _extract_usage_tokens(resp) -> tuple:
            um = getattr(resp, "usage_metadata", None)
            if um is None:
                return None, None
            try:
                prompt_tk = int(getattr(um, "prompt_token_count", 0) or 0)
            except Exception:
                prompt_tk = 0
            try:
                cand_tk = int(getattr(um, "candidates_token_count", 0) or 0)
            except Exception:
                cand_tk = 0
            return prompt_tk or None, cand_tk or None

        for attempt in range(self.MAX_RETRIES):
            if self.stop_event.is_set():
                return {"success": False}
            reservation = None
            committed = False
            try:
                budget_mgr = None
                if paid_ctx is not None:
                    budget_mgr = paid_ctx.get("budget_manager")
                    budget_nanos = int(paid_ctx.get("budget_nanos_usd", 0))
                    if budget_mgr is None:
                        return {"success": False}

                    reservation = await budget_mgr.try_reserve(
                        key_id=key_id,
                        budget_nanos_usd=budget_nanos,
                        estimated_input_tokens=prompt_tokens_est,
                        max_output_tokens=int(max_output_tokens or 0),
                    )
                    if reservation is None:
                        self.log_to_gui(
                            "付费预算不足（本 key 本月已达到上限），将降级到免费模型继续处理。",
                            "WARNING",
                        )
                        return {"success": False, "budget_exceeded": True}

                await limiter.wait_for_slot(prompt_tokens_est)
                if self.stop_event.is_set():
                    if budget_mgr is not None and reservation is not None and not committed:
                        try:
                            await budget_mgr.rollback(reservation=reservation)
                        except Exception:
                            pass
                    return {"success": False}

                # Daily request tracking: only count once per file, and only if we will actually call the API.
                if not usage_incremented:
                    async with usage_lock:
                        usage = tracker_data.get("usage", {})
                        if not isinstance(usage, dict):
                            usage = {}
                            tracker_data["usage"] = usage
                        usage[key_id] = _to_int(usage.get(key_id, 0)) + 1
                    usage_incremented = True

                response = await self.model.generate_content_async(
                    prompt, generation_config=single_file_config
                )

                if paid_ctx is not None and reservation is not None:
                    prompt_tk, cand_tk = _extract_usage_tokens(response)
                    in_tk = int(prompt_tk or prompt_tokens_est)
                    if cand_tk is None and getattr(response, "text", None):
                        cand_tk = max(1, int(len(response.text) / self.CHARS_PER_TOKEN))
                    out_tk = int(cand_tk or 0)
                    await paid_ctx["budget_manager"].commit(
                        reservation=reservation,
                        actual_input_tokens=in_tk,
                        actual_output_tokens=out_tk,
                    )
                    committed = True

                if not response.parts:
                    self.log_to_gui(
                        f"文件 '{file_item['path'].name}' 因内容安全策略被过滤，已跳过。",
                        "WARNING",
                    )
                    self.gui_queue.put(("progress_update", 1))
                    return {"success": False}

                info = json.loads(response.text)
                new_base_name = self.build_filename(info) if isinstance(info, dict) else None
                if not new_base_name:
                    title = _normalize(info.get("title")) if isinstance(info, dict) else ""
                    if is_placeholder_title(title):
                        self.log_to_gui(
                            f"检测到占位标题，拒绝重命名并进入 pending: {file_item['path'].name}",
                            "WARNING",
                        )
                    else:
                        self.log_to_gui(
                            f"无法构建有效文件名，已跳过: {file_item['path'].name}",
                            "WARNING",
                        )
                    self.gui_queue.put(("progress_update", 1))
                    return {"success": False}

                self.rename_file(
                    file_item["path"],
                    new_base_name,
                    info=info,
                    write_metadata=write_metadata,
                )
                self.gui_queue.put(("progress_update", 1))
                return {"success": True}
            except Exception as e:
                if paid_ctx is not None and reservation is not None and not committed:
                    try:
                        budget_mgr = paid_ctx.get("budget_manager")
                        if budget_mgr is not None:
                            await budget_mgr.rollback(reservation=reservation)
                    except Exception:
                        pass
                self.log_to_gui(
                    f"处理单个文件 '{file_item['path'].name}' 时出错 "
                    f"(尝试 {attempt + 1}/{self.MAX_RETRIES}): {e}",
                    "ERROR",
                )
                if "quota" in str(e).lower() or "429" in str(e):
                    raise e
                if attempt < self.MAX_RETRIES - 1:
                    await asyncio.sleep(2 ** attempt)

        self.log_to_gui(f"文件 '{file_item['path'].name}' 处理失败，已跳过。", "ERROR")
        self.gui_queue.put(("progress_update", 1))
        return {"success": False}

    async def process_batch(
        self,
        batch,
        limiter,
        semaphore,
        tracker_data,
        key_id,
        usage_lock,
        write_metadata=True,
        paid_ctx=None,
        max_output_tokens=None,
    ):
        """处理一个批次"""
        if self.stop_event.is_set():
            return {'success': False, 'failed_items': batch}

        async with semaphore:
            if self.stop_event.is_set():
                return {'success': False, 'failed_items': batch}
            if not batch:
                return {'success': True, 'failed_items': []}

            usage_incremented = False

            prompt_parts = [self.API_PROMPT_INSTRUCTION_BATCH]
            for item in batch:
                prompt_parts.append(
                    f"\n\n--- START OF FILE: {item['path'].name} ---\n"
                    f"{item['text']}\n"
                    f"--- END OF FILE: {item['path'].name} ---"
                )
            prompt = "".join(prompt_parts)
            prompt_tokens_est = max(1, int(len(prompt) / self.CHARS_PER_TOKEN))

            for attempt in range(self.MAX_RETRIES):
                if self.stop_event.is_set():
                    return {'success': False, 'failed_items': batch}
                reservation = None
                committed = False
                try:
                    budget_mgr = None
                    if paid_ctx is not None:
                        max_ctx = int(paid_ctx.get("max_context_tokens", self.PAID_MAX_CONTEXT_TOKENS))
                        margin = int(paid_ctx.get("safety_margin_tokens", self.BUDGET_SAFETY_MARGIN_TOKENS))
                        max_out = int(max_output_tokens or paid_ctx.get("max_output_tokens", self.PAID_MAX_OUTPUT_TOKENS))
                        if prompt_tokens_est + max_out + margin > max_ctx:
                            self.log_to_gui(
                                f"批次输入过大，可能超过 {max_ctx} tokens 上限，将留到后续阶段处理。",
                                "WARNING",
                            )
                            return {'success': False, 'failed_items': batch}

                        budget_mgr = paid_ctx.get("budget_manager")
                        budget_nanos = int(paid_ctx.get("budget_nanos_usd", 0))
                        if budget_mgr is None:
                            return {'success': False, 'failed_items': batch}

                        reservation = await budget_mgr.try_reserve(
                            key_id=key_id,
                            budget_nanos_usd=budget_nanos,
                            estimated_input_tokens=prompt_tokens_est,
                            max_output_tokens=max_out,
                        )
                        if reservation is None:
                            self.log_to_gui(
                                "付费预算不足（本 key 本月已达到上限），将降级到免费模型继续处理。",
                                "WARNING",
                            )
                            return {
                                'success': False,
                                'failed_items': batch,
                                'budget_exceeded': True,
                            }

                    await limiter.wait_for_slot(prompt_tokens_est)
                    if self.stop_event.is_set():
                        if budget_mgr is not None and reservation is not None and not committed:
                            try:
                                await budget_mgr.rollback(reservation=reservation)
                            except Exception:
                                pass
                        return {'success': False, 'failed_items': batch}

                    # Daily request tracking: count once per batch, only if we will actually call the API.
                    if not usage_incremented:
                        async with usage_lock:
                            usage = tracker_data.get("usage", {})
                            if not isinstance(usage, dict):
                                usage = {}
                                tracker_data["usage"] = usage
                            usage[key_id] = _to_int(usage.get(key_id, 0)) + 1
                        usage_incremented = True

                    batch_config = {
                        "response_mime_type": "application/json",
                        "response_schema": self.JSON_SCHEMA_BATCH,
                    }
                    if max_output_tokens is not None:
                        batch_config["max_output_tokens"] = int(max_output_tokens)
                    response = await self.model.generate_content_async(
                        prompt, generation_config=batch_config
                    )

                    if paid_ctx is not None and reservation is not None:
                        um = getattr(response, "usage_metadata", None)
                        try:
                            prompt_tk = int(getattr(um, "prompt_token_count", 0) or 0) if um else 0
                        except Exception:
                            prompt_tk = 0
                        try:
                            cand_tk = int(getattr(um, "candidates_token_count", 0) or 0) if um else 0
                        except Exception:
                            cand_tk = 0
                        in_tk = int(prompt_tk or prompt_tokens_est)
                        if cand_tk <= 0 and getattr(response, "text", None):
                            cand_tk = max(1, int(len(response.text) / self.CHARS_PER_TOKEN))
                        out_tk = int(cand_tk or 0)
                        await paid_ctx["budget_manager"].commit(
                            reservation=reservation,
                            actual_input_tokens=in_tk,
                            actual_output_tokens=out_tk,
                        )
                        committed = True

                    # [FIX] 检查 response.parts
                    if not response.parts:
                        self.log_to_gui(
                            f"批次（{len(batch)} 个文件）被内容安全策略过滤，将逐个重试。",
                            "WARNING"
                        )
                        return {'success': False, 'failed_items': batch}

                    results = json.loads(response.text)

                    if not isinstance(results, list) or len(results) != len(batch):
                        self.log_to_gui(
                            f"批处理返回结果数量({len(results) if isinstance(results, list) else '?'}"
                            f"/{len(batch)})或格式错误，将逐个重试。",
                            "WARNING"
                        )
                        return {'success': False, 'failed_items': batch}

                    failed_items = []
                    for item, info in zip(batch, results):
                        new_base_name = self.build_filename(info) if isinstance(info, dict) else None
                        if not new_base_name:
                            failed_items.append(item)
                            continue
                        self.rename_file(
                            item['path'],
                            new_base_name,
                            info=info,
                            write_metadata=write_metadata,
                        )

                    success_count = max(0, len(batch) - len(failed_items))
                    if success_count:
                        self.gui_queue.put(("progress_update", success_count))
                    return {'success': len(failed_items) == 0, 'failed_items': failed_items}

                except Exception as e:
                    if paid_ctx is not None and reservation is not None and not committed:
                        try:
                            budget_mgr = paid_ctx.get("budget_manager")
                            if budget_mgr is not None:
                                await budget_mgr.rollback(reservation=reservation)
                        except Exception:
                            pass
                    self.log_to_gui(
                        f"处理批次时出错 (尝试 {attempt + 1}/{self.MAX_RETRIES}): {e}",
                        "ERROR"
                    )
                    if "quota" in str(e).lower() or "429" in str(e):
                        return {'success': False, 'failed_items': batch, 'quota_exceeded': True}
                    if attempt < self.MAX_RETRIES - 1:
                        await asyncio.sleep(2 ** attempt)

            self.log_to_gui("批次处理失败，已达到最大重试次数。", "ERROR")
            return {'success': False, 'failed_items': batch}

    async def run_processing(
        self,
        api_keys_str,
        target_dir_str,
        excluded_folder_paths,
        processing_mode="batch",
        stop_event=None,
        proxy_settings=None,
        write_metadata=True,
        paid_mode=False,
        monthly_budget_usd=10.0,
        paid_concurrency=None,
        paid_economy=False,
    ):
        self.stop_event = stop_event
        all_remaining_paths = []
        processing_finished_normally = False

        mode = str(processing_mode or "batch").strip().lower()
        if mode not in ("auto", "batch", "single"):
            mode = "batch"
        economy_mode = bool(paid_economy)

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

            self.log_to_gui("开始处理...")
            if self.stop_event.is_set():
                return
            if not api_keys_str or not target_dir_str:
                self.log_to_gui("错误: API密钥和目标目录为必填项。", "ERROR")
                return

            api_keys = [key.strip() for key in api_keys_str.split(',') if key.strip()]
            target_directory = Path(target_dir_str)
            if not target_directory.is_dir():
                self.log_to_gui(f"错误: 目录不存在: {target_dir_str}", "ERROR")
                return

            pending_paths = self.load_pending_files()
            if self.stop_event.is_set():
                return

            if pending_paths:
                self.log_to_gui(f"检测到断点日志，将只处理上次未完成的 {len(pending_paths)} 个文件。")
                files_to_process_paths = pending_paths
            else:
                self.log_to_gui("未检测到断点日志，将扫描整个目录进行新任务。")
                all_found_files = list(set([
                    p for ext in self.SUPPORTED_EXTENSIONS
                    for p in target_directory.glob(f"**/*{ext}")
                ]))
                if excluded_folder_paths:
                    resolved_excluded_paths = [p.resolve() for p in excluded_folder_paths]
                    files_to_process_paths = []
                    for p in all_found_files:
                        rp = p.resolve()
                        if any(_path_is_relative_to(rp, ex) for ex in resolved_excluded_paths):
                            continue
                        files_to_process_paths.append(p)
                else:
                    files_to_process_paths = all_found_files

            if not files_to_process_paths:
                self.log_to_gui("没有需要处理的文件。", "INFO")
                self.clear_pending_files_log()
                return

            all_remaining_paths = list(files_to_process_paths)

            if self.stop_event.is_set():
                return
            self.log_to_gui(f"找到 {len(files_to_process_paths)} 个文件待处理。")
            self.gui_queue.put(("set_progress_max", len(files_to_process_paths)))

            self.log_to_gui("正在提取文件文本...")
            all_file_data_map = {}
            skipped_count = 0
            io_workers = min(32, max(4, (os.cpu_count() or 8) * 2))
            loop = asyncio.get_running_loop()
            with ThreadPoolExecutor(max_workers=io_workers) as pool:
                tasks = [
                    loop.run_in_executor(pool, self.extract_text_from_file, path)
                    for path in files_to_process_paths
                ]
                extracted_texts = await asyncio.gather(*tasks, return_exceptions=True)

            for path, extracted in zip(files_to_process_paths, extracted_texts):
                text = None if isinstance(extracted, Exception) else extracted
                if text and str(text).strip():
                    tokens = max(1, int(len(text) / self.CHARS_PER_TOKEN))
                    all_file_data_map[path] = {'path': path, 'text': text, 'tokens': tokens}
                else:
                    # [FIX] 过滤空文本文件，不发送给 API
                    self.log_to_gui(f"文件 '{path.name}' 文本提取为空，跳过。", "WARNING")
                    skipped_count += 1
                    if path in all_remaining_paths:
                        all_remaining_paths.remove(path)
                    self.gui_queue.put(("progress_update", 1))

            if skipped_count > 0:
                self.log_to_gui(f"已跳过 {skipped_count} 个无法提取文本的文件。")

            if not all_file_data_map:
                self.log_to_gui("所有文件文本提取为空，无法处理。", "WARNING")
                self.clear_pending_files_log()
                return

            tracker_data = self.load_request_tracker(silent=False)
            usage_lock = asyncio.Lock()

            def _make_paid_ctx(budget_mgr, budget_nanos, key_id_str, max_ctx, max_out):
                return {
                    "budget_manager": budget_mgr,
                    "budget_nanos_usd": int(budget_nanos),
                    "key_id": key_id_str,
                    "max_context_tokens": int(max_ctx),
                    "max_output_tokens": int(max_out),
                    "safety_margin_tokens": int(self.BUDGET_SAFETY_MARGIN_TOKENS),
                }

            def _pack_batches_ffd_dict(items, *, max_tokens, max_items=None):
                sorted_items = sorted(items, key=lambda x: x.get("tokens", 0), reverse=True)
                batches = []  # list[dict(tokens=int, items=list)]
                for it in sorted_items:
                    placed = False
                    for b in batches:
                        items_ok = (max_items is None) or (len(b["items"]) < int(max_items))
                        if items_ok and (b["tokens"] + it["tokens"] <= max_tokens):
                            b["items"].append(it)
                            b["tokens"] += it["tokens"]
                            placed = True
                            break
                    if not placed:
                        batches.append({"items": [it], "tokens": int(it.get("tokens", 0) or 0)})
                return [b["items"] for b in batches]

            def _choose_auto_mode(*, n_files, quota, economy):
                threshold = 3 if economy else 30
                if n_files <= threshold and quota >= n_files:
                    return "single", threshold
                return "batch", threshold

            async def run_phase(
                remaining_paths,
                *,
                phase_label,
                model_name,
                rpm_limit,
                tpm_limit,
                daily_request_limit,
                max_tokens_per_batch,
                concurrency_limit,
                max_items_per_batch=None,
                paid_ctx_enabled=False,
                budget_mgr=None,
                budget_nanos_usd=0,
                max_context_tokens=0,
                max_output_tokens=0,
            ):
                current_remaining = list(remaining_paths)
                limiter = self.RateLimiter(int(rpm_limit), int(tpm_limit), self.log_to_gui)
                for key_index, api_key in enumerate(api_keys):
                    if self.stop_event.is_set() or not current_remaining:
                        break

                    self.log_to_gui(f"\n=== {phase_label}：正在尝试使用 API 密钥 #{key_index + 1} ===")
                    key_id = _make_key_id(api_key)
                    usage = tracker_data.get("usage", {})
                    if not isinstance(usage, dict):
                        usage = {}
                        tracker_data["usage"] = usage

                    # Migration: old tracker may have stored raw API key as the dict key.
                    if api_key in usage and not _looks_like_key_id(api_key):
                        usage[key_id] = _to_int(usage.get(key_id, 0)) + _to_int(usage.get(api_key))
                        usage.pop(api_key, None)

                    if key_id not in usage:
                        usage[key_id] = 0

                    requests_left = int(daily_request_limit) - _to_int(usage.get(key_id, 0))
                    if requests_left <= 0:
                        self.log_to_gui("该密钥今日请求配额已用尽，将尝试下一个密钥。", "WARNING")
                        continue
                    self.log_to_gui(f"该密钥剩余请求配额: {requests_left}", "INFO")

                    if not await self.switch_and_configure_api(api_key, model_name=model_name):
                        continue

                    paid_ctx = None
                    if paid_ctx_enabled and budget_mgr is not None:
                        paid_ctx = _make_paid_ctx(
                            budget_mgr, budget_nanos_usd, key_id, max_context_tokens, max_output_tokens
                        )

                    successfully_processed_paths = set()
                    current_files_data = [
                        all_file_data_map[path]
                        for path in current_remaining
                        if path in all_file_data_map
                    ]

                    try:
                        mode_for_key = mode
                        economy_active = bool(economy_mode and paid_ctx is not None)
                        if mode_for_key == "auto":
                            mode_for_key, threshold = _choose_auto_mode(
                                n_files=len(current_files_data),
                                quota=int(requests_left),
                                economy=economy_active,
                            )
                            self.log_to_gui(
                                f"--- 自动策略：{'单文件并发' if mode_for_key == 'single' else '批处理'} "
                                f"(files={len(current_files_data)}, quota={requests_left}, "
                                f"threshold={threshold}, economy={economy_active}) ---",
                                "INFO",
                            )
                        elif mode_for_key == "single":
                            self.log_to_gui("--- 单文件并发模式 ---", "INFO")
                        else:
                            self.log_to_gui("--- 启动批处理模式 ---", "INFO")

                        if mode_for_key == "single":
                            to_process = current_files_data[: max(0, int(requests_left))]
                            if not to_process:
                                break

                            semaphore_single = asyncio.Semaphore(int(concurrency_limit))

                            async def _run_one_single(it):
                                async with semaphore_single:
                                    return await self._process_single_file(
                                        it,
                                        limiter,
                                        tracker_data,
                                        key_id,
                                        usage_lock,
                                        write_metadata,
                                        paid_ctx=paid_ctx,
                                    )

                            tasks = [_run_one_single(it) for it in to_process]
                            results = await asyncio.gather(*tasks, return_exceptions=True)

                            quota_exceeded = False
                            budget_exceeded = False
                            for it, result in zip(to_process, results):
                                if isinstance(result, Exception):
                                    self.log_to_gui(f"单文件任务发生异常: {result}", "ERROR")
                                    if "quota" in str(result).lower() or "429" in str(result):
                                        quota_exceeded = True
                                    continue
                                if isinstance(result, dict):
                                    if result.get("success"):
                                        successfully_processed_paths.add(it["path"])
                                    if result.get("budget_exceeded"):
                                        budget_exceeded = True

                            if quota_exceeded:
                                self.log_to_gui("API密钥配额已用尽或速率过快，将尝试下一个密钥。", "WARNING")
                            if budget_exceeded and paid_ctx is not None:
                                # Budget exhausted for this key in paid phase: move to next key.
                                pass

                        else:
                            pack_candidates = []
                            for it in current_files_data:
                                if it["tokens"] > max_tokens_per_batch:
                                    self.log_to_gui(f"文件 '{it['path'].name}' 过大，跳过。", "WARNING")
                                    self.gui_queue.put(("progress_update", 1))
                                    successfully_processed_paths.add(it["path"])
                                    continue
                                pack_candidates.append(it)

                            batches_all = _pack_batches_ffd_dict(
                                pack_candidates,
                                max_tokens=int(max_tokens_per_batch),
                                max_items=int(max_items_per_batch) if max_items_per_batch else None,
                            )
                            if not batches_all:
                                self.log_to_gui("根据剩余文件未能创建任何处理批次。")
                                break

                            batches_to_process = batches_all[:requests_left]
                            leftover_batches = batches_all[requests_left:]
                            if leftover_batches:
                                self.log_to_gui(
                                    f"该密钥剩余请求配额仅 {requests_left}，{len(leftover_batches)} 个批次将留给下一个密钥。",  # noqa: E501
                                    "WARNING",
                                )

                            self.log_to_gui(f"使用当前密钥处理 {len(batches_to_process)} 个批次...", "INFO")
                            requests_left -= len(batches_to_process)
                            semaphore = asyncio.Semaphore(concurrency_limit)

                            tasks = [
                                self.process_batch(
                                    b,
                                    limiter,
                                    semaphore,
                                    tracker_data,
                                    key_id,
                                    usage_lock,
                                    write_metadata,
                                    paid_ctx=paid_ctx,
                                    max_output_tokens=max_output_tokens if paid_ctx else None,
                                )
                                for b in batches_to_process
                            ]
                            results = await asyncio.gather(*tasks, return_exceptions=True)

                            failed_items_to_retry = []
                            quota_exceeded = False
                            budget_exceeded = False

                            for i, result in enumerate(results):
                                if isinstance(result, Exception):
                                    self.log_to_gui(f"批次 {i + 1} 发生异常: {result}", "ERROR")
                                    failed_items_to_retry.extend(batches_to_process[i])
                                    if "quota" in str(result).lower() or "429" in str(result):
                                        quota_exceeded = True
                                elif isinstance(result, dict):
                                    failed_items = result.get("failed_items", []) or []
                                    failed_paths = {it.get("path") for it in failed_items if isinstance(it, dict)}

                                    # Support partial success: mark succeeded items in this batch.
                                    for item in batches_to_process[i]:
                                        if item.get("path") not in failed_paths:
                                            successfully_processed_paths.add(item["path"])

                                    if not result.get("success"):
                                        if result.get("budget_exceeded"):
                                            budget_exceeded = True
                                            continue
                                        if result.get("quota_exceeded"):
                                            quota_exceeded = True
                                        else:
                                            failed_items_to_retry.extend(failed_items)

                            # Budget exceeded for this key in paid phase: skip retries and move to next key.
                            if budget_exceeded and paid_ctx is not None:
                                pass
                            # 批处理失败的文件降级为单文件重试
                            elif failed_items_to_retry and not quota_exceeded and not self.stop_event.is_set():
                                self.log_to_gui(
                                    f"有 {len(failed_items_to_retry)} 个文件批处理失败，将以单文件模式重试...",
                                    "WARNING",
                                )
                                for item in failed_items_to_retry:
                                    if self.stop_event.is_set():
                                        break
                                    if requests_left <= 0:
                                        self.log_to_gui(
                                            "该密钥今日请求配额已用尽，剩余失败文件将留给下一个密钥。",
                                            "WARNING",
                                        )
                                        break

                                    result = await self._process_single_file(
                                        item,
                                        limiter,
                                        tracker_data,
                                        key_id,
                                        usage_lock,
                                        write_metadata,
                                        paid_ctx=paid_ctx,
                                    )
                                    if result.get("budget_exceeded") and paid_ctx is not None:
                                        break

                                    requests_left -= 1
                                    if result.get("success"):
                                        successfully_processed_paths.add(item["path"])

                    except Exception as e:
                        if "quota" in str(e).lower() or "429" in str(e):
                            self.log_to_gui("API密钥配额已用尽或速率过快，将尝试下一个密钥。", "WARNING")
                        else:
                            self.log_to_gui(f"发生未预期错误: {e}", "ERROR")
                            import traceback
                            self.log_to_gui(traceback.format_exc(), "DEBUG")

                    current_remaining = [
                        path for path in current_remaining
                        if path not in successfully_processed_paths
                    ]
                    self.save_request_tracker(tracker_data)

                return current_remaining

            # Phase 1: paid tier (optional)
            if paid_mode and not self.stop_event.is_set() and all_remaining_paths:
                try:
                    budget_nanos = int(round(float(monthly_budget_usd) * 1_000_000_000))
                except Exception:
                    budget_nanos = 10_000_000_000

                effective_paid_max_input = min(
                    int(self.PAID_MAX_REQUEST_TOKENS),
                    max(
                        1,
                        int(self.PAID_MAX_CONTEXT_TOKENS)
                        - int(self.PAID_MAX_OUTPUT_TOKENS)
                        - int(self.BUDGET_SAFETY_MARGIN_TOKENS)
                        - int(self.PROMPT_OVERHEAD_TOKENS),
                    ),
                )
                conc = self.PAID_CONCURRENCY_DEFAULT if paid_concurrency is None else int(paid_concurrency)
                conc = max(1, min(conc, int(self.PAID_RPM_LIMIT)))

                self.log_to_gui("\n=== 付费阶段：Gemini 3 Flash ===", "INFO")
                budget_mgr = BudgetManager(self.BUDGET_FILE)
                all_remaining_paths = await run_phase(
                    all_remaining_paths,
                    phase_label="付费阶段",
                    model_name=self.PAID_MODEL_NAME,
                    rpm_limit=self.PAID_RPM_LIMIT,
                    tpm_limit=self.PAID_TPM_LIMIT,
                    daily_request_limit=self.PAID_DAILY_REQUEST_LIMIT,
                    max_tokens_per_batch=effective_paid_max_input,
                    concurrency_limit=conc,
                    max_items_per_batch=self.PAID_MAX_ITEMS_PER_BATCH,
                    paid_ctx_enabled=True,
                    budget_mgr=budget_mgr,
                    budget_nanos_usd=budget_nanos,
                    max_context_tokens=self.PAID_MAX_CONTEXT_TOKENS,
                    max_output_tokens=self.PAID_MAX_OUTPUT_TOKENS,
                )

            # Phase 2: free tier fallback (always if remaining)
            if not self.stop_event.is_set() and all_remaining_paths:
                if paid_mode:
                    self.log_to_gui("\n=== 降级阶段：免费模型继续处理 ===", "INFO")
                all_remaining_paths = await run_phase(
                    all_remaining_paths,
                    phase_label="免费阶段",
                    model_name=self.FREE_MODEL_NAME,
                    rpm_limit=self.RPM_LIMIT,
                    tpm_limit=self.TPM_LIMIT,
                    daily_request_limit=self.DAILY_REQUEST_LIMIT,
                    max_tokens_per_batch=self.MAX_TOKENS_PER_BATCH,
                    concurrency_limit=self.CONCURRENCY_LIMIT,
                    max_items_per_batch=None,
                    paid_ctx_enabled=False,
                )

            if self.stop_event.is_set():
                self.log_to_gui("任务被用户终止。")

            if all_remaining_paths:
                self.log_to_gui(
                    f"处理完成，仍有 {len(all_remaining_paths)} 个文件未处理，已保存到断点日志。",
                    "WARNING"
                )
                self.save_pending_files(all_remaining_paths)
            else:
                self.log_to_gui("所有文件已成功处理！")
                self.clear_pending_files_log()

            processing_finished_normally = True

        except Exception as e:
            self.log_to_gui(f"发生严重错误: {e}", "CRITICAL")
            import traceback
            self.log_to_gui(traceback.format_exc(), "DEBUG")
        finally:
            # [FIX] 仅在非正常结束时保存 pending files，避免重复保存
            if not processing_finished_normally and all_remaining_paths:
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
                for folder in sorted(subfolders):
                    folder_path_str = str(folder.resolve())
                    initial_value = folder_path_str if folder_path_str in self.initial_excluded_str else ""
                    var = ctk.StringVar(value=initial_value)
                    cb = ctk.CTkCheckBox(
                        scrollable_frame, text=folder.name, variable=var,
                        onvalue=folder_path_str, offvalue=""
                    )
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

        usage = self.tracker_data.get("usage", {})
        if not isinstance(usage, dict):
            usage = {}
            self.tracker_data["usage"] = usage

        current_keys = [k.strip() for k in parent.api_keys_entry.get().split(',') if k.strip()]
        current_key_ids = []
        current_id_to_key = {}
        for key in current_keys:
            key_id = _make_key_id(key)
            current_key_ids.append(key_id)
            current_id_to_key[key_id] = key

            # Migration: old tracker may have stored raw API key as the dict key.
            if key in usage and not _looks_like_key_id(key):
                usage[key_id] = _to_int(usage.get(key_id, 0)) + _to_int(usage.get(key))
                usage.pop(key, None)

            if key_id not in usage:
                usage[key_id] = 0

        # Do not display or keep legacy raw-key entries beyond migration.
        for k in list(usage.keys()):
            if not _looks_like_key_id(k):
                usage.pop(k, None)

        main_frame = ctk.CTkFrame(self)
        main_frame.pack(expand=True, fill="both", padx=10, pady=10)

        date_frame = ctk.CTkFrame(main_frame)
        date_frame.pack(fill="x", padx=10, pady=5)
        ctk.CTkLabel(date_frame, text="记录日期 (YYYY-MM-DD):").pack(side="left", padx=5)
        self.date_entry = ctk.CTkEntry(date_frame)
        self.date_entry.insert(0, self.tracker_data.get("date", ""))
        self.date_entry.pack(side="left", expand=True, fill="x", padx=5)

        ctk.CTkLabel(main_frame, text="各API密钥已用请求数:").pack(anchor="w", padx=10, pady=(10, 0))
        scroll_frame = ctk.CTkScrollableFrame(main_frame)
        scroll_frame.pack(expand=True, fill="both", padx=10, pady=5)

        def _mask(s: str) -> str:
            return f"{s[:8]}...{s[-4:]}" if len(s) > 12 else s

        if not usage:
            ctk.CTkLabel(scroll_frame, text="暂无用量记录。").pack(pady=10)
        else:
            # Show current keys first (masked), then any other key_ids found in the tracker.
            ordered_ids = list(dict.fromkeys(current_key_ids + sorted(
                k for k in usage.keys() if _looks_like_key_id(k)
            )))
            for key_id in ordered_ids:
                if key_id not in usage:
                    continue
                key_frame = ctk.CTkFrame(scroll_frame)
                key_frame.pack(fill="x", pady=2)
                display = current_id_to_key.get(key_id, key_id)
                ctk.CTkLabel(key_frame, text=_mask(display), width=200, anchor="w").pack(side="left", padx=5)
                entry = ctk.CTkEntry(key_frame)
                entry.insert(0, str(_to_int(usage.get(key_id, 0))))
                entry.pack(side="left", expand=True, fill="x", padx=5)
                self.usage_entries[key_id] = entry

        button_frame = ctk.CTkFrame(self, fg_color="transparent")
        button_frame.pack(fill="x", padx=10, pady=10)
        ctk.CTkButton(button_frame, text="保存", command=self.save_changes).pack(side="right", padx=5)
        ctk.CTkButton(button_frame, text="取消", command=self.destroy).pack(side="right", padx=5)

    def save_changes(self):
        new_data = {"date": self.date_entry.get(), "usage": {}}
        try:
            time.strptime(new_data["date"], '%Y-%m-%d')
            for key_id, entry in self.usage_entries.items():
                new_data["usage"][key_id] = _to_int(entry.get())
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
        self.processing_mode_var = ctk.StringVar(value="批处理")
        self.write_metadata_var = ctk.BooleanVar(value=True)
        self.paid_mode_var = ctk.BooleanVar(value=False)
        self.monthly_budget_var = ctk.StringVar(value="10")
        self.paid_concurrency_var = ctk.StringVar(value="20")
        self.paid_economy_var = ctk.BooleanVar(value=False)
        self.stop_event = threading.Event()

        # ===== 代理相关变量 =====
        self.auto_proxy_var = ctk.BooleanVar(value=True)
        self.manual_proxy_var = ctk.StringVar(value="")
        # [FIX] 新增：记录自动检测到的代理（不直接写入 manual_proxy_var）
        self._detected_proxy = ""

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
        ctk.CTkLabel(settings_frame, text="Google API Keys (逗号分隔):").grid(
            row=0, column=0, padx=10, pady=5, sticky="w"
        )
        self.api_keys_entry = ctk.CTkEntry(settings_frame, placeholder_text="key1,key2,...")
        self.api_keys_entry.grid(row=0, column=1, columnspan=2, padx=10, pady=5, sticky="ew")

        # 目标文件夹
        ctk.CTkLabel(settings_frame, text="目标文件夹:").grid(
            row=1, column=0, padx=10, pady=5, sticky="w"
        )
        self.dir_entry = ctk.CTkEntry(settings_frame, placeholder_text="尚未选择文件夹")
        self.dir_entry.grid(row=1, column=1, padx=10, pady=5, sticky="ew")
        self.browse_button = ctk.CTkButton(
            settings_frame, text="浏览...", command=self.browse_directory, width=100
        )
        self.browse_button.grid(row=1, column=2, padx=(5, 10), pady=5)

        # 功能操作
        ctk.CTkLabel(settings_frame, text="功能操作:").grid(
            row=2, column=0, padx=10, pady=5, sticky="w"
        )
        button_group_frame = ctk.CTkFrame(settings_frame, fg_color="transparent")
        button_group_frame.grid(row=2, column=1, columnspan=2, pady=5, sticky="w")

        self.exclusion_button = ctk.CTkButton(
            button_group_frame, text="选择排除文件夹...", command=self.open_exclusion_dialog
        )
        self.exclusion_button.pack(side="left", padx=(0, 5))

        self.usage_button = ctk.CTkButton(
            button_group_frame, text="查看/编辑API用量", command=self.open_usage_dialog
        )
        self.usage_button.pack(side="left", padx=5)

        # 选项行
        options_frame = ctk.CTkFrame(settings_frame, fg_color="transparent")
        options_frame.grid(row=3, column=1, columnspan=2, pady=5, sticky="w")

        ctk.CTkLabel(options_frame, text="处理模式:").pack(side="left", padx=(0, 5))
        self.processing_mode_menu = ctk.CTkOptionMenu(
            options_frame,
            values=["自动", "批处理", "单文件"],
            variable=self.processing_mode_var,
            width=120,
        )
        self.processing_mode_menu.pack(side="left", padx=(0, 15))

        self.metadata_checkbox = ctk.CTkCheckBox(
            options_frame, text="写入元数据", variable=self.write_metadata_var
        )
        self.metadata_checkbox.pack(side="left", padx=(0, 15))

        # ===== 付费模式（Gemini 3 Flash）=====
        paid_frame = ctk.CTkFrame(settings_frame, fg_color="transparent")
        paid_frame.grid(row=4, column=1, columnspan=2, pady=5, sticky="w")

        self.paid_mode_checkbox = ctk.CTkCheckBox(
            paid_frame,
            text="付费模式（Gemini 3 Flash，$10/Key/月）",
            variable=self.paid_mode_var,
            command=self.on_paid_mode_change,
        )
        self.paid_mode_checkbox.pack(side="left", padx=(0, 15))

        ctk.CTkLabel(paid_frame, text="月预算($):").pack(side="left", padx=(0, 5))
        self.monthly_budget_entry = ctk.CTkEntry(
            paid_frame, width=80, textvariable=self.monthly_budget_var
        )
        self.monthly_budget_entry.pack(side="left", padx=(0, 15))

        ctk.CTkLabel(paid_frame, text="并发:").pack(side="left", padx=(0, 5))
        self.paid_concurrency_entry = ctk.CTkEntry(
            paid_frame, width=60, textvariable=self.paid_concurrency_var
        )
        self.paid_concurrency_entry.pack(side="left", padx=(0, 10))

        self.paid_economy_checkbox = ctk.CTkCheckBox(
            paid_frame,
            text="省钱模式(尽量批处理)",
            variable=self.paid_economy_var,
            command=self.update_budget_status_label,
        )
        self.paid_economy_checkbox.pack(side="left", padx=(0, 10))

        self.budget_status_label = ctk.CTkLabel(
            settings_frame, text="付费预算: 未启用", text_color="gray"
        )
        self.budget_status_label.grid(row=5, column=1, columnspan=2, padx=10, pady=(0, 5), sticky="w")

        self.exclusion_status_label = ctk.CTkLabel(
            settings_frame, text="当前未排除任何文件夹。", text_color="gray"
        )
        self.exclusion_status_label.grid(row=6, column=1, columnspan=2, padx=10, pady=(0, 5), sticky="w")

        # ===== 代理设置 UI =====
        ctk.CTkLabel(settings_frame, text="代理设置:").grid(
            row=7, column=0, padx=10, pady=5, sticky="w"
        )
        proxy_frame = ctk.CTkFrame(settings_frame, fg_color="transparent")
        proxy_frame.grid(row=7, column=1, columnspan=2, pady=5, sticky="w")

        self.auto_proxy_checkbox = ctk.CTkCheckBox(
            proxy_frame, text="自动检测系统代理",
            variable=self.auto_proxy_var, command=self.on_proxy_mode_change
        )
        self.auto_proxy_checkbox.pack(side="left", padx=(0, 15))

        ctk.CTkLabel(proxy_frame, text="手动代理:").pack(side="left", padx=(0, 5))
        self.proxy_entry = ctk.CTkEntry(
            proxy_frame, placeholder_text="如 http://127.0.0.1:7890",
            width=200, textvariable=self.manual_proxy_var
        )
        self.proxy_entry.pack(side="left", padx=(0, 10))

        self.detect_proxy_button = ctk.CTkButton(
            proxy_frame, text="检测代理",
            command=self.detect_and_show_proxy, width=80
        )
        self.detect_proxy_button.pack(side="left")

        self.proxy_status_label = ctk.CTkLabel(
            settings_frame, text="代理状态: 未检测", text_color="gray"
        )
        self.proxy_status_label.grid(row=8, column=1, columnspan=2, padx=10, pady=(0, 5), sticky="w")

        # Initialize paid-mode widget states.
        self.on_paid_mode_change()

        # 日志区域
        log_frame = ctk.CTkFrame(self)
        log_frame.grid(row=1, column=0, padx=10, pady=(0, 10), sticky="nsew")
        log_frame.grid_rowconfigure(0, weight=1)
        log_frame.grid_columnconfigure(0, weight=1)
        self.log_textbox = ctk.CTkTextbox(log_frame, state="disabled", wrap="word")
        self.log_textbox.grid(row=0, column=0, padx=10, pady=10, sticky="nsew")

        # 控制区域
        control_frame = ctk.CTkFrame(self)
        control_frame.grid(row=2, column=0, padx=10, pady=(0, 10), sticky="ew")
        control_frame.grid_columnconfigure(0, weight=1)
        control_frame.grid_columnconfigure(1, weight=1)

        self.progressbar = ctk.CTkProgressBar(control_frame)
        self.progressbar.grid(row=0, column=0, columnspan=2, padx=10, pady=5, sticky="ew")
        self.progressbar.set(0)

        self.start_button = ctk.CTkButton(
            control_frame, text="开始重命名", command=self.start_processing_thread, height=35
        )
        self.start_button.grid(row=1, column=0, padx=10, pady=10, sticky="ew")

        self.stop_button = ctk.CTkButton(
            control_frame, text="终止", command=self.stop_processing, height=35,
            fg_color="red", hover_color="darkred", state="disabled"
        )
        self.stop_button.grid(row=1, column=1, padx=10, pady=10, sticky="ew")

    # ===== 代理相关方法 =====
    def on_proxy_mode_change(self):
        if self.auto_proxy_var.get():
            self.proxy_entry.configure(state="disabled")
            self.detect_and_show_proxy()
        else:
            self.proxy_entry.configure(state="normal")
            self.proxy_status_label.configure(text="代理状态: 使用手动配置", text_color="orange")

    def detect_and_show_proxy(self):
        """
        [FIX] 检测代理时不再覆盖 manual_proxy_var。
        自动检测结果存储在 _detected_proxy 中。
        """
        detected = ProxyDetector.detect()
        if detected:
            self._detected_proxy = detected
            self.proxy_status_label.configure(
                text=f"代理状态: 已检测到 {detected}", text_color="green"
            )
            self.log(f"检测到系统代理: {detected}")
        else:
            self._detected_proxy = ""
            self.proxy_status_label.configure(
                text="代理状态: 未检测到系统代理", text_color="gray"
            )
            self.log("未检测到系统代理，将直接连接")

    def get_proxy_settings(self):
        """
        [FIX] 分离自动检测和手动输入的代理。
        """
        if self.auto_proxy_var.get():
            return {
                'auto': True,
                'manual': self._detected_proxy  # 用自动检测的值
            }
        else:
            return {
                'auto': False,
                'manual': self.manual_proxy_var.get().strip()
            }

    # ===== 付费模式相关方法 =====
    def on_paid_mode_change(self):
        enabled = bool(self.paid_mode_var.get())
        state = "normal" if enabled else "disabled"
        try:
            self.monthly_budget_entry.configure(state=state)
            self.paid_concurrency_entry.configure(state=state)
            self.paid_economy_checkbox.configure(state=state)
        except Exception:
            pass
        self.update_budget_status_label()

    def update_budget_status_label(self):
        if not hasattr(self, "budget_status_label"):
            return

        if not bool(self.paid_mode_var.get()):
            self.budget_status_label.configure(text="付费预算: 未启用", text_color="gray")
            return

        keys = [k.strip() for k in self.api_keys_entry.get().split(",") if k.strip()]
        if not keys:
            self.budget_status_label.configure(text="付费预算: 未填写 API key", text_color="orange")
            return

        def _mask(s: str) -> str:
            return f"{s[:8]}...{s[-4:]}" if len(s) > 12 else s

        try:
            budget_usd = float(self.monthly_budget_var.get().strip() or "10")
        except Exception:
            budget_usd = 10.0
        budget_nanos = int(round(budget_usd * 1_000_000_000))

        snapshot = BudgetManager(Path("./budget_tracker.json")).read_snapshot()
        month = BudgetManager.month_key()
        month_bucket = snapshot.get("months", {})
        month_bucket = month_bucket.get(month, {}) if isinstance(month_bucket, dict) else {}

        parts = []
        show = keys[:3]
        for key in show:
            key_id = _make_key_id(key)
            entry = month_bucket.get(key_id, {}) if isinstance(month_bucket, dict) else {}
            spent_nanos = _to_int(entry.get("spent_nanos_usd", 0)) if isinstance(entry, dict) else 0
            spent_usd = spent_nanos / 1_000_000_000
            parts.append(f"{_mask(key)}: ${spent_usd:.2f}/${budget_usd:.2f}")

        if len(keys) > len(show):
            parts.append(f"+{len(keys) - len(show)} keys")

        if budget_nanos <= 0:
            self.budget_status_label.configure(text="付费预算: 无效预算", text_color="orange")
            return

        self.budget_status_label.configure(
            text="付费预算(本月): " + " | ".join(parts),
            text_color=("black", "white"),
        )

    # ===== 其他方法 =====

    def open_usage_dialog(self):
        UsageDialog(self, self.backend)

    def browse_directory(self):
        current_dir = self.dir_entry.get()
        dir_path = filedialog.askdirectory(
            title="请选择包含文件的文件夹",
            initialdir=current_dir if current_dir and Path(current_dir).is_dir() else None
        )
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
            self.exclusion_status_label.configure(
                text=f"已排除 {count} 个文件夹: {folder_names}",
                text_color=("black", "white")
            )

    def load_config(self):
        try:
            if Path(CONFIG_FILE).exists():
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    config = json.load(f)

                api_keys_from_config = config.get("api_keys", "")
                if api_keys_from_config:
                    self.api_keys_entry.insert(0, api_keys_from_config.strip())

                target_dir = config.get("target_directory", "")
                if target_dir:
                    self.dir_entry.insert(0, target_dir)

                excluded_paths_str = config.get("excluded_folders", [])
                if excluded_paths_str:
                    self.excluded_folders = [Path(p) for p in excluded_paths_str]
                    self.update_exclusion_status_label()

                # Processing mode (migration: legacy single_mode boolean)
                pm = str(config.get("processing_mode", "")).strip().lower()
                if pm == "auto":
                    self.processing_mode_var.set("自动")
                elif pm == "single":
                    self.processing_mode_var.set("单文件")
                elif pm == "batch":
                    self.processing_mode_var.set("批处理")
                else:
                    self.processing_mode_var.set(
                        "单文件" if bool(config.get("single_mode", False)) else "批处理"
                    )
                self.write_metadata_var.set(config.get("write_metadata", True))

                # 付费模式配置
                self.paid_mode_var.set(config.get("paid_mode", False))
                self.monthly_budget_var.set(str(config.get("monthly_budget_usd", "10")).strip() or "10")
                self.paid_concurrency_var.set(str(config.get("paid_concurrency", "20")).strip() or "20")
                self.paid_economy_var.set(bool(config.get("paid_economy", False)))

                # 加载代理配置
                self.auto_proxy_var.set(config.get("auto_proxy", True))
                manual_proxy = config.get("manual_proxy", "")
                if manual_proxy:
                    self.manual_proxy_var.set(manual_proxy)

                if self.auto_proxy_var.get():
                    self.proxy_entry.configure(state="disabled")
                    self.after(500, self.detect_and_show_proxy)
                else:
                    self.proxy_entry.configure(state="normal")
                    self.proxy_status_label.configure(
                        text="代理状态: 使用手动配置", text_color="orange"
                    )

                self.on_paid_mode_change()
                self.log("已从 config.json 加载保存的配置。")
        except Exception as e:
            self.log(f"无法加载配置文件: {e}", "ERROR")

    def save_config(self):
        try:
            pm_display = str(self.processing_mode_var.get()).strip()
            if pm_display == "自动":
                pm = "auto"
            elif pm_display == "单文件":
                pm = "single"
            else:
                pm = "batch"

            config_data = {
                "api_keys": self.api_keys_entry.get(),
                "target_directory": self.dir_entry.get(),
                "excluded_folders": [str(p.resolve()) for p in self.excluded_folders],
                "processing_mode": pm,
                # Legacy compatibility: keep single_mode boolean.
                "single_mode": (pm == "single"),
                "write_metadata": self.write_metadata_var.get(),
                "paid_mode": bool(self.paid_mode_var.get()),
                "monthly_budget_usd": self.monthly_budget_var.get().strip(),
                "paid_concurrency": self.paid_concurrency_var.get().strip(),
                "paid_economy": bool(self.paid_economy_var.get()),
                "auto_proxy": self.auto_proxy_var.get(),
                "manual_proxy": self.manual_proxy_var.get().strip(),
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

        paid_mode = bool(self.paid_mode_var.get())
        monthly_budget_usd = 10.0
        paid_concurrency = 20
        paid_economy = bool(self.paid_economy_var.get())
        if paid_mode:
            try:
                monthly_budget_usd = float(self.monthly_budget_var.get().strip() or "10")
                if monthly_budget_usd <= 0:
                    raise ValueError("monthly budget must be > 0")
            except Exception:
                messagebox.showerror("输入错误", "月预算($) 必须是大于 0 的数字。")
                return
            try:
                paid_concurrency = int(self.paid_concurrency_var.get().strip() or "8")
                if paid_concurrency <= 0:
                    raise ValueError("concurrency must be > 0")
            except Exception:
                messagebox.showerror("输入错误", "并发必须是大于 0 的整数。")
                return

        self.update_budget_status_label()

        proxy_settings = self.get_proxy_settings()
        proxy = proxy_settings.get('manual', '')
        if proxy:
            self.log(f"将使用代理: {proxy}")
        else:
            self.log("未配置代理，将直接连接 Google API")

        self.start_button.configure(state="disabled", text="正在处理中...")
        self.stop_button.configure(state="normal")
        self.progressbar.set(0)
        self.progress_max = 0
        self.progress_current = 0
        self.log_textbox.configure(state="normal")
        self.log_textbox.delete("1.0", "end")
        self.log_textbox.configure(state="disabled")

        pm_display = str(self.processing_mode_var.get()).strip()
        if pm_display == "自动":
            processing_mode = "auto"
        elif pm_display == "单文件":
            processing_mode = "single"
        else:
            processing_mode = "batch"

        threading.Thread(
            target=lambda: asyncio.run(self.backend.run_processing(
                self.api_keys_entry.get(),
                self.dir_entry.get(),
                self.excluded_folders,
                processing_mode,
                self.stop_event,
                proxy_settings,
                self.write_metadata_var.get(),
                paid_mode=paid_mode,
                monthly_budget_usd=monthly_budget_usd,
                paid_concurrency=paid_concurrency,
                paid_economy=paid_economy,
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
                            self.progressbar.set(
                                min(1.0, self.progress_current / self.progress_max)
                            )
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
            messagebox.showerror(
                "未捕获的异常",
                f"发生了一个严重错误:\n\n{exc_type.__name__}: {exc_value}\n\n"
                "详细信息已打印到控制台。"
            )
        except Exception:
            pass

    sys.excepthook = handle_exception

    backend = Backend(gui_queue=queue.Queue())
    app = App(backend)
    app.protocol("WM_DELETE_WINDOW", app.on_closing)
    app.mainloop()
