"""
Focus.md 自动更新器 — 多源 (邮件 + 微信私聊 + 微信群) 混合驱动

调用链:
  run_focus_update()
    ├─ extract_from_emails()        → 近7天重要邮件
    ├─ extract_from_wechat_private()→ 按角色提取私聊
    └─ extract_from_wechat_groups() → 群聊触发条件扫描
          ↓ 所有结果写入 memory_pending 表
  send_review_email()
          ↓ 发邮件给用户，列出待确认条目
  (定时) auto_apply_timeout()
          ↓ 2小时无回复自动通过高置信度条目
  apply_approved()
          ↓ 写入 focus.md / people.md / projects/*.md
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Optional

import config
from memory import db as main_db
from memory import pending as pq
from ai import client as ai_client
from email_module.reader import _safe_print
from scanner.wechat_roles import (
    ROLE_ANALYSIS_DEPTH, TRIGGER_KEYWORDS, ACKNOWLEDGEMENT_WORDS,
    get_contacts_by_role, get_core_groups, get_normal_groups,
    infer_all as infer_roles,
)

# ── 邮件提取 ──────────────────────────────────────────────────────────────

def extract_from_emails(days: int = 7, min_importance: int = 3) -> int:
    """
    从近 N 天的重要邮件提取焦点事项。
    返回新增 pending 条目数。
    """
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()

    with main_db.get_conn() as conn:
        import config as _cfg
        rows = conn.execute("""
            SELECT id, from_addr, subject, summary, importance, date, draft_reply
            FROM emails
            WHERE importance >= ?
              AND date >= ?
              AND status = 'unread'
              AND from_addr != ?
            ORDER BY importance DESC, date DESC
            LIMIT 30
        """, (min_importance, cutoff, _cfg.NETEASE_EMAIL)).fetchall()

    if not rows:
        return 0

    _safe_print(f"[FocusUpdater] 分析 {len(rows)} 封重要邮件...")

    # 构建邮件索引（id → row），方便提取后关联
    email_map = {str(r["id"]): r for r in rows}

    email_texts = []
    for r in rows:
        email_texts.append(
            f'[id:{r["id"]}] 发件人:{r["from_addr"]}  主题:{r["subject"]}  摘要:{r["summary"] or "无"}'
        )

    prompt = f"""从以下邮件中提取需要写入焦点清单的行动项。

邮件列表（按重要性排序）:
{chr(10).join(email_texts)}

提取规则:
- 只提取有明确行动要求或截止日期的条目
- 会议通知、审稿邀请、修改意见、项目进展等均需提取
- 广告/自动通知/无行动要求的跳过
- 系统自动发送的日报/简报/报告邮件不提取
- priority: urgent（今天/明天）/ normal / waiting

