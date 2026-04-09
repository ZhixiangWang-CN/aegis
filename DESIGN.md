# Aegis AI 助理 — 数据地基设计（v3）

> 版本: v3.1 | 日期: 2026-04-09
> 定位：个人使用，功能可以迭代加，**数据层一次打好**
> 与 v2 的关系：功能层设计不变，本文档专注补全 v2 缺失的数据基础设施

---

## 〇、设计哲学

```
功能是叶子，数据是根。
叶子掉了明年还能长，根烂了整棵树就完了。
```

三条原则：
1. **Markdown 是主人，数据库是仆人** — 人类可读文件永远是 ground truth，数据库只是加速查询的索引
2. **每一次写入都可追溯可回滚** — git 管理 memory 目录，SQLite 用 WAL + 定时备份
3. **模块可插拔** — 微信坏了不影响邮件，向量库挂了还有 FTS5，任何单点故障不毁全局

---

## 一、存储架构总览

```
data/
├── memory/                          ← git 仓库（自动 commit）
│   ├── self.md
│   ├── focus.md
│   ├── people.md
│   ├── decisions.md
│   ├── projects_overview.md
│   └── projects/
│       ├── 超声组学.md
│       └── ...
│
├── jarvis.db                        ← SQLite 主库（WAL 模式）
├── jarvis.db.backup-YYYY-MM-DD      ← 每日自动备份
├── chroma/                          ← 向量库
├── wechat_decrypted/                ← 微信解密数据（临时）
│
└── logs/
    ├── writes.jsonl                 ← 所有写入操作日志
    └── jarvis.log                   ← 运行日志
```

### 关键决策：为什么不用纯数据库？

个人助手的核心价值是**你随时能打开文件看到全貌**。数据库查询有门槛，Markdown 没有。
所以架构是：**Markdown 为主存 + SQLite 为索引/元数据 + Chroma 为语义检索**。三者之间的一致性由 writer 模块保证。

---

## 二、SQLite 主库完整 Schema

### 2.1 核心元数据表

```sql
-- 启用 WAL 模式（并发读写安全）
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-----------------------------------------------------
-- 统一写入日志：所有对记忆层的变更都经过这里
-----------------------------------------------------
CREATE TABLE write_log (
    id          INTEGER PRIMARY KEY,
    ts          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime')),
    target      TEXT NOT NULL,         -- 'focus.md' / 'people.md' / 'projects/xxx.md' / 'layer4'
    operation   TEXT NOT NULL,         -- 'append' / 'update' / 'delete' / 'archive'
    source      TEXT,                  -- 'email' / 'wechat' / 'manual' / 'aging' / 'system'
    content     TEXT,                  -- 变更内容摘要（方便搜索回溯）
    detail_json TEXT,                  -- 完整变更数据（JSON）
    reverted    INTEGER DEFAULT 0      -- 是否已回滚
);

CREATE INDEX idx_write_log_ts ON write_log(ts);
CREATE INDEX idx_write_log_target ON write_log(target);

-----------------------------------------------------
-- 暂存审核队列
-----------------------------------------------------
CREATE TABLE memory_pending (
    id              INTEGER PRIMARY KEY,
    source          TEXT NOT NULL,       -- email / wechat / wechat_group / rss / manual / system
    source_ref      TEXT,                -- 来源引用（邮件 message_id / 微信 msg_id）
    content         TEXT NOT NULL,       -- 给用户看的描述
    proposed_layer  TEXT NOT NULL,       -- layer1_focus / layer1_people / layer1_decision / layer2_project / layer4
    proposed_target TEXT,                -- 目标文件名
    item_type       TEXT NOT NULL,       -- focus_item / person / project_update / decision / knowledge
    item_data       TEXT NOT NULL,       -- JSON 结构化数据
    confidence      REAL NOT NULL,       -- 0.0 - 1.0
    auto_approve    INTEGER DEFAULT 0,   -- 是否允许自动审批（仅 layer4 为 1）
    extracted_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime')),
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending / approved / rejected / auto_approved / expired
    reviewed_at     TEXT,
    applied_at      TEXT,
    batch_id        TEXT,                -- 同一批提取的 batch 标识
    notes           TEXT,

    CHECK (status IN ('pending','approved','rejected','auto_approved','expired')),
    CHECK (confidence >= 0.0 AND confidence <= 1.0),
    CHECK (proposed_layer IN ('layer1_focus','layer1_people','layer1_decision','layer2_project','layer4'))
);

CREATE INDEX idx_pending_status ON memory_pending(status);
CREATE INDEX idx_pending_batch ON memory_pending(batch_id);
```

