"""
运行时设置管理（覆盖 config.py 中的默认值）

设置存储在 data/settings.json，通过 Web UI 修改。
启动时自动加载，无需重启即可生效（凭证类修改会同步写回 .credentials）。

注意：此文件故意放在项目根目录而非 config/ 子目录，
     因为 config.py 存在时 Python 无法 import config.settings。
"""
from __future__ import annotations
import json
from pathlib import Path
import config as _cfg

_SETTINGS_FILE = _cfg.DATA_DIR / "settings.json"
_CRED_FILE = Path(__file__).parent / ".credentials"

# 默认设置（完整结构，用于前端渲染）
_DEFAULTS: dict = {
    # ── 用户身份 ─────────────────────────────────────────────────────────────
    "user": {
        "owner_name":       _cfg.OWNER_NAME or "用户",
        "owner_full_name":  _cfg.OWNER_FULL_NAME or "",
        "owner_en_name":    _cfg.OWNER_EN_NAME or "",
        "aegis_wxid":       _cfg.AEGIS_WXID or "",    # Aegis监听的微信号 wxid
    },

    # ── AI / 大模型 API ───────────────────────────────────────────────────────
    "api": {
        "volc_api_key":  "",                          # 敏感：前端不回传已有值
        "volc_api_base": _cfg.VOLC_API_BASE or "https://ark.cn-beijing.volces.com/api/coding/v3",
        "volc_model":    _cfg.VOLC_MODEL or "ep-20250223173613-jq9ml",
    },

    # ── 163 邮箱 ──────────────────────────────────────────────────────────────
    "email_163": {
        "email":       _cfg.NETEASE_EMAIL or "",
        "auth_code":   "",                            # 敏感：前端不回传已有值
        "imap_host":   _cfg.NETEASE_IMAP_HOST or "imap.163.com",
        "imap_port":   int(_cfg.NETEASE_IMAP_PORT or 993),
        "smtp_host":   _cfg.NETEASE_SMTP_HOST or "smtp.163.com",
        "smtp_port":   int(_cfg.NETEASE_SMTP_PORT or 465),
        "enabled":     _cfg.MODULES.get("email_163", True),
    },

    # ── Gmail ─────────────────────────────────────────────────────────────────
    "email_gmail": {
        "email":         getattr(_cfg, "GMAIL_EMAIL", "") or "",
        "app_password":  "",                          # 敏感：前端不回传已有值
        "imap_host":     getattr(_cfg, "GMAIL_IMAP_HOST", "imap.gmail.com") or "imap.gmail.com",
        "imap_port":     int(getattr(_cfg, "GMAIL_IMAP_PORT", 993) or 993),
        "smtp_host":     getattr(_cfg, "GMAIL_SMTP_HOST", "smtp.gmail.com") or "smtp.gmail.com",
        "smtp_port":     int(getattr(_cfg, "GMAIL_SMTP_PORT", 587) or 587),
        "enabled":       _cfg.MODULES.get("email_gmail", False),
    },

    # ── 扫描目录 ─────────────────────────────────────────────────────────────
    "scan": {
        "roots":       list(_cfg.SCAN_ROOTS),
        "skip_dirs":   list(_cfg.SKIP_DIRS),
        "extensions":  list(_cfg.SUPPORTED_EXTENSIONS),
        "max_file_mb": _cfg.MAX_FILE_SIZE_MB,
    },

    # ── 定时任务 ─────────────────────────────────────────────────────────────
    "schedule": {
        "email_check_minutes":    30,
        "wechat_sync_minutes":    15,
        "wechat_contacts_minutes":30,
        "briefing_hour":          8,
        "briefing_minute":        0,
        "rss_hour":               7,
        "focus_update_hour":      19,
        "profile_update_hour":    3,
        "weekly_report_day":      "sun",
        "weekly_report_hour":     9,
        "aging_check_day":        "sun",
        "aging_check_hour":       3,
    },

    # ── 通知 ──────────────────────────────────────────────────────────────────
    "notify": {
        "urgent_importance":      4,
        "push_email":             _cfg.NETEASE_EMAIL or "",
        "briefing_enabled":       True,
        "weekly_report_enabled":  True,
        "alert_on_sync_failure":  True,
        "sync_failure_threshold": 3,
    },

    # ── 微信 ──────────────────────────────────────────────────────────────────
    "wechat": {
        "monitor_enabled": True,
        "poll_minutes": 5,
        "monitor_contacts": [],
    },

    # ── 记忆管理 ─────────────────────────────────────────────────────────────
    "memory": {
        "focus_max_active":       20,    # focus.md 最多保留活跃条目数
        "people_md_limit":        20,    # people.md 最多联系人数
        "focus_dedup_threshold":  0.6,   # 焦点去重相似度阈值（bigram Jaccard）
        "pending_expire_days":    30,    # pending 超过多少天自动 expired
        "contact_top_n":          30,    # 刷新档案时取前 N 个活跃联系人
        "group_top_n":            15,    # 刷新档案时取前 N 个活跃群组
    },

    # ── AI 参数 ───────────────────────────────────────────────────────────────
    "ai": {
        "model":                  _cfg.VOLC_MODEL or "",
        "briefing_max_chars":     400,
        "daily_score_threshold":  7.0,
        "briefing_rule_check":    True,
        "focus_extract_enabled":  True,   # 是否从消息中自动提取焦点
        "summary_max_tokens":     300,    # 单封邮件/消息摘要最大 token
    },

    # ── Web 服务 ─────────────────────────────────────────────────────────────
    "web": {
        "port":  8077,
        "title": "Aegis",
    },

    # ── 功能模块开关 ─────────────────────────────────────────────────────────
    "modules": dict(_cfg.MODULES),
}


