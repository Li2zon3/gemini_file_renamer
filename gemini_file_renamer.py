# -*- coding: utf-8 -*-
"""
Gemini File Renamer - 命令行版本（带代理支持）
使用 Gemini API 批量智能重命名文件并写入元数据

修复版 - 修复了以下问题：
- count_tokens 改为本地估算，不再浪费 API 调用
- _process_single_mode 使用 enumerate 替代 items.index()
- 增加 response.parts 空值检查
- ProcessingStats.total_failed 正确递增
- EPUB _clear_creators 更健壮
- 批处理模式支持真正的并发执行
- 改进错误处理与边界情况
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
import time
import platform
from abc import ABC, abstractmethod
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from enum import Enum, auto
from pathlib import Path
from typing import (
    Any,
    Deque,
    Dict,
    List,
    Optional,
    Sequence,
    Tuple,
)
from urllib.request import getproxies
import argparse

# 第三方库
import google.generativeai as genai
import pymupdf
from bs4 import BeautifulSoup
from docx import Document
from ebooklib import ITEM_DOCUMENT, epub
from pathvalidate import sanitize_filename
from tqdm.asyncio import tqdm


# ============================================================================
# 代理检测模块
# ============================================================================

class ProxyDetector:
    """系统代理检测器"""

    @staticmethod
    def get_windows_proxy() -> Optional[str]:
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
    def get_macos_proxy() -> Optional[str]:
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
    def get_env_proxy() -> Optional[str]:
        for var in ['HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy',
                     'ALL_PROXY', 'all_proxy']:
            proxy = os.environ.get(var)
            if proxy:
                return proxy
        return None

    @staticmethod
    def get_urllib_proxy() -> Optional[str]:
        proxies = getproxies()
        return proxies.get('https') or proxies.get('http')

    @classmethod
    def detect(cls) -> Optional[str]:
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
    def apply(cls, proxy: Optional[str] = None, auto_detect: bool = True) -> Dict[str, Any]:
        result = {'proxy': None, 'applied': False}
        proxy_to_use = proxy if proxy else (cls.detect() if auto_detect else None)
        if not proxy_to_use:
            return result
        result['proxy'] = proxy_to_use
        for var_name in ['HTTP_PROXY', 'HTTPS_PROXY', 'GRPC_PROXY',
                         'http_proxy', 'https_proxy', 'grpc_proxy']:
            os.environ[var_name] = proxy_to_use
        result['applied'] = True
        return result


# ============================================================================
# 配置模块
# ============================================================================

@dataclass(frozen=True)
class Config:
    rpm_limit: int = 10
    tpm_limit: int = 250_000
    daily_request_limit: int = 250
    max_tokens_per_request: int = 27_000
    # Hard safety ceiling for a single request's (input + output) context window.
    max_context_tokens: int = 200_000
    # Limit output to keep billing predictable (esp. in paid tier).
    max_output_tokens: int = 8_192
    # Extra headroom to avoid accidentally crossing a context pricing boundary.
    budget_safety_margin_tokens: int = 8_000
    concurrency_limit: int = 5
    max_retries: int = 3
    max_items_per_batch: int = 12
    io_workers: int = field(default_factory=lambda: min(32, max(4, (os.cpu_count() or 8) * 2)))
    chars_per_token: float = 3.5
    supported_extensions: Tuple[str, ...] = ('.pdf', '.epub', '.azw3', '.docx')
    pending_files_log: Path = field(default_factory=lambda: Path("./pending_files.txt"))
    tracker_file: Path = field(default_factory=lambda: Path("./request_tracker.json"))
    budget_file: Path = field(default_factory=lambda: Path("./budget_tracker.json"))

    @property
    def max_chars_per_request(self) -> int:
        return int(self.max_tokens_per_request * self.chars_per_token)


CONFIG = Config()


class ProcessingMode(Enum):
    BATCH = auto()
    SINGLE = auto()
    AUTO = auto()


# ============================================================================
# Prompts 和 Schema
# ============================================================================

PROMPTS = {
    'batch': (
        "Analyze the following text, which contains MULTIPLE documents concatenated together.\n"
        "Each document starts with a \"--- START OF FILE: [filename] ---\" marker and ends with "
        "an \"--- END OF FILE: [filename] ---\" marker.\n"
        "For EACH document provided, extract its metadata. Also extract a list of 3-5 relevant keywords.\n"
        "Return a single JSON array containing all the extracted JSON objects.\n"
        "The order of objects in the final list MUST match the order of the documents in the input text.\n"
        "Do not add any commentary. Only return the JSON array."
    ),
    'single': (
        "Analyze the text from the following document to extract its metadata.\n"
        "Based on the content, provide a JSON object with the following details.\n"
        "Also extract a list of 3-5 relevant keywords from the document's content.\n"
        "Do not add any commentary. Only return the JSON object."
    )
}

SINGLE_OBJECT_SCHEMA: Dict[str, Any] = {
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
    "required": ["title"]
}

BATCH_SCHEMA: Dict[str, Any] = {
    "type": "array",
    "items": SINGLE_OBJECT_SCHEMA
}


# ============================================================================
# 日志
# ============================================================================

def setup_logging(level: int = logging.INFO) -> logging.Logger:
    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[logging.StreamHandler(sys.stdout)]
    )
    return logging.getLogger(__name__)


logger = setup_logging()


# ============================================================================
# 数据模型
# ============================================================================

@dataclass
class FileItem:
    path: Path
    text: str
    tokens: int

    def __hash__(self) -> int:
        return hash(self.path)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, FileItem):
            return self.path == other.path
        return NotImplemented


@dataclass
class BatchResult:
    success: bool
    failed_items: List[FileItem] = field(default_factory=list)
    quota_exceeded: bool = False
    budget_exceeded: bool = False


@dataclass
class SingleResult:
    success: bool
    failed_item: Optional[FileItem] = None
    quota_exceeded: bool = False
    budget_exceeded: bool = False


@dataclass
class Batch:
    items: List[FileItem]
    tokens: int


@dataclass
class ProcessingStats:
    total_processed: int = 0
    total_failed: int = 0
    total_skipped: int = 0
    prep_time: float = 0.0
    api_time: float = 0.0

    @property
    def total_time(self) -> float:
        return self.prep_time + self.api_time

    @property
    def average_rate(self) -> float:
        return self.total_processed / self.api_time if self.api_time > 0 else 0.0


# ============================================================================
# API Key 隐私保护（request_tracker.json 不落盘明文 key）
# ============================================================================

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


# ============================================================================
# Paid tier: monthly budget tracker (budget_tracker.json)
# ============================================================================

_BUDGET_TRACKER_VERSION = 1

# Default pricing (USD per 1M tokens):
# - input:  $0.50 / 1,000,000 tokens  => 500 nanos / token
# - output: $3.00 / 1,000,000 tokens  => 3000 nanos / token
_IN_NANOS_PER_TOKEN = 500
_OUT_NANOS_PER_TOKEN = 3000


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class BudgetReservation:
    key_id: str
    month: str  # YYYY-MM
    reserved_nanos_usd: int
    reserved_input_tokens: int
    reserved_output_tokens: int


class BudgetManager:
    """
    Tracks per-key monthly spend in nanos USD (integer) to avoid floating point issues.
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
        self._data: Dict[str, Any] = self._load()

    @staticmethod
    def month_key(today: Optional[date] = None) -> str:
        d = today or date.today()
        return d.strftime("%Y-%m")

    def _load(self) -> Dict[str, Any]:
        default = {"version": _BUDGET_TRACKER_VERSION, "months": {}}
        if not self._path.exists():
            return default
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return default
            if data.get("version") != _BUDGET_TRACKER_VERSION:
                # Best-effort forward compatibility.
                data["version"] = _BUDGET_TRACKER_VERSION
            months = data.get("months")
            if not isinstance(months, dict):
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

    def _get_entry(self, month: str, key_id: str) -> Dict[str, Any]:
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

    async def get_spent_nanos_usd(self, key_id: str, month: Optional[str] = None) -> int:
        async with self._lock:
            m = month or self.month_key()
            entry = self._get_entry(m, key_id)
            try:
                return int(entry.get("spent_nanos_usd", 0))
            except Exception:
                return 0

    async def try_reserve(
        self,
        *,
        key_id: str,
        budget_nanos_usd: int,
        estimated_input_tokens: int,
        max_output_tokens: int,
        month: Optional[str] = None,
        input_safety_factor: float = 1.10,
    ) -> Optional[BudgetReservation]:
        """
        Reserve worst-case cost for one API request:
        - input uses an estimate (with +10% safety)
        - output uses max_output_tokens (worst-case)

        This prevents concurrent requests from overshooting the monthly budget.
        """
        if budget_nanos_usd <= 0:
            return None

        m = month or self.month_key()
        safe_in = int(max(0, estimated_input_tokens) * input_safety_factor + 0.999)
        safe_out = int(max(0, max_output_tokens))
        reserved = safe_in * self._in_nanos_per_token + safe_out * self._out_nanos_per_token

        async with self._lock:
            entry = self._get_entry(m, key_id)
            spent = int(entry.get("spent_nanos_usd", 0) or 0)
            if spent + reserved > budget_nanos_usd:
                return None

            entry["spent_nanos_usd"] = spent + reserved
            entry["requests"] = int(entry.get("requests", 0) or 0) + 1
            entry["updated_at"] = _utc_now_iso()
            self._atomic_save()

        return BudgetReservation(
            key_id=key_id,
            month=m,
            reserved_nanos_usd=reserved,
            reserved_input_tokens=safe_in,
            reserved_output_tokens=safe_out,
        )

    async def commit(
        self,
        *,
        reservation: BudgetReservation,
        actual_input_tokens: int,
        actual_output_tokens: int,
    ) -> None:
        """
        Finalize a reservation using actual token usage. If usage metadata is unavailable,
        callers should pass a best-effort estimate.
        """
        in_tk = int(max(0, actual_input_tokens))
        out_tk = int(max(0, actual_output_tokens))
        actual = in_tk * self._in_nanos_per_token + out_tk * self._out_nanos_per_token

        async with self._lock:
            entry = self._get_entry(reservation.month, reservation.key_id)
            spent = int(entry.get("spent_nanos_usd", 0) or 0)

            # Adjust: spent already includes the reservation.
            spent = spent - int(reservation.reserved_nanos_usd) + actual
            entry["spent_nanos_usd"] = max(0, spent)

            entry["input_tokens"] = int(entry.get("input_tokens", 0) or 0) + in_tk
            entry["output_tokens"] = int(entry.get("output_tokens", 0) or 0) + out_tk
            entry["updated_at"] = _utc_now_iso()
            self._atomic_save()

    async def rollback(self, *, reservation: BudgetReservation) -> None:
        """
        Undo a reservation when the corresponding API call did not complete (e.g. network error).

        This keeps the budget tracker accurate under retries and prevents "budget leakage".
        """
        async with self._lock:
            entry = self._get_entry(reservation.month, reservation.key_id)
            spent = int(entry.get("spent_nanos_usd", 0) or 0)
            spent = spent - int(reservation.reserved_nanos_usd)
            entry["spent_nanos_usd"] = max(0, spent)

            req = int(entry.get("requests", 0) or 0)
            entry["requests"] = max(0, req - 1)

            entry["updated_at"] = _utc_now_iso()
            self._atomic_save()


