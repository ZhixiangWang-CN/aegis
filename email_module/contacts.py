"""
联系人智能系统 — v3
从邮件中提取、分类、积累重要联系人，写入统一 contacts 表。
重要联系人（importance >= 70）自动推送到 pending 队列等待写入 people.md。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
import config
from memory import db
from ai import client as ai

# ── 机构类型识别 ──────────────────────────────────────────────────────────────

INSTITUTION_RULES = [
    ("university", [".edu", ".edu.cn", ".ac.cn", ".ac.uk", ".ac.jp",
                    "university", "univ.", "college", "institute"]),
    ("journal",    ["elsevier", "springer", "wiley", "nature.com", "cell.com",
                    "thelancet", "nejm", "bmj", "jama", "tandfonline",
                    "sagepub", "oxford", "cambridge", "ieee", "acm", "plos",
                    "frontiersin", "mdpi", "hindawi"]),
    ("hospital",   ["hospital", "hosp.", "medical center", "clinic",
                    "healthcare", "health system"]),
    ("government", ["nih.gov", "cdc.gov", "who.int", "nsfc.gov.cn",
                    ".gov", ".gov.cn"]),
    ("company",    [".com", ".co."]),
    ("personal",   ["gmail.com", "163.com", "126.com", "qq.com",
                    "hotmail.com", "outlook.com", "yahoo.com"]),
]

ROLE_KEYWORDS = {
    "journal_editor": ["editorial", "editor", "manuscript", "submission",
                       "accept", "reject", "revision", "review decision"],
    "reviewer":       ["peer review", "reviewer", "review request", "审稿", "外审"],
    "collaborator":   ["collaboration", "joint", "合作", "共同"],
    "conference":     ["conference", "workshop", "symposium", "call for papers", "会议"],
    "advisor":        ["supervisor", "advisor", "导师", "professor", "prof."],
    "student":        ["student", "phd", "master", "同学", "学生"],
}

# 邮件角色 → v3 统一角色（用于 people.md 和 wechat 对齐）
EMAIL_ROLE_TO_V3 = {
    "advisor":        "superior",
    "journal_editor": "collaborator",
    "collaborator":   "collaborator",
    "reviewer":       "colleague",
    "conference":     "colleague",
    "student":        "junior",
    "contact":        "unknown",
}


def _detect_institution_type(email_addr: str, display_name: str = "") -> tuple[str, str]:
    """返回 (institution_type, institution_name)"""
    combined = (email_addr + " " + display_name).lower()
    for inst_type, keywords in INSTITUTION_RULES:
        if any(kw in combined for kw in keywords):
            domain = email_addr.lower().split("@")[-1] if "@" in email_addr else ""
            parts = domain.split(".")
            institution = parts[-2] if len(parts) >= 2 else domain
            return inst_type, institution.capitalize()
    return "unknown", ""


def _detect_email_role(subject: str, body_preview: str = "") -> str:
    text = (subject + " " + body_preview).lower()
    for role, keywords in ROLE_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            return role
    return "contact"


def _calc_importance(inst_type: str, email_count: int,
                     last_contact: str, email_role: str) -> int:
    """返回 0-100 的 importance 分数（≥70 写入 people.md）"""
    score = 20  # 基础分

    if inst_type in ("university", "journal", "hospital", "government"):
        score += 15
    if inst_type == "journal":
        score += 10
    if email_role in ("journal_editor", "advisor", "collaborator"):
        score += 15
    if email_count >= 3:
        score += 10
    if email_count >= 10:
        score += 10
    if last_contact:
        try:
            last_dt = datetime.fromisoformat(last_contact)
            if datetime.now() - last_dt < timedelta(days=30):
                score += 10
        except Exception:
            pass

    return min(score, 95)  # 100 留给手动置顶


def upsert_contact(email_addr: str, display_name: str,
                   subject: str = "", body_preview: str = ""):
    """
    处理一封邮件后调用，更新统一 contacts 表。
    importance >= 70 时推送到 pending 等待写入 people.md。
    """
    if not email_addr or "@" not in email_addr:
        return
    own_emails = {config.NETEASE_EMAIL, config.GMAIL_EMAIL or ""}
    if email_addr in own_emails:
        return
    if any(kw in email_addr.lower() for kw in ["noreply", "no-reply", "mailer-daemon",
                                                 "postmaster", "bounce"]):
        return

    inst_type, institution = _detect_institution_type(email_addr, display_name)
    email_role  = _detect_email_role(subject, body_preview)
    v3_role     = EMAIL_ROLE_TO_V3.get(email_role, "unknown")
    now         = datetime.now().isoformat()
    name        = display_name or email_addr.split("@")[0]

    with db.get_conn() as conn:
        existing = conn.execute(
            "SELECT rowid as id, email_count, importance, last_seen, notes, tags FROM contacts WHERE email=?",
            (email_addr,)
        ).fetchone()

        if existing:
            old_count  = existing["email_count"] or 0
            new_count  = old_count + 1
            importance = _calc_importance(inst_type, new_count, now, email_role)

            # 更新 tags（追加话题）
            old_tags = json.loads(existing["tags"] or "[]")
            if subject and subject not in old_tags:
                old_tags.append(subject[:60])
                old_tags = old_tags[-20:]

            conn.execute("""
                UPDATE contacts SET
                    display_name = COALESCE(NULLIF(?, ''), display_name),
                    role         = CASE WHEN role='unknown' THEN ? ELSE role END,
                    importance   = MAX(importance, ?),
                    institution  = COALESCE(NULLIF(?, ''), institution),
                    institution_type = ?,
                    email_count  = ?,
                    last_seen    = ?,
                    tags         = ?
                WHERE rowid = ?
            """, (name, v3_role, importance, institution, inst_type,
                  new_count, now, json.dumps(old_tags, ensure_ascii=False),
                  existing["id"]))

            contact_id = existing["id"]
            importance_for_pending = importance
        else:
            importance = _calc_importance(inst_type, 1, now, email_role)
            tags       = json.dumps([subject[:60]] if subject else [], ensure_ascii=False)

            cur = conn.execute("""
                INSERT INTO contacts
                (display_name, email, role, importance, institution, institution_type,
                 email_count, first_seen, last_seen, tags)
                VALUES (?,?,?,?,?,?,1,?,?,?)
            """, (name, email_addr, v3_role, importance, institution, inst_type,
                  now, now, tags))
            contact_id = cur.lastrowid
            importance_for_pending = importance

    # importance >= 70 → 推送到 pending，等待写入 people.md
    if importance_for_pending >= 70:
        try:
            from memory.pending import add_person
            add_person(
                name=name, role=v3_role, source="email",
                note=f"{institution}（{inst_type}），往来邮件",
                email=email_addr,
                confidence=min(importance_for_pending / 100, 0.9),
            )
        except Exception:
            pass

    return contact_id


def get_important_contacts(min_importance: int = 70) -> list[dict]:
    """返回重要联系人（0-100 scale，默认 ≥70）"""
    return db.get_contacts_by_importance(min_importance=min_importance)


def get_contacts_summary() -> str:
    """生成联系人摘要，用于注入日报"""
    with db.get_conn() as conn:
        type_rows = conn.execute("""
            SELECT institution_type, COUNT(*) as cnt
            FROM contacts WHERE email IS NOT NULL
            GROUP BY institution_type ORDER BY cnt DESC
        """).fetchall()
        top_rows = conn.execute("""
            SELECT display_name, email, institution, role, email_count, importance
            FROM contacts WHERE importance >= 70
            ORDER BY importance DESC LIMIT 5
        """).fetchall()

    if not type_rows:
        return "暂无联系人数据"

    type_summary = ", ".join(
        f"{r['institution_type']}({r['cnt']})" for r in type_rows if r['institution_type']
    )
    top_list = "\n".join(
        f"  [{r['importance']}] {r['display_name'] or r['email']} "
        f"| {r['institution'] or '—'} | {r['role']} | 往来{r['email_count']}封"
        for r in top_rows
    )
    return f"联系人分布: {type_summary}\n重要联系人:\n{top_list}"


def enrich_contact_with_ai(email_addr: str):
    """对重要联系人做 AI 深度分析，生成关系备注（notes 字段）"""
    with db.get_conn() as conn:
        contact = conn.execute("""
            SELECT * FROM contacts
            WHERE email=? AND importance>=70 AND (notes IS NULL OR notes='')
        """, (email_addr,)).fetchone()

    if not contact:
        return

    c = dict(contact)
    tags = json.loads(c.get("tags") or "[]")

    prompt = (
        f"根据以下信息，用一句话总结这位联系人与用户的关系：\n"
        f"姓名: {c['display_name']}\n邮箱: {c['email']}\n"
        f"机构: {c.get('institution', '—')} ({c.get('institution_type', '—')})\n"
        f"角色: {c['role']}\n往来邮件数: {c['email_count']}\n"
        f"近期话题: {', '.join(tags[:5])}\n\n"
        "输出格式: 一句话20字以内，如「Nature期刊编辑，负责你的投稿审核」。只输出这一句话。"
    )
    notes = ai.chat([{"role": "user", "content": prompt}], temperature=0.3)

    with db.get_conn() as conn:
        conn.execute(
            "UPDATE contacts SET notes=? WHERE email=?",
            (notes, email_addr)
        )
