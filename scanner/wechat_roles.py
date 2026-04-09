"""
微信联系人角色推断与群组分类

角色体系（按任务流向 + 亲密度）:
  superior    — 上级/导师，他们给你任务
  collaborator— 合作者，双向任务
  junior      — 下级/学生，你给他们任务
  colleague   — 同级同事/同学，偶尔双向
  close_personal — 亲密个人（恋人/家人/挚友）
  friend      — 普通朋友
  service     — 商家/服务商，跳过

群组类型:
  core    — 课题组/项目群，全触发条件扫描
  normal  — 同学/普通群，仅高频词触发
  noise   — 广告/低价值群，跳过

使用流程:
  1. 首次运行 infer_all() — AI 推断 Top N 联系人和群的角色/类型
  2. 生成 pending 条目 → 发邮件给用户确认
  3. 用户确认后 apply_role(wxid, role) 固化
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Optional

import config
from memory import db as main_db
from ai import client as ai_client
from scanner.wechat_decrypt import _ensure_wechat_tables
from email_module.reader import _safe_print

# ── 角色/类型定义 ─────────────────────────────────────────────────────────

ROLES = {
    "superior":       "上级/导师（他们给你任务）",
    "collaborator":   "合作者（双向任务）",
    "junior":         "下级/学生（你给他们任务）",
    "colleague":      "同事/同学（偶尔协作）",
    "close_personal": "亲密个人（恋人/家人/挚友）",
    "friend":         "普通朋友",
    "service":        "商家/服务商",
    "unknown":        "未分类",
}

GROUP_TYPES = {
    "core":   "核心群（课题组/项目群）",
    "normal": "普通群（同学/一般工作群）",
    "noise":  "低价值群（广告/闲聊）",
}

# 角色 → 分析深度
ROLE_ANALYSIS_DEPTH = {
    "superior":       "full",       # AI 全文分析
    "collaborator":   "full",
    "junior":         "full",       # 重点看你自己说的话
    "colleague":      "keyword",    # 关键词扫描
    "close_personal": "keyword",
    "friend":         "skip",       # 基本跳过
    "service":        "skip",
    "unknown":        "keyword",
}

# 触发关键词（用于 keyword 级别扫描）
TRIGGER_KEYWORDS = [
    "截止", "ddl", "deadline", "下周", "今天", "明天", "记得",
    "帮我", "需要", "要求", "必须", "尽快", "紧急", "重要",
    "提交", "发送", "回复", "确认", "审核", "修改",
]

# 群聊"收到"类回复（用户表达确认/知晓）
ACKNOWLEDGEMENT_WORDS = [
    "收到", "好的", "好", "明白", "知道了", "了解", "ok", "OK",
    "没问题", "可以", "行", "嗯", "好啊",
]


# ── 统计信息 ──────────────────────────────────────────────────────────────

def get_contact_stats(top_n: int = 50) -> list[dict]:
    """
    按消息频率获取 Top N 联系人统计。
    返回包含 wxid, name, msg_count, is_sender_ratio, sample_msgs 的列表。
    """
    _ensure_wechat_tables()
    with main_db.get_conn() as conn:
        rows = conn.execute("""
            SELECT
                m.talker_wxid,
                COALESCE(c.remark, c.nickname, m.talker_name) as display_name,
                COUNT(*) as total_msgs,
                SUM(m.is_sender) as sent_by_me,
                COALESCE(c.role, 'unknown') as current_role,
                COALESCE(c.is_group, 0) as is_group,
                MAX(m.create_time) as last_msg_at
            FROM wechat_messages m
            LEFT JOIN wechat_contacts c ON c.wxid = m.talker_wxid
            WHERE m.talker_wxid NOT LIKE 'gh_%'   -- 跳过公众号
              AND m.talker_wxid NOT LIKE 'fmessage%'
            GROUP BY m.talker_wxid
            ORDER BY total_msgs DESC
            LIMIT ?
        """, (top_n,)).fetchall()

        # 为每个联系人获取最近消息样本
        result = []
        for r in rows:
            wxid = r[0]
            samples = conn.execute("""
                SELECT content FROM wechat_messages
                WHERE talker_wxid=? ORDER BY create_time DESC LIMIT 10
            """, (wxid,)).fetchall()
            sample_texts = [s[0][:80] for s in samples if s[0]]

            result.append({
                "wxid":         wxid,
                "name":         r[1] or wxid,
                "msg_count":    r[2],
                "sent_by_me":   r[3] or 0,
                "recv_ratio":   round((r[2] - (r[3] or 0)) / max(r[2], 1), 2),
                "current_role": r[4],
                "is_group":     bool(r[5]),
                "last_msg_at":  r[6],
                "samples":      sample_texts,
            })

    return result


def get_group_stats(top_n: int = 30) -> list[dict]:
    """获取群组统计"""
    _ensure_wechat_tables()
    with main_db.get_conn() as conn:
        rows = conn.execute("""
            SELECT
                m.talker_wxid,
                COALESCE(g.name, c.nickname, m.talker_name) as display_name,
                COUNT(*) as total_msgs,
                COALESCE(g.group_type, 'unknown') as current_type,
                MAX(m.create_time) as last_msg_at
            FROM wechat_messages m
            LEFT JOIN wechat_groups g ON g.wxid = m.talker_wxid
            LEFT JOIN wechat_contacts c ON c.wxid = m.talker_wxid
            WHERE (m.talker_wxid LIKE '%@chatroom'      -- 微信群 wxid 特征
                   OR COALESCE(c.is_group, 0) = 1)
            GROUP BY m.talker_wxid
            ORDER BY total_msgs DESC
            LIMIT ?
        """, (top_n,)).fetchall()

        result = []
        for r in rows:
            wxid = r[0]
            samples = conn.execute("""
                SELECT content FROM wechat_messages
                WHERE talker_wxid=? AND is_sender=0
                ORDER BY create_time DESC LIMIT 5
            """, (wxid,)).fetchall()
            result.append({
                "wxid":         wxid,
                "name":         r[1] or wxid,
                "msg_count":    r[2],
                "current_type": r[3],
                "last_msg_at":  r[4],
                "samples":      [s[0][:80] for s in samples if s[0]],
            })

    return result


# ── AI 推断 ───────────────────────────────────────────────────────────────

def _extract_freq_words(wxid: str, top_n: int = 80) -> tuple[list[str], list[str]]:
    """
    从该联系人的全部消息中提取高频词。
    返回 (我说的高频词, 对方说的高频词)
    使用字符级 bigram/trigram 统计，无需分词库。
    """
    with main_db.get_conn() as conn:
        rows = conn.execute("""
            SELECT content, is_sender FROM wechat_messages
            WHERE talker_wxid=? AND msg_type=1
            ORDER BY create_time DESC
            LIMIT 2000
        """, (wxid,)).fetchall()

    from collections import Counter
    import re as _re

    def ngrams(text: str) -> list[str]:
        """提取中文字符 bigram + trigram，以及英文单词"""
        words = []
        # 英文单词
        words += _re.findall(r'[a-zA-Z]{2,}', text.lower())
        # 中文字符序列
        chinese = _re.findall(r'[\u4e00-\u9fff]+', text)
        for seg in chinese:
            # bigram
            words += [seg[i:i+2] for i in range(len(seg)-1)]
            # trigram
            words += [seg[i:i+3] for i in range(len(seg)-2)]
        return words

    # 过滤词：无意义的高频字
    STOPWORDS = set("的了我你他她是在有和就也都不这那上下来去我你他她啊哦嗯呢吧")

    my_counter    = Counter()
    their_counter = Counter()

    for content, is_sender in rows:
        if not content:
            continue
        grams = [w for w in ngrams(content) if w not in STOPWORDS and len(w) >= 2]
        if is_sender:
            my_counter.update(grams)
        else:
            their_counter.update(grams)

    my_words    = [w for w, _ in my_counter.most_common(top_n)]
    their_words = [w for w, _ in their_counter.most_common(top_n)]
    return my_words, their_words


def _ai_infer_contact_role(name: str, msg_count: int,
                           recv_ratio: float, samples: list[str],
                           wxid: str = "") -> tuple[str, float]:
    """
    AI 推断单个联系人角色，返回 (role, confidence)。
    使用词频分析而非原始消息，更有代表性且节省 token。
    """
    # 词频分析（基于全部历史消息，而非仅最近8条）
    my_words, their_words = _extract_freq_words(wxid) if wxid else ([], [])

    freq_section = ""
    if my_words or their_words:
        freq_section = (
            f"\n我发出的高频词（代表我的关注点）: {', '.join(my_words[:40])}"
            f"\n对方发出的高频词（代表对方的关注点）: {', '.join(their_words[:40])}"
        )
    else:
        # 降级：使用样本
        freq_section = "\n最近消息样本:\n" + "\n".join(f"  · {s}" for s in samples[:8])

    prompt = f"""分析以下微信联系人，推断其与用户的关系角色。