# ============================================================================
# API 密钥管理
# ============================================================================

class APIKeyManager:
    def __init__(self, keys: List[str], tracker_file: Path):
        self._keys = keys
        self._tracker_file = tracker_file
        self._tracker = self._load_tracker()

    @staticmethod
    def _to_int(value: Any) -> int:
        try:
            return int(value)
        except Exception:
            return 0

    def _load_tracker(self) -> Dict[str, Any]:
        today_str = date.today().isoformat()
        default = {"date": today_str, "usage": {}}
        if not self._tracker_file.exists():
            return default
        try:
            with open(self._tracker_file, 'r', encoding='utf-8') as f:
                tracker = json.load(f)
            if not isinstance(tracker, dict):
                return default
            if tracker.get("date") != today_str:
                logger.info("新的一天，重置所有API密钥的每日请求计数器。")
                return default
            usage = tracker.get("usage")
            usage = usage if isinstance(usage, dict) else {}

            # Migration: legacy tracker stored raw API keys as dict keys. Migrate to key_id.
            migrated: Dict[str, int] = {}
            for k, v in usage.items():
                if k is None:
                    continue
                k_str = str(k)
                key_id = k_str if _looks_like_key_id(k_str) else _make_key_id(k_str)
                migrated[key_id] = migrated.get(key_id, 0) + self._to_int(v)
            tracker["usage"] = migrated
            return tracker
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"读取请求跟踪文件失败: {e}")
            return default

    def save_tracker(self) -> None:
        try:
            # Privacy: ensure we never persist raw API keys as usage keys.
            usage = self._tracker.get("usage", {})
            if not isinstance(usage, dict):
                usage = {}
            self._tracker["usage"] = {
                k: self._to_int(v) for k, v in usage.items() if _looks_like_key_id(k)
            }
            with open(self._tracker_file, 'w', encoding='utf-8') as f:
                json.dump(self._tracker, f, indent=4, ensure_ascii=False)
        except IOError as e:
            logger.error(f"保存请求跟踪文件失败: {e}")

    def get_usage(self, key: str) -> int:
        usage = self._tracker.get("usage", {})
        if not isinstance(usage, dict):
            usage = {}
            self._tracker["usage"] = usage

        key_id = _make_key_id(key)
        # Migration: old tracker may have stored raw API key as the dict key.
        if key in usage and not _looks_like_key_id(key):
            usage[key_id] = self._to_int(usage.get(key_id, 0)) + self._to_int(usage.get(key))
            usage.pop(key, None)
        return self._to_int(usage.get(key_id, 0))

    def increment_usage(self, key: str) -> None:
        usage = self._tracker.get("usage", {})
        if not isinstance(usage, dict):
            usage = {}
            self._tracker["usage"] = usage
        key_id = _make_key_id(key)
        # Ensure migration happens before increment.
        _ = self.get_usage(key)
        usage[key_id] = self._to_int(usage.get(key_id, 0)) + 1

    @property
    def keys(self) -> List[str]:
        return self._keys

    @property
    def count(self) -> int:
        return len(self._keys)

    def get_remaining_quota(self, key: str, daily_limit: int) -> int:
        return daily_limit - self.get_usage(key)


def configure_api_keys() -> List[str]:
    keys_str = os.getenv("GOOGLE_API_KEY")
    if not keys_str:
        print("-" * 65)
        print("未找到 GOOGLE_API_KEY 环境变量。")
        keys_str = input("请输入您的一个或多个 Google API 密钥 (若有多个，请用逗号','分隔):\n").strip()
        print("-" * 65)
    if not keys_str:
        logger.error("错误：未提供任何 API 密钥，程序即将退出。")
        sys.exit(1)
    api_keys = [key.strip() for key in keys_str.split(',') if key.strip()]
    if not api_keys:
        logger.error("错误：提供的 API 密钥为空，程序即将退出。")
        sys.exit(1)
    logger.info(f"找到 {len(api_keys)} 个 API 密钥。")
    return api_keys


# ============================================================================
# Gemini 模型包装器
# ============================================================================

