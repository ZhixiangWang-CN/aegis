"""
信息老化与清理模块
负责 focus 事项过期、项目陈旧提醒、pending 条目过期、people.md 重算
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

import config

MEMORY_DIR = config.DATA_DIR / "memory"
FOCUS_PATH = MEMORY_DIR / "focus.md"
PROJECTS_DIR = MEMORY_DIR / "projects"

# 无 DDL 的 focus 条目：14天后标 waiting，21天后删除
FOCUS_NO_DDL_WAITING_DAYS = 14
FOCUS_NO_DDL_REMOVE_DAYS = 21
# 60天无更新的项目发邮件提醒归档
PROJECT_STALE_DAYS = 60
# Layer 1 pending 超过 48h 过期
LAYER1_EXPIRE_HOURS = 48


# ── Focus 清理 ────────────────────────────────────────────────────────────────

def _parse_focus_line_date(line: str) -> datetime | None:
    """
    从 focus 条目行中提取 DDL 日期。
    格式: DDL YYYY-MM-DD 或 DDL YYYY/MM/DD
    """
    m = re.search(r'DDL\s+(\d{4}[-/]\d{2}[-/]\d{2})', line)
    if m:
        date_str = m.group(1).replace("/", "-")
        try:
            return datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            pass
    return None


def _get_line_added_date(line: str, file_stat_mtime: float) -> datetime:
    """
    Focus 行没有独立的添加时间戳，用文件 mtime 作为保守估计。
    实际使用时可扩展为从 git log 取每行添加时间。
    """
    return datetime.fromtimestamp(file_stat_mtime)


def clean_focus_items() -> dict:
    """
    清理 focus.md 中的过期条目：
    - 有 DDL 且已过期 → 删除
    - 无 DDL 且超过 14 天 → 标记为 waiting (⏳)
    - 无 DDL 且超过 21 天（已是 waiting）→ 删除

    返回统计 dict: {removed, marked_waiting}
    """
    if not FOCUS_PATH.exists():
        return {"removed": 0, "marked_waiting": 0}

    try:
        content = FOCUS_PATH.read_text(encoding="utf-8")
    except Exception as e:
        print(f"[aging] 读取 focus.md 失败: {e}")
        return {"removed": 0, "marked_waiting": 0}

    now = datetime.now()
    file_mtime = FOCUS_PATH.stat().st_mtime
    lines = content.splitlines(keepends=True)

    new_lines: list[str] = []
    removed = 0
    marked_waiting = 0

    for line in lines:
        stripped = line.rstrip("\n")

        # 只处理以 - 开头或带优先级 emoji 的条目行
        is_item = (
            stripped.lstrip().startswith("- ") or
            "🔴" in stripped or
            "🟡" in stripped or
            "⏳" in stripped
        )

        if not is_item:
            new_lines.append(line)
            continue

        # 检查 DDL
        ddl = _parse_focus_line_date(stripped)
        if ddl:
            if ddl.date() < now.date():
                # DDL 已过 → 删除
                removed += 1
                continue
            else:
                new_lines.append(line)
                continue

        # 无 DDL 条目 — 用文件 mtime 粗估（实际应记录添加时间）
        added = _get_line_added_date(stripped, file_mtime)
        age_days = (now - added).days

        if "⏳" in stripped:
            # 已是 waiting 状态，超过 21 天删除
            if age_days >= FOCUS_NO_DDL_REMOVE_DAYS:
                removed += 1
                continue
        else:
            if age_days >= FOCUS_NO_DDL_WAITING_DAYS:
                # 替换优先级标识为 ⏳
                new_line = stripped
                new_line = new_line.replace("🔴", "⏳").replace("🟡", "⏳")
                new_lines.append(new_line + "\n")
                marked_waiting += 1
                continue

        new_lines.append(line)

    if removed > 0 or marked_waiting > 0:
        new_content = "".join(new_lines)
        try:
            from memory.layers import _write
            _write(FOCUS_PATH, new_content, source="aging")
        except Exception:
            FOCUS_PATH.write_text(new_content, encoding="utf-8")
        print(f"[aging] focus 清理: 删除 {removed} 条，标 waiting {marked_waiting} 条")

    return {"removed": removed, "marked_waiting": marked_waiting}


# ── 项目陈旧检查 ──────────────────────────────────────────────────────────────

def check_project_staleness() -> dict:
    """
    检查超过 60 天无更新的项目，发邮件提醒归档。
    返回 {stale_projects: [name, ...]}
    """
    if not PROJECTS_DIR.exists():
        return {"stale_projects": []}

    now = datetime.now()
    stale = []

    for p in PROJECTS_DIR.glob("*.md"):
        try:
            mtime = datetime.fromtimestamp(p.stat().st_mtime)
            age_days = (now - mtime).days
            if age_days >= PROJECT_STALE_DAYS:
                stale.append(p.stem)
        except Exception:
            continue

    if stale:
        try:
            from email_module.sender import send_email
            names = "、".join(stale)
            body = (
                f"以下项目已超过 {PROJECT_STALE_DAYS} 天无更新，"
                f"建议确认是否需要归档：\n\n{names}\n\n"
                f"如需归档，请回复：Aegis: 归档 <项目名>"
            )
            send_email(
                to=config.NETEASE_EMAIL,
                subject=f"[Aegis] {len(stale)} 个项目可能需要归档",
                body=body,
            )
            print(f"[aging] 发送陈旧项目提醒: {stale}")
        except Exception as e:
            print(f"[aging] 发送陈旧项目邮件失败: {e}")

    return {"stale_projects": stale}


# ── Pending 过期 ──────────────────────────────────────────────────────────────

def expire_pending_items() -> dict:
    """
    Layer 1 的 pending 条目超过 48h 未处理 → status='expired'
    返回 {expired: count}
    """
    try:
        from memory import db as main_db
        cutoff = (datetime.now() - timedelta(hours=LAYER1_EXPIRE_HOURS)).isoformat()
        with main_db.get_conn() as conn:
            n = conn.execute("""
                UPDATE memory_pending
                SET status='expired', notes='auto-expired (48h timeout)'
                WHERE status='pending'
                  AND proposed_layer IN ('layer1_focus','layer1_people','layer1_decision')
                  AND extracted_at <= ?
            """, (cutoff,)).rowcount
        if n:
            print(f"[aging] Layer 1 pending 过期: {n} 条")
        return {"expired": n}
    except Exception as e:
        print(f"[aging] expire_pending_items 失败: {e}")
        return {"expired": 0}


# ── People.md 重算 ────────────────────────────────────────────────────────────

def recalc_people_md() -> dict:
    """
    重算 contacts.importance，然后更新 people.md：
    - 删除 importance < 70 且非 manually_pinned 的联系人
    - 添加 importance >= 70 的联系人（上限 20 人）
    返回 {added, removed, total}
    """
    try:
        from memory import db as main_db
        # 1. 重算 importance
        main_db.recalc_importance()

        # 2. 获取应进入 people.md 的联系人（importance >= 70，按重要度排序，取前20）
        qualified = main_db.get_contacts_by_importance(min_importance=70, limit=20)

        # 3. 读取当前 people.md
        from memory.layers import get_people, upsert_person, PEOPLE_PATH
        current_content = get_people()

        # 4. 找出当前在 people.md 中的联系人（简单解析 - 行以 - ** 开头）
        current_names_emails: set[str] = set()
        for line in current_content.splitlines():
            # 格式: - **姓名** <email> — 角色...
            m_name = re.search(r'\*\*(.+?)\*\*', line)
            m_email = re.search(r'<([^>]+)>', line)
            if m_name:
                current_names_emails.add(m_name.group(1))
            if m_email:
                current_names_emails.add(m_email.group(1))

        # 5. 确保所有 qualified 联系人都在 people.md 中
        added = 0
        for c in qualified:
            name = c.get("display_name", "")
            email = c.get("email", "")
            key = name or email
            if key and key not in current_names_emails:
                upsert_person(
                    name=name,
                    role=c.get("role", "联系人"),
                    note=c.get("notes", ""),
                    email=email or "",
                )
                added += 1

        # 6. 找出应移出的联系人（在 people.md 但 importance < 70 且非 manually_pinned）
        # 获取低重要度的联系人
        removed = 0
        try:
            with main_db.get_conn() as conn:
                low_contacts = conn.execute("""
                    SELECT display_name, email FROM contacts
                    WHERE importance < 70 AND manually_pinned = 0
                      AND in_people_md = 1
                """).fetchall()

            if low_contacts:
                lines = current_content.splitlines(keepends=True)
                new_lines = []
                for line in lines:
                    should_remove = False
                    for lc in low_contacts:
                        name = lc[0] or ""
                        email = lc[1] or ""
                        if (name and name in line) or (email and email in line):
                            should_remove = True
                            removed += 1
                            break
                    if not should_remove:
                        new_lines.append(line)

                if removed > 0:
                    new_content = "".join(new_lines)
                    try:
                        from memory.layers import _write
                        _write(PEOPLE_PATH, new_content, source="aging")
                    except Exception:
                        PEOPLE_PATH.write_text(new_content, encoding="utf-8")

                # 更新 in_people_md 标志
                with main_db.get_conn() as conn:
                    for lc in low_contacts:
                        if lc[1]:
                            conn.execute(
                                "UPDATE contacts SET in_people_md=0 WHERE email=?",
                                (lc[1],)
                            )
        except Exception as e:
            print(f"[aging] 移出低重要度联系人失败: {e}")

        total = len(qualified)
        print(f"[aging] people.md 重算: +{added} 新增, -{removed} 移出, 总计 {total} 人")
        return {"added": added, "removed": removed, "total": total}

    except Exception as e:
        print(f"[aging] recalc_people_md 失败: {e}")
        return {"added": 0, "removed": 0, "total": 0}


# ── 主入口 ────────────────────────────────────────────────────────────────────

def run_aging() -> dict:
    """
    执行所有老化检查，返回汇总结果。
    调用顺序：
      1. clean_focus_items
      2. expire_pending_items
      3. check_project_staleness
      4. recalc_people_md
    """
    print(f"[aging] 开始老化检查 @ {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    summary: dict = {}

    try:
        summary["focus"] = clean_focus_items()
    except Exception as e:
        print(f"[aging] clean_focus_items 异常: {e}")
        summary["focus"] = {"error": str(e)}

    try:
        summary["pending"] = expire_pending_items()
    except Exception as e:
        print(f"[aging] expire_pending_items 异常: {e}")
        summary["pending"] = {"error": str(e)}

    try:
        summary["projects"] = check_project_staleness()
    except Exception as e:
        print(f"[aging] check_project_staleness 异常: {e}")
        summary["projects"] = {"error": str(e)}

    try:
        summary["people"] = recalc_people_md()
    except Exception as e:
        print(f"[aging] recalc_people_md 异常: {e}")
        summary["people"] = {"error": str(e)}

    print(f"[aging] 完成: {summary}")
    return summary