### 2.2 联系人表（统一邮件 + 微信）

```sql
-----------------------------------------------------
-- 统一联系人（一个人可能同时有邮件和微信）
-----------------------------------------------------
CREATE TABLE contacts (
    id              INTEGER PRIMARY KEY,
    display_name    TEXT NOT NULL,
    -- 标识符（可空，一个人可能只有其中一种）
    email           TEXT,
    wechat_id       TEXT,                -- 微信原始 ID
    wechat_alias    TEXT,                -- 微信备注名
    -- 角色系统
    role            TEXT DEFAULT 'unknown',
    role_confidence REAL DEFAULT 0.0,
    role_source     TEXT DEFAULT 'auto', -- 'auto' / 'manual'
    role_updated_at TEXT,
    -- 重要度（决定是否进入 people.md）
    importance      INTEGER DEFAULT 0,   -- 0-100，≥70 写入 people.md
    in_people_md    INTEGER DEFAULT 0,   -- 当前是否在 people.md 中
    -- 统计
    first_seen      TEXT,
    last_seen       TEXT,
    email_count     INTEGER DEFAULT 0,
    wechat_msg_count INTEGER DEFAULT 0,
    -- 备注
    notes           TEXT,                -- AI 生成的关系摘要
    tags            TEXT,                -- JSON 数组 ["导师","超声方向"]

    CHECK (role IN ('superior','collaborator','junior','colleague',
                    'close_personal','friend','service','unknown')),
    UNIQUE(email),
    UNIQUE(wechat_id)
);

CREATE INDEX idx_contacts_role ON contacts(role);
CREATE INDEX idx_contacts_importance ON contacts(importance DESC);

-----------------------------------------------------
-- 微信群组
-----------------------------------------------------
CREATE TABLE wechat_groups (
    id              INTEGER PRIMARY KEY,
    group_id        TEXT NOT NULL UNIQUE,
    group_name      TEXT,
    group_type      TEXT DEFAULT 'normal',  -- core / normal / noise
    type_source     TEXT DEFAULT 'auto',
    member_count    INTEGER,
    last_active     TEXT,
    notes           TEXT,

    CHECK (group_type IN ('core','normal','noise'))
);
```

### 2.3 邮件存储

```sql
-----------------------------------------------------
-- 邮件
-----------------------------------------------------
CREATE TABLE emails (
    id              INTEGER PRIMARY KEY,
    message_id      TEXT NOT NULL UNIQUE,  -- 邮件 Message-ID 头
    account         TEXT NOT NULL,          -- '163' / 'gmail'
    folder          TEXT DEFAULT 'INBOX',
    from_addr       TEXT,
    from_name       TEXT,
    to_addrs        TEXT,                   -- JSON 数组
    cc_addrs        TEXT,                   -- JSON 数组
    subject         TEXT,
    date            TEXT,                   -- ISO 格式
    -- 内容（分层存储）
    body_text       TEXT,                   -- 纯文本正文
    body_html       TEXT,                   -- HTML 正文（可选保留）
    summary         TEXT,                   -- AI 摘要
    -- 处理状态
    is_processed    INTEGER DEFAULT 0,
    is_command      INTEGER DEFAULT 0,      -- 是否为 Aegis: 指令邮件
    has_attachments INTEGER DEFAULT 0,
    attachment_info TEXT,                   -- JSON [{name, size, type}]
    -- 关联
    contact_id      INTEGER,
    thread_id       TEXT,                   -- 邮件线程标识
    extracted_items TEXT,                   -- JSON：从中提取的 pending 条目 ID 列表

    FOREIGN KEY (contact_id) REFERENCES contacts(id)
);

CREATE INDEX idx_emails_date ON emails(date DESC);
CREATE INDEX idx_emails_account ON emails(account);
CREATE INDEX idx_emails_contact ON emails(contact_id);
CREATE INDEX idx_emails_processed ON emails(is_processed);
```

