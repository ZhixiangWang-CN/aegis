"""
知识背景上下文注入
直接移植自 OpenJarvis tools/storage/context.py，适配我们的存储层。

核心流程:
  用户问题 → FTS5 召回候选 → Chroma 向量重排 → 注入 SYSTEM 消息

效果: AI 回答时自动参考已有的邮件、文件、微信记录，
      不再只靠训练知识，而是基于你的真实数据作答。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RetrievalResult:
    content: str
    score:    float = 0.0
    source:   str   = ""
    metadata: dict  = field(default_factory=dict)


@dataclass
class ContextConfig:
    """
    对应 OpenJarvis ContextConfig。
    """
    enabled:            bool  = True
    top_k:              int   = 5       # 最终注入的文档数
    recall_k:           int   = 20      # FTS5 阶段召回候选数
    min_score:          float = 0.05    # 低于此分数的结果丢弃
    max_context_tokens: int   = 1500    # 注入内容 token 上限（词数估算）
    collections:        list  = field(default_factory=lambda: [
        "emails", "documents", "wechat", "papers"
    ])


def _count_tokens(text: str) -> int:
    """词数估算（中文按字符/1.5计，英文按空格分词）"""
    chinese = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    english_words = len(text.split())
    return int(chinese / 1.5 + english_words)


def _format_context(results: list[RetrievalResult]) -> str:
    """
    格式化检索结果为可注入的文本块。
    对应 OpenJarvis format_context()，加了中文标注。
    """
    if not results:
        return ""
    lines = []
    for r in results:
        # 来源标签
        src = r.source or r.metadata.get("path", "") or r.metadata.get("from", "")
        if src:
            # 只显示文件名而非完整路径
            src_short = src.split("/")[-1].split("\\")[-1][:40]
            tag = f"[来源: {src_short}]"
        else:
            coll = r.metadata.get("collection", "")
            tag = f"[{coll}]" if coll else ""
        lines.append(f"{tag} {r.content}".strip())
    return "\n\n---\n\n".join(lines)


def retrieve_context(
    query: str,
    config: ContextConfig | None = None,
) -> list[RetrievalResult]:
    """
    两阶段检索: FTS5 召回 → Chroma 向量重排。
    对应 OpenJarvis TwoStageRetriever。
    """
    from memory import fts_store, vector_store as vs

    cfg = config or ContextConfig()
    if not cfg.enabled:
        return []

    candidates: list[RetrievalResult] = []

    # ── Stage 1: FTS5 BM25 召回 ─────────────────────────────────────
    for coll in cfg.collections:
        hits = fts_store.search(query, collection=coll, top_k=cfg.recall_k)
        for h in hits:
            candidates.append(RetrievalResult(
                content=h["text"],
                score=h["score"],
                source=h.get("source", ""),
                metadata={**h.get("metadata", {}), "collection": coll},
            ))

    if not candidates:
        return []

    # ── Stage 2: Chroma 向量重排 ─────────────────────────────────────
    # 用向量搜索对 FTS5 候选集做重排（取交集提升精度）
    vec_results: dict[str, float] = {}
    for coll in cfg.collections:
        try:
            vec_hits = vs.search(coll, query, n_results=cfg.recall_k)
            for i, vh in enumerate(vec_hits):
                key = vh.get("text", "")[:100]
                # 向量排名得分（位置越前越高）
                vec_results[key] = vec_results.get(key, 0) + (1.0 / (60 + i + 1))
        except Exception:
            pass

    # RRF 融合：FTS5 分数 + 向量排名分数
    fused: list[RetrievalResult] = []
    for i, r in enumerate(candidates):
        fts_rrf  = 1.0 / (60 + i + 1)
        vec_rrf  = vec_results.get(r.content[:100], 0.0)
        combined = fts_rrf * 1.0 + vec_rrf * 1.5  # 向量权重略高
        fused.append(RetrievalResult(
            content=r.content,
            score=combined,
            source=r.source,
            metadata=r.metadata,
        ))

    # 按融合分数降序，过滤低分
    fused.sort(key=lambda x: x.score, reverse=True)
    fused = [r for r in fused if r.score >= cfg.min_score]

    # ── Token 预算控制 ────────────────────────────────────────────────
    final: list[RetrievalResult] = []
    total_tokens = 0
    for r in fused:
        t = _count_tokens(r.content)
        if total_tokens + t > cfg.max_context_tokens:
            break
        final.append(r)
        total_tokens += t
        if len(final) >= cfg.top_k:
            break

    return final


def build_context_system_message(results: list[RetrievalResult]) -> str:
    """
    生成注入 SYSTEM 的上下文消息。
    对应 OpenJarvis build_context_message()。
    """
    context_text = _format_context(results)
    if not context_text:
        return ""
    return (
        "以下是从Aegis知识库检索到的相关背景信息，"
        "请基于这些信息作答，引用来源时使用[来源: ...]标注：\n\n"
        + context_text
    )


def inject_context(
    query: str,
    messages: list[dict],
    config: ContextConfig | None = None,
) -> list[dict]:
    """
    主入口：检索知识背景，注入到消息列表头部。
    对应 OpenJarvis inject_context()。

    messages: [{"role": "user"/"assistant"/"system", "content": "..."}, ...]
    返回注入了背景的新消息列表。
    """
    results = retrieve_context(query, config)
    if not results:
        return messages

    ctx_content = build_context_system_message(results)
    if not ctx_content:
        return messages

    # 注入为第一条 system 消息（如已有 system 消息则追加到其后）
    injected = [{"role": "system", "content": ctx_content}]
    injected.extend(messages)
    return injected


def search_knowledge(
    query: str,
    top_k: int = 8,
    collections: list[str] | None = None,
) -> str:
    """
    供指令处理器调用的知识搜索接口。
    返回格式化的搜索结果文本。
    """
    cfg = ContextConfig(
        top_k=top_k,
        recall_k=30,
        collections=collections or ["emails", "documents", "wechat", "papers"],
    )
    results = retrieve_context(query, cfg)
    if not results:
        return f"知识库中未找到关于「{query}」的相关内容。"

    lines = [f"搜索「{query}」— 找到 {len(results)} 条相关内容:\n"]
    for i, r in enumerate(results, 1):
        src = r.source or r.metadata.get("path", "未知来源")
        src_short = src.split("/")[-1].split("\\")[-1][:50]
        coll = r.metadata.get("collection", "")
        lines.append(
            f"{i}. [{coll}] {r.content[:200]}\n"
            f"   📎 {src_short}\n"
        )
    return "\n".join(lines)