class GeminiModel:
    DEFAULT_MODEL_NAME = 'models/gemini-2.5-flash'

    def __init__(self):
        self._model: Optional[genai.GenerativeModel] = None
        self._api_key: Optional[str] = None
        self._model_name: str = self.DEFAULT_MODEL_NAME

    @property
    def api_key(self) -> Optional[str]:
        return self._api_key

    @property
    def is_configured(self) -> bool:
        return self._model is not None

    @property
    def model_name(self) -> str:
        return self._model_name

    def configure(self, api_key: str, model_name: Optional[str] = None) -> bool:
        try:
            genai.configure(api_key=api_key)
            if model_name:
                self._model_name = model_name
            self._model = genai.GenerativeModel(self._model_name)
            self._api_key = api_key
            logger.info(
                f"API 密钥 (id: {_make_key_id(api_key)}) 配置成功。模型: {self._model_name}"
            )
            return True
        except Exception as e:
            logger.error(f"API 密钥配置失败: {e}")
            return False

    @staticmethod
    def _extract_usage_tokens(response: object) -> Tuple[Optional[int], Optional[int]]:
        """
        Best-effort extraction of (prompt_tokens, candidates_tokens) from google-generativeai responses.
        Returns (None, None) if unavailable.
        """
        um = getattr(response, "usage_metadata", None)
        if um is None:
            return None, None

        def _get(obj: object, key: str) -> Optional[int]:
            try:
                if isinstance(obj, dict):
                    v = obj.get(key)
                else:
                    v = getattr(obj, key, None)
                if v is None:
                    return None
                return int(v)
            except Exception:
                return None

        prompt = _get(um, "prompt_token_count")
        candidates = _get(um, "candidates_token_count")
        return prompt, candidates

    async def generate_content(
        self,
        prompt: str,
        schema: Dict[str, Any],
        *,
        max_output_tokens: Optional[int] = None,
    ) -> Tuple[Optional[str], Dict[str, int]]:
        """
        异步生成内容。
        [FIX] 增加 response.parts 空值检查，返回 None 表示被安全过滤。
        """
        if not self._model:
            raise RuntimeError("模型尚未配置")
        config = {
            "response_mime_type": "application/json",
            "response_schema": schema,
        }
        if max_output_tokens is not None:
            config["max_output_tokens"] = int(max_output_tokens)
        response = await self._model.generate_content_async(prompt, generation_config=config)

        prompt_tk, cand_tk = self._extract_usage_tokens(response)
        usage = {
            "prompt_tokens": int(prompt_tk) if prompt_tk is not None else 0,
            "candidates_tokens": int(cand_tk) if cand_tk is not None else 0,
        }

        # [FIX] 检查响应是否被内容安全策略过滤
        if not response.parts:
            return None, usage

        return response.text, usage


MODEL = GeminiModel()


# ============================================================================
# 速率限制器
# ============================================================================

class RateLimiter:
    def __init__(self, rpm: int, tpm: int):
        self._rpm = rpm
        self._tpm = tpm
        self._request_timestamps: Deque[float] = deque()
        self._token_records: Deque[Tuple[float, int]] = deque()
        self._token_total = 0
        self._lock = asyncio.Lock()

    def _cleanup_old_records(self, now: float) -> None:
        cutoff = now - 60
        while self._request_timestamps and self._request_timestamps[0] < cutoff:
            self._request_timestamps.popleft()
        while self._token_records and self._token_records[0][0] < cutoff:
            _, tokens = self._token_records.popleft()
            self._token_total -= tokens

    def _calculate_wait_time(self, now: float, tokens_needed: int) -> float:
        rpm_wait = 0.0
        tpm_wait = 0.0
        if len(self._request_timestamps) >= self._rpm and self._request_timestamps:
            rpm_wait = (self._request_timestamps[0] + 60) - now
        if (self._token_total + tokens_needed) > self._tpm and self._token_records:
            tokens_to_free = (self._token_total + tokens_needed) - self._tpm
            freed = 0
            wait_until = 0.0
            for ts, tk in self._token_records:
                freed += tk
                if freed >= tokens_to_free:
                    wait_until = ts
                    break
            if wait_until > 0:
                tpm_wait = (wait_until + 60) - now
        return max(0.1, rpm_wait, tpm_wait)

    async def acquire(self, tokens_needed: int) -> None:
        async with self._lock:
            while True:
                now = time.time()
                self._cleanup_old_records(now)
                can_request = len(self._request_timestamps) < self._rpm
                can_tokens = (self._token_total + tokens_needed) <= self._tpm
                if can_request and can_tokens:
                    self._request_timestamps.append(now)
                    self._token_records.append((now, tokens_needed))
                    self._token_total += tokens_needed
                    return
                wait_time = self._calculate_wait_time(now, tokens_needed)
                logger.info(f"速率限制，等待 {wait_time:.2f} 秒...")
                await asyncio.sleep(wait_time)


# ============================================================================
# 文本提取器
# ============================================================================

class TextExtractor(ABC):
    @abstractmethod
    def extract(self, path: Path) -> str:
        pass


class PDFExtractor(TextExtractor):
    def __init__(self, pages_start: int = 4, pages_end: int = 3):
        self._pages_start = pages_start
        self._pages_end = pages_end

    def extract(self, path: Path) -> str:
        try:
            with pymupdf.open(path) as doc:
                total = doc.page_count
                pages = set(range(min(self._pages_start, total)))
                if total > self._pages_start + self._pages_end:
                    pages.update(range(total - self._pages_end, total))
                texts = [doc[i].get_text(sort=True) for i in sorted(pages)]
                return "\n".join(texts)
        except Exception as e:
            logger.error(f"PDF 提取失败 {path.name}: {e}")
            return ""


class EPUBExtractor(TextExtractor):
    def __init__(self, chapters_start: int = 5, chapters_end: int = 4):
        self._chapters_start = chapters_start
        self._chapters_end = chapters_end

    def extract(self, path: Path) -> str:
        try:
            book = epub.read_epub(path)
            items = list(book.get_items_of_type(ITEM_DOCUMENT))
            to_process = items[:self._chapters_start]
            if len(items) > self._chapters_start + self._chapters_end:
                to_process.extend(items[-self._chapters_end:])
            texts = []
            for item in to_process:
                soup = BeautifulSoup(item.get_body_content(), 'html.parser')
                texts.append(soup.get_text("\n", strip=True))
            return "\n\n".join(texts)
        except Exception as e:
            logger.error(f"EPUB 提取失败 {path.name}: {e}")
            return ""


class DOCXExtractor(TextExtractor):
    def __init__(self, paras_start: int = 20, paras_end: int = 15):
        self._paras_start = paras_start
        self._paras_end = paras_end

    def extract(self, path: Path) -> str:
        try:
            doc = Document(path)
            paras = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
            if len(paras) > self._paras_start + self._paras_end:
                result = paras[:self._paras_start] + paras[-self._paras_end:]
            else:
                result = paras
            return "\n".join(result)
        except Exception as e:
            logger.error(f"DOCX 提取失败 {path.name}: {e}")
            return ""


class TextExtractorFactory:
    _extractors: Dict[str, TextExtractor] = {
        '.pdf': PDFExtractor(),
        '.epub': EPUBExtractor(),
        '.azw3': EPUBExtractor(),
        '.docx': DOCXExtractor(),
    }

    @classmethod
    def get_extractor(cls, extension: str) -> Optional[TextExtractor]:
        return cls._extractors.get(extension.lower())


def smart_truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    head_chars = int(max_chars * 0.6)
    tail_chars = int(max_chars * 0.4)
    return f"{text[:head_chars]}\n\n--- 内容已截断 ---\n\n{text[-tail_chars:]}"


def estimate_tokens(text: str, chars_per_token: float = 3.5) -> int:
    """
    [FIX] 本地估算 token 数量，不再调用 API。
    原来的 MODEL.count_tokens() 是一个真实的 API 调用，
    对每个文件都发一次网络请求，极其缓慢且浪费资源。
    """
    if not text:
        return 0
    return max(1, int(len(text) / chars_per_token))


def extract_text(path: Path, max_chars: int) -> Optional[str]:
    extractor = TextExtractorFactory.get_extractor(path.suffix)
    if not extractor:
        logger.warning(f"不支持的文件类型: {path.name}")
        return None
    text = extractor.extract(path)
    if not text:
        return None
    return smart_truncate(text, max_chars)


def extract_and_create_item(path: Path, config: Config) -> Optional[FileItem]:
    """
    [FIX] 重命名自 extract_and_count，使用本地 token 估算。
    不再需要 MODEL 已配置。纯本地操作，线程安全。
    """
    text = extract_text(path, config.max_chars_per_request)
    if not text:
        return None
    tokens = estimate_tokens(text, config.chars_per_token)
    return FileItem(path=path, text=text, tokens=tokens)


# ============================================================================
# 元数据处理
# ============================================================================

