"""
SQLite FTS5 全文检索存储
替代纯内存 rank_bm25，支持持久化、百万级文档、原生 BM25 排名。

基于 OpenJarvis TwoStageRetriever 的 Stage 1 实现。
"""
from __future__ import annotations

import sqlite3
import hashlib
from pathlib import Path
from typing import Any

import config

FTS_DB_PATH = config.DATA_DIR / "fts_index.db"


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(FTS_DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _init():
    with _get_conn() as conn:
        conn.executescript("""
        -- FTS5 虚拟表，unicode61 tokenizer
        -- 注意: 若旧数据库使用 trigram tokenizer，请删除 fts_index.db 后重建。
        CREATE VIRTUAL TABLE IF NOT EXISTS fts_docs
        USING fts5(
            doc_id UNINDEXED,
            collection UNINDEXED,
            content,
            source UNINDEXED,
            metadata UNINDEXED,
            tokenize = 'unicode61'
        );

        -- 元数据表（FTS5 本身不支持 WHERE 过滤元数据列）
        CREATE TABLE IF NOT EXISTS fts_meta (
            doc_id TEXT PRIMARY KEY,
            collection TEXT,
            source TEXT,
            metadata TEXT,
            added_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_fts_meta_coll ON fts_meta(collection);
        """)


_init()


class FTSStore:
    """
    基于 SQLite FTS5 的文档检索。
    对应 OpenJarvis TwoStageRetriever Stage 1。
    """

    def add(self, doc_id: str, collection: str, text: str,
            source: str = "", metadata: dict | None = None) -> bool:
        """添加文档到 FTS 索引。已存在则跳过，返回是否新增。
        中文内容会先经过 jieba 分词处理后再存储，以配合 unicode61 tokenizer。
        """
        import json
        from datetime import datetime

        # 先检查是否已存在
        with _get_conn() as conn:
            exists = conn.execute(
                "SELECT 1 FROM fts_meta WHERE doc_id=?", (doc_id,)
            ).fetchone()
            if exists:
                return False

            # 对 content 进行预分词（jieba），source/metadata 保持原样（UNINDEXED）
            segmented_text = _segment_for_index(text)

            meta_json = json.dumps(metadata or {}, ensure_ascii=False)
            conn.execute(
                "INSERT INTO fts_docs(doc_id, collection, content, source, metadata) "
                "VALUES (?,?,?,?,?)",
                (doc_id, collection, segmented_text, source, meta_json)
            )
            conn.execute(
                "INSERT OR IGNORE INTO fts_meta(doc_id, collection, source, metadata, added_at) "
                "VALUES (?,?,?,?,?)",
                (doc_id, collection, source, meta_json, datetime.now().isoformat())
            )
        return True

    def search(self, query: str, collection: str = None,
               top_k: int = 20) -> list[dict]:
        """
        FTS5 BM25 检索。
        collection=None 时搜索全部集合。
        返回: [{"doc_id", "text", "score", "source", "metadata", "collection"}, ...]
        """
        if not query.strip():
            return []

        # FTS5 需要转义特殊字符
        safe_query = _escape_fts5(query)
        if not safe_query:
            return []

        import json
        results = []
        try:
            with _get_conn() as conn:
                if collection:
                    rows = conn.execute("""
                        SELECT doc_id, collection, content, source, metadata,
                               bm25(fts_docs) as score
                        FROM fts_docs
                        WHERE fts_docs MATCH ? AND collection = ?
                        ORDER BY score
                        LIMIT ?
                    """, (safe_query, collection, top_k)).fetchall()
                else:
                    rows = conn.execute("""
                        SELECT doc_id, collection, content, source, metadata,
                               bm25(fts_docs) as score
                        FROM fts_docs
                        WHERE fts_docs MATCH ?
                        ORDER BY score
                        LIMIT ?
                    """, (safe_query, top_k)).fetchall()

                for i, row in enumerate(rows):
                    meta = {}
                    try:
                        meta = json.loads(row[4] or "{}")
                    except Exception:
                        pass
                    # FTS5 bm25() 返回负数（越负越相关），转为正分
                    score = abs(float(row[5]))
                    results.append({
                        "doc_id":     row[0],
                        "collection": row[1],
                        "text":       row[2][:600],
                        "source":     row[3],
                        "metadata":   meta,
                        "score":      score,
                        "rank":       i,
                    })
        except Exception as e:
            # FTS5 查询语法错误时降级为 LIKE 搜索
            results = _fallback_like_search(query, collection, top_k)

        return results

    def delete(self, doc_id: str) -> bool:
        with _get_conn() as conn:
            n = conn.execute(
                "DELETE FROM fts_docs WHERE doc_id=?", (doc_id,)
            ).rowcount
            conn.execute("DELETE FROM fts_meta WHERE doc_id=?", (doc_id,))
        return n > 0

    def count(self, collection: str = None) -> int:
        with _get_conn() as conn:
            if collection:
                return conn.execute(
                    "SELECT COUNT(*) FROM fts_meta WHERE collection=?",
                    (collection,)
                ).fetchone()[0]
            return conn.execute("SELECT COUNT(*) FROM fts_meta").fetchone()[0]

    def collections(self) -> list[str]:
        with _get_conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT collection FROM fts_meta"
            ).fetchall()
        return [r[0] for r in rows]


