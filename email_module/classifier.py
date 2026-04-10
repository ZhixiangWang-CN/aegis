"""
邮件智能分类器
三层处理: 元数据规则 → 发件人画像 → AI精读
"""
import re
import json
import sqlite3
from datetime import datetime
import config

# ── 重点发件人特征（精读）────────────────────────────────────────────
HIGH_PRIORITY_DOMAIN_KEYWORDS = [
    # 学术机构
    ".edu", ".edu.cn", ".ac.cn", ".ac.uk", ".ac.jp", ".ac.kr",
    "university", "univ.", "college", "institute", "hospital",
    "hosp.", "medical", "clinic",
    # 顶级期刊/出版商
    "elsevier", "springer", "wiley", "nature.com", "science",
    "thelancet", "nejm.org", "bmj.com", "jama", "cell.com",
    "tandfonline", "sagepub", "oxford", "cambridge",
    "ieee.org", "acm.org", "plos", "frontiersin",
    # 权威机构
    "who.int", "nih.gov", "cdc.gov", "nsfc.gov.cn",
]

HIGH_PRIORITY_SUBJECT_KEYWORDS = [
    "review", "revision", "manuscript", "submission", "accept",
    "reject", "decision", "peer review",
    "论文", "审稿", "录用", "拒稿", "修改", "投稿",
    "基金", "课题", "项目申请", "中标",
    "会议", "conference", "workshop", "symposium",
]

# ── 低优先/自动归档特征 ────────────────────────────────────────────
JUNK_SENDER_KEYWORDS = [
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "newsletter", "marketing", "promotion", "promo",
    "notification", "alert", "automated", "mailer-daemon",
    "广告", "推广",
]

JUNK_SUBJECT_KEYWORDS = [
    "unsubscribe", "opt-out", "click here",
    "优惠", "折扣", "限时", "特惠", "秒杀", "满减",
    "验证码", "verification code", "OTP",   # 事务类，自动归档
    "账单", "invoice", "receipt",           # 账单类
    "系统通知", "登录提醒", "安全提醒",
]

# ── 重要度定义 ─────────────────────────────────────────────────────
IMPORTANCE = {
    "academic_high": 5,   # 期刊投稿相关
    "academic_normal": 4, # 学术机构来信
    "personal": 4,        # 个人/联系人邮件
    "unknown": 3,         # 待AI判断
    "notification": 2,    # 系统通知
    "marketing": 1,       # 营销/广告
}


# Aegis 系统自发邮件的主题前缀，收到后直接跳过（避免自我触发通知）
_AEGIS_SUBJECT_PREFIXES = (
    "📋 aegis日报",
    "📊 aegis",
    "✅ aegis",
    "⚠️ aegis",
    "⚠️ 重要邮件提醒",
    "[aegis]",
    "📄 aegis文档",
    "📎 aegis发送文件",
    "aegis系统测试",
)


def _is_aegis_self_email(sender: str, subject: str) -> bool:
    """判断是否是 Aegis 系统自己发出的邮件（日报、提醒、回复等）"""
    subject_lower = subject.lower().strip()
    # ① 主要检查：发件人是自己的邮箱（覆盖所有 Aegis 发出的邮件）
    if sender in (config.NETEASE_EMAIL, config.GMAIL_EMAIL or ""):
        return True
    # ② 备用检查：主题前缀匹配（防转发场景）
    if any(subject_lower.startswith(p) for p in _AEGIS_SUBJECT_PREFIXES):
        return True
    # ③ 兜底：主题含 "aegis" 且含已知系统词
    _system_words = ("日报", "简报", "状态报告", "通知质量", "系统测试", "文档:", "发送文件")
    if "aegis" in subject_lower and any(w in subject for w in _system_words):
        return True
    return False


