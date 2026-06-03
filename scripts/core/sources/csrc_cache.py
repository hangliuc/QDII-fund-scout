# -*- coding: utf-8 -*-
"""CSRC 季报数据本地缓存

设计目标：
- 季报 PDF 一旦发布就是不变的，按 instance_id 永久缓存
- 解析结果（持仓/地区/行业）也按 instance_id 缓存，避免重复 PDF 解析
- "最新季报 ID" 索引按 fund 主代码缓存，定期检查是否有新发布

缓存结构：
    ~/.fund-scout/csrc_cache/
      ├── pdfs/<instance_id>.pdf              # 原始 PDF
      ├── parsed/<instance_id>.json           # 解析结果（top10/market/industry）
      └── index/<main_code>.json              # 该基金最新 instance_id 索引

使用流程：
    1. 用户调用 predict 或 backtest
    2. CSRCCache 先查 index/<main_code>.json
       - 如果上次检查在 N 天内，直接用缓存的 instance_id
       - 否则到 CSRC 接口查最新，对比 instance_id 是否变化
    3. 用 instance_id 查 parsed/<id>.json
       - 命中：直接返回，0 ms
       - 未命中：从 pdfs/ 读 PDF，或 CSRC 下载，解析后写缓存
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = os.path.expanduser("~/.fund-scout/csrc_cache")
INDEX_TTL_SECONDS = 24 * 3600  # 索引文件 TTL：1 天（即每天最多查一次新季报）

# 缓存数据版本号 - 解析逻辑变化时递增，会让旧缓存失效
PARSED_CACHE_VERSION = 2


class CSRCCache:
    """证监会季报本地缓存

    线程安全说明：单进程内文件读写无锁，靠原子写（先写 .tmp 再 rename）保证完整性。
    """

    def __init__(self, cache_dir: str = DEFAULT_CACHE_DIR, index_ttl: int = INDEX_TTL_SECONDS):
        self.cache_dir = cache_dir
        self.index_ttl = index_ttl
        self.pdfs_dir = os.path.join(cache_dir, "pdfs")
        self.parsed_dir = os.path.join(cache_dir, "parsed")
        self.index_dir = os.path.join(cache_dir, "index")
        for d in (self.pdfs_dir, self.parsed_dir, self.index_dir):
            os.makedirs(d, exist_ok=True)

    # ------------------------------------------------------------------
    # Index: fund main_code -> latest instance_id
    # ------------------------------------------------------------------

    def _index_path(self, main_code: str) -> str:
        return os.path.join(self.index_dir, f"{main_code}.json")

    def get_cached_index(self, main_code: str) -> dict | None:
        """读取 index/<main_code>.json。如果文件过期（超过 TTL），返回 None。"""
        path = self._index_path(main_code)
        if not os.path.exists(path):
            return None
        try:
            mtime = os.path.getmtime(path)
            if time.time() - mtime > self.index_ttl:
                return None  # 过期
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("读取索引 %s 失败: %s", path, e)
            return None

    def save_index(self, main_code: str, instance_id: str, report_name: str,
                   report_send_date: str = "") -> None:
        """记录一只基金的最新 instance_id"""
        path = self._index_path(main_code)
        data = {
            "main_code": main_code,
            "instance_id": instance_id,
            "report_name": report_name,
            "report_send_date": report_send_date,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
        }
        self._atomic_write_json(path, data)

    # ------------------------------------------------------------------
    # PDF cache (raw bytes)
    # ------------------------------------------------------------------

    def _pdf_path(self, instance_id: str) -> str:
        return os.path.join(self.pdfs_dir, f"{instance_id}.pdf")

    def get_pdf(self, instance_id: str) -> bytes | None:
        path = self._pdf_path(instance_id)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "rb") as f:
                return f.read()
        except OSError as e:
            logger.warning("读取 PDF 缓存失败 %s: %s", path, e)
            return None

    def save_pdf(self, instance_id: str, pdf_bytes: bytes) -> None:
        if not pdf_bytes or not pdf_bytes.startswith(b"%PDF"):
            return
        path = self._pdf_path(instance_id)
        tmp_path = path + ".tmp"
        try:
            with open(tmp_path, "wb") as f:
                f.write(pdf_bytes)
            os.replace(tmp_path, path)
        except OSError as e:
            logger.warning("写入 PDF 缓存失败 %s: %s", path, e)

    # ------------------------------------------------------------------
    # Parsed cache (top10 / market / industry)
    # ------------------------------------------------------------------

    def _parsed_path(self, instance_id: str) -> str:
        return os.path.join(self.parsed_dir, f"{instance_id}.json")

    def get_parsed(self, instance_id: str) -> dict | None:
        path = self._parsed_path(instance_id)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # 版本检查：旧缓存自动失效
            if data.get("_cache_version", 0) < PARSED_CACHE_VERSION:
                logger.info("解析缓存版本过低 (%s), 失效: %s",
                            data.get("_cache_version", 0), path)
                return None
            return data
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("读取解析缓存失败 %s: %s", path, e)
            return None

    def save_parsed(self, instance_id: str, data: dict) -> None:
        path = self._parsed_path(instance_id)
        # 自动写入版本号
        payload = {**data, "_cache_version": PARSED_CACHE_VERSION}
        self._atomic_write_json(path, payload)

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def list_cached_funds(self) -> list[dict]:
        """列出所有已缓存的基金及其最新季报"""
        out = []
        if not os.path.exists(self.index_dir):
            return out
        for fname in sorted(os.listdir(self.index_dir)):
            if not fname.endswith(".json"):
                continue
            path = os.path.join(self.index_dir, fname)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                out.append(data)
            except Exception:
                continue
        return out

    def cache_stats(self) -> dict:
        n_pdfs = len([f for f in os.listdir(self.pdfs_dir) if f.endswith(".pdf")])
        n_parsed = len([f for f in os.listdir(self.parsed_dir) if f.endswith(".json")])
        n_index = len([f for f in os.listdir(self.index_dir) if f.endswith(".json")])
        # 估算总大小
        total_size = 0
        for d in (self.pdfs_dir, self.parsed_dir, self.index_dir):
            for f in os.listdir(d):
                total_size += os.path.getsize(os.path.join(d, f))
        return {
            "cache_dir": self.cache_dir,
            "n_pdfs": n_pdfs,
            "n_parsed": n_parsed,
            "n_indexed_funds": n_index,
            "total_size_mb": round(total_size / 1024 / 1024, 2),
        }

    def invalidate_index(self, main_code: str) -> None:
        """强制标记某基金索引为过期，下次会重新查 CSRC"""
        path = self._index_path(main_code)
        if os.path.exists(path):
            os.remove(path)

    def invalidate_all_indexes(self) -> int:
        """强制刷新所有基金的索引（保留 PDF 和解析缓存，仅触发新季报检查）"""
        n = 0
        for fname in os.listdir(self.index_dir):
            if fname.endswith(".json"):
                os.remove(os.path.join(self.index_dir, fname))
                n += 1
        return n

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _atomic_write_json(path: str, data: dict) -> None:
        tmp_path = path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        except OSError as e:
            logger.warning("写入缓存 %s 失败: %s", path, e)
