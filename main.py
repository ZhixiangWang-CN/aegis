"""
Aegis — 启动入口

用法:
  python main.py              正常运行（后台调度 + 立即执行一次邮件检查）
  python main.py --init       初始化数据库并启动硬盘扫描
  python main.py --test-email 发送测试邮件确认配置正常
  python main.py --briefing   立即生成并发送今日简报
  python main.py --scan-only  仅执行一次硬盘扫描（不启动调度）
  python main.py --report     生成系统状态报告（保存为 Markdown 文件）
  python main.py --focus-update  立即执行 focus 更新（从邮件+微信提取焦点事项）
"""
import sys
import argparse
from datetime import datetime

# Windows 终端默认 GBK，强制 UTF-8 避免表情/特殊字符 UnicodeEncodeError
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() not in ("utf-8", "utf8"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def cmd_init():
    """初始化数据库 + 记忆层 + 启动硬盘扫描（Phase 1 目录索引）"""
    print("=" * 50)
    print("Aegis初始化")
    print("=" * 50)

    from memory.db import init_db
    init_db()

    # v3: 初始化记忆层文件 + git 仓库
    try:
        from memory.layers import initialize as init_layers
        init_layers()
        print("[Init] 记忆层初始化完成")
    except Exception as e:
        print(f"[Init] 记忆层初始化失败（非致命）: {e}")

    # v3: 确保 git 仓库
    try:
        from memory.writer import ensure_git_repo
        import config
        ensure_git_repo(config.DATA_DIR / "memory")
        print("[Init] memory/ git 仓库就绪")
    except Exception as e:
        print(f"[Init] git 初始化失败（非致命）: {e}")

    # 初始化运行时配置文件
    import json as _json
    _settings_path = config.DATA_DIR / "settings.json"
    if not _settings_path.exists():
        try:
            import settings_manager
            settings_manager.save(settings_manager.load())
            print("[Init] settings.json 已创建")
        except Exception as e:
            print(f"[Init] settings.json 创建失败: {e}")

    _sync_state_path = config.DATA_DIR / "wechat_sync_state.json"
    if not _sync_state_path.exists():
        try:
            _sync_state_path.write_text("{}", encoding="utf-8")
            print("[Init] wechat_sync_state.json 已创建")
        except Exception as e:
            print(f"[Init] wechat_sync_state.json 创建失败: {e}")

    from scanner.directory_indexer import scan_roots, save_index_snapshot
    from scanner.wechat_parser import index_wechat_files

    print("\n[1/3] 扫描硬盘目录...")
    stats = scan_roots()
    save_index_snapshot()

    print("\n[2/3] 扫描微信文件...")
    index_wechat_files()

    # v3: 尝试解密导入微信聊天记录
    print("\n[3/3] 尝试导入微信聊天记录（需微信运行中）...")
    try:
        from scanner.wechat_decrypt import process_wechat
        process_wechat()
        print("[Init] 微信聊天记录导入完成")
    except Exception as e:
        print(f"[Init] 微信导入跳过（非致命）: {e}")
        print("       提示: 运行 python main.py --wechat 可手动触发")

    print("\n初始化完成！")
    print(f"  索引文件数: {stats['indexed_files']}")
    print(f"  扫描目录数: {stats['scanned_dirs']}")
    print("\n提示:")
    print("  文件深度向量化:  python main.py --vectorize")
    print("  同步历史邮件:    python main.py --sync-emails")
    print("  启动Aegis:      python main.py")


def cmd_test_email():
    """发送测试邮件"""
    from memory.db import init_db
    init_db()
    from email_module.sender import send_email
    import config

    print("发送测试邮件...")
    ok = send_email(
        to=config.NETEASE_EMAIL,
        subject="✅ Aegis系统测试",
        body=f"Aegis已成功启动！\n\n时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n这是一封测试邮件，确认邮件收发功能正常。",
    )
    if ok:
        print(f"✅ 测试邮件已发送至 {config.NETEASE_EMAIL}")
    else:
        print("❌ 发送失败，请检查 .credentials 配置")


def cmd_briefing():
    """立即生成并发送今日简报"""
    from memory.db import init_db
    init_db()
    from scheduler.jobs import send_daily_briefing
    send_daily_briefing()


def cmd_report():
    """生成系统状态报告，保存为 data/状态报告_{date}.md"""
    from memory.db import init_db
    init_db()

    from memory import db as main_db
    from memory.layers import get_self, get_focus, OVERVIEW_PATH
    from memory.pending import count_pending
    import config

    today = datetime.now().strftime("%Y-%m-%d")
    print(f"[Report] 生成状态报告 {today}...")

    # 基础内容
    self_content = get_self()
    focus_content = get_focus()

    # 联系人 Top 20
    top_contacts = main_db.get_contacts_by_importance(min_importance=0, limit=20)
    contacts_lines = []
    for c in top_contacts:
        name = c.get("display_name", "?")
        role = c.get("role", "unknown")
        imp = c.get("importance", 0)
        contacts_lines.append(f"  [{imp:3d}] {name} — {role}")
    contacts_text = "\n".join(contacts_lines) or "暂无联系人数据"

    # 项目总览
    projects_text = ""
    if OVERVIEW_PATH.exists():
        projects_text = OVERVIEW_PATH.read_text(encoding="utf-8")

    # 统计
    pending_count = count_pending()
    try:
        with main_db.get_conn() as conn:
            email_count = conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0]
            wechat_count = conn.execute(
                "SELECT COUNT(*) FROM wechat_messages"
            ).fetchone()[0]
            file_count = conn.execute(
                "SELECT COUNT(*) FROM file_index WHERE is_indexed=1"
            ).fetchone()[0]
    except Exception:
        email_count = wechat_count = file_count = 0

    # 最近写入记录
    try:
        with main_db.get_conn() as conn:
            log_rows = conn.execute("""
                SELECT ts, target, operation, source, content
                FROM write_log
                ORDER BY ts DESC LIMIT 20
            """).fetchall()
        log_lines = [
            f"  {r['ts'][:16]} [{r['source']}] {r['operation']} {r['target']}: "
            f"{(r['content'] or '')[:60]}"
            for r in log_rows
        ]
        log_text = "\n".join(log_lines) or "暂无写入记录"
    except Exception:
        log_text = "无法获取写入记录"

    # FTS 知识库统计
    fts_text = ""
    try:
        from memory.fts_store import count as fts_count, _store
        fts_total = fts_count()
        collections = _store.collections()
        fts_parts = [f"  总文档数: {fts_total}"]
        for coll in collections:
            fts_parts.append(f"  {coll}: {fts_count(coll)} 条")
        fts_text = "\n".join(fts_parts)
    except Exception as e:
        fts_text = f"  获取失败: {e}"

    report = f"""# Aegis系统状态报告 — {today}

## 系统统计
- 邮件: {email_count} 封
- 微信消息: {wechat_count} 条
- 已索引文件: {file_count} 个
- 待审核 pending: {pending_count} 条

## 知识库 (FTS)
{fts_text}

## 我是谁
{self_content}

## 当前焦点
{focus_content}

## 项目总览
{projects_text}

## 重要联系人 (Top 20)
{contacts_text}

## 最近写入记录 (Last 20)
{log_text}
"""

    report_path = config.DATA_DIR / f"状态报告_{today}.md"
    report_path.write_text(report, encoding="utf-8")
    print(f"[Report] 报告已保存至: {report_path}")
    return report_path