### 2.4 微信消息存储

```sql
-----------------------------------------------------
-- 微信消息（仅存储有分析价值的消息，不是全量镜像）
-----------------------------------------------------
CREATE TABLE wechat_messages (
    id              INTEGER PRIMARY KEY,
    msg_id          TEXT NOT NULL UNIQUE,   -- 微信原始消息 ID
    chat_type       TEXT NOT NULL,          -- 'private' / 'group'
    chat_id         TEXT NOT NULL,          -- 对方 wechat_id 或群 group_id
    sender_id       TEXT,                   -- 发送者（群聊中区分谁说的）
    is_self         INTEGER DEFAULT 0,      -- 是否是你自己发的
    content         TEXT,
    msg_type        TEXT,                   -- text / image / file / voice / link / system
    file_name       TEXT,                   -- 文件类消息的文件名
    ts              TEXT NOT NULL,
    -- 处理
    is_processed    INTEGER DEFAULT 0,
    is_trigger      INTEGER DEFAULT 0,      -- 是否触发了提取
    trigger_reason  TEXT,                   -- @me / deadline_keyword / reply_confirm / active_speak
    summary         TEXT,                   -- AI 摘要（仅对触发消息）
    extracted_items TEXT,                   -- JSON：pending 条目 ID 列表

    FOREIGN KEY (chat_id) REFERENCES contacts(wechat_id)
);

CREATE INDEX idx_wechat_ts ON wechat_messages(ts DESC);
CREATE INDEX idx_wechat_chat ON wechat_messages(chat_id);
CREATE INDEX idx_wechat_processed ON wechat_messages(is_processed);
CREATE INDEX idx_wechat_trigger ON wechat_messages(is_trigger);
```

### 2.5 文件索引

```sql
-----------------------------------------------------
-- 硬盘文件索引
-----------------------------------------------------
CREATE TABLE file_index (
    id              INTEGER PRIMARY KEY,
    path            TEXT NOT NULL UNIQUE,
    filename        TEXT NOT NULL,
    extension       TEXT,
    size_bytes      INTEGER,
    modified_at     TEXT,
    -- 内容处理
    content_hash    TEXT,                   -- SHA256，用于检测变更
    is_indexed      INTEGER DEFAULT 0,
    chunk_count     INTEGER DEFAULT 0,
    indexed_at      TEXT,
    -- 来源标记
    source          TEXT DEFAULT 'disk',    -- disk / wechat_file / email_attachment
    source_ref      TEXT
);

CREATE INDEX idx_file_ext ON file_index(extension);
CREATE INDEX idx_file_hash ON file_index(content_hash);
```

### 2.6 FTS5 全文检索表（fts_index.db，独立库）

```sql
-- 邮件全文索引
CREATE VIRTUAL TABLE fts_emails USING fts5(
    email_id UNINDEXED,
    subject,
    body_segmented,          -- jieba 分词后的正文
    summary,
    tokenize = 'unicode61'
);

-- 微信消息索引
CREATE VIRTUAL TABLE fts_wechat USING fts5(
    msg_id UNINDEXED,
    content_segmented,
    summary,
    tokenize = 'unicode61'
);

-- 文件内容索引
CREATE VIRTUAL TABLE fts_files USING fts5(
    file_id UNINDEXED,
    filename,
    content_segmented,
    tokenize = 'unicode61'
);

-- 记忆文件索引
CREATE VIRTUAL TABLE fts_memory USING fts5(
    filepath UNINDEXED,
    content_segmented,
    tokenize = 'unicode61'
);
```

