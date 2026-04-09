"""
文件元数据索引器（轻量级，秒级完成）

不读文件内容，只扫描：
  - 文件路径 / 文件名 / 扩展名
  - 最后修改时间
  - 文件大小
  - 从路径提取关键词（活跃度判断用）

结果写入 SQLite file_index 表（status='metadata'），
供Aegis知道"哪些文件最近有动静"，查询时按需精读。

活跃度分层：
  热区：最近30天修改  → current_focus
  温区：31-180天      → active
  冷区：180天以上     → archived
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import config
from memory import db as main_db

# 扫描根目录（个人工作目录，非系统目录）
DEFAULT_SCAN_ROOTS = [
    r"C:\Users\Administrator\Documents",
    r"G:\backup_documents",
    r"G:\国自然2026",
    r"E:\codes",
    r"D:\\",
]

# 跳过的目录关键词
_SKIP_DIR_KEYWORDS = {
    "adobe", "assassin", "captura", "apowersoft", "wechat files",
    "tencent", "zoom", "teams", "matlab", "oculus", "steam",
    "navicat", "qqpc", "upupoo", "videowinsoft", "letsview",
    "sunlogin", "wps", "fax", "downloads", "download", "scanned",
    ".accelerate", "onedrive", "onenote", "outlook", "visual studio",
    "windowspowershell", "league of leagues", "overwatch", "mount & blade",
    "dyson", "flingt", "rdb", "tdb", "sdb", ".data",
    "my games", "rockstar", "ubisoft", "epic games", "steam",
    "__pycache__", "node_modules", "venv", ".venv", "site-packages",
    "dist", "build", ".git", "cache", "$recycle", "system volume",
    "program files", "appdata", "windows", "drivers", "wegameapps",
    "driverbackup", "pe_jsyos", "wegame", "jetsbrains", "cursor",
    "anaconda", "miniconda", "envs",
}

# 有价值的文件扩展名（其他跳过）
_VALUABLE_EXTENSIONS = {
    ".md", ".txt", ".pdf", ".doc", ".docx",
    ".ppt", ".pptx", ".xls", ".xlsx", ".csv",
    ".py", ".ipynb", ".r", ".m",
    ".png", ".jpg",  # 可能是图表
}

# 关键文档的文件名模式（这些值得向量化）
KEY_DOC_PATTERNS = [
    r"CV", r"cv_", r"简历", r"个人介绍",
    r"研究基础", r"发表文章", r"publication",
    r"国自然.*标书", r"研究计划", r"个人陈述",
    r"background", r"研究成果", r"核心内容",
    r"材料梳理", r"notes?\.md$", r"readme\.md$",
]
_KEY_DOC_RE = re.compile("|".join(KEY_DOC_PATTERNS), re.IGNORECASE)


def _is_skip_dir(name: str) -> bool:
    nl = name.lower()
    return any(kw in nl for kw in _SKIP_DIR_KEYWORDS)


def _activity_tier(mtime: float) -> str:
    """根据修改时间判断活跃度"""
    age_days = (time.time() - mtime) / 86400
    if age_days <= 30:
        return "hot"
    elif age_days <= 180:
        return "warm"
    else:
        return "cold"


def _extract_keywords(path: str) -> str:
    """从路径中提取关键词（用于后续搜索）"""
    parts = Path(path).parts
    # 取最后3级目录名+文件名
    relevant = parts[-4:] if len(parts) >= 4 else parts
    combined = " ".join(relevant)
    # 去掉扩展名、下划线、驼峰拆分
    combined = re.sub(r'\.[a-zA-Z]{2,5}$', '', combined)
    combined = re.sub(r'[_\-]', ' ', combined)
    return combined[:200]


def scan_metadata(
    scan_roots: list[str] = None,
    max_depth: int = 6,
    incremental: bool = True,
) -> dict:
    """
    扫描文件系统元数据，写入 file_index。

    incremental=True: 只处理新增或修改过的文件（对比 DB 中已有记录的 mtime）
    返回统计信息。
    """
    scan_roots = scan_roots or DEFAULT_SCAN_ROOTS
    stats = {"scanned": 0, "new": 0, "updated": 0, "skipped": 0}

    # 先确保 schema 列存在（独立提交，避免 DDL 与 DML 同事务冲突）
    with main_db.get_conn() as conn:
        _ensure_columns(conn)

    # 加载已有记录的 mtime（增量模式）
    existing_mtimes: dict[str, float] = {}
    if incremental:
        try:
            with main_db.get_conn() as conn:
                rows = conn.execute(
                    "SELECT path, indexed_at FROM file_index WHERE status='metadata'"
                ).fetchall()
                for r in rows:
                    try:
                        ts = datetime.fromisoformat(r["indexed_at"]).timestamp() if r["indexed_at"] else 0
                        existing_mtimes[r["path"]] = ts
                    except Exception:
                        pass
        except Exception:
            pass

    now_iso = datetime.now().isoformat()
    batch: list[tuple] = []

    for root_str in scan_roots:
        root = Path(root_str)
        if not root.exists():
            continue

        for dirpath, dirnames, filenames in os.walk(root, topdown=True):
            # 过滤不需要的目录
            dirnames[:] = [
                d for d in dirnames
                if not _is_skip_dir(d) and not d.startswith(".")
            ]
            depth = len(Path(dirpath).relative_to(root).parts)
            if depth >= max_depth:
                dirnames.clear()
                continue

            for fname in filenames:
                ext = Path(fname).suffix.lower()
                if ext not in _VALUABLE_EXTENSIONS:
                    continue

                fpath = str(Path(dirpath) / fname)
                stats["scanned"] += 1

                try:
                    st = Path(fpath).stat()
                    mtime = st.st_mtime
                    size = st.st_size
                except OSError:
                    continue

                # 增量检查
                if incremental and fpath in existing_mtimes:
                    if abs(mtime - existing_mtimes[fpath]) < 2:
                        stats["skipped"] += 1
                        continue
                    stats["updated"] += 1
                else:
                    stats["new"] += 1

                tier = _activity_tier(mtime)
                keywords = _extract_keywords(fpath)
                is_key_doc = 1 if _KEY_DOC_RE.search(fname) else 0
                mtime_iso = datetime.fromtimestamp(mtime).isoformat()

                batch.append((
                    fpath, fname, ext, size,
                    tier, keywords, is_key_doc,
                    mtime_iso, now_iso,
                ))

                # 批量写入
                if len(batch) >= 500:
                    _flush_batch(batch)
                    batch.clear()

    if batch:
        _flush_batch(batch)

    print(f"[MetaIndex] 扫描完成: 总{stats['scanned']} 新增{stats['new']} 更新{stats['updated']} 跳过{stats['skipped']}")
    return stats


def _flush_batch(batch: list[tuple]):
    """批量写入 file_index"""
    try:
        with main_db.get_conn() as conn:
            conn.executemany("""
                INSERT INTO file_index
                    (path, filename, extension, size_bytes, status,
                     activity_tier, keywords, is_key_doc,
                     modified_at, indexed_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(path) DO UPDATE SET
                    size_bytes   = excluded.size_bytes,
                    status       = CASE WHEN status='vectorized' THEN 'vectorized' ELSE excluded.status END,
                    activity_tier = excluded.activity_tier,
                    keywords     = excluded.keywords,
                    is_key_doc   = excluded.is_key_doc,
                    modified_at  = excluded.modified_at,
                    indexed_at   = excluded.indexed_at
            """, [
                (path, fname, ext, size, "metadata",
                 tier, kw, is_key, mtime, now)
                for path, fname, ext, size, tier, kw, is_key, mtime, now in batch
            ])
    except Exception as e:
        print(f"[MetaIndex] 写入失败: {e}")


def _ensure_columns(conn):
    """确保 file_index 有新增列"""
    for col, defn in [
        ("activity_tier", "TEXT DEFAULT 'cold'"),
        ("keywords",      "TEXT"),
        ("is_key_doc",    "INTEGER DEFAULT 0"),
        ("modified_at",   "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE file_index ADD COLUMN {col} {defn}")
        except Exception:
            pass  # 列已存在


def get_hot_files(limit: int = 50) -> list[dict]:
    """返回最近30天修改的文件（当前焦点）"""
    with main_db.get_conn() as conn:
        _ensure_columns(conn)
        rows = conn.execute("""
            SELECT path, filename, extension, activity_tier, keywords, modified_at
            FROM file_index
            WHERE activity_tier = 'hot'
            ORDER BY modified_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def get_key_docs() -> list[dict]:
    """返回识别为关键文档的文件（供向量化用）"""
    with main_db.get_conn() as conn:
        _ensure_columns(conn)
        rows = conn.execute("""
            SELECT path, filename, extension, modified_at
            FROM file_index
            WHERE is_key_doc = 1
              AND status != 'vectorized'
            ORDER BY modified_at DESC
        """).fetchall()
    return [dict(r) for r in rows]


def search_files(query: str, tier: str = None, limit: int = 20) -> list[dict]:
    """
    按关键词搜索文件元数据（不读内容）。
    用于Aegis回答"我有没有关于XX的文件"类问题。
    """
    with main_db.get_conn() as conn:
        _ensure_columns(conn)
        if tier:
            rows = conn.execute("""
                SELECT path, filename, activity_tier, keywords, modified_at
                FROM file_index
                WHERE keywords LIKE ? AND activity_tier = ?
                ORDER BY modified_at DESC LIMIT ?
            """, (f"%{query}%", tier, limit)).fetchall()
        else:
            rows = conn.execute("""
                SELECT path, filename, activity_tier, keywords, modified_at
                FROM file_index
                WHERE keywords LIKE ? OR filename LIKE ?
                ORDER BY modified_at DESC LIMIT ?
            """, (f"%{query}%", f"%{query}%", limit)).fetchall()
    return [dict(r) for r in rows]
