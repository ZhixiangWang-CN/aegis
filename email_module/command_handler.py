"""
Aegis邮件指令系统 — 通过邮件下达命令

检测规则（二选一即可）:
  1. 主题以 "Aegis:" 或 "Aegis:" 开头（新邮件指令）
  2. 收到来自用户自己的回复邮件（In-Reply-To 包含Aegis发送的邮件 ID）

支持的指令（AI 自由解析，以下是核心动作）:
  - 发送邮件 / 回复 [联系人] [内容]
  - 查询 [关键词]  — 语义搜索知识库
  - 总结 [话题]   — 汇总近期相关邮件
  - 简报          — 立即生成今日简报
  - 状态          — 系统运行状态
  - 记住 [信息]   — 写入个人档案
  - 联系人 [查询] — 查询联系人信息
"""
from __future__ import annotations

import email
import hashlib
import imaplib
import time
from email.utils import parseaddr

import config
from email_module.reader import _connect, _decode_header_value, _extract_body, _safe_print
from email_module.sender import send_email
from memory import db
from ai import client as ai

# 指令触发前缀
COMMAND_PREFIXES = ("aegis:", "jv:", "jarvis:")


def _is_command_email(subject: str, from_addr: str, in_reply_to: str = "") -> bool:
    """判断是否是用户给Aegis的指令邮件"""
    subject_lower = subject.lower().strip()
    # 1. 主题前缀触发
    if any(subject_lower.startswith(p) for p in COMMAND_PREFIXES):
        return True
    # 2. 用户回复Aegis发出的邮件（From 是用户自己）
    if from_addr in (config.NETEASE_EMAIL, config.GMAIL_EMAIL or ""):
        if in_reply_to:  # 有 In-Reply-To 头说明是回复
            return True
    return False


def fetch_commands() -> list[dict]:
    """
    拉取用户发给Aegis的指令邮件。
    返回列表，每项含 id/subject/body/from_addr/reply_to_subject
    """
    mail = _connect()
    if not mail:
        return []

    commands = []
    try:
        mail.select("INBOX")
        # 搜索用户自己发来的邮件（回复或主题前缀）
        # 注意：163 IMAP SEARCH FROM 支持不稳定，改为批量头部扫描
        status, data = mail.search(None, "UNSEEN")
        if status != "OK":
            return []

        all_unread = data[0].split()
        for eid in reversed(all_unread[-100:]):   # 最近100封未读
            try:
                status, hdr_data = mail.fetch(eid, "(RFC822.HEADER)")
                if status != "OK":
                    continue
                msg = email.message_from_bytes(hdr_data[0][1])
                subject    = _decode_header_value(msg.get("Subject", ""))
                from_raw   = _decode_header_value(msg.get("From", ""))
                _, from_addr = parseaddr(from_raw)
                in_reply_to = msg.get("In-Reply-To", "")
                date_str   = msg.get("Date", "")
                msg_id     = msg.get("Message-ID", "")

                if not _is_command_email(subject, from_addr, in_reply_to):
                    continue

                # 是指令邮件，拉取正文
                status2, full_data = mail.fetch(eid, "(RFC822)")
                if status2 != "OK":
                    continue
                full_msg = email.message_from_bytes(full_data[0][1])
                body = _extract_body(full_msg)

                uid = hashlib.md5(
                    (msg_id or f"{from_addr}{subject}{date_str}").encode()
                ).hexdigest()

                # 跳过已处理的指令
                if db.command_exists(uid):
                    continue

                commands.append({
                    "id": uid,
                    "imap_id": eid,
                    "from_addr": from_addr,
                    "subject": subject,
                    "body": body.strip(),
                    "date": date_str,
                    "in_reply_to": in_reply_to,
                })

            except Exception as e:
                _safe_print(f"[Cmd] 解析失败: {e}")

        mail.logout()

    except Exception as e:
        _safe_print(f"[Cmd] 拉取失败: {e}")

    return commands


def _extract_command_text(subject: str, body: str) -> str:
    """从邮件中提取指令文本"""
    # 去掉主题前缀
    subject_lower = subject.lower().strip()
    cmd_from_subject = subject
    for p in COMMAND_PREFIXES:
        if subject_lower.startswith(p):
            cmd_from_subject = subject[len(p):].strip()
            break

    # 正文里去掉引用部分（回复邮件通常包含原文引用）
    clean_body = []
    for line in body.splitlines():
        # 跳过引用行（以 > 开头）
        if line.strip().startswith(">"):
            break
        clean_body.append(line)
    body_text = "\n".join(clean_body).strip()

    # 优先用正文，正文为空则用主题
    return body_text if body_text else cmd_from_subject


