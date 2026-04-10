"""
附件自动归档管理器

职责：
1. 邮件/微信收到附件时自动下载并归类存储
2. 根据发件人、主题、文件名匹配对应项目文件夹
3. 提供搜索接口：find_attachment(keyword) 用于发送指令
4. 支持按需发送：send_attachment(keyword, to_contact)

文件夹结构：
  data/attachments/
  ├── projects/
  │   ├── 博士后申请/        ← 从 data/memory/projects/ 自动创建
  │   └── NAFLD分析/
  ├── _contracts/            ← 合同 / 协议
  ├── _papers/               ← 学术论文 PDF
  ├── _reports/              ← 报告 / 周报 / 总结
  ├── _data/                 ← 数据文件 (csv/xlsx/mat)
  └── _inbox/                ← 未能匹配，待手动归类
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import datetime
from pathlib import Path

import config

# ── 路径 ──────────────────────────────────────────────────────────────────────
ATTACH_ROOT  = config.DATA_DIR / "attachments"
PROJECTS_DIR = ATTACH_ROOT / "projects"
SYSTEM_DIRS  = {
    "_contracts": ["合同", "协议", "contract", "agreement", "nda"],
    "_papers":    ["论文", "paper", "manuscript", "preprint", "article"],
    "_reports":   ["报告", "汇报", "总结", "周报", "月报", "report", "summary"],
    "_data":      [".csv", ".xlsx", ".xls", ".mat", ".npz", ".h5", "数据", "dataset"],
    "_inbox":     [],  # 兜底
}
ATTACH_INDEX = config.DATA_DIR / "attachment_index.json"


# ── 初始化目录 ────────────────────────────────────────────────────────────────

def ensure_dirs():
    """根据 projects/*.md 自动创建项目子文件夹"""
    ATTACH_ROOT.mkdir(parents=True, exist_ok=True)
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    for d in SYSTEM_DIRS:
        (ATTACH_ROOT / d).mkdir(exist_ok=True)

    # 从 memory/projects/ 读取项目名，创建对应目录
    proj_dir = config.DATA_DIR / "memory" / "projects"
    if proj_dir.exists():
        for md in proj_dir.glob("*.md"):
            if md.stem != "INDEX":
                safe_name = re.sub(r'[<>:"/\\|?*]', '_', md.stem)
                (PROJECTS_DIR / safe_name).mkdir(exist_ok=True)


# ── 归类规则 ──────────────────────────────────────────────────────────────────

def _load_project_keywords() -> dict[str, list[str]]:
    """
    读取 projects/*.md 的文件名和首行标题作为关键词。
    返回 {项目目录名: [关键词列表]}
    """
    result = {}
    proj_dir = config.DATA_DIR / "memory" / "projects"
    if not proj_dir.exists():
        return result
    for md in proj_dir.glob("*.md"):
        if md.stem == "INDEX":
            continue
        safe_name = re.sub(r'[<>:"/\\|?*]', '_', md.stem)
        keywords = [md.stem.lower()]
        # 读取前5行提取更多关键词
        try:
            lines = md.read_text("utf-8", errors="ignore").splitlines()[:5]
            for line in lines:
                words = re.findall(r'[\w\u4e00-\u9fff]{2,}', line)
                keywords.extend(w.lower() for w in words if len(w) >= 2)
        except Exception:
            pass
        result[safe_name] = list(set(keywords))
    return result


def _categorize(filename: str, sender: str = "", subject: str = "") -> Path:
    """
    判断文件应存入哪个目录。
    优先级：项目匹配 > 系统分类 > _inbox
    """
    ensure_dirs()
    text = f"{filename} {subject} {sender}".lower()
    fname_lower = filename.lower()

    # ① 项目匹配
    proj_kw = _load_project_keywords()
    best_proj = None
    best_score = 0
    for proj_name, keywords in proj_kw.items():
        score = sum(1 for kw in keywords if kw in text)
        if score > best_score:
            best_score = score
            best_proj = proj_name
    if best_proj and best_score >= 2:
        return PROJECTS_DIR / best_proj

    # ② 系统分类（按关键词和扩展名）
    for dir_name, keywords in SYSTEM_DIRS.items():
        if dir_name == "_inbox":
            continue
        if any(kw in fname_lower or kw in text for kw in keywords):
            return ATTACH_ROOT / dir_name

    return ATTACH_ROOT / "_inbox"


# ── 核心存储接口 ──────────────────────────────────────────────────────────────

def save_attachment(
    content: bytes,
    filename: str,
    sender: str = "",
    subject: str = "",
    source: str = "email",   # "email" | "wechat"
    source_id: str = "",
) -> Path:
    """
    保存附件到对应目录，返回最终存储路径。
    自动处理文件名冲突（加哈希后缀）。
    """
    ensure_dirs()
    dest_dir = _categorize(filename, sender, subject)
    dest_dir.mkdir(parents=True, exist_ok=True)

    # 去重：同内容不重复存储
    content_hash = hashlib.md5(content).hexdigest()[:8]
    stem = Path(filename).stem
    suffix = Path(filename).suffix or ""
    safe_stem = re.sub(r'[<>:"/\\|?*\s]', '_', stem)[:60]

    dest = dest_dir / f"{safe_stem}{suffix}"
    if dest.exists():
        # 内容相同则跳过
        if dest.read_bytes() == content:
            _index_attachment(dest, sender, subject, source, source_id, content_hash)
            return dest
        # 内容不同则加哈希区分
        dest = dest_dir / f"{safe_stem}_{content_hash}{suffix}"

    dest.write_bytes(content)
    _index_attachment(dest, sender, subject, source, source_id, content_hash)
    print(f"[Attach] 已归档: {dest.relative_to(ATTACH_ROOT)}")
    return dest


def _index_attachment(path: Path, sender: str, subject: str,
                      source: str, source_id: str, content_hash: str):
    """更新附件索引 JSON，用于搜索"""
    try:
        index = json.loads(ATTACH_INDEX.read_text("utf-8")) if ATTACH_INDEX.exists() else []
        # 检查是否已有此路径
        paths = {e["path"] for e in index}
        if str(path) in paths:
            return
        index.append({
            "path": str(path),
            "filename": path.name,
            "folder": path.parent.name,
            "sender": sender,
            "subject": subject,
            "source": source,
            "source_id": source_id,
            "hash": content_hash,
            "saved_at": datetime.now().isoformat(),
        })
        # 只保留最近 5000 条
        ATTACH_INDEX.write_text(
            json.dumps(index[-5000:], ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    except Exception as e:
        print(f"[Attach] 索引更新失败: {e}")


# ── 搜索接口 ──────────────────────────────────────────────────────────────────

def find_attachments(keyword: str, limit: int = 5) -> list[dict]:
    """
    按关键词搜索已归档附件。
    匹配文件名、发件人、主题、目录名。
    """
    if not ATTACH_INDEX.exists():
        return []
    try:
        index = json.loads(ATTACH_INDEX.read_text("utf-8"))
    except Exception:
        return []

    kw = keyword.lower()
    results = []
    for entry in reversed(index):  # 最新的优先
        text = f"{entry.get('filename','')} {entry.get('subject','')} {entry.get('sender','')} {entry.get('folder','')}".lower()
        if kw in text:
            # 确认文件还存在
            if Path(entry["path"]).exists():
                results.append(entry)
        if len(results) >= limit:
            break
    return results


def list_attachments(folder: str = "", limit: int = 20) -> list[dict]:
    """列出指定文件夹（或全部）的附件"""
    if not ATTACH_INDEX.exists():
        return []
    try:
        index = json.loads(ATTACH_INDEX.read_text("utf-8"))
    except Exception:
        return []

    results = []
    for entry in reversed(index):
        if folder and folder.lower() not in entry.get("folder", "").lower():
            continue
        if Path(entry["path"]).exists():
            results.append(entry)
        if len(results) >= limit:
            break
    return results


def get_attachment_summary() -> str:
    """生成附件库概览，用于 Aegis: 状态 指令"""
    ensure_dirs()
    lines = ["📁 **附件库**"]
    # 项目文件夹
    if PROJECTS_DIR.exists():
        for d in sorted(PROJECTS_DIR.iterdir()):
            if d.is_dir():
                count = len(list(d.glob("*")))
                if count > 0:
                    lines.append(f"  📂 projects/{d.name}/  ({count} 个文件)")
    # 系统文件夹
    for dir_name in SYSTEM_DIRS:
        d = ATTACH_ROOT / dir_name
        if d.exists():
            count = len(list(d.glob("*")))
            if count > 0:
                lines.append(f"  📂 {dir_name}/  ({count} 个文件)")
    return "\n".join(lines) if len(lines) > 1 else "附件库为空"


# ── 微信 FileStorage 扫描 ─────────────────────────────────────────────────────

def scan_wechat_files(days_back: int = 7) -> int:
    """
    扫描微信 FileStorage 目录，将近期文件归档。
    只处理有意义的文件类型（排除图片/语音）。
    """
    from json import loads
    keys_file = Path(config.BASE_DIR) / "vendor" / "wechat-decrypt" / "all_keys.json"
    if not keys_file.exists():
        return 0

    try:
        db_dir = Path(loads(keys_file.read_text("utf-8")).get("_db_dir", ""))
    except Exception:
        return 0

    # 微信文件在 FileStorage/File/YYYY-MM/ 下
    file_storage = db_dir.parent / "FileStorage" / "File"
    if not file_storage.exists():
        # 尝试常见路径
        wechat_root = db_dir.parent.parent
        file_storage = wechat_root / "FileStorage" / "File"
    if not file_storage.exists():
        return 0

    VALID_EXTENSIONS = {
        ".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt",
        ".csv", ".txt", ".md", ".zip", ".rar", ".7z",
    }
    cutoff = datetime.now().timestamp() - days_back * 86400
    count = 0

    for f in file_storage.rglob("*"):
        if not f.is_file():
            continue
        if f.suffix.lower() not in VALID_EXTENSIONS:
            continue
        if f.stat().st_mtime < cutoff:
            continue
        # 检查是否已归档
        existing = find_attachments(f.name, limit=1)
        if existing and Path(existing[0]["path"]).exists():
            continue
        try:
            content = f.read_bytes()
            save_attachment(content, f.name, sender="WeChat", source="wechat")
            count += 1
        except Exception:
            pass

    return count
