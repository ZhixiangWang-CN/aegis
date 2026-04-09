"""
记忆文件构建器

三层结构:
  personal/background.md  — 个人背景（从 Documents 提取）
  personal/skills.md      — 专业技能
  personal/ongoing.md     — 当前推进的工作/研究
  projects/{name}.md      — 每个项目一个文件（从 E:/codes 提取）
  INDEX.md                — 总览

流程（文件夹名优先策略）:
  1. 扫目录树 → 收集文件夹/文件名（速度极快，本身即含丰富信息）
  2. AI 分析文件夹名 → 推断研究领域、事件、重点目录
  3. 只精读 AI 识别出的重点文件（CV、研究基础、发表列表等）
  4. AI 整合 → personal/background.md（已校验）
  5. 定期月度更新（from_emails + from_wechat 新信息合并）
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import config
from ai import client as ai
from scanner.file_reader import read_file

MEMORY_DIR = config.DATA_DIR / "memory"
PERSONAL_DIR = MEMORY_DIR / "personal"
PROJECTS_DIR = MEMORY_DIR / "projects"

# Documents 里有价值的子目录（软件目录跳过）
_DOC_SKIP_KEYWORDS = {
    "adobe", "assassin", "bandicut", "captura", "game", "apowersoft",
    "wechat", "tencent", "zoom", "teams", "matlab", "oculus", "steam",
    "navicat", "qqpc", "upupoo", "youCalligrapher", "videowinsoft",
    "letsview", "sunlogin", "wps", "fax", "downloads", "download",
    "scanned", ".accelerate", "onedrive", "onenote", "outlook",
    "visual studio", "windowspowershell", "python scripts",
    "league of legends", "overwatch", "mount", "dyson", "flinging",
    "radiant", "slicerdicom",
}

# E:/codes 里非项目的文件/目录
_CODES_SKIP = {
    "__pycache__", ".git", "node_modules", "venv", ".venv",
    "site-packages", "dist", "build",
}

# 项目有价值的文件（优先读这些）
_PROJECT_VALUE_FILES = {
    "readme.md", "readme.txt", "readme",
    "main.py", "app.py", "run.py",
    "设计.md", "说明.md", "计划.md", "进度.md", "notes.md",
    "todo.md", "todo.txt",
}


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _should_skip_doc_dir(name: str) -> bool:
    nl = name.lower()
    return any(kw in nl for kw in _DOC_SKIP_KEYWORDS)


_FOLDER_ANALYSIS_PROMPT = """你是一个文件系统分析助手。以下是一个研究人员电脑的目录和文件名列表（来自 Documents 和个人工作目录）。

请完成两件事：
1. **从目录/文件名推断关键信息**：研究领域、重要事件（会议、申请、出国等）、正在进行的项目、可能的合作关系
2. **列出最值得精读的文件路径**（优先级从高到低，最多20条）：
   - CV / 个人介绍 / 简历
   - 研究基础整理 / 发表文章列表
   - 国自然/基金申请标书核心文件
   - 个人陈述 / 研究计划
   - 工作报告 / 学习情况报告

输出格式：
## 从目录名推断的关键信息
（分条列出，说明推断依据）

