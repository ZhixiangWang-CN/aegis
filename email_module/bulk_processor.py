"""
历史邮件批量处理
策略: 只拉元数据 → 规则分类 → 重要的才读正文 → AI精读
支持 163 和 Gmail 双邮箱。
"""
import imaplib
import email
import time
from email.header import decode_header
from email.utils import parseaddr
import hashlib
from email_module.reader import _decode_header_value, _extract_body, _connect, _safe_print
from email_module.classifier import classify_email, update_sender_profile
from email_module.summarizer import process_new_emails
from email_module.contacts import upsert_contact
from memory import db
import config


def fetch_all_metadata(months_back: int = 12) -> list[dict]:
    """
    拉取最近 N 个月所有邮件的元数据（不读正文），速度很快。
    """
    mail = _connect()
    if not mail:
        return []

    results = []
    try:
        mail.select("INBOX")

        # 按时间范围搜索
        from datetime import datetime, timedelta
        since_date = (datetime.now() - timedelta(days=months_back * 30)).strftime("%d-%b-%Y")
        status, data = mail.search(None, f'SINCE {since_date}')
        if status != "OK":
            return []

        all_ids = data[0].split()
        _safe_print(f"[Bulk] 共找到 {len(all_ids)} 封邮件（近{months_back}个月）")

        # 批量拉取 ENVELOPE（只含头部，不含正文，速度极快）
        batch_size = 100
        for i in range(0, len(all_ids), batch_size):
            batch = all_ids[i:i + batch_size]
            id_set = b",".join(batch)
            status, msg_data = mail.fetch(id_set, "(ENVELOPE)")
            if status != "OK":
                continue

            for item in msg_data:
                if not isinstance(item, tuple):
                    continue
                try:
                    # ENVELOPE 格式解析
                    raw = item[1].decode("utf-8", errors="replace")
                    # 用 RFC822.HEADER 方式更可靠
                    pass
                except Exception:
                    continue

            # 改用 RFC822.HEADER 拉取头部（更可靠）
            status, header_data = mail.fetch(id_set, "(RFC822.HEADER)")
            for item in header_data:
                if not isinstance(item, tuple):
                    continue
                try:
                    msg = email.message_from_bytes(item[1])
                    subject  = _decode_header_value(msg.get("Subject", ""))
                    from_raw = _decode_header_value(msg.get("From", ""))
                    _, from_addr = parseaddr(from_raw)
                    date_str = msg.get("Date", "")
                    msg_id   = msg.get("Message-ID", "")
                    uid = hashlib.md5(
                        (msg_id or f"{from_addr}{subject}{date_str}").encode()
                    ).hexdigest()
                    results.append({
                        "id": uid,
                        "from_addr": from_addr,
                        "subject": subject,
                        "date": date_str,
                        "imap_id": None,  # 后续精读时用
                    })
                except Exception:
                    continue

            _safe_print(f"[Bulk] 元数据进度: {min(i + batch_size, len(all_ids))}/{len(all_ids)}")
            time.sleep(0.5)  # 避免 IMAP 限速

        mail.logout()
    except Exception as e:
        _safe_print(f"[Bulk] 拉取失败: {e}")

    return results


