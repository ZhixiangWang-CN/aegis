"""
微信记忆构建器（三层结构）

第一层：重要联系人档案（私聊 > 50条）
  → data/memory/contacts/{name}.md
  内容：身份/关系 · 主要话题 · 最近动态 · 待跟进

第二层：重要群聊档案（群消息 > 100条）
  → data/memory/groups/{name}.md
  内容：群的主题 · 核心成员 · 最近讨论

第三层：近期活跃决定/承诺/任务流水（最近30天）
  → data/memory/wechat_active.md
  内容：明确的承诺 · 截止时间 · 待办

汇总：
  → data/memory/from_wechat.md

运行：
  python main.py --build-wechat-memory
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import config
from ai import client as ai
from memory import db as main_db
from memory.writer import get_writer

MEMORY_DIR   = config.DATA_DIR / "memory"
CONTACTS_DIR = MEMORY_DIR / "contacts"
GROUPS_DIR   = MEMORY_DIR / "groups"
WECHAT_MEM   = MEMORY_DIR / "from_wechat.md"
ACTIVE_MEM   = MEMORY_DIR / "wechat_active.md"

# 私聊：用户本人发言次数最少阈值（低于此不建档案）
MIN_MY_MSGS_PRIVATE = 20
# 群聊：用户本人发言次数最少阈值
MIN_MY_MSGS_GROUP   = 20
# 每个联系人最多送给AI的消息条数（取最近N条）
MAX_MSGS_FOR_PROFILE = 120
# 近期活跃判断（天）
RECENT_DAYS = 30


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _is_group(wxid: str) -> bool:
    """微信群的 wxid 以 @ 结尾（chatroom）"""
    return wxid.endswith("@chatroom")


# ── AI Prompts ─────────────────────────────────────────────────────────────

_CONTACT_PROFILE_PROMPT = """\
你是Aegis，正在整理用户的微信通讯记忆。

以下是用户与某联系人的微信聊天记录（取最近若干条）。请提取：
1. 这个联系人是谁（姓名/机构/身份，从对话内容推断）
2. 与用户的关系（合作者/导师/学生/朋友/同事/服务/其他）
3. 主要聊什么话题或项目（最多5条，按重要性排）
4. 最近动态（最近几条消息在说什么）
5. 有无待跟进事项（用户承诺的/对方等待回复的）

输出格式（Markdown）：
**身份**: （推断的姓名/机构/角色）
**关系**: （与用户的关系）
**主要话题**:
- （话题1）
- （话题2）
**最近动态**: （1-2句）
**待跟进**: （无则省略）

只输出有实质内容的字段，不要编造。"""


_GROUP_PROFILE_PROMPT = """\
你是Aegis，正在整理用户的微信群聊记忆。

以下是某个微信群的聊天记录（取最近若干条）。请提取：
1. 这个群的主题/用途（从对话内容判断）
2. 核心活跃成员（提及最多的人名）
3. 最近在讨论什么（最近的主要话题）
4. 有无待用户处理的事项

输出格式（Markdown）：
**群主题**: （一句话描述这个群的用途）
**核心成员**: （姓名列表，逗号分隔）
**最近讨论**: （1-3条最近话题）
**需用户处理**: （无则省略）

只输出有实质内容的字段，不要编造。"""


_ACTIVE_DECISIONS_PROMPT = """\
你是Aegis，正在从用户最近30天的微信消息中提取重要行动项。

请从以下消息中提取：
1. 用户做出的明确承诺（"我明天发给你"、"这周搞定"）
2. 对方在等待用户回复/处理的事项
3. 提到的明确截止时间
4. 待办任务

只提取明确、具体的内容，不要提取模糊的闲聊。
来源人名标注清楚。

输出格式（Markdown 列表）：
## 用户的承诺/待办
- （内容）—— 涉及：（对方姓名）

## 对方在等待
- （内容）—— 来自：（对方姓名）

## 截止时间
- （截止日期）：（事项）

