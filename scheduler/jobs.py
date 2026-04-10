"""APScheduler 定时任务定义"""
from datetime import datetime
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.schedulers.background import BackgroundScheduler

# 连续失败计数器（进程内存，重启清零）
_fail_counts: dict[str, int] = {}


def _check_alert(job_id: str, ok: bool):
    """
    记录 job 连续失败次数，超过阈值时发邮件告警。
    ok=True 时重置计数。
    """
    if ok:
        _fail_counts.pop(job_id, None)
        return
    _fail_counts[job_id] = _fail_counts.get(job_id, 0) + 1
    try:
        from settings_manager import get
        threshold = int(get("notify.sync_failure_threshold") or 3)
        enabled = get("notify.alert_on_sync_failure") is not False
    except Exception:
        threshold, enabled = 3, True

    if enabled and _fail_counts[job_id] >= threshold:
        try:
            from email_module.sender import send_email
            import config
            send_email(
                to=config.NETEASE_EMAIL,
                subject=f"[Aegis] ⚠️ 任务 {job_id} 连续失败 {_fail_counts[job_id]} 次",
                body=(
                    f"任务 [{job_id}] 已连续失败 {_fail_counts[job_id]} 次，"
                    f"最后失败时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}。\n"
                    "请检查日志或重启Aegis。"
                ),
            )
            # 发完告警重置，避免每次都发
            _fail_counts[job_id] = 0
        except Exception as e:
            print(f"[告警] 发送失败邮件本身失败: {e}")


def check_emails():
    """每30分钟: 拉取163+Gmail新邮件，重要邮件立即推送；同时检查用户指令"""
    from email_module.reader import fetch_new_emails
    from email_module.gmail_reader import fetch_new_gmail
    from email_module.summarizer import process_new_emails
    from email_module.sender import send_email
    from email_module.command_handler import process_commands
    import config

    print(f"[Job] check_emails 开始 @ {datetime.now().strftime('%H:%M')}")

    # 先处理用户指令（优先响应）— 邮件通道
    try:
        process_commands()
    except Exception as e:
        print(f"[Job] 指令处理异常: {e}")

    # 合并两个邮箱的新邮件
    new_emails = fetch_new_emails(limit=30)
    try:
        new_emails += fetch_new_gmail(limit=30)
    except Exception as e:
        print(f"[Job] Gmail 拉取跳过: {e}")

    if not new_emails:
        return

    important = process_new_emails(new_emails)

    # 重要性 >= 4 的邮件立即推送提醒
    urgent = [e for e in important if e.get("importance", 0) >= 4]
    if urgent:
        lines = []
        for e in urgent:
            lines.append(
                f"⚠️ 【{e['from_addr']}】{e['subject']}\n"
                f"   摘要: {e.get('summary', '')}\n"
                + (f"   草稿回复: {e['draft_reply']}\n" if e.get('draft_reply') else "")
            )
        body = "Aegis提醒：以下邮件需要您关注\n\n" + "\n".join(lines)
        send_email(config.NETEASE_EMAIL, f"⚠️ 重要邮件提醒（{len(urgent)}封）", body)