def _segment_for_index(text: str) -> str:
    """
    对要存入 FTS5 的文本进行预分词（jieba），返回空格分隔的词语字符串。
    jieba 不可用时返回原文（unicode61 仍可匹配）。
    """
    try:
        from memory.tokenizer_cn import segment
        result = segment(text)
        return result if result.strip() else text
    except Exception:
        return text


def _escape_fts5(query: str) -> str:
    """
    把用户查询转为 FTS5 unicode61 OR 查询。

    优先使用 jieba 分词获取候选词；
    jieba 不可用时降级为 bigram 窗口。
    多个候选用 OR 连接（召回导向，后续 RRF 重排精度）。
    """
    clean = query.replace('"', '').replace("'", "").strip()
    if not clean:
        return ""

    candidates: set[str] = set()

    # 1. 尝试 jieba 分词
    try:
        from memory.tokenizer_cn import segment_for_search
        terms = segment_for_search(clean)
        if terms:
            candidates.update(t for t in terms if len(t) >= 2)
    except Exception:
        pass

    # 2. 如果 jieba 不可用或返回空，fallback 到 bigram
    if not candidates:
        import re
        # 英文单词
        eng_words = re.findall(r'[a-zA-Z0-9]{2,}', clean)
        candidates.update(eng_words)
        # 中文 bigram
        chinese_chars = [c for c in clean if '\u4e00' <= c <= '\u9fff']
        for i in range(len(chinese_chars) - 1):
            candidates.add("".join(chinese_chars[i:i+2]))
        # 整串（如果足够短）
        if 2 <= len(clean) <= 20:
            candidates.add(clean)

    if not candidates:
        return ""

    # 每个候选用双引号包裹，OR 连接
    return " OR ".join(f'"{c}"' for c in candidates)


def _fallback_like_search(query: str, collection: str, top_k: int) -> list[dict]:
    """FTS5 查询失败时的 LIKE 降级"""
    import json
    keyword = query[:50]
    with _get_conn() as conn:
        if collection:
            rows = conn.execute("""
                SELECT doc_id, collection, content, source, metadata
                FROM fts_docs WHERE content LIKE ? AND collection=? LIMIT ?
            """, (f"%{keyword}%", collection, top_k)).fetchall()
        else:
            rows = conn.execute("""
                SELECT doc_id, collection, content, source, metadata
                FROM fts_docs WHERE content LIKE ? LIMIT ?
            """, (f"%{keyword}%", top_k)).fetchall()
    results = []
    for i, row in enumerate(rows):
        meta = {}
        try:
            meta = json.loads(row[4] or "{}")
        except Exception:
            pass
        results.append({
            "doc_id": row[0], "collection": row[1],
            "text": row[2][:600], "source": row[3],
            "metadata": meta, "score": 1.0, "rank": i,
        })
    return results


# 全局单例
_store = FTSStore()


def add_document(doc_id: str, collection: str, text: str,
                 source: str = "", metadata: dict | None = None) -> bool:
    return _store.add(doc_id, collection, text, source, metadata)


def search(query: str, collection: str = None, top_k: int = 20) -> list[dict]:
    return _store.search(query, collection, top_k)


def count(collection: str = None) -> int:
    return _store.count(collection)


def get_store() -> FTSStore:
    return _store
