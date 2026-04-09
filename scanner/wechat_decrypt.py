"""
微信聊天记录解密与知识库构建 — 支持微信 3.x (pywxdump) 和 4.x (ylytdeng/wechat-decrypt)

微信 4.x 变化:
  - 进程名: WeChat.exe → Weixin.exe
  - 加密: SQLCipher 3 → SQLCipher 4 (AES-256-CBC + HMAC-SHA512)
  - 数据库路径: %APPDATA%/Tencent/xwechat/

两阶段流程:
  阶段一（需管理员）: 从进程内存提取密钥 → all_keys.json
    python main.py --wechat
  阶段二（普通权限）: 读取 all_keys.json → 解密 → 导入
    python main.py --wechat --wx-key=<已有keys文件路径>
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import struct
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

import config
from memory import db, vector_store
from email_module.reader import _safe_print

# 解密后数据库缓存目录
DECRYPT_DIR = config.DATA_DIR / "wechat_decrypted"
# vendor 工具路径
VENDOR_DIR = Path(__file__).parent.parent / "vendor" / "wechat-decrypt"

# SQLCipher 4 参数
PAGE_SZ    = 4096
RESERVE_SZ = 80
SALT_SZ    = 16
IV_SZ      = 16
HMAC_SZ    = 64
KEY_SZ     = 32
SQLITE_HDR = b'SQLite format 3\x00'


def _ensure_wechat_tables():
    """确保微信相关表已创建（wechat_messages, wechat_contacts, wechat_groups 定义在 db.py）"""
    db.init_db()


# ── SQLCipher 4 解密（内联，不依赖 vendor config.py）──────────────────────

def _derive_mac_key(enc_key: bytes, salt: bytes) -> bytes:
    mac_salt = bytes(b ^ 0x3A for b in salt)
    return hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=KEY_SZ)


def _decrypt_page(enc_key: bytes, page_data: bytes, pgno: int) -> bytes:
    try:
        from Crypto.Cipher import AES
    except ImportError:
        raise RuntimeError("请安装 pycryptodome: pip install pycryptodome")

    iv = page_data[PAGE_SZ - RESERVE_SZ : PAGE_SZ - RESERVE_SZ + IV_SZ]
    if pgno == 1:
        encrypted = page_data[SALT_SZ : PAGE_SZ - RESERVE_SZ]
        decrypted = AES.new(enc_key, AES.MODE_CBC, iv).decrypt(encrypted)
        page = bytearray(SQLITE_HDR + decrypted + b'\x00' * RESERVE_SZ)
        return bytes(page)
    else:
        encrypted = page_data[:PAGE_SZ - RESERVE_SZ]
        decrypted = AES.new(enc_key, AES.MODE_CBC, iv).decrypt(encrypted)
        return decrypted + b'\x00' * RESERVE_SZ


def decrypt_wx4_db(db_path: Path, enc_key_hex: str) -> Optional[Path]:
    """
    解密单个微信 4.x SQLCipher 数据库，输出到 DECRYPT_DIR。
    返回解密后文件路径，失败返回 None。
    """
    DECRYPT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DECRYPT_DIR / db_path.name

    enc_key = bytes.fromhex(enc_key_hex)
    file_size = db_path.stat().st_size
    total_pages = (file_size + PAGE_SZ - 1) // PAGE_SZ

    try:
        with open(db_path, 'rb') as fin, open(out_path, 'wb') as fout:
            for pgno in range(1, total_pages + 1):
                page = fin.read(PAGE_SZ)
                if not page:
                    break
                if len(page) < PAGE_SZ:
                    page = page + b'\x00' * (PAGE_SZ - len(page))
                fout.write(_decrypt_page(enc_key, page, pgno))
        return out_path
    except Exception as e:
        _safe_print(f"[WeChat4] 解密失败 {db_path.name}: {e}")
        return None


# ── 阶段一：提取密钥（需管理员权限）──────────────────────────────────────

def _load_vendor_module(name: str):
    """用 importlib 按完整路径加载 vendor 模块，避免与 Jarvis 模块同名冲突"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        f"_vendor_{name}", VENDOR_DIR / f"{name}.py"
    )
    mod = importlib.util.module_from_spec(spec)
    # 让 vendor 模块之间互相 import 能找到对方
    if str(VENDOR_DIR) not in sys.path:
        sys.path.insert(0, str(VENDOR_DIR))
    spec.loader.exec_module(mod)
    return mod