JOURNAL_KEYWORDS = frozenset([
    "journal", "review", "proceedings", "transactions", "quarterly",
    "annals", "bulletin", "magazine", "advances", "letters", "studies",
    "science", "research", "technology", "medicine", "report", "archives",
    "学报", "法学", "研究", "评论", "科学", "技术", "杂志", "动态",
    "报告", "医学", "经济", "哲学", "历史", "通讯", "汇刊", "纪要"
])

UNKNOWN_AUTHOR_MARKERS = frozenset(["作者不详"])
ROLE_INVALID_TOKENS = frozenset(["null", "none", "n/a", "unknown", "不详", "未知"])
ROLE_INVALID_SUBSTRINGS = frozenset(["无法提取", "不明确", "系统返回null", "系统返回 null"])


class TextNormalizer:
    @staticmethod
    def normalize(value: Any) -> str:
        if value is None:
            return ""
        text = str(value).strip()
        return "" if not text or text.lower() == "null" else text

    @staticmethod
    def normalize_list(values: Optional[List[Any]]) -> List[str]:
        if not values:
            return []
        return [v for v in map(TextNormalizer.normalize, values) if v]

    @staticmethod
    def normalize_authors(values: Optional[List[Any]]) -> List[str]:
        authors = TextNormalizer.normalize_list(values)
        return [a for a in authors if a not in UNKNOWN_AUTHOR_MARKERS]

    @staticmethod
    def normalize_role(value: Any) -> str:
        normalized = TextNormalizer.normalize(value)
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


class MetadataBuilder:
    def __init__(self, info: Dict[str, Any]):
        self._info = info
        self._n = TextNormalizer

    @property
    def title(self) -> str:
        return self._n.normalize(self._info.get("title"))

    @property
    def authors(self) -> List[str]:
        return self._n.normalize_authors(self._info.get("authors"))

    @property
    def authors_str(self) -> str:
        return "、".join(self.authors)

    @property
    def keywords(self) -> List[str]:
        return self._n.normalize_list(self._info.get("keywords"))

    @property
    def keywords_str(self) -> str:
        return ", ".join(self.keywords)

    @property
    def translators(self) -> str:
        return self._n.normalize_role(self._info.get("translators"))

    @property
    def editors(self) -> str:
        return self._n.normalize_role(self._info.get("editors"))

    @property
    def publisher(self) -> str:
        return self._n.normalize(self._info.get("publisher_or_journal"))

    def build_details_string(self) -> str:
        details = []
        mappings = [
            ("出版/期刊", self.publisher),
            ("卷期", self._n.normalize(self._info.get("journal_volume_issue"))),
            ("日期", self._n.normalize(self._info.get("publication_date"))),
            ("编者", self.editors),
            ("译者", self.translators),
            ("页码", self._n.normalize(self._info.get("start_page"))),
        ]
        for label, value in mappings:
            if value:
                details.append(f"{label}: {value}")
        return " | ".join(details)

    def build_filename(self) -> Optional[str]:
        if not self.title:
            return None
        main_part = f"{self.title} - {self.authors_str}" if self.authors_str else self.title
        extras = []
        if self.translators:
            extras.append(f"{self.translators} 译")
        if self.editors and not self.authors:
            pub_lower = self.publisher.lower()
            if not any(k in pub_lower for k in JOURNAL_KEYWORDS):
                extras.append(f"{self.editors} 编")
        return f"{main_part} ({', '.join(extras)})" if extras else main_part


# ============================================================================
# 元数据写入器
# ============================================================================

class MetadataWriter(ABC):
    @abstractmethod
    def write(self, path: Path, builder: MetadataBuilder) -> None:
        pass


class PDFMetadataWriter(MetadataWriter):
    def write(self, path: Path, builder: MetadataBuilder) -> None:
        try:
            with pymupdf.open(path) as doc:
                metadata = doc.metadata
                metadata['title'] = builder.title
                metadata['author'] = builder.authors_str
                metadata['subject'] = builder.build_details_string()
                metadata['keywords'] = builder.keywords_str
                doc.set_metadata(metadata)
                doc.save(doc.name, incremental=True, encryption=pymupdf.PDF_ENCRYPT_KEEP)
            logger.info(f"PDF 元数据写入成功: {path.name}")
        except Exception as e:
            logger.error(f"PDF 元数据写入失败 {path.name}: {e}")


class DOCXMetadataWriter(MetadataWriter):
    def write(self, path: Path, builder: MetadataBuilder) -> None:
        try:
            doc = Document(path)
            cp = doc.core_properties
            cp.title = builder.title
            cp.author = builder.authors_str
            cp.subject = builder.build_details_string()
            cp.keywords = builder.keywords_str
            cp.comments = "Metadata updated by Gemini File Renamer"
            doc.save(path)
            logger.info(f"DOCX 元数据写入成功: {path.name}")
        except Exception as e:
            logger.error(f"DOCX 元数据写入失败 {path.name}: {e}")


class EPUBMetadataWriter(MetadataWriter):
    def _clear_creators(self, book: epub.EpubBook) -> None:
        """
        [FIX] 更健壮的作者清除逻辑。
        ebooklib 内部结构在不同版本中可能不同，需要安全处理。
        """
        namespace = "http://purl.org/dc/elements/1.1/"
        try:
            meta = book.metadata.get(namespace)
            if meta is None:
                return

            if isinstance(meta, dict):
                meta.pop("creator", None)
            elif isinstance(meta, list):
                book.metadata[namespace] = [
                    item for item in meta
                    if not (isinstance(item, (tuple, list)) and len(item) > 0 and item[0] == "creator")
                ]
        except Exception as e:
            logger.warning(f"清除 EPUB 作者信息时出错（非致命）: {e}")

    def write(self, path: Path, builder: MetadataBuilder) -> None:
        try:
            book = epub.read_epub(path)
            book.set_title(builder.title)
            self._clear_creators(book)
            for author in builder.authors:
                book.add_author(author)
            description_parts = []
            details = builder.build_details_string()
            if details:
                description_parts.append(details)
            if builder.keywords:
                description_parts.append(f"Keywords: {builder.keywords_str}")
            if description_parts:
                book.add_metadata('DC', 'description', "\n".join(description_parts))
            epub.write_epub(path, book)
            logger.info(f"EPUB 元数据写入成功: {path.name}")
        except Exception as e:
            logger.error(f"EPUB 元数据写入失败 {path.name}: {e}")


class MetadataWriterFactory:
    _writers: Dict[str, MetadataWriter] = {
        '.pdf': PDFMetadataWriter(),
        '.docx': DOCXMetadataWriter(),
        '.epub': EPUBMetadataWriter(),
        '.azw3': EPUBMetadataWriter(),
    }

    @classmethod
    def get_writer(cls, extension: str) -> Optional[MetadataWriter]:
        return cls._writers.get(extension.lower())


# ============================================================================
# 文件重命名器
# ============================================================================

class FileRenamer:
    def __init__(self, write_metadata: bool = True):
        self._write_metadata = write_metadata
        self._executor = ThreadPoolExecutor(max_workers=4)

    async def process(self, path: Path, info: Dict[str, Any]) -> None:
        builder = MetadataBuilder(info)
        new_name = builder.build_filename()
        if not new_name:
            logger.warning(f"无法构建文件名: {path.name}")
            return
        safe_name = sanitize_filename(new_name).strip()
        if not safe_name or safe_name in {".", ".."}:
            logger.warning(f"非法文件名: {new_name}")
            return
        new_path = path.with_name(f"{safe_name}{path.suffix}")
        counter = 1
        while new_path.exists() and new_path != path:
            new_path = path.with_name(f"{safe_name}_{counter}{path.suffix}")
            counter += 1
        if new_path != path:
            try:
                path.rename(new_path)
                logger.info(f"重命名: {path.name} -> {new_path.name}")
            except OSError as e:
                logger.error(f"重命名失败 {path.name}: {e}")
                return
        else:
            # 文件名没变，仍然写入元数据
            new_path = path

        if self._write_metadata:
            await self._write_metadata_async(new_path, builder)

    async def _write_metadata_async(self, path: Path, builder: MetadataBuilder) -> None:
        writer = MetadataWriterFactory.get_writer(path.suffix)
        if not writer:
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._executor, writer.write, path, builder)


