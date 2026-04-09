"""
Aegis看门狗 — 崩溃自动重启

用法:
  python watchdog.py              # 监控主进程
  python watchdog.py --web        # 同时带 web UI

安装开机自启（以管理员运行）:
  python watchdog.py --install    # 注册 Windows 计划任务
  python watchdog.py --uninstall  # 删除计划任务
"""
import subprocess
import sys
import time
import os
from datetime import datetime
from pathlib import Path

BASE_DIR  = Path(__file__).parent
LOG_FILE  = BASE_DIR / "data" / "logs" / "watchdog.log"
PYTHON    = sys.executable
MAX_CRASHES_IN_WINDOW = 8   # 10分钟内最多崩溃8次触发告警
CRASH_WINDOW_SEC      = 600
RESTART_DELAY_SEC     = 20  # 崩溃后等待20秒再重启


def _log(msg: str):
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _send_alert(msg: str):
    """发邮件告警（失败不影响主流程）"""
    try:
        sys.path.insert(0, str(BASE_DIR))
        from email_module.sender import send_email
        import config
        send_email(config.NETEASE_EMAIL, "⚠️ Aegis崩溃告警", msg)
    except Exception:
        pass


def run_watchdog(extra_args: list[str]):
    cmd          = [PYTHON, str(BASE_DIR / "main.py")] + extra_args
    crash_times  = []
    total_starts = 0

    _log(f"看门狗启动 | 命令: {' '.join(cmd)}")

    while True:
        total_starts += 1
        _log(f"[第{total_starts}次] 启动进程...")
        t_start = time.time()

        try:
            result   = subprocess.run(cmd, cwd=str(BASE_DIR))
            exit_code = result.returncode
        except KeyboardInterrupt:
            _log("用户 Ctrl+C，停止看门狗")
            break
        except Exception as e:
            exit_code = -1
            _log(f"启动失败: {e}")

        duration = time.time() - t_start

        if exit_code == 0:
            _log(f"进程正常退出（运行 {duration:.0f}s），不重启")
            break

        _log(f"进程崩溃 (exit={exit_code}, 运行{duration:.0f}s)")

        now = time.time()
        crash_times.append(now)
        crash_times = [t for t in crash_times if now - t < CRASH_WINDOW_SEC]

        if len(crash_times) >= MAX_CRASHES_IN_WINDOW:
            alert = (
                f"Aegis在 {CRASH_WINDOW_SEC//60} 分钟内崩溃了 {len(crash_times)} 次，"
                f"可能存在严重问题，请检查日志：\n{LOG_FILE}"
            )
            _log(f"⚠️ 频繁崩溃告警: {alert}")
            _send_alert(alert)
            crash_times.clear()

        _log(f"{RESTART_DELAY_SEC} 秒后重启...")
        time.sleep(RESTART_DELAY_SEC)


def install_task(extra_args: list[str]):
    """注册 Windows 计划任务，开机自动启动看门狗"""
    import shlex
    args_str   = " ".join(shlex.quote(a) for a in extra_args)
    task_name  = "Aegis_Watchdog"
    script_path = str(BASE_DIR / "watchdog.py")
    cmd_action = f'"{PYTHON}" "{script_path}"'
    if args_str:
        cmd_action += f" {args_str}"

    # schtasks 注册：开机触发，以当前用户运行
    schtask_cmd = (
        f'schtasks /Create /F /TN "{task_name}" '
        f'/SC ONSTART /DELAY 0000:30 '
        f'/TR "{cmd_action}" '
        f'/RL HIGHEST'
    )
    ret = os.system(schtask_cmd)
    if ret == 0:
        print(f"✅ 计划任务 '{task_name}' 已注册，下次开机自动启动")
        print(f"   命令: {cmd_action}")
    else:
        print(f"❌ 注册失败（需要管理员权限），请以管理员运行此脚本")


def uninstall_task():
    task_name = "Aegis_Watchdog"
    ret = os.system(f'schtasks /Delete /F /TN "{task_name}"')
    if ret == 0:
        print(f"✅ 计划任务 '{task_name}' 已删除")
    else:
        print(f"❌ 删除失败（任务可能不存在或权限不足）")


if __name__ == "__main__":
    args = sys.argv[1:]

    if "--install" in args:
        extra = [a for a in args if a != "--install"]
        install_task(extra)
    elif "--uninstall" in args:
        uninstall_task()
    else:
        run_watchdog(args)
