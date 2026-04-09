"""
SQLite 主库 — v3 schema
设计原则: Markdown 是主人，数据库是仆人（加速查询的索引）

新表直接按 v3 建；旧表安全迁移（ADD COLUMN），旧 helper 函数全部保留，
现有代码零改动。
"""
import sqlite3
import json
from datetime import datetime
from pathlib import Path
import config


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(config.DB_PATH), timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=60000")
    return conn


def _safe_add_column(conn, table: str, col: str, col_type: str):
    """安全添加列，已存在则跳过"""
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
    except Exception:
        pass


def init_db():
    with get_conn() as conn:
        conn.executescript("""
        -- ─────────────────────────────────────────────────────────────
        -- 写入日志：所有对 memory/*.md 的变更都经过这里
        -- ─────────────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS write_log (
            id          INTEGER PRIMARY KEY,
            ts          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime')),
            target      TEXT NOT NULL,
            operation   TEXT NOT NULL,
            source      TEXT,
            content     TEXT,
            detail_json TEXT,
            git_hash    TEXT,
            reverted    INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_write_log_ts     ON write_log(ts);
        CREATE INDEX IF NOT EXISTS idx_write_log_target ON write_log(target);

        -- ─────────────────────────────────────────────────────────────
        -- 统一联系人表（邮件 + 微信合并）
        -- ─────────────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS contacts (
            id               INTEGER PRIMARY KEY,
            display_name     TEXT NOT NULL,
            email            TEXT,
            wechat_id        TEXT,
            wechat_alias     TEXT,
            -- 角色
            role             TEXT DEFAULT 'unknown',
            role_confidence  REAL DEFAULT 0.0,
            role_source      TEXT DEFAULT 'auto',
            role_updated_at  TEXT,
            -- 重要度（0-100，≥70 写入 people.md）
            importance       INTEGER DEFAULT 0,
            in_people_md     INTEGER DEFAULT 0,
            manually_pinned  INTEGER DEFAULT 0,
            -- 统计
            first_seen       TEXT,
            last_seen        TEXT,
            email_count      INTEGER DEFAULT 0,
            wechat_msg_count INTEGER DEFAULT 0,
            -- 备注
            institution      TEXT,
            institution_type TEXT,
            notes            TEXT,
            tags             TEXT,
            UNIQUE(email),
            UNIQUE(wechat_id)
        );
        CREATE INDEX IF NOT EXISTS idx_contacts_role       ON contacts(role);
        CREATE INDEX IF NOT EXISTS idx_contacts_importance ON contacts(importance DESC);
        CREATE INDEX IF NOT EXISTS idx_contacts_email      ON contacts(email);

        -- ─────────────────────────────────────────────────────────────
        -- 邮件
        -- ─────────────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS emails (
            id              TEXT PRIMARY KEY,   -- message hash（兼容旧代码）
            message_id      TEXT,               -- RFC822 Message-ID
            account         TEXT DEFAULT '163',
            folder          TEXT DEFAULT 'INBOX',
            from_addr       TEXT,
            from_name       TEXT,
            subject         TEXT,
            date            TEXT,
            body            TEXT,               -- 兼容旧字段名
            body_text       TEXT,
            summary         TEXT,
            importance      INTEGER DEFAULT 2,
            category        TEXT,
            status          TEXT DEFAULT 'unread',
            needs_reply     INTEGER DEFAULT 0,
            draft_reply     TEXT,
            is_processed    INTEGER DEFAULT 0,
            is_command      INTEGER DEFAULT 0,
            has_attachments INTEGER DEFAULT 0,
            attachment_info TEXT,
            contact_id      INTEGER,
            thread_id       TEXT,
            extracted_items TEXT,
            created_at      TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_emails_date      ON emails(date DESC);

        -- ─────────────────────────────────────────────────────────────
        -- 微信消息
        -- ─────────────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS wechat_messages (
            id          INTEGER PRIMARY KEY,
            msg_id      TEXT NOT NULL UNIQUE,
            chat_type   TEXT DEFAULT 'private',
            chat_id     TEXT NOT NULL,
            talker_wxid TEXT,
            talker_name TEXT,
            sender_id   TEXT,
            is_self     INTEGER DEFAULT 0,
            is_sender   INTEGER DEFAULT 0,   -- 兼容旧字段
            content     TEXT,
            msg_type    INTEGER DEFAULT 1,
            file_name   TEXT,
            ts          TEXT,
            create_time TEXT,               -- 兼容旧字段
            is_processed INTEGER DEFAULT 0,
            is_trigger   INTEGER DEFAULT 0,
            trigger_reason TEXT,
            summary      TEXT,
            extracted_items TEXT,
            indexed_at   TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_wechat_ts        ON wechat_messages(ts DESC);
        CREATE INDEX IF NOT EXISTS idx_wechat_chat      ON wechat_messages(chat_id);
        CREATE INDEX IF NOT EXISTS idx_wechat_processed ON wechat_messages(is_processed);
        CREATE INDEX IF NOT EXISTS idx_wechat_trigger   ON wechat_messages(is_trigger);

        -- ─────────────────────────────────────────────────────────────
        -- 微信联系人（兼容旧代码，新代码优先用统一 contacts 表）
        -- ─────────────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS wechat_contacts (
            wxid                TEXT PRIMARY KEY,
            nickname            TEXT,
            remark              TEXT,
            avatar_url          TEXT,
            role                TEXT DEFAULT 'unknown',
            role_confidence     REAL DEFAULT 0.0,
            role_source         TEXT DEFAULT '',
            msg_count           INTEGER DEFAULT 0,
            msg_count_at_check  INTEGER DEFAULT 0,
            last_msg_at         TEXT,
            last_role_check_at  TEXT,
            is_group            INTEGER DEFAULT 0,
            updated_at          TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_wx_contacts_role ON wechat_contacts(role);

        -- ─────────────────────────────────────────────────────────────
        -- 微信群组
        -- ─────────────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS wechat_groups (
            id              INTEGER PRIMARY KEY,
            wxid            TEXT NOT NULL UNIQUE,
            name            TEXT,
            display_name    TEXT,
            group_type      TEXT DEFAULT 'normal',
            type_confidence REAL DEFAULT 0.0,
            type_source     TEXT DEFAULT 'auto',
            member_count    INTEGER DEFAULT 0,
            last_active     TEXT,
            last_msg_at     TEXT,
            notes           TEXT,
            updated_at      TEXT
        );

        -- ─────────────────────────────────────────────────────────────
        -- 文件索引
        -- ─────────────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS file_index (
            id           INTEGER PRIMARY KEY,
            path         TEXT NOT NULL UNIQUE,
            filename     TEXT,
            file_type    TEXT,
            extension    TEXT,
            size_kb      INTEGER,
            size_bytes   INTEGER,
            modified_at  TEXT,
            content_hash TEXT,
            summary      TEXT,
            indexed_at   TEXT,
            is_indexed   INTEGER DEFAULT 0,
            chunk_count  INTEGER DEFAULT 0,
            status       TEXT DEFAULT 'pending',
            source       TEXT DEFAULT 'disk',
            source_ref   TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_file_path ON file_index(path);

        -- ─────────────────────────────────────────────────────────────
        -- 暂存审核队列（v3 完整版）
        -- ─────────────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS memory_pending (
            id              INTEGER PRIMARY KEY,
            source          TEXT NOT NULL,
            source_ref      TEXT,
            content         TEXT NOT NULL,
            proposed_layer  TEXT NOT NULL,
            proposed_target TEXT,
            proposed_section TEXT,
            item_type       TEXT NOT NULL DEFAULT 'focus_item',
            item_data       TEXT NOT NULL DEFAULT '{}',
            confidence      REAL NOT NULL DEFAULT 0.5,
            auto_approve    INTEGER DEFAULT 0,
            extracted_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime')),
            status          TEXT NOT NULL DEFAULT 'pending',
            reviewed_at     TEXT,
            applied_at      TEXT,
            batch_id        TEXT,
            notes           TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_pending_status ON memory_pending(status);
        CREATE INDEX IF NOT EXISTS idx_pending_batch  ON memory_pending(batch_id);
        CREATE INDEX IF NOT EXISTS idx_pending_src    ON memory_pending(source);

        -- ─────────────────────────────────────────────────────────────
        -- 每日简报
        -- ─────────────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS daily_reports (
            date    TEXT PRIMARY KEY,
            content TEXT,
            sent_at TEXT
        );

        -- ─────────────────────────────────────────────────────────────
        -- 邮件指令
        -- ─────────────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS commands (
            id          TEXT PRIMARY KEY,
            instruction TEXT,
            result      TEXT,
            executed_at TEXT
        );

        -- ─────────────────────────────────────────────────────────────
        -- RSS 订阅条目
        -- ─────────────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS rss_items (
            id         TEXT PRIMARY KEY,
            feed_name  TEXT,
            feed_url   TEXT,
            title      TEXT,
            link       TEXT,
            summary    TEXT,
            importance INTEGER DEFAULT 3,
            published  TEXT,
            fetched_at TEXT,
            is_indexed INTEGER DEFAULT 0
        );
        """)

        # ── 安全迁移旧表（已存在的表补充新列）─────────────────────────
        _safe_add_column(conn, "emails", "message_id",      "TEXT")
        _safe_add_column(conn, "emails", "account",         "TEXT DEFAULT '163'")
        _safe_add_column(conn, "emails", "from_name",       "TEXT")
        _safe_add_column(conn, "emails", "body_text",       "TEXT")
        _safe_add_column(conn, "emails", "is_processed",    "INTEGER DEFAULT 0")
        _safe_add_column(conn, "emails", "is_command",      "INTEGER DEFAULT 0")
        _safe_add_column(conn, "emails", "has_attachments", "INTEGER DEFAULT 0")
        _safe_add_column(conn, "emails", "attachment_info", "TEXT")
        _safe_add_column(conn, "emails", "contact_id",      "INTEGER")
        _safe_add_column(conn, "emails", "thread_id",       "TEXT")
        _safe_add_column(conn, "emails", "extracted_items", "TEXT")

        # 迁移后才能安全建这些索引（列可能是 ALTER TABLE 刚加的）
        for ddl in [
            "CREATE INDEX IF NOT EXISTS idx_emails_account    ON emails(account)",
            "CREATE INDEX IF NOT EXISTS idx_emails_processed  ON emails(is_processed)",
            "CREATE INDEX IF NOT EXISTS idx_file_ext          ON file_index(extension)",
            "CREATE INDEX IF NOT EXISTS idx_file_hash         ON file_index(content_hash)",
            "CREATE INDEX IF NOT EXISTS idx_file_status       ON file_index(status)",
        ]:
            try:
                conn.execute(ddl)
            except Exception:
                pass

        _safe_add_column(conn, "file_index", "filename",     "TEXT")
        _safe_add_column(conn, "file_index", "extension",    "TEXT")
        _safe_add_column(conn, "file_index", "size_bytes",   "INTEGER")
        _safe_add_column(conn, "file_index", "content_hash", "TEXT")
        _safe_add_column(conn, "file_index", "is_indexed",   "INTEGER DEFAULT 0")
        _safe_add_column(conn, "file_index", "chunk_count",  "INTEGER DEFAULT 0")
        _safe_add_column(conn, "file_index", "source",       "TEXT DEFAULT 'disk'")
        _safe_add_column(conn, "file_index", "source_ref",   "TEXT")

        # ── contacts 表迁移：旧表列名与 v3 不同，需重建 ──────────────────
        _migrate_contacts_table(conn)

    print("[DB] v3 schema 初始化完成")