def _detect_wx4_db_dir() -> Optional[str]:
    """从微信 ini 文件自动检测 db_storage 目录（不依赖 vendor config.py）"""
    import glob
    appdata = os.environ.get("APPDATA", "")
    config_dir = os.path.join(appdata, "Tencent", "xwechat", "config")
    if not os.path.isdir(config_dir):
        return None
    data_roots = []
    for ini_file in glob.glob(os.path.join(config_dir, "*.ini")):
        for enc in ("utf-8", "gbk"):
            try:
                content = Path(ini_file).read_text(encoding=enc).strip()
                if os.path.isdir(content):
                    data_roots.append(content)
                break
            except Exception:
                continue
    for root in data_roots:
        for match in glob.glob(os.path.join(root, "xwechat_files", "*", "db_storage")):
            if os.path.isdir(match):
                return match
    return None


def extract_keys_wx4() -> Optional[dict]:
    """
    从 Weixin.exe 进程内存提取数据库加密密钥。
    需要管理员权限，返回 {rel_path: {key, salt}, ...} 或 None。
    """
    _safe_print("[WeChat4] 尝试从 Weixin.exe 提取密钥...")

    try:
        # 检查进程
        import subprocess, ctypes
        r = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True
        )
        if "Weixin.exe" not in r.stdout:
            _safe_print("[WeChat4] ❌ Weixin.exe 未运行，请确认微信已登录")
            return None

        # 检查管理员权限
        if not ctypes.windll.shell32.IsUserAnAdmin():
            _safe_print("[WeChat4] ❌ 需要管理员权限才能读取进程内存")
            return None

        # 检测数据库目录
        db_dir = _detect_wx4_db_dir()
        if not db_dir or not Path(db_dir).exists():
            _safe_print(f"[WeChat4] ❌ 未找到微信数据库目录")
            _safe_print("          请在 vendor/wechat-decrypt/config.json 中手动设置 db_dir")
            return None
        _safe_print(f"[WeChat4] 数据库目录: {db_dir}")

        # 加载 vendor 模块（避免 config 命名冲突）
        if str(VENDOR_DIR) not in sys.path:
            sys.path.insert(0, str(VENDOR_DIR))

        from key_scan_common import (
            collect_db_files, scan_memory_for_keys,
            cross_verify_keys, save_results,
        )
        from find_all_keys_windows import get_pids, enum_regions, read_mem

        db_files, salt_to_dbs = collect_db_files(db_dir)
        if not db_files:
            _safe_print("[WeChat4] ❌ 未找到加密数据库文件")
            return None
        _safe_print(f"[WeChat4] 找到 {len(db_files)} 个数据库文件")

        import re
        hex_re = re.compile(rb"x'([0-9a-fA-F]{64,192})'")
        key_map: dict = {}
        remaining_salts = set(salt_to_dbs.keys())

        for pid, mem_kb in get_pids():
            if not remaining_salts:
                break
            h = ctypes.windll.kernel32.OpenProcess(0x0410, False, pid)
            if not h:
                continue
            try:
                for base, size in enum_regions(h):
                    if not remaining_salts or size > 50 * 1024 * 1024:
                        continue
                    data = read_mem(h, base, size)
                    if not data:
                        continue
                    scan_memory_for_keys(
                        data, hex_re, db_files, salt_to_dbs,
                        key_map, remaining_salts, base, pid, _safe_print
                    )
            finally:
                ctypes.windll.kernel32.CloseHandle(h)

        cross_verify_keys(db_files, salt_to_dbs, key_map, _safe_print)

        if not key_map:
            _safe_print("[WeChat4] ❌ 未提取到密钥（可能是微信版本 4.1.7.56+ 暂不支持）")
            return None

        keys_file = VENDOR_DIR / "all_keys.json"
        save_results(db_files, salt_to_dbs, key_map, db_dir,
                     str(keys_file), _safe_print)
        _safe_print(f"[WeChat4] ✅ 密钥已保存: {keys_file}")

        with open(keys_file, encoding="utf-8") as f:
            keys = json.load(f)
        return {k: v for k, v in keys.items() if not k.startswith("_")}

    except RuntimeError as e:
        _safe_print(f"[WeChat4] {e}")
        return None
    except Exception as e:
        _safe_print(f"[WeChat4] 密钥提取失败: {e}")
        import traceback; traceback.print_exc()
        return None


