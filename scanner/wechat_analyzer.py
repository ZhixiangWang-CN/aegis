"""
微信历史聊天 AI 批量分析
对已导入的历史聊天记录做结构化提取：
  - 项目动态 → memory/projects/*.md (via pending)
  - 重要决定 → memory/decisions.md (via pending)
  - 待办任务 → memory/focus.md (via pending)
  - 同步补建 FTS5 全文索引

运行方式:
  python main.py --analyze-wechat           # 分析全部历史
  python main.py --analyze-wechat --days 30 # 只分析最近30天
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from collections import defaultdict

from memory import db as main_db
from ai import client as ai


# 每次送给 AI 分析的消息窗口（条数）
WINDOW_SIZE = 80

# 每个联系人/群最多分析几个窗口（防止 token 爆炸）
MAX_WINDOWS_PER_CHAT = 5

# 只分析消息数达到阈值的会话
MIN_MSGS_TO_ANALYZE = 10

# FTS5 索引批大小
FTS_BATCH = 200


# ── FTS5 索引 ─────────────────────────────────────────────────────────────

def _index_messages_fts(msgs: list[dict]):
    """把微信消息批量写入 FTS5 全文索引"""
    try:
        from memory.fts_store import get_store
        fts = get_store()
        for m in msgs:
            doc_id = f"wechat_{m['msg_id']}"
            text = f"[{m.get('talker_name', '')}] {m.get('content', '')}"
            fts.add(
                doc_id=doc_id,
                collection="wechat",
                text=text,
                source="wechat",
                metadata={
                    "talker": m.get("talker_wxid", ""),
                    "name": m.get("talker_name", ""),
                    "time": m.get("create_time", ""),
                },
            )
    except Exception as e:
        print(f"[WxAnalyzer] FTS 索引失败: {e}")


def build_fts_index(days_back: int = None):
    """
    为所有（或最近N天）微信消息补建 FTS5 索引。
    """
    print("[WxAnalyzer] 开始补建微信 FTS5 索引...")
    with main_db.get_conn() as conn:
        if days_back:
            since = (datetime.now() - timedelta(days=days_back)).isoformat()
            rows = conn.execute("""
                SELECT msg_id, talker_wxid, talker_name, content, create_time
                FROM wechat_messages
                WHERE content IS NOT NULL AND create_time >= ?
                ORDER BY create_time ASC
            """, (since,)).fetchall()
        else:
            rows = conn.execute("""
                SELECT msg_id, talker_wxid, talker_name, content, create_time
                FROM wechat_messages
                WHERE content IS NOT NULL
                ORDER BY create_time ASC
            """).fetchall()

    msgs = [dict(r) for r in rows]
    total = len(msgs)
    print(f"[WxAnalyzer] 共 {total} 条消息需要索引")

    for i in range(0, total, FTS_BATCH):
        batch = msgs[i:i + FTS_BATCH]
        _index_messages_fts(batch)
        if (i // FTS_BATCH) % 10 == 0:
            print(f"[WxAnalyzer] FTS 进度: {min(i + FTS_BATCH, total)}/{total}")

    print(f"[WxAnalyzer] FTS 索引完成: {total} 条")
    return total


# ── AI 提取 ───────────────────────────────────────────────────────────────

_EXTRACT_PROMPT = """你是Aegis，正在分析用户的微信聊天记录。
从下面的对话片段中提取结构化信息，只提取明确出现的内容，不要臆测。

输出 JSON，格式如下（没有则为空列表/空字符串）：
{
  "projects": [
    {"name": "项目名", "update": "项目动态描述", "status": "进行中/暂停/完成"}
  ],
  "decisions": [
    {"decision": "决定内容", "reason": "原因（如有）"}
  ],
  "tasks": [
    {"text": "待办内容", "deadline": "截止日期（如有，格式YYYY-MM-DD）", "project": "关联项目（如有）"}
  ],
  "key_facts": [
    "关于这个人/群的重要事实，一句话"
  ]
}

