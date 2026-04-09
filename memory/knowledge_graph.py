"""
知识图谱 — 基于 SQLite 的实体关系存储
灵感来源: OpenJarvis knowledge_graph.py，纯 Python/SQLite 实现（去掉 Rust 依赖）

实体类型: person, paper, journal, institution, project, topic
关系类型: authored, submitted_to, reviewed_by, collaborated_with,
          affiliated_with, cited, related_to, sent_email_to
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

import config

DB_PATH = config.DATA_DIR / "knowledge_graph.db"


def _get_conn():
    import sqlite3
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _init():
    with _get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS entities (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            name TEXT NOT NULL,
            attrs TEXT DEFAULT '{}',
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS relations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            src_id TEXT NOT NULL,
            rel_type TEXT NOT NULL,
            dst_id TEXT NOT NULL,
            weight REAL DEFAULT 1.0,
            attrs TEXT DEFAULT '{}',
            created_at TEXT,
            UNIQUE(src_id, rel_type, dst_id)
        );

        CREATE INDEX IF NOT EXISTS idx_relations_src ON relations(src_id);
        CREATE INDEX IF NOT EXISTS idx_relations_dst ON relations(dst_id);
        CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(type);
        CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name);
        """)


_init()


# ────────────────────────── 实体操作 ──────────────────────────────────

def upsert_entity(entity_id: str, entity_type: str, name: str,
                  attrs: dict | None = None) -> str:
    """插入或更新实体，返回 entity_id"""
    import json
    with _get_conn() as conn:
        conn.execute("""
            INSERT INTO entities (id, type, name, attrs, created_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
              name=excluded.name,
              attrs=excluded.attrs
        """, (entity_id, entity_type, name,
              json.dumps(attrs or {}, ensure_ascii=False),
              datetime.now().isoformat()))
    return entity_id


def add_relation(src_id: str, rel_type: str, dst_id: str,
                 weight: float = 1.0, attrs: dict | None = None):
    """添加有向关系（已存在则权重+1）"""
    import json
    with _get_conn() as conn:
        existing = conn.execute(
            "SELECT id, weight FROM relations WHERE src_id=? AND rel_type=? AND dst_id=?",
            (src_id, rel_type, dst_id)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE relations SET weight=weight+1 WHERE id=?",
                (existing["id"],)
            )
        else:
            conn.execute("""
                INSERT OR IGNORE INTO relations
                (src_id, rel_type, dst_id, weight, attrs, created_at)
                VALUES (?,?,?,?,?,?)
            """, (src_id, rel_type, dst_id, weight,
                  json.dumps(attrs or {}, ensure_ascii=False),
                  datetime.now().isoformat()))


def get_entity(entity_id: str) -> Optional[dict]:
    with _get_conn() as conn:
        r = conn.execute("SELECT * FROM entities WHERE id=?", (entity_id,)).fetchone()
        return dict(r) if r else None


def find_entities(name_query: str, entity_type: str | None = None,
                  limit: int = 10) -> list[dict]:
    """模糊搜索实体"""
    with _get_conn() as conn:
        if entity_type:
            rows = conn.execute("""
                SELECT * FROM entities
                WHERE name LIKE ? AND type=?
                ORDER BY rowid DESC LIMIT ?
            """, (f"%{name_query}%", entity_type, limit)).fetchall()
        else:
            rows = conn.execute("""
                SELECT * FROM entities
                WHERE name LIKE ?
                ORDER BY rowid DESC LIMIT ?
            """, (f"%{name_query}%", limit)).fetchall()
        return [dict(r) for r in rows]