# ── 阶段二：解密并导入消息 ────────────────────────────────────────────────

def _find_wx4_db_dir() -> Optional[Path]:
    """自动查找微信 4.x db_storage 目录"""
    appdata = os.environ.get("APPDATA", "")
    import glob
    patterns = [
        os.path.join(appdata, "Tencent", "xwechat", "config", "*.ini"),
    ]
    for pat in patterns:
        for ini_file in glob.glob(pat):
            try:
                for enc in ("utf-8", "gbk"):
                    try:
                        content = Path(ini_file).read_text(encoding=enc).strip()
                        if os.path.isdir(content):
                            # 在该目录下找 db_storage
                            for match in glob.glob(
                                os.path.join(content, "xwechat_files", "*", "db_storage")
                            ):
                                if os.path.isdir(match):
                                    return Path(match)
                        break
                    except UnicodeDecodeError:
                        continue
            except Exception:
                continue
    return None


def _import_wx4_contacts(contact_db: Path):
    """从解密后的 contact.db 导入联系人"""
    if not contact_db.exists():
        return 0
    count = 0
    try:
        conn_wx = sqlite3.connect(str(contact_db))
        conn_wx.row_factory = sqlite3.Row
        rows = conn_wx.execute(
            "SELECT username, nick_name, remark FROM contact "
            "WHERE username NOT LIKE '%@chatroom' AND username != 'filehelper'"
        ).fetchall()
        conn_wx.close()

        now = datetime.now().isoformat()
        with db.get_conn() as conn_j:
            for r in rows:
                wxid = r["username"] or ""
                nick = r["nick_name"] or ""
                remark = r["remark"] or ""
                name = remark if remark else nick if nick else wxid
                if not wxid or not name:
                    continue
                try:
                    conn_j.execute("""
                        INSERT OR IGNORE INTO wechat_contacts
                        (wxid, nickname, remark, updated_at)
                        VALUES (?, ?, ?, ?)
                    """, (wxid, nick, remark, now))
                    count += 1
                except Exception:
                    continue
        _safe_print(f"[WeChat4] 联系人已导入: {count} 个")
    except Exception as e:
        _safe_print(f"[WeChat4] 联系人导入失败: {e}")
    return count


def _decompress_content(content, ct) -> str:
    """解压 zstd 压缩的消息内容"""
    if ct == 4 and isinstance(content, bytes):
        try:
            import zstandard as zstd
            return zstd.ZstdDecompressor().decompress(content).decode("utf-8", errors="replace")
        except Exception:
            pass
    if isinstance(content, bytes):
        return content.decode("utf-8", errors="replace")
    return str(content) if content else ""