没有内容的节直接省略。"""


# ── 数据加载 ──────────────────────────────────────────────────────────────

def _load_wxid_name_map() -> dict[str, str]:
    """从 wechat_contacts 加载 wxid → 显示名 的映射（优先 remark > nickname > wxid）"""
    try:
        with main_db.get_conn() as conn:
            rows = conn.execute(
                "SELECT wxid, nickname, remark FROM wechat_contacts"
            ).fetchall()
        result = {}
        for r in rows:
            wxid = r["wxid"] or ""
            if not wxid:
                continue
            name = (r["remark"] or "").strip() or (r["nickname"] or "").strip() or wxid
            result[wxid] = name
        return result
    except Exception:
        return {}


def _resolve_name(wxid: str, talker_name: str, name_map: dict[str, str]) -> str:
    """
    将 talker_name 解析为可读名称。
    如果 talker_name 看起来是原始 wxid（以 wxid_ 开头，或与 talker_wxid 相同），
    则从 wechat_contacts 查找真实昵称/备注。
    """
    raw = (talker_name or "").strip()
    # 若名称就是 wxid 形式，或与 wxid 相同，尝试查 contacts
    if not raw or raw == wxid or raw.startswith("wxid_") or raw.endswith("@chatroom"):
        looked_up = name_map.get(wxid, "")
        if looked_up and not looked_up.startswith("wxid_"):
            return looked_up
        return raw or wxid
    return raw


def _load_chats(days_back: int = None) -> dict[str, list[dict]]:
    """加载微信消息，按 wxid 分组，返回 {wxid: [msg, ...]}"""
    name_map = _load_wxid_name_map()

    with main_db.get_conn() as conn:
        if days_back:
            since = (datetime.now() - timedelta(days=days_back)).isoformat()
            rows = conn.execute("""
                SELECT msg_id, talker_wxid, talker_name, content,
                       is_sender, create_time
                FROM wechat_messages
                WHERE content IS NOT NULL AND LENGTH(TRIM(content)) > 1
                  AND create_time >= ?
                ORDER BY talker_wxid, create_time ASC
            """, (since,)).fetchall()
        else:
            rows = conn.execute("""
                SELECT msg_id, talker_wxid, talker_name, content,
                       is_sender, create_time
                FROM wechat_messages
                WHERE content IS NOT NULL AND LENGTH(TRIM(content)) > 1
                ORDER BY talker_wxid, create_time ASC
            """).fetchall()

    by_chat: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        row = dict(r)
        # 解析真实显示名
        row["talker_name"] = _resolve_name(row["talker_wxid"], row["talker_name"], name_map)
        by_chat[row["talker_wxid"]].append(row)
    return dict(by_chat)


def _format_msgs(msgs: list[dict], contact_name: str, max_msgs: int = MAX_MSGS_FOR_PROFILE) -> str:
    """把消息列表格式化为可读文本，取最近 max_msgs 条"""
    recent = msgs[-max_msgs:]
    lines = []
    for m in recent:
        speaker = "我" if m.get("is_sender") else (contact_name or m.get("talker_name") or "对方")
        ts = (m.get("create_time") or "")[:10]
        content = (m.get("content") or "").strip()[:300]
        if content:
            lines.append(f"[{ts}] {speaker}: {content}")
    return "\n".join(lines)


# ── 第一层：联系人档案 ─────────────────────────────────────────────────────

def build_contact_profiles(chats: dict[str, list[dict]], top_n: int = None) -> int:
    """为重要私聊联系人生成档案文件，返回生成数量"""
    CONTACTS_DIR.mkdir(parents=True, exist_ok=True)
    count = 0

    # 筛选私聊：按用户本人发言次数过滤，优先分析发言多的
    # 若 is_sender 数据不可用（全为0，旧版导入），降级为总消息数 ÷ 2 估算
    all_my = sum(1 for msgs in chats.values() for m in msgs if m.get("is_sender"))
    use_total_fallback = (all_my == 0)
    if use_total_fallback:
        print("[WxMem] 注意: is_sender 数据缺失，使用总消息数÷2估算（建议重新导入微信数据）")

    private_chats = []
    for wxid, msgs in chats.items():
        if _is_group(wxid):
            continue
        my_count = sum(1 for m in msgs if m.get("is_sender"))
        if use_total_fallback:
            my_count = len(msgs) // 2  # 估算：假设双方各发一半
        if my_count >= MIN_MY_MSGS_PRIVATE:
            private_chats.append((wxid, msgs, my_count))
    private_chats.sort(key=lambda x: x[2], reverse=True)

    if top_n:
        private_chats = private_chats[:top_n]
    print(f"[WxMem] 私聊联系人: {len(private_chats)} 个（我发言≥{MIN_MY_MSGS_PRIVATE}次{'[估算]' if use_total_fallback else ''}{'，限制Top'+str(top_n) if top_n else ''}）")

    for wxid, msgs, my_count in private_chats:
        contact_name = msgs[0].get("talker_name") or wxid
        safe_name = re.sub(r'[<>:"/\\|?*@.\r\n\t]', '_', contact_name).strip('_')
        out_path = CONTACTS_DIR / f"wx_{safe_name}.md"

        # 已有档案且较新（3天内）则跳过
        if out_path.exists():
            age_days = (datetime.now().timestamp() - out_path.stat().st_mtime) / 86400
            if age_days < 3:
                count += 1
                continue

        msgs_text = _format_msgs(msgs, contact_name)
        print(f"[WxMem] 分析联系人: {contact_name[:20]} (总{len(msgs)}条, 我发言{my_count}次)")

        try:
            profile = ai.chat(
                messages=[{"role": "user", "content": f"联系人: {contact_name}\n消息总数: {len(msgs)}\n\n最近消息:\n{msgs_text}"}],
                system_prompt=_CONTACT_PROFILE_PROMPT,
                temperature=0.2,
            )
            if not profile or not profile.strip():
                continue

            # 判断最近是否活跃
            recent_cutoff = (datetime.now() - timedelta(days=RECENT_DAYS)).isoformat()
            recent_count = sum(1 for m in msgs if (m.get("create_time") or "") >= recent_cutoff)
            activity = "🔴 活跃" if recent_count > 5 else ("🟡 近期有往来" if recent_count > 0 else "⚫ 不活跃")

            file_content = (
                f"# 微信联系人: {contact_name}\n"
                f"> 消息总数: {len(msgs)} | 近30天: {recent_count}条 | {activity} | 更新: {_ts()}\n\n"
                + profile.strip()
            )
            rel_path = str(out_path.relative_to(MEMORY_DIR))
            get_writer().write(rel_path, "update", file_content, "wechat")
            count += 1
        except Exception as e:
            print(f"[WxMem] 联系人分析失败 {contact_name}: {e}")

    return count


# ── 第二层：群聊档案 ──────────────────────────────────────────────────────

def build_group_profiles(chats: dict[str, list[dict]], top_n: int = None) -> int:
    """为重要群聊生成档案文件，返回生成数量"""
    GROUPS_DIR.mkdir(parents=True, exist_ok=True)
    count = 0

    # 判断 is_sender 是否可用
    all_my_group = sum(
        1 for wxid, msgs in chats.items()
        if _is_group(wxid)
        for m in msgs if m.get("is_sender")
    )
    use_total_fallback = (all_my_group == 0)
    if use_total_fallback:
        print(f"[WxMem] 注意: is_sender 数据缺失，使用总消息数÷10估算（群聊）")

    group_chats = []
    for wxid, msgs in chats.items():
        if not _is_group(wxid):
            continue
        my_count = sum(1 for m in msgs if m.get("is_sender"))
        if use_total_fallback:
            my_count = len(msgs) // 10  # 群里自己发言比例低，取10%估算
        if my_count >= MIN_MY_MSGS_GROUP:
            group_chats.append((wxid, msgs, my_count))
    group_chats.sort(key=lambda x: x[2], reverse=True)

    if top_n:
        group_chats = group_chats[:top_n]
    print(f"[WxMem] 重要群聊: {len(group_chats)} 个（我发言≥{MIN_MY_MSGS_GROUP}次{'[估算]' if use_total_fallback else ''}{'，限制Top'+str(top_n) if top_n else ''}）")

    for wxid, msgs, my_count in group_chats:
        group_name = msgs[0].get("talker_name") or wxid
        safe_name = re.sub(r'[<>:"/\\|?*@.]', '_', group_name)
        out_path = GROUPS_DIR / f"{safe_name}.md"

        if out_path.exists():
            age_days = (datetime.now().timestamp() - out_path.stat().st_mtime) / 86400
            if age_days < 3:
                count += 1
                continue

        # 群聊只取最近消息
        msgs_text = _format_msgs(msgs, group_name, max_msgs=150)
        print(f"[WxMem] 分析群聊: {group_name[:25]} (总{len(msgs)}条, 我发言{my_count}次)")

        try:
            profile = ai.chat(
                messages=[{"role": "user", "content": f"群名: {group_name}\n消息总数: {len(msgs)}\n\n最近消息:\n{msgs_text}"}],
                system_prompt=_GROUP_PROFILE_PROMPT,
                temperature=0.2,
            )
            if not profile or not profile.strip():
                continue

            recent_cutoff = (datetime.now() - timedelta(days=RECENT_DAYS)).isoformat()
            recent_count = sum(1 for m in msgs if (m.get("create_time") or "") >= recent_cutoff)

            file_content = (
                f"# 微信群: {group_name}\n"
                f"> 消息总数: {len(msgs)} | 近30天: {recent_count}条 | 更新: {_ts()}\n\n"
                + profile.strip()
            )
            rel_path = str(out_path.relative_to(MEMORY_DIR))
            get_writer().write(rel_path, "update", file_content, "wechat")
            count += 1
        except Exception as e:
            print(f"[WxMem] 群聊分析失败 {group_name}: {e}")

    return count


# ── 第三层：近期活跃决定/任务 ─────────────────────────────────────────────

def build_active_log(chats: dict[str, list[dict]]) -> str:
    """
    从最近30天所有消息中提取承诺/待办/截止时间。
    只处理有实质内容的私聊（群聊噪音太多）。
    """
    cutoff = (datetime.now() - timedelta(days=RECENT_DAYS)).isoformat()

    # 收集最近30天的私聊消息
    recent_lines = []
    for wxid, msgs in chats.items():
        if _is_group(wxid):
            continue
        contact_name = msgs[0].get("talker_name") or wxid
        recent = [m for m in msgs if (m.get("create_time") or "") >= cutoff]
        if len(recent) < 3:
            continue
        for m in recent[-30:]:  # 每人最多30条
            speaker = "我" if m.get("is_sender") else contact_name
            ts = (m.get("create_time") or "")[:10]
            content = (m.get("content") or "").strip()[:200]
            if content:
                recent_lines.append(f"[{ts}][{contact_name}] {speaker}: {content}")

    if not recent_lines:
        return "（最近30天无私聊消息）"

    # 按时间排序
    recent_lines.sort()
    all_text = "\n".join(recent_lines[-400:])  # 最多400行

    print(f"[WxMem] 提取近期活跃决定/任务（{len(recent_lines)}条消息）...")
    try:
        result = ai.chat(
            messages=[{"role": "user", "content": all_text}],
            system_prompt=_ACTIVE_DECISIONS_PROMPT,
            temperature=0.2,
        )
        return result.strip() if result else "（无法提取）"
    except Exception as e:
        print(f"[WxMem] 活跃决定提取失败: {e}")
        return f"（提取失败: {e}）"


# ── 汇总 from_wechat.md ───────────────────────────────────────────────────

def _build_summary(chats: dict, contact_count: int, group_count: int) -> str:
    """生成 from_wechat.md 的统计头部"""
    total_msgs = sum(len(v) for v in chats.values())
    private_count = sum(1 for k in chats if not _is_group(k))
    group_total = sum(1 for k in chats if _is_group(k))

    cutoff = (datetime.now() - timedelta(days=RECENT_DAYS)).isoformat()
    active_contacts = sum(
        1 for wxid, msgs in chats.items()
        if not _is_group(wxid)
        and any((m.get("create_time") or "") >= cutoff and m.get("is_sender") for m in msgs)
    )

    return f"""# 记忆来源: 微信聊天