def neighbors(entity_id: str, rel_type: str | None = None,
              direction: str = "both", limit: int = 20) -> list[dict]:
    """
    查询实体的邻居。
    direction: "out"(出边), "in"(入边), "both"
    """
    with _get_conn() as conn:
        results = []
        if direction in ("out", "both"):
            q = "SELECT * FROM relations WHERE src_id=?"
            params = [entity_id]
            if rel_type:
                q += " AND rel_type=?"
                params.append(rel_type)
            q += f" ORDER BY weight DESC LIMIT {limit}"
            rows = conn.execute(q, params).fetchall()
            for r in rows:
                dst = conn.execute("SELECT * FROM entities WHERE id=?",
                                   (r["dst_id"],)).fetchone()
                results.append({"direction": "out", "relation": r["rel_type"],
                                 "weight": r["weight"],
                                 "entity": dict(dst) if dst else {"id": r["dst_id"]}})

        if direction in ("in", "both"):
            q = "SELECT * FROM relations WHERE dst_id=?"
            params = [entity_id]
            if rel_type:
                q += " AND rel_type=?"
                params.append(rel_type)
            q += f" ORDER BY weight DESC LIMIT {limit}"
            rows = conn.execute(q, params).fetchall()
            for r in rows:
                src = conn.execute("SELECT * FROM entities WHERE id=?",
                                   (r["src_id"],)).fetchone()
                results.append({"direction": "in", "relation": r["rel_type"],
                                 "weight": r["weight"],
                                 "entity": dict(src) if src else {"id": r["src_id"]}})

        return sorted(results, key=lambda x: x["weight"], reverse=True)[:limit]


def get_summary() -> str:
    """返回知识图谱统计摘要"""
    with _get_conn() as conn:
        entity_counts = conn.execute("""
            SELECT type, COUNT(*) as cnt FROM entities
            GROUP BY type ORDER BY cnt DESC
        """).fetchall()
        rel_count = conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
        top_rels = conn.execute("""
            SELECT rel_type, COUNT(*) as cnt FROM relations
            GROUP BY rel_type ORDER BY cnt DESC LIMIT 5
        """).fetchall()

    lines = [f"知识图谱: {rel_count} 条关系"]
    for r in entity_counts:
        lines.append(f"  {r['type']}: {r['cnt']} 个实体")
    if top_rels:
        lines.append("主要关系类型: " + " | ".join(
            f"{r['rel_type']}({r['cnt']})" for r in top_rels))
    return "\n".join(lines)


# ────────────────────────── 从邮件构建图谱 ────────────────────────────

def build_from_contacts():
    """
    从 contacts 表自动构建知识图谱（实体+关系）。
    机构→用户关系，联系人→机构归属。
    """
    from memory import db as main_db
    user_id = "user:self"
    upsert_entity(user_id, "person", "用户(自己)",
                  {"email": config.NETEASE_EMAIL})

    try:
        with main_db.get_conn() as conn:
            contacts = conn.execute(
                "SELECT email, name, institution, institution_type, role, importance FROM contacts"
            ).fetchall()
    except Exception:
        return

    for c in contacts:
        email  = c["email"] or ""
        name   = c["name"] or email.split("@")[0]
        inst   = c["institution"] or ""
        itype  = c["institution_type"] or "unknown"
        role   = c["role"] or "contact"
        imp    = c["importance"] or 2

        if not email:
            continue

        # 联系人实体
        person_id = f"person:{email}"
        upsert_entity(person_id, "person", name,
                      {"email": email, "role": role, "importance": imp})

        # 机构实体
        if inst:
            inst_id = f"org:{inst.lower()[:40]}"
            upsert_entity(inst_id, itype, inst)
            add_relation(person_id, "affiliated_with", inst_id,
                         weight=float(imp))

        # 用户→联系人关系
        rel = _map_role_to_relation(role)
        add_relation(user_id, rel, person_id, weight=float(imp))

    print(f"[KG] 从联系人构建图谱完成: {get_summary()}")


def _map_role_to_relation(role: str) -> str:
    mapping = {
        "journal_editor": "submitted_to",
        "reviewer":       "reviewed_by",
        "collaborator":   "collaborated_with",
        "advisor":        "supervised_by",
        "conference":     "submitted_to",
    }
    return mapping.get(role, "sent_email_to")


def add_paper(title: str, journal: str | None = None,
              authors: list[str] | None = None, status: str = "submitted"):
    """
    向图谱中添加一篇论文及其关系。
    可从邮件分析中调用。
    """
    import re
    paper_id = f"paper:{re.sub(r'[^a-z0-9]', '_', title.lower()[:50])}"
    upsert_entity(paper_id, "paper", title, {"status": status})

    user_id = "user:self"
    add_relation(user_id, "authored", paper_id)

    if journal:
        journal_id = f"journal:{journal.lower()[:40]}"
        upsert_entity(journal_id, "journal", journal)
        add_relation(paper_id, "submitted_to", journal_id)

    if authors:
        for author in authors:
            author_id = f"person:author:{author.lower()[:30]}"
            upsert_entity(author_id, "person", author)
            add_relation(author_id, "authored", paper_id)
