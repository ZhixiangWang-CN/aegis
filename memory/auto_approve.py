"""
v3 自动审批规则
Layer 1 永不自动；Layer 4 立即；Layer 2 有条件（邮件来源 + 高置信度 + 4h 延迟）。
"""
from __future__ import annotations

from datetime import datetime, timedelta

# ── 规则配置 ──────────────────────────────────────────────────────────────────

AUTO_APPROVE_RULES: dict[str, dict] = {
    "layer1_focus": {
        "auto": True,
        "min_confidence": 0.6,         # 置信度 >= 0.6 即自动写入
        "delay_hours": 0,              # 立即通过，不等待
    },
    "layer1_people": {
        "auto": False,                 # 联系人信息仍需手动确认
    },
    "layer1_decision": {
        "auto": True,
        "min_confidence": 0.8,
        "delay_hours": 24,
    },
    "layer2_project": {
        "auto": True,
        "min_confidence": 0.85,
        "source_whitelist": ["email"],  # 仅邮件来源可自动通过
        "delay_hours": 4,              # 提取后至少 4 小时才能自动通过
    },
    "layer4": {
        "auto": True,
        "min_confidence": 0.6,
        "delay_hours": 0,              # 立即通过
    },
}

LAYER1_EXPIRE_HOURS = 48  # Layer 1 pending 超过此时间 → expired


# ── 核心判断 ──────────────────────────────────────────────────────────────────

def should_auto_approve(item: dict) -> bool:
    """
    判断一条 pending 条目是否满足自动审批条件。

    item 字段来自 memory_pending 表，必须包含：
      proposed_layer, confidence, source, extracted_at, status, auto_approve
    """
    layer = item.get("proposed_layer", "")
    rule = AUTO_APPROVE_RULES.get(layer)

    if rule is None:
        return False
    if not rule.get("auto", False):
        return False

    # 置信度检查
    min_conf = rule.get("min_confidence", 0.6)
    if float(item.get("confidence", 0)) < min_conf:
        return False

    # 来源白名单检查
    whitelist = rule.get("source_whitelist")
    if whitelist and item.get("source") not in whitelist:
        return False

    # 延迟检查
    delay_hours = rule.get("delay_hours", 0)
    if delay_hours > 0:
        extracted_at_str = item.get("extracted_at", "")
        if extracted_at_str:
            try:
                extracted_at = datetime.fromisoformat(extracted_at_str)
                eligible_at = extracted_at + timedelta(hours=delay_hours)
                if datetime.now() < eligible_at:
                    return False
            except ValueError:
                pass  # 日期解析失败，忽略延迟检查

    return True


def process_auto_approvals() -> int:
    """
    扫描所有 pending 条目，对满足规则的条目自动批准（status='auto_approved'）。
    返回本次自动批准的数量。
    """
    try:
        from memory import db as main_db

        with main_db.get_conn() as conn:
            rows = conn.execute("""
                SELECT id, proposed_layer, confidence, source, extracted_at,
                       status, auto_approve
                FROM memory_pending
                WHERE status = 'pending'
                ORDER BY extracted_at ASC
            """).fetchall()

        approved_count = 0
        for row in rows:
            item = dict(row)
            if should_auto_approve(item):
                with main_db.get_conn() as conn:
                    n = conn.execute("""
                        UPDATE memory_pending
                        SET status='auto_approved',
                            reviewed_at=?,
                            notes='auto-approved by rule'
                        WHERE id=? AND status='pending'
                    """, (datetime.now().isoformat(), item["id"])).rowcount
                if n:
                    approved_count += 1

        if approved_count:
            print(f"[auto_approve] 自动批准 {approved_count} 条 pending")

        return approved_count

    except Exception as e:
        print(f"[auto_approve] process_auto_approvals 失败: {e}")
        return 0


def expire_stale_layer1() -> int:
    """
    将超过 LAYER1_EXPIRE_HOURS 小时未处理的 Layer 1 pending 条目标为 expired。
    返回过期数量。
    """
    try:
        from memory import db as main_db

        cutoff = (datetime.now() - timedelta(hours=LAYER1_EXPIRE_HOURS)).isoformat()
        with main_db.get_conn() as conn:
            n = conn.execute("""
                UPDATE memory_pending
                SET status='expired',
                    notes='auto-expired (Layer 1 ' || ? || 'h timeout)'
                WHERE status='pending'
                  AND proposed_layer IN ('layer1_focus','layer1_people','layer1_decision')
                  AND extracted_at <= ?
            """, (str(LAYER1_EXPIRE_HOURS), cutoff)).rowcount

        if n:
            print(f"[auto_approve] Layer 1 过期 {n} 条")
        return n

    except Exception as e:
        print(f"[auto_approve] expire_stale_layer1 失败: {e}")
        return 0
