"""
Aegis记忆管理 — 按来源分文件存储
基于 OpenJarvis memory management 模式。

文件结构:
  data/memory/
    profile.md      — 核心身份档案（手动维护 + 综合提炼）
    from_emails.md  — 从邮件中提取的知识
    from_files.md   — 从硬盘文件中提取的知识
    from_wechat.md  — 从微信聊天中提取的知识
    from_user.md    — 用户主动告知的信息（指令/对话）
    contacts.md     — 重要联系人动态
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import config

# 记忆目录（独立子目录，不与其他数据混放）
MEMORY_DIR = config.DATA_DIR / "memory"
MEMORY_DIR.mkdir(parents=True, exist_ok=True)

# 各来源文件路径
FILES = {
    "profile":   MEMORY_DIR / "profile.md",
    "emails":    MEMORY_DIR / "from_emails.md",
    "files":     MEMORY_DIR / "from_files.md",
    "wechat":    MEMORY_DIR / "from_wechat.md",
    "user":      MEMORY_DIR / "from_user.md",
    "contacts":  MEMORY_DIR / "contacts.md",
}

# 保留旧路径兼容（MEMORY.md 重定向到 profile.md）
MEMORY_PATH = FILES["profile"]

_TEMPLATES = {
    "profile": """\
# Aegis — 用户核心档案
> 来源: 综合提炼 + 手动维护
> 最后更新: {ts}

## 身份与职业
- 邮箱: {email}

## 专业领域

## 当前目标与计划

## 行为模式

""",
    "emails": """\
# 记忆来源: 邮件
> 从邮件往来中提炼的知识和事实。
> 最后更新: {ts}

## 重要联系人
<!-- 从邮件中识别的重要联系人 -->

## 项目进展
<!-- 从邮件内容中推断的项目状态 -->

## 碎片事实
<!-- 邮件中发现的关于用户的有价值信息 -->

""",
    "files": """\
# 记忆来源: 硬盘文件
> 从扫描的本地文件中提炼的知识。
> 最后更新: {ts}

## 关注话题
<!-- 从文件内容推断的研究兴趣 -->

## 专业知识
<!-- 从文件中发现的专业背景 -->

## 碎片事实
<!-- 文件中发现的有价值信息 -->

""",
    "wechat": """\
# 记忆来源: 微信聊天
> 从微信聊天记录中提炼的知识。
> 最后更新: {ts}

## 常用联系人
<!-- 微信上的重要联系人 -->

## 社交模式
<!-- 从聊天中推断的社交习惯 -->

## 碎片事实
<!-- 聊天中发现的有价值信息 -->

""",
    "user": """\
# 记忆来源: 用户直接告知
> 用户通过对话/邮件指令主动提供的信息（优先级最高）。
> 最后更新: {ts}

## 明确指示
<!-- 用户明确告诉Aegis的事实 -->

## 个人偏好
<!-- 用户表达的偏好和习惯 -->

## 待办与提醒
<!-- 用户要求记住的待办事项 -->

""",
    "contacts": """\
# 记忆来源: 联系人动态
> 汇总各渠道识别的重要联系人。
> 最后更新: {ts}

## 学术合作者

## 期刊编辑

## 基金机构

## 其他重要联系人

