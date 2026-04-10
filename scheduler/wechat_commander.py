"""
微信指令通道 — 实时监听 + 指令执行 + 微信直接回复

架构:
  实时模式：监控微信 DB 文件 mtime 变化（5秒轮询），检测到新消息立即解密处理
  备用模式：Scheduler 每2分钟调用一次，双保险

当前监听目标（测试阶段）:
  WATCH_CONTACTS = [{"wxid": "...", "name": "向日葵"}]
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

# 全量监听模式：空列表 = 监听所有联系人
# 如需限制指令权限，在 config.py 中设置 AEGIS_COMMAND_WXIDS（逗号分隔的 wxid）
WATCH_CONTACTS: list[dict] = []

# 用于主动发消息时的 pyautogui 方案（Ctrl+F 搜索联系人）
LISTEN_CONTACT = config.get("AEGIS_DISPLAY_NAME", "") or "文件传输助手"
AEGIS_WXID = config.AEGIS_WXID or ""

# 允许下达 Aegis: 指令的 wxid 白名单（空 = 不限制）
_COMMAND_WHITELIST: set[str] = set(
    w.strip() for w in (config.get("AEGIS_COMMAND_WXIDS", "") or "").split(",") if w.strip()
)

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


def _load_contact_names() -> dict[str, str]:
    """从主库 wechat_contacts 表读取 {wxid: display_name}"""
    try:
        with main_db.get_conn() as conn:
            rows = conn.execute(
                "SELECT wxid, remark, nickname FROM wechat_contacts"
            ).fetchall()
            result = {}
            for r in rows:
                wxid = r[0] or ""
                name = r[1] or r[2] or wxid  # 优先备注名，其次昵称
                if wxid:
                    result[wxid] = name
            return result
    except Exception:
        return {}


def _read_all_new_messages(since_ts: float) -> list[dict]:
    """
    扫描全量微信消息：遍历所有 message_*.db 中的 Name2Id 表，
    读取每个联系人自 since_ts 以来收到的新消息（非本人发送）。
    """
    all_keys, db_dir = _get_keys_and_dbdir()
    if not all_keys:
        return []

    msg_keys = {k: v for k, v in all_keys.items()
                if k.startswith("message\\message_") and not k.endswith("fts.db")}

    results: list[dict] = []
    seen_uids: set[str] = set()

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

            # 读取 Name2Id 获取所有联系人 wxid
            try:
                n2i_rows = conn.execute(
                    "SELECT user_name, rowid FROM Name2Id"
                ).fetchall()
            except Exception:
                continue

            for n2i in n2i_rows:
                talker_wxid = n2i["user_name"] or ""
                if not talker_wxid:
                    continue
                sender_rowid = n2i["rowid"]
                table_name = f"Msg_{hashlib.md5(talker_wxid.encode()).hexdigest()}"
                is_group = "@chatroom" in talker_wxid

                try:
                    rows = conn.execute(f"""
                        SELECT local_id, create_time, real_sender_id,
                               message_content, WCDB_CT_message_content
                        FROM [{table_name}]
                        WHERE local_type = 1
                          AND create_time > ?
                    """, (int(since_ts),)).fetchall()
                except Exception:
                    continue

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

                    content = content.strip()
                    if not content:
                        continue

                    # 判断是否本人发送（本人消息跳过）
                    if is_group:
                        if ":\n" not in content:
                            continue  # 群消息无前缀 = 本人发送，跳过
                        sender_prefix, content = content.split(":\n", 1)
                        content = content.strip()
                        actual_sender_wxid = sender_prefix.strip()
                    else:
                        # 私聊：real_sender_id 为空/0 = 本人发送，跳过
                        if not r["real_sender_id"]:
                            continue
                        actual_sender_wxid = talker_wxid

                    uid = hashlib.md5(
                        f"{rel}_{r['local_id']}_{talker_wxid}".encode()
                    ).hexdigest()
                    if uid in seen_uids:
                        continue
                    seen_uids.add(uid)

                    results.append({
                        "uid": uid,
                        "wxid": actual_sender_wxid,
                        "talker_wxid": talker_wxid,  # 会话 wxid（群聊时与 sender 不同）
                        "content": content,
                        "create_time": r["create_time"],
                        "ts": datetime.fromtimestamp(r["create_time"]).isoformat(),
                        "is_group": is_group,
                    })

        except Exception:
            pass
        finally:
            if conn:
                conn.close()

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
    wxid = msg.get("wxid", "")
    is_group = msg.get("is_group", False)
    if uid in processed:
        return

    processed.add(uid)
    _save_processed(processed)

    # ── 指令处理 ──────────────────────────────────────────────────────────────
    if _is_command(content):
        # 白名单检查（空白名单 = 不限制）
        if _COMMAND_WHITELIST and wxid and wxid not in _COMMAND_WHITELIST:
            print(f"[WxCmd] 拒绝指令（非授权联系人 {contact_name}）: {content[:40]}")
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
            try:
                from notifier import notify_wechat_command
                notify_wechat_command(contact_name, instruction, result)
            except Exception:
                pass
        except Exception as e:
            _reply(f"❌ 执行失败: {e}", contact_name)
        return

    # ── 非指令：重要性评分 + 桌面通知 ────────────────────────────────────────
    try:
        from memory.importance_learner import score_message
        hour = datetime.fromtimestamp(msg.get("create_time", 0)).hour
        score = score_message(
            wxid=wxid,
            content=content,
            is_group=is_group,
            hour=hour,
        )
        if score >= 0.6:
            print(f"[WxCmd] 重要消息（{contact_name} score={score:.2f}）: {content[:60]}")
            try:
                from notifier import notify_wechat_important
                notify_wechat_important(contact_name, content, score)
            except Exception:
                pass
        elif score >= 0.3:
            print(f"[WxCmd] 普通消息（{contact_name} score={score:.2f}）: {content[:40]}")
    except Exception:
        # 无法评分时也记录
        print(f"[WxCmd] 收到消息（{contact_name}）: {content[:60]}")


# ── 公开发送接口 ──────────────────────────────────────────────────────────────

def send_wechat_msg(contact_name: str, msg: str) -> bool:
    return _reply_wechat(msg, who=contact_name)


def send_wechat_to_filehelper(msg: str) -> bool:
    return _reply_wechat(msg, who="文件传输助手")


# ── 实时监听：DB mtime 监控 ───────────────────────────────────────────────────

def start_realtime_listener() -> threading.Thread | None:
    """
    启动实时监听线程：
    - 优先用 watchdog 文件系统事件监听（变更延迟 < 1 秒）
    - watchdog 不可用时降级为 2 秒 mtime 轮询
    - 检测到 Aegis: 前缀指令，执行并微信回复 + 桌面通知
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

        # 共享状态
        _global_since = [(datetime.now() - timedelta(minutes=2)).timestamp()]
        processed = _load_processed()
        changed_flag = threading.Event()
        contact_names = _load_contact_names()  # {wxid: name}

        def _process_changes():
            """检测到 DB 变化后扫描全量新消息"""
            since = _global_since[0]
            try:
                new_msgs = _read_all_new_messages(since_ts=since)
            except Exception as e:
                print(f"[WxCmd] 全量扫描异常: {e}")
                return
            if not new_msgs:
                return
            _global_since[0] = max(m["create_time"] for m in new_msgs)
            for msg in new_msgs:
                wxid = msg["wxid"]
                name = contact_names.get(wxid) or contact_names.get(msg["talker_wxid"]) or wxid
                try:
                    _handle_message(msg, name, processed)
                except Exception as e:
                    print(f"[WxCmd] 消息处理异常({name}): {e}")

        # ── 尝试启动 watchdog 文件系统监听 ────────────────────────────────────
        observer = None
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler

            msg_dir = db_dir / "message"
            if not msg_dir.exists():
                # 尝试在 db_dir 本身监听
                msg_dir = db_dir

            class _DBHandler(FileSystemEventHandler):
                def on_modified(self, event):
                    if not event.is_directory and "message_" in event.src_path:
                        changed_flag.set()

                def on_created(self, event):
                    if not event.is_directory and "message_" in event.src_path:
                        changed_flag.set()

            observer = Observer()
            observer.schedule(_DBHandler(), str(msg_dir), recursive=True)
            observer.start()
            print(f"[WxCmd] ✅ watchdog 文件监听已启动 — 目录: {msg_dir}")
            use_watchdog = True
        except Exception as e:
            print(f"[WxCmd] watchdog 启动失败，降级为轮询: {e}")
            use_watchdog = False

        last_mtimes = _get_db_mtimes(all_keys, db_dir)
        print(f"[WxCmd] ✅ 实时监听已启动 — 模式: 全量联系人"
              f" | {'watchdog' if use_watchdog else '轮询2s'}"
              f"{' | 指令白名单: ' + str(len(_COMMAND_WHITELIST)) + '人' if _COMMAND_WHITELIST else ' | 指令白名单: 无限制'}")

        while _listener_running:
            try:
                if use_watchdog:
                    # watchdog 模式：等待文件变化事件（最多 1 秒超时保底）
                    if changed_flag.wait(timeout=1.0):
                        changed_flag.clear()
                        _process_changes()
                else:
                    # 降级模式：2 秒 mtime 轮询
                    time.sleep(2)
                    cur_mtimes = _get_db_mtimes(all_keys, db_dir)
                    if any(cur_mtimes.get(k) != last_mtimes.get(k) for k in cur_mtimes):
                        last_mtimes = cur_mtimes
                        _process_changes()
            except Exception as e:
                print(f"[WxCmd] 监听循环异常: {e}")
                time.sleep(2)

        if observer:
            observer.stop()
            observer.join()
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
        "mode": "全量联系人",
        "command_whitelist_size": len(_COMMAND_WHITELIST),
    }