## 推荐精读文件（按优先级）
（每行一个完整路径）"""


def _scan_folder_tree(doc_roots: list[str]) -> str:
    """快速扫描目录树，返回文件夹名+关键文件名列表（不读内容）"""
    _skip = {
        "adobe", "assassin", "bandicut", "captura", "game", "apowersoft",
        "wechat", "tencent", "zoom", "teams", "matlab", "oculus", "steam",
        "navicat", "qqpc", "upupoo", "videowinsoft", "letsview", "sunlogin",
        "wps", "fax", "downloads", "download", "scanned", ".accelerate",
        "onedrive", "onenote", "outlook", "visual studio", "windowspowershell",
        "python scripts", "league of legends", "overwatch", "mount & blade",
        "dyson", "flingt", "rdb", "tdb", "sdb", "cache", "logs", "saves",
        "mods", "my games", "rockstar", "ubisoft", "pdf",  # pdf folder == citation data
    }
    allowed_file_ext = {".md", ".txt", ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx"}
    lines = []
    for root in doc_roots:
        root_p = Path(root)
        if not root_p.exists():
            continue
        lines.append(f"=== {root_p} ===")
        for dirpath, dirnames, filenames in os.walk(root_p, topdown=True):
            dirnames[:] = [
                d for d in dirnames
                if not any(k in d.lower() for k in _skip) and not d.startswith(".")
            ]
            depth = len(Path(dirpath).relative_to(root_p).parts)
            if depth > 3:
                dirnames.clear()
                continue
            rel = str(Path(dirpath).relative_to(root_p))
            if depth >= 1:
                lines.append("  " * depth + rel.split(os.sep)[-1] + "/")
            # 只在 depth<=2 时列文件（减少噪音）
            if depth <= 2:
                for fname in filenames:
                    ext = Path(fname).suffix.lower()
                    if ext in allowed_file_ext:
                        lines.append("  " * (depth + 1) + fname)
    return "\n".join(lines)


def _collect_personal_texts(doc_roots: list[str]) -> list[tuple[str, str]]:
    """
    文件夹名优先策略：
    1. 先扫目录树（只收集名称）
    2. AI分析文件夹名 → 找出重点文件
    3. 只精读重点文件（不读全部）
    返回 [(abs_path, text), ...]
    """
    print("[MemBuilder] 扫描目录树（文件夹名分析）...")
    folder_tree = _scan_folder_tree(doc_roots)

    # AI 分析目录名，找出重点文件
    priority_files: list[str] = []
    try:
        analysis = ai.chat(
            messages=[{"role": "user", "content": folder_tree}],
            system_prompt=_FOLDER_ANALYSIS_PROMPT,
            temperature=0.2,
        )
        if analysis:
            print(f"[MemBuilder] 目录分析完成")
            # 提取推荐文件路径（"## 推荐精读文件" 之后的行）
            in_files_section = False
            for line in analysis.splitlines():
                line = line.strip()
                if "推荐精读文件" in line:
                    in_files_section = True
                    continue
                if in_files_section and line.startswith("#"):
                    break
                if in_files_section and line and not line.startswith("-"):
                    # 可能是路径
                    p = Path(line.lstrip("- ").strip())
                    if p.exists():
                        priority_files.append(str(p))
                elif in_files_section and line.startswith("- "):
                    p = Path(line[2:].strip())
                    if p.exists():
                        priority_files.append(str(p))
    except Exception as e:
        print(f"[MemBuilder] 目录分析失败，降级为全扫描: {e}")

    print(f"[MemBuilder] AI 推荐精读文件: {len(priority_files)} 个")

    # 总是额外包含几个必读关键文件（CV、研究基础等）
    _MUST_READ_PATTERNS = [
        "CV", "cv", "简历", "个人介绍", "personal",
        "研究基础", "发表文章", "publication", "Publication",
        "国自然标书", "个人陈述", "研究计划", "背景",
    ]
    allowed_ext = {".md", ".txt", ".docx", ".doc", ".pdf"}
    for root in doc_roots:
        root_p = Path(root)
        if not root_p.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root_p, topdown=True):
            depth = len(Path(dirpath).relative_to(root_p).parts)
            if depth > 4:
                dirnames.clear()
                continue
            for fname in filenames:
                if Path(fname).suffix.lower() not in allowed_ext:
                    continue
                if any(kw in fname for kw in _MUST_READ_PATTERNS):
                    full = str(Path(dirpath) / fname)
                    if full not in priority_files:
                        priority_files.append(full)

    # 精读这些文件
    results = []
    seen = set()
    for fpath in priority_files[:40]:  # 最多40个
        if fpath in seen:
            continue
        seen.add(fpath)
        try:
            text = read_file(fpath)
            if text and len(text.strip()) > 50:
                results.append((fpath, text[:4000]))
                print(f"[MemBuilder] 读取: {Path(fpath).name[:50]}")
        except Exception:
            continue

    if not results:
        # 完全兜底：退化到老方式，但只扫 depth<=2
        print("[MemBuilder] 无精读文件，退化为有限全扫描...")
        allowed_ext2 = {".md", ".txt", ".docx", ".doc", ".pdf", ".rtf"}
        for root in doc_roots:
            root_p = Path(root)
            if not root_p.exists():
                continue
            for dirpath, dirnames, filenames in os.walk(root_p, topdown=True):
                depth = len(Path(dirpath).relative_to(root_p).parts)
                if depth > 2:
                    dirnames.clear()
                    continue
                for fname in filenames:
                    ext = Path(fname).suffix.lower()
                    if ext not in allowed_ext2:
                        continue
                    fp = str(Path(dirpath) / fname)
                    try:
                        text = read_file(fp)
                        if text and len(text.strip()) > 50:
                            results.append((fp, text[:3000]))
                    except Exception:
                        continue
    return results


def _collect_project_info(project_dir: Path) -> Optional[str]:
    """读取一个项目目录的关键信息，返回拼接文本"""
    chunks = []

    # 优先读 README 和 notes
    for fname in _PROJECT_VALUE_FILES:
        candidate = project_dir / fname
        if candidate.exists():
            try:
                text = read_file(str(candidate))
                if text and len(text.strip()) > 20:
                    chunks.append(f"[{fname}]\n{text[:2000]}")
            except Exception:
                pass

    # 读 .md 文件（最多3个）
    md_count = 0
    for f in project_dir.iterdir():
        if f.suffix.lower() == ".md" and f.name.lower() not in _PROJECT_VALUE_FILES:
            if md_count >= 3:
                break
            try:
                text = read_file(str(f))
                if text and len(text.strip()) > 50:
                    chunks.append(f"[{f.name}]\n{text[:1000]}")
                    md_count += 1
            except Exception:
                pass

    # 若没有文档，读最近修改的 .py 文件前几行（了解项目结构）
    if not chunks:
        py_files = sorted(project_dir.glob("*.py"),
                          key=lambda f: f.stat().st_mtime, reverse=True)
        for py in py_files[:2]:
            try:
                text = py.read_text(encoding="utf-8", errors="ignore")[:800]
                if text.strip():
                    chunks.append(f"[{py.name}]\n{text}")
            except Exception:
                pass

    return "\n\n".join(chunks) if chunks else None


_PERSONAL_PROMPT = """你是一个信息提取助手。以下是用户自己的个人文件内容（来自用户的 Documents 目录）。