---

## 三、向量存储设计

### 3.1 Embedding 模型

```python
# 本地模型，隐私优先，不走远程 API
EMBEDDING_MODEL = "BAAI/bge-m3"   # 多语言，中文优秀，1024维
# 备选: moka-ai/m3e-base（中文专精，768维，更轻量）

from sentence_transformers import SentenceTransformer
model = SentenceTransformer(EMBEDDING_MODEL)
```

### 3.2 Chroma Collection 设计

```python
COLLECTIONS = {
    "emails":       "邮件摘要 + 正文片段",
    "wechat":       "微信消息摘要",
    "files":        "文件内容分块",
    "memory_notes": "memory/*.md 的段落",
}

# 每条 document 的统一 metadata
metadata_schema = {
    "source_type": str,   # email / wechat / file / memory
    "source_id":   str,   # 对应主库 ID
    "date":        str,   # ISO 日期，用于时间衰减排序
    "contact_id":  int,   # 关联联系人（可选）
    "project":     str,   # 关联项目名（可选）
    "chunk_index": int,
}
```

### 3.3 两阶段检索管线

```
用户查询
    ↓
jieba 分词          embedding 向量化
    ↓                    ↓
FTS5 BM25           Chroma cosine
Top-30              Top-30
    ↓                    ↓
        RRF 融合（k=60）
              ↓
        去重 + 按 date 加权
              ↓
           Top-10
              ↓
        拼装为 AI 上下文
```

```python
def rrf_merge(fts_results, vec_results, k=60):
    scores = {}
    for rank, doc_id in enumerate(fts_results):
        scores[doc_id] = scores.get(doc_id, 0) + 1 / (k + rank + 1)
    for rank, doc_id in enumerate(vec_results):
        scores[doc_id] = scores.get(doc_id, 0) + 1 / (k + rank + 1)
    return sorted(scores, key=scores.get, reverse=True)
```

---

## 四、统一写入器（MemoryWriter）

