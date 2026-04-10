"""
Windows 桌面通知模块

触发时机：
- 重要邮件到达（评分 ≥ 4）
- 微信 Aegis: 指令执行完毕
- 新的紧急焦点事项写入 focus.md
- 系统错误 / 崩溃告警

点击通知可打开 Web UI。
"""
from __future__ import annotations

import threading
from typing import Literal

_LOCK = threading.Lock()
_APP_ID = "Aegis"
_WEB_URL = "http://127.0.0.1:8077"


def notify(
    title: str,
    message: str,
    level: Literal["info", "warning", "email", "wechat", "focus"] = "info",
    url: str = _WEB_URL,
) -> None:
    """
    发送 Windows 桌面 Toast 通知，非阻塞（在子线程中执行）。

    level 控制图标和声音：
      info    — 静默
      warning — 系统警告音
      email   — 邮件提示音
      wechat  — 消息提示音
      focus   — 强提示音
    """
    def _send():
        try:
            from winotify import Notification, audio as winaudio

            icons = {
                "info":    None,
                "warning": None,
                "email":   None,
                "wechat":  None,
                "focus":   None,
            }

            sounds = {
                "info":    None,
                "warning": winaudio.Default,
                "email":   winaudio.Mail,
                "wechat":  winaudio.IM,
                "focus":   winaudio.Reminder,
            }

            toast = Notification(
                app_id=_APP_ID,
                title=title,
                msg=message[:200],
                duration="short",
                launch=url,
            )

            snd = sounds.get(level)
            if snd:
                toast.set_audio(snd, loop=False)

            with _LOCK:
                toast.show()

        except ImportError:
            # winotify 未安装，静默降级
            pass
        except Exception as e:
            print(f"[Notify] 通知发送失败: {e}")

    t = threading.Thread(target=_send, daemon=True)
    t.start()


# ── 快捷函数（带通知日志）────────────────────────────────────────────────────

def notify_email(subject: str, sender: str, score: int):
    """重要邮件到达通知"""
    import hashlib
    notif_id = hashlib.md5(f"email_{sender}_{subject}".encode()).hexdigest()[:12]
    notify(
        title=f"📧 重要邮件（{score}分）",
        message=f"{sender}\n{subject}",
        level="email",
        url=f"{_WEB_URL}/#email",
    )
    try:
        from memory.importance_learner import log_notification
        log_notification(
            notif_id=notif_id, notif_type="email",
            contact=sender, contact_name=sender.split("@")[0],
            content_preview=subject, score=score / 5.0,
            features={"email_score": score},
        )
    except Exception:
        pass


def notify_wechat_command(contact: str, instruction: str, result_preview: str):
    """微信指令执行完毕通知"""
    import hashlib
    notif_id = hashlib.md5(f"wxcmd_{contact}_{instruction}".encode()).hexdigest()[:12]
    notify(
        title=f"💬 微信指令已执行（{contact}）",
        message=f"{instruction[:40]}\n→ {result_preview[:80]}",
        level="wechat",
        url=_WEB_URL,
    )
    try:
        from memory.importance_learner import log_notification
        log_notification(
            notif_id=notif_id, notif_type="wechat_command",
            contact=contact, contact_name=contact,
            content_preview=instruction, score=0.9,
            features={"is_command": True},
        )
    except Exception:
        pass


def notify_wechat_important(wxid: str, contact_name: str, content: str, score: float, features: dict):
    """微信扫描发现重要消息"""
    import hashlib
    notif_id = hashlib.md5(f"wx_{wxid}_{content[:30]}".encode()).hexdigest()[:12]
    notify(
        title=f"💬 重要消息 — {contact_name}",
        message=content[:120],
        level="wechat",
        url=_WEB_URL,
    )
    try:
        from memory.importance_learner import log_notification
        log_notification(
            notif_id=notif_id, notif_type="wechat_scan",
            contact=wxid, contact_name=contact_name,
            content_preview=content, score=score,
            features=features,
        )
    except Exception:
        pass


def notify_focus(item: str):
    """新紧急焦点事项通知"""
    notify(
        title="🎯 新紧急事项",
        message=item[:120],
        level="focus",
        url=f"{_WEB_URL}/#memory",
    )


def notify_error(component: str, detail: str):
    """系统错误通知"""
    notify(
        title=f"⚠️ Aegis 错误 — {component}",
        message=detail[:150],
        level="warning",
    )