def _migrate_contacts_table(conn):
    """
    检测 contacts 表是否是旧版 schema（缺 display_name / last_seen / tags 等列），
    如果是则做一次数据迁移：旧表 → contacts_v3_new → 重命名回 contacts。
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(contacts)").fetchall()}
    # 旧表特征：有 name 但没有 display_name
    if "display_name" in cols:
        return  # 已是 v3，跳过

    print("[DB] 检测到旧版 contacts 表，开始迁移...")
    try:
        conn.executescript("""
        -- 1. 备份旧表
        ALTER TABLE contacts RENAME TO contacts_v2_backup;

        -- 2. 创建 v3 contacts 表
        CREATE TABLE IF NOT EXISTS contacts (
            id               INTEGER PRIMARY KEY,
            display_name     TEXT NOT NULL,
            email            TEXT,
            wechat_id        TEXT,
            wechat_alias     TEXT,
            role             TEXT DEFAULT 'unknown',
            role_confidence  REAL DEFAULT 0.0,
            role_source      TEXT DEFAULT 'auto',
            role_updated_at  TEXT,
            importance       INTEGER DEFAULT 0,
            in_people_md     INTEGER DEFAULT 0,
            manually_pinned  INTEGER DEFAULT 0,
            first_seen       TEXT,
            last_seen        TEXT,
            email_count      INTEGER DEFAULT 0,
            wechat_msg_count INTEGER DEFAULT 0,
            institution      TEXT,
            institution_type TEXT,
            notes            TEXT,
            tags             TEXT,
            UNIQUE(email),
            UNIQUE(wechat_id)
        );

        -- 3. 迁移数据（字段名映射）
        INSERT OR IGNORE INTO contacts
            (display_name, email, role, importance, institution, institution_type,
             email_count, first_seen, last_seen, notes, tags)
        SELECT
            COALESCE(name, email),        -- display_name ← name
            email,
            COALESCE(role, 'unknown'),
            COALESCE(importance, 0),
            institution,
            institution_type,
            COALESCE(email_count, 0),
            COALESCE(created_at, ''),     -- first_seen ← created_at
            COALESCE(last_contact, updated_at, ''), -- last_seen ← last_contact
            notes,
            COALESCE(topics, '[]')        -- tags ← topics
        FROM contacts_v2_backup;

        -- 4. 重建索引
        CREATE INDEX IF NOT EXISTS idx_contacts_role       ON contacts(role);
        CREATE INDEX IF NOT EXISTS idx_contacts_importance ON contacts(importance DESC);
        CREATE INDEX IF NOT EXISTS idx_contacts_email      ON contacts(email);
        """)
        count = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
        print(f"[DB] contacts 表迁移完成，共迁移 {count} 条联系人")
    except Exception as e:
        print(f"[DB] contacts 迁移失败（非致命）: {e}")


# ── write_log ────────────────────────────────────────────────────────────────

def log_write(target: str, operation: str, source: str,
              content: str = "", detail: dict = None, git_hash: str = "") -> int:
    """记录一次写入操作，返回 log id"""
    with get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO write_log (target, operation, source, content, detail_json, git_hash)
            VALUES (?,?,?,?,?,?)
        """, (target, operation, source, content,
              json.dumps(detail or {}, ensure_ascii=False), git_hash))
        return cur.lastrowid