# ============================================================================
# 批处理打包器
# ============================================================================

def pack_batches_ffd(
    items: List[FileItem],
    max_tokens: int,
    max_items: Optional[int] = None
) -> List[Batch]:
    sorted_items = sorted(items, key=lambda x: x.tokens, reverse=True)
    batches: List[Batch] = []
    for item in sorted_items:
        # [FIX] 跳过超大文件（不应出现，但做防御性检查）
        if item.tokens > max_tokens:
            logger.warning(f"文件 token 数 ({item.tokens}) 超过单批上限 ({max_tokens})，跳过: {item.path.name}")
            continue
        placed = False
        for batch in batches:
            can_fit_tokens = batch.tokens + item.tokens <= max_tokens
            can_fit_items = max_items is None or len(batch.items) < max_items
            if can_fit_tokens and can_fit_items:
                batch.items.append(item)
                batch.tokens += item.tokens
                placed = True
                break
        if not placed:
            batches.append(Batch(items=[item], tokens=item.tokens))
    return batches


# ============================================================================
# 断点续传
# ============================================================================

class PendingFilesManager:
    def __init__(self, log_path: Path):
        self._log_path = log_path

    def load(self) -> List[Path]:
        if not self._log_path.exists():
            return []
        try:
            with open(self._log_path, 'r', encoding='utf-8') as f:
                return [Path(line.strip()) for line in f if line.strip()]
        except IOError:
            return []

    def save(self, paths: Sequence[Path]) -> None:
        try:
            with open(self._log_path, 'w', encoding='utf-8') as f:
                for path in paths:
                    f.write(f"{path}\n")
        except IOError as e:
            logger.error(f"保存待处理文件日志失败: {e}")

    def clear(self) -> None:
        if self._log_path.exists():
            try:
                self._log_path.unlink()
                logger.info("待处理文件日志已清空。")
            except OSError as e:
                logger.error(f"清空日志失败: {e}")


# ============================================================================
# 错误处理
# ============================================================================

def is_quota_error(error: Exception) -> bool:
    msg = str(error).lower()
    return any(keyword in msg for keyword in ("quota", "exceeded", "429", "resource_exhausted"))


# ============================================================================
# 处理器
# ============================================================================

@dataclass(frozen=True)
class PaidBudgetContext:
    budget_manager: BudgetManager
    monthly_budget_nanos_usd: int
    key_id: str


class FileProcessor:
    def __init__(self, config: Config, limiter: RateLimiter, renamer: FileRenamer):
        self._config = config
        self._limiter = limiter
        self._renamer = renamer

    async def process_batch(
        self,
        batch: Batch,
        pbar: tqdm,
        *,
        paid_ctx: Optional[PaidBudgetContext] = None,
    ) -> BatchResult:
        if not batch.items or not MODEL.is_configured:
            pbar.update(len(batch.items))
            return BatchResult(success=False, failed_items=list(batch.items))

        parts = [PROMPTS['batch']]
        for item in batch.items:
            parts.extend([
                f"\n\n--- START OF FILE: {item.path.name} ---\n",
                item.text,
                f"\n--- END OF FILE: {item.path.name} ---"
            ])
        prompt = "".join(parts)
        prompt_tokens_est = estimate_tokens(prompt, self._config.chars_per_token)

        for attempt in range(self._config.max_retries):
            reservation: Optional[BudgetReservation] = None
            committed = False
            try:
                if paid_ctx is not None:
                    max_out = int(self._config.max_output_tokens)
                    max_ctx = int(self._config.max_context_tokens)
                    margin = int(self._config.budget_safety_margin_tokens)
                    if prompt_tokens_est + max_out + margin > max_ctx:
                        logger.warning(
                            f"批次输入过大，可能超过 {max_ctx} tokens 上限，跳过该批次。"
                        )
                        pbar.update(len(batch.items))
                        return BatchResult(success=False, failed_items=list(batch.items))

                    reservation = await paid_ctx.budget_manager.try_reserve(
                        key_id=paid_ctx.key_id,
                        budget_nanos_usd=paid_ctx.monthly_budget_nanos_usd,
                        estimated_input_tokens=prompt_tokens_est,
                        max_output_tokens=max_out,
                    )
                    if reservation is None:
                        logger.warning(
                            "付费预算不足（本 key 本月已达到上限），将把文件留到后续阶段处理。"
                        )
                        pbar.update(len(batch.items))
                        return BatchResult(
                            success=False,
                            failed_items=list(batch.items),
                            budget_exceeded=True,
                        )

                await self._limiter.acquire(prompt_tokens_est)

                response_text, usage = await MODEL.generate_content(
                    prompt,
                    BATCH_SCHEMA,
                    max_output_tokens=self._config.max_output_tokens,
                )

                if paid_ctx is not None and reservation is not None:
                    in_tk = usage.get("prompt_tokens") or prompt_tokens_est
                    out_tk = usage.get("candidates_tokens")
                    if (out_tk is None or out_tk <= 0) and response_text:
                        out_tk = estimate_tokens(response_text, self._config.chars_per_token)
                    out_tk = int(out_tk or 0)
                    await paid_ctx.budget_manager.commit(
                        reservation=reservation,
                        actual_input_tokens=in_tk,
                        actual_output_tokens=out_tk,
                    )
                    committed = True

                # [FIX] 检查安全过滤
                if response_text is None:
                    logger.warning(f"批次被内容安全策略过滤，跳过 {len(batch.items)} 个文件。")
                    pbar.update(len(batch.items))
                    return BatchResult(success=False, failed_items=list(batch.items))

                results = json.loads(response_text)

                if not isinstance(results, list) or len(results) != len(batch.items):
                    logger.warning(
                        f"批处理结果数不匹配: 预期 {len(batch.items)}, 得到 "
                        f"{len(results) if isinstance(results, list) else 'non-list'}"
                    )
                    pbar.update(len(batch.items))
                    return BatchResult(success=False, failed_items=list(batch.items))

                for item, info in zip(batch.items, results):
                    await self._renamer.process(item.path, info)

                pbar.update(len(batch.items))
                return BatchResult(success=True)

            except json.JSONDecodeError as e:
                logger.error(f"JSON 解析失败: {e}")
                break
            except Exception as e:
                if paid_ctx is not None and reservation is not None and not committed:
                    try:
                        await paid_ctx.budget_manager.rollback(reservation=reservation)
                    except Exception:
                        # Best-effort rollback; do not hide the original error.
                        pass
                logger.error(f"批处理错误 (尝试 {attempt + 1}): {e}")
                if is_quota_error(e):
                    logger.warning("配额已用尽")
                    return BatchResult(success=False, failed_items=list(batch.items), quota_exceeded=True)
                if attempt < self._config.max_retries - 1:
                    await asyncio.sleep(2 ** (attempt + 1))

        pbar.update(len(batch.items))
        return BatchResult(success=False, failed_items=list(batch.items))

    async def process_single(
        self,
        item: FileItem,
        pbar: tqdm,
        *,
        paid_ctx: Optional[PaidBudgetContext] = None,
    ) -> SingleResult:
        if not MODEL.is_configured:
            pbar.update(1)
            return SingleResult(success=False, failed_item=item)

        prompt = f"{PROMPTS['single']}\n\n{item.text}"
        prompt_tokens_est = estimate_tokens(prompt, self._config.chars_per_token)

        for attempt in range(self._config.max_retries):
            reservation: Optional[BudgetReservation] = None
            committed = False
            try:
                if paid_ctx is not None:
                    max_out = int(self._config.max_output_tokens)
                    max_ctx = int(self._config.max_context_tokens)
                    margin = int(self._config.budget_safety_margin_tokens)
                    if prompt_tokens_est + max_out + margin > max_ctx:
                        logger.warning(
                            f"文件 '{item.path.name}' 输入过大，可能超过 {max_ctx} tokens 上限，跳过。"
                        )
                        pbar.update(1)
                        return SingleResult(success=False, failed_item=item)

                    reservation = await paid_ctx.budget_manager.try_reserve(
                        key_id=paid_ctx.key_id,
                        budget_nanos_usd=paid_ctx.monthly_budget_nanos_usd,
                        estimated_input_tokens=prompt_tokens_est,
                        max_output_tokens=max_out,
                    )
                    if reservation is None:
                        logger.warning(
                            "付费预算不足（本 key 本月已达到上限），将把文件留到后续阶段处理。"
                        )
                        pbar.update(1)
                        return SingleResult(
                            success=False,
                            failed_item=item,
                            budget_exceeded=True,
                        )

                await self._limiter.acquire(prompt_tokens_est)

                response_text, usage = await MODEL.generate_content(
                    prompt,
                    SINGLE_OBJECT_SCHEMA,
                    max_output_tokens=self._config.max_output_tokens,
                )

                if paid_ctx is not None and reservation is not None:
                    in_tk = usage.get("prompt_tokens") or prompt_tokens_est
                    out_tk = usage.get("candidates_tokens")
                    if (out_tk is None or out_tk <= 0) and response_text:
                        out_tk = estimate_tokens(response_text, self._config.chars_per_token)
                    out_tk = int(out_tk or 0)
                    await paid_ctx.budget_manager.commit(
                        reservation=reservation,
                        actual_input_tokens=in_tk,
                        actual_output_tokens=out_tk,
                    )
                    committed = True

                # [FIX] 检查安全过滤
                if response_text is None:
                    logger.warning(f"文件 '{item.path.name}' 被内容安全策略过滤，跳过。")
                    pbar.update(1)
                    return SingleResult(success=False, failed_item=item)

                info = json.loads(response_text)
                await self._renamer.process(item.path, info)
                pbar.update(1)
                return SingleResult(success=True)

            except json.JSONDecodeError as e:
                logger.error(f"JSON 解析失败 {item.path.name}: {e}")
                break
            except Exception as e:
                if paid_ctx is not None and reservation is not None and not committed:
                    try:
                        await paid_ctx.budget_manager.rollback(reservation=reservation)
                    except Exception:
                        pass
                logger.error(f"处理错误 {item.path.name} (尝试 {attempt + 1}): {e}")
                if is_quota_error(e):
                    return SingleResult(success=False, failed_item=item, quota_exceeded=True)
                if attempt < self._config.max_retries - 1:
                    await asyncio.sleep(2 ** (attempt + 1))

        pbar.update(1)
        return SingleResult(success=False, failed_item=item)