def process_bulk_emails(months_back: int = 12):
    """
    批量处理历史邮件主入口。
    1. 拉元数据
    2. 规则分类
    3. 重要的拉正文 + AI精读
    """
    _safe_print(f"[Bulk] 开始批量处理近 {months_back} 个月邮件...")

    all_meta = fetch_all_metadata(months_back)
    if not all_meta:
        _safe_print("[Bulk 没有获取到邮件元数据")
        return

    # 统计
    stats = {"total": len(all_meta), "skip": 0, "need_ai": 0, "already_done": 0}

    to_read_full = []   # 需要精读正文的邮件

    for em in all_meta:
        if db.email_exists(em["id"]):
            stats["already_done"] += 1
            continue

        result = classify_email(em["from_addr"], em["subject"])
        importance = result["importance"]
        category   = result["category"]

        # Aegis 自发邮件（日报/提醒/回复）完全跳过，不写库不通知
        if category == "self":
            em["_mark_read"] = True
            stats["skip"] += 1
            continue

        update_sender_profile(em["from_addr"], category, importance)
        upsert_contact(em["from_addr"], em["from_addr"].split("@")[0], em["subject"])

        if not result["need_ai"]:
            # 直接写库，不读正文
            db.save_email(
                email_id=em["id"],
                from_addr=em["from_addr"],
                subject=em["subject"],
                date=em["date"],
                body="",
                summary=f"[{category}] {em['subject']}",
                importance=importance,
                category=category,
                needs_reply=False,
                draft_reply=None,
            )
            # 广告/事务类直接标记为已读
            em["_mark_read"] = True
            stats["skip"] += 1
        else:
            to_read_full.append(em)
            stats["need_ai"] += 1

    _safe_print(f"[Bulk] 分类完成: 总{stats['total']} | 规则归档{stats['skip']} | "
               f"需精读{stats['need_ai']} | 已处理{stats['already_done']}")

    # 精读重要邮件（批量拉正文）
    if to_read_full:
        _fetch_and_process_bodies(to_read_full)

    # 标记广告/事务类邮件为已读
    to_mark_read = [em for em in all_meta if em.get("_mark_read")]
    if to_mark_read:
        _mark_emails_as_read(to_mark_read)

    _safe_print("[Bulk] 批量处理完成！")


# ───────────────────────── Gmail 批量处理 ─────────────────────────

def _connect_gmail_imap():
    """连接 Gmail IMAP"""
    try:
        import imaplib as _imap
        mail = _imap.IMAP4_SSL(config.GMAIL_IMAP_HOST, config.GMAIL_IMAP_PORT)
        mail.login(config.GMAIL_EMAIL, config.GMAIL_APP_PWD)
        return mail
    except Exception as e:
        _safe_print(f"[BulkGmail] 登录失败: {e}")
        return None


def fetch_gmail_metadata(months_back: int = 12) -> list[dict]:
    """拉取 Gmail 最近 N 个月所有邮件的元数据（不读正文）"""
    mail = _connect_gmail_imap()
    if not mail:
        return []

    results = []
    try:
        mail.select("INBOX")
        from datetime import datetime, timedelta
        since_date = (datetime.now() - timedelta(days=months_back * 30)).strftime("%d-%b-%Y")
        status, data = mail.search(None, f'SINCE {since_date}')
        if status != "OK":
            return []

        all_ids = data[0].split()
        _safe_print(f"[BulkGmail] 共找到 {len(all_ids)} 封邮件（近{months_back}个月）")

        batch_size = 100
        for i in range(0, len(all_ids), batch_size):
            batch = all_ids[i:i + batch_size]
            id_set = b",".join(batch)
            status, header_data = mail.fetch(id_set, "(RFC822.HEADER)")
            if status != "OK":
                continue
            for item in header_data:
                if not isinstance(item, tuple):
                    continue
                try:
                    msg = email.message_from_bytes(item[1])
                    subject  = _decode_header_value(msg.get("Subject", ""))
                    from_raw = _decode_header_value(msg.get("From", ""))
                    _, from_addr = parseaddr(from_raw)
                    date_str = msg.get("Date", "")
                    msg_id   = msg.get("Message-ID", "")
                    uid = hashlib.md5(
                        (msg_id or f"{from_addr}{subject}{date_str}").encode()
                    ).hexdigest()

                    # 屏蔽自发邮件
                    if from_addr in (config.NETEASE_EMAIL, config.GMAIL_EMAIL):
                        continue

                    results.append({
                        "id": uid,
                        "from_addr": from_addr,
                        "subject": subject,
                        "date": date_str,
                        "source": "gmail",
                    })
                except Exception:
                    continue

            _safe_print(f"[BulkGmail] 元数据进度: {min(i + batch_size, len(all_ids))}/{len(all_ids)}")
            time.sleep(0.3)

        mail.logout()
    except Exception as e:
        _safe_print(f"[BulkGmail] 拉取失败: {e}")

    return results


