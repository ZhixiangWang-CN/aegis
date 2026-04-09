"""
SQLite 备份策略
使用 sqlite3 在线备份（不需要停服务），每日保留最近 7 天。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path


BACKUP_KEEP_DAYS = 7


def daily_backup(db_path: Path) -> Path | None:
    """
    将 db_path 在线备份到同目录下 jarvis.db.backup-YYYY-MM-DD。
    使用 sqlite3.Connection.backup() 实现热备份，不停服。
    自动清理超过 7 天的旧备份。
    返回备份文件路径，失败返回 None。
    """
    if not db_path.exists():
        print(f"[backup] 源数据库不存在: {db_path}")
        return None

    today = datetime.now().strftime("%Y-%m-%d")
    backup_path = db_path.parent / f"jarvis.db.backup-{today}"

    try:
        src = sqlite3.connect(str(db_path))
        dst = sqlite3.connect(str(backup_path))
        try:
            src.backup(dst)
            dst.close()
            src.close()
            print(f"[backup] 备份完成: {backup_path}")
        except Exception as e:
            dst.close()
            src.close()
            raise e
    except Exception as e:
        print(f"[backup] 备份失败: {e}")
        return None

    # 清理旧备份
    _cleanup_old_backups(db_path)
    return backup_path


def _cleanup_old_backups(db_path: Path):
    """删除超过 BACKUP_KEEP_DAYS 天的备份文件"""
    cutoff = datetime.now() - timedelta(days=BACKUP_KEEP_DAYS)
    parent = db_path.parent

    removed = 0
    for f in parent.glob("jarvis.db.backup-*"):
        # 提取日期部分
        date_str = f.name.replace("jarvis.db.backup-", "")
        try:
            file_date = datetime.strptime(date_str, "%Y-%m-%d")
            if file_date < cutoff:
                f.unlink()
                removed += 1
                print(f"[backup] 清理旧备份: {f.name}")
        except ValueError:
            # 日期格式不匹配，跳过
            continue

    if removed:
        print(f"[backup] 共清理 {removed} 个旧备份")


def get_backup_list(db_path: Path) -> list[Path]:
    """
    返回所有备份文件，按日期升序排列（最旧在前）。
    """
    parent = db_path.parent
    backups = []
    for f in parent.glob("jarvis.db.backup-*"):
        date_str = f.name.replace("jarvis.db.backup-", "")
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
            backups.append(f)
        except ValueError:
            continue
    # 按文件名（含日期）排序
    return sorted(backups, key=lambda p: p.name)
