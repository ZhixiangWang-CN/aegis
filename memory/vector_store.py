"""
Chroma 向量库封装 + FTS5 双写
写入时同时更新 Chroma（语义检索）和 FTS5（关键词检索）。
两者通过 context_inject.py 的两阶段检索融合使用。
"""
import chromadb
from chromadb.utils import embedding_functions
import config

_client = None
_collections = {}

# 使用 Chroma 内置默认嵌入（all-MiniLM-L6-v2，首次运行自动下载）
_ef = embedding_functions.DefaultEmbeddingFunction()


def get_client():
    global _client
    if _client is None:
        _client = chromadb.PersistentClient(path=config.CHROMA_PATH)
    return _client


def get_collection(name: str):
    if name not in _collections:
        _collections[name] = get_client().get_or_create_collection(
            name=name,
            embedding_function=_ef,
        )
    return _collections[name]


def add_document(collection_name: str, doc_id: str, text: str,
                 metadata: dict = None):
    """
    添加文档：同时写入 Chroma（向量）和 FTS5（关键词）。
    两阶段检索的基础。
    """
    if not text or not text.strip():
        return
    text_trunc = text[:2000]
    meta = metadata or {}

    # ── Chroma（语义向量）────────────────────────────────────────────
    try:
        col = get_collection(collection_name)
        col.upsert(
            ids=[doc_id],
            documents=[text_trunc],
            metadatas=[meta],
        )
    except Exception as e:
        print(f"[VectorStore] Chroma 写入失败 {doc_id}: {e}")

    # ── FTS5（关键词 BM25）──────────────────────────────────────────
    try:
        from memory.fts_store import add_document as fts_add
        source = meta.get("path", "") or meta.get("from", "")
        fts_add(
            doc_id=doc_id,
            collection=collection_name,
            text=text_trunc,
            source=source,
            metadata=meta,
        )
    except Exception as e:
        print(f"[VectorStore] FTS5 写入失败 {doc_id}: {e}")


def search(collection_name: str, query_text: str, n_results: int = 5) -> list[dict]:
    """
    语义向量检索（单独调用）。
    混合检索请用 memory.context_inject.retrieve_context()。
    """
    col = get_collection(collection_name)
    try:
        results = col.query(
            query_texts=[query_text],
            n_results=min(n_results, max(col.count(), 1)),
        )
        docs  = results.get("documents", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        ids   = results.get("ids", [[]])[0]
        return [
            {"id": i, "text": d, "metadata": m}
            for i, d, m in zip(ids, docs, metas)
        ]
    except Exception:
        return []


# 兼容旧调用
def query(collection_name: str, query_text: str, n_results: int = 5) -> list[dict]:
    return search(collection_name, query_text, n_results)
