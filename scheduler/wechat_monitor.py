"""
微信新消息监控 — 只读不写
每5分钟扫一次监控联系人的新消息，AI提取重要事项写入 focus.md

逻辑：
1. 读取各联系人的新消息（since 上次检查时间）
2. AI判断是否有重要内容（待办、时间约定、需跟进）
3. 有则 add_focus_item，无则跳过
"""
from __future__ import annotations

import json
import time
import threading
from datetime import datetime
from pathlib import Path

import config
from ai import client as ai_client
from memory.layers import FocusItem, add_focus_item
from scheduler.wechat_commander import _read_contact_messages, _load_processed, _save_processed

_SETTINGS_FILE = config.DATA_DIR / "settings.json"

# 默认监控联系人（若 settings.json 未配置则用此列表）
# 在 Web UI 设置页 → 微信 → 监控联系人 中填写
_DEFAULT_CONTACTS: list[dict] = []


def _get_config() -> tuple[list[dict], int]:
    """从 settings.json 读取监控联系人和轮询间隔"""
    try:
        if _SETTINGS_FILE.exists():
            s = json.loads(_SETTINGS_FILE.read_text("utf-8")).get("wechat", {})
            contacts = s.get("monitor_contacts", _DEFAULT_CONTACTS)
            poll_minutes = s.get("poll_minutes", 5)
            return contacts, max(1, poll_minutes) * 60
    except Exception:
        pass
    return _DEFAULT_CONTACTS, 300

_STATE_FILE = config.DATA_DIR / "wechat_monitor_state.json"
_monitor_thread: threading.Thread | None = None
_running = False


# ── 状态持久化（每人上次检查时间）────────────────────────────────────────────

def _load_state() -> dict[str, float]:
    try:
        if _STATE_FILE.exists():
            return json.loads(_STATE_FILE.read_text("utf-8"))
    except Exception:
        pass
    now = datetime.now().timestamp()
    contacts, _ = _get_config()
    return {c["wxid"]: now for c in contacts}


def _save_state(state: dict[str, float]):
    try:
        _STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")
    except Exception:
        pass


# ── AI 提取重要事项 ───────────────────────────────────────────────────────────

def _extract_focus_items(name: str, relation: str, messages: list[dict]) -> list[FocusItem]:
    """让AI判断消息中是否有值得记录的焦点事项"""
    if not messages:
        return []

    msg_text = "\n".join(
        f"[{datetime.fromtimestamp(m['create_time']).strftime('%H:%M')}] {m['content']}"
        for m in messages
    )

    prompt = f"""以下是{name}（{relation}）在微信发来的消息：

{msg_text}

请从中提取值得记录的重要事项（待办、时间约定、需要跟进的事、重要信息）。
如果没有重要内容（比如只是闲聊问候），直接返回空列表。

以JSON数组格式返回，每条格式：
{{"text": "事项描述", "deadline": "截止日期(可选,格式YYYY-MM-DD)", "priority": "urgent/normal/waiting"}}

只返回JSON，不要其他内容。如果没有重要事项，返回 []"""

    try:
        resp = ai_client.chat(
            [{"role": "user", "content": prompt}],
            system_prompt="你是一个信息提取助手，只提取真正重要的待办和约定，闲聊不算。"
        )
        items_data = json.loads(resp.strip().strip("```json").strip("```").strip())
        if not isinstance(items_data, list):
            return []
        result = []
        for d in items_data:
            if not isinstance(d, dict) or not d.get("text"):
                continue
            result.append(FocusItem(
                text=d["text"],
                deadline=d.get("deadline", ""),
                source="wechat",
                project="",
                db_ref=f"wechat:{name}",
                priority=d.get("priority", "normal"),
            ))
        return result
    except Exception:
        return []


# ── 单次扫描 ──────────────────────────────────────────────────────────────────

def scan_once():
    """扫描一次所有联系人的新消息，提取焦点事项"""
    watch_contacts, _ = _get_config()
    if not watch_contacts:
        return

    state = _load_state()
    processed = _load_processed()
    updated = False

    for contact in watch_contacts:
        wxid = contact["wxid"]
        name = contact["name"]
        relation = contact["relation"]
        since_ts = state.get(wxid, datetime.now().timestamp())

        new_msgs = _read_contact_messages(wxid, since_ts=int(since_ts) + 1)
        fresh = [m for m in new_msgs if m["uid"] not in processed]

        if not fresh:
            continue

        print(f"[微信监控] {name} 有 {len(fresh)} 条新消息")

        # 提取焦点事项
        items = _extract_focus_items(name, relation, fresh)
        for item in items:
            add_focus_item(item)
            print(f"  → 已记录焦点事项: {item.text}")

        # 更新已处理 & 最新时间戳
        for m in fresh:
            processed.add(m["uid"])
        state[wxid] = max(m["create_time"] for m in fresh)
        updated = True

    if updated:
        _save_processed(processed)
        _save_state(state)


# ── 后台循环 ──────────────────────────────────────────────────────────────────

def _loop():
    global _running
    watch_contacts, poll_interval = _get_config()
    print(f"[微信监控] 启动，每 {poll_interval//60} 分钟扫描一次")
    print(f"  监控联系人：{', '.join(c['name'] for c in watch_contacts)}")
    while _running:
        try:
            scan_once()
        except Exception as e:
            print(f"[微信监控] 扫描出错: {e}")
        # 每次重新读轮询间隔，支持运行时修改设置
        _, poll_interval = _get_config()
        for _ in range(poll_interval):
            if not _running:
                break
            time.sleep(1)
    print("[微信监控] 已停止")


def start():
    global _monitor_thread, _running
    if _running:
        return
    _running = True
    _monitor_thread = threading.Thread(target=_loop, daemon=True, name="wechat-monitor")
    _monitor_thread.start()


def stop():
    global _running
    _running = False


# ── 直接运行测试 ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    print("[测试] 立即扫描一次...")
    scan_once()
    print("[测试] 完成")