请提取关于**这个用户本人**的结构化信息，包括：
- 姓名、单位、职称/职位
- 教育背景
- 研究/工作方向
- 当前推进的项目或工作
- 专业技能
- 任何重要的个人背景信息

**注意**：只提取关于文件所有者本人的信息，不要提取文献引用或他人信息。
如果某段内容明显是别人写的（如论文摘要、文献笔记），请跳过。

输出格式为 Markdown，分以下几节：
## 基本信息
## 教育背景
## 研究/工作方向
## 当前项目
## 专业技能
## 其他重要事实

只写有实质内容的节，没有信息的节直接省略。"""


_PROJECT_PROMPT = """你是一个项目信息提取助手。以下是用户某个项目目录中的文件内容。

请提取这个项目的结构化信息：
- 项目名称和一句话描述
- 项目目的/解决什么问题
- 当前状态（开发中/暂停/完成/原型）
- 技术栈
- 关键功能或模块
- 当前面临的问题或下一步计划（如有）

输出格式为 Markdown：
## 项目名称
（项目目录名）

## 一句话描述

## 目的与背景

## 当前状态

## 技术栈

## 关键功能

## 下一步/待解决

只写有实质内容的节。"""


_VERIFY_PROMPT = """以下是从用户个人文件中提取的信息。请仔细审查，标出任何**明显是其他人（非用户本人）**的信息。

规则：
- 如果某条信息明显描述的是别人（如"某某作者是...的开发者"、文献作者、项目引用者），请标注 ❌
- 如果是用户本人的信息，保留并标注 ✅
- 对于不确定的，标注 ❓ 并说明原因