def _import_wx4_messages(msg_db: Path, contact_names: dict) -> int:
    """从解密后的单个 message_*.db 导入消息"""
    import hashlib as _hl2
    if not msg_db.exists():
        return 0
    saved = 0
    to_vectorize = []

    try:
        conn_wx = sqlite3.connect(str(msg_db))
        conn_wx.row_factory = sqlite3.Row

        # Name2Id: user_name → Msg_{md5(user_name)} 表
        try:
            name2id_rows = conn_wx.execute(
                "SELECT user_name FROM Name2Id"
            ).fetchall()
        except Exception:
            conn_wx.close()
            return 0

        now = datetime.now().isoformat()

        with db.get_conn() as conn_j:
            for n2i in name2id_rows:
                talker_wxid = n2i["user_name"] or ""
                if not talker_wxid:
                    continue
                talker_name = contact_names.get(talker_wxid, talker_wxid)
                # WeChat 4.x: table name = Msg_{md5(user_name)}
                table_name  = f"Msg_{_hl2.md5(talker_wxid.encode()).hexdigest()}"

                try:
                    msgs = conn_wx.execute(f"""
                        SELECT local_id, local_type, create_time,
                               real_sender_id, message_content,
                               WCDB_CT_message_content
                        FROM [{table_name}]
                        WHERE local_type = 1
                        ORDER BY create_time ASC
                    """).fetchall()
                except Exception:
                    continue

                for m in msgs:
                    content_raw = m["message_content"]
                    ct          = m["WCDB_CT_message_content"]
                    content     = _decompress_content(content_raw, ct)

                    if not content or len(content.strip()) < 5:
                        continue

                    # 判断是否本人发送
                    # 私聊：real_sender_id 为空 = 本人发送；有值 = 对方发送
                    # 群聊：消息格式 "wxid_xxx:\n消息内容"，无前缀 = 本人发送
                    is_group = "@chatroom" in talker_wxid
                    if is_group and ":\n" in content:
                        sender_id, content = content.split(":\n", 1)
                        is_self = 0  # 有发送者前缀 → 他人发送
                    elif is_group:
                        is_self = 1  # 群消息无前缀 → 本人发送
                    else:
                        # 私聊：real_sender_id 为空 → 本人发送
                        sender_id = m["real_sender_id"] or ""
                        is_self   = 1 if not sender_id else 0

                    ct_iso = ""
                    ts_raw = m["create_time"]
                    if isinstance(ts_raw, (int, float)) and ts_raw > 1_000_000_000:
                        ct_iso = datetime.fromtimestamp(ts_raw).isoformat()
                    elif isinstance(ts_raw, str):
                        ct_iso = ts_raw

                    msg_uid = f"wx4_{msg_db.stem}_{m['local_id']}_{talker_wxid}"
                    import hashlib as _hl
                    uid = _hl.md5(msg_uid.encode()).hexdigest()

                    try:
                        conn_j.execute("""
                            INSERT INTO wechat_messages
                            (msg_id, chat_id, talker_wxid, talker_name, content,
                             msg_type, is_sender, is_self, create_time, ts, indexed_at)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(msg_id) DO UPDATE SET
                                is_sender = excluded.is_sender,
                                is_self   = excluded.is_self
                        """, (uid, talker_wxid, talker_wxid, talker_name,
                              content[:2000], m["local_type"],
                              is_self, is_self, ct_iso, ct_iso, now))
                        saved += 1

                        if len(content.strip()) > 20:
                            to_vectorize.append({
                                "id": uid,
                                "talker": talker_name,
                                "content": content[:1000],
                                "time": ct_iso,
                            })
                    except Exception:
                        continue

        conn_wx.close()
    except Exception as e:
        _safe_print(f"[WeChat4] 消息导入失败 {msg_db.name}: {e}")

    if to_vectorize:
        _vectorize_messages(to_vectorize)

    return saved