# ── DB 轮询备用入口（每2分钟由 scheduler 调用）────────────────────────────────

def _sync_new_wechat_messages():
    """同步全量新消息到 wechat_messages 表"""
    now_str = datetime.now().isoformat()
    since_ts = (datetime.now() - timedelta(minutes=5)).timestamp()
    contact_names = _load_contact_names()

    try:
        new_msgs = _read_all_new_messages(since_ts=since_ts)
    except Exception as e:
        print(f"[WxCmd] 全量同步失败: {e}")
        return

    if not new_msgs:
        return

    saved = 0
    with main_db.get_conn() as conn:
        for m in new_msgs:
            wxid = m["wxid"]
            talker = m["talker_wxid"]
            name = contact_names.get(wxid) or contact_names.get(talker) or wxid
            try:
                conn.execute("""
                    INSERT OR IGNORE INTO wechat_messages
                    (msg_id, chat_id, talker_wxid, talker_name, content,
                     msg_type, is_sender, is_self, create_time, ts, indexed_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """, (m["uid"], talker, wxid, name,
                      m["content"][:2000], 1, 0, 0, m["ts"], m["ts"], now_str))
                saved += 1
            except Exception:
                pass

    if saved:
        print(f"[WxCmd] 同步 {saved} 条新消息（全量联系人）")


def process_wechat_commands():
    """备用轮询：全量同步 + 处理指令"""
    _sync_new_wechat_messages()

    processed = _load_processed()
    since_ts = (datetime.now() - timedelta(minutes=5)).timestamp()
    contact_names = _load_contact_names()

    try:
        new_msgs = _read_all_new_messages(since_ts=since_ts)
    except Exception:
        return

    for m in new_msgs:
        wxid = m["wxid"]
        name = contact_names.get(wxid) or contact_names.get(m["talker_wxid"]) or wxid
        _handle_message(m, name, processed)


# ── 兼容旧调用名 ──────────────────────────────────────────────────────────────
start_wxauto_listener = start_realtime_listener