def get_write_log(log_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM write_log WHERE id=?", (log_id,)).fetchone()
    return dict(row) if row else None


def mark_reverted(log_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE write_log SET reverted=1 WHERE id=?", (log_id,))


# ── 邮件（保持旧接口完整兼容）────────────────────────────────────────────────

def save_email(email_id, from_addr, subject, date, body,
               summary, importance, category, needs_reply, draft_reply,
               account="163", from_name="", message_id=""):
    with get_conn() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO emails
            (id, message_id, account, from_addr, from_name, subject, date,
             body, body_text, summary, importance, category, status,
             needs_reply, draft_reply, is_processed, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'unread',?,?,1,?)
        """, (email_id, message_id or email_id, account,
              from_addr, from_name, subject, date,
              body[:5000] if body else "", body[:5000] if body else "",
              summary, importance, category,
              1 if needs_reply else 0, draft_reply,
              datetime.now().isoformat()))


def get_important_emails(min_importance: int = 3, limit: int = 20) -> list:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM emails
            WHERE importance >= ? AND status = 'unread'
            ORDER BY importance DESC, date DESC
            LIMIT ?
        """, (min_importance, limit)).fetchall()
    return [dict(r) for r in rows]


def email_exists(email_id: str) -> bool:
    with get_conn() as conn:
        return conn.execute(
            "SELECT 1 FROM emails WHERE id=?", (email_id,)
        ).fetchone() is not None


# ── 联系人（新接口，基于统一 contacts 表）────────────────────────────────────

def upsert_contact(display_name: str, email: str = None, wechat_id: str = None,
                   role: str = "unknown", importance: int = 0,
                   institution: str = "", notes: str = "") -> int:
    """插入或更新统一联系人，返回 id"""
    now = datetime.now().isoformat()
    with get_conn() as conn:
        # 先尝试找已有记录
        existing = None
        if email:
            existing = conn.execute(
                "SELECT id FROM contacts WHERE email=?", (email,)
            ).fetchone()
        if not existing and wechat_id:
            existing = conn.execute(
                "SELECT id FROM contacts WHERE wechat_id=?", (wechat_id,)
            ).fetchone()

        if existing:
            conn.execute("""
                UPDATE contacts SET
                    display_name=?, role=?, importance=MAX(importance,?),
                    institution=COALESCE(NULLIF(?,'''), institution),
                    notes=COALESCE(NULLIF(?,''), notes),
                    last_seen=?
                WHERE id=?
            """, (display_name, role, importance, institution, notes, now, existing[0]))
            return existing[0]
        else:
            cur = conn.execute("""
                INSERT INTO contacts
                (display_name, email, wechat_id, role, importance,
                 institution, notes, first_seen, last_seen)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (display_name, email, wechat_id, role, importance,
                  institution, notes, now, now))
            return cur.lastrowid


def get_contacts_by_importance(min_importance: int = 70, limit: int = 20) -> list:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM contacts
            WHERE importance >= ?
            ORDER BY importance DESC LIMIT ?
        """, (min_importance, limit)).fetchall()
    return [dict(r) for r in rows]


# ── 文件索引（保持旧接口）────────────────────────────────────────────────────

def upsert_file(path, file_type, size_kb, modified_at):
    from pathlib import Path as _Path
    p = _Path(path)
    with get_conn() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO file_index
            (path, filename, file_type, extension, size_kb, modified_at, status)
            VALUES (?,?,?,?,?,?,'pending')
        """, (path, p.name, file_type, p.suffix.lower(), size_kb, modified_at))


def update_file_summary(path, summary):
    with get_conn() as conn:
        conn.execute("""
            UPDATE file_index
            SET summary=?, indexed_at=?, status='indexed', is_indexed=1
            WHERE path=?
        """, (summary, datetime.now().isoformat(), path))


def get_pending_files(limit: int = 50) -> list:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM file_index WHERE status='pending'
            ORDER BY CASE file_type
                WHEN '.md'   THEN 1 WHEN '.txt'  THEN 2
                WHEN '.docx' THEN 3 WHEN '.pdf'  THEN 4
                WHEN '.py'   THEN 5 ELSE 6 END
            LIMIT ?
        """, (limit,)).fetchall()
    return [dict(r) for r in rows]


