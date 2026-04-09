"""
选择性向量化：只处理高价值内容，不再全量向量化硬盘文件

向量化范围（优先级从高到低）：
  1. 邮件内容       — 由 email summarizer 写入，此处不重复
  2. 微信消息       — 由 wechat_analyzer 写入，此处不重复
  3. 关键文档       — CV / 发表列表 / 国自然标书 / 研究基础整理等（约100个）
  4. 活跃代码文件   — E:/codes 下最近30天修改的 .py/.md（了解项目进展）

全量文件（278k）不再向量化，改用元数据索引（file_metadata_indexer.py）。
"""
from memory import db, vector_store, profile
from ai import client as ai
from scanner.file_reader import read_file
from scanner.chunking import chunk_text, ChunkConfig
import re

# 使用 OpenJarvis 的分块策略: 512 token块 + 64 token重叠，尊重段落边界
_CHUNK_CONFIG = ChunkConfig(chunk_size=512, chunk_overlap=64, min_chunk_size=50)

# 关键文档模式（这些文件值得向量化）
_KEY_DOC_PATTERNS = re.compile(
    r"CV|简历|个人介绍|研究基础|发表文章|publication|国自然.*标书"
    r"|研究计划|个人陈述|background|研究成果|核心内容|材料梳理"
    r"|notes?\.md$|readme\.md$",
    re.IGNORECASE,
)

# 值得向量化的根目录（只处理这些路径下的文件）
_VECTORIZE_ROOTS = {
    r"c:/users/administrator/documents",
    r"g:/backup_documents",
    r"g:/国自然2026",
    r"e:/codes",
}

# 绝对跳过的路径关键词
_SKIP_PATH_KEYWORDS = {
    "site-packages", "node_modules", "vendor", "dist-packages",
    "miniconda", "anaconda", "envs", "venv", ".venv",
    "program files", "appdata", "windows", ".git",
    "rdb", "tdb", "sdb", ".data/pdf", "my games",
    "steam", "epic", "rockstar", "adobe", "cache",
}


def _should_vectorize(path: str) -> bool:
    """
    判断文件是否应该向量化。
    只处理：关键文档 OR E:/codes 下活跃的代码/说明文件。
    """
    import time
    from pathlib import Path as _P

    path_lower = path.lower().replace("\\", "/")

    # 跳过明确无用路径
    if any(kw in path_lower for kw in _SKIP_PATH_KEYWORDS):
        return False

    # 检查是否在允许的根目录下
    in_allowed_root = any(root in path_lower for root in _VECTORIZE_ROOTS)
    if not in_allowed_root:
        return False

    fname = _P(path).name

    # 关键文档（不限修改时间）
    if _KEY_DOC_PATTERNS.search(fname):
        return True

    # E:/codes 下：只处理最近 60 天修改的 .py / .md / .ipynb
    if "e:/codes" in path_lower or "e:\\codes" in path.lower():
        try:
            age_days = (time.time() - _P(path).stat().st_mtime) / 86400
            ext = _P(path).suffix.lower()
            return age_days <= 60 and ext in {".py", ".md", ".ipynb", ".txt"}
        except Exception:
            return False

    # Documents / G 盘：只处理关键文档（已在上面判断），其他跳过
    return False


def _is_user_content(path: str) -> bool:
    """判断文件是否是用户自创内容（而非第三方库/软件）"""
    return _should_vectorize(path)


def _index_chunks_to_fts(path: str, chunks, file_type: str):
    """将文件分块写入 FTS5 全文索引（jieba 分词）"""
    try:
        from memory.fts_store import get_store
        fts = get_store()
        for chunk in chunks:
            fts.add(
                doc_id=f"file_{path}::chunk{chunk.index}",
                collection="documents",
                text=chunk.content,
                source="disk",
                metadata={"path": path, "type": file_type, "chunk_index": chunk.index},
            )
    except Exception as e:
        print(f"[Vectorizer] FTS 索引失败 {path}: {e}")


def process_pending_files(batch_size: int = 20):
    """
    处理一批 pending 状态的文件（只处理通过 _should_vectorize 筛选的）。
    使用 OpenJarvis 分块策略：段落感知分块 + 向量化每个chunk + FTS5 索引。
    """
    pending = db.get_pending_files(limit=batch_size * 10)  # 多拉一些，过滤后够用
    if not pending:
        print("[Vectorizer] 没有待处理文件")
        return 0

    # 过滤：只处理值得向量化的文件
    to_process = [f for f in pending if _should_vectorize(f["path"])]

    # 不值得向量化的文件直接标记为跳过，避免下次重复出现
    skip_paths = [f["path"] for f in pending if not _should_vectorize(f["path"])]
    if skip_paths:
        try:
            with db.get_conn() as conn:
                conn.executemany(
                    "UPDATE file_index SET status='skipped', is_indexed=0 WHERE path=?",
                    [(p,) for p in skip_paths]
                )
        except Exception:
            pass

    to_process = to_process[:batch_size]
    if not to_process:
        print("[Vectorizer] 本批无需向量化的文件（已跳过非关键文件）")
        return 0

    processed = 0
    for file_record in to_process:
        path = file_record["path"]
        try:
            text = read_file(path)
            if not text or len(text.strip()) < 50:
                db.update_file_summary(path, "内容过少，跳过")
                continue

            # 用 OpenJarvis 分块策略切分文档
            chunks = chunk_text(text, source=path, config=_CHUNK_CONFIG)

            # 每个 chunk 独立存入向量库（提高检索精度）
            for chunk in chunks:
                vector_store.add_document(
                    collection_name="documents",
                    doc_id=f"{path}::chunk{chunk.index}",
                    text=chunk.content,
                    metadata={
                        "path": path,
                        "type": file_record["file_type"],
                        "chunk_index": chunk.index,
                        "offset": chunk.offset,
                    },
                )

            # 同步写入 FTS5 全文索引（支持中文关键词检索）
            _index_chunks_to_fts(path, chunks, file_record["file_type"])

            # AI 摘要整篇文档（用于数据库存档）
            summary = ai.summarize(text)

            # 仅对用户自创内容提取人物信息（跳过第三方库/软件文档）
            if _is_user_content(path):
                info = ai.extract_profile_info(text, source=path)
                if info:
                    profile.merge_extracted(info)

            db.update_file_summary(path, summary)
            processed += 1
            print(f"[Vectorizer] 已处理({len(chunks)}块): {path}")

        except Exception as e:
            print(f"[Vectorizer] 处理失败 {path}: {e}")
            db.update_file_summary(path, f"处理失败: {e}")

    print(f"[Vectorizer] 本批处理完成: {processed}/{len(pending)}")
    return processed