以JSON数组输出，每项格式:
{{
  "email_id": "来源邮件的id字段（原样复制）",
  "text": "简洁描述（20字内）",
  "deadline": "YYYY-MM-DD 或 空",
  "priority": "urgent/normal/waiting",
  "project": "关联项目名（如有）",
  "confidence": 0.0-1.0
}}
只输出JSON数组，无相关内容则输出 []。"""

    added = 0
    try:
        raw = ai_client.chat(
            messages=[{"role": "user", "content": prompt}],
            system_prompt="你是任务提取专家。从邮件中精准提取行动项，不要过度提取，宁少勿滥。",
            temperature=0.2,
        )
        raw = raw.strip().strip("```json").strip("```").strip()
        items = json.loads(raw)
        if not isinstance(items, list):
            items = []

        for item in items:
            text = item.get("text", "").strip()
            if not text:
                continue
            email_id = str(item.get("email_id", ""))
            src_row = email_map.get(email_id)
            from_name = src_row["from_addr"] if src_row else ""
            db_ref = f"email:{email_id}" if email_id else ""
            pq.add_focus(
                text=text,
                source="email",
                deadline=item.get("deadline", ""),
                project=item.get("project", ""),
                db_ref=db_ref,
                priority=item.get("priority", "normal"),
                confidence=float(item.get("confidence", 0.6)),
                from_name=from_name,
            )
            added += 1

    except Exception as e:
        _safe_print(f"[FocusUpdater] 邮件提取失败: {e}")

    _safe_print(f"[FocusUpdater] 邮件 → {added} 条 pending")
    return added


# ── 微信私聊提取 ──────────────────────────────────────────────────────────

def extract_from_wechat_private(days: int = 3) -> int:
    """
    从近 N 天的微信私聊提取焦点事项。
    按联系人角色决定分析深度。
    """
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    added = 0

    # 按角色分组处理
    for role, depth in ROLE_ANALYSIS_DEPTH.items():
        if depth == "skip":
            continue

        wxids = get_contacts_by_role(role)
        if not wxids:
            continue

        for wxid in wxids:
            n = _process_private_chat(wxid, role, depth, cutoff)
            added += n

    _safe_print(f"[FocusUpdater] 微信私聊 → {added} 条 pending")
    return added


def _process_private_chat(wxid: str, role: str, depth: str, cutoff: str) -> int:
    """处理单个联系人的私聊消息"""
    with main_db.get_conn() as conn:
        rows = conn.execute("""
            SELECT content, is_sender, create_time
            FROM wechat_messages
            WHERE talker_wxid=? AND create_time >= ?
            ORDER BY create_time ASC
        """, (wxid, cutoff)).fetchall()

        if not rows:
            return 0

        # 获取联系人显示名
        contact = conn.execute("""
            SELECT COALESCE(remark, nickname, ?) FROM wechat_contacts WHERE wxid=?
        """, (wxid, wxid)).fetchone()
        name = contact[0] if contact else wxid

    msgs = [dict(r) for r in rows]

    if depth == "keyword":
        return _keyword_scan_private(name, msgs, role, wxid)
    else:  # full
        return _full_analyze_private(name, msgs, role, wxid)


def _keyword_scan_private(name: str, msgs: list[dict],
                          role: str, wxid: str = "") -> int:
    """关键词扫描模式：只关注含触发词的消息"""
    added = 0
    for msg in msgs:
        content = msg["content"] or ""
        content_lower = content.lower()
        hits = [kw for kw in TRIGGER_KEYWORDS if kw in content_lower]
        if not hits:
            continue

        # 有触发词，构建简单 pending 条目
        priority = "urgent" if any(
            w in content_lower for w in ("截止", "今天", "明天", "紧急", "尽快")
        ) else "normal"

        pq.add_focus(
            text=content[:60],
            source="wechat",
            priority=priority,
            db_ref=f"wechat:{wxid}",
            from_name=name,
            confidence=0.55,
        )
        added += 1
        if added >= 3:  # 每人最多3条关键词触发
            break

    return added


def _full_analyze_private(name: str, msgs: list[dict],
                          role: str, wxid: str) -> int:
    """全文 AI 分析模式"""
    # 构建对话文本
    lines = []
    for m in msgs[-50:]:  # 最近50条
        speaker = "我" if m["is_sender"] else name
        lines.append(f"[{speaker}] {m['content'][:150]}")
    conversation = "\n".join(lines)

    # junior 角色重点看自己说的话
    focus_hint = ""
    if role == "junior":
        focus_hint = "\n特别注意：重点提取「我」说的承诺/任务，因为这是你对下级的指派。"
    elif role == "superior":
        focus_hint = "\n特别注意：重点提取对方发布的任务/要求/截止日期。"

    prompt = f"""分析以下微信对话（与{name}，角色：{role}），提取行动项。{focus_hint}

对话内容:
{conversation}

提取规则:
- 只提取有明确行动要求或承诺的条目
- 截止日期、任务指派、需要跟进的事项
- 纯闲聊/已完成的事跳过

