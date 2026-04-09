import os
from pathlib import Path

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

_credentials = {}

def _load_credentials():
    cred_file = BASE_DIR / ".credentials"
    if not cred_file.exists():
        raise FileNotFoundError(f"找不到凭证文件: {cred_file}")
    with open(cred_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, val = line.partition("=")
                _credentials[key.strip()] = val.strip()

_load_credentials()

def get(key: str, default=None):
    return _credentials.get(key, default)

# 火山引擎
VOLC_API_KEY   = get("VOLC_API_KEY")
VOLC_API_BASE  = get("VOLC_API_BASE", "https://ark.cn-beijing.volces.com/api/v3")
VOLC_MODEL     = get("VOLC_MODEL", "doubao-seed-2-0-lite-260215")

# 网易邮箱
NETEASE_EMAIL      = get("NETEASE_EMAIL")
NETEASE_AUTH_CODE  = get("NETEASE_AUTH_CODE")
NETEASE_IMAP_HOST  = get("NETEASE_IMAP_HOST", "imap.163.com")
NETEASE_IMAP_PORT  = int(get("NETEASE_IMAP_PORT", "993"))
NETEASE_SMTP_HOST  = get("NETEASE_SMTP_HOST", "smtp.163.com")
NETEASE_SMTP_PORT  = int(get("NETEASE_SMTP_PORT", "465"))

# Gmail
GMAIL_EMAIL      = get("GMAIL_EMAIL")
GMAIL_APP_PWD    = get("GMAIL_APP_PASSWORD")
GMAIL_IMAP_HOST  = get("GMAIL_IMAP_HOST", "imap.gmail.com")
GMAIL_IMAP_PORT  = int(get("GMAIL_IMAP_PORT", "993"))
GMAIL_SMTP_HOST  = get("GMAIL_SMTP_HOST", "smtp.gmail.com")
GMAIL_SMTP_PORT  = int(get("GMAIL_SMTP_PORT", "465"))

# 数据路径
DB_PATH          = DATA_DIR / "jarvis.db"
PROFILE_PATH     = DATA_DIR / "profile.json"
FILE_INDEX_PATH  = DATA_DIR / "file_index.json"
CHROMA_PATH      = str(DATA_DIR / "chroma")

# 扫描配置（只扫有价值的目录，避免系统/驱动垃圾）
SCAN_ROOTS = [
    "C:/Users/Administrator/Documents",
    "C:/Users/Administrator/Desktop",
    "D:/",
    "E:/codes",
    "G:/Documents",
]
SKIP_DIRS = {
    "Windows", "Program Files", "Program Files (x86)",
    "ProgramData", "AppData", "node_modules", "__pycache__",
    ".git", "System Volume Information", "$Recycle.Bin",
    "Recovery", "hiberfil.sys",
}
SUPPORTED_EXTENSIONS = {".md", ".txt", ".docx", ".pdf", ".py", ".ipynb", ".csv"}
MAX_FILE_SIZE_MB = 10

# 模块开关
MODULES = {
    "email_163":       True,
    "email_gmail":     False,  # App Password may be disabled
    "wechat":          True,
    "rss":             True,
    "disk_scan":       True,
    "vector_store":    True,
    "fts_store":       True,
    "knowledge_graph": False,
}

# 用户信息（在 .credentials 里配置）
OWNER_NAME       = get("OWNER_NAME", "用户")          # 显示名，用于日报问候
OWNER_FULL_NAME  = get("OWNER_FULL_NAME", "")         # 全名（中文），用于邮件记忆提取
OWNER_EN_NAME    = get("OWNER_EN_NAME", "")           # 英文名
JARVIS_WXID      = get("JARVIS_WXID", "")             # 兼容旧配置
AEGIS_WXID       = get("AEGIS_WXID", "") or JARVIS_WXID  # Aegis 专用微信号 wxid

# 路径
MEMORY_DIR = DATA_DIR / "memory"
LOGS_DIR   = DATA_DIR / "logs"
FTS_DB_PATH = DATA_DIR / "fts_index.db"

# people.md 设置
PEOPLE_MD_LIMIT = 20
PEOPLE_MD_MIN_IMPORTANCE = 70