> 最后更新: {_ts()} | 总消息: {total_msgs:,}条 | 私聊: {private_count}人 | 群聊: {group_total}个
> 建档联系人: {contact_count}人 | 建档群聊: {group_count}个 | 近30天活跃联系人: {active_contacts}人

## 联系人档案
> 详见 `data/memory/contacts/wx_*.md`（共{contact_count}个文件）

## 群聊档案
> 详见 `data/memory/groups/*.md`（共{group_count}个文件）

"""


# ── 增量刷新活跃联系人（分层，节省 token） ───────────────────────────────────

# 触发 AI 分析的关键词（命中任意一个才调用 AI）
_TRIGGER_KEYWORDS = [
    # 时间约定
    "明天", "后天", "周一", "周二", "周三", "周四", "周五", "周六", "周日",
    "下周", "下个月", "几点", "几号", "月", "号", "点钟", "上午", "下午", "晚上",
    # 任务/承诺
    "帮我", "麻烦你", "你帮", "记得", "别忘", "需要", "要我", "我来",
    "发给你", "给你发", "确认", "回复我", "等你", "等我", "等一下",
    "尽快", "今天", "赶紧", "抓紧",
    # 重要信息
    "重要", "紧急", "急", "注意", "提醒", "通知", "告诉你",
    "结果", "进展", "更新", "完成了", "做好了", "搞定",
]

def _has_trigger(msgs: list) -> bool:
    """规则过滤：消息里是否含触发关键词"""
    for m in msgs:
        content = (m.get("content") or m[2] if isinstance(m, (list, tuple)) else m.get("content") or "")
        for kw in _TRIGGER_KEYWORDS:
            if kw in content:
                return True
    return False


def _patch_profile(md_path: Path, contact_name: str, new_activity: str,
                   new_followup: str, total_msgs: int, recent_count: int):
    """只更新档案中的「最近动态」和「待跟进」字段，不重写整个文件"""
    try:
        content = md_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return

    # 更新头部统计行
    activity = "🔴 活跃" if recent_count > 5 else ("🟡 近期有往来" if recent_count > 0 else "⚫ 不活跃")
    lines = content.splitlines()
    if len(lines) > 1 and lines[1].startswith(">"):
        lines[1] = f"> 消息总数: {total_msgs} | 近30天: {recent_count}条 | {activity} | 更新: {_ts()}"

    content = "\n".join(lines)

    # patch 最近动态
    if new_activity:
        if "**最近动态**:" in content:
            content = re.sub(
                r"\*\*最近动态\*\*:.*?(?=\n\*\*|\Z)",
                f"**最近动态**: {new_activity}",
                content, flags=re.DOTALL
            )
        else:
            content += f"\n**最近动态**: {new_activity}"

    # patch 待跟进
    if new_followup:
        if "**待跟进**:" in content:
            content = re.sub(
                r"\*\*待跟进\*\*:.*?(?=\n\*\*|\Z)",
                f"**待跟进**: {new_followup}",
                content, flags=re.DOTALL
            )
        else:
            content += f"\n**待跟进**: {new_followup}"

    try:
        rel_path = str(md_path.relative_to(MEMORY_DIR))
        get_writer().write(rel_path, "update", content, "wechat")
    except Exception:
        md_path.write_text(content, encoding="utf-8")


def refresh_active_contacts() -> int:
    """
    增量更新活跃联系人档案 + 提取焦点事项（分层，节省 token）。

    第1层（无 AI）：检查有没有新消息，没有直接跳过
    第2层（规则）：关键词过滤，纯闲聊不调 AI
    第3层（AI）：只提取增量（最近动态+待跟进+焦点），不重建全文
    全量重建只在手动 build_wechat_memory() 时触发。

    返回触发 AI 分析的联系人数。
    """
    from memory.layers import FocusItem, add_focus_item

    # ── 第1层: 读活跃档案，查是否有新消息 ──────────────────────────────────
    name_map = _load_wxid_name_map()
    name_to_wxid = {v: k for k, v in name_map.items()}

    candidates = []  # [(wxid, contact_name, md_path, profile_mtime, new_msgs)]
    for md_path in CONTACTS_DIR.glob("wx_*.md"):
        try:
            header = md_path.read_text(encoding="utf-8", errors="replace")[:300]
        except Exception:
            continue
        if "🔴 活跃" not in header and "🟡 近期" not in header:
            continue

        first_line = header.splitlines()[0]
        chat_id_raw = first_line.replace("# 微信联系人:", "").strip()
        wxid = chat_id_raw if (chat_id_raw.startswith("wxid_") or "@chatroom" in chat_id_raw) \
               else name_to_wxid.get(chat_id_raw, chat_id_raw)
        profile_mtime = datetime.fromtimestamp(md_path.stat().st_mtime).isoformat()

        try:
            with main_db.get_conn() as conn:
                new_rows = conn.execute("""
                    SELECT talker_name, content, is_sender, create_time
                    FROM wechat_messages
                    WHERE (talker_wxid = ? OR chat_id = ?)
                      AND create_time > ?
                      AND content IS NOT NULL AND LENGTH(TRIM(content)) > 1
                    ORDER BY create_time ASC
                """, (wxid, wxid, profile_mtime)).fetchall()
        except Exception:
            continue

        if not new_rows:
            continue  # 无新消息，跳过

        contact_name = _resolve_name(wxid, new_rows[-1]["talker_name"], name_map) if new_rows else chat_id_raw
        candidates.append((wxid, contact_name, md_path, profile_mtime, [dict(r) for r in new_rows]))

    if not candidates:
        return 0

    print(f"[WxMem刷新] {len(candidates)} 个联系人有新消息")

    # ── 第2层: 关键词过滤，剔除纯闲聊 ──────────────────────────────────────
    to_analyze = [(wxid, name, path, mtime, msgs)
                  for wxid, name, path, mtime, msgs in candidates
                  if _has_trigger(msgs)]

    skipped = len(candidates) - len(to_analyze)
    if skipped:
        print(f"  → {skipped} 个纯闲聊跳过（无关键词），仅更新时间戳")
        # 纯闲聊：只更新头部时间戳，不调 AI
        for wxid, name, md_path, mtime, msgs in candidates:
            if not _has_trigger(msgs):
                try:
                    with main_db.get_conn() as conn:
                        total = conn.execute(
                            "SELECT COUNT(*) FROM wechat_messages WHERE talker_wxid=? OR chat_id=?",
                            (wxid, wxid)
                        ).fetchone()[0]
                        recent_cutoff = (datetime.now() - timedelta(days=RECENT_DAYS)).isoformat()
                        recent = conn.execute(
                            "SELECT COUNT(*) FROM wechat_messages WHERE (talker_wxid=? OR chat_id=?) AND create_time>=?",
                            (wxid, wxid, recent_cutoff)
                        ).fetchone()[0]
                    _patch_profile(md_path, name, "", "", total, recent)
                except Exception:
                    pass

    if not to_analyze:
        return 0

    print(f"  → {len(to_analyze)} 个有实质内容，调用 AI 分析")

    # ── 第3层: AI 只提取增量 ─────────────────────────────────────────────────
    _DELTA_PROMPT = """\
