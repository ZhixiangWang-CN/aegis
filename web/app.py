"""
Aegis Web UI — FastAPI 后端

启动方式：
  python main.py --web           # 默认 http://localhost:8077
  python main.py --web --port 8080
"""
from __future__ import annotations

import json
import sys
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# 把项目根目录加入路径
sys.path.insert(0, str(Path(__file__).parent.parent))

import config
from memory import db as main_db

from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.responses import StreamingResponse, HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ── 初始化 ─────────────────────────────────────────────────────────────────

app = FastAPI(title="Aegis", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8077", "http://127.0.0.1:8077"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Token 认证中间件 ─────────────────────────────────────────────────────────
# 从环境变量或 .credentials 读取 WEB_TOKEN；未配置时仅本地访问（127.0.0.1）
_WEB_TOKEN: str = os.environ.get("JARVIS_WEB_TOKEN", "")

# 不需要认证的路径前缀（静态资源 + 健康检查）
_NO_AUTH_PREFIXES = ("/static/", "/health")


@app.middleware("http")
async def token_auth_middleware(request: Request, call_next):
    """
    若配置了 JARVIS_WEB_TOKEN，要求所有非静态请求携带：
      Authorization: Bearer <token>  或  ?token=<token>
    未配置 token 时，不做认证（仅依赖 127.0.0.1 绑定保护）。
    """
    if _WEB_TOKEN:
        path = request.url.path
        if not any(path.startswith(p) for p in _NO_AUTH_PREFIXES):
            auth_header = request.headers.get("Authorization", "")
            query_token = request.query_params.get("token", "")
            provided = ""
            if auth_header.startswith("Bearer "):
                provided = auth_header[7:]
            elif query_token:
                provided = query_token
            if provided != _WEB_TOKEN:
                from fastapi.responses import JSONResponse
                return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    return await call_next(request)


STATIC_DIR = Path(__file__).parent / "static"
MEMORY_DIR = config.DATA_DIR / "memory"

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ── 工具函数 ────────────────────────────────────────────────────────────────

def _read_md(path: Path) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def _build_chat_system_prompt() -> str:
    """构建对话系统提示词，注入关键记忆"""
    parts = [
        f"你是Aegis，{config.OWNER_NAME}的私人AI助理。你了解主人的研究背景、工作进展和生活情况。",
        "回答时直接、简洁，像一个了解主人的得力助理。支持中英文混用。",
        "",
    ]

    bg = _read_md(MEMORY_DIR / "personal" / "background.md")
    if bg:
        parts.append(f"## 关于用户的背景\n{bg[:3000]}")

    focus = _read_md(MEMORY_DIR / "focus.md")
    if focus:
        parts.append(f"## 当前工作焦点\n{focus[:800]}")

    wx_active = _read_md(MEMORY_DIR / "wechat_active.md")
    if wx_active:
        parts.append(f"## 近期微信活跃事项\n{wx_active[:800]}")

    from_emails = _read_md(MEMORY_DIR / "from_emails.md")
    if from_emails:
        parts.append(f"## 邮件摘要\n{from_emails[:2000]}")

    return "\n\n".join(parts)


# ── Pydantic 模型 ───────────────────────────────────────────────────────────

class ChatMessage(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    inject_memory: bool = True


class PendingAction(BaseModel):
    action: str  # "approve" | "reject"
    note: Optional[str] = None


# ── 路由：页面 ──────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = STATIC_DIR / "index.html"
    if html_path.exists():
        return html_path.read_text(encoding="utf-8")
    return "<h1>Aegis Web UI</h1><p>static/index.html 未找到</p>"


# ── 路由：系统状态 ──────────────────────────────────────────────────────────

@app.get("/api/status")
async def get_status():
    """系统状态统计"""
    try:
        with main_db.get_conn() as conn:
            email_count = conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0]
            email_important = conn.execute(
                "SELECT COUNT(*) FROM emails WHERE importance >= 3"
            ).fetchone()[0]
            wechat_count = conn.execute(
                "SELECT COUNT(DISTINCT chat_id) FROM wechat_messages"
            ).fetchone()[0]
            wechat_msgs = conn.execute("SELECT COUNT(*) FROM wechat_messages").fetchone()[0]
            file_count = conn.execute("SELECT COUNT(*) FROM file_index").fetchone()[0]
            hot_files = conn.execute(
                "SELECT COUNT(*) FROM file_index WHERE activity_tier='hot'"
            ).fetchone()[0] if _has_column(conn, "file_index", "activity_tier") else 0
            pending_count = conn.execute(
                "SELECT COUNT(*) FROM memory_pending WHERE status='pending'"
            ).fetchone()[0]
            contact_count = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]

        # 记忆文件统计
        mem_files = list(MEMORY_DIR.rglob("*.md")) if MEMORY_DIR.exists() else []
        contact_files = list((MEMORY_DIR / "contacts").glob("*.md")) if (MEMORY_DIR / "contacts").exists() else []
        group_files = list((MEMORY_DIR / "groups").glob("*.md")) if (MEMORY_DIR / "groups").exists() else []
        project_files = list((MEMORY_DIR / "projects").glob("*.md")) if (MEMORY_DIR / "projects").exists() else []

        return {
            "emails": {"total": email_count, "important": email_important},
            "wechat": {"chats": wechat_count, "messages": wechat_msgs},
            "files": {"indexed": file_count, "hot": hot_files},
            "memory": {
                "total_md": len(mem_files),
                "contacts": len(contact_files),
                "groups": len(group_files),
                "projects": len(project_files),
            },
            "pending": pending_count,
            "db_contacts": contact_count,
            "updated_at": datetime.now().isoformat(),
        }
    except Exception as e:
        return {"error": str(e)}


