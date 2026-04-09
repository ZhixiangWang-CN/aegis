"""
微信文件专项处理
微信PC端路径: C:/Users/{user}/Documents/WeChat Files/{wxid}/
主要读取 FileStorage/File/ 下的可读文档
聊天记录 .db 文件为加密格式（SQLCipher），v1.0 暂跳过
"""
import os
from pathlib import Path
from memory import db
from scanner.directory_indexer import ALLOWED_EXTENSIONS


def find_wechat_roots() -> list[str]:
    """自动发现所有用户的微信文件目录"""
    roots = []
    # 常见路径
    candidates = []
    users_dir = Path("C:/Users")
    if users_dir.exists():
        for user_dir in users_dir.iterdir():
            if not user_dir.is_dir():
                continue
            for base in [
                user_dir / "Documents" / "WeChat Files",
                user_dir / "Documents" / "微信文件",
                Path("D:/WeChat Files"),
                Path("E:/WeChat Files"),
            ]:
                if base.exists():
                    candidates.append(str(base))

    # 去重
    seen = set()
    for c in candidates:
        if c not in seen:
            roots.append(c)
            seen.add(c)
    return roots


def index_wechat_files(wechat_roots: list[str] = None) -> int:
    """
    扫描微信文件目录，只读取 FileStorage/File/ 下的文档类文件。
    返回新增索引文件数。
    """
    if wechat_roots is None:
        wechat_roots = find_wechat_roots()

    if not wechat_roots:
        print("[WeChat] 未找到微信文件目录")
        return 0

    count = 0
    for wechat_root in wechat_roots:
        print(f"[WeChat] 扫描微信目录: {wechat_root}")
        wechat_path = Path(wechat_root)

        # 遍历各 wxid 账号目录
        for wxid_dir in wechat_path.iterdir():
            if not wxid_dir.is_dir():
                continue

            # 优先处理 FileStorage/File — 用户接收的文件
            file_storage = wxid_dir / "FileStorage" / "File"
            if file_storage.exists():
                count += _index_dir(file_storage, source="wechat_file")

            # 如果有手动导出的聊天记录 txt，也一并索引
            for txt_file in wxid_dir.rglob("*.txt"):
                ext = txt_file.suffix.lower()
                if ext in ALLOWED_EXTENSIONS:
                    try:
                        stat = txt_file.stat()
                        size_kb = stat.st_size // 1024
                        if size_kb < 5000:   # 跳过超大聊天记录
                            db.upsert_file(
                                str(txt_file), ext, size_kb,
                                str(txt_file.stat().st_mtime)
                            )
                            count += 1
                    except (PermissionError, OSError):
                        continue

    print(f"[WeChat] 新增索引 {count} 个文件")
    return count


def _index_dir(directory: Path, source: str) -> int:
    count = 0
    for fpath in directory.rglob("*"):
        if not fpath.is_file():
            continue
        ext = fpath.suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            continue
        try:
            stat = fpath.stat()
            size_kb = stat.st_size // 1024
            if size_kb > 10 * 1024:  # 跳过 > 10MB
                continue
            db.upsert_file(str(fpath), ext, size_kb, str(stat.st_mtime))
            count += 1
        except (PermissionError, OSError):
            continue
    return count