def cmd_focus_update():
    """立即执行 focus 更新（从邮件+微信提取焦点事项）"""
    from memory.db import init_db
    init_db()
    from scheduler.focus_updater import run_focus_update
    stats = run_focus_update(send_email=True)
    print(f"[FocusUpdate] 完成: {stats}")


def cmd_scan_only():
    """仅执行一次硬盘扫描"""
    from memory.db import init_db
    init_db()
    from scanner.directory_indexer import scan_roots, save_index_snapshot
    from scanner.wechat_parser import index_wechat_files
    scan_roots()
    index_wechat_files()
    save_index_snapshot()


def cmd_run():
    """正常运行模式"""
    from memory.db import init_db
    init_db()

    # 确保运行时文件存在
    import config as _cfg
    _sync_state = _cfg.DATA_DIR / "wechat_sync_state.json"
    if not _sync_state.exists():
        _sync_state.write_text("{}", encoding="utf-8")
    _settings_file = _cfg.DATA_DIR / "settings.json"
    if not _settings_file.exists():
        try:
            import settings_manager as _sm
            _sm.save(_sm.load())
        except Exception:
            pass

    print("=" * 50)
    print(f"Aegis启动 @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)

    # 启动后台调度器
    from scheduler.jobs import start_scheduler, check_emails
    scheduler = start_scheduler(background=True)

    # 尝试启动微信实时监听（wxauto）
    from scheduler.wechat_commander import start_wxauto_listener
    start_wxauto_listener()

    # 启动后立即执行一次邮件检查（含微信指令轮询）
    print("\n[启动] 立即执行一次邮件+微信指令检查...")
    check_emails()

    print("\n[运行中] Aegis已启动，按 Ctrl+C 退出")
    print("  下次日报时间: 明天 08:00")
    print("  邮件检查间隔: 每15分钟\n")

    try:
        import time
        while True:
            time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        print("\nAegis已停止。")
        scheduler.shutdown()


def main():
    parser = argparse.ArgumentParser(description="Aegis AI 助理")
    parser.add_argument("--init",        action="store_true", help="初始化并扫描硬盘")
    parser.add_argument("--test-email",  action="store_true", help="发送测试邮件")
    parser.add_argument("--briefing",    action="store_true", help="立即发送今日简报")
    parser.add_argument("--scan-only",   action="store_true", help="仅执行硬盘扫描")
    parser.add_argument("--sync-emails", action="store_true", help="批量处理历史邮件（163+Gmail）")
    parser.add_argument("--months",      type=int, default=12, help="历史邮件同步月数（默认12）")
    parser.add_argument("--vectorize",   action="store_true", help="批量向量化已索引文件（深度读取）")
    parser.add_argument("--batch",       type=int, default=50, help="向量化批次大小（默认50）")
    parser.add_argument("--wechat",      action="store_true", help="解密并导入微信聊天记录（需微信运行）")
    parser.add_argument("--wx-key",      type=str, default=None, help="手动指定微信密钥（微信未运行时使用）")
    parser.add_argument("--rss",         action="store_true", help="拉取学术RSS订阅新文章")
    parser.add_argument("--kg-build",    action="store_true", help="从联系人数据构建知识图谱")
    parser.add_argument("--memory",       action="store_true", help="查看记忆文件统计")
    parser.add_argument("--search",       type=str, default=None, help="混合搜索知识库")
    parser.add_argument("--report",       action="store_true", help="生成系统状态报告")
    parser.add_argument("--focus-update",    action="store_true", help="立即执行焦点事项提取")
    parser.add_argument("--analyze-wechat",  action="store_true", help="AI批量分析历史微信聊天（提取项目/决定/任务）")
    parser.add_argument("--days",            type=int, default=None, help="analyze-wechat 分析最近N天（默认全部）")
    parser.add_argument("--build-memory",    action="store_true", help="构建记忆文件（personal/ + projects/ + INDEX.md）")
    parser.add_argument("--personal-only",   action="store_true", help="只构建个人信息记忆")
    parser.add_argument("--projects-only",   action="store_true", help="只构建项目记忆")
    parser.add_argument("--build-email-memory",  action="store_true", help="从数据库邮件批量提取记忆（联系人画像+事实）")
    parser.add_argument("--build-wechat-memory", action="store_true", help="构建微信三层记忆（联系人档案+群聊档案+近期活跃）")
    parser.add_argument("--wx-days",             type=int, default=None, help="微信记忆分析天数（默认全部）")
    parser.add_argument("--wx-top",              type=int, default=50,   help="只处理消息最多的前N个联系人/群（默认50）")
    parser.add_argument("--web",                  action="store_true", help="启动 Web UI（默认端口 8077）")
    parser.add_argument("--port",                 type=int, default=8077, help="Web UI 端口（默认 8077）")
    parser.add_argument("--scan-metadata",        action="store_true", help="扫描本地文件元数据索引")
    args = parser.parse_args()

    if args.web:
        from web.app import start_web
        start_web(port=args.port)
        return
    elif args.scan_metadata:
        from memory.db import init_db
        init_db()
        from scanner.file_metadata_indexer import scan_metadata
        print("开始元数据扫描（增量）...")
        stats = scan_metadata(incremental=True)
        print(f"完成: 扫描{stats['scanned']} 新增{stats['new']} 更新{stats['updated']} 跳过{stats['skipped']}")
        return
    elif args.init:
        cmd_init()
    elif args.test_email:
        cmd_test_email()
    elif args.briefing:
        cmd_briefing()
    elif args.scan_only:
        cmd_scan_only()
    elif args.vectorize:
        from memory.db import init_db
        init_db()
        from scanner.vectorizer import process_pending_files
        total_done = 0
        while True:
            done = process_pending_files(batch_size=args.batch)
            total_done += done
            if done == 0:
                break
        print(f"[Vectorize] 全部完成，共向量化 {total_done} 个文件")
    elif args.sync_emails:
        from memory.db import init_db
        init_db()
        from email_module.bulk_processor import process_bulk_emails, process_bulk_gmail_emails
        print(f"[sync] 开始同步 163 邮件（近 {args.months} 个月）...")
        process_bulk_emails(months_back=args.months)
        print(f"[sync] 开始同步 Gmail（近 {args.months} 个月）...")
        process_bulk_gmail_emails(months_back=args.months)
        # 同步完成后自动提取邮件记忆
        print(f"\n[sync] 邮件同步完成，开始提取邮件记忆...")
        from scanner.email_memory_builder import build_email_memory
        stats = build_email_memory()
        print(f"[sync] 邮件记忆构建完成: {stats['profiles_written']} 个联系人画像")
    elif args.wechat:
        from memory.db import init_db
        init_db()
        from scanner.wechat_decrypt import process_wechat
        process_wechat(manual_key=args.wx_key)
    elif args.rss:
        from memory.db import init_db
        init_db()
        from scanner.rss_monitor import fetch_all_feeds
        new_items = fetch_all_feeds()
        print(f"[RSS] 共获取 {len(new_items)} 篇新文章")
    elif args.kg_build:
        from memory.db import init_db
        init_db()
        from memory.knowledge_graph import build_from_contacts, get_summary as kg_summary
        build_from_contacts()
        print(kg_summary())
    elif args.memory:
        from memory.memory_manage import list_files, get_path
        print(f"\n记忆目录: {get_path()}\n")
        for source, count in list_files().items():
            print(f"  {source:12s}: {count:3d} 条记录")
    elif args.search:
        from memory.db import init_db
        init_db()
        from memory.fts_store import get_store
        fts = get_store()
        results = fts.search(args.search, collection="emails", top_k=5)
        results += fts.search(args.search, collection="documents", top_k=3)
        if not results:
            print(f"未找到: {args.search}")
        else:
            print(f"\n搜索「{args.search}」结果:\n")
            for r in results[:8]:
                meta = r.get("metadata", {})
                print(f"  [{r.get('score', 0):.3f}] {r.get('text', '')[:120]}")
                print(f"           来源: {meta.get('path', meta.get('from', '?'))}\n")
    elif args.report:
        cmd_report()
    elif args.focus_update:
        cmd_focus_update()
    elif args.build_memory or args.personal_only or args.projects_only:
        from scanner.memory_builder import build_all, build_personal_memory, build_project_memories, build_index
        if args.personal_only:
            build_personal_memory()
            build_index()
        elif args.projects_only:
            build_project_memories()
            build_index()
        else:
            build_all()
    elif args.build_wechat_memory:
        from memory.db import init_db
        init_db()
        from scanner.wechat_memory_builder import build_wechat_memory
        stats = build_wechat_memory(days_back=args.wx_days, top_contacts=args.wx_top, top_groups=args.wx_top)
        print(f"\n微信记忆构建完成:")
        print(f"  会话总数: {stats['total_chats']}")
        print(f"  联系人档案: {stats['contact_profiles']} 个")
        print(f"  群聊档案: {stats['group_profiles']} 个")
        print(f"\n联系人档案: data/memory/contacts/wx_*.md")
        print(f"群聊档案:   data/memory/groups/*.md")
        print(f"近期活跃:   data/memory/wechat_active.md")
    elif args.build_email_memory:
        from memory.db import init_db
        init_db()
        from scanner.email_memory_builder import build_email_memory
        stats = build_email_memory()
        print(f"\n邮件记忆构建完成:")
        print(f"  处理邮件: {stats['emails_total']} 封")
        print(f"  真实联系人: {stats['real_contacts']} 人")
        print(f"  生成画像: {stats['profiles_written']} 份")
        print(f"  联系人档案: {stats['contact_files']} 个")
        print(f"\n记忆文件: data/memory/from_emails.md")
        print(f"联系人档案: data/memory/contacts/")
    elif args.analyze_wechat:
        from memory.db import init_db
        init_db()
        from scanner.wechat_analyzer import analyze_wechat_history
        days = args.days
        print(f"[分析] 开始 AI 分析微信历史{'（全部）' if not days else f'（最近{days}天）'}...")
        stats = analyze_wechat_history(days_back=days)
        print(f"\n分析结果:")
        print(f"  会话分析: {stats['chats_analyzed']} 个")
        print(f"  发现项目动态: {stats['projects_found']} 条")
        print(f"  发现重要决定: {stats['decisions_found']} 条")
        print(f"  发现待办任务: {stats['tasks_found']} 条")
        print(f"  FTS 索引: {stats['fts_indexed']} 条消息")
        print(f"\n以上内容已推送到 pending 队列，等待你审核确认。")
        print(f"运行 python main.py --report 查看详情。")
    else:
        cmd_run()


if __name__ == "__main__":
    main()