def _has_column(conn, table, col):
    try:
        conn.execute(f"SELECT {col} FROM {table} LIMIT 1")
        return True
    except Exception:
        return False


# ── 路由：邮件 ──────────────────────────────────────────────────────────────

@app.get("/api/emails")
async def get_emails(
    limit: int = Query(30, le=100),
    importance: int = Query(3, ge=1, le=5),
    offset: int = Query(0),
):
    """获取重要邮件列表"""
    with main_db.get_conn() as conn:
        rows = conn.execute("""
            SELECT id, from_addr, from_name, subject, date, summary,
                   importance, category, needs_reply, is_processed
            FROM emails
            WHERE importance >= ?
            ORDER BY date DESC
            LIMIT ? OFFSET ?
        """, (importance, limit, offset)).fetchall()

        total = conn.execute(
            "SELECT COUNT(*) FROM emails WHERE importance >= ?", (importance,)
        ).fetchone()[0]

    return {
        "total": total,
        "items": [dict(r) for r in rows],
    }


@app.get("/api/emails/detail")
async def get_email_detail(id: int = Query(...)):
    """获取单封邮件详情（query param 方式）"""
    with main_db.get_conn() as conn:
        row = conn.execute("SELECT * FROM emails WHERE id=?", (id,)).fetchone()
    if not row:
        raise HTTPException(404, "邮件不存在")
    return dict(row)


@app.get("/api/emails/{email_id}")
async def get_email_detail_path(email_id: int):
    """获取单封邮件详情（path param 兼容）"""
    with main_db.get_conn() as conn:
        row = conn.execute("SELECT * FROM emails WHERE id=?", (email_id,)).fetchone()
    if not row:
        raise HTTPException(404, "邮件不存在")
    return dict(row)


# ── 路由：联系人 ────────────────────────────────────────────────────────────

@app.get("/api/contacts")
async def get_contacts(limit: int = Query(50, le=200)):
    """获取联系人列表（按重要度排序）"""
    with main_db.get_conn() as conn:
        rows = conn.execute("""
            SELECT id, display_name, email, wechat_id, role, importance,
                   last_seen, email_count, wechat_msg_count, institution
            FROM contacts
            ORDER BY importance DESC, last_seen DESC
            LIMIT ?
        """, (limit,)).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/contacts/files")
async def get_contact_files():
    """获取联系人记忆文件列表"""
    contacts_dir = MEMORY_DIR / "contacts"
    if not contacts_dir.exists():
        return []
    files = []
    for f in sorted(contacts_dir.glob("*.md")):
        stat = f.stat()
        files.append({
            "name": f.stem,
            "path": str(f.relative_to(MEMORY_DIR)),
            "size": stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
        })
    return files


# ── 路由：记忆 ──────────────────────────────────────────────────────────────

