"""
记忆暂存队列 — AI 提取 → 用户审核 → 写入记忆层

流程:
  信息源(邮件/微信/RSS) → AI提取 → pending表 → 发邮件给用户
  → 用户回复 "Aegis: 确认 1,3,5" → apply_approved() → 写入 layers.py
  → 2小时无回复 + 置信度>=0.8 → 自动通过
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from memory import db as main_db

AUTO_APPLY_HOURS = 2
AUTO_APPLY_MIN_CONFIDENCE = 0.8


def _ensure_table():
    with main_db.get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS memory_pending (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            source       TEXT,     -- email / wechat / wechat_group / rss / manual
            content      TEXT,     -- 原始提取内容（供用户阅读）
            proposed_layer  TEXT,  -- focus / people / project / decision / layer4
            proposed_target TEXT,  -- 目标文件名或集合名
            proposed_section TEXT, -- 目标章节（如 "进行中"）
            item_type    TEXT,     -- focus_item / person / project_update / decision
            item_data    TEXT,     -- JSON: 结构化条目数据
            confidence   REAL DEFAULT 0.5,
            extracted_at TEXT,
            status       TEXT DEFAULT 'pending',
            applied_at   TEXT,
            notes        TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_pending_status ON memory_pending(status);
        CREATE INDEX IF NOT EXISTS idx_pending_src    ON memory_pending(source);
        """)


_ensure_table()


# ── 写入 ─────────────────────────────────────────────────────────────────

def add(source: str, content: str, proposed_layer: str,
        proposed_target: str = "", proposed_section: str = "",
        item_type: str = "focus_item", item_data: dict = None,
        confidence: float = 0.5, notes: str = "",
        auto_approve: int = 0) -> int:
    """添加一条待审核条目，返回 id"""
    with main_db.get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO memory_pending
            (source, content, proposed_layer, proposed_target, proposed_section,
             item_type, item_data, confidence, extracted_at, status, notes, auto_approve)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (source, content, proposed_layer, proposed_target, proposed_section,
              item_type,
              json.dumps(item_data or {}, ensure_ascii=False),
              confidence,
              datetime.now().isoformat(),
              "pending", notes, auto_approve))
        return cur.lastrowid


def add_focus(text: str, source: str, deadline: str = "",
              project: str = "", db_ref: str = "",
              priority: str = "normal", confidence: float = 0.6,
              from_name: str = "") -> int:
    """便捷：添加一条 focus 待审核条目"""
    item_data = {"text": text, "deadline": deadline, "source": source,
                 "project": project, "db_ref": db_ref, "priority": priority,
                 "from_name": from_name}
    content = text
    if deadline:
        content += f"（DDL: {deadline}）"
    if from_name:
        content += f"（来自: {from_name}）"
    if project:
        content += f" [{project}]"
    return add(source=source, content=content,
               proposed_layer="layer1_focus", proposed_target="focus.md",
               item_type="focus_item", item_data=item_data,
               confidence=confidence)


def add_person(name: str, role: str, source: str,
               note: str = "", email: str = "",
               confidence: float = 0.6) -> int:
    """便捷：添加一条联系人待审核条目"""
    item_data = {"name": name, "role": role, "note": note, "email": email}
    content = f"{name} — {role}"
    if note:
        content += f"，{note}"
    return add(source=source, content=content,
               proposed_layer="layer1_people", proposed_target="people.md",
               item_type="person", item_data=item_data,
               confidence=confidence)


def add_layer4(source: str, content: str, confidence: float = 0.7,
               item_data: dict = None) -> int:
    """便捷：添加一条 Layer 4 知识库条目（低风险，auto_approve=1）"""
    return add(source=source, content=content, proposed_layer="layer4",
               item_type="knowledge", item_data=item_data,
               confidence=confidence, auto_approve=1)


# ── 查询 ─────────────────────────────────────────────────────────────────

def get_pending(source: str = None, item_type: str = None) -> list[dict]:
    """获取所有 pending 条目"""
    sql = "SELECT * FROM memory_pending WHERE status='pending'"
    params = []
    if source:
        sql += " AND source=?"
        params.append(source)
    if item_type:
        sql += " AND item_type=?"
        params.append(item_type)
    sql += " ORDER BY confidence DESC, extracted_at DESC"

    with main_db.get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def count_pending() -> int:
    with main_db.get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM memory_pending WHERE status='pending'"
        ).fetchone()[0]


# ── 审核操作 ─────────────────────────────────────────────────────────────

def approve(pending_id: int, notes: str = "") -> bool:
    with main_db.get_conn() as conn:
        n = conn.execute("""
            UPDATE memory_pending SET status='approved', notes=?
            WHERE id=? AND status='pending'
        """, (notes, pending_id)).rowcount
    return n > 0


def reject(pending_id: int, notes: str = "") -> bool:
    with main_db.get_conn() as conn:
        n = conn.execute("""
            UPDATE memory_pending SET status='rejected', notes=?
            WHERE id=? AND status='pending'
        """, (notes, pending_id)).rowcount
    return n > 0


