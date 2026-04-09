"""
Aegis冷启动引导程序 — bootstrap.py

解决的问题：
  --init 之后 memory/ 和数据库都是空的，首次 --sync-emails 可能处理几百封邮件，
  全程没有进度反馈、不支持断点续传、token 消耗不可见。

本脚本分阶段引导：
  Phase 1  初始化数据库 + 记忆层
  Phase 2  邮件同步（断点续传 + 实时进度 + token 估算）
  Phase 3  本地文件元数据索引
  Phase 4  关键文件向量化（批量，断点续传）
  Phase 5  微信导入（可跳过）
  Phase 6  构建记忆（邮件联系人 + 文件记忆）

断点续传：
  data/bootstrap_checkpoint.json 记录每个 Phase 的进度，
  中断后重新运行会从上次中断处继续。

用法：
  python bootstrap.py                  # 全流程
  python bootstrap.py --phase 2        # 只跑某一阶段
  python bootstrap.py --resume         # 从上次中断处继续（默认行为）
  python bootstrap.py --reset          # 清除断点，从头开始
  python bootstrap.py --months 6       # 同步最近6个月邮件（默认3）
  python bootstrap.py --dry-run        # 只检查，不执行
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

# Windows GBK 终端乱码修复
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() not in ("utf-8", "utf8"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import config

CHECKPOINT_FILE = config.DATA_DIR / "bootstrap_checkpoint.json"
LOG_FILE        = config.DATA_DIR / "logs" / "bootstrap.log"


# ── 进度工具 ──────────────────────────────────────────────────────────────────

class Progress:
    """简单的终端进度显示"""

    def __init__(self, total: int, prefix: str = "", width: int = 40):
        self.total   = max(total, 1)
        self.prefix  = prefix
        self.width   = width
        self.current = 0
        self.start_t = time.time()

    def update(self, n: int = 1, suffix: str = ""):
        self.current = min(self.current + n, self.total)
        pct    = self.current / self.total
        filled = int(self.width * pct)
        bar    = "█" * filled + "░" * (self.width - filled)
        elapsed = time.time() - self.start_t
        eta = (elapsed / pct * (1 - pct)) if pct > 0 else 0
        eta_str = f"{eta:.0f}s" if eta < 60 else f"{eta/60:.1f}min"
        line = f"\r{self.prefix} [{bar}] {self.current}/{self.total} {pct:.0%}  ETA {eta_str}  {suffix}"
        print(line[:120], end="", flush=True)

    def done(self, msg: str = ""):
        elapsed = time.time() - self.start_t
        print(f"\r{self.prefix} [{'█'*self.width}] {self.total}/{self.total} ✓  {elapsed:.1f}s  {msg}")


def log(msg: str, level: str = "INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}][{level}] {msg}"
    print(line)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def section(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


# ── 断点管理 ──────────────────────────────────────────────────────────────────

def load_checkpoint() -> dict:
    if CHECKPOINT_FILE.exists():
        try:
            return json.loads(CHECKPOINT_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_checkpoint(cp: dict):
    CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_FILE.write_text(
        json.dumps(cp, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def mark_phase_done(cp: dict, phase: int, stats: dict = None):
    cp[f"phase{phase}"] = {
        "done": True,
        "finished_at": datetime.now().isoformat(),
        "stats": stats or {},
    }
    save_checkpoint(cp)


def phase_done(cp: dict, phase: int) -> bool:
    return cp.get(f"phase{phase}", {}).get("done", False)


# ── Token 估算 ────────────────────────────────────────────────────────────────

def estimate_tokens(text: str) -> int:
    """粗略估算 token 数（中文≈1.5字/token，英文≈0.75词/token）"""
    cn_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    en_words = len(text.split()) - cn_chars // 2
    return int(cn_chars / 1.5 + max(en_words, 0) * 1.3)


# ── Phase 1：初始化 ────────────────────────────────────────────────────────────

def phase1_init(cp: dict, dry_run: bool = False) -> dict:
    section("Phase 1 · 初始化数据库 + 记忆层")

    if phase_done(cp, 1):
        log("Phase 1 已完成，跳过")
        return {}

    if dry_run:
        log("[dry-run] 跳过实际执行")
        return {}

    from memory.db import init_db
    init_db()
    log("✓ SQLite 数据库初始化完成")

    try:
        from memory.layers import initialize as init_layers
        init_layers()
        log("✓ 记忆层文件初始化完成")
    except Exception as e:
        log(f"记忆层初始化失败（非致命）: {e}", "WARN")

    try:
        from memory.writer import ensure_git_repo
        ensure_git_repo(config.DATA_DIR / "memory")
        log("✓ memory/ git 仓库就绪")
    except Exception as e:
        log(f"git 初始化失败（非致命）: {e}", "WARN")

    stats = {"db": "ok", "layers": "ok"}
    mark_phase_done(cp, 1, stats)
    return stats


# ── Phase 2：邮件同步（断点续传）─────────────────────────────────────────────

def phase2_emails(cp: dict, months: int = 3, dry_run: bool = False) -> dict:
    section(f"Phase 2 · 邮件同步（最近 {months} 个月，断点续传）")

    if phase_done(cp, 2):
        log("Phase 2 已完成，跳过")
        return {}

    if dry_run:
        log("[dry-run] 跳过实际执行")
        return {}

    # 恢复断点
    p2_state = cp.get("phase2_progress", {})
    processed_ids: set = set(p2_state.get("processed_ids", []))
    total_tokens = p2_state.get("total_tokens", 0)
    total_cost_cny = p2_state.get("total_cost_cny", 0.0)

    if processed_ids:
        log(f"断点恢复：已处理 {len(processed_ids)} 封，继续未完成的部分...")

    from email_module.bulk_processor import process_bulk_emails
    from memory import db as main_db

    # ── 先拉取元数据（快速）──────────────────────────────────────
    log("拉取 163 邮件元数据中...")
    try:
        synced = process_bulk_emails(months_back=months)
        log(f"✓ 163 邮件元数据同步完成（新增约 {synced} 封）")
    except Exception as e:
        log(f"163 邮件同步失败: {e}", "ERROR")
        synced = 0

    # Gmail
    try:
        from email_module.bulk_processor import process_bulk_gmail_emails
        synced_g = process_bulk_gmail_emails(months_back=months)
        log(f"✓ Gmail 元数据同步完成（新增约 {synced_g} 封）")
        synced += synced_g
    except Exception as e:
        log(f"Gmail 跳过: {e}", "WARN")

    # ── AI 分析未处理邮件（带进度条）────────────────────────────
    with main_db.get_conn() as conn:
        unprocessed = conn.execute("""
            SELECT id, subject, from_addr, body_text
            FROM emails
            WHERE is_processed = 0
            ORDER BY date DESC
        """).fetchall()

    unprocessed = [dict(r) for r in unprocessed
                   if r["id"] not in processed_ids]

    if not unprocessed:
        log("所有邮件已处理，无需 AI 分析")
        mark_phase_done(cp, 2, {"synced": synced, "ai_analyzed": len(processed_ids), "tokens": total_tokens})
        return {}

    log(f"待 AI 分析邮件：{len(unprocessed)} 封")
    log(f"预计 token 消耗：~{len(unprocessed) * 600:,}（每封约600 tokens）")
    log(f"预计费用（豆包 lite）：~¥{len(unprocessed) * 600 / 1000 * 0.0008:.2f}")
    log(f"预计耗时：~{len(unprocessed) * 2 // 60} 分钟（每封约2秒）")
    print()

    from ai.client import analyze_email
    prog = Progress(len(unprocessed), prefix="AI 分析邮件")
    ai_done = 0
    save_every = 20  # 每20封保存一次断点

    for i, email in enumerate(unprocessed):
        try:
            body = (email.get("body_text") or "")[:1500]
            tokens_this = estimate_tokens(
                f"{email.get('subject','')} {email.get('from_addr','')} {body}"
            )
            result = analyze_email(
                subject=email.get("subject", ""),
                sender=email.get("from_addr", ""),
                body=body,
            )
            with main_db.get_conn() as conn:
                conn.execute("""
                    UPDATE emails SET
                        summary    = ?,
                        importance = ?,
                        category   = ?,
                        needs_reply= ?,
                        draft_reply= ?,
                        is_processed = 1
                    WHERE id = ?
                """, (
                    result.get("summary", ""),
                    result.get("importance", 2),
                    result.get("category", "其他"),
                    1 if result.get("needs_reply") else 0,
                    result.get("draft_reply"),
                    email["id"],
                ))

            processed_ids.add(email["id"])
            total_tokens    += tokens_this
            total_cost_cny  += tokens_this / 1000 * 0.0008
            ai_done += 1

            prog.update(1, suffix=f"token累计:{total_tokens:,} ¥{total_cost_cny:.3f}")

            # 断点保存
            if (i + 1) % save_every == 0:
                cp["phase2_progress"] = {
                    "processed_ids": list(processed_ids),
                    "total_tokens":  total_tokens,
                    "total_cost_cny": total_cost_cny,
                }
                save_checkpoint(cp)

        except KeyboardInterrupt:
            print()
            log("用户中断，已保存断点，下次运行将从此处继续")
            cp["phase2_progress"] = {
                "processed_ids": list(processed_ids),
                "total_tokens":  total_tokens,
                "total_cost_cny": total_cost_cny,
            }
            save_checkpoint(cp)
            sys.exit(0)
        except Exception as e:
            log(f"邮件分析失败 [{email.get('subject','?')[:30]}]: {e}", "WARN")
            prog.update(1)

    prog.done()

    stats = {
        "synced":      synced,
        "ai_analyzed": ai_done,
        "tokens":      total_tokens,
        "cost_cny":    round(total_cost_cny, 4),
    }
    log(f"✓ 邮件阶段完成 | 分析:{ai_done}封 | tokens:{total_tokens:,} | 费用:¥{total_cost_cny:.3f}")
    mark_phase_done(cp, 2, stats)

    # 清理断点进度
    cp.pop("phase2_progress", None)
    save_checkpoint(cp)
    return stats


# ── Phase 3：文件元数据索引 ───────────────────────────────────────────────────

def phase3_file_index(cp: dict, dry_run: bool = False) -> dict:
    section("Phase 3 · 本地文件元数据索引")

    if phase_done(cp, 3):
        log("Phase 3 已完成，跳过")
        return {}

    if dry_run:
        log("[dry-run] 跳过实际执行")
        return {}

    log(f"扫描目录: {config.SCAN_ROOTS}")
    log("（仅索引元数据，不读取文件内容，速度很快）")

    try:
        from scanner.file_metadata_indexer import scan_metadata
        stats = scan_metadata(incremental=False)
        log(f"✓ 元数据索引完成 | 扫描:{stats['scanned']} 新增:{stats['new']} 更新:{stats['updated']}")
        mark_phase_done(cp, 3, stats)
        return stats
    except Exception as e:
        log(f"文件索引失败: {e}", "ERROR")
        return {}


# ── Phase 4：关键文件向量化（断点续传）───────────────────────────────────────

def phase4_vectorize(cp: dict, batch_size: int = 30, dry_run: bool = False) -> dict:
    section("Phase 4 · 关键文件向量化（断点续传）")

    if phase_done(cp, 4):
        log("Phase 4 已完成，跳过")
        return {}

    if dry_run:
        log("[dry-run] 跳过实际执行")
        return {}

    from memory import db as main_db
    with main_db.get_conn() as conn:
        pending_count = conn.execute(
            "SELECT COUNT(*) FROM file_index WHERE status='pending'"
        ).fetchone()[0]

    log(f"待向量化文件：{pending_count} 个（只处理关键文件，非全盘）")
    if pending_count == 0:
        log("无待处理文件，跳过")
        mark_phase_done(cp, 4, {"vectorized": 0})
        return {"vectorized": 0}

    log(f"预计耗时：~{pending_count // batch_size * 3} 秒（每批约3秒）")

    from scanner.vectorizer import process_pending_files
    prog = Progress(pending_count, prefix="向量化文件")
    total_done = 0

    while True:
        try:
            done = process_pending_files(batch_size=batch_size)
            if done == 0:
                break
            total_done += done
            prog.update(done)
        except KeyboardInterrupt:
            print()
            log(f"用户中断，已完成 {total_done} 个，下次运行将继续")
            sys.exit(0)
        except Exception as e:
            log(f"向量化批次失败: {e}", "WARN")
            break

    prog.done()
    stats = {"vectorized": total_done}
    log(f"✓ 向量化完成 | 处理:{total_done} 个文件")
    mark_phase_done(cp, 4, stats)
    return stats


# ── Phase 5：微信导入（可跳过）───────────────────────────────────────────────

def phase5_wechat(cp: dict, skip: bool = False, dry_run: bool = False) -> dict:
    section("Phase 5 · 微信聊天记录导入")

    if phase_done(cp, 5):
        log("Phase 5 已完成，跳过")
        return {}

    if skip:
        log("跳过微信导入（--skip-wechat）")
        mark_phase_done(cp, 5, {"skipped": True})
        return {}

    if dry_run:
        log("[dry-run] 跳过实际执行")
        return {}

    log("尝试解密导入微信聊天记录（需微信运行中）...")
    try:
        from scanner.wechat_decrypt import process_wechat
        process_wechat()
        log("✓ 微信聊天记录导入完成")
        mark_phase_done(cp, 5, {"done": True})
        return {"done": True}
    except Exception as e:
        log(f"微信导入失败（非致命，可稍后手动运行 --wechat）: {e}", "WARN")
        log("  提示：确保微信已登录并在前台，然后运行: python main.py --wechat")
        mark_phase_done(cp, 5, {"skipped": True, "reason": str(e)})
        return {}


# ── Phase 6：构建记忆 ─────────────────────────────────────────────────────────

def phase6_memory(cp: dict, dry_run: bool = False) -> dict:
    section("Phase 6 · 构建初始记忆（邮件联系人 + 文件记忆）")

    if phase_done(cp, 6):
        log("Phase 6 已完成，跳过")
        return {}

    if dry_run:
        log("[dry-run] 跳过实际执行")
        return {}

    stats = {}

    # 邮件联系人记忆
    log("构建邮件联系人记忆...")
    try:
        from scanner.email_memory_builder import build_email_memory
        r = build_email_memory()
        log(f"✓ 邮件记忆 | 联系人:{r.get('real_contacts',0)} 画像:{r.get('profiles_written',0)}")
        stats["email_memory"] = r
    except Exception as e:
        log(f"邮件记忆构建失败: {e}", "WARN")

    # 文件记忆
    log("构建文件记忆...")
    try:
        from scanner.memory_builder import build_all
        build_all()
        log("✓ 文件记忆构建完成")
        stats["file_memory"] = "ok"
    except Exception as e:
        log(f"文件记忆构建失败: {e}", "WARN")

    mark_phase_done(cp, 6, stats)
    return stats


# ── 摘要报告 ──────────────────────────────────────────────────────────────────

def print_summary(cp: dict, total_time: float):
    section("Bootstrap 完成摘要")
    for i in range(1, 7):
        p = cp.get(f"phase{i}", {})
        if p.get("done"):
            s = p.get("stats", {})
            extra = ""
            if i == 2:
                extra = f"  分析:{s.get('ai_analyzed',0)}封 tokens:{s.get('tokens',0):,} ¥{s.get('cost_cny',0)}"
            elif i == 3:
                extra = f"  扫描:{s.get('scanned',0)} 新增:{s.get('new',0)}"
            elif i == 4:
                extra = f"  向量化:{s.get('vectorized',0)}个文件"
            print(f"  Phase {i} ✓{extra}")
        else:
            print(f"  Phase {i} ○ 未完成")

    print(f"\n  总耗时: {total_time:.0f}s ({total_time/60:.1f}min)")
    print(f"\n下一步:")
    print(f"  启动系统:  python watchdog.py")
    print(f"  启动Web:   python main.py --web")
    print(f"  立即简报:  python main.py --briefing")
    if not cp.get("phase5", {}).get("done") or cp.get("phase5", {}).get("stats", {}).get("skipped"):
        print(f"  导入微信:  python main.py --wechat  (稍后手动运行)")


# ── 主入口 ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Aegis冷启动引导程序",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python bootstrap.py                  全流程冷启动
  python bootstrap.py --resume         断点续传（默认行为）
  python bootstrap.py --reset          清除断点从头开始
  python bootstrap.py --phase 2        只跑 Phase 2（邮件同步）
  python bootstrap.py --months 6       同步最近6个月邮件
  python bootstrap.py --skip-wechat    跳过微信导入
  python bootstrap.py --dry-run        只检查环境，不执行
        """,
    )
    parser.add_argument("--resume",      action="store_true", help="从断点继续（默认）")
    parser.add_argument("--reset",       action="store_true", help="清除断点，从头开始")
    parser.add_argument("--phase",       type=int, default=0, help="只运行指定阶段 (1-6)")
    parser.add_argument("--months",      type=int, default=3, help="邮件同步月数（默认3）")
    parser.add_argument("--batch",       type=int, default=30, help="向量化批次大小（默认30）")
    parser.add_argument("--skip-wechat", action="store_true", help="跳过微信导入")
    parser.add_argument("--dry-run",     action="store_true", help="只检查，不执行")
    args = parser.parse_args()

    print(f"""
╔══════════════════════════════════════════════════════════╗
║           Aegis Bootstrap — 冷启动引导程序               ║
║  支持断点续传 | 实时进度 | token 消耗追踪                 ║
╚══════════════════════════════════════════════════════════╝
  开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
  邮件同步: 最近 {args.months} 个月
  断点文件: {CHECKPOINT_FILE}
""")

    # 加载/重置断点
    if args.reset:
        if CHECKPOINT_FILE.exists():
            CHECKPOINT_FILE.unlink()
            log("已清除断点文件，从头开始")
        cp = {}
    else:
        cp = load_checkpoint()
        if cp:
            done_phases = [i for i in range(1, 7) if phase_done(cp, i)]
            if done_phases:
                log(f"发现断点：Phase {done_phases} 已完成，将跳过")

    t_start = time.time()

    # 初始化 DB（所有 phase 都需要）
    if not args.dry_run:
        from memory.db import init_db
        init_db()

    # 运行指定或全部阶段
    run_all = (args.phase == 0)

    if run_all or args.phase == 1:
        phase1_init(cp, dry_run=args.dry_run)

    if run_all or args.phase == 2:
        phase2_emails(cp, months=args.months, dry_run=args.dry_run)

    if run_all or args.phase == 3:
        phase3_file_index(cp, dry_run=args.dry_run)

    if run_all or args.phase == 4:
        phase4_vectorize(cp, batch_size=args.batch, dry_run=args.dry_run)

    if run_all or args.phase == 5:
        phase5_wechat(cp, skip=args.skip_wechat, dry_run=args.dry_run)

    if run_all or args.phase == 6:
        phase6_memory(cp, dry_run=args.dry_run)

    print_summary(cp, time.time() - t_start)


if __name__ == "__main__":
    main()