@app.get("/api/memory/tree")
async def get_memory_tree():
    """获取记忆目录树"""
    if not MEMORY_DIR.exists():
        return []

    def _scan(directory: Path, depth=0):
        items = []
        try:
            for p in sorted(directory.iterdir()):
                if p.name.startswith(".") or p.name.endswith(".db"):
                    continue
                if p.is_dir():
                    children = _scan(p, depth + 1) if depth < 3 else []
                    items.append({
                        "type": "dir",
                        "name": p.name,
                        "path": str(p.relative_to(MEMORY_DIR)),
                        "children": children,
                    })
                elif p.suffix == ".md":
                    items.append({
                        "type": "file",
                        "name": p.name,
                        "path": str(p.relative_to(MEMORY_DIR)),
                        "size": p.stat().st_size,
                    })
        except PermissionError:
            pass
        return items

    return _scan(MEMORY_DIR)


@app.get("/api/memory/file")
async def get_memory_file(path: str = Query(...)):
    """读取记忆文件内容"""
    safe_path = MEMORY_DIR / path.lstrip("/\\").replace("..", "")
    if not safe_path.exists() or not safe_path.is_file():
        raise HTTPException(404, f"文件不存在: {path}")
    if safe_path.suffix not in (".md", ".txt"):
        raise HTTPException(400, "只支持 .md 和 .txt 文件")
    return {
        "path": path,
        "content": safe_path.read_text(encoding="utf-8"),
        "modified": datetime.fromtimestamp(safe_path.stat().st_mtime).isoformat(),
    }


@app.get("/api/memory/overview")
async def get_memory_overview():
    """获取核心记忆概览（background + focus + from_emails 摘要）"""
    return {
        "background": _read_md(MEMORY_DIR / "personal" / "background.md")[:3000],
        "focus": _read_md(MEMORY_DIR / "focus.md"),
        "from_emails_summary": _read_md(MEMORY_DIR / "from_emails.md")[:2000],
        "wechat_active": _read_md(MEMORY_DIR / "wechat_active.md")[:2000],
        "index": _read_md(MEMORY_DIR / "INDEX.md"),
    }


class FocusAction(BaseModel):
    action: str   # "complete" | "delete"
    text: str     # 精确匹配的条目文本（- [ ] 后面的内容）


@app.post("/api/focus/action")
async def focus_action(body: FocusAction):
    """勾选完成或删除 focus.md 中的条目"""
    focus_path = MEMORY_DIR / "focus.md"
    if not focus_path.exists():
        raise HTTPException(404, "focus.md 不存在")

    content = focus_path.read_text(encoding="utf-8")
    lines = content.splitlines(keepends=True)
    new_lines = []
    found = False

    search = body.text.strip()
    for line in lines:
        stripped = line.strip()
        # 匹配 "- [ ] 文本" 或 "- [x] 文本"
        if not found and (
            stripped == f"- [ ] {search}" or
            stripped == f"- [x] {search}" or
            stripped.startswith(f"- [ ] {search}") or
            stripped.startswith(f"- [x] {search}")
        ):
            found = True
            if body.action == "complete":
                # 把 [ ] 改为 [x]，并加上完成日期
                from datetime import date
                done_line = line.replace("- [ ]", "- [x]", 1).replace("- [x]", "- [x]", 1)
                # 确保是完成标记
                done_line = line.replace("[ ]", "[x]", 1).rstrip()
                done_line += f"  ✓ {date.today()}\n"
                new_lines.append(done_line)
            # delete: 直接跳过（不加入 new_lines）
        else:
            new_lines.append(line)

    if not found:
        raise HTTPException(404, f"未找到条目: {search}")

    new_content = "".join(new_lines)
    try:
        from memory.writer import get_writer
        get_writer().write("focus.md", "update", new_content, "web_ui")
    except Exception:
        focus_path.write_text(new_content, encoding="utf-8")
    return {"ok": True, "action": body.action, "text": search}


class FocusAdd(BaseModel):
    text: str          # 用户输入的原始描述
    ai_parse: bool = True   # 是否让AI解析优先级/截止时间/项目