def _lookup_contact_email(name: str) -> str:
    """按姓名/备注模糊搜索联系人邮件地址"""
    try:
        with db.get_conn() as conn:
            rows = conn.execute("""
                SELECT email, display_name FROM contacts
                WHERE display_name LIKE ? AND email IS NOT NULL AND email != ''
                ORDER BY importance DESC LIMIT 3
            """, (f"%{name}%",)).fetchall()
            if rows:
                return rows[0]["email"]
            # 也查邮件发件人
            rows2 = conn.execute("""
                SELECT from_addr FROM emails
                WHERE (from_name LIKE ? OR from_addr LIKE ?)
                  AND from_addr NOT LIKE '%noreply%'
                ORDER BY importance DESC LIMIT 1
            """, (f"%{name}%", f"%{name}%")).fetchone()
            return rows2["from_addr"] if rows2 else ""
    except Exception:
        return ""


def _execute_command(instruction: str, context: dict) -> str:
    """
    用 AI 解析并执行指令，返回结果文本。
    """
    from memory import profile
    from memory.memory_manage import get_summary as mm_summary, add_fact

    # 构建 AI 执行 prompt
    system_prompt = (
        "你是Aegis，用户的AI助理。用户通过邮件给你下达了一条指令。\n"
        "你需要：\n"
        "1. 理解指令意图\n"
        "2. 决定执行哪个动作（见下方）\n"
        "3. 生成执行结果或回复内容\n\n"
        "可执行的动作类型：\n"
        "  REPLY_EMAIL   — 起草并发送邮件给某联系人\n"
        "  SEARCH        — 搜索知识库或邮件\n"
        "  SUMMARIZE     — 汇总某个话题的近期邮件\n"
        "  BRIEFING      — 生成今日简报\n"
        "  STATUS        — 系统状态报告\n"
        "  REMEMBER      — 记录到个人档案\n"
        "  CONTACT_QUERY — 查询联系人信息\n"
        "  WRITE_WORD    — 生成 Word 文档（可含表格）\n"
        "  SEND_FILE     — 将本地文件作为附件发送到用户邮箱\n"
        "  CHAT          — 普通问答/对话\n\n"
        "REPLY_EMAIL 的 params 格式：\n"
        '  {"to": "email@addr.com（如知道）", "to_name": "联系人姓名", '
        '"subject": "邮件主题", "draft": "邮件正文（代替用户写，语气自然专业）"}\n\n'
        "WRITE_WORD 的 params 格式：\n"
        '  {"instruction": "文档生成指令（完整描述想要什么内容）", '
        '"send_to_email": true或false（是否生成后自动发给用户）}\n\n'
        "SEND_FILE 的 params 格式：\n"
        '  {"file_path": "文件完整路径或文件名关键词", '
        '"subject": "邮件主题（可选）"}\n\n'
        "以JSON格式输出：\n"
        '{"action": "ACTION_TYPE", "params": {...}, "response": "给用户的回复文本"}\n'
        "只输出JSON。"
    )

    user_content = (
        f"用户指令：{instruction}\n\n"
        f"当前个人档案摘要：\n{mm_summary()}\n\n"
        f"请解析指令并生成回复。"
    )

    import json
    try:
        raw = ai.chat(
            messages=[{"role": "user", "content": user_content}],
            system_prompt=system_prompt,
            temperature=0.3,
        )
        raw = raw.strip().strip("```json").strip("```").strip()
        parsed = json.loads(raw)
        action   = parsed.get("action", "CHAT")
        params   = parsed.get("params", {})
        response = parsed.get("response", "")
    except Exception:
        action, params, response = "CHAT", {}, ""

    # ── 执行具体动作 ──────────────────────────────────────────
    result_text = response

    if action == "BRIEFING":
        try:
            from scheduler.jobs import send_daily_briefing
            send_daily_briefing()
            result_text = "✅ 今日简报已生成并发送，请查收邮件。"
        except Exception as e:
            result_text = f"简报生成失败: {e}"

    elif action == "STATUS":
        with db.get_conn() as conn:
            emails_c = conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0]
            imp_c    = conn.execute("SELECT COUNT(*) FROM emails WHERE importance>=4").fetchone()[0]
            files_c  = conn.execute("SELECT COUNT(*) FROM file_index").fetchone()[0]
            vec_c    = conn.execute("SELECT COUNT(*) FROM file_index WHERE status='indexed'").fetchone()[0]
            cont_c   = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
        result_text = (
            f"Aegis系统状态\n\n"
            f"邮件: {emails_c} 封已处理 | 重要(★4+): {imp_c} 封\n"
            f"联系人: {cont_c} 个\n"
            f"文件索引: {files_c} 个 | 已向量化: {vec_c} 个\n"
            f"调度器: 每30分钟检查邮件 | 每天08:00简报 | 每天03:00向量化\n"
        )

    elif action == "REMEMBER":
        fact = params.get("fact") or instruction
        try:
            add_fact(fact)
            result_text = f"✅ 已记录到个人档案：{fact}"
        except Exception as e:
            result_text = f"记录失败: {e}"

    elif action == "SEARCH":
        query = params.get("query", instruction)
        result_text = _search_knowledge(query)

    elif action == "SUMMARIZE":
        topic = params.get("topic", instruction)
        result_text = _summarize_topic(topic)

    elif action == "CONTACT_QUERY":
        query = params.get("query", instruction)
        result_text = _query_contacts(query)

    elif action == "REPLY_EMAIL":
        to_addr  = params.get("to", "")
        to_name  = params.get("to_name", "")
        draft    = params.get("draft", response)
        subject  = params.get("subject", "")

        # 如果只有名字没有地址，尝试从联系人库查找
        if to_name and not to_addr:
            to_addr = _lookup_contact_email(to_name)

        # 补全主题
        if not subject:
            subject = f"回复: {to_name or to_addr}"

        if to_addr and "@" in to_addr:
            ok = send_email(to_addr, subject, draft)
            result_text = (
                f"✅ 已发送邮件给 {to_name or to_addr} <{to_addr}>\n"
                f"主题: {subject}\n\n内容:\n{draft}"
                if ok else f"❌ 发送失败: {to_addr}"
            )
        else:
            result_text = (
                f"⚠️ 未找到 '{to_name or to_addr}' 的邮件地址，草稿如下：\n\n"
                f"主题: {subject}\n\n{draft}"
            )

    elif action == "WRITE_WORD":
        instruction_text = params.get("instruction", instruction)
        send_to_email = params.get("send_to_email", True)
        try:
            from tools.document_builder import ai_generate_doc
            doc_path, description = ai_generate_doc(instruction_text)
            result_text = f"✅ 文档已生成: {doc_path.name}\n描述: {description}"
            if send_to_email:
                import config as _cfg
                ok = send_email(
                    to=_cfg.NETEASE_EMAIL,
                    subject=f"📄 Aegis文档: {doc_path.stem}",
                    body=f"Aegis已根据您的指令生成文档，详见附件。\n\n指令: {instruction_text}",
                    attachments=[str(doc_path)],
                )
                result_text += f"\n{'✅ 文档已发送到您的邮箱' if ok else '❌ 邮件发送失败，文档保存在: ' + str(doc_path)}"
        except Exception as e:
            result_text = f"❌ 文档生成失败: {e}"

    elif action == "SEND_FILE":
        file_path_str = params.get("file_path", "")
        email_subject = params.get("subject", f"📎 Aegis发送文件")
        try:
            from pathlib import Path as _Path
            import config as _cfg
            # 先尝试直接路径
            p = _Path(file_path_str)
            if not p.exists():
                # 在 data/documents/ 下搜索关键词
                matches = list((_cfg.DATA_DIR / "documents").glob(f"*{file_path_str}*"))
                if matches:
                    p = matches[0]
            if p.exists():
                ok = send_email(
                    to=_cfg.NETEASE_EMAIL,
                    subject=email_subject,
                    body=f"Aegis附件发送\n文件: {p.name}",
                    attachments=[str(p)],
                )
                result_text = (
                    f"✅ 文件已发送到邮箱: {p.name} ({p.stat().st_size // 1024}KB)"
                    if ok else f"❌ 发送失败，文件路径: {p}"
                )
            else:
                result_text = f"⚠️ 找不到文件: {file_path_str}\n提示: 可以指定完整路径，或文件保存在 data/documents/ 目录下"
        except Exception as e:
            result_text = f"❌ 文件发送失败: {e}"

    # ── pending 审核指令 ──────────────────────────────────────────
    # 处理 "Aegis: 确认 1,3,5" / "Aegis: 确认全部" / "Aegis: 拒绝 2,4"
    from memory.pending import parse_review_command, approve_by_ids, approve_all, reject, apply_approved
    review_action, review_ids = parse_review_command(instruction)
    if review_action == "approve":
        n = approve_by_ids(review_ids)
        applied = apply_approved()
        result_text = f"✅ 已通过 {n} 条，写入记忆层 {applied} 条"
    elif review_action == "approve_all":
        n = approve_all()
        applied = apply_approved()
        result_text = f"✅ 已通过全部 {n} 条，写入记忆层 {applied} 条"
    elif review_action == "reject":
        n = sum(1 for i in review_ids if reject(i))
        result_text = f"✅ 已拒绝 {n} 条"

    # ── 微信角色/群类型设置指令 ─────────────────────────────────
    if not result_text or result_text == "✅ 指令已执行":
        try:
            from scheduler.focus_updater import handle_role_command
            role_result = handle_role_command(instruction)
            if role_result:
                result_text = role_result
        except Exception:
            pass

    return result_text or "✅ 指令已执行"


