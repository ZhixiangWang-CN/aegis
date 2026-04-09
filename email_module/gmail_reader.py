"""Gmail IMAP/SMTP 支持（应用专用密码方式）"""
import imaplib
import smtplib
import email
import base64
import hashlib
from email.utils import parseaddr
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import config
from email_module.reader import _decode_header_value, _extract_body, _safe_print
from memory import db


def _connect_gmail() -> imaplib.IMAP4_SSL | None:
    try:
        mail = imaplib.IMAP4_SSL(config.GMAIL_IMAP_HOST, config.GMAIL_IMAP_PORT)
        mail.login(config.GMAIL_EMAIL, config.GMAIL_APP_PWD)
        return mail
    except Exception as e:
        _safe_print(f"[Gmail] 登录失败: {e}")
        return None


def fetch_new_gmail(limit: int = 30) -> list[dict]:
    """拉取 Gmail 新邮件"""
    mail = _connect_gmail()
    if not mail:
        return []

    new_emails = []
    try:
        mail.select("INBOX")
        status, data = mail.search(None, "ALL")
        if status != "OK":
            return []

        all_ids = data[0].split()
        fetch_ids = all_ids[-limit:]

        for eid in reversed(fetch_ids):
            status, msg_data = mail.fetch(eid, "(RFC822)")
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

            if db.email_exists(uid):
                continue

            # 屏蔽自发邮件（Aegis日报循环问题）
            if from_addr in (config.NETEASE_EMAIL, config.GMAIL_EMAIL):
                continue

            new_emails.append({
                "id": uid,
                "from_addr": from_addr,
                "subject": subject,
                "date": date_str,
                "body": _extract_body(msg),
                "source": "gmail",
            })

        mail.logout()
        _safe_print(f"[Gmail] 拉取完成，发现 {len(new_emails)} 封新邮件")

    except Exception as e:
        _safe_print(f"[Gmail] 读取失败: {e}")

    return new_emails


def mark_as_read_gmail(subjects: set[str]):
    """把指定主题的邮件标记为已读"""
    mail = _connect_gmail()
    if not mail:
        return
    try:
        mail.select("INBOX")
        status, data = mail.search(None, "UNSEEN")
        if status != "OK":
            return
        marked = 0
        for eid in data[0].split():
            status, hdr = mail.fetch(eid, "(RFC822.HEADER)")
            if status != "OK":
                continue
            msg = email.message_from_bytes(hdr[0][1])
            subject = _decode_header_value(msg.get("Subject", ""))
            if subject in subjects:
                mail.store(eid, "+FLAGS", "\\Seen")
                marked += 1
        _safe_print(f"[Gmail] 已标记 {marked} 封邮件为已读")
        mail.logout()
    except Exception as e:
        _safe_print(f"[Gmail] 标记已读失败: {e}")


def _b64_header(text: str) -> str:
    return f"=?utf-8?b?{base64.b64encode(text.encode('utf-8')).decode('ascii')}?="


def send_gmail(to: str, subject: str, body: str) -> bool:
    """通过 Gmail SMTP 发送邮件"""
    try:
        msg = MIMEMultipart("alternative")
        msg["From"]    = f"{_b64_header('Aegis')} <{config.GMAIL_EMAIL}>"
        msg["To"]      = to
        msg["Subject"] = _b64_header(subject)
        msg.attach(MIMEText(body, "plain", "utf-8"))

        with smtplib.SMTP_SSL(config.GMAIL_SMTP_HOST, config.GMAIL_SMTP_PORT) as s:
            s.login(config.GMAIL_EMAIL, config.GMAIL_APP_PWD)
            s.sendmail(config.GMAIL_EMAIL, to, msg.as_bytes())

        _safe_print(f"[Gmail] 发送成功 → {to} | {subject}")
        return True
    except Exception as e:
        _safe_print(f"[Gmail] 发送失败: {e}")
        return False
