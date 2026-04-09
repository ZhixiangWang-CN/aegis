"""
Layer 1 Token 预算管理器
控制注入 AI context 的第一层记忆总 token 数，避免超限。

豆包 / doubao 不完全兼容 tiktoken，使用字符级粗估：
  - 中文字符 ≈ 1.5 tokens
  - 英文/数字字符 ≈ 0.25 tokens
  - 其他字符 ≈ 0.5 tokens
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

# ── 配置 ─────────────────────────────────────────────────────────────────────

LAYER1_BUDGET = 1200  # tokens 硬上限

LAYER1_SLOTS = [
    {"file": "self.md",      "max_tokens": 200,  "truncate": "tail"},
    {"file": "focus.md",     "max_tokens": 400,  "truncate": "by_priority"},
    {"file": "people.md",    "max_tokens": 300,  "truncate": "tail"},
    {"file": "decisions.md", "max_tokens": 300,  "truncate": "keep_principles"},
]

# ── Token 估算 ────────────────────────────────────────────────────────────────

def estimate_tokens(text: str) -> int:
    """
    粗估 token 数（字符级）。
    1 中文字符 ≈ 1.5 tokens
    1 英文/数字字符 ≈ 0.25 tokens
    其他字符 ≈ 0.5 tokens
    """
    if not text:
        return 0
    count = 0.0
    for ch in text:
        cp = ord(ch)
        if 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF:
            # CJK 统一汉字
            count += 1.5
        elif 0x3000 <= cp <= 0x303F or 0xFF00 <= cp <= 0xFFEF:
            # 全角标点
            count += 0.5
        elif ch.isascii():
            count += 0.25
        else:
            count += 0.5
    return max(1, int(count))


# ── 裁剪策略 ──────────────────────────────────────────────────────────────────

def truncate_focus(text: str, max_tokens: int) -> str:
    """
    focus.md 优先级裁剪：先保 🔴，再 🟡，再 ⏳，最后其他。
    保留文件头（# 标题 + > 注释行）不受优先级影响。
    """
    if estimate_tokens(text) <= max_tokens:
        return text

    lines = text.splitlines(keepends=True)

    # 分离 header（标题 + 注释行）和 body
    header_lines: list[str] = []
    body_lines: list[str] = []
    in_header = True
    for line in lines:
        stripped = line.lstrip()
        if in_header and (stripped.startswith("#") or stripped.startswith(">")):
            header_lines.append(line)
        else:
            in_header = False
            body_lines.append(line)

    header_text = "".join(header_lines)
    header_tokens = estimate_tokens(header_text)
    remaining = max_tokens - header_tokens

    if remaining <= 0:
        return header_text[:max_tokens * 3]  # 粗截断保险

    # 按优先级桶分类
    buckets: dict[str, list[str]] = {
        "urgent":  [],   # 🔴
        "normal":  [],   # 🟡
        "waiting": [],   # ⏳
        "other":   [],
    }
    for line in body_lines:
        if "🔴" in line:
            buckets["urgent"].append(line)
        elif "🟡" in line:
            buckets["normal"].append(line)
        elif "⏳" in line:
            buckets["waiting"].append(line)
        else:
            buckets["other"].append(line)

    result_lines: list[str] = []
    used = 0
    for bucket_key in ("urgent", "normal", "waiting", "other"):
        for line in buckets[bucket_key]:
            t = estimate_tokens(line)
            if used + t <= remaining:
                result_lines.append(line)
                used += t
            # 超出时跳过而不截断（保持行完整）

    return header_text + "".join(result_lines)


def truncate_decisions(text: str, max_tokens: int) -> str:
    """
    decisions.md 裁剪：含"原则"的行优先保留，其余按时间倒序（后写的更重要）。
    """
    if estimate_tokens(text) <= max_tokens:
        return text

    lines = text.splitlines(keepends=True)

    # 分离 header
    header_lines: list[str] = []
    body_lines: list[str] = []
    in_header = True
    for line in lines:
        stripped = line.lstrip()
        if in_header and (stripped.startswith("#") or stripped.startswith(">")):
            header_lines.append(line)
        else:
            in_header = False
            body_lines.append(line)

    header_text = "".join(header_lines)
    header_tokens = estimate_tokens(header_text)
    remaining = max_tokens - header_tokens

    if remaining <= 0:
        return header_text

    # 含"原则"的行 → 高优先级桶
    principle_lines: list[str] = []
    normal_lines: list[str] = []
    for line in body_lines:
        if "原则" in line:
            principle_lines.append(line)
        else:
            normal_lines.append(line)

    # normal_lines 倒序（最新的先加）
    normal_lines_rev = list(reversed(normal_lines))

    result_lines: list[str] = []
    used = 0
    for line in principle_lines + normal_lines_rev:
        t = estimate_tokens(line)
        if used + t <= remaining:
            result_lines.append(line)
            used += t

    return header_text + "".join(result_lines)


def _truncate_tail(text: str, max_tokens: int) -> str:
    """简单 tail 截断：保留开头（header + 尽可能多内容），超出时截尾"""
    if estimate_tokens(text) <= max_tokens:
        return text
    # 粗估字符数（1 token ≈ 平均 1.2 字符，偏保守）
    char_budget = int(max_tokens / 1.2)
    return text[:char_budget] + "\n…（已截断）"


# ── 核心构建函数 ──────────────────────────────────────────────────────────────

def build_layer1(memory_dir: Path) -> str:
    """
    读取 LAYER1_SLOTS 定义的文件，在预算内组装 Layer 1 字符串。
    总 token 数不超过 LAYER1_BUDGET。

    返回可直接注入 AI system_prompt 的字符串。
    """
    parts: list[str] = []
    total_used = 0

    for slot in LAYER1_SLOTS:
        file_path = memory_dir / slot["file"]
        if not file_path.exists():
            continue

        try:
            raw = file_path.read_text(encoding="utf-8")
        except Exception as e:
            print(f"[token_budget] 读取 {slot['file']} 失败: {e}")
            continue

        max_tok = slot["max_tokens"]
        strategy: str = slot["truncate"]

        # 应用裁剪策略
        if strategy == "by_priority":
            text = truncate_focus(raw, max_tok)
        elif strategy == "keep_principles":
            text = truncate_decisions(raw, max_tok)
        else:
            # "tail" 或未知策略
            text = _truncate_tail(raw, max_tok)

        tok = estimate_tokens(text)

        # 检查总预算
        if total_used + tok > LAYER1_BUDGET:
            # 尝试在剩余预算内压缩
            remaining_budget = LAYER1_BUDGET - total_used
            if remaining_budget <= 30:
                break  # 已无空间
            text = _truncate_tail(text, remaining_budget)
            tok = estimate_tokens(text)

        parts.append(text.strip())
        total_used += tok

        if total_used >= LAYER1_BUDGET:
            break

    return "\n\n---\n\n".join(p for p in parts if p)