联系人名称: {name}
消息总数: {msg_count}条（对方发给我的比例: {recv_ratio:.0%}）{freq_section}

可选角色（选一个最合适的）:
  superior      — 上级/导师，发布任务、审核工作
  collaborator  — 合作者，双向讨论项目
  junior        — 下级/学生，向你汇报或请教
  colleague     — 同事/同学，日常协作
  close_personal— 亲密个人（恋人/家人/挚友）
  friend        — 普通朋友
  service       — 商家/服务商/外卖快递

以JSON输出: {{"role": "角色", "confidence": 0.0-1.0, "reason": "一句话理由"}}
只输出JSON。"""

    try:
        raw = ai_client.chat(
            messages=[{"role": "user", "content": prompt}],
            system_prompt="你是社交关系分析专家。根据高频词和消息统计推断关系类型，输出简洁准确。",
            temperature=0.2,
        )
        raw = raw.strip().strip("```json").strip("```").strip()
        data = json.loads(raw)
        role = data.get("role", "unknown")
        conf = float(data.get("confidence", 0.5))
        if role not in ROLES:
            role = "unknown"
        return role, conf
    except Exception:
        return "unknown", 0.3


def _ai_infer_group_type(name: str, msg_count: int,
                         samples: list[str], wxid: str = "") -> tuple[str, float]:
    """AI 推断群组类型，返回 (group_type, confidence)。使用词频分析。"""
    # 群聊词频（所有成员发言，不区分 is_sender）
    _, freq_words = _extract_freq_words(wxid) if wxid else ([], [])
    if freq_words:
        freq_section = f"\n群内高频词: {', '.join(freq_words[:50])}"
    else:
        samples_text = "\n".join(f"  · {s}" for s in samples[:5])
        freq_section = f"\n近期消息样本:\n{samples_text}"

    prompt = f"""分析以下微信群，推断其重要程度类型。