def _search_knowledge(query: str) -> str:
    """两阶段混合搜索（FTS5 + 向量 RRF 融合）"""
    try:
        from memory.context_inject import search_knowledge
        return search_knowledge(query, top_k=8)
    except Exception as e:
        return f"搜索失败: {e}"


def _summarize_topic(topic: str) -> str:
    """汇总近期某话题的邮件"""
    with db.get_conn() as conn:
        rows = conn.execute("""
            SELECT from_addr, subject, summary, importance, date
            FROM emails
            WHERE (subject LIKE ? OR summary LIKE ?)
              AND importance >= 2
            ORDER BY importance DESC, date DESC
            LIMIT 10
        """, (f"%{topic}%", f"%{topic}%")).fetchall()

    if not rows:
        return f"未找到与「{topic}」相关的邮件。"

    lines = [f"关于「{topic}」的近期邮件（共{len(rows)}封）：\n"]
    for r in rows:
        lines.append(f"  ★{r[3]} [{r[0]}] {r[1]}\n  摘要: {r[2] or '—'}")
    summary_text = "\n".join(lines)

    # AI 综合汇总
    condensed = ai.chat(
        messages=[{"role": "user", "content": f"请用100字内汇总以下邮件列表的核心信息：\n\n{summary_text}"}],
        system_prompt="你是Aegis，简洁专业。",
        temperature=0.3,
    )
    return summary_text + f"\n\nAI汇总：{condensed}"


