"""
Aegis四层记忆架构
设计参考: MemGPT + PARA + Zettelkasten + 认知科学

第一层(始终加载): self.md / focus.md / people.md
第二层(按需加载): projects/*.md
第三层(检索):     decisions.md / archive/
第四层(搜索):     SQLite + FTS5 + Chroma
"""
from __future__ import annotations

import re
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

import config

MEMORY_DIR = config.DATA_DIR / "memory"
PROJECTS_DIR = MEMORY_DIR / "projects"
ARCHIVE_DIR  = MEMORY_DIR / "archive"

for _d in (MEMORY_DIR, PROJECTS_DIR, ARCHIVE_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ── 文件路径 ─────────────────────────────────────────────────────────
SELF_PATH      = MEMORY_DIR / "self.md"
FOCUS_PATH     = MEMORY_DIR / "focus.md"
PEOPLE_PATH    = MEMORY_DIR / "people.md"
DECISIONS_PATH = MEMORY_DIR / "decisions.md"

# ── 模板 ─────────────────────────────────────────────────────────────
_SELF_TEMPLATE = """\
# 我是谁
> 最后更新: {ts}

职业: 医学影像AI研究者，放射组学方向
工作风格: 深夜高产，邮件响应快，行政任务易拖延
沟通偏好: 学术联系人用正式语气，合作者较随意
当前重心: 超声组学 + 肝脏疾病AI诊断
邮箱: {email}
"""

_FOCUS_TEMPLATE = """\
# 本周焦点
> 最后更新: {ts}
> 来源: 自动(邮件+微信) + 手动确认

"""

_PEOPLE_TEMPLATE = """\
# 重要联系人
> 最后更新: {ts}
> 只记录真正重要的人（≤20人），详细历史在数据库

"""

_DECISIONS_TEMPLATE = """\
# 决策记录
> 记录重要决策和原因，帮助AI理解你的思维方式
> 最后更新: {ts}

"""

_PROJECT_TEMPLATE = """\
# {name}
> 创建: {ts} | 状态: 进行中
> 一个研究方向/项目的全部相关信息

## 进行中
<!-- 当前活跃的具体任务 -->

## 下一步
<!-- 具体可执行的下一个行动 -->

## 连接
<!-- 关联的人、决策、资源 -->
→ search: "{search_hint}"

## 历史记录
<!-- 里程碑和重要事件 -->
"""


# ── 基础读写 ──────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _read(path: Path, template: str = "") -> str:
    if path.exists():
        return path.read_text(encoding="utf-8")
    content = template.format(
        ts=_now(),
        email=config.NETEASE_EMAIL,
        name=path.stem,
        search_hint=path.stem,
    )
    path.write_text(content, encoding="utf-8")
    return content


def _write(path: Path, content: str, source: str = "system"):
    # 更新时间戳
    content = re.sub(r"> 最后更新: .+", f"> 最后更新: {_now()}", content)
    # 经由 MemoryWriter 写入（git 追踪 + write_log）
    try:
        from memory.writer import get_writer
        target = str(path.relative_to(MEMORY_DIR))
        get_writer().write(target, "update", content, source)
    except Exception:
        # MemoryWriter 失败时降级为直接写入（保证功能不中断）
        path.write_text(content, encoding="utf-8")


def _update_ts(content: str) -> str:
    return re.sub(r"> 最后更新: .+", f"> 最后更新: {_now()}", content)


# ── 第一层: 始终加载 ──────────────────────────────────────────────────

def get_self() -> str:
    return _read(SELF_PATH, _SELF_TEMPLATE)


def get_focus() -> str:
    return _read(FOCUS_PATH, _FOCUS_TEMPLATE)


def get_people() -> str:
    return _read(PEOPLE_PATH, _PEOPLE_TEMPLATE)


def get_first_layer() -> str:
    """返回第一层全部内容（每次对话都注入），由 token_budget 控制总长度"""
    try:
        from memory.token_budget import build_layer1
        return build_layer1(MEMORY_DIR)
    except Exception as e:
        print(f"[layers] token_budget 失败，降级为直接拼接: {e}")
        parts = [get_self(), get_focus(), get_people()]
        return "\n\n---\n\n".join(p for p in parts if p.strip())


def update_self(field: str, value: str):
    """更新 self.md 中的某个字段（如: 职业、工作风格）"""
    content = get_self()
    pattern = rf"^{re.escape(field)}: .+$"
    new_line = f"{field}: {value}"
    if re.search(pattern, content, re.MULTILINE):
        content = re.sub(pattern, new_line, content, flags=re.MULTILINE)
    else:
        content = content.rstrip() + f"\n{new_line}\n"
    _write(SELF_PATH, content)


# ── 第一层: focus.md 更新（核心） ────────────────────────────────────

class FocusItem:
    """单条焦点事项"""
    __slots__ = ("text", "deadline", "source", "project", "db_ref", "priority", "from_name")

    def __init__(self, text: str, deadline: str = "", source: str = "",
                 project: str = "", db_ref: str = "", priority: str = "normal",
                 from_name: str = ""):
        self.text      = text
        self.deadline  = deadline
        self.source    = source      # "email" / "wechat" / "manual"
        self.project   = project     # 关联项目名
        self.db_ref    = db_ref      # → emails:id 或 → wechat:id
        self.priority  = priority    # "urgent" / "normal" / "waiting"
        self.from_name = from_name   # 发件人/发消息人姓名（用于展示来源）

    def to_md(self) -> str:
        icon = {"urgent": "🔴", "normal": "🟡", "waiting": "⏳"}.get(self.priority, "·")
        parts = [f"{icon} {self.text}"]
        if self.deadline:
            parts.append(f"DDL {self.deadline}")
        if self.from_name:
            parts.append(f"来自:{self.from_name}")
        if self.db_ref:
            # 去掉旧格式中可能已带的 "→ " 前缀，避免双箭头
            clean_ref = re.sub(r'^→\s*', '', self.db_ref).strip()
            parts.append(f"→ {clean_ref}")
        if self.project:
            parts.append(f"[{self.project}]")
        src_tag = {"email": "📧", "wechat": "💬", "manual": "✏️"}.get(self.source, "")
        if src_tag:
            parts.append(src_tag)
        return "- " + "  ".join(parts)


def set_focus(items: list[FocusItem], pending_review: list[FocusItem] = None):
    """
    替换 focus.md 内容。
    pending_review: 待用户确认的建议项（加 ? 标记）
    """
    lines = [f"# 本周焦点",
             f"> 最后更新: {_now()}",
             f"> 来源: 自动(邮件+微信) + 手动确认",
             ""]

    if items:
        lines.append("## 已确认")
        for item in items:
            lines.append(item.to_md())
        lines.append("")

    if pending_review:
        lines.append("## 待确认（回复 Aegis: 确认 或 Aegis: 修改）")
        for item in pending_review:
            lines.append("? " + item.to_md()[2:])  # 替换前缀为 ?
        lines.append("")

    _write(FOCUS_PATH, "\n".join(lines), "system")


def _focus_text_similar(a: str, b: str) -> bool:
    """
    简单相似度检查：字符级 bigram 重叠，>= 60% 视为重复。
    对中文和英文均有效（不依赖空格分词）。
    """
    if not a or not b:
        return False
    # 生成 bigram 字符集合
    def bigrams(s: str) -> set:
        s = s.lower().replace(" ", "")
        return {s[i:i+2] for i in range(len(s) - 1)} if len(s) > 1 else {s}
    a_bi = bigrams(a)
    b_bi = bigrams(b)
    if not a_bi or not b_bi:
        return False
    overlap = len(a_bi & b_bi)
    shorter = min(len(a_bi), len(b_bi))
    return overlap / shorter >= 0.6


def add_focus_item(item: FocusItem):
    """追加一条焦点事项，相似内容已存在则跳过"""
    content = get_focus()

    # 去重：提取已有条目文字，与新条目比较
    existing_texts = []
    for line in content.splitlines():
        line = line.strip()
        if line.startswith(("- 🔴", "- 🟡", "- ⏳", "- ·", "? 🔴", "? 🟡", "? ⏳")):
            # 取第一个双空格前的描述文字
            text_part = line.lstrip("- ?🔴🟡⏳· ").split("  ")[0].strip()
            existing_texts.append(text_part)

    if any(_focus_text_similar(item.text, t) for t in existing_texts):
        return  # 已有相似条目，静默跳过

    # 找到"已确认"段落后面插入
    if "## 已确认" in content:
        content = content.replace(
            "## 已确认\n",
            f"## 已确认\n{item.to_md()}\n"
        )
    else:
        content += f"\n## 已确认\n{item.to_md()}\n"
    _write(FOCUS_PATH, content)

    # 桌面通知：紧急焦点事项
    if getattr(item, "priority", "normal") == "urgent":
        try:
            from notifier import notify_focus
            notify_focus(item.text)
        except Exception:
            pass


def complete_focus_item(keyword: str):
    """标记焦点事项为完成 [x]（记录正向隐式信号）"""
    content = get_focus()
    new_lines = []
    for line in content.splitlines():
        if keyword.lower() in line.lower() and line.strip().startswith("- [ ]"):
            line = line.replace("- [ ]", "- [x]", 1)
            # 隐式信号：焦点完成 → 正向
            try:
                from memory.importance_learner import record_signal
                record_signal("focus_completed", content_hint=keyword[:80])
            except Exception:
                pass
        new_lines.append(line)
    _write(FOCUS_PATH, "\n".join(new_lines))


def clear_focus_item(keyword: str):
    """从 focus.md 删除包含关键词的条目"""
    content = get_focus()
    removed = []
    lines = []
    for l in content.splitlines():
        if keyword.lower() in l.lower() and l.startswith("- "):
            removed.append(l)
        else:
            lines.append(l)
    if removed:
        # 隐式信号：焦点被删除 → 负向（可能是噪音）
        try:
            from memory.importance_learner import record_signal
            record_signal("focus_deleted", content_hint=keyword[:80])
        except Exception:
            pass
    _write(FOCUS_PATH, "\n".join(lines))


# ── 第一层: people.md ────────────────────────────────────────────────

def upsert_person(name: str, role: str, note: str = "", email: str = ""):
    """更新或添加重要联系人"""
    content = get_people()
    person_line = f"- **{name}**"
    if email:
        person_line += f" <{email}>"
    person_line += f" — {role}"
    if note:
        person_line += f"，{note}"

    # 如果已存在同名/同邮件，替换
    lines = content.splitlines()
    updated = False
    for i, line in enumerate(lines):
        if name in line or (email and email in line):
            lines[i] = person_line
            updated = True
            break
    if not updated:
        # 追加
        lines.append(person_line)

    _write(PEOPLE_PATH, "\n".join(lines))


# ── 第二层: projects/ ────────────────────────────────────────────────

def list_projects() -> list[str]:
    """返回所有活跃项目名（不含 archive）"""
    return [p.stem for p in PROJECTS_DIR.glob("*.md")]


def get_project(name: str) -> Optional[str]:
    """读取项目文件，找不到返回 None"""
    # 模糊匹配
    for p in PROJECTS_DIR.glob("*.md"):
        if name.lower() in p.stem.lower():
            return p.read_text(encoding="utf-8")
    return None


def create_project(name: str, description: str = "",
                   search_hint: str = "") -> Path:
    """创建新项目文件"""
    safe_name = re.sub(r'[<>:"/\\|?*]', '_', name)
    path = PROJECTS_DIR / f"{safe_name}.md"
    if path.exists():
        return path

    content = _PROJECT_TEMPLATE.format(
        name=name,
        ts=_now(),
        search_hint=search_hint or name,
    )
    if description:
        content = content.replace(
            "## 进行中\n<!-- 当前活跃的具体任务 -->",
            f"## 进行中\n{description}"
        )
    _write(path, content, "system")
    print(f"[Memory] 新建项目: {name}")
    refresh_projects_overview()
    return path


def update_project(name: str, section: str, content_line: str):
    """向项目文件的指定段落追加一行"""
    for p in PROJECTS_DIR.glob("*.md"):
        if name.lower() in p.stem.lower():
            content = p.read_text(encoding="utf-8")
            # 找到段落并插入
            section_marker = f"## {section}"
            if section_marker in content:
                content = content.replace(
                    section_marker + "\n",
                    section_marker + f"\n- {content_line}\n"
                )
            else:
                content += f"\n{section_marker}\n- {content_line}\n"
            _write(p, content)
            refresh_projects_overview()
            return True
    return False


def get_relevant_projects(topic: str) -> list[tuple[str, str]]:
    """
    根据关键词找相关项目文件。
    返回 [(项目名, 内容), ...]
    """
    results = []
    for p in PROJECTS_DIR.glob("*.md"):
        content = p.read_text(encoding="utf-8")
        if topic.lower() in content.lower() or topic.lower() in p.stem.lower():
            results.append((p.stem, content))
    return results


def archive_project(name: str):
    """将项目归档"""
    for p in PROJECTS_DIR.glob("*.md"):
        if name.lower() in p.stem.lower():
            archive_path = ARCHIVE_DIR / p.name
            # 在文件头添加归档标记
            content = p.read_text(encoding="utf-8")
            content = content.replace(
                "| 状态: 进行中",
                f"| 状态: 已归档 {_now()}"
            )
            _write(archive_path, content, "system")
            p.unlink()
            # 记录原文件删除
            try:
                from memory.writer import get_writer
                get_writer().write(
                    str(p.relative_to(MEMORY_DIR)), "delete_line",
                    f"[归档] {name} → archive/", "system"
                )
            except Exception:
                pass
            print(f"[Memory] 项目已归档: {name}")
            refresh_projects_overview()
            return True
    return False


# ── 第三层: decisions.md ────────────────────────────────────────────

def add_decision(decision: str, reason: str, date: str = ""):
    """记录一个重要决策"""
    content = _read(DECISIONS_PATH, _DECISIONS_TEMPLATE)
    date = date or datetime.now().strftime("%Y-%m")
    entry = f"- [{date}] {decision}\n  原因: {reason}\n"
    content = content.rstrip() + "\n\n" + entry
    _write(DECISIONS_PATH, content)


def get_decisions() -> str:
    return _read(DECISIONS_PATH, _DECISIONS_TEMPLATE)


# ── 全局上下文构建（供 AI 调用）────────────────────────────────────

def build_ai_context(topic: str = "") -> dict:
    """
    构建 AI 需要的上下文字典。
    topic: 当前话题，用于按需加载相关项目文件。
    """
    ctx = {
        "layer1": get_first_layer(),
        "projects": [],
        "decisions": "",
    }

    # 按需加载相关项目
    if topic:
        related = get_relevant_projects(topic)
        ctx["projects"] = related[:3]  # 最多3个

    # decisions 总是附带（用于AI理解用户风格）
    decisions_content = get_decisions()
    if len(decisions_content) < 2000:
        ctx["decisions"] = decisions_content

    return ctx


def format_ai_context(topic: str = "") -> str:
    """返回可直接注入 AI system_prompt 的上下文字符串"""
    ctx = build_ai_context(topic)
    parts = ["## 用户背景\n" + ctx["layer1"]]

    if ctx["projects"]:
        parts.append("## 相关项目")
        for name, content in ctx["projects"]:
            # 只取前300字避免太长
            parts.append(f"### {name}\n{content[:300]}...")

    if ctx["decisions"]:
        parts.append("## 决策风格参考\n" + ctx["decisions"][-500:])

    return "\n\n".join(parts)


# ── 项目总览 ──────────────────────────────────────────────────────────

OVERVIEW_PATH = MEMORY_DIR / "projects_overview.md"


def _init_projects_overview():
    if not OVERVIEW_PATH.exists():
        OVERVIEW_PATH.write_text(
            f"# 项目总览\n> 最后更新: {_now()} | 活跃: 0 | 归档: 0\n\n"
            "## 进行中\n\n*暂无活跃项目*\n\n"
            "## 最近归档\n\n*暂无*\n",
            encoding="utf-8"
        )


def refresh_projects_overview():
    """重新生成 projects_overview.md，项目有变动时调用"""
    active = []
    for p in PROJECTS_DIR.glob("*.md"):
        try:
            text = p.read_text(encoding="utf-8")
            # 提取当前任务和下一步
            cur_task = _extract_section_first_line(text, "进行中")
            next_step = _extract_section_first_line(text, "下一步")
            # 最近更新时间（git mtime 或文件 mtime）
            mtime = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d")
            active.append({
                "name": p.stem,
                "cur_task": cur_task,
                "next_step": next_step,
                "updated": mtime,
            })
        except Exception:
            continue

    archive_count = len(list(ARCHIVE_DIR.glob("*.md")))

    lines = [
        "# 项目总览",
        f"> 最后更新: {_now()} | 活跃: {len(active)} | 归档: {archive_count}",
        "",
        "## 进行中",
        "",
    ]

    if active:
        lines.append("| 项目 | 当前任务 | 下一步 | 最近更新 |")
        lines.append("|------|---------|--------|---------|")
        for a in sorted(active, key=lambda x: x["updated"], reverse=True):
            lines.append(
                f"| [{a['name']}](projects/{a['name']}.md) "
                f"| {a['cur_task'] or '—'} "
                f"| {a['next_step'] or '—'} "
                f"| {a['updated']} |"
            )
    else:
        lines.append("*暂无活跃项目*")

    lines += ["", "## 最近归档", ""]
    archived = sorted(ARCHIVE_DIR.glob("*.md"),
                      key=lambda p: p.stat().st_mtime, reverse=True)[:5]
    for ap in archived:
        mtime = datetime.fromtimestamp(ap.stat().st_mtime).strftime("%Y-%m")
        lines.append(f"- {ap.stem} | 归档于 {mtime}")
    if not archived:
        lines.append("*暂无*")

    content = "\n".join(lines) + "\n"
    _write(OVERVIEW_PATH, content, "system")


def _extract_section_first_line(text: str, section: str) -> str:
    """从 Markdown 中提取指定 ## 段落的第一个非空非注释行"""
    in_section = False
    for line in text.splitlines():
        if line.startswith(f"## {section}"):
            in_section = True
            continue
        if in_section:
            if line.startswith("## "):
                break
            stripped = line.strip()
            if stripped and not stripped.startswith("<!--"):
                return stripped.lstrip("- ").strip()[:40]
    return ""


# ── 初始化 ─────────────────────────────────────────────────────────

def initialize():
    """初始化记忆文件（已存在则跳过）"""
    _read(SELF_PATH, _SELF_TEMPLATE)
    _read(FOCUS_PATH, _FOCUS_TEMPLATE)
    _read(PEOPLE_PATH, _PEOPLE_TEMPLATE)
    _read(DECISIONS_PATH, _DECISIONS_TEMPLATE)
    _init_projects_overview()

    # 确保 memory/ 目录是 git 仓库
    try:
        from memory.writer import ensure_git_repo
        ensure_git_repo(MEMORY_DIR)
    except Exception as e:
        print(f"[Memory] git 初始化失败（非致命）: {e}")

    # 迁移已有项目
    _migrate_existing_data()
    print(f"[Memory] 四层记忆初始化完成: {MEMORY_DIR}")


def _migrate_existing_data():
    """从旧数据（contacts、emails表）迁移已知重要信息"""
    try:
        from memory import db as main_db
        with main_db.get_conn() as conn:
            # 迁移重要联系人
            top_contacts = conn.execute("""
                SELECT email, name, institution, role, importance
                FROM contacts WHERE importance >= 4
                ORDER BY importance DESC LIMIT 15
            """).fetchall()

        people_content = get_people()
        existing = people_content

        for c in top_contacts:
            name = c[1] or c[0].split("@")[0]
            role = c[3] or "联系人"
            inst = c[2] or ""
            note = f"{inst}，{role}" if inst else role
            line = f"- **{name}** <{c[0]}> — {note}"
            if c[0] not in existing and name not in existing:
                existing += line + "\n"

        _write(PEOPLE_PATH, existing)
    except Exception:
        pass
