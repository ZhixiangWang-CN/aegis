# Aegis AI 助理 — 部署与使用说明

> 版本: v3.0 | 适用系统: Windows 10/11  
> 本文档覆盖从零开始的完整部署流程，以及迁移到新设备的操作步骤。

---

## 目录

1. [系统概览](#1-系统概览)
2. [环境准备](#2-环境准备)
3. [凭证配置](#3-凭证配置)
4. [首次初始化](#4-首次初始化)
5. [微信数据接入](#5-微信数据接入)
6. [启动与日常运行](#6-启动与日常运行)
7. [Web UI](#7-web-ui)
8. [常用命令速查](#8-常用命令速查)
9. [自动开机启动](#9-自动开机启动)
10. [数据迁移](#10-数据迁移)
11. [目录结构说明](#11-目录结构说明)
12. [故障排查](#12-故障排查)

---

## 1. 系统概览

Aegis是一套本地运行的个人 AI 助理，主要功能：

| 功能 | 说明 |
|------|------|
| 邮件监控 | 每30分钟检查 163 + Gmail，AI 分析重要性，重要邮件即时推送 |
| 每日简报 | 每天 08:00 生成并发邮件，融合邮件 + 微信 + 学术RSS |
| 微信记忆 | 从微信聊天记录提取联系人档案、群聊档案、近期待办 |
| 文件索引 | 扫描本地磁盘，建立元数据索引和向量语义搜索 |
| 指令通道 | 通过给自己发邮件或微信"文件传输助手"发指令控制系统 |
| Web UI | 浏览器访问 `http://localhost:8077`，含聊天/记忆/邮件/搜索 |
| 自动重启 | watchdog.py 崩溃自动重启，频繁崩溃时发邮件告警 |

**数据存储位置**：所有数据存放在 `data/` 目录下，迁移时只需搬运该目录即可。

---

## 2. 环境准备

### 2.1 Python 版本

需要 **Python 3.10+**（推荐 3.11）。

```bash
python --version  # 确认版本
```

### 2.2 安装依赖

```bash
cd E:\codes\Aegis
pip install -r requirements.txt
```

> **注意**：`pywxdump` 依赖较重，如不需要微信功能可从 requirements.txt 中注释掉 `pywxdump` 和 `wxauto`。

### 2.3 可选：Git（用于记忆层版本管理）

记忆层文件会自动 git commit，建议安装 Git：
- 下载：https://git-scm.com/download/win
- 安装后无需额外配置，系统自动初始化仓库

---

## 3. 凭证配置

在项目根目录创建 `.credentials` 文件（不要提交到 git）：

```ini
# 火山引擎 Doubao API
VOLC_API_KEY=你的API密钥
VOLC_API_BASE=https://ark.cn-beijing.volces.com/api/v3
VOLC_MODEL=doubao-seed-2-0-lite-260215

# 网易163邮箱（SMTP/IMAP 授权码，不是登录密码）
NETEASE_EMAIL=你的邮箱@163.com
NETEASE_AUTH_CODE=你的授权码

# Gmail（可选，App Password）
GMAIL_EMAIL=你的邮箱@gmail.com
GMAIL_APP_PASSWORD=你的应用专用密码
```

### 3.1 获取163授权码

1. 登录 163 邮箱网页版
2. 设置 → POP3/SMTP/IMAP → 开启 IMAP 服务
3. 生成"授权码"（不是登录密码）

### 3.2 获取 Gmail App Password

1. Google 账号 → 安全性 → 两步验证（需先开启）
2. 搜索"应用专用密码" → 选择"邮件" → 生成16位密码

### 3.3 获取火山引擎 API Key

1. 访问 https://console.volcengine.com/ark
2. 创建 API Key
3. 在"模型推理"页面创建接入点，获取模型 ID（即 `VOLC_MODEL`）

---

## 4. 首次初始化

```bash
python main.py --init
```

该命令执行：
1. 初始化 SQLite 数据库（`data/jarvis.db`）
2. 创建记忆层目录结构（`data/memory/`）
3. 初始化 git 仓库（记忆版本管理）
4. 扫描本地磁盘目录索引（`config.py` 中 `SCAN_ROOTS` 指定）

> 初始化后建议立即测试邮件配置：
> ```bash
> python main.py --test-email
> ```

### 4.1 修改扫描目录

编辑 `config.py` 中的 `SCAN_ROOTS` 列表：

```python
SCAN_ROOTS = [
    "C:/Users/你的用户名/Documents",
    "C:/Users/你的用户名/Desktop",
    "D:/",
    # 根据实际情况修改
]
```

### 4.2 同步历史邮件

```bash
# 同步最近12个月的邮件（默认）
python main.py --sync-emails

# 同步最近3个月
python main.py --sync-emails --months 3
```

首次同步会批量处理所有历史邮件，可能需要几十分钟。同步完成后自动构建邮件记忆。

---

## 5. 微信数据接入

微信数据接入需要在**微信运行时**执行，使用 pywxdump 工具解密本地数据库。

### 5.1 导入微信聊天记录

```bash
# 确保微信已登录并在前台运行
python main.py --wechat
```

如微信未运行但已知密钥：
```bash
python main.py --wechat --wx-key 你的微信密钥
```

### 5.2 构建微信记忆

导入完成后，用 AI 分析聊天记录，生成联系人档案：

```bash
# 分析所有聊天，最多处理前50个联系人/群
python main.py --build-wechat-memory

# 只分析最近90天，处理前30个
python main.py --build-wechat-memory --wx-days 90 --wx-top 30
```

生成文件：
- `data/memory/contacts/wx_*.md` — 每个联系人的档案
- `data/memory/groups/*.md` — 每个群聊的档案
- `data/memory/wechat_active.md` — 近30天承诺/待办

### 5.3 微信指令通道

系统支持通过微信"文件传输助手"发送指令：

| 指令示例 | 功能 |
|---------|------|
| `日报` | 立即生成并发送今日简报 |
| `状态` | 发送系统状态报告 |
| `搜索 某个关键词` | 搜索知识库 |
| `记住 某件事` | 写入记忆 |

---

## 6. 启动与日常运行

### 6.1 正常启动

```bash
python main.py
```

启动后：
- 后台调度器开始运行（邮件检查每30分钟一次）
- 立即执行一次邮件检查
- 尝试连接微信（如微信在运行）
- 进入阻塞循环，Ctrl+C 退出

### 6.2 推荐：用 watchdog 启动（崩溃自动重启）

```bash
python watchdog.py
```

watchdog 会监控 main.py，崩溃后等待20秒自动重启。如果10分钟内崩溃8次以上，会发邮件告警。

### 6.3 定时任务说明

| 任务 | 时间 | 说明 |
|------|------|------|
| 邮件检查 | 每30分钟 | 拉取新邮件 + 处理用户指令 |
| 每日简报 | 每天 08:00 | 生成并发送今日简报 |
| RSS拉取 | 每天 07:00 | 拉取学术RSS订阅 |
| 焦点提取 | 每天 19:00 | 从邮件+微信提取当日焦点事项 |
| 夜间维护 | 每天 03:00 | 文件索引 + pending自动处理 + 数据库备份 |
| 每周报告 | 周日 09:00 | 发送系统状态周报 |
| 老化清理 | 周日 03:00 | 清理过期focus/pending条目 |
| 记忆更新 | 每月1日 04:00 | 个人背景记忆月度更新 |

---

## 7. Web UI

```bash
# 启动Web UI（默认端口8077）
python main.py --web

# 指定端口
python main.py --web --port 8080
```

访问 `http://localhost:8077`，包含以下页面：

| 页面 | 功能 |
|------|------|
| 聊天 | 与Aegis实时对话，支持知识注入 |
| 仪表盘 | 系统统计、今日摘要 |
| 记忆 | 分层记忆文件树，可在线编辑 |
| 邮件 | 查看、搜索历史邮件及AI分析 |
| 搜索 | 全文搜索知识库（邮件+文件+微信） |
| Pending | 审核待写入记忆的条目 |

> **提示**：Web UI 和主进程是独立的，可以单独启动 Web UI 只浏览数据，不会影响调度。

---

## 8. 常用命令速查

```bash
# 启动
python main.py                          # 正常运行（调度模式）
python watchdog.py                      # 带自动重启的运行（推荐）
python main.py --web                    # 仅启动Web UI

# 初始化/配置
python main.py --init                   # 首次初始化
python main.py --test-email             # 测试邮件配置

# 数据同步
python main.py --sync-emails            # 同步历史邮件
python main.py --sync-emails --months 3 # 同步最近3个月
python main.py --wechat                 # 导入微信聊天记录
python main.py --rss                    # 立即拉取RSS

# 记忆构建
python main.py --build-wechat-memory    # 构建微信三层记忆
python main.py --build-email-memory     # 构建邮件联系人记忆
python main.py --build-memory           # 构建文件记忆（personal+projects）
python main.py --scan-metadata          # 更新文件元数据索引

# 向量化
python main.py --vectorize              # 批量向量化文件
python main.py --vectorize --batch 30   # 批次大小30

# 手动触发
python main.py --briefing               # 立即生成今日简报
python main.py --focus-update           # 立即提取焦点事项
python main.py --report                 # 生成系统状态报告

# 搜索
python main.py --search "关键词"        # 命令行搜索知识库
```

---

## 9. 自动开机启动

以**管理员**权限运行：

```bash
# 注册开机自启（Windows计划任务）
python watchdog.py --install

# 同时带Web UI
python watchdog.py --web --install

# 取消自启
python watchdog.py --uninstall
```

注册后，每次开机系统会自动延迟30秒后启动Aegis。

验证任务是否注册成功：
```bash
schtasks /Query /TN "Aegis_Watchdog"
```

---

## 10. 数据迁移

### 10.1 需要迁移的内容

| 目录/文件 | 说明 | 是否必须 |
|-----------|------|---------|
| `data/` | 所有数据（数据库、记忆、日志） | **必须** |
| `.credentials` | API密钥和邮箱配置 | **必须** |
| `config.py` | 扫描目录等配置 | 建议修改 |
| 代码目录 | 整个项目目录 | **必须** |

### 10.2 迁移步骤

**在旧设备上：**

1. 停止Aegis（Ctrl+C 或关闭watchdog）
2. 打包 `data/` 目录（包含数据库、记忆文件）
3. 保存 `.credentials` 文件

**在新设备上：**

```bash
# 1. 复制项目代码到新位置
# 2. 恢复 data/ 目录
# 3. 复制 .credentials 文件

# 4. 安装依赖
pip install -r requirements.txt

# 5. 修改 config.py 中的扫描路径
#    SCAN_ROOTS 改为新设备的实际路径

# 6. 初始化（只需运行一次，会跳过已有数据）
python main.py --init

# 7. 测试
python main.py --test-email

# 8. 启动
python watchdog.py
```

### 10.3 SQLite 数据库路径

默认数据库路径：`data/jarvis.db`（相对于项目根目录）。
迁移时直接复制该文件即可，无需任何修改。

### 10.4 微信数据重建

微信聊天记录无法直接迁移（加密绑定设备）。在新设备上需要：
1. 确保微信在新设备上登录
2. 重新运行 `python main.py --wechat` 解密导入
3. 重新运行 `python main.py --build-wechat-memory` 构建记忆

---

## 11. 目录结构说明

```
Aegis/
├── main.py                    # 主入口，所有命令行参数
├── watchdog.py                # 进程守护，崩溃自动重启
├── config.py                  # 全局配置（路径、模块开关）
├── .credentials               # 凭证文件（不提交git）
├── requirements.txt           # Python依赖
│
├── ai/
│   └── client.py              # AI接口（火山引擎Doubao）
│
├── email_module/
│   ├── reader.py              # 163邮件读取
│   ├── gmail_reader.py        # Gmail读取
│   ├── sender.py              # 邮件发送
│   ├── summarizer.py          # AI邮件分析
│   └── command_handler.py     # 邮件指令处理
│
├── memory/
│   ├── db.py                  # SQLite数据库操作
│   ├── layers.py              # 记忆层文件管理
│   ├── writer.py              # 记忆写入（含git commit）
│   ├── pending.py             # 待审核队列
│   ├── aging.py               # 记忆老化清理
│   └── fts_store.py           # 全文搜索索引
│
├── scanner/
│   ├── directory_indexer.py   # 磁盘目录索引
│   ├── file_metadata_indexer.py # 文件元数据索引
│   ├── vectorizer.py          # 文件向量化
│   ├── wechat_decrypt.py      # 微信解密导入
│   ├── wechat_memory_builder.py # 微信三层记忆构建
│   ├── email_memory_builder.py  # 邮件记忆构建
│   ├── memory_builder.py      # 文件记忆构建
│   └── rss_monitor.py         # 学术RSS监控
│
├── scheduler/
│   ├── jobs.py                # 定时任务定义
│   ├── focus_updater.py       # 焦点事项提取
│   └── wechat_commander.py    # 微信指令通道
│
├── web/
│   ├── app.py                 # FastAPI后端
│   └── static/index.html      # Web UI前端
│
├── data/                      # 所有运行时数据（迁移时整体打包）
│   ├── jarvis.db              # SQLite主数据库
│   ├── fts_index.db           # 全文搜索索引
│   ├── chroma/                # 向量数据库
│   ├── logs/                  # 运行日志
│   └── memory/                # 分层记忆文件（git仓库）
│       ├── personal/
│       │   └── background.md  # 个人背景
│       ├── projects/          # 项目档案
│       ├── contacts/          # 联系人档案
│       ├── groups/            # 群聊档案
│       ├── from_emails.md     # 邮件提取的记忆
│       ├── from_wechat.md     # 微信汇总记忆
│       ├── from_files.md      # 文件扫描记忆
│       ├── wechat_active.md   # 近期微信活跃事项
│       └── INDEX.md           # 记忆索引
│
└── docs/
    ├── Aegis系统设计文档.md
    └── 部署与使用说明.md      # 本文档
```

---

## 12. 故障排查

### 12.1 启动报错：找不到 .credentials

```
FileNotFoundError: 找不到凭证文件: .../.credentials
```

**解决**：在项目根目录创建 `.credentials` 文件，参考第3节。

---

### 12.2 邮件发送失败

```
[Send] 发送失败: ...
```

**检查**：
1. `.credentials` 中的 `NETEASE_AUTH_CODE` 是否正确（是授权码不是密码）
2. 163邮箱是否开启了 IMAP/SMTP 服务
3. 运行 `python main.py --test-email` 测试

---

### 12.3 Windows 终端乱码/报错

```
UnicodeEncodeError: 'gbk' codec can't encode character
```

**解决**：`main.py` 顶部已处理，如仍有问题，在终端执行：
```bash
chcp 65001
```
或在 Windows 设置中将系统区域设置为 UTF-8。

---

### 12.4 微信导入失败

```
[WxDecrypt] 获取密钥失败
```

**检查**：
1. 微信是否正在运行且已登录
2. 以管理员权限运行脚本
3. pywxdump 版本是否支持当前微信版本（参考 `vendor/wechat-decrypt/README.md`）

---

### 12.5 端口被占用（Web UI）

```
[Errno 10048] error while attempting to bind on address ('0.0.0.0', 8077)
```

**解决**：
```bash
# 查找占用端口的进程
netstat -ano | findstr :8077
# 终止进程（替换PID）
taskkill /F /PID 进程PID
# 或换端口
python main.py --web --port 8078
```

---

### 12.6 频繁崩溃

查看 watchdog 日志：
```
data/logs/watchdog.log
```

查看主进程日志：
```
data/logs/jarvis.log
```

常见原因：
- API Key 失效或余额不足（Doubao）
- 网络连接问题（邮件/API请求超时）
- SQLite 数据库锁（多实例冲突，确保只运行一个实例）

---

### 12.7 记忆文件写入冲突

如果多次运行初始化或并发写入导致记忆混乱，可以查看：
```bash
# 查看记忆目录的git历史
cd data/memory
git log --oneline -20
# 回滚到某个时间点
git checkout <commit_hash> -- .
```

---

## 附：模块开关

`config.py` 中可以按需开启/关闭功能模块：

```python
MODULES = {
    "email_163":       True,   # 163邮件（建议开启）
    "email_gmail":     False,  # Gmail（需要App Password）
    "wechat":          False,  # 微信（需要pywxdump + 微信运行）
    "rss":             True,   # 学术RSS
    "disk_scan":       True,   # 本地文件扫描
    "vector_store":    True,   # 向量语义搜索
    "fts_store":       True,   # 全文搜索
    "knowledge_graph": False,  # 知识图谱（实验性）
}
```