def _vectorize_messages(msgs: list[dict]):
    """按联系人聚合后向量化"""
    from collections import defaultdict
    import hashlib as _hl

    by_talker = defaultdict(list)
    for m in msgs:
        by_talker[m["talker"]].append(m)

    vectorized = 0
    for talker, talker_msgs in by_talker.items():
        for i in range(0, len(talker_msgs), 100):
            chunk = talker_msgs[i:i + 100]
            texts = [f"[{m['time'][:10]}] {m['content']}" for m in chunk]
            combined = f"微信对话 — {talker}:\n" + "\n".join(texts)
            chunk_id = _hl.md5(f"wechat4:{talker}:{i}".encode()).hexdigest()
            try:
                vector_store.add_document(
                    collection_name="wechat",
                    doc_id=chunk_id,
                    text=combined[:3000],
                    metadata={"talker": talker, "source": "wechat4"},
                )
                vectorized += 1
            except Exception:
                continue

    if vectorized:
        _safe_print(f"[WeChat4] 向量化完成: {vectorized} 个对话块")


def import_wx4_from_keys(keys: dict, db_dir: Path) -> dict:
    """
    用已有密钥解密并导入所有数据库。
    keys: {rel_path: {key: hex, salt: hex}, ...}
    """
    stats = {"contacts": 0, "messages": 0, "dbs_decrypted": 0}
    DECRYPT_DIR.mkdir(parents=True, exist_ok=True)

    _safe_print(f"[WeChat4] 开始解密 {len(keys)} 个数据库...")

    # 先解密 contact.db
    contact_db_decrypted = None
    for rel_path, key_info in keys.items():
        if "contact.db" in rel_path.lower():
            src = db_dir / rel_path.replace("/", os.sep).replace("\\", os.sep)
            if src.exists():
                out = DECRYPT_DIR / f"contact_{src.name}"
                enc_key = bytes.fromhex(key_info.get("enc_key") or key_info["key"])
                n_pages = src.stat().st_size // PAGE_SZ
                try:
                    with open(src, 'rb') as fin, open(out, 'wb') as fout:
                        for pgno in range(1, n_pages + 1):
                            page = fin.read(PAGE_SZ)
                            if not page:
                                break
                            if len(page) < PAGE_SZ:
                                page += b'\x00' * (PAGE_SZ - len(page))
                            fout.write(_decrypt_page(enc_key, page, pgno))
                    contact_db_decrypted = out
                    stats["dbs_decrypted"] += 1
                except Exception as e:
                    _safe_print(f"[WeChat4] 解密 contact.db 失败: {e}")

    # 加载联系人名称映射
    contact_names = {}
    if contact_db_decrypted:
        stats["contacts"] = _import_wx4_contacts(contact_db_decrypted)
        try:
            conn_c = sqlite3.connect(str(contact_db_decrypted))
            conn_c.row_factory = sqlite3.Row
            for r in conn_c.execute("SELECT username, nick_name, remark FROM contact"):
                name = r["remark"] or r["nick_name"] or r["username"]
                contact_names[r["username"]] = name
            conn_c.close()
        except Exception:
            pass

    # 解密并导入消息数据库
    for rel_path, key_info in keys.items():
        if "message" not in rel_path.lower():
            continue
        src = db_dir / rel_path.replace("/", os.sep).replace("\\", os.sep)
        if not src.exists():
            continue

        out = DECRYPT_DIR / f"msg_{src.name}"
        enc_key = bytes.fromhex(key_info.get("enc_key") or key_info["key"])
        n_pages = (src.stat().st_size + PAGE_SZ - 1) // PAGE_SZ

        try:
            with open(src, 'rb') as fin, open(out, 'wb') as fout:
                for pgno in range(1, n_pages + 1):
                    page = fin.read(PAGE_SZ)
                    if not page:
                        break
                    if len(page) < PAGE_SZ:
                        page += b'\x00' * (PAGE_SZ - len(page))
                    fout.write(_decrypt_page(enc_key, page, pgno))
            stats["dbs_decrypted"] += 1
            n = _import_wx4_messages(out, contact_names)
            stats["messages"] += n
        except Exception as e:
            _safe_print(f"[WeChat4] 处理 {rel_path} 失败: {e}")

    _safe_print(f"[WeChat4] 导入完成: {stats}")
    return stats