# ============================================================================
# 主应用
# ============================================================================

class Application:
    def __init__(
        self,
        config: Config,
        mode: ProcessingMode,
        write_metadata: bool,
        target_dir: Path,
        *,
        auto_single_threshold: int = 30,
        auto_economy_single_threshold: int = 3,
        paid_economy_mode: bool = False,
    ):
        self._config = config
        self._mode = mode
        self._write_metadata = write_metadata
        self._target_dir = target_dir
        self._auto_single_threshold = max(1, int(auto_single_threshold))
        self._auto_economy_single_threshold = max(1, int(auto_economy_single_threshold))
        self._paid_economy_mode = bool(paid_economy_mode)

        self._key_manager: Optional[APIKeyManager] = None
        self._pending_manager = PendingFilesManager(config.pending_files_log)
        self._limiter = RateLimiter(config.rpm_limit, config.tpm_limit)
        self._renamer = FileRenamer(write_metadata)
        self._processor = FileProcessor(config, self._limiter, self._renamer)
        self._stats = ProcessingStats()

    async def run(
        self,
        *,
        tier: str = "free",
        free_model: str = GeminiModel.DEFAULT_MODEL_NAME,
        paid_model: str = "gemini-3-flash-preview",
        paid_config: Optional[Config] = None,
        monthly_budget_usd: float = 10.0,
        on_budget_exceeded: str = "downgrade",
        budget_manager: Optional[BudgetManager] = None,
    ) -> None:
        api_keys = configure_api_keys()
        self._key_manager = APIKeyManager(api_keys, self._config.tracker_file)

        logger.info(f"运行模式: {'批处理' if self._mode == ProcessingMode.BATCH else '单文件'}")
        logger.info(f"元数据写入: {'开启' if self._write_metadata else '关闭'}")

        if self._target_dir.exists() and not self._target_dir.is_dir():
            logger.error(f"目标路径不是目录: {self._target_dir}")
            return
        if not self._target_dir.is_dir():
            self._target_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"已创建目录: {self._target_dir}")

        file_paths = self._get_files_to_process()
        if not file_paths:
            logger.info("没有待处理的文件。")
            return

        prep_start = time.time()
        file_items = await self._prepare_files(file_paths)
        self._stats.prep_time = time.time() - prep_start

        if not file_items:
            logger.warning("未能提取任何文件内容。")
            return

        api_start = time.time()
        remaining: List[FileItem]
        if tier == "paid":
            if paid_config is None:
                raise ValueError("paid_config is required when tier=paid")

            monthly_budget_nanos = int(round(float(monthly_budget_usd) * 1_000_000_000))
            if budget_manager is None:
                budget_manager = BudgetManager(paid_config.budget_file)

            logger.info("\n=== 付费阶段: Gemini 3 Flash ===")
            remaining = await self._process_files(
                file_items,
                config=paid_config,
                model_name=paid_model,
                budget_manager=budget_manager,
                monthly_budget_nanos_usd=monthly_budget_nanos,
            )

            if remaining and on_budget_exceeded == "downgrade":
                logger.info("\n=== 降级阶段: 免费模型继续处理 ===")
                remaining = await self._process_files(
                    remaining,
                    config=self._config,
                    model_name=free_model,
                )
            elif remaining and on_budget_exceeded == "stop":
                logger.warning(
                    "付费阶段未处理完且已设置 --on-budget-exceeded=stop，将停止并保留 pending。"
                )
        else:
            remaining = await self._process_files(
                file_items,
                config=self._config,
                model_name=free_model,
            )
        self._stats.api_time = time.time() - api_start

        if remaining:
            self._pending_manager.save([item.path for item in remaining])
            logger.warning(f"剩余 {len(remaining)} 个文件未处理。")
        else:
            self._pending_manager.clear()

        self._print_summary()

    def _get_files_to_process(self) -> List[Path]:
        pending = self._pending_manager.load()
        if pending:
            existing = [p for p in pending if p.exists()]
            logger.info(f"从断点恢复: {len(existing)} 个文件")
            return existing

        logger.info("扫描目录...")
        paths = []
        for ext in self._config.supported_extensions:
            paths.extend(self._target_dir.glob(f"**/*{ext}"))
        return sorted(set(paths), key=str)

    async def _prepare_files(self, paths: List[Path]) -> List[FileItem]:
        """
        [FIX] 不再需要先配置 MODEL 来计数 token。
        使用纯本地的文本提取和 token 估算。
        """
        # 仍然需要配置 MODEL 用于后续 API 调用
        for key in self._key_manager.keys:
            if MODEL.configure(key):
                break
        else:
            logger.error("所有 API 密钥均无效。")
            return []

        logger.info(f"提取文本和估算 token (并发={self._config.io_workers})...")

        loop = asyncio.get_running_loop()
        items = []

        with ThreadPoolExecutor(max_workers=self._config.io_workers) as pool:
            tasks = [
                loop.run_in_executor(pool, extract_and_create_item, p, self._config)
                for p in paths
            ]
            results = await asyncio.gather(*tasks)

        for result in results:
            if result and result.tokens <= self._config.max_tokens_per_request:
                items.append(result)
            elif result:
                logger.warning(f"文件过大，跳过: {result.path.name}")
                self._stats.total_skipped += 1

        logger.info(f"准备完成: {len(items)} 个文件")
        return items

    async def _process_files(
        self,
        items: List[FileItem],
        *,
        config: Config,
        model_name: str,
        budget_manager: Optional[BudgetManager] = None,
        monthly_budget_nanos_usd: Optional[int] = None,
    ) -> List[FileItem]:
        limiter = RateLimiter(config.rpm_limit, config.tpm_limit)
        processor = FileProcessor(config, limiter, self._renamer)
        remaining: Deque[FileItem] = deque(items)

        for key_index, api_key in enumerate(self._key_manager.keys):
            if not remaining:
                break

            logger.info(f"\n--- 第 {key_index + 1}/{self._key_manager.count} 遍 ---")
            logger.info(f"待处理: {len(remaining)} 个文件")

            if not MODEL.configure(api_key, model_name=model_name):
                continue

            quota = self._key_manager.get_remaining_quota(api_key, config.daily_request_limit)
            if quota <= 0:
                logger.warning("配额已用尽，跳过此密钥。")
                continue

            logger.info(f"剩余配额: {quota}")

            current_items = list(remaining)
            remaining = deque()

            paid_ctx: Optional[PaidBudgetContext] = None
            if budget_manager is not None and monthly_budget_nanos_usd is not None:
                paid_ctx = PaidBudgetContext(
                    budget_manager=budget_manager,
                    monthly_budget_nanos_usd=int(monthly_budget_nanos_usd),
                    key_id=_make_key_id(api_key),
                )

            with tqdm(total=len(current_items), desc=f"密钥 #{key_index + 1}", unit="file") as pbar:
                run_mode = self._mode
                if run_mode == ProcessingMode.AUTO:
                    economy = bool(self._paid_economy_mode and paid_ctx is not None)
                    threshold = self._auto_economy_single_threshold if economy else self._auto_single_threshold
                    if len(current_items) <= threshold and quota >= len(current_items):
                        run_mode = ProcessingMode.SINGLE
                    else:
                        run_mode = ProcessingMode.BATCH
                    logger.info(
                        f"自动模式选择: {'单文件并发' if run_mode == ProcessingMode.SINGLE else '批处理'} "
                        f"(files={len(current_items)}, quota={quota}, threshold={threshold}, economy={economy})"
                    )

                if run_mode == ProcessingMode.BATCH:
                    remaining = await self._process_batch_mode(
                        current_items,
                        quota,
                        pbar,
                        api_key,
                        config=config,
                        processor=processor,
                        paid_ctx=paid_ctx,
                    )
                else:
                    remaining = await self._process_single_mode(
                        current_items,
                        quota,
                        pbar,
                        api_key,
                        config=config,
                        processor=processor,
                        paid_ctx=paid_ctx,
                    )

            self._key_manager.save_tracker()

        return list(remaining)

    async def _process_batch_mode(
        self,
        items: List[FileItem],
        quota: int,
        pbar: tqdm,
        api_key: str,
        *,
        config: Config,
        processor: FileProcessor,
        paid_ctx: Optional[PaidBudgetContext],
    ) -> Deque[FileItem]:
        batches = pack_batches_ffd(
            items, config.max_tokens_per_request, config.max_items_per_batch
        )

        remaining: Deque[FileItem] = deque()

        batches_to_process = batches[:quota]
        leftover_batches = batches[quota:]

        for batch in leftover_batches:
            remaining.extend(batch.items)

        # [FIX] 使用 semaphore 限制并发，而非逐个顺序执行
        semaphore = asyncio.Semaphore(config.concurrency_limit)

        async def process_with_semaphore(batch: Batch) -> BatchResult:
            async with semaphore:
                result = await processor.process_batch(batch, pbar, paid_ctx=paid_ctx)
                if not result.budget_exceeded:
                    self._key_manager.increment_usage(api_key)
                return result

        tasks = [process_with_semaphore(b) for b in batches_to_process]
        # [FIX] return_exceptions=True 防止单个失败导致整体崩溃
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"批次 {i} 发生异常: {result}")
                remaining.extend(batches_to_process[i].items)
                self._stats.total_failed += len(batches_to_process[i].items)
                if is_quota_error(result):
                    # 将后续未处理的批次加入剩余
                    for j in range(i + 1, len(batches_to_process)):
                        remaining.extend(batches_to_process[j].items)
                    break
            elif result.success:
                self._stats.total_processed += len(batches_to_process[i].items)
            else:
                remaining.extend(result.failed_items)
                if not result.budget_exceeded:
                    self._stats.total_failed += len(result.failed_items)
                if result.quota_exceeded:
                    for j in range(i + 1, len(batches_to_process)):
                        remaining.extend(batches_to_process[j].items)
                    break

        return remaining

    async def _process_single_mode(
        self,
        items: List[FileItem],
        quota: int,
        pbar: tqdm,
        api_key: str,
        *,
        config: Config,
        processor: FileProcessor,
        paid_ctx: Optional[PaidBudgetContext],
    ) -> Deque[FileItem]:
        remaining: Deque[FileItem] = deque()
        to_process = list(items[:quota])
        for item in items[quota:]:
            remaining.append(item)

        semaphore = asyncio.Semaphore(config.concurrency_limit)

        async def process_one(it: FileItem) -> SingleResult:
            async with semaphore:
                result = await processor.process_single(it, pbar, paid_ctx=paid_ctx)
                if not result.budget_exceeded:
                    self._key_manager.increment_usage(api_key)
                return result

        tasks = [process_one(it) for it in to_process]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for it, result in zip(to_process, results):
            if isinstance(result, Exception):
                logger.error(f"处理错误 {it.path.name}: {result}")
                remaining.append(it)
                self._stats.total_failed += 1
                continue

            if result.success:
                self._stats.total_processed += 1
                continue

            # Not processed: keep for later.
            if result.failed_item:
                remaining.append(result.failed_item)
            elif it:
                remaining.append(it)

            if not result.budget_exceeded:
                self._stats.total_failed += 1

        return remaining

    def _print_summary(self) -> None:
        print("\n" + "-" * 65)
        print("运行结束！")
        print(f"成功处理: {self._stats.total_processed} 个文件")
        print(f"处理失败: {self._stats.total_failed} 个文件")
        if self._stats.total_skipped > 0:
            print(f"跳过（过大）: {self._stats.total_skipped} 个文件")
        print(f"\n--- 耗时分析 ---")
        print(f"准备阶段: {self._stats.prep_time:.2f} 秒")
        print(f"API处理: {self._stats.api_time:.2f} 秒")
        print(f"总耗时: {self._stats.total_time:.2f} 秒")
        if self._stats.total_processed > 0:
            print(f"平均速率: {self._stats.average_rate:.2f} 文件/秒")