直接输出审查后的内容，在每条信息前加标注。"""


def build_personal_memory(doc_roots: list[str] = None) -> str:
    """从 Documents 提取个人信息，写入 personal/ 目录，返回摘要"""
    doc_roots = doc_roots or [
        r"C:\Users\Administrator\Documents",
        r"G:\backup_documents",
        r"G:\国自然2026",
    ]

    print("[MemBuilder] 收集个人文件...")
    texts = _collect_personal_texts(doc_roots)
    print(f"[MemBuilder] 找到 {len(texts)} 个个人文件")

    if not texts:
        return "未找到个人文件"

    # 分批发给 AI（每批约 8000 字符）
    batches = []
    current, current_len = [], 0
    for rel, text in texts:
        entry = f"--- 文件: {rel} ---\n{text}\n"
        if current_len + len(entry) > 8000 and current:
            batches.append(current)
            current, current_len = [], 0
        current.append(entry)
        current_len += len(entry)
    if current:
        batches.append(current)

    all_extracted = []
    for i, batch in enumerate(batches):
        print(f"[MemBuilder] 分析个人文件批次 {i+1}/{len(batches)}...")
        content = "\n".join(batch)
        try:
            result = ai.chat(
                messages=[{"role": "user", "content": content}],
                system_prompt=_PERSONAL_PROMPT,
                temperature=0.2,
            )
            if result and result.strip():
                all_extracted.append(result.strip())
        except Exception as e:
            print(f"[MemBuilder] AI提取失败: {e}")

    combined = "\n\n---\n\n".join(all_extracted)

    # AI 核查
    print("[MemBuilder] AI核查个人信息...")
    try:
        verified = ai.chat(
            messages=[{"role": "user", "content": combined}],
            system_prompt=_VERIFY_PROMPT,
            temperature=0.1,
        )
    except Exception as e:
        verified = combined
        print(f"[MemBuilder] 核查失败，跳过: {e}")

    # 写入文件
    PERSONAL_DIR.mkdir(parents=True, exist_ok=True)
    out = PERSONAL_DIR / "background.md"
    out.write_text(
        f"# 个人背景记忆\n> 从个人文件提取 | 最后更新: {_ts()}\n"
        f"> ✅=确认是本人信息  ❌=可能是他人信息  ❓=不确定\n\n"
        + verified,
        encoding="utf-8"
    )
    print(f"[MemBuilder] 个人记忆已写入: {out}")

    # 自动校验与二次优化
    print("[MemBuilder] 开始自动校验和二次优化...")
    validate_and_optimize_personal_memory()

    return f"个人记忆提取完成，共处理 {len(texts)} 个文件"


def build_project_memories(codes_root: str = None) -> dict:
    """为 E:/codes 下每个项目创建记忆文件"""
    codes_root = Path(codes_root or r"E:\codes")
    if not codes_root.exists():
        return {"error": f"{codes_root} 不存在"}

    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    stats = {"processed": 0, "skipped": 0, "failed": 0}

    project_dirs = [
        d for d in codes_root.iterdir()
        if d.is_dir()
        and d.name not in _CODES_SKIP
        and not d.name.startswith(".")
    ]

    print(f"[MemBuilder] 找到 {len(project_dirs)} 个项目目录")

    for proj_dir in sorted(project_dirs, key=lambda d: d.stat().st_mtime, reverse=True):
        proj_name = proj_dir.name
        safe_name = re.sub(r'[<>:"/\\|?*]', '_', proj_name)
        out_path = PROJECTS_DIR / f"{safe_name}.md"

        # 已有记忆且较新（7天内）则跳过
        if out_path.exists():
            age_days = (datetime.now().timestamp() - out_path.stat().st_mtime) / 86400
            if age_days < 7:
                stats["skipped"] += 1
                continue

        info_text = _collect_project_info(proj_dir)
        if not info_text:
            stats["skipped"] += 1
            continue

        try:
            result = ai.chat(
                messages=[{"role": "user",
                           "content": f"项目目录名: {proj_name}\n\n{info_text}"}],
                system_prompt=_PROJECT_PROMPT,
                temperature=0.2,
            )
            if result and result.strip():
                out_path.write_text(
                    f"# 项目: {proj_name}\n"
                    f"> 最后更新: {_ts()} | 目录: {proj_dir}\n\n"
                    + result.strip(),
                    encoding="utf-8"
                )
                print(f"[MemBuilder] OK {proj_name}")
                stats["processed"] += 1
            else:
                stats["skipped"] += 1
        except Exception as e:
            print(f"[MemBuilder] FAIL {proj_name}: {e}")
            stats["failed"] += 1

    return stats


def build_index() -> str:
    """生成 INDEX.md 总览"""
    lines = [
        "# Aegis记忆总览",
        f"> 最后更新: {_ts()}",
        "",
        "## 个人信息",
    ]

    # Personal files
    if PERSONAL_DIR.exists():
        for f in sorted(PERSONAL_DIR.glob("*.md")):
            lines.append(f"- [{f.stem}](personal/{f.name})")
    else:
        lines.append("- （未生成，运行 --build-memory）")

    lines += ["", "## 项目记忆"]

    # Project files
    if PROJECTS_DIR.exists():
        projects = sorted(PROJECTS_DIR.glob("*.md"),
                          key=lambda f: f.stat().st_mtime, reverse=True)
        for f in projects:
            lines.append(f"- [{f.stem}](projects/{f.name})")
        if not projects:
            lines.append("- （未生成，运行 --build-memory）")
    else:
        lines.append("- （未生成，运行 --build-memory）")

    lines += ["", "## 其他来源记忆"]
    for name in ["from_emails.md", "from_wechat.md", "from_files.md"]:
        p = MEMORY_DIR / name
        if p.exists():
            lines.append(f"- [{name}]({name})")

    index_path = MEMORY_DIR / "INDEX.md"
    index_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[MemBuilder] INDEX.md 已生成: {len(projects if PROJECTS_DIR.exists() else [])} 个项目")
    return str(index_path)


_VALIDATE_OPTIMIZE_PROMPT = """你是个人知识库的质检与优化助手。下面是从多个文件中提取的关于用户本人的个人背景信息（可能存在重复、混入他人信息、或表述不准确的问题）。

