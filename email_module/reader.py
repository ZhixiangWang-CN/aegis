"""IMAP 收取邮件（网易163）"""
import imaplib
import email
from email.header import decode_header
from email.utils import parseaddr
import hashlib
import config
from memory import db


def _decode_header_value(raw) -> str:
    parts = decode_header(raw or "")
    result = []
    for part, charset in parts:
        if isinstance(part, bytes):
            result.append(part.decode(charset or "utf-8", errors="ignore"))
        else:
            result.append(part)
    return "".join(result)


def _extract_body(msg) -> str:
    """从 email.Message 对象提取纯文本正文"""
    body_parts = []
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in cd:
                charset = part.get_content_charset() or "utf-8"
                payload = part.get_payload(decode=True)
                if payload:
                    body_parts.append(payload.decode(charset, errors="ignore"))
    else:
        charset = msg.get_content_charset() or "utf-8"
        payload = msg.get_payload(decode=True)
        if payload:
            body_parts.append(payload.decode(charset, errors="ignore"))
    return "\n".join(body_parts)[:5000]


def _safe_print(msg: str):
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("ascii", errors="replace").decode("ascii"))


class _163IMAP(imaplib.IMAP4_SSL):
    """
    163 邮箱定制 IMAP 客户端。
    登录后发送 ID 命令通过安全验证，再选择 INBOX。
    """
    def id_(self):
        """发送 IMAP ID 扩展命令（非标准命令用 xatom）"""
        return self.xatom('ID', '("name" "Jarvis" "version" "1.0")')


def _connect() -> _163IMAP | None:
    """建立 IMAP 连接，163 需要在登录后发送 ID 命令通过安全验证。"""
    try:
        mail = _163IMAP(config.NETEASE_IMAP_HOST, config.NETEASE_IMAP_PORT)
        mail.login(config.NETEASE_EMAIL, config.NETEASE_AUTH_CODE)
        # 登录后立即发 ID，解除 163 的"不安全登录"限制
        mail.id_()
        return mail
    except Exception as e:
        print(f"[Email] 登录失败: {e}")
        return None


def fetch_new_emails(limit: int = 30) -> list[dict]:
    """
    连接 IMAP，拉取最近 limit 封邮件中未处理的部分。
    返回新邮件列表（未写库，由 summarizer 负责写库）。
    """
    mail = _connect()
    if not mail:
        return []

    new_emails = []
    try:
        select_status, select_data = mail.select("INBOX")
        if select_status != "OK":
            err = select_data[0].decode() if select_data else "未知"
            print(f"[Email] INBOX 选择失败: {err}")
            if "Unsafe Login" in err:
                print("[Email] 163 安全拦截，请执行以下步骤:")
                print("  1. 登录 https://mail.163.com")
                print("  2. 设置 → POP3/SMTP/IMAP → 开启 IMAP")
                print("  3. 生成新的授权码，更新 .credentials 文件")
            mail.logout()
            return []

        # 获取所有邮件 ID，取最新 limit 封
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
            # 优先用 Message-ID 去重，fallback 到内容 hash
            message_id = msg.get("Message-ID", "")
            uid = hashlib.md5(
                (message_id or f"{from_addr}{subject}{date_str}").encode()
            ).hexdigest()

            if db.email_exists(uid):
                continue

            # 屏蔽Aegis自己发出的日报/通知，避免循环处理
            if from_addr == config.NETEASE_EMAIL:
                continue

            body = _extract_body(msg)
            new_emails.append({
                "id": uid,
                "from_addr": from_addr,
                "subject": subject,
                "date": date_str,
                "body": body,
            })

        mail.logout()
        _safe_print(f"[Email] 拉取完成，发现 {len(new_emails)} 封新邮件")

    except Exception as e:
        print(f"[Email] 读取失败: {e}")

    return new_emails
