```
   ___   ____  ___  ___ ____
  / _ | / __/ / _ \/ _// __/
 / __ |/ _/  / __, / _/_\ \
/_/ |_/___/ /_/ |_/___/___/
  Your machine. Your data. Your AI.
```

# Aegis — 本地优先的个人 AI 助理

[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Platform: Windows](https://img.shields.io/badge/Platform-Windows%2010%2F11-informational?logo=windows)](https://www.microsoft.com/windows)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](https://github.com/your-repo/aegis/pulls)
[![Zero Cloud](https://img.shields.io/badge/Cloud%20Dependency-Zero-critical)](https://github.com/your-repo/aegis)

> **A privacy-first personal AI assistant that reads your WeChat, email, files, and research feeds — then tells you what actually matters.**
> 100% local. No SaaS subscriptions. No data leaves your machine.

---

## Why Aegis? 为什么选 Aegis？

- **🔒 True zero-cloud privacy** — All LLM calls use your own API key against a configurable endpoint. Credentials live in a local `.credentials` file. No telemetry, no cloud sync, no vendor lock-in whatsoever.
- **💬 WeChat-native on Windows** — Decrypts SQLCipher 4 databases directly from your local WeChat installation, syncs incrementally (mtime tracking — only re-decrypts files that changed), and turns chat noise into structured action items.
- **🧠 Human-readable, git-versioned memory** — Your knowledge base is plain Markdown files in `data/memory/`. Every write is auto-committed. Browse, edit, and `git revert` without any special tooling. It outlives any app.
- **📬 Command channel from anywhere** — Prefix any email or WeChat message with `Aegis:` and you have a remote terminal: search files, draft documents, confirm pending tasks, send email — all from your phone.
- **📰 Daily briefing without LLM waste** — A rule engine scores your morning digest first (zero tokens consumed). The LLM only rewrites when the quality score falls below 7. No unnecessary API spend.

---

## Feature Showcase

| Category | What it does | Key detail |
|---|---|---|
| 💬 **WeChat sync** | Decrypt + incremental import | SQLCipher 4 via pywxdump 3.0+; mtime cache, every 15 min |
| 💬 **WeChat analysis** | Extract decisions, tasks, projects | AI batch-analyzes history; results go to pending review queue |
| 💬 **WeChat commands** | Real-time listener | wxauto monitors your Aegis account; `Aegis:` prefix triggers actions |
| 📧 **Dual inbox** | 163 + Gmail | IMAP every 30 min; importance score 1–5; instant push if ≥ 4 |
| 📧 **Email commands** | Control via email | Send `Aegis:` email to yourself — works from any device |
| 📁 **Full-text search** | SQLite FTS5 | Indexes `.md .txt .docx .pdf .py .ipynb .csv` across all scan roots |
| 📁 **Semantic search** | ChromaDB + BM25 | Hybrid dense + sparse retrieval; `--vectorize` for batch embedding |
| 📰 **Academic RSS** | Journal monitoring | Fetches new papers daily at 07:00, surfaces in briefing |
| 🧠 **Four-layer memory** | Structured KB | focus.md → people.md → projects/ → FTS5+vector KB |
| ✍️ **MemoryWriter** | Safe, auditable writes | Thread-locked, git-committed, write_log in SQLite, rollbackable |
| 🔁 **Smart dedup** | Bigram Jaccard | Prevents near-duplicate focus items from flooding your list |
| 📊 **Daily briefing** | 08:00 auto-digest | Fuses email + WeChat + RSS + papers; rule-scored first, AI only if needed |
| 🌐 **Web UI** | localhost:8077 | Chat, memory tree, pending review, settings — all in-browser |
| 📝 **Doc generation** | Word file output | `Aegis: write ...` drafts a `.docx`, auto-emails it back |
| 🐾 **Watchdog** | Auto-restart | Crash loop detection; email alert on repeated failures |
| ⚙️ **Module toggles** | `config.py MODULES` | Individually disable WeChat, Gmail, RSS, vector store, knowledge graph |

---

## Architecture 系统架构

```
┌─────────────────────────────────────────────────────────────────┐
│                        DATA SOURCES                             │
│                                                                 │
│  💬 WeChat DB         📧 163 / Gmail       📁 Local Files       │
│  (SQLCipher 4)        (IMAP every 30m)    (FTS5 + ChromaDB)    │
│  incremental 15m      score 1-5           .md .docx .pdf ...   │
│  mtime tracking       instant push ≥4    configurable roots    │
│                                                                 │
│  📰 Academic RSS        (daily 07:00)                          │
└──────────┬───────────────────┬──────────────────┬──────────────┘
           │                   │                  │
           ▼                   ▼                  ▼
┌────────────────────────────────────────────────────────────────┐
│                      PROCESSING LAYER                          │
│                                                                │
│   scanner/                email_module/    memory/             │
│   ├─ wechat_decrypt        ├─ bulk_proc    ├─ fts_store        │
│   ├─ wechat_analyzer       ├─ sender       ├─ vector_store     │
│   ├─ wechat_memory_builder └─ IMAP/SMTP    ├─ bm25_store       │
│   ├─ email_memory_builder                  ├─ knowledge_graph  │
│   ├─ vectorizer                            └─ pending          │
│   └─ rss_monitor                                               │
│                                                                │
│          Volcano Engine Doubao LLM  (OpenAI-compatible)        │
│          Rule engine (0 tokens) → AI only when score < 7       │
└──────────────────────────┬─────────────────────────────────────┘
                           │
                           ▼
┌────────────────────────────────────────────────────────────────┐
│                   FOUR-LAYER MEMORY                            │
│                                                                │
│  L1  focus.md          ← Active todos, urgent items (≤15)      │
│  L2  people.md         ← Top-N contacts ranked by importance   │
│  L3  projects/*.md     ← Per-project context + decisions       │
│  L4  FTS5 + ChromaDB   ← Full knowledge base: emails, files,   │
│                           WeChat, docs — search < 100ms        │
│                                                                │
│  Plain Markdown · git-versioned · write_log · rollbackable     │
└──────────────────────────┬─────────────────────────────────────┘
                           │
                           ▼
┌────────────────────────────────────────────────────────────────┐
│                       USER SURFACE                             │
│                                                                │
│  🌐 Web UI :8077      📧 Daily briefing 08:00   📱 Commands    │
│  Chat interface       Rule-scored first          Aegis: <cmd>  │
│  Memory tree          AI rewrite if < 7          email/WeChat  │
│  Pending review       Fuses all sources          any device    │
│  Settings panel                                                │
└────────────────────────────────────────────────────────────────┘
```

---

## Quick Start

**Prerequisites:** Python 3.11+, Git, WeChat installed and signed in (for WeChat features; all other features work cross-platform)

**1. Clone the repository**
```bash
git clone https://github.com/your-repo/aegis.git
cd aegis
```

**2. Install dependencies**
```bash
pip install -r requirements.txt
```

**3. Configure credentials**

Create `.credentials` in the project root (never committed to git):
```ini
# Volcano Engine — Doubao LLM
VOLC_API_KEY=your_key_here
VOLC_MODEL=doubao-seed-2-0-lite-260215
VOLC_API_BASE=https://ark.cn-beijing.volces.com/api/v3

# 163 Email (IMAP auth code, not login password)
NETEASE_EMAIL=you@163.com
NETEASE_AUTH_CODE=your_imap_auth_code

# Gmail (optional — set email_gmail: false in config.py to skip)
GMAIL_EMAIL=you@gmail.com
GMAIL_APP_PASSWORD=your_app_password

# Identity — used in briefing greeting and contact filtering
OWNER_NAME=你的名字
OWNER_FULL_NAME=完整中文姓名
OWNER_EN_NAME=Your Name
AEGIS_WXID=wxid_xxxxxxxxxx
```

**4. Initialize — builds the database, memory layers, git repo, and scans disk**
```bash
python main.py --init
```

**5. (Optional but recommended) Bulk-sync history and vectorize files**
```bash
python main.py --sync-emails --months 12   # pull last 12 months of email
python main.py --wechat                    # decrypt and import WeChat history
python main.py --vectorize                 # embed all indexed files into ChromaDB
```

**6. Run**
```bash
python watchdog.py          # recommended: auto-restarts on crash
# or
python main.py              # run directly
python main.py --web        # Web UI only at http://localhost:8077
```

---

## Command Channel — `Aegis:` Commands

Send the prefix `Aegis:` as the **first line** of an email subject/body, or as a WeChat message to your configured `AEGIS_WXID`. Aegis picks it up on the next check cycle.

```
Aegis: 今天有什么需要处理的？
  → Returns current focus.md + top pending items.

Aegis: search 季度汇报 合同
  → Hybrid FTS5 + vector search. Returns top results
    with source path, relevance score, and excerpt.

Aegis: 帮我给张三发邮件，说明天开会时间改了
  → Drafts and sends the email; logs to write_log.

Aegis: write 项目进展汇报 A项目80%完成，B项目延期到5月
  → Generates a structured .docx file, emails it back.

Aegis: confirm 1,3,5
  → Approves pending items #1, #3, #5 from the review
    queue, committing them to the appropriate memory layer.

Aegis: report
  → Generates a full system status report: email count,
    WeChat messages, indexed files, top contacts, write log.
    Saves as data/状态报告_YYYY-MM-DD.md and emails you.
```

---

## Memory System — Four Layers

All writes go through `memory/writer.py` — serialized with a thread lock, recorded to `write_log` in SQLite, and auto-committed to git. Every change is auditable and rollbackable with `git revert`.

```
data/memory/                          (independent git repo)
│
├── focus.md              ← L1: Active todos and decisions
│   "- [ ] Review Q3 budget (src: 张总 WeChat, 2024-11-08)"
│   Dedup via bigram Jaccard — no near-duplicate entries
│   Aging: items older than threshold auto-archived
│
├── people.md             ← L2: Top-N contacts by importance score
│   Rebuilt from contacts/*.md; low-scorers rotated out
│   Shows role, last contact, key relationship notes
│
├── contacts/
│   ├── wx_zhangsan.md    ← Per-contact WeChat profile
│   └── email_lisi.md     ← Per-contact email profile
│
├── projects/
│   ├── INDEX.md          ← Project directory with status overview
│   └── project_name.md   ← Decisions, history, key facts per project
│
├── groups/
│   └── groupname.md      ← WeChat group profiles and activity summary
│
├── personal/
│   └── background.md     ← User profile: research areas, institutions
│
├── wechat_active.md      ← Recently active WeChat conversations
│
└── archive/              ← Completed focus items, stale projects
```

<details>
<summary>MemoryWriter internals</summary>

All memory writes go through `memory/writer.py`:

1. Acquires an `RLock` (thread-safe, reentrant)
2. Runs `git status` — detects and commits any manual edits before applying programmatic changes
3. Applies the write (append / overwrite / patch)
4. Inserts a row into `write_log` (timestamp, source, operation, target, content snippet)
5. Calls `git commit -m "..."` on `data/memory/`
6. In batch mode: aggregates multiple writes into a single commit to keep git history clean

To roll back any change: `git -C data/memory log --oneline` then `git -C data/memory revert <hash>`

</details>

---

## Web UI — localhost:8077

```bash
python main.py --web            # default port 8077
python main.py --web --port 8080
```

| Panel | What's there |
|---|---|
| **Chat** | Conversational interface with full memory context injection. Ask about people, projects, emails, or files. Streaming SSE output. |
| **Memory Tree** | Browse `data/memory/` as an interactive file tree. Click any `.md` to read inline. |
| **Pending Review** | Queue of AI-extracted items awaiting your confirmation — tasks, contacts, project updates. Approve or reject with one click; approved items go straight to the relevant memory layer. |
| **Settings** | Toggle modules (WeChat, Gmail, RSS, vector store), adjust scheduler intervals, set briefing time. Changes write to `data/settings.json` and take effect immediately. |

> Web UI binds to `127.0.0.1` by default (local access only). Set `JARVIS_WEB_TOKEN` in `.credentials` to enable Bearer Token authentication for LAN access.

---

## Scheduled Jobs

| Time | Job |
|---|---|
| Every 2 min | WeChat command DB poll (immediate `Aegis:` response) |
| Every 15 min | WeChat incremental sync (mtime-tracked, only changed DBs) |
| Every 30 min | Email check — 163 + Gmail; process `Aegis:` email commands |
| Every 30 min | Scan active WeChat contacts; extract focus items |
| Daily 07:00 | Fetch academic RSS feeds |
| Daily 08:00 | **Daily briefing** — fuse all sources, rule-score, AI rewrite if < 7 |
| Daily 19:00 | Focus updater — extract action items from email + WeChat |
| Daily 03:00 | File index update + DB backup + auto-approve pending items |
| Sunday 09:00 | Weekly status report email |
| Sunday 03:00 | Memory aging — archive expired focus items and stale pending |
| 1st of month 04:00 | Personal background memory refresh |

> All intervals are configurable in the Web UI Settings panel — writes to `data/settings.json`, effective immediately.

---

## Tech Stack

| Component | Technology |
|---|---|
| **Language** | Python 3.11 |
| **Web framework** | FastAPI + Uvicorn (SSE streaming) |
| **Scheduler** | APScheduler 3.x |
| **Primary database** | SQLite (WAL mode) — `data/jarvis.db` |
| **Full-text search** | SQLite FTS5 |
| **Vector search** | ChromaDB 0.5+ |
| **Sparse retrieval** | BM25 (`memory/bm25_store.py`, no external deps) |
| **WeChat decryption** | pywxdump 3.0+ (SQLCipher 4 AES-CBC) |
| **WeChat automation** | wxauto 3.9+ (real-time message listener) |
| **Document parsing** | python-docx, pdfplumber, openpyxl, chardet |
| **Document generation** | python-docx (`.docx` with tables on command) |
| **LLM backend** | Volcano Engine Doubao (OpenAI-compatible API) |
| **Memory versioning** | Git (auto-committed by MemoryWriter on every write) |
| **Email** | imaplib + smtplib (stdlib, zero extra deps) |
| **Watchdog** | Custom `watchdog.py` — crash loop detection + email alert |

---

## Roadmap

- [ ] **Multi-LLM backend** — plug-and-play support for Ollama (fully local), OpenAI, and Gemini alongside Doubao; configurable per-task model routing
- [ ] **macOS / Linux packaging** — WeChat decryption is Windows-only by nature, but all other modules are cross-platform today; Docker + systemd packaging for headless server deployment
- [ ] **Vector re-ranking** — add a cross-encoder re-ranker stage after BM25 + dense retrieval for higher-precision hybrid search
- [ ] **Calendar integration** — read local calendar events into the daily briefing; auto-detect scheduling conflicts extracted from email and WeChat
- [ ] **Mobile companion** — lightweight iOS/Android app for receiving briefings and sending `Aegis:` commands without a full email client

---

## Screenshots 截图

> 📸 Screenshots coming soon — Web UI chat panel, daily briefing email, memory tree viewer, and pending review queue.

---

## Contributing

Aegis is a personal project built for real daily use. Contributions that improve stability, cross-platform support, or privacy guarantees are especially welcome.

1. Fork and branch: `git checkout -b feature/your-feature`
2. Make changes; ensure `python main.py --init` and `python main.py --test-email` still pass
3. Open a PR with a clear description of what changed and why

**Areas that would benefit most:**
- macOS WeChat decryption path handling
- Unit tests for `memory/writer.py` and `scheduler/jobs.py`
- Docker / systemd packaging for headless Linux deployment
- Cross-encoder re-ranker for search quality improvement

---

## License

MIT © see [LICENSE](LICENSE)

---

<sub>Built for power users who want their AI assistant to know their actual life — not a sanitized cloud profile. 为了那些希望 AI 真正了解自己生活的人而建。</sub>
