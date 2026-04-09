"""
邮件记忆提取器

把数据库里的所有邮件过一遍，提取：
  1. 重要联系人画像（谁 / 关系 / 往来内容）
  2. 项目与合作进展
  3. 关于用户本人的关键事实（职位、论文、基金、机构等）
  4. 待处理/待回复事项

输出到 data/memory/from_emails.md
同时把联系人画像写入 data/memory/contacts/（每人一个文件，重要性>=3）
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

import config
from ai import client as ai
from memory import db as main_db
from memory.writer import get_writer

MEMORY_DIR    = config.DATA_DIR / "memory"
CONTACTS_DIR  = MEMORY_DIR / "contacts"
EMAIL_MEM_PATH = MEMORY_DIR / "from_emails.md"

# 过滤掉纯广告/系统邮件的关键词
_SKIP_SENDER_KEYWORDS = {
    "noreply", "no-reply", "donotreply", "mailer-daemon", "postmaster",
    "newsletter", "marketing", "notifications", "updates", "alerts",
    "support", "info@", "hello@", "contact@", "bounce",
    "jobsdb", "linkedin", "twitter", "facebook", "youtube",
    "amazon", "adobe", "microsoft", "google", "apple", "volcengine",
    "ryanair", "booking", "airbnb", "offertoday", "jamanetwork",
}

def _is_real_contact(addr: str) -> bool:
    """判断是否是真实人类联系人（非自动化系统邮件）"""
    addr_lower = addr.lower()
    return not any(kw in addr_lower for kw in _SKIP_SENDER_KEYWORDS)


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


# ── AI Prompts ─────────────────────────────────────────────────────────────

_CONTACT_PROMPT = """\
你是一个邮件分析助手，帮助整理一个研究人员的邮件往来记忆。

以下是与某联系人的邮件往来记录（主题+摘要）。请提取：
1. 这个联系人是谁（姓名/机构/角色，尽量从邮件内容推断）
2. 与用户的关系（合作者/导师/学生/编辑/行政/服务/其他）
3. 往来的主要事务或项目
4. 最近的动态（最新一封在说什么）
5. 需要跟进的事项（如有）

输出格式（Markdown）：
## [联系人邮箱]

**身份**: （姓名/机构/角色）
**关系**: （与用户的关系）
**主要事务**: （1-3条最重要的往来内容）
**最新动态**: （最近一封的核心内容）
**待跟进**: （如无则省略）

只输出有实质内容的字段，没有信息的字段省略。"""


def _build_global_facts_prompt() -> str:
    """动态构建全局事实提取 prompt，注入用户配置信息"""
    import config as _cfg
    parts = []
    if _cfg.OWNER_FULL_NAME:
        parts.append(f"- 中文姓名：{_cfg.OWNER_FULL_NAME}")
    if _cfg.OWNER_EN_NAME:
        parts.append(f"- 英文姓名：{_cfg.OWNER_EN_NAME}")
    owner_hint = "\n".join(parts) if parts else "（未在 .credentials 中配置，请自行推断）"
    return _GLOBAL_FACTS_PROMPT.format(owner_hint=owner_hint)


_GLOBAL_FACTS_PROMPT = """\
你是一个信息提取助手。以下是一个研究人员的邮件摘要列表（subject + summary，按重要性排序）。

用户基本信息（已知，勿修改，来自 config.OWNER_FULL_NAME / OWNER_EN_NAME）：
{owner_hint}

请从邮件中提取关于**用户本人**的关键事实（不要重复已知信息，只补充新内容）：
- 用户的研究方向/领域（从合作邮件、投稿邮件推断）
- 用户所在机构/合作机构
- 用户正在推进的项目、论文投稿、基金申请
- 用户最近的重要决定或事件
- 用户常联系的机构/合作者所在地

**重要：用户中文名已在上方给出，不要猜测或列出其他中文名变体。**

不要提取他人信息，只关注能说明**用户本人**的事实。

输出格式：Markdown 列表，分节：
## 研究方向与领域
## 机构与合作
## 进行中的项目/投稿/基金
## 近期重要事件
## 其他事实

只写有实质内容的节。"""


_SUMMARY_PROMPT = """\
你是一个助手，把一批邮件联系人分析结果整理成简洁的记忆总览。

