"""
tools/cleanup_focus.py — 清理 focus.md

规则：
1. [x] 已完成项 → 归档到 archive/focus_archive.md
2. 截止日期已过的 [ ] 项 → 归档
3. 🟡 重复邮件通知（同主题保留一条）→ 归档
4. 归档后 focus.md 保留 ≤15 条活跃项
"""
from __future__ import annotations

import re
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

# 确保可以 import 项目模块
sys.path.insert(0, str(Path(__file__).parent.parent))

import config
from memory.writer import get_writer

FOCUS_PATH   = config.DATA_DIR / "memory" / "focus.md"
MEMORY_DIR   = config.DATA_DIR / "memory"
ARCHIVE_DIR  = MEMORY_DIR / "archive"
ARCHIVE_PATH = ARCHIVE_DIR / "focus_archive.md"

TODAY = date.today()


def _extract_deadline(line: str) -> date | None:
    """从行中提取 (截止:YYYY-MM-DD) 格式的日期"""
    m = re.search(r'\(截止:(\d{4}-\d{2}-\d{2})\)', line)
    if m:
        try:
            return date.fromisoformat(m.group(1))
        except ValueError:
            pass
    return None


def _normalize_email_key(line: str) -> str:
    """提取邮件通知的核心主题用于去重，取方括号地址+内容前20字"""
    m = re.search(r'邮件\[([^\]]+)\]:\s*(.+)', line)
    if m:
        # 用地址 + 内容关键词去重
        addr = m.group(1)
        content_key = m.group(2)[:30]
        return f"{addr}:{content_key}"
    return line[:60]


def cleanup(dry_run: bool = False) -> dict:
    if not FOCUS_PATH.exists():
        print(f"[cleanup_focus] focus.md 不存在: {FOCUS_PATH}")
        return {}

    text = FOCUS_PATH.read_text(encoding="utf-8")
    lines = text.splitlines()

    keep_lines   = []
    archive_items = []
    current_section = ""

    # 去重邮件通知：同地址+相似内容只保留第一条
    seen_email_keys: dict[str, str] = {}

    for line in lines:
        stripped = line.strip()

        # 段落标题
        if stripped.startswith("##"):
            current_section = stripped
            keep_lines.append(line)
            continue

        # 非条目行（空行、标题行）
        if not stripped.startswith("- "):
            keep_lines.append(line)
            continue

        # ── 判断是否归档 ──

        # 1. [x] 已完成
        if re.match(r'- \[x\]', stripped, re.IGNORECASE):
            archive_items.append(f"{line}  ← 已完成 @ {TODAY}")
            continue

        # 2. 截止日期已过
        deadline = _extract_deadline(stripped)
        if deadline and deadline < TODAY:
            archive_items.append(f"{line}  ← 截止已过 ({deadline})")
            continue

        # 3. 重复邮件通知
        if "🟡 邮件[" in stripped:
            key = _normalize_email_key(stripped)
            # 同地址判断：取前缀相似
            addr_match = re.search(r'邮件\[([^\]]+)\]', stripped)
            if addr_match:
                addr = addr_match.group(1)
                # 同一地址+高度相似内容才去重
                same_addr_keys = [k for k in seen_email_keys if k.startswith(addr + ":")]
                is_dup = False
                for existing_key in same_addr_keys:
                    # 简单：同地址且内容前15字相同
                    if key[:50] == existing_key[:50]:
                        is_dup = True
                        break
                if is_dup:
                    archive_items.append(f"{line}  ← 重复通知")
                    continue
                seen_email_keys[key] = line

        keep_lines.append(line)

    # 统计活跃条目数
    active_count = sum(
        1 for l in keep_lines
        if l.strip().startswith("- [") and "- [x]" not in l.lower()
    )

    print(f"[cleanup_focus] 归档 {len(archive_items)} 条，保留活跃条目 {active_count} 条")
    for item in archive_items:
        print(f"  归档: {item[:80]}".encode("utf-8", errors="replace").decode("utf-8", errors="replace"))

    if dry_run:
        print("[dry_run] 未写入文件")
        return {"archived": len(archive_items), "active": active_count}

    # 写入归档文件
    if archive_items:
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        archive_header = f"\n\n## 归档批次 @ {ts}\n"
        archive_block = archive_header + "\n".join(archive_items) + "\n"

        if ARCHIVE_PATH.exists():
            existing = ARCHIVE_PATH.read_text(encoding="utf-8")
            archive_content = existing.rstrip() + archive_block
        else:
            archive_content = f"# focus.md 归档记录\n" + archive_block

        rel_archive = str(ARCHIVE_PATH.relative_to(MEMORY_DIR))
        get_writer().write(rel_archive, "update", archive_content, "cleanup_focus")

    # 写回 focus.md（去掉多余空行）
    new_text = "\n".join(keep_lines).rstrip() + "\n"
    # 更新时间戳
    new_text = re.sub(
        r'> 更新: \d{4}-\d{2}-\d{2}',
        f'> 更新: {TODAY}',
        new_text,
    )
    get_writer().write("focus.md", "update", new_text, "cleanup_focus")
    print(f"[cleanup_focus] focus.md 已更新，归档至 {ARCHIVE_PATH}")

    return {"archived": len(archive_items), "active": active_count}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="清理 focus.md")
    parser.add_argument("--dry-run", action="store_true", help="只预览不写入")
    args = parser.parse_args()
    result = cleanup(dry_run=args.dry_run)
    print(f"结果: {result}")