以JSON数组输出:
{{
  "text": "简洁描述（20字内）",
  "deadline": "YYYY-MM-DD 或 空",
  "priority": "urgent/normal/waiting",
  "project": "关联项目（如有）",
  "confidence": 0.0-1.0
}}
无相关内容则输出 []。"""

    added = 0
    try:
        raw = ai_client.chat(
            messages=[{"role": "user", "content": prompt}],
            system_prompt="你是任务提取专家。精准提取承诺和任务，宁少勿滥。",
            temperature=0.2,
        )
        raw = raw.strip().strip("```json").strip("```").strip()
        items = json.loads(raw)
        if not isinstance(items, list):
            items = []

        for item in items:
            text = item.get("text", "").strip()
            if not text:
                continue
            pq.add_focus(
                text=text,
                source="wechat",
                deadline=item.get("deadline", ""),
                project=item.get("project", ""),
                db_ref=f"wechat:{wxid}",
                from_name=name,
                priority=item.get("priority", "normal"),
                confidence=float(item.get("confidence", 0.6)),
            )
            added += 1

    except Exception as e:
        _safe_print(f"[FocusUpdater] 私聊分析失败({name}): {e}")

    return added


# ── 微信群聊提取 ──────────────────────────────────────────────────────────

def extract_from_wechat_groups(days: int = 3) -> int:
    """
    从微信群聊提取焦点事项。
    触发条件: @我 / 用户回复"收到" / 含截止词 / 用户主动发言
    """
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    added = 0

    core_groups   = get_core_groups()
    normal_groups = get_normal_groups()

    # 核心群：全触发条件
    for wxid in core_groups:
        n = _process_group(wxid, cutoff, is_core=True)
        added += n

    # 普通群：仅关键词触发
    for wxid in normal_groups:
        n = _process_group(wxid, cutoff, is_core=False)
        added += n

    _safe_print(f"[FocusUpdater] 微信群聊 → {added} 条 pending")
    return added


def _process_group(wxid: str, cutoff: str, is_core: bool) -> int:
    """处理单个群的消息"""
    with main_db.get_conn() as conn:
        # 获取群名
        group = conn.execute(
            "SELECT COALESCE(display_name, name, ?) FROM wechat_groups WHERE wxid=?",
            (wxid, wxid)
        ).fetchone()
        group_name = group[0] if group else wxid

        rows = conn.execute("""
            SELECT content, is_sender, create_time
            FROM wechat_messages
            WHERE talker_wxid=? AND create_time >= ?
            ORDER BY create_time ASC
        """, (wxid, cutoff)).fetchall()

    if not rows:
        return 0

    msgs = [dict(r) for r in rows]

    # ── 扫描触发条件 ────────────────────────────────────────────────────
    triggered_segments = []  # [(reason, messages_context)]

    for i, msg in enumerate(msgs):
        content = msg["content"] or ""
        content_lower = content.lower()

        # 1. 用户回复了"收到"类 → 前一条消息是任务
        if msg["is_sender"] and any(ack in content for ack in ACKNOWLEDGEMENT_WORDS):
            if i > 0:
                prev = msgs[i - 1]
                context = _get_context_window(msgs, i, window=5)
                triggered_segments.append(("ack_received", context, prev["content"]))

        # 2. @用户（简单检测：通常包含 @ 符号）
        elif not msg["is_sender"] and "@" in content and is_core:
            context = _get_context_window(msgs, i, window=3)
            triggered_segments.append(("at_mention", context, content))

        # 3. 含截止词（核心群必看，普通群也看）
        elif not msg["is_sender"] and any(kw in content_lower for kw in TRIGGER_KEYWORDS):
            context = _get_context_window(msgs, i, window=3)
            triggered_segments.append(("deadline_keyword", context, content))

        # 4. 用户主动发言（非回复型）—— 只在核心群
        elif msg["is_sender"] and is_core and len(content) > 10:
            if not any(ack in content for ack in ACKNOWLEDGEMENT_WORDS):
                context = _get_context_window(msgs, i, window=2)
                triggered_segments.append(("my_message", context, content))

    if not triggered_segments:
        return 0

    # 去重合并相邻触发片段
    triggered_segments = triggered_segments[:10]  # 最多10个触发点

    return _analyze_group_triggers(group_name, triggered_segments, wxid)


def _get_context_window(msgs: list[dict], center: int,
                        window: int = 3) -> list[dict]:
    """获取消息上下文窗口"""
    start = max(0, center - window)
    end   = min(len(msgs), center + window + 1)
    return msgs[start:end]


def _analyze_group_triggers(group_name: str,
                             segments: list[tuple],
                             wxid: str) -> int:
    """用 AI 分析群聊触发片段，提取行动项"""
    # 构建分析文本
    sections = []
    for reason, context_msgs, trigger_content in segments:
        reason_desc = {
            "ack_received": "【你回复了收到，前文可能是任务】",
            "at_mention":   "【有人@你】",
            "deadline_keyword": "【含截止日期关键词】",
            "my_message":   "【你主动发言】",
        }.get(reason, "【触发】")

        ctx_text = "\n".join(
            f"  {'我' if m['is_sender'] else '群成员'}: {m['content'][:100]}"
            for m in context_msgs
        )
        sections.append(f"{reason_desc}\n{ctx_text}")

    full_text = f"群名：{group_name}\n\n" + "\n\n".join(sections)

    prompt = f"""分析以下微信群聊片段，提取需要行动的事项。

{full_text}

提取规则:
- "收到"前的消息通常是任务指派，高优先提取
- 提取有明确截止、需要回复/执行的事项
- 已完成/纯通知跳过