def classify_by_metadata(sender: str, subject: str) -> dict:
    """
    第一层: 纯规则分类，不调AI，毫秒级。
    返回: {category, importance, need_ai, reason}
    """
    # Aegis 自发邮件直接跳过，importance=0 不触发任何后续处理
    if _is_aegis_self_email(sender, subject):
        return {"category": "self", "importance": 0,
                "need_ai": False, "reason": "aegis_self_sent"}

    sender_lower = sender.lower()
    subject_lower = subject.lower()

    # 1. 垃圾/营销 — 直接归档
    if any(k in sender_lower for k in JUNK_SENDER_KEYWORDS):
        return {"category": "marketing", "importance": 1,
                "need_ai": False, "reason": "junk_sender"}

    if any(k in subject_lower for k in JUNK_SUBJECT_KEYWORDS):
        # 验证码等事务类单独标记
        cat = "transactional" if any(k in subject_lower for k in ["验证码", "verification code", "otp"]) else "marketing"
        return {"category": cat, "importance": 1,
                "need_ai": False, "reason": "junk_subject"}

    # 2. 学术高优先 — 期刊投稿相关
    is_academic_domain = any(k in sender_lower for k in HIGH_PRIORITY_DOMAIN_KEYWORDS)
    is_academic_subject = any(k in subject_lower for k in HIGH_PRIORITY_SUBJECT_KEYWORDS)

    if is_academic_domain and is_academic_subject:
        return {"category": "academic_high", "importance": 5,
                "need_ai": True, "reason": "academic_domain+subject"}

    if is_academic_domain:
        return {"category": "academic_normal", "importance": 4,
                "need_ai": True, "reason": "academic_domain"}

    if is_academic_subject:
        return {"category": "academic_high", "importance": 5,
                "need_ai": True, "reason": "academic_subject"}

    # 3. 未知 — 交给AI
    return {"category": "unknown", "importance": 3,
            "need_ai": True, "reason": "unknown"}


# ── 发件人画像（持久化到 SQLite）──────────────────────────────────

def _get_conn():
    conn = sqlite3.connect(str(config.DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sender_profiles (
            sender TEXT PRIMARY KEY,
            category TEXT,
            avg_importance REAL,
            email_count INTEGER DEFAULT 1,
            last_seen TEXT
        )
    """)
    return conn


def get_sender_profile(sender: str) -> dict | None:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM sender_profiles WHERE sender=?", (sender,)
        ).fetchone()
    if row:
        return {"sender": row[0], "category": row[1],
                "avg_importance": row[2], "email_count": row[3]}
    return None


def update_sender_profile(sender: str, category: str, importance: int):
    with _get_conn() as conn:
        existing = conn.execute(
            "SELECT avg_importance, email_count FROM sender_profiles WHERE sender=?",
            (sender,)
        ).fetchone()
        now = datetime.now().isoformat()
        if existing:
            avg = (existing[0] * existing[1] + importance) / (existing[1] + 1)
            conn.execute("""
                UPDATE sender_profiles
                SET category=?, avg_importance=?, email_count=email_count+1, last_seen=?
                WHERE sender=?
            """, (category, round(avg, 2), now, sender))
        else:
            conn.execute("""
                INSERT INTO sender_profiles (sender, category, avg_importance, last_seen)
                VALUES (?,?,?,?)
            """, (sender, category, float(importance), now))


def classify_email(sender: str, subject: str) -> dict:
    """
    完整分类入口：先查发件人画像 → 再规则 → 最后标记需要AI精读。
    返回分类结果，need_ai=True 的交给 summarizer 读正文。
    """
    # 优先用已有的发件人画像（见过这个发件人）
    profile = get_sender_profile(sender)
    if profile and profile["email_count"] >= 3:
        # 已见过3次以上，直接用历史画像
        imp = round(profile["avg_importance"])
        return {
            "category": profile["category"],
            "importance": imp,
            "need_ai": imp >= 3,   # 重要的还是精读
            "reason": f"sender_profile(n={profile['email_count']})"
        }

    # 第一次见或见过次数少，走规则
    return classify_by_metadata(sender, subject)
