<div align="center">

```
   ___   ____  ___  ___ ____
  / _ | / __/ / _ \/ _// __/
 / __ |/ _/  / __, / _/_\ \
/_/ |_/___/ /_/ |_/___/___/
```

# Your Personal Digital Twin

### An AI that doesn't just answer questions — it *knows* you.
### Your people. Your decisions. Your projects. Persistent. Private. Yours.

[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Platform: Windows](https://img.shields.io/badge/Platform-Windows%2010%2F11-informational?logo=windows)](https://www.microsoft.com/windows)
[![Zero Cloud](https://img.shields.io/badge/Cloud%20Dependency-Zero-critical)](#)
[![中文文档](https://img.shields.io/badge/文档-中文版-red)](README_CN.md)

</div>

---

> **A digital twin is a living model of you** — built from your own data, running on your own machine.
> Not a chatbot you prompt from scratch every time. An entity that already knows your world.
<img width="1200" height="675" alt="image" src="https://github.com/user-attachments/assets/0d77cfce-466f-4e7c-9e4d-8f63e9c98063" />

Aegis reads your WeChat, your email, your files. It learns who matters to you, tracks every ongoing project, and remembers every decision you've made. It wakes up before you do, processes everything that happened overnight, and delivers a briefing tailored to *your* life — not a generic summary, but one that knows Zhang owes you a contract signature and that Project B slipped to May.

All of it runs locally. Nothing leaves your machine except the text summaries you explicitly send to an LLM. Your twin is yours alone.

---

## The Problem With Every AI Tool You've Tried

They're stateless. Every session starts from zero.

They don't know that Zhang always delays until the last minute. They don't know Project B slipped to May. They don't know you've been ignoring that grant deadline email for three days. They don't know *anything* — because nothing about you was ever stored anywhere they can reach.

So you repeat yourself. You re-explain context. You manually synthesize information that already exists in your own inbox and chat history. You use AI as a fancy search box instead of as something that actually knows you.

**Aegis fixes this at the root.**

It continuously reads your WeChat, your email, your files. It extracts who matters, what's urgent, what you've decided. It builds a structured, versioned, human-readable model of your entire digital life — and keeps it alive, automatically, in the background.

Ask it anything about your own world. Command it from your phone. Wake up to a briefing that already knows what happened yesterday.

This is what AI was supposed to feel like.

---

## What It Does

| Layer | What Aegis builds | How |
|---|---|---|
| **Your people** | Relationship profiles for every contact — role, history, importance, last interaction | WeChat + email, rebuilt continuously |
| **Your projects** | Per-project files with decisions, context, and status | Extracted from conversations + files, git-versioned |
| **Your priorities** | A live focus list (≤15 items) of what actually needs your attention | AI-extracted from all sources, deduped, auto-aged |
| **Your knowledge** | Full-text + semantic search across everything you've ever touched | FTS5 + ChromaDB, indexed locally |
| **Your voice** | Drafts emails, writes documents, sends briefings — in your context | Command channel via email or WeChat |
| **Your attention** | Real-time desktop notifications for important messages | Windows toast, < 1s latency via filesystem watcher |
| **Your preferences** | Learns what matters to you over time | Daily reconciliation + implicit behavioral signals |

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                      YOUR DIGITAL LIFE                       │
│                                                              │
│   💬 WeChat           📧 Email              📁 Files        │
│   Every conversation  163 + Gmail           .md .docx .pdf  │
│   Decrypted locally   IMAP, scored 1–5      FTS5 + vectors  │
│   Incremental sync    Instant push ≥4       All scan roots  │
│                                                              │
│   📰 Academic feeds   (daily, configurable)                  │
└────────────┬─────────────────────┬──────────────────────────┘
             │                     │
             ▼                     ▼
┌──────────────────────────────────────────────────────────────┐
│                    UNDERSTANDING LAYER                       │
│                                                              │
│  Who matters?     What's urgent?    What did you decide?    │
│  Contact scoring  Focus extraction  Project memory          │
│  Relationship     Dedup + aging     Decisions logged        │
│  graph building   auto-archived     git-versioned           │
│                                                              │
│         LLM (Volcano Engine / OpenAI-compatible)            │
│         Rule engine first — LLM only when needed            │
└────────────────────────┬─────────────────────────────────────┘
                         │
                         ▼
┌──────────────────────────────────────────────────────────────┐
│                    THE TWIN — FOUR LAYERS                    │
│                                                              │
│  L1  focus.md      Your live to-do list. Auto-maintained.   │
│  L2  people.md     Everyone who matters, ranked by signal.  │
│  L3  projects/     One file per project. Decisions inside.  │
│  L4  FTS5+vectors  Your complete knowledge base. <100ms.    │
│                                                              │
│  Plain Markdown · git-versioned · rollbackable · yours      │
└────────────────────────┬─────────────────────────────────────┘
                         │
                         ▼
┌──────────────────────────────────────────────────────────────┐
│                  INTELLIGENCE LAYER (learns you)             │
│                                                              │
│  importance_learner.py                                       │
│  Contact score × keyword weight × time pattern → priority   │
│  Implicit: focus completed/deleted → signal                  │
│  Daily 21:00 reconciliation: "were today's alerts useful?"   │
│  Bayesian weight update → system gets smarter every day      │
└────────────────────────┬─────────────────────────────────────┘
                         │
                         ▼
┌──────────────────────────────────────────────────────────────┐
│                      HOW YOU REACH IT                        │
│                                                              │
│  🌐 Web UI :8077    📧 Daily briefing 08:00   📱 Commands   │
│  Chat, memory tree  Rule-scored first         Aegis: <cmd>  │
│  Review queue       AI rewrites if < 7        email/WeChat  │
│  Live settings      Fuses all sources         from phone    │
└──────────────────────────────────────────────────────────────┘
```

---

## Quick Start

**Requirements:** Python 3.11+, Git, Windows 10/11 (WeChat decryption is Windows-only; all other features are cross-platform)

### 1. Clone

```bash
git clone https://github.com/ZhixiangWang-CN/aegis.git
cd aegis
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .credentials.example .credentials
# Edit .credentials — fill in your API key and email credentials
```

Minimum required:
```ini
VOLC_API_KEY=your_volcano_engine_key
NETEASE_EMAIL=you@163.com
NETEASE_AUTH_CODE=your_imap_auth_code
OWNER_NAME=Your Name
```

### 3. Initialize

```bash
python main.py --init          # Creates DB, memory layers, git repo
python main.py --sync-emails --months 6   # Pull email history
python main.py --wechat        # Decrypt and import WeChat
python main.py --vectorize     # Embed everything into ChromaDB
```

### 4. Run

```bash
python watchdog.py             # Recommended: auto-restarts on crash
# or
python main.py                 # Run directly
python main.py --web           # Web UI only at http://localhost:8077
```

---

## Command Channel

Send `Aegis:` as the first line of an email subject, or as a WeChat message to your configured account. Aegis picks it up on the next check cycle and replies.

```
Aegis: 今天有什么需要处理的？
  → Returns focus.md + top pending items

Aegis: search quarterly report contract
  → Hybrid FTS5 + semantic search, top results with excerpts

Aegis: 帮我给张三发邮件，说明天开会推迟到下午三点
  → Drafts and sends the email, logs the action

Aegis: write project status update  A项目80%完成，B项目延期到5月
  → Generates a structured .docx, emails it back to you

Aegis: confirm 1,3,5
  → Approves pending items #1, #3, #5 into memory

Aegis: report
  → Full system status: emails, contacts, indexed files, write log
```

---

## Memory: The Twin's Brain

All writes go through `memory/writer.py` — thread-locked, SQLite-logged, and auto-committed to git. Every change is auditable. Any change is reversible.

```
data/memory/                    ← independent git repo
│
├── focus.md                    ← What needs your attention RIGHT NOW
│   Deduped by bigram Jaccard · Auto-aged · ≤15 active items
│
├── people.md                   ← Everyone who matters, scored and ranked
│   Rebuilt from all contacts · Low-scorers rotated out
│
├── contacts/
│   ├── wx_zhangsan.md          ← WeChat contact: profile, history, key notes
│   └── email_lisi.md           ← Email contact: role, importance, pattern
│
├── projects/
│   ├── INDEX.md                ← All projects with status at a glance
│   └── project_name.md         ← Decisions, timeline, key facts
│
└── archive/                    ← Completed items, finished projects
```

To roll back any write:
```bash
git -C data/memory log --oneline
git -C data/memory revert <hash>
```

---

## Scheduled Jobs

| Schedule | Job |
|---|---|
| Every 2 min | WeChat command poll — instant `Aegis:` response |
| Every 15 min | WeChat incremental sync (mtime-tracked, only changed DBs) |
| Every 30 min | Email check — 163 + Gmail; process `Aegis:` commands |
| Every 30 min | Scan active contacts; extract focus items |
| Daily 07:00 | Fetch academic RSS feeds |
| Daily 08:00 | **Daily briefing** — fuse all sources, rule-score, AI only if needed |
| Daily 19:00 | Focus updater — extract action items from the day |
| Daily 21:00 | **Notification reconciliation** — summarize today's alerts, collect feedback, update importance weights |
| Daily 03:00 | File index update + DB backup + auto-approve pending |
| Sunday 03:00 | Memory aging — archive expired focus, stale pending |

> All intervals are configurable in the Web UI settings panel.

---

## Self-Learning: Getting Smarter Every Day

Most notification systems send the same alerts forever, regardless of whether you care. Aegis learns.

**The scoring pipeline** — every incoming message is scored before any AI call:
```
score = contact_useful_rate × keyword_boost × time_pattern × consecutive_msgs
```

**Two signal types collected automatically:**

| Signal | When | Weight |
|---|---|---|
| Focus item completed ✅ | You mark a task done | Strong positive |
| Focus item deleted ❌ | You remove it immediately | Strong negative |
| Quick reply detected 📱 | You reply within 30 min | Positive |
| No action in 24h ⏳ | Alert ignored entirely | Weak negative |
| Explicit feedback ✍️ | You reply `Aegis: 对账 有用 1,3 噪音 2` | Strongest signal |

**Daily reconciliation at 21:00** — Aegis sends you a report:
```
📊 Today's notification quality — April 10
Sent 8 alerts: ✅ Useful 5  ❓ Unclear 3  🔇 Noise 0

Unclear — did these matter?
  1. [14:32] Zhang San: "please confirm the contract by..."
  2. [16:45] Work group: "weekly report template attached"
  3. [20:10] Li Si: "are you free tomorrow morning?"

Reply: Aegis: 对账 有用 1,3  噪音 2
```

Weights are updated using a Bayesian rolling average — early feedback has high impact, later feedback is more conservative. Stored in `data/learned_importance.json`, fully inspectable and editable.

---

## Tech Stack

| Component | Technology |
|---|---|
| Language | Python 3.11 |
| Web framework | FastAPI + Uvicorn (SSE streaming) |
| Scheduler | APScheduler 3.x |
| Primary DB | SQLite WAL — `data/jarvis.db` |
| Full-text search | SQLite FTS5 |
| Vector search | ChromaDB 0.5+ |
| Sparse retrieval | BM25 (pure Python, no deps) |
| WeChat decryption | SQLCipher 4 AES-CBC, incremental mtime sync |
| Document parsing | python-docx, pdfplumber, openpyxl |
| LLM backend | Volcano Engine Doubao (OpenAI-compatible) |
| Memory versioning | Git — every write is a commit |
| Email | imaplib + smtplib (stdlib) |
| Watchdog | Custom crash-loop detection + email alert |

---

## Privacy

- Everything runs locally. No data leaves your machine except LLM API calls (text summaries only — never raw files or full conversations).
- `.credentials` holds your API keys and is excluded from git.
- `data/` holds all personal data and is excluded from git.
- WeChat decryption keys live in `vendor/wechat-decrypt/all_keys.json` — also excluded.
- Web UI binds to `127.0.0.1` by default. Set `JARVIS_WEB_TOKEN` to enable Bearer Token auth for LAN access.

---

## Roadmap

- [ ] **WhatsApp integration** — local DB decryption for Windows Desktop, same local-first approach as WeChat; no cloud relay, no ToS-violating automation. Contributor help welcome.
- [ ] **Ollama / local LLM** — fully offline mode, zero API calls
- [ ] **macOS / Linux** — WeChat decryption is Windows-only, but everything else works cross-platform today; Docker + systemd packaging
- [ ] **Cross-encoder re-ranking** — add a re-ranker stage after BM25 + dense retrieval
- [ ] **Calendar integration** — pull local calendar into briefings; detect scheduling conflicts from email/WeChat
- [ ] **Mobile companion** — lightweight app for briefings and `Aegis:` commands without a full email client

---

## Contributing

Aegis is built for real daily use. Contributions that improve stability, cross-platform support, or privacy guarantees are especially welcome.

1. Fork and branch: `git checkout -b feature/your-feature`
2. Ensure `python main.py --init` and `python main.py --test-email` still pass
3. Open a PR with a clear description of what changed and why

**High-value areas:**
- macOS WeChat decryption path handling
- Unit tests for `memory/writer.py` and `scheduler/jobs.py`
- Docker / systemd packaging for headless Linux
- Local LLM backend (Ollama integration)

---

## License

MIT © [ZhixiangWang-CN](https://github.com/ZhixiangWang-CN)

---

<sub>Built on the belief that your AI should know your actual life — not a sanitized, stateless prompt. Your twin. Your machine. Your data.</sub>
