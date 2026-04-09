"""
tools/merge_contacts.py — 联系人跨源合并

将 wechat_contacts (wxid/nickname/remark) 与 contacts (email/display_name) 做名字匹配，
把 wechat_id 回填到 contacts 表，方便跨渠道聚合画像。

匹配策略（按优先级）：
1. 精确匹配: contacts.display_name == wechat_contacts.remark
2. 精确匹配: contacts.display_name == wechat_contacts.nickname
3. 模糊匹配: 名字字符集重叠 >= 80%（适应"王磊"vs"王磊博士"等情形）
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import config
from memory import db as main_db


def _name_similarity(a: str, b: str) -> float:
    """字符级 Jaccard 相似度（去除空格/称谓后缀）"""
    SUFFIXES = ("博士", "教授", "老师", "院士", "主任", "院长", "Dr", "Prof", "PhD")
    for s in SUFFIXES:
        a = a.replace(s, "").strip()
        b = b.replace(s, "").strip()
    if not a or not b:
        return 0.0
    set_a = set(a)
    set_b = set(b)
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return intersection / union if union else 0.0


def merge(dry_run: bool = False, threshold: float = 0.8) -> dict:
    stats = {"matched_exact": 0, "matched_fuzzy": 0, "skipped": 0, "total": 0}

    with main_db.get_conn() as conn:
        contacts = conn.execute(
            "SELECT id, display_name, email FROM contacts"
            " WHERE (wechat_id IS NULL OR wechat_id = '')"
        ).fetchall()

        wx_contacts = conn.execute(
            "SELECT wxid, nickname, remark FROM wechat_contacts"
            " WHERE is_group = 0"
        ).fetchall()

        stats["total"] = len(contacts)

        # Build lookup dicts for fast exact matching
        by_remark  = {}
        by_nickname = {}
        for wxid, nickname, remark in wx_contacts:
            if remark:
                by_remark.setdefault(remark.strip(), wxid)
            if nickname:
                by_nickname.setdefault(nickname.strip(), wxid)

        for contact_id, display_name, email in contacts:
            if not display_name:
                stats["skipped"] += 1
                continue

            name = display_name.strip()
            matched_wxid = None
            match_type = None

            # 1. Exact remark match
            if name in by_remark:
                matched_wxid = by_remark[name]
                match_type = "exact_remark"

            # 2. Exact nickname match
            elif name in by_nickname:
                matched_wxid = by_nickname[name]
                match_type = "exact_nickname"

            # 3. Fuzzy match
            else:
                best_score = 0.0
                best_wxid = None
                for wxid, nickname, remark in wx_contacts:
                    for candidate in filter(None, [remark, nickname]):
                        score = _name_similarity(name, candidate)
                        if score > best_score:
                            best_score = score
                            best_wxid = wxid
                if best_score >= threshold:
                    matched_wxid = best_wxid
                    match_type = f"fuzzy({best_score:.2f})"

            if matched_wxid:
                if match_type.startswith("exact"):
                    stats["matched_exact"] += 1
                else:
                    stats["matched_fuzzy"] += 1
                print(f"  [{match_type}] {name} ({email}) → {matched_wxid}")
                if not dry_run:
                    conn.execute(
                        "UPDATE contacts SET wechat_id=? WHERE id=?",
                        (matched_wxid, contact_id)
                    )
            else:
                stats["skipped"] += 1

        if not dry_run:
            conn.commit()

    print(f"\n[merge_contacts] {'[DRY RUN] ' if dry_run else ''}结果:")
    print(f"  精确匹配: {stats['matched_exact']} 人")
    print(f"  模糊匹配: {stats['matched_fuzzy']} 人")
    print(f"  未匹配:   {stats['skipped']} 人")
    print(f"  总联系人: {stats['total']} 人")
    return stats


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="联系人跨源合并")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--threshold", type=float, default=0.8,
                        help="模糊匹配相似度阈值 (default: 0.8)")
    args = parser.parse_args()
    merge(dry_run=args.dry_run, threshold=args.threshold)