群名称: {name}
消息总数: {msg_count}条{freq_section}

可选类型:
  core   — 核心群：课题组群、项目合作群、重要工作群（需要全面监控）
  normal — 普通群：同学群、部门群、一般工作群（关键词触发时关注）
  noise  — 低价值群：广告群、老乡群、闲聊群（忽略）

以JSON输出: {{"type": "类型", "confidence": 0.0-1.0, "reason": "一句话理由"}}
只输出JSON。"""

    try:
        raw = ai_client.chat(
            messages=[{"role": "user", "content": prompt}],
            system_prompt="你是社交关系分析专家。根据群名和聊天样本判断群的重要程度。",
            temperature=0.2,
        )
        raw = raw.strip().strip("```json").strip("```").strip()
        data = json.loads(raw)
        gtype = data.get("type", "normal")
        conf  = float(data.get("confidence", 0.5))
        if gtype not in GROUP_TYPES:
            gtype = "normal"
        return gtype, conf
    except Exception:
        return "normal", 0.3


# ── 批量推断 ─────────────────────────────────────────────────────────────

def infer_all(top_contacts: int = 30, top_groups: int = 20,
              force: bool = False) -> dict:
    """
    对 Top N 联系人和群组做角色/类型推断。
    已有手动设置的跳过（除非 force=True）。
    返回推断结果 {"contacts": [...], "groups": [...]}
    """
    from memory.pending import add_person

    _ensure_wechat_tables()
    results = {"contacts": [], "groups": []}

    # ── 联系人 ────────────────────────────────────────────────────────────
    contacts = get_contact_stats(top_contacts)
    for c in contacts:
        if c["is_group"]:
            continue
        # 已有手动角色时跳过
        if c["current_role"] != "unknown" and not force:
            results["contacts"].append({**c, "action": "skip (already set)"})
            continue

        _safe_print(f"[Roles] 推断联系人角色: {c['name']} ({c['msg_count']}条消息)")
        role, conf = _ai_infer_contact_role(
            c["name"], c["msg_count"], c["recv_ratio"], c["samples"],
            wxid=c["wxid"]
        )

        # 写回数据库（AI 推断，待用户确认）
        _update_contact_role(c["wxid"], role, conf, source="ai")

        # 置信度 < 0.7 的放 pending 等用户确认
        if conf < 0.7:
            add_person(
                name=c["name"], role=f"{role}（{ROLES.get(role, role)}）",
                source="wechat",
                note=f"AI推断（{conf:.0%}置信度），{c['msg_count']}条消息",
                confidence=conf,
            )

        results["contacts"].append({**c, "inferred_role": role, "confidence": conf})

    # ── 群组 ─────────────────────────────────────────────────────────────
    groups = get_group_stats(top_groups)
    for g in groups:
        if g["current_type"] != "unknown" and not force:
            results["groups"].append({**g, "action": "skip (already set)"})
            continue

        _safe_print(f"[Roles] 推断群类型: {g['name']} ({g['msg_count']}条消息)")
        gtype, conf = _ai_infer_group_type(g["name"], g["msg_count"], g["samples"],
                                           wxid=g["wxid"])

        _update_group_type(g["wxid"], g["name"], gtype, conf, source="ai")

        results["groups"].append({**g, "inferred_type": gtype, "confidence": conf})

    _safe_print(
        f"[Roles] 推断完成: {len(results['contacts'])} 联系人, "
        f"{len(results['groups'])} 群组"
    )
    return results


# ── 新联系人持续检测 ─────────────────────────────────────────────────────

# 触发推断的消息数阈值
MIN_MSGS_FOR_INFERENCE = 5    # 至少5条才值得推断
FULL_FREQ_THRESHOLD    = 30   # 30条以上用词频分析
RECHECK_DAYS           = 7    # 7天内不重复推断
RECHECK_GROWTH_RATIO   = 0.5  # 消息数增长 50% 以上时重新推断


def check_new_contacts(max_batch: int = 20) -> int:
    """
    检测需要角色推断的联系人（新出现 or 需重新评估）。
    每次微信同步后调用。
    返回触发推断的联系人数。
    """
    from datetime import datetime, timedelta

    _ensure_wechat_tables()
    recheck_cutoff = (datetime.now() - timedelta(days=RECHECK_DAYS)).isoformat()

    with main_db.get_conn() as conn:
        rows = conn.execute("""
            SELECT wxid,
                   COALESCE(remark, nickname, wxid) as name,
                   msg_count,
                   msg_count_at_check,
                   role,
                   role_source,
                   last_role_check_at,
                   is_group
            FROM wechat_contacts
            WHERE is_group = 0
              AND wxid NOT LIKE 'gh_%'
              AND msg_count >= ?
              AND (
                  -- 新联系人：从未推断过
                  last_role_check_at IS NULL
                  -- 消息量大幅增长（关系可能变化）
                  OR (msg_count > msg_count_at_check * (1 + ?)
                      AND last_role_check_at < ?)
                  -- 手动设置的不重复推断
              )
              AND role_source != 'manual'
            ORDER BY msg_count DESC
            LIMIT ?
        """, (MIN_MSGS_FOR_INFERENCE, RECHECK_GROWTH_RATIO,
              recheck_cutoff, max_batch)).fetchall()

    if not rows:
        return 0

    triggered = 0
    for row in rows:
        wxid        = row[0]
        name        = row[1]
        msg_count   = row[2]
        old_count   = row[3] or 0
        old_role    = row[4]
        is_recheck  = old_role != 'unknown'

        _safe_print(
            f"[Roles] {'重新评估' if is_recheck else '新联系人'}: "
            f"{name} ({msg_count}条消息)"
        )

        if msg_count >= FULL_FREQ_THRESHOLD:
            # 消息足够：词频 + AI
            role, conf = _ai_infer_contact_role(
                name, msg_count,
                recv_ratio=0.5,  # 实际从 stats 取更精确，这里简化
                samples=[],
                wxid=wxid,
            )
        else:
            # 消息少：仅用样本快速推断
            with main_db.get_conn() as conn:
                samples = conn.execute("""
                    SELECT content FROM wechat_messages
                    WHERE talker_wxid=? ORDER BY create_time DESC LIMIT 15
                """, (wxid,)).fetchall()
            sample_texts = [s[0][:80] for s in samples if s[0]]
            role, conf = _ai_infer_contact_role(
                name, msg_count, recv_ratio=0.5, samples=sample_texts, wxid=""
            )

        # 记录推断结果
        _update_contact_role(wxid, role, conf, source="ai")
        _mark_role_checked(wxid, msg_count)

        # 角色发生变化 or 低置信度 → 放 pending 通知用户
        if is_recheck and role != old_role:
            from memory.pending import add_person
            add_person(
                name=name, role=f"{role}（{ROLES.get(role, role)}）",
                source="wechat",
                note=f"角色变更 {old_role}→{role}（{conf:.0%}置信度，{msg_count}条消息）",
                confidence=conf,
            )
        elif not is_recheck and conf < 0.7:
            from memory.pending import add_person
            add_person(
                name=name, role=f"{role}（{ROLES.get(role, role)}）",
                source="wechat",
                note=f"新联系人，AI推断（{conf:.0%}置信度，{msg_count}条消息）",
                confidence=conf,
            )

        triggered += 1

    return triggered


def _mark_role_checked(wxid: str, current_msg_count: int):
    """标记推断时间和当时消息数"""
    with main_db.get_conn() as conn:
        conn.execute("""
            UPDATE wechat_contacts
            SET last_role_check_at=?, msg_count_at_check=?
            WHERE wxid=?
        """, (datetime.now().isoformat(), current_msg_count, wxid))


# ── 手动设置 ─────────────────────────────────────────────────────────────

def set_contact_role(name_or_wxid: str, role: str) -> bool:
    """手动设置联系人角色（邮件指令调用）"""
    if role not in ROLES:
        _safe_print(f"[Roles] 未知角色: {role}，可选: {list(ROLES.keys())}")
        return False

    _ensure_wechat_tables()
    with main_db.get_conn() as conn:
        # 按备注/昵称模糊匹配
        rows = conn.execute("""
            SELECT wxid FROM wechat_contacts
            WHERE wxid=? OR remark LIKE ? OR nickname LIKE ?
        """, (name_or_wxid, f"%{name_or_wxid}%", f"%{name_or_wxid}%")).fetchall()

    if not rows:
        _safe_print(f"[Roles] 未找到联系人: {name_or_wxid}")
        return False

    for row in rows:
        _update_contact_role(row[0], role, confidence=1.0, source="manual")

    _safe_print(f"[Roles] 已设置 {name_or_wxid} → {role}")
    return True


def set_group_type(name_or_wxid: str, group_type: str) -> bool:
    """手动设置群组类型"""
    if group_type not in GROUP_TYPES:
        return False

    _ensure_wechat_tables()
    with main_db.get_conn() as conn:
        rows = conn.execute("""
            SELECT wxid FROM wechat_groups
            WHERE wxid=? OR name LIKE ? OR display_name LIKE ?
        """, (name_or_wxid, f"%{name_or_wxid}%", f"%{name_or_wxid}%")).fetchall()

    if not rows:
        return False

    for row in rows:
        _update_group_type(row[0], name_or_wxid, group_type, 1.0, "manual")

    _safe_print(f"[Roles] 已设置群 {name_or_wxid} → {group_type}")
    return True


# ── DB 写入工具 ───────────────────────────────────────────────────────────

def _update_contact_role(wxid: str, role: str, confidence: float, source: str):
    with main_db.get_conn() as conn:
        conn.execute("""
            UPDATE wechat_contacts
            SET role=?, role_confidence=?, role_source=?, updated_at=?
            WHERE wxid=?
        """, (role, confidence, source, datetime.now().isoformat(), wxid))


def _update_group_type(wxid: str, name: str, gtype: str,
                       confidence: float, source: str):
    with main_db.get_conn() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO wechat_groups
            (wxid, name, group_type, type_confidence, type_source, updated_at)
            VALUES (?,?,?,?,?,?)
        """, (wxid, name, gtype, confidence, source, datetime.now().isoformat()))


