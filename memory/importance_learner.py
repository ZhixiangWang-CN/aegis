"""
重要性学习引擎

职责：
1. 记录每条推送通知的特征 (data/notification_log.jsonl)
2. 收集隐式行为信号 (data/feedback_log.jsonl)
   - 焦点事项被标记完成 → 正向
   - 焦点事项被直接删除 → 负向
   - 收到通知后 30 分钟内回复了对方 → 正向
   - 24 小时内无任何后续行动 → 弱负向
3. 每日对账 (daily_reconcile)：生成报告 → 发给用户 → 接收回复 → 更新权重
4. 提供评分接口 score_message() 供扫描管线调用

权重存储：data/learned_importance.json
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

import config

# ── 文件路径 ──────────────────────────────────────────────────────────────────
_DATA_DIR          = config.DATA_DIR
_WEIGHTS_FILE      = _DATA_DIR / "learned_importance.json"
_NOTIF_LOG         = _DATA_DIR / "notification_log.jsonl"
_FEEDBACK_LOG      = _DATA_DIR / "feedback_log.jsonl"

_LOCK = threading.Lock()

# ── 默认权重 ──────────────────────────────────────────────────────────────────
_DEFAULT_WEIGHTS: dict = {
    "contacts": {},          # wxid → {"useful_rate": 0.5, "count": 0}
    "groups": {},            # group_wxid → {"useful_rate": 0.5, "count": 0}
    "keywords": {
        "截止": 1.5, "deadline": 1.5, "ddl": 1.5,
        "紧急": 1.6, "urgent": 1.6, "重要": 1.4,
        "帮我": 1.3, "麻烦你": 1.3, "需要你": 1.3,
        "合同": 1.4, "签字": 1.4, "审批": 1.4,
        "明天": 1.2, "今天": 1.2, "几点": 1.2,
        "确认": 1.2, "回复": 1.1,
        "周报": 0.5, "日报": 0.5, "通知": 0.6,
        "广告": 0.1, "推广": 0.1,
    },
    "patterns": {
        "late_night":        1.3,   # 22:00-07:00 发的消息
        "consecutive_3":     1.5,   # 连续发 ≥3 条
        "long_message":      1.2,   # 消息 > 100 字
        "at_mention":        1.6,   # 群里 @你
        "first_msg_of_day":  1.1,   # 今天第一次发消息
    },
    "thresholds": {
        "notify":  0.55,    # 高于此分数推送通知
        "ai_scan": 0.30,    # 高于此分数送 AI 分析
        "ignore":  0.15,    # 低于此分数直接跳过
    },
    "last_updated": "",
}


# ── 权重读写 ──────────────────────────────────────────────────────────────────

def load_weights() -> dict:
    try:
        if _WEIGHTS_FILE.exists():
            w = json.loads(_WEIGHTS_FILE.read_text("utf-8"))
            # 合并缺失的默认键（版本兼容）
            for k, v in _DEFAULT_WEIGHTS.items():
                if k not in w:
                    w[k] = v
                elif isinstance(v, dict):
                    for kk, vv in v.items():
                        w[k].setdefault(kk, vv)
            return w
    except Exception:
        pass
    return json.loads(json.dumps(_DEFAULT_WEIGHTS))


def save_weights(w: dict):
    with _LOCK:
        w["last_updated"] = datetime.now().isoformat()
        _WEIGHTS_FILE.write_text(
            json.dumps(w, ensure_ascii=False, indent=2), encoding="utf-8"
        )


# ── 评分接口 ──────────────────────────────────────────────────────────────────

def score_message(
    wxid: str,
    content: str,
    is_group: bool = False,
    hour: Optional[int] = None,
    consecutive_count: int = 1,
    at_mentioned: bool = False,
) -> float:
    """
    计算消息重要性分数 [0.0, 1.0]。
    分数 >= thresholds.notify  → 推送桌面通知
    分数 >= thresholds.ai_scan → 送 AI 深度分析
    分数 <  thresholds.ignore  → 直接跳过
    """
    w = load_weights()

    # ① 联系人/群组基础分
    if is_group:
        contact_data = w["groups"].get(wxid, {})
    else:
        contact_data = w["contacts"].get(wxid, {})
    base = contact_data.get("useful_rate", 0.5)

    # ② 关键词最大倍率
    kw_boost = 1.0
    content_lower = content.lower()
    for kw, weight in w["keywords"].items():
        if kw in content_lower or kw in content:
            kw_boost = max(kw_boost, weight)

    # ③ 行为模式倍率（叠加）
    p = w["patterns"]
    pattern_mult = 1.0
    if hour is not None and (hour >= 22 or hour < 7):
        pattern_mult *= p.get("late_night", 1.3)
    if consecutive_count >= 3:
        pattern_mult *= p.get("consecutive_3", 1.5)
    if len(content) > 100:
        pattern_mult *= p.get("long_message", 1.2)
    if at_mentioned:
        pattern_mult *= p.get("at_mention", 1.6)

    score = base * kw_boost * pattern_mult
    return min(round(score, 4), 1.0)


def get_thresholds() -> dict:
    return load_weights().get("thresholds", _DEFAULT_WEIGHTS["thresholds"])


# ── 通知日志 ──────────────────────────────────────────────────────────────────

def log_notification(
    notif_id: str,
    notif_type: str,          # "email" | "wechat_command" | "wechat_scan" | "focus"
    contact: str,             # wxid 或邮箱
    contact_name: str,
    content_preview: str,
    score: float,
    features: dict,           # keyword_hits, consecutive, is_group, hour ...
):
    """记录一条已推送的通知，供对账和信号匹配使用"""
    entry = {
        "id": notif_id,
        "ts": datetime.now().isoformat(),
        "date": date.today().isoformat(),
        "type": notif_type,
        "contact": contact,
        "contact_name": contact_name,
        "preview": content_preview[:120],
        "score": score,
        "features": features,
        "signals": [],          # 后续收集的信号填这里（方便对账）
    }
    with _LOCK:
        with open(_NOTIF_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ── 隐式信号收集 ──────────────────────────────────────────────────────────────

def record_signal(
    signal: str,              # "focus_completed" | "focus_deleted" | "replied_soon" | "no_action_24h"
    contact: str = "",
    content_hint: str = "",   # 用于模糊匹配通知日志
    explicit_score: Optional[int] = None,  # 用户显式评分：1=有用，-1=噪音
):
    """
    记录一条反馈信号，并尝试关联到最近的通知日志。
    """
    entry = {
        "ts": datetime.now().isoformat(),
        "date": date.today().isoformat(),
        "signal": signal,
        "contact": contact,
        "hint": content_hint[:80],
        "explicit_score": explicit_score,
    }
    with _LOCK:
        with open(_FEEDBACK_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ── 今日通知读取 ──────────────────────────────────────────────────────────────

def _read_today_notifications() -> list[dict]:
    today = date.today().isoformat()
    result = []
    try:
        if not _NOTIF_LOG.exists():
            return []
        for line in _NOTIF_LOG.read_text("utf-8").splitlines():
            try:
                e = json.loads(line)
                if e.get("date") == today:
                    result.append(e)
            except Exception:
                pass
    except Exception:
        pass
    return result


def _read_today_signals() -> list[dict]:
    today = date.today().isoformat()
    result = []
    try:
        if not _FEEDBACK_LOG.exists():
            return []
        for line in _FEEDBACK_LOG.read_text("utf-8").splitlines():
            try:
                e = json.loads(line)
                if e.get("date") == today:
                    result.append(e)
            except Exception:
                pass
    except Exception:
        pass
    return result


# ── 每日对账 ──────────────────────────────────────────────────────────────────

def daily_reconcile() -> str:
    """
    每日对账主函数（由 scheduler 在 21:00 调用）：
    1. 汇总今日通知 + 隐式信号
    2. 推断哪些通知"有用"、哪些"可能是噪音"
    3. 应用隐式信号自动更新权重（不需要用户干预）
    4. 生成报告文字返回（供发邮件/微信推送）
    """
    notifications = _read_today_notifications()
    signals       = _read_today_signals()

    if not notifications:
        return ""  # 今天没有推送通知，不发报告

    # ── 统计信号 ──────────────────────────────────────────────────────────────
    completed_hints  = {s["hint"] for s in signals if s["signal"] == "focus_completed"}
    deleted_hints    = {s["hint"] for s in signals if s["signal"] == "focus_deleted"}
    replied_contacts = {s["contact"] for s in signals if s["signal"] == "replied_soon"}
    explicit_useful  = {s["hint"] for s in signals if s.get("explicit_score") == 1}
    explicit_noise   = {s["hint"] for s in signals if s.get("explicit_score") == -1}

    useful   = []
    noise    = []
    unclear  = []

    for n in notifications:
        preview = n["preview"]
        contact = n["contact"]
        is_useful = (
            any(h in preview for h in completed_hints) or
            contact in replied_contacts or
            any(h in preview for h in explicit_useful)
        )
        is_noise = (
            any(h in preview for h in deleted_hints) or
            any(h in preview for h in explicit_noise)
        )
        if is_useful:
            useful.append(n)
        elif is_noise:
            noise.append(n)
        else:
            unclear.append(n)

    # ── 自动更新权重（隐式信号部分）──────────────────────────────────────────
    _auto_update_weights(useful, noise)

    # ── 生成报告 ──────────────────────────────────────────────────────────────
    total = len(notifications)
    lines = [
        f"📊 **今日通知质量报告** — {date.today().strftime('%m月%d日')}",
        f"共推送 {total} 条通知：✅ 有用 {len(useful)} | ❓ 待确认 {len(unclear)} | 🔇 噪音 {len(noise)}",
        "",
    ]

    if unclear:
        lines.append("**以下通知没有检测到后续行动，请告诉我它们是否有用：**")
        for i, n in enumerate(unclear[:8], 1):
            t = datetime.fromisoformat(n["ts"]).strftime("%H:%M")
            lines.append(f"  {i}. [{t}] {n['contact_name']}: {n['preview'][:60]}")
        lines.append("")
        lines.append("回复示例：")
        lines.append("  `Aegis: 对账 有用 1,3  噪音 2,4,5`")
        lines.append("  `Aegis: 对账 都有用`")
        lines.append("  `Aegis: 对账 都是噪音`")

    if noise:
        lines.append(f"\n已自动降低 {len(noise)} 个来源的权重（隐式信号判定为噪音）。")

    if not unclear and not noise:
        lines.append("今天的通知质量很好，权重无需调整。")

    # 把 unclear 列表存到临时文件，供用户回复时对号入座
    _save_pending_reconcile(unclear)

    return "\n".join(lines)


def _save_pending_reconcile(unclear: list[dict]):
    """暂存待确认通知列表，供用户回复对账指令时查找"""
    path = _DATA_DIR / "reconcile_pending.json"
    path.write_text(
        json.dumps({"date": date.today().isoformat(), "items": unclear},
                   ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def handle_reconcile_reply(instruction: str) -> str:
    """
    处理用户回复的对账指令，例如：
      "对账 有用 1,3  噪音 2,4,5"
      "对账 都有用"
      "对账 都是噪音"
    """
    path = _DATA_DIR / "reconcile_pending.json"
    if not path.exists():
        return "没有待确认的对账记录。"

    try:
        data = json.loads(path.read_text("utf-8"))
        if data.get("date") != date.today().isoformat():
            return "对账记录已过期，请等待今晚新的报告。"
        items = data["items"]
    except Exception:
        return "读取对账记录失败。"

    instr = instruction.lower().strip()
    useful_ids: set[int] = set()
    noise_ids:  set[int] = set()

    if "都有用" in instr:
        useful_ids = set(range(1, len(items) + 1))
    elif "都是噪音" in instr or "都噪音" in instr:
        noise_ids = set(range(1, len(items) + 1))
    else:
        import re
        useful_match = re.search(r"有用\s*([\d,，\s]+)", instr)
        noise_match  = re.search(r"噪音\s*([\d,，\s]+)", instr)
        if useful_match:
            useful_ids = {int(x) for x in re.findall(r"\d+", useful_match.group(1))}
        if noise_match:
            noise_ids  = {int(x) for x in re.findall(r"\d+", noise_match.group(1))}

    useful_items = [items[i-1] for i in useful_ids if 1 <= i <= len(items)]
    noise_items  = [items[i-1] for i in noise_ids  if 1 <= i <= len(items)]

    _auto_update_weights(useful_items, noise_items)

    path.unlink(missing_ok=True)

    return (
        f"✅ 已更新权重：{len(useful_items)} 条标记为有用，"
        f"{len(noise_items)} 条标记为噪音。\n"
        "系统会逐渐减少类似噪音通知。"
    )


# ── 权重自动更新 ──────────────────────────────────────────────────────────────

def _auto_update_weights(useful: list[dict], noise: list[dict]):
    """
    根据有用/噪音列表更新联系人和群组的 useful_rate。
    使用贝叶斯滚动均值，样本量越大越保守。
    """
    if not useful and not noise:
        return

    w = load_weights()

    def _update(section: str, wxid: str, is_useful: bool):
        entry = w[section].setdefault(wxid, {"useful_rate": 0.5, "count": 0})
        n = entry["count"]
        r = entry["useful_rate"]
        # 贝叶斯更新，前 10 条快速学习，之后逐渐保守
        lr = max(0.05, 1.0 / (n + 1))
        entry["useful_rate"] = round(r + lr * ((1.0 if is_useful else 0.0) - r), 4)
        entry["count"] = n + 1

    for n in useful:
        section = "groups" if n.get("features", {}).get("is_group") else "contacts"
        _update(section, n["contact"], True)

    for n in noise:
        section = "groups" if n.get("features", {}).get("is_group") else "contacts"
        _update(section, n["contact"], False)

    save_weights(w)
