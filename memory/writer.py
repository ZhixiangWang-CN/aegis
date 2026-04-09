"""
memory/writer.py — 所有 memory/*.md 写入的唯一入口

职责：
1. 串行化写入（线程锁）
2. 写入前检测用户手动编辑（git status → 先 commit [manual]）
3. 写入前记录 write_log
4. 写入后自动 git commit
5. 提供回滚接口（git revert）
6. 同步追加 writes.jsonl 冗余日志
"""
from __future__ import annotations

import json
import re
import subprocess
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

import config

MEMORY_DIR  = config.DATA_DIR / "memory"
LOGS_DIR    = config.DATA_DIR / "logs"
WRITES_LOG  = LOGS_DIR / "writes.jsonl"

# git 是否可用（首次调用时检测）
_git_available: Optional[bool] = None


def _check_git() -> bool:
    global _git_available
    if _git_available is None:
        try:
            r = subprocess.run(["git", "--version"], capture_output=True, timeout=5)
            _git_available = (r.returncode == 0)
        except Exception:
            _git_available = False
    return _git_available


def _git(args: list[str], cwd: Path) -> tuple[int, str]:
    """运行 git 命令，返回 (returncode, stdout)"""
    if not _check_git():
        return -1, ""
    try:
        r = subprocess.run(
            ["git"] + args, cwd=str(cwd),
            capture_output=True, text=True, timeout=30
        )
        return r.returncode, r.stdout.strip()
    except Exception as e:
        return -1, str(e)


def ensure_git_repo(memory_dir: Path = MEMORY_DIR):
    """确保 memory/ 目录是 git 仓库，不是则初始化"""
    memory_dir.mkdir(parents=True, exist_ok=True)
    if not (memory_dir / ".git").exists():
        _git(["init"], memory_dir)
        _git(["config", "user.name", "Jarvis"], memory_dir)
        _git(["config", "user.email", "jarvis@local"], memory_dir)
        # 创建 .gitignore
        gitignore = memory_dir / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text("*.tmp\n*.bak\n", encoding="utf-8")
        # 初始提交
        _git(["add", "."], memory_dir)
        _git(["commit", "-m", "[init] memory 目录初始化", "--allow-empty"], memory_dir)
        print(f"[Writer] git 仓库已初始化: {memory_dir}")


# ── 核心写入器 ────────────────────────────────────────────────────────────────