""",
}

# 段落标题 → 所属文件映射
_SECTION_TO_FILE = {
    "## 身份与职业":     "profile",
    "## 专业领域":       "profile",
    "## 当前目标与计划": "profile",
    "## 行为模式":       "profile",
    "## 重要联系人":     "emails",
    "## 项目进展":       "emails",
    "## 关注话题":       "files",
    "## 专业知识":       "files",
    "## 常用联系人":     "wechat",
    "## 社交模式":       "wechat",
    "## 明确指示":       "user",
    "## 个人偏好":       "user",
    "## 待办与提醒":     "user",
    "## 学术合作者":     "contacts",
    "## 期刊编辑":       "contacts",
    "## 基金机构":       "contacts",
    "## 其他重要联系人": "contacts",
}


# ────────────────────────── 基础读写 ──────────────────────────────────

def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _read(source: str) -> str:
    path = FILES[source]
    if path.exists():
        return path.read_text(encoding="utf-8")
    tmpl = _TEMPLATES.get(source, "# {source}\n> 最后更新: {ts}\n\n")
    return tmpl.format(ts=_now(), email=config.NETEASE_EMAIL, source=source)


def _write(source: str, content: str):
    path = FILES[source]
    path.parent.mkdir(parents=True, exist_ok=True)
    # 更新时间戳
    content = re.sub(r"> 最后更新: .+", f"> 最后更新: {_now()}", content)
    path.write_text(content, encoding="utf-8")


# ────────────────────────── 段落操作 ──────────────────────────────────

def _get_section_lines(content: str, section_title: str) -> list[str]:
    lines = content.splitlines()
    in_section = False
    result = []
    for line in lines:
        if line.strip() == section_title:
            in_section = True
            continue
        if in_section:
            if line.startswith("## "):
                break
            result.append(line)
    return result


def _set_section_content(content: str, section_title: str,
                         new_lines: list[str]) -> str:
    lines = content.splitlines()
    out = []
    in_section = False
    for line in lines:
        if line.strip() == section_title:
            in_section = True
            out.append(line)
            for nl in new_lines:
                out.append(nl)
            continue
        if in_section and line.startswith("## "):
            in_section = False
        if not in_section:
            out.append(line)
    if not any(line.strip() == section_title for line in lines):
        out.append(section_title)
        out.extend(new_lines)
    return "\n".join(out) + "\n"


def _add_bullet(source: str, section_title: str, item: str,
                max_items: int = 50) -> bool:
    """向指定来源文件的指定段落添加 bullet，返回是否新增"""
    content = _read(source)
    existing = _get_section_lines(content, section_title)
    bullets = [l for l in existing if l.strip().startswith("-")]
    item_line = f"- {item}"
    if item_line in bullets:
        return False  # 已存在
    bullets.append(item_line)
    if len(bullets) > max_items:
        bullets = bullets[-max_items:]
    new_lines = [""] + bullets + [""]
    content = _set_section_content(content, section_title, new_lines)
    _write(source, content)
    return True


# ────────────────────────── 公共 API ──────────────────────────────────

def add_fact(fact: str, source: str = "emails"):
    """
    追加一条事实到对应来源文件。
    source: "emails" | "files" | "wechat" | "user"
    """
    if not fact:
        return
    section = {
        "emails":  "## 碎片事实",
        "files":   "## 碎片事实",
        "wechat":  "## 碎片事实",
        "user":    "## 明确指示",
        "profile": "## 行为模式",
    }.get(source, "## 碎片事实")
    _add_bullet(source, section, fact, max_items=100)


def set_profession(profession: str, force: bool = False):
    """设置职业（写入 profile.md）"""
    if not profession:
        return
    content = _read("profile")
    lines = _get_section_lines(content, "## 身份与职业")
    for l in lines:
        if l.strip().startswith("- 职业/方向:") and not force:
            return
    new_lines = []
    updated = False
    for l in lines:
        if l.strip().startswith("- 职业/方向:"):
            new_lines.append(f"- 职业/方向: {profession}")
            updated = True
        else:
            new_lines.append(l)
    if not updated:
        new_lines.append(f"- 职业/方向: {profession}")
    content = _set_section_content(content, "## 身份与职业", new_lines)
    _write("profile", content)


def add_expertise(domain: str, source: str = "files"):
    if not domain:
        return
    _add_bullet("profile", "## 专业领域", domain, max_items=30)
    if source != "profile":
        _add_bullet(source, "## 专业知识", domain, max_items=30)


def add_goal(goal: str):
    if not goal:
        return
    _add_bullet("profile", "## 当前目标与计划", goal, max_items=20)


def add_contact_note(note: str, source: str = "emails"):
    if not note:
        return
    section_map = {
        "emails":  "## 重要联系人",
        "wechat":  "## 常用联系人",
        "contacts": "## 其他重要联系人",
    }
    section = section_map.get(source, "## 重要联系人")
    _add_bullet(source, section, note, max_items=50)


def add_topic(topic: str, source: str = "files"):
    if not topic:
        return
    _add_bullet(source, "## 关注话题", topic, max_items=40)


def add_pattern(pattern: str):
    if not pattern:
        return
    _add_bullet("profile", "## 行为模式", pattern, max_items=20)


def add_preference(pref: str):
    """记录用户偏好（来自用户直接指令）"""
    if not pref:
        return
    _add_bullet("user", "## 个人偏好", pref, max_items=30)


def add_todo(todo: str):
    """记录待办事项（来自用户指令）"""
    if not todo:
        return
    _add_bullet("user", "## 待办与提醒", f"{_now()} — {todo}", max_items=30)


def merge_extracted(info: dict, source: str = "files"):
    """
    将 AI 提取结果合并进对应来源文件。
    source: "emails" | "files" | "wechat"
    注意: profession 不自动更新（防污染）
    """
    if not info:
        return

    for domain in (info.get("expertise") or []):
        add_expertise(domain, source=source)

    for goal in (info.get("goals") or []):
        if source in ("emails", "user"):  # 只从邮件/用户指令提取目标
            add_goal(goal)

    for topic in (info.get("topics") or []):
        add_topic(topic, source=source)

    for contact in (info.get("contacts") or []):
        add_contact_note(contact, source=source)

    if info.get("insights"):
        add_fact(info["insights"], source=source)


# ────────────────────────── 汇总摘要 ──────────────────────────────────

def get_summary(sources: list[str] | None = None) -> str:
    """
    返回所有来源的聚合摘要，用于注入 AI prompt。
    sources=None 时读取所有文件。
    """
    if sources is None:
        sources = ["profile", "emails", "files", "wechat", "user"]

    parts = []

    for src in sources:
        content = _read(src)
        src_parts = _extract_key_bullets(content, src)
        parts.extend(src_parts)

    return "\n".join(parts) if parts else "档案尚未建立"


def _extract_key_bullets(content: str, source: str) -> list[str]:
    """从单个文件提取关键 bullet（最多每类5条）"""
    parts = []

    def bullets(section: str, limit: int = 5) -> list[str]:
        lines = _get_section_lines(content, section)
        items = [l.strip().lstrip("- ") for l in lines
                 if l.strip().startswith("-")]
        return [i for i in items if i][:limit]

    if source == "profile":
        ident = _get_section_lines(content, "## 身份与职业")
        for l in ident:
            l = l.strip()
            if l.startswith("- ") and "邮箱:" not in l:
                parts.append(l[2:])
        exp = bullets("## 专业领域", 6)
        if exp:
            parts.append(f"专业领域: {', '.join(exp)}")
        goals = bullets("## 当前目标与计划", 3)
        if goals:
            parts.append(f"当前目标: {'; '.join(goals)}")
        patterns = bullets("## 行为模式", 3)
        if patterns:
            parts.append(f"行为习惯: {'; '.join(patterns)}")

    elif source == "emails":
        facts = bullets("## 碎片事实", 3)
        if facts:
            parts.append(f"邮件洞察: {'; '.join(facts[-3:])}")
        proj = bullets("## 项目进展", 3)
        if proj:
            parts.append(f"项目进展: {'; '.join(proj)}")

    elif source == "files":
        topics = bullets("## 关注话题", 5)
        if topics:
            parts.append(f"关注话题: {', '.join(topics)}")

    elif source == "wechat":
        social = bullets("## 社交模式", 2)
        if social:
            parts.append(f"社交: {'; '.join(social)}")
        facts = bullets("## 碎片事实", 2)
        if facts:
            parts.append(f"微信洞察: {'; '.join(facts[-2:])}")

    elif source == "user":
        explicit = bullets("## 明确指示", 5)
        if explicit:
            parts.append(f"用户说: {'; '.join(explicit[-3:])}")
        prefs = bullets("## 个人偏好", 3)
        if prefs:
            parts.append(f"偏好: {'; '.join(prefs)}")

    return parts


# ────────────────────────── 初始化 ────────────────────────────────────

def initialize():
    """初始化所有记忆文件（已存在则跳过）"""
    created = []
    for name, path in FILES.items():
        if not path.exists():
            _write(name, _read(name))
            created.append(name)
    if created:
        print(f"[Memory] 初始化: {', '.join(created)}")
    else:
        print(f"[Memory] 档案目录: {MEMORY_DIR}")


def list_files() -> dict[str, int]:
    """返回各记忆文件的 bullet 数量"""
    result = {}
    for name, path in FILES.items():
        if path.exists():
            content = path.read_text(encoding="utf-8")
            count = content.count("\n- ")
            result[name] = count
        else:
            result[name] = 0
    return result


def get_path() -> Path:
    return MEMORY_DIR