def _fetch_gmail_bodies(email_metas: list[dict]):
    """拉取 Gmail 重要邮件正文并做 AI 精读"""
    target_ids_set = {m["id"] for m in email_metas}
    BATCH_SIZE = 5

    mail = _connect_gmail_imap()
    if not mail:
        return
    mail.select("INBOX")
    status, data = mail.search(None, "ALL")
    all_imap_ids = data[0].split()[-500:]
    mail.logout()

    pending = []
    for eid in reversed(all_imap_ids):
        mail2 = _connect_gmail_imap()
        if not mail2:
            break
        mail2.select("INBOX")
        try:
            status, msg_data = mail2.fetch(eid, "(RFC822)")
            if status != "OK":
                continue
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)

            subject  = _decode_header_value(msg.get("Subject", ""))
            from_raw = _decode_header_value(msg.get("From", ""))
            _, from_addr = parseaddr(from_raw)
            date_str = msg.get("Date", "")
            msg_id   = msg.get("Message-ID", "")
            uid = hashlib.md5(
                (msg_id or f"{from_addr}{subject}{date_str}").encode()
            ).hexdigest()

            if from_addr in (config.NETEASE_EMAIL, config.GMAIL_EMAIL):
                continue
            if db.email_exists(uid) or uid not in target_ids_set:
                continue

            body = _extract_body(msg)
            pending.append({"id": uid, "from_addr": from_addr,
                            "subject": subject, "date": date_str, "body": body,
                            "source": "gmail"})
        finally:
            mail2.logout()

        if len(pending) >= BATCH_SIZE:
            process_new_emails(pending)
            _safe_print(f"[BulkGmail] 已精读 {len(pending)} 封，继续...")
            pending = []
            time.sleep(2)

    if pending:
        process_new_emails(pending)
    _safe_print("[BulkGmail] 精读完成")


def _mark_gmail_as_read(email_metas: list[dict]):
    """把广告/事务类 Gmail 邮件标记为已读"""
    mail = _connect_gmail_imap()
    if not mail:
        return
    try:
        mail.select("INBOX")
        status, data = mail.search(None, "UNSEEN")
        if status != "OK":
            return
        target_subjects = {em["subject"] for em in email_metas}
        marked = 0
        for eid in data[0].split():
            status, hdr = mail.fetch(eid, "(RFC822.HEADER)")
            if status != "OK":
                continue
            msg = email.message_from_bytes(hdr[0][1])
            subject = _decode_header_value(msg.get("Subject", ""))
            if subject in target_subjects:
                mail.store(eid, "+FLAGS", "\\Seen")
                marked += 1
        _safe_print(f"[BulkGmail] 已标记 {marked} 封广告/通知类邮件为已读")
        mail.logout()
    except Exception as e:
        _safe_print(f"[BulkGmail] 标记已读失败: {e}")


def process_bulk_gmail_emails(months_back: int = 12):
    """
    批量处理 Gmail 历史邮件主入口。
    1. 拉元数据
    2. 规则分类
    3. 重要的拉正文 + AI精读
    """
    _safe_print(f"[BulkGmail] 开始批量处理近 {months_back} 个月 Gmail 邮件...")

    all_meta = fetch_gmail_metadata(months_back)
    if not all_meta:
        _safe_print("[BulkGmail] 没有获取到邮件元数据")
        return

    stats = {"total": len(all_meta), "skip": 0, "need_ai": 0, "already_done": 0}
    to_read_full = []

    for em in all_meta:
        if db.email_exists(em["id"]):
            stats["already_done"] += 1
            continue

        result = classify_email(em["from_addr"], em["subject"])
        importance = result["importance"]
        category   = result["category"]

        # Aegis 自发邮件（日报/提醒/回复）完全跳过，不写库不通知
        if category == "self":
            em["_mark_read"] = True
            stats["skip"] += 1
            continue

        update_sender_profile(em["from_addr"], category, importance)
        upsert_contact(em["from_addr"], em["from_addr"].split("@")[0], em["subject"])

        if not result["need_ai"]:
            db.save_email(
                email_id=em["id"],
                from_addr=em["from_addr"],
                subject=em["subject"],
                date=em["date"],
                body="",
                summary=f"[{category}] {em['subject']}",
                importance=importance,
                category=category,
                needs_reply=False,
                draft_reply=None,
            )
            em["_mark_read"] = True
            stats["skip"] += 1
        else:
            to_read_full.append(em)
            stats["need_ai"] += 1

    _safe_print(f"[BulkGmail] 分类完成: 总{stats['total']} | 规则归档{stats['skip']} | "
               f"需精读{stats['need_ai']} | 已处理{stats['already_done']}")

    if to_read_full:
        _fetch_gmail_bodies(to_read_full)

    to_mark_read = [em for em in all_meta if em.get("_mark_read")]
    if to_mark_read:
        _mark_gmail_as_read(to_mark_read)

    _safe_print("[BulkGmail] 批量处理完成！")


