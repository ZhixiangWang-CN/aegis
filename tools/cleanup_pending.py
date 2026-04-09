"""
tools/cleanup_pending.py — 清理 memory_pending 积压

操作：
1. 删除重复条目（同 content，仅保留 id 最小的一条）
2. 将明显的通讯列表/期刊邮件标记为 expired
3. 自动 apply confidence >= 0.9 且非 newsletter 的 person 条目
4. 超过 30 天未处理的 pending → 标记 expired
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import config
from memory import db as main_db

# 通讯列表关键词（这些都是自动化邮件，不是真实联系人）
NEWSLETTER_KEYWORDS = [
    "journal", "newsletter", "noreply", "no-reply", "no_reply",
    "updates@", "notification", "alert@", "digest@", "mailer",
    "jamanetwork", "pubmed", "elsevier", "springer", "wiley",
    "donotreply", "do-not-reply", "bounce",
]

EXPIRE_AFTER_DAYS = 30


def _is_newsletter(content: str, item_data: str) -> bool:
    """判断是否为通讯列表/期刊自动邮件"""
    text = (content + " " + (item_data or "")).lower()
    return any(kw in text for kw in NEWSLETTER_KEYWORDS)


def cleanup(dry_run: bool = False) -> dict:
    stats = {
        "dedup_removed": 0,
        "newsletter_expired": 0,
        "auto_applied": 0,
        "too_old_expired": 0,
    }

    with main_db.get_conn() as conn:
        # ── 1. 去重：同 content 保留最小 id ──────────────────────────────────
        dups = conn.execute("""
            SELECT content, MIN(id) as keep_id, COUNT(*) as cnt
            FROM memory_pending WHERE status='pending'
            GROUP BY content HAVING cnt > 1
        """).fetchall()

        for content, keep_id, cnt in dups:
            if not dry_run:
                conn.execute("""
                    UPDATE memory_pending SET status='expired', notes='重复条目去重'
                    WHERE content=? AND status='pending' AND id != ?
                """, (content, keep_id))
            stats["dedup_removed"] += cnt - 1

        # ── 2. Newsletter → expired ───────────────────────────────────────────
        pending_rows = conn.execute("""
            SELECT id, content, item_data FROM memory_pending WHERE status='pending'
        """).fetchall()

        newsletter_ids = []
        high_conf_ids = []
        now = datetime.now()

        for row_id, content, item_data in pending_rows:
            if _is_newsletter(content or "", item_data or ""):
                newsletter_ids.append(row_id)

        if newsletter_ids and not dry_run:
            placeholders = ",".join("?" * len(newsletter_ids))
            conn.execute(f"""
                UPDATE memory_pending SET status='expired', notes='通讯列表/期刊邮件'
                WHERE id IN ({placeholders})
            """, newsletter_ids)
        stats["newsletter_expired"] = len(newsletter_ids)

        # ── 3. 超过 N 天未处理 → expired ─────────────────────────────────────
        cutoff = (now - timedelta(days=EXPIRE_AFTER_DAYS)).isoformat()
        old_rows = conn.execute("""
            SELECT COUNT(*) FROM memory_pending
            WHERE status='pending' AND extracted_at < ?
        """, (cutoff,)).fetchone()[0]

        if not dry_run and old_rows > 0:
            conn.execute("""
                UPDATE memory_pending SET status='expired', notes='超过30天未处理'
                WHERE status='pending' AND extracted_at < ?
            """, (cutoff,))
        stats["too_old_expired"] = old_rows

        # ── 4. 高置信度非 newsletter person 条目 → auto apply ────────────────
        # (只统计，不自动写 memory，需人工或调用 layers.apply_pending)
        high_conf = conn.execute("""
            SELECT COUNT(*) FROM memory_pending
            WHERE status='pending' AND confidence >= 0.9
            AND item_type='person'
        """).fetchone()[0]
        stats["auto_applied"] = high_conf  # 记录数量，不实际 apply（避免噪音）

        if not dry_run:
            conn.commit()

    print(f"[cleanup_pending] {'[DRY RUN] ' if dry_run else ''}结果:")
    print(f"  去重删除:       {stats['dedup_removed']} 条")
    print(f"  Newsletter过期: {stats['newsletter_expired']} 条")
    print(f"  超期过期:       {stats['too_old_expired']} 条")
    print(f"  高置信可apply:  {stats['auto_applied']} 条（未自动执行）")

    # 最终剩余
    with main_db.get_conn() as conn:
        remaining = conn.execute(
            "SELECT COUNT(*) FROM memory_pending WHERE status='pending'"
        ).fetchone()[0]
    print(f"  剩余pending:    {remaining} 条")
    stats["remaining"] = remaining
    return stats


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="清理 memory_pending 积压")
    parser.add_argument("--dry-run", action="store_true", help="只预览不写入")
    args = parser.parse_args()
    result = cleanup(dry_run=args.dry_run)
