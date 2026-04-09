```
   ___   ____  ___  ___ ____
  / _ | / __/ / _ \/ _// __/
 / __ |/ _/  / __, / _/_\ \
/_/ |_/___/ /_/ |_/___/___/
          你的数字分身
```

# Aegis — 你的个人数字分身

[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Platform: Windows](https://img.shields.io/badge/Platform-Windows%2010%2F11-informational?logo=windows)](https://www.microsoft.com/windows)
[![Zero Cloud](https://img.shields.io/badge/云依赖-零-critical)](#)
[![English](https://img.shields.io/badge/Docs-English-blue)](README.md)

---

*你说过的每一句话。你做过的每一个决定。你认识的每一个重要的人。*
*散落在微信、邮件、硬盘——和正在消退的记忆里。*

**Aegis 把这一切拼回来。** 它读你的聊天，认识你的人，追踪你的项目，在你自己的电脑上构建一个关于你的持久模型。完全属于你，不上云，不订阅，不泄露。

不是聊天机器人。不是搜索引擎。是你的数字分身。

---

## 你用过的每个 AI 工具，都有同一个问题

它们没有记忆。每次对话从零开始。

它们不知道张总每次都拖到最后一刻。不知道 B 项目已经延期到五月。不知道你有三天没回那封截止日期邮件。它们什么都不知道——因为关于你的一切，从来没有被存在任何它们能触达的地方。

所以你反复解释背景。反复同步上下文。你亲手去整合那些本来就躺在自己收件箱和聊天记录里的信息。你把 AI 当成一个高级搜索框，而不是一个真正**认识你**的存在。

**Aegis 从根上解决这个问题。**

它持续读取你的微信、邮件、本地文件。提取谁重要、什么紧急、你决定了什么。把你整个数字生活构建成一个结构化的、版本化的、人类可读的模型——并在后台自动持续维护它。

问它任何关于你自己世界的事。从手机上下达指令。每天早上醒来，收到一份已经知道昨天发生了什么的简报。

这才是 AI 本来应该有的样子。

---

## 它在构建什么

| 层级 | Aegis 构建的内容 | 数据来源 |
|---|---|---|
| **你的人脉** | 每个联系人的关系档案——角色、历史、重要度、最近互动 | 微信 + 邮件，持续更新 |
| **你的项目** | 每个项目独立档案，记录决策、背景、进展 | 对话+文件提取，git 版本化 |
| **你的优先级** | 实时焦点清单（≤15条），只留真正需要你注意的事 | 全源提取，自动去重，自动老化 |
| **你的知识库** | 全文+语义搜索，覆盖你接触过的所有内容 | FTS5 + ChromaDB，本地索引 |
| **你的行动力** | 代你起草邮件、生成文档、发送简报 | 指令通道，邮件或微信触发 |

---

## 系统架构

```
┌──────────────────────────────────────────────────────────────┐
│                        你的数字生活                           │
│                                                              │
│   💬 微信聊天记录      📧 邮件收发          📁 本地文件       │
│   全量解密，本地存储   163 + Gmail          .md .docx .pdf   │
│   mtime 增量同步      重要度 1-5 评分       FTS5 + 向量索引  │
│   每15分钟更新        ≥4 即时推送           可配置扫描目录    │
│                                                              │
│   📰 学术 RSS         每天 07:00 拉取                        │
└──────────┬───────────────────┬──────────────────────────────┘
           │                   │
           ▼                   ▼
┌──────────────────────────────────────────────────────────────┐
│                        理解层                                │
│                                                              │
│   谁重要？           什么紧急？          你决定了什么？       │
│   联系人评分         焦点事项提取        项目决策记录        │
│   关系图谱构建       去重 + 老化清理     git 版本管理        │
│                                                              │
│           LLM（火山引擎 Doubao / OpenAI 兼容接口）           │
│           规则引擎优先（零 token）→ 质量不足时才调用 AI      │
└──────────────────────┬───────────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────────────┐
│                    你的数字分身——四层记忆                     │
│                                                              │
│  L1  focus.md     你现在该干什么。自动维护，≤15条活跃项。    │
│  L2  people.md    所有重要的人，按信号强度排序。             │
│  L3  projects/    每个项目一个文件，决策在里面。             │
│  L4  FTS5+向量    你的完整知识库，搜索 < 100ms。            │
│                                                              │
│  纯 Markdown · git 版本控制 · 可回滚 · 人类可读 · 你的      │
└──────────────────────┬───────────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────────────┐
│                      触达方式                                │
│                                                              │
│  🌐 网页 :8077      📧 每日简报 08:00     📱 指令通道        │
│  聊天/记忆树        规则评分优先           Aegis: <指令>     │
│  待审核队列         AI 重写分数 < 7        邮件或微信        │
│  实时设置           融合全部来源           手机上就能用      │
└──────────────────────────────────────────────────────────────┘
```

---

## 快速开始

**环境要求：** Python 3.11+，Git，Windows 10/11（微信解密功能需要 Windows，其余功能跨平台）

### 1. 克隆并安装

```bash
git clone https://github.com/ZhixiangWang-CN/aegis.git
cd aegis
pip install -r requirements.txt
```

### 2. 配置凭证

```bash
cp .credentials.example .credentials
# 编辑 .credentials，填入你的 API Key 和邮箱信息
```

必填项：
```ini
VOLC_API_KEY=your_volcano_engine_key        # 火山引擎 Doubao API Key
NETEASE_EMAIL=you@163.com                   # 163 邮箱
NETEASE_AUTH_CODE=your_imap_auth_code       # IMAP 授权码（非登录密码）
OWNER_NAME=你的名字                          # 用于简报问候和联系人过滤
```

### 3. 初始化

```bash
python main.py --init                        # 建库 + 创建记忆层 + 初始化 git
python main.py --sync-emails --months 6      # 同步近6个月邮件
python main.py --wechat                      # 解密并导入微信聊天记录
python main.py --vectorize                   # 向量化所有文件
```

### 4. 启动

```bash
python watchdog.py          # 推荐：崩溃自动重启
# 或者
python main.py              # 直接运行
python main.py --web        # 仅启动 Web UI：http://localhost:8077
```

---

## 指令通道

给自己发邮件（主题第一行写 `Aegis:`），或在微信里发消息（以 `Aegis:` 开头）。下次轮询时系统解析执行，回复结果。

```
Aegis: 今天有什么需要处理的？
  → 返回当前 focus.md + 待审核重要项

Aegis: 搜索 国自然 截止日期
  → FTS5 + 向量混合搜索，返回相关内容摘要

Aegis: 帮我给张三发邮件，说明天开会推迟到下午三点
  → 起草并发送邮件，操作记入日志

Aegis: 帮我写一份项目进展汇报，A项目80%完成，B项目延期到5月
  → 生成结构化 .docx 文件，自动发到你邮箱

Aegis: 确认 1,3,5
  → 审批待审核队列中的第 1、3、5 条，写入对应记忆层

Aegis: 状态
  → 完整系统报告：邮件量、联系人、索引文件、写入日志
```

---

## 记忆体系——分身的大脑

所有写入都经过 `memory/writer.py`：线程锁保护 + SQLite 操作日志 + 自动 git commit。每次变更可追溯，任何变更可回滚。

```
data/memory/                      ← 独立 git 仓库
│
├── focus.md                      ← 你现在该做什么（实时维护）
│   bigram Jaccard 去重 · 自动老化 · ≤15条活跃项
│
├── people.md                     ← 所有重要联系人，按重要度排序
│   从全部联系人档案重建 · 低分者自动轮换出去
│
├── contacts/
│   ├── wx_zhangsan.md            ← 微信联系人：画像、往来历史、关键备注
│   └── email_lisi.md             ← 邮件联系人：角色、重要度、互动模式
│
├── projects/
│   ├── INDEX.md                  ← 所有项目一览，含状态
│   └── project_name.md           ← 决策记录、时间线、关键信息
│
└── archive/                      ← 已完成条目、结束的项目
```

回滚任意一次写入：
```bash
git -C data/memory log --oneline
git -C data/memory revert <hash>
```

---

## 自动任务时刻表

| 时间 | 任务 |
|---|---|
| 每2分钟 | 微信指令轮询——即时响应 `Aegis:` |
| 每15分钟 | 微信增量同步（mtime 跟踪，只解密有变化的 DB） |
| 每30分钟 | 邮件检查——163 + Gmail；处理 `Aegis:` 邮件指令 |
| 每30分钟 | 扫描活跃联系人，AI 提取焦点事项 |
| 每天 07:00 | 拉取学术 RSS |
| 每天 08:00 | **每日简报**——融合全部来源，规则评分，低于7分时 AI 重写 |
| 每天 19:00 | 从当天邮件+微信提取行动项 |
| 每天 03:00 | 文件索引更新 + 数据库备份 + 自动审批待处理项 |
| 每周日 03:00 | 记忆老化清理——过期焦点归档，陈旧 pending 清理 |

> 所有间隔均可在 Web UI 设置页实时调整。

---

## 隐私保障

- 所有数据本地处理，不上传任何个人信息到云端
- LLM 调用仅发送文本摘要，不发送完整文件或聊天记录原文
- `.credentials` 含 API Key，已在 `.gitignore` 中排除，**绝不提交**
- `data/` 目录含全部个人数据，已排除
- 微信数据库密钥存在本地 `vendor/wechat-decrypt/all_keys.json`，已排除
- Web UI 默认绑定 `127.0.0.1`（仅本机访问）；设置 `JARVIS_WEB_TOKEN` 可启用局域网 Bearer Token 认证

---

## 技术栈

| 组件 | 技术 |
|---|---|
| 语言 | Python 3.11 |
| Web 框架 | FastAPI + Uvicorn（SSE 流式输出） |
| 调度器 | APScheduler 3.x |
| 主数据库 | SQLite WAL 模式 |
| 全文搜索 | SQLite FTS5 |
| 向量搜索 | ChromaDB 0.5+ |
| 稀疏检索 | BM25（纯 Python，无额外依赖） |
| 微信解密 | SQLCipher 4 AES-CBC，mtime 增量同步 |
| 文档解析 | python-docx、pdfplumber、openpyxl |
| AI 接口 | 火山引擎 Doubao（OpenAI 兼容） |
| 记忆版本管理 | Git（每次写入自动 commit） |
| 邮件收发 | imaplib + smtplib（标准库，零依赖） |
| 进程守护 | 自研 watchdog.py，崩溃循环检测 + 邮件告警 |

---

## 路线图

- [ ] **本地 LLM（Ollama）** — 完全离线模式，零 API 调用
- [ ] **macOS / Linux 打包** — 微信解密是 Windows 专属，但其余模块今天就能跨平台；Docker + systemd 无头部署
- [ ] **Cross-encoder 重排序** — BM25 + 向量检索后增加精排阶段
- [ ] **日历集成** — 读取本地日历写入简报；从邮件+微信自动检测日程冲突
- [ ] **移动端伴侣** — 轻量 App，接收简报和发送 `Aegis:` 指令

---

## 参与贡献

Aegis 是一个在真实日常使用中迭代的个人项目。欢迎能提升稳定性、跨平台支持或隐私保障的贡献。

1. Fork 并新建分支：`git checkout -b feature/your-feature`
2. 确保 `python main.py --init` 和 `python main.py --test-email` 仍可通过
3. 提 PR，清楚说明改了什么、为什么

**最需要帮助的方向：**
- macOS 微信解密路径适配
- `memory/writer.py` 和 `scheduler/jobs.py` 的单元测试
- Docker / systemd 无头 Linux 部署
- Ollama 本地 LLM 后端集成

---

## License

MIT © [ZhixiangWang-CN](https://github.com/ZhixiangWang-CN)

---

<sub>为那些希望 AI 真正认识自己、而不是每次都从零开始的人而建。你的数字生活，你的分身，你的机器。</sub>