# ── 查询工具（供 focus_updater 使用）────────────────────────────────────

def get_contacts_by_role(role: str) -> list[str]:
    """返回指定角色的所有 wxid 列表"""
    _ensure_wechat_tables()
    with main_db.get_conn() as conn:
        rows = conn.execute(
            "SELECT wxid FROM wechat_contacts WHERE role=? AND is_group=0",
            (role,)
        ).fetchall()
    return [r[0] for r in rows]


def get_core_groups() -> list[str]:
    """返回所有核心群 wxid"""
    _ensure_wechat_tables()
    with main_db.get_conn() as conn:
        rows = conn.execute(
            "SELECT wxid FROM wechat_groups WHERE group_type='core'"
        ).fetchall()
    return [r[0] for r in rows]


def get_normal_groups() -> list[str]:
    """返回普通群 wxid（仅关键词触发时扫描）"""
    _ensure_wechat_tables()
    with main_db.get_conn() as conn:
        rows = conn.execute(
            "SELECT wxid FROM wechat_groups WHERE group_type='normal'"
        ).fetchall()
    return [r[0] for r in rows]


def format_role_summary() -> str:
    """生成角色分配摘要，用于邮件汇报"""
    _ensure_wechat_tables()
    with main_db.get_conn() as conn:
        role_counts = conn.execute("""
            SELECT role, COUNT(*) FROM wechat_contacts
            WHERE is_group=0 GROUP BY role ORDER BY COUNT(*) DESC
        """).fetchall()
        type_counts = conn.execute("""
            SELECT group_type, COUNT(*) FROM wechat_groups GROUP BY group_type
        """).fetchall()

    lines = ["【微信联系人角色分布】"]
    for role, cnt in role_counts:
        desc = ROLES.get(role, role)
        lines.append(f"  {desc}: {cnt}人")

    lines.append("\n【群组类型分布】")
    for gtype, cnt in type_counts:
        desc = GROUP_TYPES.get(gtype, gtype)
        lines.append(f"  {desc}: {cnt}个")

    return "\n".join(lines)