class MemoryWriter:
    """线程安全的 memory/*.md 写入器，支持 batch 批量写入模式"""

    def __init__(self, memory_dir: Path = MEMORY_DIR):
        self.memory_dir = memory_dir
        self._lock = threading.RLock()   # Reentrant：batch 内可再次调用 write()
        self._batch_mode = False
        self._batch_ops: list[dict] = []
        ensure_git_repo(memory_dir)
        LOGS_DIR.mkdir(parents=True, exist_ok=True)

    def write(self, target: str, operation: str, content: str,
              source: str = "system", detail: dict = None) -> int:
        """
        唯一写入入口。
        target:    相对于 memory_dir 的文件路径，如 'focus.md' / 'projects/xxx.md'
        operation: 'append' / 'update' / 'replace_section' / 'delete_line'
        content:   写入内容（replace_section 时传 dict）
        source:    来源标记
        detail:    附加结构化数据（写入 write_log）
        返回 write_log id（batch 模式下返回 0，退出 batch 时统一写 log）
        """
        with self._lock:
            # 1. 检测并提交用户手动编辑（batch 模式只在第一次操作时检测）
            if not self._batch_mode:
                self._commit_manual_edits()

            # 2. 执行文件操作
            filepath = self.memory_dir / target
            filepath.parent.mkdir(parents=True, exist_ok=True)
            self._apply(filepath, operation, content)

            # 3. batch 模式：延迟 git commit 和 write_log，等退出 batch 时统一处理
            if self._batch_mode:
                self._batch_ops.append({
                    "target": target, "operation": operation,
                    "source": source, "content": str(content)[:200],
                    "detail": detail,
                })
                return 0

            # 4. 非 batch 模式：立即 git commit
            git_hash = self._git_commit(target, operation, source)

            # 5. 记录 write_log（数据库）
            from memory import db
            log_id = db.log_write(target, operation, source,
                                  str(content)[:200], detail, git_hash)

            # 6. 追加 writes.jsonl
            self._append_jsonl({
                "ts": datetime.now().isoformat(),
                "target": target, "operation": operation,
                "source": source, "content": str(content)[:200],
                "git_hash": git_hash, "log_id": log_id,
            })

            return log_id

    @contextmanager
    def batch(self, batch_label: str = "batch"):
        """
        批量写入模式：上下文内多次 write() 只产生一次 git commit。

        用于大批量文件更新（如刷新99个联系人档案），避免碎片 commit。

        用法：
            with get_writer().batch("wechat_refresh"):
                for contact in contacts:
                    get_writer().write(f"contacts/{contact}.md", "update", content)
            # 退出时统一 git commit + write_log
        """
        with self._lock:
            self._batch_mode = True
            self._batch_ops = []
            try:
                yield
            finally:
                self._batch_mode = False
                if self._batch_ops:
                    # 一次性 git commit
                    _git(["add", "."], self.memory_dir)
                    msg = (f"[jarvis:batch] {batch_label} "
                           f"({len(self._batch_ops)} files)")
                    _git(["commit", "-m", msg, "--allow-empty"], self.memory_dir)
                    _, git_hash = _git(["rev-parse", "--short", "HEAD"],
                                       self.memory_dir)

                    # 批量写 write_log + jsonl
                    from memory import db
                    ts = datetime.now().isoformat()
                    for op in self._batch_ops:
                        log_id = db.log_write(
                            op["target"], op["operation"], op["source"],
                            op["content"], op.get("detail"), git_hash,
                        )
                        self._append_jsonl({
                            "ts": ts, "git_hash": git_hash, "log_id": log_id,
                            **{k: op[k] for k in
                               ("target", "operation", "source", "content")},
                        })
                self._batch_ops = []

    def rollback(self, log_id: int) -> bool:
        """回滚指定 write_log id 对应的 git commit"""
        with self._lock:
            from memory import db
            log = db.get_write_log(log_id)
            if not log or not log.get("git_hash") or log.get("reverted"):
                return False

            code, _ = _git(["revert", "--no-commit", log["git_hash"]], self.memory_dir)
            if code == 0:
                self._git_commit(log["target"], "rollback", "system")
                db.mark_reverted(log_id)
                return True
            return False

    # ── 内部方法 ─────────────────────────────────────────────────────────────

    def _commit_manual_edits(self):
        """检测用户手动修改 → 先提交为 [manual]"""
        code, status = _git(["status", "--porcelain"], self.memory_dir)
        if code == 0 and status.strip():
            _git(["add", "."], self.memory_dir)
            _git(["commit", "-m",
                  f"[manual] 用户手动编辑 @ {datetime.now().strftime('%Y-%m-%d %H:%M')}"],
                 self.memory_dir)

    def _git_commit(self, target: str, operation: str, source: str) -> str:
        """提交变更，返回 commit hash"""
        _git(["add", "."], self.memory_dir)
        msg = f"[jarvis:{source}] {operation} {target}"
        _git(["commit", "-m", msg, "--allow-empty"], self.memory_dir)
        _, git_hash = _git(["rev-parse", "--short", "HEAD"], self.memory_dir)
        return git_hash

    def _apply(self, filepath: Path, operation: str, content):
        """执行实际文件修改"""
        if operation == "append":
            with open(filepath, "a", encoding="utf-8") as f:
                f.write("\n" + str(content))

        elif operation == "update":
            # 整文件替换（用于 AI 重写整个文件的场景）
            filepath.write_text(str(content), encoding="utf-8")

        elif operation == "replace_section":
            # content: {"section": "## 已确认", "new_content": "..."}
            if isinstance(content, dict):
                section = content.get("section", "")
                new_content = content.get("new_content", "")
                self._replace_section(filepath, section, new_content)
            else:
                filepath.write_text(str(content), encoding="utf-8")

        elif operation == "delete_line":
            # content: 要删除的行的关键词
            if filepath.exists():
                lines = filepath.read_text(encoding="utf-8").splitlines()
                lines = [l for l in lines if str(content) not in l]
                filepath.write_text("\n".join(lines), encoding="utf-8")

        elif operation == "upsert_line":
            # content: {"match": "关键词", "line": "新行内容"}
            if isinstance(content, dict):
                match_kw = content.get("match", "")
                new_line = content.get("line", "")
                self._upsert_line(filepath, match_kw, new_line)

    def _replace_section(self, filepath: Path, section_header: str, new_content: str):
        """替换 Markdown 文件中某个 ## 段落的内容"""
        if not filepath.exists():
            return
        text = filepath.read_text(encoding="utf-8")
        # 找到段落起止位置
        pattern = rf"({re.escape(section_header)}\n)(.*?)(?=\n## |\Z)"
        replacement = f"{section_header}\n{new_content}\n"
        new_text = re.sub(pattern, replacement, text, flags=re.DOTALL)
        if new_text == text:
            # 段落不存在，追加
            new_text = text.rstrip() + f"\n\n{section_header}\n{new_content}\n"
        filepath.write_text(new_text, encoding="utf-8")

    def _upsert_line(self, filepath: Path, match_kw: str, new_line: str):
        """更新包含 match_kw 的行；不存在则追加"""
        if not filepath.exists():
            filepath.write_text(new_line + "\n", encoding="utf-8")
            return
        lines = filepath.read_text(encoding="utf-8").splitlines()
        updated = False
        for i, line in enumerate(lines):
            if match_kw in line:
                lines[i] = new_line
                updated = True
                break
        if not updated:
            lines.append(new_line)
        filepath.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _append_jsonl(self, entry: dict):
        try:
            with open(WRITES_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass


# ── 全局单例 ──────────────────────────────────────────────────────────────────

_writer: Optional[MemoryWriter] = None


def get_writer() -> MemoryWriter:
    global _writer
    if _writer is None:
        _writer = MemoryWriter()
    return _writer


# ── 便捷函数（供其他模块调用）─────────────────────────────────────────────────

def write(target: str, operation: str, content, source: str = "system",
          detail: dict = None) -> int:
    return get_writer().write(target, operation, content, source, detail)


def rollback(log_id: int) -> bool:
    return get_writer().rollback(log_id)