以JSON数组输出:
{{
  "text": "描述（20字内）",
  "deadline": "YYYY-MM-DD 或 空",
  "priority": "urgent/normal/waiting",
  "project": "关联项目（如有）",
  "confidence": 0.0-1.0
}}
无相关内容则输出 []。"""

    added = 0
    try:
        raw = ai_client.chat(
            messages=[{"role": "user", "content": prompt}],
            system_prompt="你是任务提取专家。聚焦群聊中的明确任务。",
            temperature=0.2,
        )
        raw = raw.strip().strip("```json").strip("```").strip()
        items = json.loads(raw)
        if not isinstance(items, list):
            items = []

        for item in items:
            text = item.get("text", "").strip()
            if not text:
                continue
            pq.add_focus(
                text=text,
                source="wechat_group",
                deadline=item.get("deadline", ""),
                project=item.get("project", ""),
                db_ref=f"wechat:{wxid}",
                from_name=group_name,
                priority=item.get("priority", "normal"),
                confidence=float(item.get("confidence", 0.6)),
            )
            added += 1

    except Exception as e:
        _safe_print(f"[FocusUpdater] 群分析失败({group_name}): {e}")

    return added


# ── 群文件索引 ────────────────────────────────────────────────────────────

def index_group_files(wxid: str, group_name: str):
    """
    将核心群的文件名记录到 Layer 4（FTS5 索引）。
    实际文件内容由 directory_indexer 处理。
    """
    # 微信群文件通常在 WeChat Files/<wxid>/FileStorage/File/ 下
    import os
    from pathlib import Path
    from memory.fts_store import add_document as fts_add

    base_dirs = [
        Path(f"C:/Users/{os.getenv('USERNAME', 'user')}/Documents/WeChat Files"),
        Path(f"C:/Users/{os.getenv('USERNAME', 'user')}/Documents/微信文件"),
    ]

    file_count = 0
    for base in base_dirs:
        file_dir = base / wxid / "FileStorage" / "File"
        if not file_dir.exists():
            continue
        for f in file_dir.rglob("*"):
            if f.is_file() and f.suffix.lower() in (
                ".pdf", ".docx", ".xlsx", ".pptx", ".txt", ".md"
            ):
                doc_id = f"group_file:{wxid}:{f.name}"
                fts_add(
                    doc_id=doc_id,
                    collection="wechat_files",
                    text=f"群文件 [{group_name}]: {f.name}",
                    source=str(f),
                    metadata={"group": group_name, "wxid": wxid,
                               "file": f.name, "path": str(f)},
                )
                file_count += 1

    if file_count:
        _safe_print(f"[FocusUpdater] 群文件索引: {group_name} → {file_count} 个文件")


# ── 主入口 ───────────────────────────────────────────────────────────────

def run_focus_update(send_email: bool = True) -> dict:
    """
    完整的 focus 更新流程。
    1. 检查是否需要先推断角色
    2. 从三个来源提取
    3. 发送确认邮件
    4. 自动通过超时的高置信度条目
    5. 应用已审核条目

    返回统计信息。
    """
    from memory.pending import (
        auto_apply_timeout, apply_approved, format_for_email, count_pending
    )
    from email_module.sender import send_email as send

    stats = {"email": 0, "wechat": 0, "groups": 0, "total_new": 0,
             "auto_applied": 0, "applied": 0}

    # ── 0. 检查是否需要先推断角色 ───────────────────────────────────────
    _maybe_init_roles()

    # ── 1. 提取 ─────────────────────────────────────────────────────────
    stats["email"]   = extract_from_emails()
    stats["wechat"]  = extract_from_wechat_private()
    stats["groups"]  = extract_from_wechat_groups()
    stats["total_new"] = stats["email"] + stats["wechat"] + stats["groups"]

    # ── 2. 核心群文件索引 ────────────────────────────────────────────────
    for wxid in get_core_groups():
        with main_db.get_conn() as conn:
            g = conn.execute(
                "SELECT COALESCE(display_name, name, ?) FROM wechat_groups WHERE wxid=?",
                (wxid, wxid)
            ).fetchone()
        index_group_files(wxid, g[0] if g else wxid)

    # ── 3. 自动通过超时条目 ──────────────────────────────────────────────
    stats["auto_applied"] = auto_apply_timeout()
    if stats["auto_applied"]:
        _safe_print(f"[FocusUpdater] 自动通过 {stats['auto_applied']} 条超时高置信度条目")

    # ── 4. 应用已审核条目 ────────────────────────────────────────────────
    stats["applied"] = apply_approved()
    if stats["applied"]:
        _safe_print(f"[FocusUpdater] 已写入记忆层: {stats['applied']} 条")

    # ── 5. 发送确认邮件 ──────────────────────────────────────────────────
    pending_count = count_pending()
    if send_email and pending_count > 0:
        body = format_for_email()
        subject = f"📌 Aegis: {pending_count} 条待确认信息（邮件+微信提取）"
        ok = send(config.NETEASE_EMAIL, subject, body)
        if ok:
            _safe_print(f"[FocusUpdater] 已发送确认邮件: {pending_count} 条待审核")

    _safe_print(
        f"[FocusUpdater] 完成 — "
        f"新增:{stats['total_new']} 待审:{pending_count} "
        f"已应用:{stats['applied']}"
    )
    return stats


def _maybe_init_roles():
    """首次运行时自动推断联系人角色"""
    try:
        from scanner.wechat_decrypt import _ensure_wechat_tables
        _ensure_wechat_tables()

        with main_db.get_conn() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM wechat_contacts WHERE is_group=0"
            ).fetchone()[0]
            unclassified = conn.execute(
                "SELECT COUNT(*) FROM wechat_contacts "
                "WHERE is_group=0 AND role='unknown'"
            ).fetchone()[0]

        # 超过 60% 未分类时触发推断
        if total > 0 and unclassified / total > 0.6:
            _safe_print("[FocusUpdater] 首次运行，开始推断联系人角色...")
            results = infer_roles(top_contacts=30, top_groups=20)

            # 发送角色摘要邮件
            from scanner.wechat_roles import format_role_summary
            from email_module.sender import send_email as send
            from memory.pending import format_for_email

            summary = format_role_summary()
            pending_roles = format_for_email()
            body = (
                "Aegis已自动推断微信联系人角色，请确认：\n\n"
                f"{summary}\n\n"
                "部分低置信度联系人需要你手动确认：\n\n"
                f"{pending_roles}\n\n"
                "手动修正指令：\n"
                "  Aegis: 设置联系人 张三 角色=合作者\n"
                "  Aegis: 设置群 课题组群 类型=core\n"
                "  Aegis: 设置联系人 李四 角色=junior"
            )
            send(config.NETEASE_EMAIL, "🤖 Aegis：微信联系人角色推断完成，请确认", body)

    except Exception as e:
        _safe_print(f"[FocusUpdater] 角色初始化失败（非致命）: {e}")


# ── 邮件指令处理扩展 ─────────────────────────────────────────────────────

def handle_role_command(instruction: str) -> str:
    """
    处理角色设置指令（由 command_handler 调用）。
    指令格式:
      Aegis: 设置联系人 张三 角色=合作者
      Aegis: 设置群 课题组群 类型=core
    """
    from scanner.wechat_roles import set_contact_role, set_group_type, ROLES, GROUP_TYPES

    # 联系人角色设置
    m = re.search(r'设置联系人?\s+(\S+)\s+角色[=＝](\S+)', instruction)
    if m:
        name, role_str = m.group(1), m.group(2)
        # 中文角色名映射
        role_map = {
            "上级": "superior", "导师": "superior", "老板": "superior",
            "合作者": "collaborator", "合作": "collaborator",
            "下级": "junior", "学生": "junior",
            "同事": "colleague", "同学": "colleague",
            "亲密": "close_personal", "家人": "close_personal", "恋人": "close_personal",
            "朋友": "friend",
            "服务": "service", "商家": "service",
        }
        role = role_map.get(role_str, role_str)
        ok = set_contact_role(name, role)
        if ok:
            return f"✅ 已设置 {name} 角色 → {role}（{ROLES.get(role, role)}）"
        return f"❌ 未找到联系人或角色无效: {name} / {role_str}"

    # 群类型设置
    m = re.search(r'设置群\s+(\S+)\s+类型[=＝](\S+)', instruction)
    if m:
        name, type_str = m.group(1), m.group(2)
        type_map = {"核心": "core", "重要": "core", "普通": "normal", "忽略": "noise", "跳过": "noise"}
        gtype = type_map.get(type_str, type_str)
        ok = set_group_type(name, gtype)
        if ok:
            return f"✅ 已设置群 {name} 类型 → {gtype}（{GROUP_TYPES.get(gtype, gtype)}）"
        return f"❌ 未找到群或类型无效: {name} / {type_str}"

    return ""