# ============================================================================
# 命令行接口
# ============================================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="使用 Gemini API 批量智能重命名文件并写入元数据。",
        formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        "directory", nargs='?', default="./files_to_rename",
        help="待处理文件目录 (默认: ./files_to_rename)"
    )
    parser.add_argument(
        "--mode", choices=['batch', 'single', 'auto'], default='batch',
        help="处理模式:\n  batch: 批处理 (默认)\n  single: 单文件处理\n  auto: 自动选择（少量文件用单文件并发，大量文件用批处理）"
    )
    parser.add_argument("--no-metadata", action="store_true", help="禁用元数据写入")
    parser.add_argument("--proxy", type=str, default=None, help="手动指定代理地址")
    parser.add_argument("--no-proxy", action="store_true", help="禁用自动代理检测")

    # Paid tier options
    parser.add_argument(
        "--tier", choices=["free", "paid"], default="free",
        help="运行套餐:\n  free: 使用当前免费模型 (默认)\n  paid: 使用 Gemini 3 Flash + 月度预算限制"
    )
    parser.add_argument(
        "--monthly-budget-usd", type=float, default=10.0,
        help="付费模式：每个 API key 每月预算上限（美元，默认 10）"
    )
    parser.add_argument(
        "--budget-file", type=str, default="./budget_tracker.json",
        help="付费模式：月度预算跟踪文件路径 (默认: ./budget_tracker.json)"
    )
    parser.add_argument(
        "--show-budget", action="store_true",
        help="显示 budget_tracker.json 中本月预算使用情况并退出（不会显示明文 key）"
    )
    parser.add_argument(
        "--paid-model", type=str, default="gemini-3-flash-preview",
        help="付费模式使用的模型名 (默认: gemini-3-flash-preview)"
    )
    parser.add_argument(
        "--free-model", type=str, default=GeminiModel.DEFAULT_MODEL_NAME,
        help=f"免费模式使用的模型名 (默认: {GeminiModel.DEFAULT_MODEL_NAME})"
    )
    parser.add_argument(
        "--max-context-tokens", type=int, default=200_000,
        help="单次请求上下文 token 硬上限 (默认: 200000; paid 会强制 <= 200000)"
    )
    parser.add_argument(
        "--paid-max-request-tokens", type=int, default=100_000,
        help="付费模式：单次请求输入侧 token 上限，用于批处理打包 (默认: 100000)"
    )
    parser.add_argument(
        "--paid-max-output-tokens", type=int, default=8_192,
        help="付费模式：max_output_tokens (默认: 8192)"
    )
    parser.add_argument(
        "--paid-max-items", type=int, default=40,
        help="付费模式：单个批次最多文件数 (默认: 40)"
    )
    parser.add_argument(
        "--paid-concurrency", type=int, default=20,
        help="付费模式：并发上限（默认 20；会自动 clamp 到 <= paid-rpm-limit）"
    )
    parser.add_argument(
        "--paid-rpm-limit", type=int, default=1_000,
        help="付费模式：每分钟请求数上限 RPM（默认 1000；若频繁 429，可调低）"
    )
    parser.add_argument(
        "--paid-tpm-limit", type=int, default=1_000_000,
        help="付费模式：每分钟 token 上限 TPM（默认 1000000；若频繁 429，可调低）"
    )
    parser.add_argument(
        "--paid-daily-request-limit", type=int, default=10_000,
        help="付费模式：每日请求上限（默认 10000；用于限制 request_tracker.json 的每日计数）"
    )
    parser.add_argument(
        "--paid-economy", action="store_true",
        help="付费模式：省钱模式（在 --mode auto 下尽量使用批处理以减少重复 prompt/thinking 开销）"
    )
    parser.add_argument(
        "--auto-single-threshold", type=int, default=30,
        help="--mode auto 时：文件数 <= 该阈值会优先选择单文件并发 (默认 30)"
    )
    parser.add_argument(
        "--auto-economy-single-threshold", type=int, default=3,
        help="--mode auto + --paid-economy 时：文件数 <= 该阈值才会选择单文件并发 (默认 3)"
    )
    parser.add_argument(
        "--on-budget-exceeded", choices=["downgrade", "stop"], default="downgrade",
        help="付费预算耗尽后的行为:\n  downgrade: 自动降级到免费模型继续处理 (默认)\n  stop: 立即停止并保留 pending"
    )
    parser.add_argument(
        "--paid-input-usd-per-1m", type=float, default=0.50,
        help="付费模式：输入 token 单价（USD/1M tokens，默认 0.50，可用于价格变动时手动覆盖）"
    )
    parser.add_argument(
        "--paid-output-usd-per-1m", type=float, default=3.00,
        help="付费模式：输出 token 单价（USD/1M tokens，默认 3.00，可用于价格变动时手动覆盖）"
    )
    return parser.parse_args()