从以下微信新消息中提取增量信息，输出 JSON：
{
  "activity": "最近动态一句话摘要（无则空字符串）",
  "followup": "待跟进事项（无则空字符串）",
  "focus": [{"text":"...", "deadline":"YYYY-MM-DD或空", "priority":"urgent/normal/waiting"}]
}
focus 只包含明确的待办/约定/承诺，纯闲聊返回空列表。只返回 JSON。"""

    ai_called = 0
    for wxid, contact_name, md_path, profile_mtime, new_msgs in to_analyze:
        msg_text = "\n".join(
            f"[{m['create_time'][:16]}] {'我' if m['is_sender'] else contact_name}: {m['content'][:200]}"
            for m in new_msgs[-30:]  # 最多30条，控制 token
        )
        try:
            resp = ai.chat(
                [{"role": "user", "content": f"联系人: {contact_name}\n\n新消息:\n{msg_text}"}],
                system_prompt=_DELTA_PROMPT,
                temperature=0.1,
            )
            delta = json.loads(resp.strip().strip("```json").strip("```").strip())
        except Exception as e:
            print(f"  [跳过] {contact_name} AI解析失败: {e}")
            continue

        # 更新档案（只 patch 变化字段）
        try:
            with main_db.get_conn() as conn:
                total = conn.execute(
                    "SELECT COUNT(*) FROM wechat_messages WHERE talker_wxid=? OR chat_id=?", (wxid, wxid)
                ).fetchone()[0]
                recent_cutoff = (datetime.now() - timedelta(days=RECENT_DAYS)).isoformat()
                recent = conn.execute(
                    "SELECT COUNT(*) FROM wechat_messages WHERE (talker_wxid=? OR chat_id=?) AND create_time>=?",
                    (wxid, wxid, recent_cutoff)
                ).fetchone()[0]
        except Exception:
            total, recent = 0, 0

        _patch_profile(md_path, contact_name,
                       delta.get("activity", ""),
                       delta.get("followup", ""),
                       total, recent)

        # 写入焦点事项
        for item in (delta.get("focus") or []):
            if item.get("text"):
                add_focus_item(FocusItem(
                    text=item["text"],
                    deadline=item.get("deadline", ""),
                    source="wechat",
                    db_ref=f"wechat:{contact_name}",
                    priority=item.get("priority", "normal"),
                ))
                print(f"  💬 焦点[{contact_name}]: {item['text']}")

        ai_called += 1
        print(f"  ✓ {contact_name}: 动态已更新" + (f"，{len(delta.get('focus',[]))}条焦点" if delta.get("focus") else ""))

    print(f"[WxMem刷新] 完成：AI分析 {ai_called} 人，跳过闲聊 {skipped} 人")
    return ai_called


# ── 主入口 ────────────────────────────────────────────────────────────────

def build_wechat_memory(days_back: int = None, top_contacts: int = None, top_groups: int = None) -> dict:
    """
    微信记忆构建主入口。

    days_back=None: 加载全部消息用于统计/分析
    实际对话分析会优先取最近消息。
    """
    print(f"[WxMem] 加载微信消息（{'全部' if not days_back else f'最近{days_back}天'}）...")
    # 联系人档案分析全部历史（了解关系），活跃日志只看30天
    all_chats = _load_chats(days_back=days_back)
    recent_chats = _load_chats(days_back=RECENT_DAYS)

    print(f"[WxMem] 共 {len(all_chats)} 个会话")

    # 第一层：联系人档案（batch 模式，全程一次 git commit）
    print("\n[WxMem] === 第一层: 联系人档案 ===")
    with get_writer().batch(f"wechat_contacts_{datetime.now().strftime('%Y%m%d')}"):
        contact_count = build_contact_profiles(all_chats, top_n=top_contacts)

    # 第二层：群聊档案（batch 模式）
    print("\n[WxMem] === 第二层: 群聊档案 ===")
    with get_writer().batch(f"wechat_groups_{datetime.now().strftime('%Y%m%d')}"):
        group_count = build_group_profiles(all_chats, top_n=top_groups)

    # 第三层：近期活跃决定
    print("\n[WxMem] === 第三层: 近期活跃决定/任务 ===")
    active_log = build_active_log(recent_chats)

    # 写 wechat_active.md（通过 MemoryWriter）
    active_content = (
        f"# 近期微信活跃事项（最近{RECENT_DAYS}天）\n"
        f"> 更新: {_ts()}\n\n"
        + active_log
    )
    get_writer().write(
        str(ACTIVE_MEM.relative_to(MEMORY_DIR)), "update", active_content, "wechat"
    )

    # 写 from_wechat.md（汇总入口，通过 MemoryWriter）
    summary = _build_summary(all_chats, contact_count, group_count)
    wechat_content = summary + "## 近期活跃事项\n\n" + active_log
    get_writer().write(
        str(WECHAT_MEM.relative_to(MEMORY_DIR)), "update", wechat_content, "wechat"
    )

    print(f"\n[WxMem] 完成: 联系人档案{contact_count}个, 群聊档案{group_count}个")
    print(f"[WxMem] 输出: {WECHAT_MEM}")

    return {
        "total_chats": len(all_chats),
        "contact_profiles": contact_count,
        "group_profiles": group_count,
        "active_log": len(active_log),
    }