def send_daily_briefing():
    """每天 08:00: 生成并发送今日简报"""
    from memory import db, profile
    from ai import client as ai
    from email_module.sender import send_daily_briefing as send_briefing
    import config

    today = datetime.now().strftime("%Y-%m-%d")
    print(f"[Job] daily_briefing 开始 @ {today}")

    if db.report_exists_today(today):
        print("[Job] 今日简报已发送，跳过")
        return

    # 收集上下文
    important_emails = db.get_important_emails(min_importance=3, limit=10)
    email_summaries = "\n".join(
        f"  ★{e['importance']} [{e['from_addr']}] {e['subject']} — {e['summary']}"
        + (f"\n  草稿回复: {e['draft_reply']}" if e.get('draft_reply') else "")
        for e in important_emails
    ) or "暂无重要邮件"

    from email_module.contacts import get_contacts_summary
    from scanner.rss_monitor import get_recent_papers
    recent_papers = get_recent_papers(limit=5, min_importance=4)
    papers_summary = "\n".join(
        f"  [{p['feed_name']}] {p['title']} — {p['summary'][:80]}"
        for p in recent_papers
    ) or "暂无"

    # 微信近期活跃事项
    wechat_active = ""
    try:
        wx_path = config.DATA_DIR / "memory" / "wechat_active.md"
        if wx_path.exists():
            wechat_active = wx_path.read_text(encoding="utf-8")[:1200]
    except Exception:
        pass

    # 微信总览（from_wechat.md 前段）
    wechat_summary = ""
    try:
        wx_sum_path = config.DATA_DIR / "memory" / "from_wechat.md"
        if wx_sum_path.exists():
            wechat_summary = wx_sum_path.read_text(encoding="utf-8")[:600]
    except Exception:
        pass

    context = {
        "date": today,
        "email_count": len(important_emails),
        "email_summaries": email_summaries,
        "profile_summary": profile.get_summary(),
        "contacts_summary": get_contacts_summary(),
        "new_papers": papers_summary,
        "wechat_active": wechat_active,
        "wechat_summary": wechat_summary,
    }

    briefing = ai.generate_daily_briefing(context)

    # OpenJarvis 自评分机制: 低于7分自动重新生成
    score, feedback = ai.evaluate_briefing(briefing, context)
    if score < 7.0 and feedback:
        print(f"[Job] 日报评分 {score:.1f}/10，根据反馈重新生成...")
        context["regenerate_feedback"] = feedback
        context["prev_score"] = score
        briefing = ai.generate_daily_briefing(context)

    send_briefing(briefing, today)
    db.save_daily_report(today, briefing)
    print(f"[Job] 日报发送完成（评分: {score:.1f}/10）")


def _fetch_rss():
    """每天 07:00: 拉取学术RSS订阅"""
    try:
        from scanner.rss_monitor import fetch_all_feeds
        new_items = fetch_all_feeds(notify_important=True)
        print(f"[Job] rss_fetch: {len(new_items)} 篇新文章")
    except Exception as e:
        print(f"[Job] rss_fetch 失败: {e}")


def update_profile_nightly():
    """每天 03:00: 元数据索引 + 向量化关键文件 + 应用超时 pending + SQLite 备份"""
    from scanner.vectorizer import process_pending_files
    from memory.pending import auto_apply_timeout, apply_approved
    import config
    print(f"[Job] profile_update 开始 @ {datetime.now()}")

    # 增量更新文件元数据索引（秒级完成）
    try:
        from scanner.file_metadata_indexer import scan_metadata
        meta_stats = scan_metadata(incremental=True)
        print(f"[Job] 元数据索引: 新增{meta_stats['new']} 更新{meta_stats['updated']}")
    except Exception as e:
        print(f"[Job] 元数据索引失败（非致命）: {e}")

    # 只向量化关键文件（CV/标书/活跃代码）
    process_pending_files(batch_size=30)
    # 应用超时自动通过的 pending 条目
    n = auto_apply_timeout()
    if n:
        applied = apply_approved()
        print(f"[Job] 自动通过 {n} 条 pending，已写入记忆层 {applied} 条")
    # SQLite 每日备份
    try:
        from memory.backup import daily_backup
        daily_backup(config.DB_PATH)
    except Exception as e:
        print(f"[Job] 备份失败（非致命）: {e}")
    # 重算联系人重要度
    try:
        from memory import db as main_db
        main_db.recalc_importance()
    except Exception as e:
        print(f"[Job] recalc_importance 失败（非致命）: {e}")

    # 每日 pending 过期清理（48h 超时条目标为 expired）
    try:
        from memory.aging import expire_pending_items, clean_focus_items
        expire_pending_items()
        clean_focus_items()
    except Exception as e:
        print(f"[Job] aging 清理失败（非致命）: {e}")


