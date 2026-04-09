"""SMTP 发送邮件（网易163）"""
import smtplib
import base64
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from pathlib import Path
import config


def _safe_print(msg: str):
    """Windows GBK 终端安全输出，过滤无法编码的字符"""
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("ascii", errors="replace").decode("ascii"))


def _b64_header(text: str) -> str:
    """RFC 2047 base64 编码，兼容 emoji 和中文，避免 Windows GBK 问题"""
    return f"=?utf-8?b?{base64.b64encode(text.encode('utf-8')).decode('ascii')}?="


def send_email(
    to: str,
    subject: str,
    body: str,
    html: bool = False,
    attachments: list = None,
) -> bool:
    """
    发送邮件。
    to: 收件人地址
    subject: 主题
    body: 正文（纯文本或HTML）
    html: 是否为HTML格式
    attachments: 附件路径列表（str 或 Path），可选
    """
    try:
        msg = MIMEMultipart("mixed")
        msg["From"]    = f"{_b64_header('Aegis')} <{config.NETEASE_EMAIL}>"
        msg["To"]      = to
        msg["Subject"] = _b64_header(subject)

        # 正文
        mime_type = "html" if html else "plain"
        part = MIMEText(body, mime_type, "utf-8")
        msg.attach(part)

        # 附件
        for att_path in (attachments or []):
            p = Path(att_path)
            if not p.exists():
                _safe_print(f"[Email] 附件不存在，跳过: {p}")
                continue
            with open(p, "rb") as f:
                att = MIMEBase("application", "octet-stream")
                att.set_payload(f.read())
            encoders.encode_base64(att)
            att.add_header(
                "Content-Disposition",
                "attachment",
                filename=_b64_header(p.name),
            )
            msg.attach(att)
            _safe_print(f"[Email] 附件已加载: {p.name} ({p.stat().st_size // 1024}KB)")

        with smtplib.SMTP_SSL(config.NETEASE_SMTP_HOST, config.NETEASE_SMTP_PORT) as server:
            server.login(config.NETEASE_EMAIL, config.NETEASE_AUTH_CODE)
            server.sendmail(config.NETEASE_EMAIL, to, msg.as_bytes())

        _safe_print(f"[Email] 发送成功 → {to} | {subject}")
        return True

    except Exception as e:
        _safe_print(f"[Email] 发送失败: {e}")
        return False


def send_daily_briefing(content: str, date: str) -> bool:
    subject = f"📋 Aegis日报 — {date}"
    return send_email(
        to=config.NETEASE_EMAIL,
        subject=subject,
        body=content,
    )