请完成两项工作：

### 第一步：校验
逐条检查，标记：
- ✅ 确认是用户本人的真实信息
- ❌ 明显是他人的信息（论文作者、被引用者、第三方描述）→ 直接删除
- ❓ 不确定，保留但添加备注

### 第二步：优化整合
将通过校验的信息重新整合为一份**清晰、无重复、结构完整**的个人背景文档，格式如下：

```markdown
## 基本信息
## 教育背景（时间倒序）
## 工作与研究经历（时间倒序）
## 研究方向
## 代表性成果（论文、项目）
## 专业技能
## 其他（荣誉、资助等）
```

要求：
- 合并重复信息，保留最完整的版本
- 时间信息尽量保留
- 不要编造任何内容
- 只输出最终整合后的 Markdown，不需要解释过程"""


_MONTHLY_UPDATE_PROMPT = """你是个人背景记忆的月度更新助手。

你会收到两部分内容：
1. **当前个人背景**：已有的个人背景 Markdown 文件
2. **新增记忆来源**：最近从邮件、微信、项目文件中提炼的新信息

你的任务：
- 检查新增来源中是否有关于**用户本人**的新信息（新职位、新论文、新项目、新技能、新联系方式等）
- 将有效的新信息**合并更新**到个人背景中
- 对已有信息进行修正（如有明显过时或错误的内容）
- 保持整体结构不变，只在相应节中追加或修改

