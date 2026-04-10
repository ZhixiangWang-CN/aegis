"""邮件 AI 处理: 分析重要性、生成摘要、起草回复，写入数据库 + FTS5 + pending 队列"""
from ai import client as ai
from memory import db, vector_store
from email_module.contacts import upsert_contact


def _safe_print(msg: str):
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("ascii", errors="replace").decode("ascii"))


def _index_to_fts(email_id: str, subject: str, body: str, summary: str,
                  from_addr: str, date: str, importance: int):
    """将邮件内容写入 FTS5 全文索引（jieba 分词）"""
    try:
        from memory.fts_store import get_store
        fts = get_store()
        text = f"{subject}\n{summary}\n{body[:1000]}"
        fts.add(
            doc_id=f"email_{email_id}",
            collection="emails",
            text=text,
            source="email",
            metadata={"from": from_addr, "date": date, "importance": importance},
        )
    except Exception as e:
        _safe_print(f"[Summarizer] FTS 索引失败: {e}")


def process_new_emails(emails: list[dict]) -> list[dict]:
    """
    对一批新邮件做 AI 分析，写库，返回重要性>=3 的邮件列表（用于推送）
    """
    important = []
    for em in emails:
        try:
            result = ai.analyze_email(
                subject=em["subject"],
                sender=em["from_addr"],
                body=em["body"],
            )
            importance   = result.get("importance", 2)
            summary      = result.get("summary", em["subject"])
            category     = result.get("category", "其他")
            needs_reply  = result.get("needs_reply", False)
            draft_reply  = result.get("draft_reply")

            # 写入数据库
            db.save_email(
                email_id=em["id"],
                from_addr=em["from_addr"],
                subject=em["subject"],
                date=em["date"],
                body=em["body"],
                summary=summary,
                importance=importance,
                category=category,
                needs_reply=needs_reply,
                draft_reply=draft_reply,
            )

            # 存入向量库（语义检索）
            vector_store.add_document(
                collection_name="emails",
                doc_id=em["id"],
                text=f"{em['subject']} {summary}",
                metadata={
                    "from": em["from_addr"],
                    "date": em["date"],
                    "importance": importance,
                },
            )

            # 存入 FTS5 全文索引（关键词检索）
            _index_to_fts(
                email_id=em["id"],
                subject=em["subject"],
                body=em["body"],
                summary=summary,
                from_addr=em["from_addr"],
                date=em["date"],
                importance=importance,
            )

            # 更新联系人记录
            upsert_contact(
                email_addr=em["from_addr"],
                display_name=em["from_addr"].split("@")[0],
                subject=em["subject"],
                body_preview=em["body"][:200],
            )

            if importance >= 3:
                important.append({**em, **result})
                _safe_print(f"[Summarizer] 重要邮件(★{importance}): {em['subject']}")

                # importance >= 4: 推送摘要到 pending 队列 + 桌面通知
                if importance >= 4 and summary:
                    try:
                        from memory.pending import add_focus
                        deadline = result.get("deadline", "")
                        project  = result.get("project", "")
                        add_focus(
                            text=f"邮件[{em['from_addr']}]: {summary}",
                            source="email",
                            deadline=deadline,
                            project=project,
                            confidence=min(importance / 5.0 * 0.8, 0.9),
                        )
                    except Exception:
                        pass
                    # 桌面通知：重要邮件到达
                    try:
                        from notifier import notify_email
                        notify_email(
                            subject=em["subject"],
                            sender=em["from_addr"],
                            score=importance,
                        )
                    except Exception:
                        pass
            else:
                _safe_print(f"[Summarizer] 普通邮件(★{importance}): {em['subject']}")

        except Exception as e:
            _safe_print(f"[Summarizer] 处理失败 {em['subject']}: {e}")

    return important