def approve_by_ids(ids: list[int]) -> int:
    return sum(1 for i in ids if approve(i))


def approve_all(min_confidence: float = 0.0) -> int:
    with main_db.get_conn() as conn:
        return conn.execute("""
            UPDATE memory_pending SET status='approved'
            WHERE status='pending' AND confidence >= ?
        """, (min_confidence,)).rowcount


def auto_apply_timeout() -> int:
    """v3: delegate to auto_approve module"""
    from memory.auto_approve import process_auto_approvals
    return process_auto_approvals()


# ── 应用到记忆层 ─────────────────────────────────────────────────────────

def apply_approved() -> int:
    """将所有 approved 条目写入记忆层，返回成功数"""
    from memory import layers

    with main_db.get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM memory_pending WHERE status IN ('approved', 'auto_approved')
            ORDER BY extracted_at ASC
        """).fetchall()

    applied = 0
    for row in [dict(r) for r in rows]:
        try:
            item_data = json.loads(row.get("item_data") or "{}")
            layer = row["proposed_layer"]

            if layer in ("focus", "layer1_focus"):
                item = layers.FocusItem(
                    text=item_data.get("text", row["content"]),
                    deadline=item_data.get("deadline", ""),
                    source=item_data.get("source", row["source"]),
                    project=item_data.get("project", ""),
                    db_ref=item_data.get("db_ref", ""),
                    priority=item_data.get("priority", "normal"),
                    from_name=item_data.get("from_name", ""),
                )
                if item.text:
                    layers.add_focus_item(item)

            elif layer in ("people", "layer1_people"):
                layers.upsert_person(
                    name=item_data.get("name", ""),
                    role=item_data.get("role", "联系人"),
                    note=item_data.get("note", ""),
                    email=item_data.get("email", ""),
                )

            elif layer in ("project", "layer2_project"):
                layers.update_project(
                    row["proposed_target"],
                    row.get("proposed_section") or "历史记录",
                    row["content"],
                )

            elif layer in ("decision", "layer1_decision"):
                layers.add_decision(
                    decision=item_data.get("decision", row["content"]),
                    reason=item_data.get("reason", ""),
                )

            elif layer == "layer4":
                # Layer 4: 写入 FTS5 全文索引（如果尚未写入）
                try:
                    from memory.fts_store import add_document
                    doc_id = f"pending_{row['id']}"
                    add_document(
                        doc_id=doc_id,
                        collection="knowledge",
                        text=row["content"],
                        source=row["source"],
                        metadata=item_data,
                    )
                except Exception as fts_err:
                    print(f"[Pending] Layer4 FTS 写入失败: {fts_err}")

            with main_db.get_conn() as conn:
                conn.execute("""
                    UPDATE memory_pending SET status='applied', applied_at=?
                    WHERE id=?
                """, (datetime.now().isoformat(), row["id"]))
            applied += 1

        except Exception as e:
            print(f"[Pending] 写入失败 id={row['id']}: {e}")

    return applied


# ── 邮件格式化 ───────────────────────────────────────────────────────────

def format_for_email() -> str:
    """将待审核条目格式化为邮件正文"""
    items = get_pending()
    if not items:
        return "暂无待审核条目。"

    ICONS = {"focus": "📌", "people": "👤", "project": "📁",
             "decision": "⚖️", "layer4": "🗄️"}
    SOURCE_ICONS = {"email": "📧", "wechat": "💬", "wechat_group": "👥",
                    "rss": "📰", "manual": "✏️"}

    lines = [f"Aegis提取了 {len(items)} 条待确认信息：\n"]
    for item in items:
        icon = ICONS.get(item["proposed_layer"], "·")
        src_icon = SOURCE_ICONS.get(item["source"], "")
        conf_pct = f"{item['confidence']:.0%}"
        lines.append(
            f"{icon} [{item['id']}] {src_icon} ({conf_pct}) {item['content'][:120]}"
        )

    lines += [
        "",
        "─" * 40,
        "回复操作：",
        "  Aegis: 确认 1,3,5     → 通过指定编号",
        "  Aegis: 确认全部       → 通过全部",
        "  Aegis: 拒绝 2,4       → 拒绝指定编号",
        "",
        f"注：置信度≥80% 的条目将在 {AUTO_APPLY_HOURS} 小时后自动通过。",
    ]
    return "\n".join(lines)


# ── 解析用户指令 ─────────────────────────────────────────────────────────

def parse_review_command(instruction: str) -> tuple[str, list[int]]:
    """
    解析用户回复指令。
    返回 (action, ids)，action 为 'approve'/'reject'/'approve_all'
    """
    import re
    text = instruction.strip()

    if "确认全部" in text or "全部确认" in text:
        return "approve_all", []

    ids = [int(x) for x in re.findall(r'\d+', text)]

    if any(w in text for w in ("确认", "通过", "ok", "好")):
        return "approve", ids
    if any(w in text for w in ("拒绝", "不要", "删除", "cancel")):
        return "reject", ids

    return "unknown", []