def load() -> dict:
    """加载设置，不存在的键用默认值填充"""
    if not _SETTINGS_FILE.exists():
        return _deep_copy(_DEFAULTS)
    try:
        saved = json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
        return _merge(_DEFAULTS, saved)
    except Exception:
        return _deep_copy(_DEFAULTS)


def save(settings: dict) -> None:
    """保存设置，并将凭证类字段同步写回 .credentials 和 config 模块"""
    _SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)

    # 敏感字段：空字符串表示"不修改"，保留现有值
    existing = load()
    settings = _merge(existing, settings)

    for section, fields in [
        ("api",        ["volc_api_key"]),
        ("email_163",  ["auth_code"]),
        ("email_gmail",["app_password"]),
    ]:
        for field in fields:
            if not settings.get(section, {}).get(field):
                settings.setdefault(section, {})[field] = existing.get(section, {}).get(field, "")

    _SETTINGS_FILE.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 回写 .credentials 并热更新 config 模块
    _sync_credentials(settings)
    _apply_to_runtime(settings)


def _sync_credentials(s: dict) -> None:
    """将设置中的凭证同步写回 .credentials 文件"""
    if not _CRED_FILE.exists():
        return
    try:
        lines = _CRED_FILE.read_text(encoding="utf-8").splitlines()
    except Exception:
        return

    mapping = {
        "OWNER_NAME":         s.get("user", {}).get("owner_name"),
        "OWNER_FULL_NAME":    s.get("user", {}).get("owner_full_name"),
        "OWNER_EN_NAME":      s.get("user", {}).get("owner_en_name"),
        "AEGIS_WXID":         s.get("user", {}).get("aegis_wxid"),
        "VOLC_API_KEY":       s.get("api", {}).get("volc_api_key"),
        "VOLC_API_BASE":      s.get("api", {}).get("volc_api_base"),
        "VOLC_MODEL":         s.get("api", {}).get("volc_model"),
        "NETEASE_EMAIL":      s.get("email_163", {}).get("email"),
        "NETEASE_AUTH_CODE":  s.get("email_163", {}).get("auth_code"),
        "NETEASE_IMAP_HOST":  s.get("email_163", {}).get("imap_host"),
        "NETEASE_IMAP_PORT":  str(s.get("email_163", {}).get("imap_port", "")),
        "NETEASE_SMTP_HOST":  s.get("email_163", {}).get("smtp_host"),
        "NETEASE_SMTP_PORT":  str(s.get("email_163", {}).get("smtp_port", "")),
        "GMAIL_EMAIL":        s.get("email_gmail", {}).get("email"),
        "GMAIL_APP_PASSWORD": s.get("email_gmail", {}).get("app_password"),
        "GMAIL_IMAP_HOST":    s.get("email_gmail", {}).get("imap_host"),
        "GMAIL_IMAP_PORT":    str(s.get("email_gmail", {}).get("imap_port", "")),
        "GMAIL_SMTP_HOST":    s.get("email_gmail", {}).get("smtp_host"),
        "GMAIL_SMTP_PORT":    str(s.get("email_gmail", {}).get("smtp_port", "")),
    }

    new_lines = []
    written_keys = set()
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            new_lines.append(line)
            continue
        key = stripped.split("=")[0].strip()
        if key in mapping and mapping[key] is not None and mapping[key] != "":
            new_lines.append(f"{key}={mapping[key]}")
            written_keys.add(key)
        else:
            new_lines.append(line)

    # 追加不在文件中的新 key
    for key, val in mapping.items():
        if key not in written_keys and val:
            new_lines.append(f"{key}={val}")

    try:
        _CRED_FILE.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    except Exception as e:
        print(f"[settings] .credentials 回写失败: {e}")