def _mark_emails_as_read(email_metas: list[dict]):
    """
    通过 IMAP STORE 命令把指定邮件标记为已读（\\Seen）。
    只标记广告/事务类，重要邮件保持未读。
    """
    mail = _connect()
    if not mail:
        return
    try:
        mail.select("INBOX")
        status, data = mail.search(None, "UNSEEN")
        if status != "OK":
            return

        unread_ids = set(data[0].split())
        target_subjects = {em["subject"] for em in email_metas}
        marked = 0

        # 逐封匹配（通过头部确认是同一封）
        for eid in unread_ids:
            status, hdr = mail.fetch(eid, "(RFC822.HEADER)")
            if status != "OK":
                continue
            msg = email.message_from_bytes(hdr[0][1])
            subject = _decode_header_value(msg.get("Subject", ""))
            if subject in target_subjects:
                mail.store(eid, "+FLAGS", "\\Seen")
                marked += 1

        _safe_print(f"[Bulk] 已标记 {marked} 封广告/通知类邮件为已读")
        mail.logout()
    except Exception as e:
        _safe_print(f"[Bulk] 标记已读失败: {e}")


def _fetch_and_process_bodies(email_metas: list[dict]):
    """
    拉取需要精读的邮件正文，调用AI分析。
    每批 AI 处理前重新连接 IMAP，避免长时间处理导致连接超时。
    """
    target_ids_set = {m["id"] for m in email_metas}
    BATCH_SIZE = 5  # 每批拉5封，AI处理完再拉下一批

    # 先一次性拿到所有需要的邮件 IMAP 序列号
    mail = _connect()
    if not mail:
        return
    mail.select("INBOX")
    status, data = mail.search(None, "ALL")
    all_imap_ids = data[0].split()[-200:]  # 最多处理最近200封
    mail.logout()

    # 按批次处理：每批重新连接，避免 IMAP idle timeout
    pending = []
    for eid in reversed(all_imap_ids):
        mail2 = _connect()
        if not mail2:
            break
        mail2.select("INBOX")
        try:
            status, msg_data = mail2.fetch(eid, "(RFC822)")
            if status != "OK":
                continue
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)

            subject  = _decode_header_value(msg.get("Subject", ""))
            from_raw = _decode_header_value(msg.get("From", ""))
            _, from_addr = parseaddr(from_raw)
            date_str = msg.get("Date", "")
            msg_id   = msg.get("Message-ID", "")
            uid = hashlib.md5(
                (msg_id or f"{from_addr}{subject}{date_str}").encode()
            ).hexdigest()

            if db.email_exists(uid) or uid not in target_ids_set:
                continue

            body = _extract_body(msg)
            pending.append({"id": uid, "from_addr": from_addr,
                             "subject": subject, "date": date_str, "body": body})

            # 提取并归档附件（importance >= 4 的邮件在 process_new_emails 后触发通知，
            # 附件在此处先行保存，归类由 attachment_manager 自动完成）
            _save_email_attachments(msg, from_addr, subject)
        finally:
            mail2.logout()

        if len(pending) >= BATCH_SIZE:
            process_new_emails(pending)
            _safe_print(f"[Bulk] 已精读 {len(pending)} 封，继续...")
            pending = []
            time.sleep(2)

    if pending:
        process_new_emails(pending)

    _safe_print(f"[Bulk] 精读完成")


def _save_email_attachments(msg, from_addr: str, subject: str):
    """从 email.Message 对象中提取并归档所有附件"""
    SKIP_TYPES = {"text/plain", "text/html"}
    try:
        from scanner.attachment_manager import save_attachment
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type in SKIP_TYPES:
                continue
            filename = part.get_filename()
            if not filename:
                continue
            from email_module.reader import _decode_header_value
            filename = _decode_header_value(filename)
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            try:
                save_attachment(
                    content=payload,
                    filename=filename,
                    sender=from_addr,
                    subject=subject,
                    source="email",
                )
            except Exception as e:
                _safe_print(f"[Attach] 附件保存失败 {filename}: {e}")
    except ImportError:
        pass
    except Exception as e:
        _safe_print(f"[Attach] 附件提取失败: {e}")