只输出 JSON，不要解释。"""


def _ai_extract(chat_name: str, msgs_text: str) -> dict:
    """对一段对话做 AI 结构化提取"""
    prompt = f"对话来源: {chat_name}\n\n{msgs_text}"
    try:
        raw = ai.chat(
            messages=[{"role": "user", "content": prompt}],
            system_prompt=_EXTRACT_PROMPT,
            temperature=0.2,
        )
        raw = raw.strip().strip("```json").strip("```").strip()
        return json.loads(raw)
    except Exception:
        return {}


def _push_to_pending(extracted: dict, chat_name: str, source_ref: str):
    """将 AI 提取结果推送到 pending 队列"""
    from memory.pending import add_focus, add

    projects = extracted.get("projects", [])
    decisions = extracted.get("decisions", [])
    tasks = extracted.get("tasks", [])

    for p in projects:
        name = p.get("name", "").strip()
        update = p.get("update", "").strip()
        if name and update:
            add(
                source="wechat",
                content=f"[{name}] {update}",
                proposed_layer="layer2_project",
                proposed_target=f"projects/{name}.md",
                proposed_section="历史记录",
                item_type="project_update",
                item_data={"project": name, "update": update,
                           "status": p.get("status", ""), "chat": chat_name},
                confidence=0.7,
            )

    for d in decisions:
        decision = d.get("decision", "").strip()
        if decision:
            add(
                source="wechat",
                content=decision,
                proposed_layer="layer1_decision",
                proposed_target="decisions.md",
                item_type="decision",
                item_data={"decision": decision, "reason": d.get("reason", ""),
                           "chat": chat_name},
                confidence=0.75,
            )

    for t in tasks:
        text = t.get("text", "").strip()
        if text:
            add_focus(
                text=text,
                source="wechat",
                deadline=t.get("deadline", ""),
                project=t.get("project", ""),
                confidence=0.65,
            )


# ── 主分析流程 ────────────────────────────────────────────────────────────

def analyze_wechat_history(days_back: int = None, skip_fts: bool = False) -> dict:
    """
    主入口：对历史微信聊天做 AI 结构化提取 + FTS5 索引。
    days_back=None 分析全部历史。

    返回统计 dict。
    """
    stats = {
        "chats_analyzed": 0,
        "windows_processed": 0,
        "projects_found": 0,
        "decisions_found": 0,
        "tasks_found": 0,
        "fts_indexed": 0,
    }

    # 1. 补建 FTS5 索引
    if not skip_fts:
        stats["fts_indexed"] = build_fts_index(days_back=days_back)

    # 2. 读取消息，按会话分组
    with main_db.get_conn() as conn:
        if days_back:
            since = (datetime.now() - timedelta(days=days_back)).isoformat()
            rows = conn.execute("""
                SELECT msg_id, talker_wxid, talker_name, content,
                       is_sender, create_time
                FROM wechat_messages
                WHERE content IS NOT NULL AND create_time >= ?
                ORDER BY talker_wxid, create_time ASC
            """, (since,)).fetchall()
        else:
            rows = conn.execute("""
                SELECT msg_id, talker_wxid, talker_name, content,
                       is_sender, create_time
                FROM wechat_messages
                WHERE content IS NOT NULL
                ORDER BY talker_wxid, create_time ASC
            """).fetchall()

    by_chat: dict[str, list] = defaultdict(list)
    for r in rows:
        by_chat[r["talker_wxid"]].append(dict(r))

    print(f"[WxAnalyzer] 共 {len(by_chat)} 个会话需要分析")

    # 3. 逐会话 AI 分析
    for wxid, msgs in by_chat.items():
        if len(msgs) < MIN_MSGS_TO_ANALYZE:
            continue

        chat_name = msgs[0].get("talker_name") or wxid

        # 按窗口分批分析（每次 WINDOW_SIZE 条，最多 MAX_WINDOWS_PER_CHAT 批）
        windows_done = 0
        for i in range(0, len(msgs), WINDOW_SIZE):
            if windows_done >= MAX_WINDOWS_PER_CHAT:
                break

            window = msgs[i:i + WINDOW_SIZE]
            lines = []
            for m in window:
                speaker = "我" if m.get("is_sender") else chat_name
                time_str = (m.get("create_time") or "")[:10]
                lines.append(f"[{time_str}] {speaker}: {m['content'][:200]}")
            msgs_text = "\n".join(lines)

            extracted = _ai_extract(chat_name, msgs_text)
            if not extracted:
                windows_done += 1
                continue

            source_ref = f"wechat:{wxid}:offset{i}"
            _push_to_pending(extracted, chat_name, source_ref)

            stats["projects_found"]  += len(extracted.get("projects", []))
            stats["decisions_found"] += len(extracted.get("decisions", []))
            stats["tasks_found"]     += len(extracted.get("tasks", []))
            stats["windows_processed"] += 1
            windows_done += 1

        stats["chats_analyzed"] += 1
        if stats["chats_analyzed"] % 5 == 0:
            print(f"[WxAnalyzer] 进度: {stats['chats_analyzed']}/{len(by_chat)} 个会话")

    print(f"[WxAnalyzer] 分析完成: {stats}")
    return stats