请生成：
1. 重要联系人列表（分类：学术合作者 / 期刊编辑 / 基金机构 / 导师学生 / 其他）
2. 从邮件往来推断的进行中项目
3. 需要用户关注或跟进的事项

格式简洁，用 Markdown。"""


# ── 数据加载 ──────────────────────────────────────────────────────────────

def _load_all_emails() -> list[dict]:
    """加载 DB 中所有有效邮件（至少有主题或摘要）"""
    with main_db.get_conn() as conn:
        rows = conn.execute("""
            SELECT id, from_addr, subject, summary, importance, date, body
            FROM emails
            WHERE (summary IS NOT NULL AND summary != '')
               OR (subject IS NOT NULL AND subject != '')
            ORDER BY importance DESC, date DESC
        """).fetchall()
    return [dict(r) for r in rows]


def _group_by_sender(emails: list[dict]) -> dict[str, list[dict]]:
    """按发件人分组"""
    groups: dict[str, list[dict]] = defaultdict(list)
    for em in emails:
        groups[em["from_addr"]].append(em)
    return dict(groups)


# ── 单联系人分析 ──────────────────────────────────────────────────────────

def _analyze_contact(addr: str, emails: list[dict]) -> Optional[str]:
    """对单个联系人的邮件列表做 AI 分析，返回 Markdown 画像"""
    # 构建输入文本（每封：日期 | 主题 | 摘要）
    lines = []
    for em in sorted(emails, key=lambda e: e.get("date") or "", reverse=True)[:30]:
        date_short = (em.get("date") or "")[:16]
        subj  = em.get("subject") or ""
        summ  = em.get("summary") or ""
        imp   = em.get("importance", 1)
        lines.append(f"[{date_short}] ★{imp} {subj}" + (f" — {summ}" if summ else ""))

    content = f"联系人: {addr}\n\n往来邮件（共{len(emails)}封，显示最近30封）:\n" + "\n".join(lines)

    try:
        result = ai.chat(
            messages=[{"role": "user", "content": content}],
            system_prompt=_CONTACT_PROMPT,
            temperature=0.2,
        )
        return result.strip() if result else None
    except Exception as e:
        print(f"[EmailMem] 分析 {addr} 失败: {e}")
        return None


# ── 全局事实提取 ──────────────────────────────────────────────────────────

def _extract_global_facts(emails: list[dict]) -> str:
    """从所有重要邮件中提取关于用户本人的事实"""
    # 只取 importance >= 2 的邮件，按重要性排序
    important = sorted(
        [e for e in emails if e.get("importance", 1) >= 2],
        key=lambda e: e.get("importance", 1),
        reverse=True,
    )[:200]  # 最多200封

    lines = []
    for em in important:
        date_short = (em.get("date") or "")[:10]
        subj  = em.get("subject") or ""
        summ  = em.get("summary") or ""
        imp   = em.get("importance", 1)
        lines.append(f"★{imp} [{date_short}] {subj}" + (f" — {summ[:100]}" if summ else ""))

    # 分批（每批约 6000 字符）
    batch_texts = []
    cur, cur_len = [], 0
    for line in lines:
        if cur_len + len(line) > 6000 and cur:
            batch_texts.append("\n".join(cur))
            cur, cur_len = [], 0
        cur.append(line)
        cur_len += len(line)
    if cur:
        batch_texts.append("\n".join(cur))

    all_facts = []
    for i, batch in enumerate(batch_texts):
        print(f"[EmailMem] 全局事实提取 {i+1}/{len(batch_texts)}...")
        try:
            result = ai.chat(
                messages=[{"role": "user", "content": batch}],
                system_prompt=_build_global_facts_prompt(),
                temperature=0.2,
            )
            if result and result.strip():
                all_facts.append(result.strip())
        except Exception as e:
            print(f"[EmailMem] 全局事实提取失败: {e}")

    if len(all_facts) > 1:
        # 多批结果合并一次
        merged_input = "\n\n---\n\n".join(all_facts)
        try:
            final = ai.chat(
                messages=[{"role": "user", "content": merged_input}],
                system_prompt="请将以下多份邮件分析结果合并整理为一份，去除重复，保留最完整信息。格式保持 Markdown 分节。",
                temperature=0.15,
            )
            return final.strip() if final else "\n".join(all_facts)
        except Exception:
            return "\n\n---\n\n".join(all_facts)
    elif all_facts:
        return all_facts[0]
    return "（无法提取）"


# ── 主入口 ────────────────────────────────────────────────────────────────

def build_email_memory(
    min_importance_for_contact: int = 2,
    min_emails_for_contact: int = 2,
) -> dict:
    """
    全量邮件记忆构建主入口。

    min_importance_for_contact: 联系人至少有一封邮件达到此重要性
    min_emails_for_contact:     联系人至少有这么多封邮件（减少噪音）
    """
    print(f"[EmailMem] 加载邮件数据...")
    emails = _load_all_emails()
    print(f"[EmailMem] 共 {len(emails)} 封邮件")

    # ── 1. 全局事实提取 ──
    print("[EmailMem] 提取全局事实（用户相关信息）...")
    global_facts = _extract_global_facts(emails)

    # ── 2. 联系人分析 ──
    by_sender = _group_by_sender(emails)

    # 筛选真实联系人（非系统邮件，有足够往来量，有足够重要性）
    real_contacts = {}
    for addr, ems in by_sender.items():
        if not _is_real_contact(addr):
            continue
        max_imp = max((e.get("importance", 1) for e in ems), default=1)
        if max_imp < min_importance_for_contact:
            continue
        if len(ems) < min_emails_for_contact:
            continue
        real_contacts[addr] = ems

    print(f"[EmailMem] 找到 {len(real_contacts)} 个真实联系人")

    # 按最高重要性排序
    sorted_contacts = sorted(
        real_contacts.items(),
        key=lambda kv: max(e.get("importance", 1) for e in kv[1]),
        reverse=True,
    )

    contact_profiles: list[str] = []
    CONTACTS_DIR.mkdir(parents=True, exist_ok=True)

    for i, (addr, ems) in enumerate(sorted_contacts):
        max_imp = max(e.get("importance", 1) for e in ems)
        print(f"[EmailMem] 分析联系人 {i+1}/{len(sorted_contacts)}: {addr[:40]} (★{max_imp}, {len(ems)}封)")
        profile = _analyze_contact(addr, ems)
        if not profile:
            continue

        contact_profiles.append(profile)

        # 重要性 >= 3 的联系人单独存一个文件
        if max_imp >= 3:
            safe_name = re.sub(r'[<>:"/\\|?*@.]', '_', addr)
            contact_path = CONTACTS_DIR / f"{safe_name}.md"
            file_content = (
                f"# 联系人: {addr}\n"
                f"> 邮件往来: {len(ems)}封 | 最高重要性: ★{max_imp} | 更新: {_ts()}\n\n"
                + profile
            )
            try:
                rel_path = str(contact_path.relative_to(MEMORY_DIR))
                get_writer().write(rel_path, "update", file_content, "email")
            except Exception:
                contact_path.write_text(file_content, encoding="utf-8")

    # ── 3. 写入 from_emails.md ──
    ts = _ts()
    sections = [
        f"# 记忆来源: 邮件",
        f"> 最后更新: {ts} | 共处理 {len(emails)} 封邮件 | 真实联系人 {len(real_contacts)} 人",
        "",
        "## 关于用户的关键事实",
        "",
        global_facts,
        "",
        "---",
        "",
        f"## 重要联系人画像（{len(contact_profiles)}人）",
        "",
    ]
    sections.extend(contact_profiles)

    email_content = "\n".join(sections)
    try:
        rel_path = str(EMAIL_MEM_PATH.relative_to(MEMORY_DIR))
        get_writer().write(rel_path, "update", email_content, "email")
    except Exception:
        EMAIL_MEM_PATH.write_text(email_content, encoding="utf-8")
    print(f"[EmailMem] 已写入: {EMAIL_MEM_PATH}")
    print(f"[EmailMem] 联系人档案: {CONTACTS_DIR} ({len([f for f in CONTACTS_DIR.glob('*.md')])} 个)")

    stats = {
        "emails_total": len(emails),
        "real_contacts": len(real_contacts),
        "profiles_written": len(contact_profiles),
        "contact_files": len(list(CONTACTS_DIR.glob("*.md"))),
    }
    return stats