def _run_focus_update():
    """每天 19:00: 从邮件+微信提取今日焦点，发邮件请用户确认"""
    try:
        from scheduler.focus_updater import run_focus_update
        stats = run_focus_update(send_email=True)
        print(f"[Job] focus_update: {stats}")
    except Exception as e:
        print(f"[Job] focus_update 失败: {e}")


def _daily_reconcile():
    """每天 21:00: 通知质量对账 — 汇总隐式信号 + 生成报告发给用户"""
    try:
        from memory.importance_learner import daily_reconcile
        from email_module.sender import send_email
        import config

        report = daily_reconcile()
        if not report:
            print("[Job] daily_reconcile: 今天没有推送通知，跳过")
            return

        send_email(
            to=config.NETEASE_EMAIL,
            subject="📊 Aegis 今日通知质量报告",
            body=report,
        )
        print(f"[Job] daily_reconcile: 报告已发送（{len(report)} 字）")
    except Exception as e:
        print(f"[Job] daily_reconcile 失败: {e}")


def _weekly_report():
    """每周日 09:00: 发送每周状态报告"""
    from memory import db as main_db
    from memory.layers import (
        get_self, get_focus, OVERVIEW_PATH, MEMORY_DIR
    )
    from memory.pending import count_pending
    from email_module.sender import send_email
    from datetime import datetime
    import config

    print(f"[Job] weekly_report 开始 @ {datetime.now()}")

    try:
        # 基础内容
        self_content = get_self()
        focus_content = get_focus()

        # 联系人
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

        # Pending 统计
        pending_count = count_pending()

        # 知识库统计
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

        # 最近 write_log
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

        today = datetime.now().strftime("%Y-%m-%d")
        report = f"""# Aegis每周状态报告 — {today}

## 系统统计
- 邮件: {email_count} 封
- 微信消息: {wechat_count} 条
- 已索引文件: {file_count} 个
- 待审核 pending: {pending_count} 条

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
        # 保存到文件
        report_path = config.DATA_DIR / f"状态报告_{today}.md"
        report_path.write_text(report, encoding="utf-8")

        # 发送邮件
        send_email(
            to=config.NETEASE_EMAIL,
            subject=f"[Aegis] 每周状态报告 {today}",
            body=report,
        )
        print(f"[Job] 每周报告已发送并保存至 {report_path}")

    except Exception as e:
        print(f"[Job] weekly_report 失败: {e}")


def _monthly_memory_update():
    """每月1日 04:00: 月度个人背景记忆更新（从邮件+微信+项目记忆合并）"""
    try:
        from scanner.memory_builder import monthly_update_personal_memory
        result = monthly_update_personal_memory()
        print(f"[Job] monthly_memory_update: {result}")
    except Exception as e:
        print(f"[Job] monthly_memory_update 失败: {e}")


def _aging_check():
    """每周日 03:00: 老化检查"""
    from memory.aging import run_aging
    stats = run_aging()
    print(f"[Job] aging: {stats}")


def _daily_backup():
    """每天 03:00: SQLite 备份（已合并到 update_profile_nightly）"""
    from memory.backup import daily_backup
    import config
    daily_backup(config.DB_PATH)


def check_wechat_commands():
    """每2分钟: 微信指令 DB 轮询备用通道（wxauto 实时监听的补充）"""
    import config
    if not config.MODULES.get("wechat"):
        return
    try:
        from scheduler.wechat_commander import process_wechat_commands
        process_wechat_commands()
    except Exception as e:
        print(f"[Job] 微信指令处理异常: {e}")


def sync_wechat_messages():
    """每15分钟: 同步微信新消息到 DB（基于文件 mtime，有变化才解密）"""
    if not config.MODULES.get("wechat"):
        return
    try:
        from scanner.wechat_decrypt import sync_wechat_incremental
        sync_wechat_incremental()
        _check_alert("wechat_sync", ok=True)
    except Exception as e:
        print(f"[Job] 微信消息同步异常: {e}")
        _check_alert("wechat_sync", ok=False)


def refresh_wechat_contacts():
    """每30分钟: 增量更新活跃联系人档案 + 提取焦点事项"""
    if not config.MODULES.get("wechat"):
        return
    try:
        from scanner.wechat_memory_builder import refresh_active_contacts
        refresh_active_contacts()
        _check_alert("wechat_contacts_refresh", ok=True)
    except Exception as e:
        print(f"[Job] 微信联系人刷新异常: {e}")
        _check_alert("wechat_contacts_refresh", ok=False)


def start_scheduler(background: bool = False):
    """
    启动调度器。
    background=True: 后台线程模式（配合其他逻辑使用）
    background=False: 阻塞模式（独立运行时使用）
    """
    SchedulerClass = BackgroundScheduler if background else BlockingScheduler
    scheduler = SchedulerClass(timezone="Asia/Shanghai")

    # 每30分钟检查邮件+用户指令
    scheduler.add_job(check_emails, "interval", minutes=30, id="check_emails")

    # 每2分钟: 微信指令 DB 轮询（wxauto 实时监听的补充，确保不漏消息）
    scheduler.add_job(check_wechat_commands, "interval", minutes=2, id="wechat_commands")

    # 每30分钟: 增量更新活跃联系人档案 + 提取焦点
    scheduler.add_job(refresh_wechat_contacts, "interval", minutes=30, id="wechat_contacts_refresh")

    # 每天 08:00 发日报
    scheduler.add_job(send_daily_briefing, "cron", hour=8, minute=0, id="daily_briefing")

    # 每天 07:00 拉取学术 RSS 订阅（早于日报，以便纳入简报）
    scheduler.add_job(_fetch_rss, "cron", hour=7, minute=0, id="rss_fetch")

    # 每天 19:00 从邮件+微信提取焦点事项，发邮件请用户确认
    scheduler.add_job(_run_focus_update, "cron", hour=19, minute=0, id="focus_update")

    # 每天 03:00 更新档案（后台处理文件 + 自动应用超时 pending + 备份 + 重要度重算）
    scheduler.add_job(update_profile_nightly, "cron", hour=3, minute=0, id="profile_update")

    # 每周日 09:00 发送每周状态报告
    scheduler.add_job(_weekly_report, "cron", day_of_week="sun", hour=9, minute=0, id="weekly_report")

    # 每周日 03:00 老化检查（清理过期 focus / pending）
    scheduler.add_job(_aging_check, "cron", day_of_week="sun", hour=3, minute=0, id="aging_check")

    # 每月1日 04:00 个人背景记忆月度更新
    scheduler.add_job(_monthly_memory_update, "cron", day=1, hour=4, minute=0, id="monthly_memory_update")

    # 每天 21:00 通知质量对账（隐式+显式信号汇总，更新重要性权重）
    scheduler.add_job(_daily_reconcile, "cron", hour=21, minute=0, id="daily_reconcile")

    print("[Scheduler] 已注册任务:")
    print("  - check_emails:          每30分钟（邮件指令检测）")
    print("  - wechat_commands:       每2分钟（微信指令 DB 轮询备用）")
    print("  - daily_briefing:        每天 08:00")
    print("  - rss_fetch:             每天 07:00")
    print("  - focus_update:          每天 19:00（邮件+微信焦点提取）")
    print("  - daily_reconcile:       每天 21:00（通知质量对账 + 权重更新）")
    print("  - profile_update:        每天 03:00（文件索引 + pending 自动写入 + 备份）")
    print("  - weekly_report:         每周日 09:00（状态报告）")
    print("  - aging_check:           每周日 03:00（老化清理）")
    print("  - monthly_memory_update: 每月1日 04:00（个人背景月度更新）")

    scheduler.start()
    return scheduler
