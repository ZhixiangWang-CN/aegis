"""
中文分词器 — 基于 jieba，带医学/学术词典支持
用于 FTS5 全文检索的预分词处理
"""
from __future__ import annotations

import re
from pathlib import Path

# jieba 为可选依赖，未安装时降级
_jieba = None
_jieba_ready = False

def _init_jieba():
    global _jieba, _jieba_ready
    if _jieba_ready:
        return _jieba is not None
    _jieba_ready = True
    try:
        import jieba
        import jieba.posseg as pseg  # noqa: F401

        # 加载医学/学术自定义词典
        dict_path = Path(__file__).parent.parent / "config" / "medical_dict.txt"
        if dict_path.exists():
            jieba.load_userdict(str(dict_path))

        # 关闭 jieba 的 INFO 输出
        import logging
        logging.getLogger("jieba").setLevel(logging.WARNING)

        _jieba = jieba
        return True
    except ImportError:
        print("[tokenizer_cn] jieba 未安装，将使用字符级 fallback。"
              "安装: pip install jieba")
        _jieba = None
        return False


# 常见停用词（精简版，避免过滤有意义词汇）
_STOPWORDS = frozenset({
    "的", "了", "在", "是", "我", "有", "和", "就", "不", "人",
    "都", "一", "一个", "上", "也", "很", "到", "说", "要", "去",
    "你", "会", "着", "没有", "看", "好", "自己", "这", "那",
    "他", "她", "它", "们", "来", "过", "把", "被", "给", "让",
    "从", "以", "及", "或", "与", "但", "而", "并", "等", "等等",
    "中", "为", "对", "于", "其", "所", "此", "这个", "那个",
    "可以", "可能", "需要", "如果", "因为", "所以", "然后",
})


def segment(text: str) -> str:
    """
    jieba 分词，返回空格连接的词语字符串（供 FTS5 索引存储用）。
    - 过滤单字（英文单词除外）
    - 过滤停用词
    - 英文单词保留原样
    """
    if not text or not text.strip():
        return ""

    if _init_jieba():
        try:
            words = _jieba.cut(text, cut_all=False)
            result = []
            for w in words:
                w = w.strip()
                if not w:
                    continue
                # 英文/数字：保留长度 >= 2 的
                if re.match(r'^[a-zA-Z0-9_\-\.]+$', w):
                    if len(w) >= 2:
                        result.append(w)
                    continue
                # 停用词过滤
                if w in _STOPWORDS:
                    continue
                # 单字过滤（中文）
                if len(w) == 1:
                    continue
                result.append(w)
            return " ".join(result)
        except Exception as e:
            print(f"[tokenizer_cn] jieba 分词失败，使用 fallback: {e}")

    # fallback: bigram 字符切分
    return _bigram_segment(text)


def segment_for_search(query: str) -> list[str]:
    """
    对搜索 query 进行分词，返回词语列表（供 FTS5 查询构造用）。
    比 segment() 更激进地保留词语（不过滤单字中文关键词）。
    """
    if not query or not query.strip():
        return []

    if _init_jieba():
        try:
            words = _jieba.cut(query, cut_all=False)
            result = []
            seen = set()
            for w in words:
                w = w.strip()
                if not w or w in seen:
                    continue
                seen.add(w)
                # 英文/数字：保留长度 >= 2
                if re.match(r'^[a-zA-Z0-9_\-\.]+$', w):
                    if len(w) >= 2:
                        result.append(w)
                    continue
                # 对搜索保留单字，但去掉常见虚词
                if w in _STOPWORDS:
                    continue
                result.append(w)
            return result if result else _bigram_list(query)
        except Exception as e:
            print(f"[tokenizer_cn] segment_for_search 失败，使用 fallback: {e}")

    return _bigram_list(query)


def _bigram_segment(text: str) -> str:
    """fallback: 2-gram 滑动窗口 + 英文单词"""
    tokens = set()
    # 英文单词
    for m in re.finditer(r'[a-zA-Z0-9]{2,}', text):
        tokens.add(m.group())
    # 中文 bigram
    chinese = [c for c in text if '\u4e00' <= c <= '\u9fff']
    for i in range(len(chinese) - 1):
        tokens.add(chinese[i] + chinese[i + 1])
    return " ".join(tokens)


def _bigram_list(text: str) -> list[str]:
    """fallback: 返回 bigram 列表"""
    tokens = []
    seen = set()
    for m in re.finditer(r'[a-zA-Z0-9]{2,}', text):
        w = m.group()
        if w not in seen:
            seen.add(w)
            tokens.append(w)
    chinese = [c for c in text if '\u4e00' <= c <= '\u9fff']
    for i in range(len(chinese) - 1):
        bg = chinese[i] + chinese[i + 1]
        if bg not in seen:
            seen.add(bg)
            tokens.append(bg)
    return tokens