@app.post("/api/focus/add")
async def focus_add(body: FocusAdd):
    """
    添加新焦点事项。
    ai_parse=True 时，AI 从自然语言中提取结构化信息（优先级/截止日期/关联项目），
    并生成简洁的条目文本写入 focus.md。
    """
    from ai import client as ai_client
    import json as _json
    from datetime import date

    raw = body.text.strip()
    if not raw:
        raise HTTPException(400, "内容不能为空")

    # ── AI 解析 ──────────────────────────────────────────────────
    if body.ai_parse:
        try:
            parse_prompt = f"""用户想添加一条焦点事项（待办/目标/任务）：

「{raw}」

请解析并输出 JSON：
{{
  "text": "简洁的条目描述（≤30字，去掉冗余词，保留核心行动）",
  "priority": "urgent | normal | waiting",
  "deadline": "YYYY-MM-DD 或空字符串",
  "project": "关联项目名（如有，否则空字符串）",
  "section": "紧急 | 常规 | 等待/观察"
}}

今天是 {date.today()}。
只输出 JSON。"""
            result = ai_client.chat(
                messages=[{"role": "user", "content": parse_prompt}],
                system_prompt="你是任务解析助手，从自然语言中精准提取结构化行动项。",
                temperature=0.1,
            )
            result = result.strip().strip("```json").strip("```").strip()
            parsed = _json.loads(result)
        except Exception:
            parsed = {"text": raw, "priority": "normal", "deadline": "", "project": "", "section": "常规"}
    else:
        parsed = {"text": raw, "priority": "normal", "deadline": "", "project": "", "section": "常规"}

    text      = parsed.get("text", raw).strip()
    deadline  = parsed.get("deadline", "").strip()
    project   = parsed.get("project", "").strip()
    section   = parsed.get("section", "常规").strip()
    priority  = parsed.get("priority", "normal")

    # 构建条目行
    item_line = f"- [ ] {text}"
    if deadline:
        item_line += f" (截止:{deadline})"
    if project:
        item_line += f" [{project}]"

    # ── 写入 focus.md ─────────────────────────────────────────────
    focus_path = MEMORY_DIR / "focus.md"
    if not focus_path.exists():
        # 初始化文件（通过 MemoryWriter）
        init_content = (
            f"# 当前焦点清单\n> 更新: {date.today()}\n\n"
            "## 紧急\n\n## 常规\n\n## 等待/观察\n"
        )
        try:
            from memory.writer import get_writer
            get_writer().write("focus.md", "update", init_content, "web_ui")
        except Exception:
            focus_path.write_text(init_content, encoding="utf-8")

    content = focus_path.read_text(encoding="utf-8")

    # 找到对应 section 插入；找不到则追加到文件末尾
    section_map = {
        "紧急": "## 紧急", "urgent": "## 紧急",
        "常规": "## 常规", "normal": "## 常规",
        "等待/观察": "## 等待/观察", "waiting": "## 等待/观察",
    }
    target_header = section_map.get(section, section_map.get(priority, "## 常规"))

    if target_header in content:
        # 在 section 标题后的第一个空行或下一个 ## 前插入
        lines = content.split("\n")
        insert_idx = None
        in_section = False
        for i, line in enumerate(lines):
            if line.strip() == target_header:
                in_section = True
                continue
            if in_section:
                if line.startswith("## "):
                    insert_idx = i
                    break
                if line.strip().startswith("- "):
                    insert_idx = i + 1  # 插在最后一条 item 后面
        if insert_idx is None:
            insert_idx = len(lines)
        lines.insert(insert_idx, item_line)
        content = "\n".join(lines)
    else:
        content = content.rstrip() + f"\n\n{target_header}\n{item_line}\n"

    try:
        from memory.writer import get_writer
        get_writer().write("focus.md", "update", content, "web_ui")
    except Exception:
        focus_path.write_text(content, encoding="utf-8")

    return {
        "ok": True,
        "item": item_line,
        "section": target_header,
        "parsed": parsed,
    }


@app.get("/api/memory/groups")
async def get_group_files():
    """获取微信群聊记忆文件列表"""
    groups_dir = MEMORY_DIR / "groups"
    if not groups_dir.exists():
        return []
    files = []
    for f in sorted(groups_dir.glob("*.md")):
        stat = f.stat()
        files.append({
            "name": f.stem,
            "path": str(f.relative_to(MEMORY_DIR)),
            "size": stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
        })
    return files