# ── 微信 3.x 兼容（pywxdump）─────────────────────────────────────────────

def _try_wx3(manual_key: Optional[str] = None) -> bool:
    """尝试用 pywxdump 处理微信 3.x"""
    try:
        from pywxdump import get_wx_info, batch_decrypt, DBHandler
    except ImportError:
        return False

    key = manual_key
    if not key:
        infos = get_wx_info()
        if not infos:
            return False
        for info in (infos if isinstance(infos, list) else [infos]):
            k = info.get("key", "")
            if k and len(k) == 64:
                key = k
                break

    if not key:
        return False

    _safe_print("[WeChat3] 检测到微信 3.x 密钥，使用 pywxdump 处理...")
    # ... (保留原有 3.x 处理逻辑)
    return True


# ── 主入口 ────────────────────────────────────────────────────────────────

def process_wechat(manual_key: Optional[str] = None):
    """
    微信聊天记录导入主入口。
    自动检测微信版本（3.x / 4.x）并选择对应解密方案。
    """
    _safe_print("[WeChat] === 开始导入微信聊天记录 ===")

    # 检查已有 all_keys.json（之前提取过）
    keys_file = VENDOR_DIR / "all_keys.json"
    if keys_file.exists() and not manual_key:
        _safe_print(f"[WeChat4] 发现已有密钥文件: {keys_file}")
        try:
            with open(keys_file, encoding="utf-8") as f:
                data = json.load(f)
            keys = {k: v for k, v in data.items() if not k.startswith("_")}
            db_dir_str = data.get("_info", {}).get("wechat_dir") or _detect_wx4_db_dir()
            if keys and db_dir_str:
                import_wx4_from_keys(keys, Path(db_dir_str))
                _post_import()
                return
        except Exception as e:
            _safe_print(f"[WeChat4] 读取密钥文件失败: {e}")

    # 先尝试提取微信 4.x 密钥
    keys = extract_keys_wx4()
    if keys:
        db_dir_str = _detect_wx4_db_dir()
        if db_dir_str:
            import_wx4_from_keys(keys, Path(db_dir_str))
            _post_import()
            return

    # 回退到微信 3.x (pywxdump)
    if _try_wx3(manual_key):
        _post_import()
        return

    _safe_print("[WeChat] ❌ 无法提取微信密钥")
    _safe_print("          可能原因:")
    _safe_print("          1. 微信 4.x: 需要管理员权限 (以管理员身份运行终端)")
    _safe_print("          2. 微信版本 4.1.7.56+: 暂无开源工具支持")
    _safe_print("          3. 微信未登录")


def _post_import():
    """导入后续处理：更新统计 + 角色推断"""
    try:
        with db.get_conn() as conn:
            conn.execute("""
                UPDATE wechat_contacts
                SET msg_count = (
                    SELECT COUNT(*) FROM wechat_messages
                    WHERE talker_wxid = wechat_contacts.wxid
                ),
                last_msg_at = (
                    SELECT MAX(create_time) FROM wechat_messages
                    WHERE talker_wxid = wechat_contacts.wxid
                )
            """)
        _safe_print("[WeChat] 消息计数已更新")
    except Exception as e:
        _safe_print(f"[WeChat] 统计更新失败: {e}")

    try:
        from scanner.wechat_roles import check_new_contacts
        check_new_contacts(max_batch=30)
    except Exception as e:
        _safe_print(f"[WeChat] 角色推断跳过: {e}")


# ── 增量同步 ──────────────────────────────────────────────────────────────