def _apply_to_runtime(s: dict) -> None:
    """热更新 config 模块属性（无需重启即可生效）"""
    import config as cfg

    u = s.get("user", {})
    if u.get("owner_name"):    cfg.OWNER_NAME       = u["owner_name"]
    if u.get("owner_full_name"): cfg.OWNER_FULL_NAME = u["owner_full_name"]
    if u.get("owner_en_name"): cfg.OWNER_EN_NAME    = u["owner_en_name"]
    if u.get("aegis_wxid"):    cfg.AEGIS_WXID       = u["aegis_wxid"]

    api = s.get("api", {})
    if api.get("volc_api_key"):  cfg.VOLC_API_KEY  = api["volc_api_key"]
    if api.get("volc_api_base"): cfg.VOLC_API_BASE = api["volc_api_base"]
    if api.get("volc_model"):
        cfg.VOLC_MODEL = api["volc_model"]

    e163 = s.get("email_163", {})
    if e163.get("email"):       cfg.NETEASE_EMAIL      = e163["email"]
    if e163.get("auth_code"):   cfg.NETEASE_AUTH_CODE  = e163["auth_code"]
    if e163.get("imap_host"):   cfg.NETEASE_IMAP_HOST  = e163["imap_host"]
    if e163.get("imap_port"):   cfg.NETEASE_IMAP_PORT  = int(e163["imap_port"])
    if e163.get("smtp_host"):   cfg.NETEASE_SMTP_HOST  = e163["smtp_host"]
    if e163.get("smtp_port"):   cfg.NETEASE_SMTP_PORT  = int(e163["smtp_port"])

    # 同步 ai.model → VOLC_MODEL
    ai = s.get("ai", {})
    if ai.get("model"):
        cfg.VOLC_MODEL = ai["model"]

    # 同步模块开关
    mods = s.get("modules", {})
    if mods:
        cfg.MODULES.update(mods)


def get(key_path: str, default=None):
    """点分隔路径读取，如 get('schedule.briefing_hour')"""
    s = load()
    parts = key_path.split(".")
    for p in parts:
        if isinstance(s, dict) and p in s:
            s = s[p]
        else:
            return default
    return s


def _merge(defaults: dict, overrides: dict) -> dict:
    result = _deep_copy(defaults)
    for k, v in overrides.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _merge(result[k], v)
        else:
            result[k] = v
    return result


def _deep_copy(d):
    return json.loads(json.dumps(d, ensure_ascii=False))