@app.get("/api/memory/projects")
async def get_project_files():
    """获取项目记忆文件列表"""
    projects_dir = MEMORY_DIR / "projects"
    if not projects_dir.exists():
        return []
    files = []
    for f in sorted(projects_dir.glob("*.md")):
        stat = f.stat()
        files.append({
            "name": f.stem,
            "path": str(f.relative_to(MEMORY_DIR)),
            "size": stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
        })
    return files


# ── 路由：待审核 ────────────────────────────────────────────────────────────

@app.get("/api/pending")
async def get_pending(status: str = Query("pending"), limit: int = Query(50)):
    """获取待审核记忆条目"""
    with main_db.get_conn() as conn:
        rows = conn.execute("""
            SELECT id, source, source_ref, content, proposed_layer,
                   proposed_target, item_type, confidence,
                   extracted_at, status, notes
            FROM memory_pending
            WHERE status = ?
            ORDER BY extracted_at DESC
            LIMIT ?
        """, (status, limit)).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/pending/{item_id}")
async def handle_pending(item_id: int, body: PendingAction):
    """审批/拒绝待审核条目"""
    from memory.pending import approve, reject
    now = datetime.now().isoformat()

    with main_db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM memory_pending WHERE id=?", (item_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "条目不存在")

    if body.action == "approve":
        approve(item_id)
        return {"ok": True, "action": "approved"}
    elif body.action == "reject":
        reject(item_id)
        return {"ok": True, "action": "rejected"}
    else:
        raise HTTPException(400, "action 必须是 approve 或 reject")


# ── 路由：微信消息 ──────────────────────────────────────────────────────────

@app.get("/api/wechat/chats")
async def get_wechat_chats(limit: int = Query(50)):
    """获取微信会话列表（按消息量排序）"""
    with main_db.get_conn() as conn:
        rows = conn.execute("""
            SELECT chat_id, talker_name,
                   COUNT(*) as msg_count,
                   MAX(create_time) as last_time,
                   SUM(CASE WHEN is_sender=1 THEN 1 ELSE 0 END) as my_count
            FROM wechat_messages
            GROUP BY chat_id
            ORDER BY msg_count DESC
            LIMIT ?
        """, (limit,)).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/wechat/messages")
async def get_wechat_messages(
    chat_id: str = Query(...),
    limit: int = Query(50),
    offset: int = Query(0),
):
    """获取某会话的消息记录"""
    with main_db.get_conn() as conn:
        rows = conn.execute("""
            SELECT id, talker_name, content, is_sender, create_time, msg_type
            FROM wechat_messages
            WHERE chat_id = ?
            ORDER BY create_time DESC
            LIMIT ? OFFSET ?
        """, (chat_id, limit, offset)).fetchall()
        total = conn.execute(
            "SELECT COUNT(*) FROM wechat_messages WHERE chat_id=?", (chat_id,)
        ).fetchone()[0]
    return {"total": total, "items": [dict(r) for r in rows]}


# ── 路由：搜索 ──────────────────────────────────────────────────────────────