def show_budget_summary(budget_file: Path) -> None:
    month = BudgetManager.month_key()
    if not budget_file.exists():
        print("\n" + "=" * 65)
        print("预算用量 (paid tier)")
        print("=" * 65)
        print(f"未找到预算文件: {budget_file}")
        print("=" * 65)
        return

    try:
        with open(budget_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print("\n" + "=" * 65)
        print("预算用量 (paid tier)")
        print("=" * 65)
        print(f"读取预算文件失败: {e}")
        print("=" * 65)
        return

    months = data.get("months", {})
    month_bucket = months.get(month, {}) if isinstance(months, dict) else {}
    if not isinstance(month_bucket, dict) or not month_bucket:
        print("\n" + "=" * 65)
        print("预算用量 (paid tier)")
        print("=" * 65)
        print(f"本月 ({month}) 暂无预算记录。")
        print("=" * 65)
        return

    print("\n" + "=" * 65)
    print(f"预算用量 (paid tier) - {month}")
    print("=" * 65)

    total_spent_nanos = 0
    for key_id, entry in sorted(month_bucket.items(), key=lambda kv: str(kv[0])):
        if not _looks_like_key_id(str(key_id)):
            continue
        if not isinstance(entry, dict):
            continue
        spent_nanos = int(entry.get("spent_nanos_usd", 0) or 0)
        total_spent_nanos += spent_nanos
        in_tk = int(entry.get("input_tokens", 0) or 0)
        out_tk = int(entry.get("output_tokens", 0) or 0)
        req = int(entry.get("requests", 0) or 0)
        spent_usd = spent_nanos / 1_000_000_000
        print(
            f"{key_id}: ${spent_usd:.4f} | req={req} | in={in_tk:,} | out={out_tk:,}"
        )

    print("-" * 65)
    print(f"合计: ${total_spent_nanos / 1_000_000_000:.4f}")
    print("=" * 65)


async def main() -> None:
    args = parse_args()

    if args.show_budget:
        show_budget_summary(Path(args.budget_file))
        return

    if not args.no_proxy:
        if args.proxy:
            logger.info(f"使用手动指定代理: {args.proxy}")
            result = ProxyDetector.apply(proxy=args.proxy, auto_detect=False)
        else:
            logger.info("正在检测系统代理...")
            result = ProxyDetector.apply(auto_detect=True)
        if result['applied']:
            logger.info(f"代理已配置: {result['proxy']}")
        else:
            logger.info("未检测到代理，将直接连接")
    else:
        logger.info("代理功能已禁用")

    if args.mode == 'batch':
        mode = ProcessingMode.BATCH
    elif args.mode == 'single':
        mode = ProcessingMode.SINGLE
    else:
        mode = ProcessingMode.AUTO
    write_metadata = not args.no_metadata
    target_dir = Path(args.directory)

    app = Application(
        config=CONFIG,
        mode=mode,
        write_metadata=write_metadata,
        target_dir=target_dir,
        auto_single_threshold=int(args.auto_single_threshold),
        auto_economy_single_threshold=int(args.auto_economy_single_threshold),
        paid_economy_mode=bool(args.paid_economy),
    )
    tier = args.tier

    if tier == "paid":
        # Enforce the 200k hard cap in paid tier to avoid crossing context pricing boundaries.
        max_context = min(int(args.max_context_tokens), 200_000)
        max_out = max(1, int(args.paid_max_output_tokens))
        margin = CONFIG.budget_safety_margin_tokens

        # Extra conservative overhead for prompt markers/instructions.
        prompt_overhead_tokens = 2_000

        effective_max_input = min(
            int(args.paid_max_request_tokens),
            max(1, max_context - max_out - margin - prompt_overhead_tokens),
        )

        paid_rpm_limit = max(1, int(args.paid_rpm_limit))
        paid_tpm_limit = max(1, int(args.paid_tpm_limit))
        paid_daily_request_limit = max(1, int(args.paid_daily_request_limit))

        paid_concurrency = max(1, min(int(args.paid_concurrency), paid_rpm_limit))

        paid_config = replace(
            CONFIG,
            rpm_limit=paid_rpm_limit,
            tpm_limit=paid_tpm_limit,
            daily_request_limit=paid_daily_request_limit,
            max_tokens_per_request=effective_max_input,
            max_items_per_batch=max(1, int(args.paid_max_items)),
            concurrency_limit=paid_concurrency,
            max_context_tokens=max_context,
            max_output_tokens=max_out,
            budget_file=Path(args.budget_file),
        )

        # Pricing override support: nanos/token = (USD/1M) * 1000.
        in_nanos = int(round(float(args.paid_input_usd_per_1m) * 1000))
        out_nanos = int(round(float(args.paid_output_usd_per_1m) * 1000))
        budget_mgr = BudgetManager(
            paid_config.budget_file,
            in_nanos_per_token=in_nanos,
            out_nanos_per_token=out_nanos,
        )

        await app.run(
            tier="paid",
            free_model=args.free_model,
            paid_model=args.paid_model,
            paid_config=paid_config,
            monthly_budget_usd=float(args.monthly_budget_usd),
            on_budget_exceeded=args.on_budget_exceeded,
            budget_manager=budget_mgr,
        )
    else:
        await app.run(
            tier="free",
            free_model=args.free_model,
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n程序被用户中断。")
        sys.exit(0)
