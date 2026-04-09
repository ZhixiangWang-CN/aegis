"""
微信指令通道 — 实时监听 + 指令执行 + 微信直接回复

架构:
  实时模式：监控微信 DB 文件 mtime 变化（5秒轮询），检测到新消息立即解密处理
  备用模式：Scheduler 每2分钟调用一次，双保险

当前监听目标（测试阶段）:
  WATCH_CONTACTS = [{"wxid": "wxid_xxx", "name": "联系人备注名"}]
  后续扩展为 all_contacts 模式

用法（联系人在微信发）:
  Aegis: 今天有什么需要处理的？
  Aegis: 搜索 国自然
  Aegis: 状态
  Aegis: 确认 1,3,5
  Aegis: 帮我写一份进展报告
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import config
from memory import db as main_db

# ── 配置 ─────────────────────────────────────────────────────────────────────

COMMAND_PREFIXES = ("aegis:", "jv:", "jarvis:")

# 当前监听的联系人列表（wxid + 备注名）
# 在 Web UI 设置页 → 微信 → 监控联系人 中填写，或直接在 data/settings.json 编辑
WATCH_CONTACTS: list[dict] = []

# 用于主动发消息时的 pyautogui 方案（Ctrl+F 搜索联系人）
LISTEN_CONTACT = config.get("AEGIS_DISPLAY_NAME", "") or "文件传输助手"
AEGIS_WXID = config.AEGIS_WXID or ""

_PROCESSED_FILE = config.DATA_DIR / "wechat_cmd_processed.json"

# 实时监听线程
_listener_thread: threading.Thread | None = None
_listener_running = False


# ── 微信 DB 解密工具 ──────────────────────────────────────────────────────────

def _get_keys_and_dbdir() -> tuple[dict, Path] | tuple[None, None]:
    keys_file = Path(config.BASE_DIR) / "vendor" / "wechat-decrypt" / "all_keys.json"
    if not keys_file.exists():
        return None, None
    try:
        all_keys = json.loads(keys_file.read_text("utf-8"))
        db_dir = Path(all_keys.get("_db_dir", ""))
        return all_keys, db_dir
    except Exception:
        return None, None


def _decrypt_page(enc_key: bytes, page_data: bytes, pgno: int) -> bytes:
    from Crypto.Cipher import AES
    PAGE_SZ, RESERVE_SZ, SALT_SZ, IV_SZ = 4096, 80, 16, 16
    SQLITE_HDR = b"SQLite format 3\x00"
    iv = page_data[PAGE_SZ - RESERVE_SZ: PAGE_SZ - RESERVE_SZ + IV_SZ]
    if pgno == 1:
        dec = AES.new(enc_key, AES.MODE_CBC, iv).decrypt(page_data[SALT_SZ:PAGE_SZ - RESERVE_SZ])
        return bytes(SQLITE_HDR + dec + b"\x00" * RESERVE_SZ)
    dec = AES.new(enc_key, AES.MODE_CBC, iv).decrypt(page_data[:PAGE_SZ - RESERVE_SZ])
    return dec + b"\x00" * RESERVE_SZ


# mtime-based 解密缓存：{src_path_str: (mtime_float, decrypted_path)}
_DECRYPT_CACHE: dict[str, tuple[float, Path]] = {}
_DECRYPT_CACHE_DIR = config.DATA_DIR / "wechat_cmd_cache"


def _decrypt_db_cached(src: Path, enc_key: bytes) -> Path | None:
    """
    解密 message_*.db，使用 mtime 缓存避免重复解密。
    mtime 未变时直接返回上次的解密文件；变了才重新解密。
    """
    _DECRYPT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    src_key = str(src)
    current_mtime = src.stat().st_mtime

    cached_mtime, cached_path = _DECRYPT_CACHE.get(src_key, (0.0, None))
    if cached_mtime == current_mtime and cached_path and cached_path.exists():
        return cached_path  # 缓存命中，直接复用

    # 缓存失效：重新解密
    if cached_path:
        cached_path.unlink(missing_ok=True)

    PAGE_SZ = 4096
    n_pages = (src.stat().st_size + PAGE_SZ - 1) // PAGE_SZ
    out = _DECRYPT_CACHE_DIR / f"cmd_{src.stem}.db"
    try:
        with open(src, "rb") as fin, open(out, "wb") as fout:
            for pgno in range(1, n_pages + 1):
                page = fin.read(PAGE_SZ)
                if not page:
                    break
                if len(page) < PAGE_SZ:
                    page += b"\x00" * (PAGE_SZ - len(page))
                fout.write(_decrypt_page(enc_key, page, pgno))
        _DECRYPT_CACHE[src_key] = (current_mtime, out)
        return out
    except Exception:
        out.unlink(missing_ok=True)
        return None


# 保留旧函数名兼容性（不再删除临时文件，改用缓存版本）
def _decrypt_db_to_tmp(src: Path, enc_key: bytes) -> Path | None:
    return _decrypt_db_cached(src, enc_key)


def _read_contact_messages(wxid: str, since_ts: float = 0) -> list[dict]:
    """
    从所有 message_*.db 中读取指定联系人发来的消息（仅对方发送，非本人）。
    使用 Name2Id 表将 wxid 转换为内部 rowid 后过滤 real_sender_id。
    since_ts: Unix 时间戳，只返回更新的消息。
    """
    all_keys, db_dir = _get_keys_and_dbdir()
    if not all_keys:
        return []

    table_name = f"Msg_{hashlib.md5(wxid.encode()).hexdigest()}"
    msg_keys = {k: v for k, v in all_keys.items()
                if k.startswith("message\\message_") and not k.endswith("fts.db")}

    results = []
    for rel, key_info in msg_keys.items():
        src = db_dir / rel.replace("/", os.sep).replace("\\", os.sep)
        if not src.exists():
            continue
        enc_key = bytes.fromhex(key_info.get("enc_key") or key_info["key"])
        tmp = _decrypt_db_to_tmp(src, enc_key)
        if not tmp:
            continue
        conn = None
        try:
            conn = sqlite3.connect(str(tmp))
            conn.row_factory = sqlite3.Row

            # 用 Name2Id 查联系人的内部 rowid
            sender_rowid = None
            try:
                row = conn.execute(
                    "SELECT rowid FROM Name2Id WHERE user_name=?", (wxid,)
                ).fetchone()
                if row:
                    sender_rowid = row["rowid"]
            except Exception:
                pass

            try:
                if sender_rowid is not None:
                    # 精确过滤：只取对方发来的消息
                    rows = conn.execute(f"""
                        SELECT local_id, create_time, real_sender_id,
                               message_content, WCDB_CT_message_content
                        FROM [{table_name}]
                        WHERE local_type = 1
                          AND create_time > ?
                          AND real_sender_id = ?
                        ORDER BY create_time ASC
                    """, (int(since_ts), sender_rowid)).fetchall()
                else:
                    # sender_rowid 未知，无法区分方向，跳过此 DB
                    rows = []

                for r in rows:
                    content_raw = r["message_content"]
                    ct = r["WCDB_CT_message_content"]
                    if ct == 4 and isinstance(content_raw, bytes):
                        try:
                            import zstandard as zstd
                            content = zstd.ZstdDecompressor().decompress(content_raw).decode("utf-8", "replace")
                        except Exception:
                            content = ""
                    elif isinstance(content_raw, bytes):
                        content = content_raw.decode("utf-8", "replace")
                    else:
                        content = content_raw or ""

                    if not content.strip():
                        continue

                    results.append({
                        "uid": hashlib.md5(f"{rel}_{r['local_id']}_{wxid}".encode()).hexdigest(),
                        "wxid": wxid,
                        "content": content.strip(),
                        "create_time": r["create_time"],
                        "ts": datetime.fromtimestamp(r["create_time"]).isoformat(),
                    })
            except Exception:
                pass  # 此 DB 不含目标表
        except Exception:
            pass
        finally:
            if conn:
                conn.close()
            # 缓存模式下不删除解密文件，由 _decrypt_db_cached 管理生命周期

    results.sort(key=lambda x: x["create_time"])
    return results


def _get_db_mtimes(all_keys: dict, db_dir: Path) -> dict[str, float]:
    """获取所有 message_*.db 的当前 mtime"""
    mtimes = {}
    for rel in all_keys:
        if rel.startswith("message\\message_") and not rel.endswith("fts.db"):
            p = db_dir / rel.replace("/", os.sep).replace("\\", os.sep)
            if p.exists():
                mtimes[rel] = p.stat().st_mtime
    return mtimes


# ── 去重记录 ──────────────────────────────────────────────────────────────────

def _load_processed() -> set:
    try:
        if _PROCESSED_FILE.exists():
            data = json.loads(_PROCESSED_FILE.read_text("utf-8"))
            cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
            return {k for k, v in data.items() if v > cutoff}
    except Exception:
        pass
    return set()


def _save_processed(processed: set):
    try:
        now = datetime.now().isoformat()
        _PROCESSED_FILE.write_text(
            json.dumps({k: now for k in processed}, ensure_ascii=False),
            encoding="utf-8"
        )
    except Exception:
        pass


# ── 指令解析 ──────────────────────────────────────────────────────────────────

def _is_command(content: str) -> bool:
    return any(content.strip().lower().startswith(p) for p in COMMAND_PREFIXES)


def _extract_instruction(content: str) -> str:
    low = content.strip().lower()
    for p in COMMAND_PREFIXES:
        if low.startswith(p):
            return content.strip()[len(p):].strip()
    return content.strip()


def _is_safe(instruction: str) -> bool:
    try:
        from email_module.injection_guard import is_safe
        return is_safe(instruction)
    except Exception:
        return True


def _execute(instruction: str) -> str:
    try:
        from email_module.command_handler import _execute_command
        return _execute_command(instruction, context={"source": "wechat"})
    except Exception as e:
        return f"执行失败: {e}"


# ── 发送消息（pyautogui + wxauto 双保险）──────────────────────────────────────

def _send_via_pyautogui(contact_name: str, msg: str) -> bool:
    try:
        import win32gui, win32con
        import pyautogui, pyperclip
        pyautogui.FAILSAFE = False  # 禁止角落触发 failsafe

        hwnd = win32gui.FindWindow("Qt51514QWindowIcon", "微信")
        if not hwnd:
            return False

        rect = win32gui.GetWindowRect(hwnd)
        x, y, x2, y2 = rect
        w, h = x2 - x, y2 - y

        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        time.sleep(0.4)
        win32gui.SetForegroundWindow(hwnd)
        time.sleep(0.7)

        pyautogui.hotkey("ctrl", "f")
        time.sleep(0.5)
        pyperclip.copy(contact_name)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.9)
        pyautogui.press("enter")
        time.sleep(1.3)

        pyautogui.click(x + int(w * 0.65), y + int(h * 0.88))
        time.sleep(0.4)

        for part in [msg[i:i + 500] for i in range(0, len(msg), 500)]:
            pyperclip.copy(part)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.2)
            pyautogui.press("enter")
            time.sleep(0.3)

        return True
    except Exception as e:
        print(f"[WxCmd] pyautogui 发送失败: {e}")
        return False


def _reply_wechat(msg: str, who: str = None) -> bool:
    """发送微信消息：先试 wxauto，失败用 pyautogui"""
    target = who or LISTEN_CONTACT

    # 方案A: wxauto（微信在前台时可用）
    try:
        from wxauto import WeChat
        wx = WeChat()
        for part in [msg[i:i + 500] for i in range(0, len(msg), 500)]:
            wx.SendMsg(part, who=target)
            time.sleep(0.2)
        print(f"[WxCmd] wxauto 发送成功 → {target}")
        return True
    except Exception:
        pass

    # 方案B: pyautogui（稳定方案）
    ok = _send_via_pyautogui(target, msg)
    if ok:
        print(f"[WxCmd] pyautogui 发送成功 → {target}")
    return ok


def _reply_email(msg: str):
    try:
        from email_module.sender import send_email
        send_email(to=config.NETEASE_EMAIL, subject="✅ Aegis 回复", body=msg)
    except Exception as e:
        print(f"[WxCmd] 邮件降级失败: {e}")


def _reply(msg: str, contact_name: str = None):
    if not _reply_wechat(msg, contact_name):
        _reply_email(msg)


# ── 处理单条消息 ──────────────────────────────────────────────────────────────

def _handle_message(msg: dict, contact_name: str, processed: set):
    uid = msg["uid"]
    content = msg["content"]
    if uid in processed:
        return

    processed.add(uid)
    _save_processed(processed)

    if not _is_command(content):
        # 不是指令 — 不处理（后续可扩展为普通对话 AI 回复）
        print(f"[WxCmd] 收到消息（{contact_name}）: {content[:60]}")
        return

    instruction = _extract_instruction(content)
    print(f"[WxCmd] 执行指令（{contact_name}）: {instruction[:80]}")

    if not _is_safe(instruction):
        _reply("⚠️ 指令被安全策略拒绝", contact_name)
        return

    try:
        result = _execute(instruction)
        _reply(result, contact_name)
        print(f"[WxCmd] 已回复 ({len(result)} 字)")
    except Exception as e:
        _reply(f"❌ 执行失败: {e}", contact_name)


# ── 公开发送接口 ──────────────────────────────────────────────────────────────

def send_wechat_msg(contact_name: str, msg: str) -> bool:
    return _reply_wechat(msg, who=contact_name)


def send_wechat_to_filehelper(msg: str) -> bool:
    return _reply_wechat(msg, who="文件传输助手")


# ── 实时监听：DB mtime 监控 ───────────────────────────────────────────────────

def start_realtime_listener() -> threading.Thread | None:
    """
    启动实时监听线程：
    - 监控微信 message_*.db 文件 mtime，有变化立即解密处理
    - 只处理 WATCH_CONTACTS 中的联系人
    - 检测到 Aegis: 前缀指令，执行并微信回复
    """
    global _listener_thread, _listener_running

    if _listener_running and _listener_thread and _listener_thread.is_alive():
        return _listener_thread

    def _loop():
        global _listener_running

        all_keys, db_dir = _get_keys_and_dbdir()
        if not all_keys:
            print("[WxCmd] 未找到微信密钥，实时监听无法启动")
            _listener_running = False
            return

        # 初始化 mtime 快照 & 处理时间戳
        last_mtimes = _get_db_mtimes(all_keys, db_dir)
        # 从 2 分钟前开始，避免处理历史旧消息
        last_ts: dict[str, float] = {
            c["wxid"]: (datetime.now() - timedelta(minutes=2)).timestamp()
            for c in WATCH_CONTACTS
        }
        processed = _load_processed()

        watch_names = {c["wxid"]: c["name"] for c in WATCH_CONTACTS}
        print(f"[WxCmd] ✅ 实时监听已启动 — 监控联系人: {[c['name'] for c in WATCH_CONTACTS]}")

        while _listener_running:
            try:
                cur_mtimes = _get_db_mtimes(all_keys, db_dir)
                changed = any(cur_mtimes.get(k) != last_mtimes.get(k) for k in cur_mtimes)

                if changed:
                    last_mtimes = cur_mtimes
                    for contact in WATCH_CONTACTS:
                        wxid = contact["wxid"]
                        name = contact["name"]
                        since = last_ts.get(wxid, 0)

                        new_msgs = _read_contact_messages(wxid, since_ts=since)
                        if new_msgs:
                            # 更新时间戳到最新消息
                            last_ts[wxid] = max(m["create_time"] for m in new_msgs)
                            for msg in new_msgs:
                                _handle_message(msg, name, processed)

            except Exception as e:
                print(f"[WxCmd] 监听循环异常: {e}")

            time.sleep(5)

        _listener_running = False
        print("[WxCmd] 实时监听已停止")

    _listener_running = True
    _listener_thread = threading.Thread(target=_loop, daemon=True, name="wechat-listener")
    _listener_thread.start()
    return _listener_thread


def stop_realtime_listener():
    global _listener_running
    _listener_running = False


def listener_status() -> dict:
    return {
        "running": _listener_running and _listener_thread is not None and _listener_thread.is_alive(),
        "watch_contacts": [c["name"] for c in WATCH_CONTACTS],
    }


# ── DB 轮询备用入口（每2分钟由 scheduler 调用）────────────────────────────────

def _sync_new_wechat_messages():
    """同步主要 WATCH_CONTACTS 的新消息到 wechat_messages 表"""
    all_keys, db_dir = _get_keys_and_dbdir()
    if not all_keys:
        return

    now_str = datetime.now().isoformat()
    since_ts = (datetime.now() - timedelta(minutes=5)).timestamp()

    for contact in WATCH_CONTACTS:
        wxid = contact["wxid"]
        name = contact["name"]
        new_msgs = _read_contact_messages(wxid, since_ts=since_ts)

        if not new_msgs:
            continue

        with main_db.get_conn() as conn:
            for m in new_msgs:
                try:
                    cur = conn.execute("""
                        INSERT OR IGNORE INTO wechat_messages
                        (msg_id, chat_id, talker_wxid, talker_name, content,
                         msg_type, is_sender, is_self, create_time, ts, indexed_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    """, (m["uid"], wxid, wxid, name,
                          m["content"][:2000], 1, 0, 0, m["ts"], m["ts"], now_str))
                except Exception:
                    pass

        print(f"[WxCmd] 同步 {len(new_msgs)} 条新消息（{name}）")


def process_wechat_commands():
    """备用轮询：同步消息 + 处理指令"""
    _sync_new_wechat_messages()

    processed = _load_processed()
    since = (datetime.now() - timedelta(minutes=5)).isoformat()

    for contact in WATCH_CONTACTS:
        wxid = contact["wxid"]
        name = contact["name"]
        since_ts = (datetime.now() - timedelta(minutes=5)).timestamp()

        new_msgs = _read_contact_messages(wxid, since_ts=since_ts)
        cmd_msgs = [m for m in new_msgs
                    if m["uid"] not in processed and _is_command(m["content"])]
        for m in cmd_msgs:
            _handle_message(m, name, processed)


# ── 兼容旧调用名 ──────────────────────────────────────────────────────────────
start_wxauto_listener = start_realtime_listener