@app.get("/api/search")
async def search(q: str = Query(..., min_length=1), limit: int = Query(20)):
    """跨源全文搜索（邮件 + 文件 + 微信）"""
    results = []

    pat = f"%{q}%"
    with main_db.get_conn() as conn:
        # 邮件搜索（subject + summary，中文 LIKE 有效）
        rows = conn.execute("""
            SELECT 'email' as source, id,
                   COALESCE(subject,'(无主题)') as title,
                   COALESCE(summary, subject, '') as snippet,
                   COALESCE(from_addr,'') as meta,
                   COALESCE(date,'') as ts,
                   COALESCE(importance,1) as importance
            FROM emails
            WHERE subject LIKE ? OR summary LIKE ? OR from_addr LIKE ? OR from_name LIKE ?
            ORDER BY importance DESC, date DESC
            LIMIT ?
        """, (pat, pat, pat, pat, limit // 2)).fetchall()
        results.extend([dict(r) for r in rows])

        # 微信消息搜索
        wx_rows = conn.execute("""
            SELECT 'wechat' as source, id,
                   COALESCE(talker_name,'') as title,
                   COALESCE(content,'') as snippet,
                   COALESCE(chat_id,'') as meta,
                   COALESCE(create_time,'') as ts,
                   0 as importance
            FROM wechat_messages
            WHERE content LIKE ?
            ORDER BY create_time DESC
            LIMIT ?
        """, (pat, limit // 3)).fetchall()
        results.extend([dict(r) for r in wx_rows])

        # 文件名搜索（file_index 无 id 列，用 rowid）
        file_rows = conn.execute("""
            SELECT 'file' as source, rowid as id,
                   COALESCE(filename,'') as title,
                   COALESCE(path,'') as snippet,
                   COALESCE(extension,'') as meta,
                   COALESCE(indexed_at,'') as ts,
                   0 as importance
            FROM file_index
            WHERE filename LIKE ? OR path LIKE ?
            ORDER BY indexed_at DESC
            LIMIT ?
        """, (pat, pat, 10)).fetchall()
        results.extend([dict(r) for r in file_rows])

    # 记忆文件搜索
    for md_file in MEMORY_DIR.rglob("*.md"):
        try:
            content = md_file.read_text(encoding="utf-8", errors="ignore")
            if q.lower() in content.lower():
                results.append({
                    "source": "memory",
                    "id": None,
                    "title": md_file.stem,
                    "snippet": _find_snippet(content, q, 150),
                    "meta": str(md_file.relative_to(MEMORY_DIR)),
                    "ts": datetime.fromtimestamp(md_file.stat().st_mtime).isoformat(),
                    "importance": 3,
                })
        except Exception:
            pass

    # 按 importance 和 ts 排序
    results.sort(key=lambda x: (-(x.get("importance") or 0), x.get("ts", "") or ""), reverse=False)
    return {"query": q, "total": len(results), "results": results[:limit]}


def _find_snippet(text: str, query: str, length: int = 150) -> str:
    idx = text.lower().find(query.lower())
    if idx < 0:
        return text[:length]
    start = max(0, idx - 30)
    return ("..." if start > 0 else "") + text[start:start + length] + "..."


# ── 路由：流式对话 ──────────────────────────────────────────────────────────

def _is_action_request(text: str) -> bool:
    """判断用户输入是否是需要执行操作的指令（而非纯聊天）"""
    action_keywords = (
        "发邮件", "发送邮件", "回复邮件", "发给", "发到",
        "生成文档", "写一份", "帮我写", "生成word", "生成表格",
        "发文件", "发送文件", "附件",
        "记住", "记录",
        "搜索", "查找", "查一下",
        "日报", "简报", "状态报告",
        "确认", "拒绝",
    )
    low = text.lower()
    return any(kw in low for kw in action_keywords)


@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest):
    """
    流式对话 (SSE)，自动注入用户记忆。
    检测到操作指令时，先执行指令再流式输出结果。
    """

    def generate():
        try:
            import ai.client as ai_client

            # 取最后一条用户消息
            last_user = next(
                (m.content for m in reversed(req.messages) if m.role == "user"), ""
            )

            # ── 操作指令：走 command_handler 执行引擎 ──────────────
            if _is_action_request(last_user):
                try:
                    from email_module.command_handler import _execute_command
                    result = _execute_command(last_user, context={"source": "web"})
                    # 流式输出执行结果
                    chunk_size = 80
                    for i in range(0, len(result), chunk_size):
                        data = json.dumps({"text": result[i:i+chunk_size]}, ensure_ascii=False)
                        yield f"data: {data}\n\n"
                    yield "data: [DONE]\n\n"
                    return
                except Exception as e:
                    # 执行失败降级到普通对话
                    err_msg = f"[执行失败: {e}，切换为普通对话模式]\n\n"
                    yield f"data: {json.dumps({'text': err_msg}, ensure_ascii=False)}\n\n"

            # ── 普通对话：流式 AI 回复 ────────────────────────────
            system_prompt = _build_chat_system_prompt() if req.inject_memory else "你是Aegis，一个智能AI助理。"
            messages = [{"role": m.role, "content": m.content} for m in req.messages]
            msgs = [{"role": "system", "content": system_prompt}] + messages

            stream = ai_client.get_client().chat.completions.create(
                model=config.VOLC_MODEL,
                messages=msgs,
                stream=True,
                temperature=0.7,
            )

            for chunk in stream:
                delta = chunk.choices[0].delta
                if delta.content:
                    data = json.dumps({"text": delta.content}, ensure_ascii=False)
                    yield f"data: {data}\n\n"

            yield "data: [DONE]\n\n"

        except Exception as e:
            err = json.dumps({"error": str(e)}, ensure_ascii=False)
            yield f"data: {err}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/chat")
async def chat_simple(req: ChatRequest):
    """非流式对话"""
    import ai.client as ai_client
    system_prompt = _build_chat_system_prompt() if req.inject_memory else "你是Aegis。"
    messages = [{"role": m.role, "content": m.content} for m in req.messages]
    result = ai_client.chat(messages, system_prompt=system_prompt)
    return {"content": result}


# ── 路由：任务触发 ──────────────────────────────────────────────────────────

@app.post("/api/tasks/{task_name}/run")
async def run_task(task_name: str):
    """立即触发后台任务"""
    allowed = {
        "briefing", "focus_update", "check_emails",
        "build_email_memory", "build_wechat_memory",
    }
    if task_name not in allowed:
        raise HTTPException(400, f"未知任务: {task_name}，支持: {', '.join(allowed)}")

    import threading

    def _run():
        main_db.init_db()
        if task_name == "briefing":
            from scheduler.jobs import send_daily_briefing
            send_daily_briefing()
        elif task_name == "focus_update":
            from scheduler.focus_updater import run_focus_update
            run_focus_update(send_email=False)
        elif task_name == "check_emails":
            from scheduler.jobs import check_emails
            check_emails()
        elif task_name == "build_email_memory":
            from scanner.email_memory_builder import build_email_memory
            build_email_memory()
        elif task_name == "build_wechat_memory":
            from scanner.wechat_memory_builder import build_wechat_memory
            build_wechat_memory(top_contacts=100, top_groups=100)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return {"ok": True, "task": task_name, "status": "started"}


# ── 设置 API ────────────────────────────────────────────────────────────────

def _mask_sensitive(s: dict) -> dict:
    """
    返回脱敏后的设置用于前端展示：
    敏感字段（API key、授权码、密码）已有值时返回空字符串，
    前端识别为"已配置，留空则不修改"。
    """
    import copy, json
    d = json.loads(json.dumps(s, ensure_ascii=False))
    _SENSITIVE = [
        ("api",         "volc_api_key"),
        ("email_163",   "auth_code"),
        ("email_gmail", "app_password"),
    ]
    for section, field in _SENSITIVE:
        if d.get(section, {}).get(field):
            d[section][field] = ""   # 不发送真实值到前端
    return d


@app.get("/api/settings")
async def get_settings():
    from settings_manager import load
    s = load()
    return _mask_sensitive(s)


@app.post("/api/settings")
async def save_settings(request: Request):
    """保存设置：空的敏感字段保留现有值，其他字段直接覆盖"""
    from settings_manager import save
    body = await request.json()
    save(body)   # save() 内部已处理空敏感字段保留逻辑
    # 同步扫描目录
    from settings_manager import load
    merged = load()
    if "scan" in merged and "roots" in merged["scan"]:
        config.SCAN_ROOTS = merged["scan"]["roots"]
    return {"ok": True}


@app.post("/api/settings/scan-roots")
async def add_scan_root(request: Request):
    """添加一个扫描目录"""
    body = await request.json()
    path = body.get("path", "").strip()
    if not path:
        raise HTTPException(400, "path 不能为空")
    from settings_manager import load, save
    s = load()
    roots = s.setdefault("scan", {}).setdefault("roots", [])
    if path not in roots:
        roots.append(path)
        save(s)
        config.SCAN_ROOTS = roots
    return s


@app.delete("/api/settings/scan-roots")
async def remove_scan_root(request: Request):
    """删除一个扫描目录"""
    body = await request.json()
    path = body.get("path", "").strip()
    from settings_manager import load, save
    s = load()
    roots = s.setdefault("scan", {}).setdefault("roots", [])
    if path in roots:
        roots.remove(path)
        save(s)
        config.SCAN_ROOTS = roots
    return s


# ── 启动入口 ────────────────────────────────────────────────────────────────

def start_web(host: str = "127.0.0.1", port: int = 8077):
    import uvicorn
    main_db.init_db()
    print(f"\nAegis Web UI 启动中...")
    print(f"  地址: http://localhost:{port}")
    print(f"  按 Ctrl+C 停止\n")
    uvicorn.run(app, host=host, port=port, log_level="warning")