输出：更新后的完整个人背景 Markdown（包含更新日志节：`## 更新记录`，记录本次变更内容和日期）"""


def validate_and_optimize_personal_memory() -> str:
    """
    对已有的 personal/background.md 做校验 + 二次优化整合。
    去除他人信息，消除重复，提升质量。
    """
    bg_path = PERSONAL_DIR / "background.md"
    if not bg_path.exists():
        return "background.md 不存在，请先运行 --personal-only"

    current = bg_path.read_text(encoding="utf-8")
    print("[MemBuilder] 开始校验和二次优化个人背景...")

    try:
        optimized = ai.chat(
            messages=[{"role": "user", "content": current}],
            system_prompt=_VALIDATE_OPTIMIZE_PROMPT,
            temperature=0.15,
        )
    except Exception as e:
        print(f"[MemBuilder] 优化失败: {e}")
        return f"优化失败: {e}"

    if not optimized or not optimized.strip():
        return "AI 返回空结果，保留原文件"

    # 备份原文件
    backup = PERSONAL_DIR / f"background_backup_{datetime.now().strftime('%Y%m%d_%H%M')}.md"
    backup.write_text(current, encoding="utf-8")

    # 写入优化后的版本
    bg_path.write_text(
        f"# 个人背景\n> 最后更新: {_ts()} | 已校验优化\n\n"
        + optimized.strip(),
        encoding="utf-8"
    )
    print(f"[MemBuilder] 校验优化完成，备份保存至: {backup.name}")
    return "校验优化完成"


def monthly_update_personal_memory() -> str:
    """
    月度更新：从邮件记忆、微信记忆、项目记忆中提炼新信息，
    合并更新到 personal/background.md。
    """
    bg_path = PERSONAL_DIR / "background.md"
    if not bg_path.exists():
        return "background.md 不存在，请先运行 --personal-only"

    current_bg = bg_path.read_text(encoding="utf-8")

    # 收集其他记忆来源的最新内容
    sources = []
    for src_name in ["from_emails.md", "from_wechat.md"]:
        src_path = MEMORY_DIR / src_name
        if src_path.exists():
            text = src_path.read_text(encoding="utf-8")
            if text.strip():
                sources.append(f"=== {src_name} ===\n{text[:3000]}")

    # 最近30天内更新的项目记忆
    if PROJECTS_DIR.exists():
        import time
        cutoff = time.time() - 30 * 86400
        recent_projects = [
            f for f in PROJECTS_DIR.glob("*.md")
            if f.stat().st_mtime > cutoff
        ]
        for proj_f in recent_projects[:10]:  # 最多10个
            text = proj_f.read_text(encoding="utf-8")
            sources.append(f"=== 项目: {proj_f.stem} ===\n{text[:800]}")

    if not sources:
        print("[MemBuilder] 没有新的记忆来源，跳过月度更新")
        return "无新内容，跳过"

    combined_sources = "\n\n".join(sources)
    prompt_content = (
        f"## 当前个人背景\n\n{current_bg}\n\n"
        f"---\n\n## 新增记忆来源\n\n{combined_sources}"
    )

    print(f"[MemBuilder] 月度更新：检查 {len(sources)} 个来源...")

    try:
        updated = ai.chat(
            messages=[{"role": "user", "content": prompt_content}],
            system_prompt=_MONTHLY_UPDATE_PROMPT,
            temperature=0.15,
        )
    except Exception as e:
        print(f"[MemBuilder] 月度更新失败: {e}")
        return f"失败: {e}"

    if not updated or not updated.strip():
        return "AI 返回空结果，保留原文件"

    # 备份原文件
    backup = PERSONAL_DIR / f"background_backup_{datetime.now().strftime('%Y%m%d')}.md"
    backup.write_text(current_bg, encoding="utf-8")

    bg_path.write_text(
        f"# 个人背景\n> 最后更新: {_ts()} | 月度更新\n\n"
        + updated.strip(),
        encoding="utf-8"
    )
    print(f"[MemBuilder] 月度更新完成")
    return "月度更新完成"


def build_all(skip_personal: bool = False, skip_projects: bool = False) -> dict:
    """全量构建所有记忆文件"""
    results = {}

    if not skip_personal:
        print("\n=== 阶段1: 提取个人信息 ===")
        results["personal"] = build_personal_memory()

    if not skip_projects:
        print("\n=== 阶段2: 构建项目记忆 ===")
        results["projects"] = build_project_memories()

    print("\n=== 阶段3: 生成总览 ===")
    results["index"] = build_index()

    print(f"\n[MemBuilder] 全部完成: {results}")
    return results