def _query_contacts(query: str) -> str:
    """查询联系人信息"""
    with db.get_conn() as conn:
        rows = conn.execute("""
            SELECT email, name, institution, institution_type, role, importance, notes
            FROM contacts
            WHERE email LIKE ? OR name LIKE ? OR institution LIKE ?
            ORDER BY importance DESC
            LIMIT 5
        """, (f"%{query}%", f"%{query}%", f"%{query}%")).fetchall()

    if not rows:
        return f"未找到「{query}」相关联系人。"

    lines = [f"联系人查询「{query}」：\n"]
    for r in rows:
        lines.append(
            f"  ★{r[5]} {r[1]} <{r[0]}>\n"
            f"  机构: {r[2] or '—'} [{r[3]}/{r[4]}]\n"
            f"  备注: {r[6] or '—'}"
        )
    return "\n".join(lines)


def process_commands():
    """
    主入口：拉取并处理所有待执行的用户指令。
    由 scheduler 定期调用。
    """
    commands = fetch_commands()
    if not commands:
        return

    _safe_print(f"[Cmd] 发现 {len(commands)} 条用户指令")

    for cmd in commands:
        try:
            instruction = _extract_command_text(cmd["subject"], cmd["body"])

            # 注入防护检查
            from email_module.injection_guard import is_safe, scan as iscan
            if not is_safe(instruction):
                scan_result = iscan(instruction)
                _safe_print(f"[Cmd] ⚠️ 拒绝执行（{scan_result}）: {instruction[:60]}")
                send_email(config.NETEASE_EMAIL,
                           "⚠️ Aegis安全警告",
                           f"拒绝执行可疑指令:\n{instruction[:200]}\n\n原因: {scan_result}")
                db.save_command(cmd["id"], f"[BLOCKED] {instruction}", str(scan_result))
                continue

            _safe_print(f"[Cmd] 执行: {instruction[:60]}...")

            result = _execute_command(instruction, context=cmd)

            # 存库（避免重复处理）
            db.save_command(cmd["id"], instruction, result)

            # 回复结果给用户
            reply_subject = f"✅ Aegis回复: {cmd['subject'][:40]}"
            send_email(config.NETEASE_EMAIL, reply_subject, result)
            _safe_print(f"[Cmd] 已回复: {reply_subject}")

        except Exception as e:
            _safe_print(f"[Cmd] 处理失败: {e}")
