"""
BM25 + 向量混合搜索
基于 OpenJarvis hybrid.py 的 RRF 融合算法，纯 Python 实现。

BM25: 关键词精确匹配（作者名、期刊名、专业术语效果好）
Vector: 语义相似度（表达方式不同但含义相近时效果好）
RRF融合: 取两者排名的倒数和，平衡精确与语义
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rank_bm25 import BM25Okapi

import config

_INDEX_PATH = config.DATA_DIR / "bm25_index.json"


class BM25Store:
    """
    内存 BM25 索引 + JSON 持久化。
    支持与 Chroma 向量库的 RRF 混合检索。
    """

    def __init__(self, index_path: Path = _INDEX_PATH):
        self._path = index_path
        self._docs: list[str] = []          # 原始文档文本
        self._ids: list[str] = []           # 文档 ID
        self._metas: list[dict] = []        # 元数据
        self._bm25: BM25Okapi | None = None
        self._load()

    # ── 持久化 ─────────────────────────────────────────────────────────

    def _load(self):
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                self._docs  = data.get("docs", [])
                self._ids   = data.get("ids", [])
                self._metas = data.get("metas", [])
                if self._docs:
                    self._bm25 = BM25Okapi([d.lower().split() for d in self._docs])
            except Exception:
                self._docs, self._ids, self._metas = [], [], []

    def _save(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps({"docs": self._docs, "ids": self._ids, "metas": self._metas},
                       ensure_ascii=False),
            encoding="utf-8",
        )

    # ── 写入 ────────────────────────────────────────────────────────────

    def add(self, doc_id: str, text: str, metadata: dict | None = None):
        """追加文档。已存在则跳过（按 doc_id 去重）。"""
        if doc_id in self._ids:
            return
        self._docs.append(text)
        self._ids.append(doc_id)
        self._metas.append(metadata or {})
        self._bm25 = BM25Okapi([d.lower().split() for d in self._docs])
        # 每100条自动持久化
        if len(self._docs) % 100 == 0:
            self._save()

    def flush(self):
        """强制持久化"""
        self._save()

    # ── 检索 ────────────────────────────────────────────────────────────

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        """
        BM25 检索，返回:
        [{"id": ..., "text": ..., "score": ..., "metadata": ...}, ...]
        """
        if not self._bm25 or not self._docs:
            return []
        tokens = query.lower().split()
        scores = self._bm25.get_scores(tokens)
        # 取 top_k 最高分
        ranked = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)[:top_k]
        results = []
        for idx, score in ranked:
            if score > 0:
                results.append({
                    "id":       self._ids[idx],
                    "text":     self._docs[idx][:500],
                    "score":    float(score),
                    "metadata": self._metas[idx],
                    "rank":     len(results),
                })
        return results

    def count(self) -> int:
        return len(self._docs)


# ── 全局实例（按集合分开）────────────────────────────────────────────

_stores: dict[str, BM25Store] = {}


def get_store(collection: str) -> BM25Store:
    if collection not in _stores:
        path = config.DATA_DIR / f"bm25_{collection}.json"
        _stores[collection] = BM25Store(path)
    return _stores[collection]


# ── RRF 混合检索 ─────────────────────────────────────────────────────

def reciprocal_rank_fusion(
    ranked_lists: list[list[dict]],
    *,
    k: int = 60,
    weights: list[float] | None = None,
) -> list[dict]:
    """
    Reciprocal Rank Fusion — 直接来自 OpenJarvis hybrid.py。

    RRF_score(d) = Σ weight_i / (k + rank_i(d))

    ranked_lists: 多个排序列表，每项需含 "id", "text", "metadata"
    """
    if weights is None:
        weights = [1.0] * len(ranked_lists)

    scores: dict[str, float] = {}
    best: dict[str, dict] = {}

    for weight, results in zip(weights, ranked_lists):
        for rank, item in enumerate(results):
            key = item.get("id", item.get("text", "")[:50])
            rrf = weight / (k + rank + 1)
            scores[key] = scores.get(key, 0.0) + rrf
            if key not in best:
                best[key] = item

    fused = []
    for key, fused_score in sorted(scores.items(), key=lambda x: x[1], reverse=True):
        item = dict(best[key])
        item["score"] = fused_score
        fused.append(item)
    return fused


def hybrid_search(
    collection: str,
    query: str,
    top_k: int = 5,
    bm25_weight: float = 1.0,
    vec_weight: float = 1.5,
) -> list[dict]:
    """
    混合搜索: BM25 + 向量，RRF 融合。
    向量权重默认略高（语义更重要）。
    """
    from memory import vector_store as vs

    fetch_k = top_k * 3

    # BM25 检索
    bm25_results = get_store(collection).search(query, top_k=fetch_k)

    # 向量检索
    vec_raw = []
    try:
        vec_hits = vs.search(collection, query, n_results=fetch_k)
        for i, hit in enumerate(vec_hits):
            vec_raw.append({
                "id":       hit.get("id", f"vec_{i}"),
                "text":     hit.get("text", ""),
                "score":    1.0 - i * 0.05,  # 近似分数
                "metadata": hit.get("metadata", {}),
            })
    except Exception:
        pass

    if not bm25_results and not vec_raw:
        return []

    fused = reciprocal_rank_fusion(
        [bm25_results, vec_raw],
        weights=[bm25_weight, vec_weight],
    )
    return fused[:top_k]