**所有对 memory/*.md 的修改必须经过此模块。**

```python
# memory/writer.py

class MemoryWriter:
    """
    职责：
    1. 串行化所有写入（RLock 可重入锁）
    2. 写入前检测用户手动编辑（git status → 先 commit [manual]）
    3. 写入后记录 write_log（SQLite）+ writes.jsonl（JSONL 冗余日志）
    4. 写入后自动 git commit
    5. 提供回滚接口（git revert）
    6. 批量写入模式（batch），减少 git 历史碎片
    """

    def write(self, target, operation, content, source="system", detail=None):
        with self._lock:
            self._check_manual_edits()             # 先处理用户手动修改
            self._apply(target, operation, content) # 实际写入
            if self._batch_mode:
                self._batch_ops.append(...)        # batch 模式：延迟提交
                return 0
            git_hash = self._git_commit(...)        # 立即 git commit
            log_id = db.log_write(...)              # 记录 write_log
            self._append_jsonl(...)                 # 追加 writes.jsonl
            return log_id

    @contextmanager
    def batch(self, batch_label: str = "batch"):
        """
        批量写入上下文：多次 write() 仅产生一次 git commit。
        适用于大批量文件更新（如刷新全部联系人档案）。

        用法：
            with get_writer().batch("wechat_contacts_20260409"):
                for contact in contacts:
                    get_writer().write(f"contacts/{contact}.md", "update", ...)
            # 退出时统一 git commit + write_log
        """

    def rollback(self, write_log_id):
        """回滚指定写入（git revert --no-commit + 重新 commit）"""

    # 支持的 operation 类型：
    # - append         追加到文件末尾
    # - update         整文件替换
    # - replace_section 替换 ## 段落
    # - delete_line    删除含关键词的行
    # - upsert_line    更新匹配行，不存在则追加
```

### 调用规范

所有业务模块必须通过 `get_writer()` 单例写入，**禁止直接调用 `path.write_text()`**：

```python
# ❌ 错误：绕过 MemoryWriter
focus_path.write_text(content, encoding="utf-8")

# ✅ 正确：通过统一入口
from memory.writer import get_writer
get_writer().write("focus.md", "update", content, source="web_ui")
```

已完成此规范化的模块：`memory/layers.py`、`memory/aging.py`、`scanner/wechat_memory_builder.py`、`scanner/email_memory_builder.py`、`web/app.py`。

---

## 五、Layer 1 Token 预算

```python
# memory/token_budget.py

LAYER1_BUDGET = 1200  # tokens 硬上限

LAYER1_SLOTS = [
    {"file": "self.md",      "max_tokens": 200,  "truncate": "tail"},
    {"file": "focus.md",     "max_tokens": 400,  "truncate": "by_priority"},
    {"file": "people.md",    "max_tokens": 300,  "truncate": "tail"},
    {"file": "decisions.md", "max_tokens": 300,  "truncate": "keep_principles"},
]

# focus.md 裁剪策略：先保留 🔴，再 🟡，再 ⏳
# decisions.md 裁剪策略：原则类永远保留，普通决策按时间裁
```

---

## 六、中文分词方案

```python
# memory/tokenizer_cn.py — jieba 替代 FTS5 trigram

import jieba
jieba.load_userdict("config/medical_dict.txt")  # 加载医学词典

def segment(text: str) -> str:
    words = jieba.cut(text, cut_all=False)
    return " ".join(w.strip() for w in words if w.strip())

# config/medical_dict.txt 示例：
# 超声组学 5 n
# 放射组学 5 n
# 肝脂肪变性 5 n
# NAFLD 5 eng
```

---

## 七、备份策略

```
Layer A: memory/ 目录 git 自动版本（每次写入一个 commit）
Layer B: SQLite 每日备份（jarvis.db → jarvis.db.backup-YYYY-MM-DD，保留7天）
Layer C: writes.jsonl 追加写日志（数据库损坏时可重建）
```

```python
def daily_backup(db_path):
    # SQLite 在线备份，不需要停服务
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(f"{db_path}.backup-{today}")
    src.backup(dst)
    cleanup_old_backups(keep_days=7)
```

---

## 八、自动审批规则

```python
# memory/auto_approve.py

AUTO_APPROVE_RULES = {
    "layer1_focus":    {"auto": False},                    # 永不自动，必须用户确认
    "layer1_people":   {"auto": False},
    "layer1_decision": {"auto": False},
    "layer2_project":  {"auto": True,                      # 条件自动
                        "min_confidence": 0.85,
                        "source_whitelist": ["email"],     # 仅邮件来源
                        "delay_hours": 4},
    "layer4":          {"auto": True,                      # 立即写入，低风险
                        "min_confidence": 0.6,
                        "delay_hours": 0},
}

# Layer 1 超过 48 小时未处理 → status = 'expired'，进入每周报告提醒
LAYER1_EXPIRE_HOURS = 48
```

---

## 九、people.md 淘汰策略

```python
PEOPLE_MD_LIMIT = 20

# importance 评分（每周重算）
def calc_importance(contact):
    score = 0
    score += recent_email_count(contact, days=30) * 3
    score += recent_wechat_count(contact, days=30) * 2
    score += {"superior": 30, "collaborator": 20, "junior": 15,
              "close_personal": 25}.get(contact.role, 0)
    score += focus_reference_count(contact) * 5
    score += project_reference_count(contact) * 3
    if contact.manually_pinned:
        score = 100  # 手动置顶，永不淘汰
    return min(score, 100)

# 已满时：新人 importance > 当前最低者 → 替换
def should_enter_people_md(contact, current_list):
    if len(current_list) < PEOPLE_MD_LIMIT:
        return contact.importance >= 70
    lowest = min(current_list, key=lambda c: c.importance)
    if contact.importance > lowest.importance:
        remove_from_people_md(lowest)
        return True
    return False
```

---

## 十、数据一致性

```
原则：Markdown 是 source of truth

启动时：
  1. git status 检测手动修改 → 先 commit [manual]
  2. 读取 .md 文件与 SQLite 元数据比对 → 以 Markdown 为准更新 SQLite
  3. Chroma 通过 content_hash 跳过重复向量化

运行时：
  MemoryWriter 保证写入顺序：
    Markdown → SQLite → Chroma
    任何步骤失败 → 回滚 + 记录错误日志

每周日 03:00：
  遍历 memory/*.md → 对比 SQLite → 修复不一致 → 写入周报
```

---

## 十一、模块可插拔

```python
# config.py
MODULES = {
    "email_163":       True,
    "email_gmail":     False,   # 需要 OAuth2 配置
    "wechat":          True,    # 需要 all_keys.json（vendor/wechat-decrypt/）
    "rss":             True,
    "disk_scan":       True,
    "vector_store":    True,
    "fts_store":       True,
    "knowledge_graph": False,   # v1 不实现
}
```

各模块对应的定时任务在 `scheduler/jobs.py` 中均有守卫：

```python
def sync_wechat_messages():
    if not config.MODULES.get("wechat"):
        return
    ...
```

关闭任一模块只需将对应值改为 `False`，无需修改任务代码。

---

## 十二、Gmail OAuth2

```python
# email_module/gmail_oauth.py
# 依赖: pip install google-auth-oauthlib google-api-python-client

SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']

def get_gmail_service():
    # 首次：浏览器授权 → 保存 token
    # 后续：自动刷新 token
    ...

# 需要在 Google Cloud Console 创建 OAuth2 Client ID
# 类型选 "Desktop App"，下载 client_secret.json 放到 .credentials/
```

---

## 十三、实现优先级

```
Phase 0 — 数据骨架（优先）
  ├── SQLite schema（write_log + 统一 contacts）
  ├── memory/ git init
  ├── MemoryWriter（含手动编辑检测）
  └── writes.jsonl 追加日志

Phase 1 — 邮件链路
  ├── 163 IMAP → emails 表
  ├── AI 摘要 → jieba → FTS5
  ├── bge-m3 → Chroma
  └── pending → 确认邮件 → MemoryWriter 写入

Phase 2 — 检索验证
  ├── RRF 两阶段检索
  ├── token_budget Layer 1 构建
  └── 手动测试搜索质量

Phase 3 — 定时任务
  ├── daily_briefing / focus_update
  ├── aging + people.md 重算
  ├── 每日 SQLite 备份
  └── 每周状态报告

Phase 4 — 微信（条件启用）
  ├── 全量导入（管理员 + process_wechat()，首次/密钥更新后）
  ├── 增量同步（sync_wechat_incremental，每15分钟，mtime 跟踪）
  │     data/wechat_sync_state.json  ← 各 message_*.db 的上次同步时间戳
  ├── 联系人档案刷新（refresh_active_contacts，每30分钟）
  │     三层 Token 优化：
  │     L1 无变化 → 跳过（0 token）
  │     L2 纯闲聊 → 只更新时间戳（0 token）
  │     L3 有触发词 → delta AI 补丁（~300 token，只改变化部分）
  └── 微信指令通道（check_wechat_commands，每2分钟）

Phase 5 — 硬盘扫描 + RSS
```

---

## 十四、v2 → v3 关键修正

| 问题 | v2 | v3 |
|------|----|----|
| 文件并发写入 | 无保护 | MemoryWriter 串行化 + RLock（可重入锁） |
| 手动编辑冲突 | 未考虑 | git status 检测，先 commit 手动修改 |
| 数据回滚 | 无 | git 版本控制 + write_log + rollback |
| Token 超限 | 无约束 | token_budget.py 硬截断 + 优先级裁剪 |
| 简报自评 Token 浪费 | AI 自评（循环偏见） | 规则评分（0 token）+ 仅低分时触发 AI |
| 自动审批风险 | 所有层 2h 自动 | Layer 1 永不自动，Layer 4 即时，Layer 2 有条件 |
| 中文检索 | FTS5 trigram | jieba 预分词 + 医学词典 |
| Embedding 模型 | 未指定 | bge-m3 本地（隐私优先） |
| Gmail 认证 | imaplib App Password | OAuth2 完整流程 |
| people.md 溢出 | 无淘汰 | importance 评分 + 自动替换最低分 |
| 数据一致性 | 未考虑 | Markdown 为准 + 启动校验 + 每周检查 |
| 备份 | 无 | git + 每日 SQLite backup + JSONL 日志 |
| 知识图谱 | 列出未定义 | v1 不实现，降低复杂度 |
| 微信同步 | 手动全量 | 增量同步（mtime 跟踪，只解密变化的 DB） |
| 微信联系人更新 | 手动触发 | 三层 Token 优化：无变化跳过→关键词门控→delta AI 补丁 |
| 模块故障隔离 | 未考虑 | config.MODULES 开关 + 定时任务内守卫 |
| MemoryWriter 绕过 | 多处直接 write_text | 所有业务模块统一经 get_writer()，write_log 可追溯 |
| 批量写入 git 碎片 | 每文件一 commit | batch() 上下文，整批仅一次 commit |
| focus.md 膨胀 | 无清理 | cleanup_focus.py + aging 老化（已完成/过期自动归档） |
| pending 积压 | 无过期机制 | cleanup_pending.py（去重 + Newsletter 过期 + 30天超期） |
| 联系人孤岛 | email/wechat 完全隔离 | merge_contacts.py（名字匹配回填 wechat_id） |
| 运行时文件缺失 | 首次启动崩溃 | --init 和启动时自动创建 settings.json + wechat_sync_state.json |
| Web UI 暴露风险 | 绑定 0.0.0.0 | 默认绑定 127.0.0.1 + 可选 JARVIS_WEB_TOKEN Bearer 认证 |
| settings 保存失败 | config.settings 模块冲突 | 独立 settings_manager.py 避免命名冲突 |

---

## 十五、运维与维护

### 日常检查

```bash
# 查看 write_log 记录数（应持续增长）
sqlite3 data/jarvis.db "SELECT COUNT(*) FROM write_log;"

# 查看最近 git 提交
cd data/memory && git log --oneline -10

# 查看 pending 积压状态
sqlite3 data/jarvis.db "SELECT status, COUNT(*) FROM memory_pending GROUP BY status;"

# 查看 focus.md 当前活跃条目数
grep -c '\- \[ \]' data/memory/focus.md
```

### 维护工具

| 工具 | 用途 | 建议频率 |
|------|------|----------|
| `tools/cleanup_focus.py` | 归档已完成/过期的焦点条目 | 每周或发现条目 >20 时 |
| `tools/cleanup_pending.py` | 清理重复/Newsletter/超期 pending | 每月或 pending > 100 时 |
| `tools/merge_contacts.py --dry-run` | 预览联系人跨源匹配 | 数据积累一段时间后 |

### 告警机制

`scheduler/jobs.py` 中的 `_check_alert(job_id, ok)` 监控关键任务：
- `wechat_sync` — 微信消息同步
- `wechat_contacts_refresh` — 联系人档案刷新

连续失败次数达到阈值（默认 3 次）时，自动发邮件告警。通过 `settings.json` 中的 `notify.sync_failure_threshold` 调整阈值。

### 回滚操作

```python
# 回滚指定 write_log ID 对应的变更
from memory.writer import rollback
rollback(log_id=42)   # git revert 对应 commit

# 也可直接 git 操作（memory/ 是独立 git 仓库）
cd data/memory
git log --oneline    # 找到目标 commit hash
git revert <hash>    # 撤销该次变更
```