def sync_wechat_incremental() -> dict:
    """
    增量同步微信消息：只解密 mtime 有变化的 message_*.db 文件。
    依赖已有的 all_keys.json（不重新提取密钥，无需管理员权限）。
    """
    STATE_FILE = config.DATA_DIR / "wechat_sync_state.json"
    stats = {"checked": 0, "synced": 0, "messages": 0}

    # 加载密钥文件
    keys_file = VENDOR_DIR / "all_keys.json"
    if not keys_file.exists():
        _safe_print("[WeChat增量] 未找到 all_keys.json，请先以管理员身份运行完整同步")
        return stats

    try:
        with open(keys_file, encoding="utf-8") as f:
            data = json.load(f)
        keys = {k: v for k, v in data.items() if not k.startswith("_")}
        db_dir_str = data.get("_info", {}).get("wechat_dir") or _detect_wx4_db_dir()
        if not db_dir_str:
            _safe_print("[WeChat增量] 未找到微信数据库目录")
            return stats
        db_dir = Path(db_dir_str)
    except Exception as e:
        _safe_print(f"[WeChat增量] 读取密钥失败: {e}")
        return stats

    # 加载上次同步状态 {rel_path: mtime_float}
    sync_state: dict = {}
    if STATE_FILE.exists():
        try:
            sync_state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass

    # 从系统 DB 加载联系人名称映射
    contact_names: dict = {}
    try:
        with db.get_conn() as conn:
            for r in conn.execute("SELECT wxid, remark, nickname FROM wechat_contacts"):
                name = r["remark"] or r["nickname"] or r["wxid"]
                contact_names[r["wxid"]] = name
    except Exception:
        pass

    DECRYPT_DIR.mkdir(parents=True, exist_ok=True)
    changed = False
    _SQLITE_HDR = b"SQLite format 3\x00"

    for rel_path, key_info in keys.items():
        if "message" not in rel_path.lower():
            continue

        src = db_dir / rel_path.replace("/", os.sep).replace("\\", os.sep)
        if not src.exists():
            continue

        stats["checked"] += 1
        current_mtime = src.stat().st_mtime
        if current_mtime <= sync_state.get(rel_path, 0):
            continue  # 文件未变化，跳过

        # 解密
        out = DECRYPT_DIR / f"msg_{src.name}"
        enc_key = bytes.fromhex(key_info.get("enc_key") or key_info["key"])
        n_pages = (src.stat().st_size + PAGE_SZ - 1) // PAGE_SZ

        try:
            with open(src, "rb") as fin, open(out, "wb") as fout:
                for pgno in range(1, n_pages + 1):
                    page = fin.read(PAGE_SZ)
                    if not page:
                        break
                    if len(page) < PAGE_SZ:
                        page += b"\x00" * (PAGE_SZ - len(page))
                    fout.write(_decrypt_page(enc_key, page, pgno))

            # 校验解密结果是合法 SQLite（密钥失效时会产生乱码）
            with open(out, "rb") as f:
                header = f.read(16)
            if header != _SQLITE_HDR:
                _safe_print(
                    f"[WeChat增量] ⚠️  {src.name} 解密失败（非法 SQLite header）"
                    f"——密钥可能已失效，请以管理员身份重新运行 python main.py --wechat"
                )
                out.unlink(missing_ok=True)
                continue

            n = _import_wx4_messages(out, contact_names)
            stats["messages"] += n
            stats["synced"] += 1
            sync_state[rel_path] = current_mtime
            changed = True
            _safe_print(f"[WeChat增量] {src.name}: +{n} 条消息")
        except Exception as e:
            _safe_print(f"[WeChat增量] 处理 {rel_path} 失败: {e}")

    # 持久化状态
    if changed:
        try:
            STATE_FILE.write_text(
                json.dumps(sync_state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass
        _post_import()

    _safe_print(
        f"[WeChat增量] 完成: 检查 {stats['checked']} 个DB，"
        f"同步 {stats['synced']} 个，新增 {stats['messages']} 条消息"
    )
    return stats