# ── 日报 ─────────────────────────────────────────────────────────────────────

def save_daily_report(date: str, content: str):
    with get_conn() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO daily_reports (date, content, sent_at)
            VALUES (?,?,?)
        """, (date, content, datetime.now().isoformat()))


def report_exists_today(date: str) -> bool:
    with get_conn() as conn:
        return conn.execute(
            "SELECT 1 FROM daily_reports WHERE date=?", (date,)
        ).fetchone() is not None


# ── 指令 ─────────────────────────────────────────────────────────────────────

def save_command(cmd_id: str, instruction: str, result: str):
    with get_conn() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO commands (id, instruction, result, executed_at)
            VALUES (?,?,?,?)
        """, (cmd_id, instruction, result, datetime.now().isoformat()))


def command_exists(cmd_id: str) -> bool:
    with get_conn() as conn:
        return conn.execute(
            "SELECT 1 FROM commands WHERE id=?", (cmd_id,)
        ).fetchone() is not None


# ── importance 重算（每周调用）───────────────────────────────────────────────

def recalc_importance():
    """
    重新计算所有联系人的 importance 分数。
    每周日由调度器调用。
    """
    from memory.layers import FOCUS_PATH, PROJECTS_DIR

    # 读取 focus.md 和 projects/*.md 中的名字引用
    focus_text = FOCUS_PATH.read_text(encoding="utf-8") if FOCUS_PATH.exists() else ""
    project_texts = []
    if PROJECTS_DIR.exists():
        for p in PROJECTS_DIR.glob("*.md"):
            project_texts.append(p.read_text(encoding="utf-8"))

    with get_conn() as conn:
        contacts = conn.execute("SELECT id, display_name, email, wechat_id, role, manually_pinned FROM contacts").fetchall()
        for c in contacts:
            if c["manually_pinned"]:
                conn.execute("UPDATE contacts SET importance=100 WHERE id=?", (c["id"],))
                continue

            score = 0
            name = c["display_name"] or ""
            email = c["email"] or ""

            # 近30天邮件互动
            email_cnt = conn.execute("""
                SELECT COUNT(*) FROM emails
                WHERE (from_addr=? OR from_addr LIKE ?)
                  AND date >= date('now','-30 days')
            """, (email, f"%{email}%")).fetchone()[0]
            score += email_cnt * 3

            # 近30天微信互动
            wx_cnt = conn.execute("""
                SELECT COUNT(*) FROM wechat_messages
                WHERE talker_wxid=? AND create_time >= date('now','-30 days')
            """, (c["wechat_id"] or "",)).fetchone()[0]
            score += wx_cnt * 2

            # 角色加权
            role_bonus = {"superior": 30, "collaborator": 20, "junior": 15,
                          "close_personal": 25, "colleague": 5}.get(c["role"], 0)
            score += role_bonus

            # 被 focus.md 引用
            if name and name in focus_text:
                score += focus_text.count(name) * 5

            # 被 projects 引用
            for pt in project_texts:
                if name and name in pt:
                    score += 3

            score = min(score, 99)  # 99 以下，100 留给手动置顶
            conn.execute("UPDATE contacts SET importance=? WHERE id=?", (score, c["id"]))

    print("[DB] importance 重算完成")
