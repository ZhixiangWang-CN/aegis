"""
Phase 1: 目录扫描
策略: 文件夹名称优先判断 → 整个目录不对直接跳过 → 再按后缀白名单过滤
"""
import os
import json
from pathlib import Path
from datetime import datetime
import config
from memory import db

# ── 文件夹黑名单（含这些关键词的目录直接跳过整棵树）──────────────────
SKIP_DIR_KEYWORDS = {
    # 系统
    "windows", "system32", "syswow64", "winsxs", "boot", "recovery",
    "system volume information", "$recycle.bin", "perflogs",
    "$winreagent", "pe_jsyos",
    # 软件/运行时
    "program files", "programdata", "appdata", "application data",
    "node_modules", "site-packages", "dist-packages", "venv", ".venv",
    "env", "__pycache__", ".git", ".svn", ".hg",
    "cache", "caches", ".cache",
    # 构建产物
    "dist", "build", "target", "out", "output", "bin", "obj",
    ".next", ".nuxt", "coverage",
    # 软件安装/更新
    "installer", "setup", "update", "patch", "temp", "tmp",
    # 游戏/媒体/软件
    "steam", "steamapps", "epic games", "wegameapps",
    # 大型软件目录（matlab, photoshop, cursor等）
    "matlab", "adobe", "cursor", "jetbrains",
    # 驱动备份
    "driverbackup",
}

# ── 文件后缀白名单（只索引这些后缀）──────────────────────────────────
ALLOWED_EXTENSIONS = {
    # 笔记/文档
    ".md", ".txt", ".docx", ".doc", ".odt", ".rtf",
    # PDF
    ".pdf",
    # 代码（反映技能和项目）
    ".py", ".ipynb", ".r", ".m", ".sql",
    # 数据
    ".csv", ".json",  # 小json可能是配置/笔记
    # 表格
    ".xlsx", ".xls",
}

# ── 文件夹名称高价值关键词（含这些词优先处理）──────────────────────────
HIGH_VALUE_DIR_KEYWORDS = {
    "笔记", "note", "notes", "日记", "diary", "journal",
    "论文", "paper", "thesis", "研究", "research",
    "项目", "project", "work", "工作", "科研",
    "简历", "resume", "cv", "个人", "personal",
    "计划", "plan", "目标", "goal",
    "学习", "study", "课程", "course",
    "文档", "document", "doc", "资料",
    "微信", "wechat",
}


def _should_skip_dir(dir_name: str) -> bool:
    name_lower = dir_name.lower()
    return any(kw in name_lower for kw in SKIP_DIR_KEYWORDS)


def _dir_priority(dir_path: str) -> int:
    """返回目录优先级，数字越小越优先"""
    name_lower = Path(dir_path).name.lower()
    if any(kw in name_lower for kw in HIGH_VALUE_DIR_KEYWORDS):
        return 1
    return 2


def scan_roots(roots: list[str] = None) -> dict:
    """
    扫描根目录列表，返回统计信息。
    同时将合格文件写入 SQLite file_index 表。
    """
    roots = roots or config.SCAN_ROOTS
    stats = {"scanned_dirs": 0, "indexed_files": 0, "skipped_dirs": 0}
    max_size = config.MAX_FILE_SIZE_MB * 1024  # KB

    for root in roots:
        root_path = Path(root)
        if not root_path.exists():
            continue
        print(f"[Scanner] 开始扫描: {root}")

        for dirpath, dirnames, filenames in os.walk(root, topdown=True):
            depth = len(Path(dirpath).relative_to(root_path).parts)
            if depth > 8:           # 超过8层深度直接剪枝，防止陷入深层目录
                dirnames.clear()
                continue

            # 原地修改 dirnames 来剪枝（os.walk topdown 特性）
            dirnames[:] = [
                d for d in dirnames
                if not _should_skip_dir(d)
                and not d.startswith(".")    # 跳过隐藏目录（.开头）
            ]
            stats["scanned_dirs"] += 1

            if stats["scanned_dirs"] % 500 == 0:
                print(f"[Scanner] 已扫描 {stats['scanned_dirs']} 个目录, {stats['indexed_files']} 个文件...")

            # 批量写入合格文件
            batch = []
            for fname in filenames:
                ext = Path(fname).suffix.lower()
                if ext not in ALLOWED_EXTENSIONS:
                    continue
                full_path = os.path.join(dirpath, fname)
                try:
                    stat = os.stat(full_path)
                    size_kb = stat.st_size // 1024
                    if size_kb > max_size:
                        continue
                    modified_at = datetime.fromtimestamp(stat.st_mtime).isoformat()
                    batch.append((full_path, ext, size_kb, modified_at))
                except (PermissionError, OSError):
                    continue

            for item in batch:
                db.upsert_file(*item)
                stats["indexed_files"] += 1

    print(f"[Scanner] 完成: {stats}")
    return stats


def save_index_snapshot():
    """把当前 file_index 表导出为 JSON 快照（便于查看）"""
    import sqlite3
    conn = sqlite3.connect(str(config.DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT path, file_type, size_kb, modified_at, status FROM file_index ORDER BY file_type, path").fetchall()
    conn.close()
    snapshot = [dict(r) for r in rows]
    with open(config.FILE_INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    print(f"[Scanner] 索引快照已保存: {len(snapshot)} 个文件")
