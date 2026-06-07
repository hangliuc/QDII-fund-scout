# -*- coding: utf-8 -*-
"""定时任务统一管理工具

集中处理 macOS launchd / Linux crontab 的 plist/cron 生成、卸载、状态查询，
被 ui/server.py 直接 import，run.sh 通过命令行调用。

CLI 用法：
    python3 schedule_setup.py status
    python3 schedule_setup.py setup --times "09:00,15:30" --weekdays
    python3 schedule_setup.py setup --times "09:00" --everyday
    python3 schedule_setup.py remove
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
from typing import Iterable

CONFIG_DIR = os.path.expanduser("~/.fund-scout")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
SCHEDULE_SCRIPT_PATH = os.path.join(CONFIG_DIR, "scheduled_push.sh")
PLIST_PATH = os.path.expanduser("~/Library/LaunchAgents/com.fundscout.push.plist")
SCHEDULE_LOG = os.path.join(CONFIG_DIR, "schedule.log")

# 允许 ui/server.py 与 run.sh 共享的"项目脚本根目录"。
# 默认按当前文件所在目录计算（即 scripts/），调用方可覆盖。
DEFAULT_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _resolve_python() -> str:
    """获取 python3 的绝对路径，避免 cron/launchd 拿到没装依赖的 system python。"""
    try:
        out = subprocess.run(
            ["bash", "-lc", "command -v python3 || true"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        return out or "/usr/bin/env python3"
    except Exception:
        return "/usr/bin/env python3"


def _parse_times(times: str) -> list[tuple[int, int]]:
    """把 '09:00,15:30' 解析为 [(9, 0), (15, 30)]"""
    parsed = []
    for t in times.split(","):
        t = t.strip()
        if not t:
            continue
        m = re.match(r"^(\d{1,2}):(\d{2})$", t)
        if not m:
            raise ValueError(f"时间格式错误: {t}（期望 HH:MM）")
        h, mi = int(m.group(1)), int(m.group(2))
        if h > 23 or mi > 59:
            raise ValueError(f"时间越界: {t}")
        parsed.append((h, mi))
    if not parsed:
        raise ValueError("未提供推送时间")
    return parsed


def _write_schedule_script(scripts_dir: str) -> str:
    """生成 ~/.fund-scout/scheduled_push.sh"""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    python_bin = _resolve_python()
    content = f"""#!/bin/bash
# QDII-fund-scout 定时推送脚本（由 schedule_setup 自动生成，请勿手动修改）
CONFIG_FILE="{CONFIG_FILE}"
SCRIPT_DIR="{scripts_dir}"
PYTHON_BIN="{python_bin}"

if [ ! -f "$CONFIG_FILE" ]; then
    echo "[$(date)] 配置文件不存在，跳过" >> "{SCHEDULE_LOG}"
    exit 1
fi

cd "$SCRIPT_DIR"

# 1) 先自动检查 / 刷新 CSRC 季报缓存（季报披露后无感跟进）
#    失败也不影响推送（季报每季度只更新一次，临时挂掉问题不大）
"$PYTHON_BIN" holdings_refresh.py >> "{SCHEDULE_LOG}" 2>&1 || true

# 2) 拉数据 + 推送
"$PYTHON_BIN" cli.py compare --config "$CONFIG_FILE" --push feishu,wechat >> "{SCHEDULE_LOG}" 2>&1
echo "[$(date)] 推送完成" >> "{SCHEDULE_LOG}"
"""
    with open(SCHEDULE_SCRIPT_PATH, "w") as f:
        f.write(content)
    os.chmod(SCHEDULE_SCRIPT_PATH, 0o755)
    return SCHEDULE_SCRIPT_PATH


# ---------------------------------------------------------------------------
# 平台分支：macOS launchd
# ---------------------------------------------------------------------------

def _setup_macos(times: list[tuple[int, int]], weekdays_only: bool) -> dict:
    import plistlib

    intervals: list[dict] = []
    for h, mi in times:
        if weekdays_only:
            for d in range(1, 6):
                intervals.append({"Hour": h, "Minute": mi, "Weekday": d})
        else:
            intervals.append({"Hour": h, "Minute": mi})

    plist = {
        "Label": "com.fundscout.push",
        "ProgramArguments": ["/bin/bash", SCHEDULE_SCRIPT_PATH],
        "StartCalendarInterval": intervals,
        "StandardOutPath": SCHEDULE_LOG,
        "StandardErrorPath": SCHEDULE_LOG,
        "EnvironmentVariables": {
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        },
    }
    os.makedirs(os.path.dirname(PLIST_PATH), exist_ok=True)
    # 先尝试卸载旧任务（避免重名冲突）
    subprocess.run(["launchctl", "unload", PLIST_PATH], capture_output=True, timeout=5)
    with open(PLIST_PATH, "wb") as f:
        plistlib.dump(plist, f)
    r = subprocess.run(["launchctl", "load", PLIST_PATH], capture_output=True, text=True, timeout=5)
    if r.returncode != 0:
        return {"ok": False, "error": f"launchctl load 失败: {r.stderr.strip()}"}
    return {"ok": True}


def _remove_macos() -> dict:
    if os.path.exists(PLIST_PATH):
        subprocess.run(["launchctl", "unload", PLIST_PATH], capture_output=True, timeout=5)
        os.remove(PLIST_PATH)
    if os.path.exists(SCHEDULE_SCRIPT_PATH):
        os.remove(SCHEDULE_SCRIPT_PATH)
    return {"ok": True}


def _status_macos() -> dict:
    if not os.path.exists(PLIST_PATH):
        return {"active": False}
    try:
        import plistlib
        with open(PLIST_PATH, "rb") as f:
            plist = plistlib.load(f)
        intervals = plist.get("StartCalendarInterval", [])
        if isinstance(intervals, dict):
            intervals = [intervals]
        times: list[str] = []
        weekdays = False
        for iv in intervals:
            t = f"{iv.get('Hour', 0):02d}:{iv.get('Minute', 0):02d}"
            if t not in times:
                times.append(t)
            if iv.get("Weekday", 0) != 0:
                weekdays = True
        return {"active": True, "times": times, "weekdays": weekdays}
    except Exception as e:
        return {"active": False, "error": str(e)}


# ---------------------------------------------------------------------------
# 平台分支：Linux crontab
# ---------------------------------------------------------------------------

def _build_cron_expr(times: list[tuple[int, int]], weekdays_only: bool) -> str:
    minutes = ",".join(str(mi) for _, mi in times)
    hours = ",".join(str(h) for h, _ in times)
    dow = "1-5" if weekdays_only else "*"
    return f"{minutes} {hours} * * {dow}"


def _setup_linux(times: list[tuple[int, int]], weekdays_only: bool) -> dict:
    expr = _build_cron_expr(times, weekdays_only)
    existing = ""
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            existing = "\n".join(
                line for line in r.stdout.splitlines()
                if "scheduled_push.sh" not in line
            )
    except FileNotFoundError:
        return {"ok": False, "error": "crontab 命令不可用"}

    new_cron = (existing + ("\n" if existing else "") + f"{expr} bash {SCHEDULE_SCRIPT_PATH}\n")
    r = subprocess.run(["crontab", "-"], input=new_cron, text=True,
                       capture_output=True, timeout=5)
    if r.returncode != 0:
        return {"ok": False, "error": f"crontab 写入失败: {r.stderr.strip()}"}
    return {"ok": True, "cron": expr}


def _remove_linux() -> dict:
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            new_cron = "\n".join(
                line for line in r.stdout.splitlines()
                if "scheduled_push.sh" not in line
            )
            subprocess.run(["crontab", "-"], input=new_cron + "\n",
                           text=True, capture_output=True, timeout=5)
    except FileNotFoundError:
        pass
    if os.path.exists(SCHEDULE_SCRIPT_PATH):
        os.remove(SCHEDULE_SCRIPT_PATH)
    return {"ok": True}


def _status_linux() -> dict:
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return {"active": False}
        for line in r.stdout.splitlines():
            if "scheduled_push.sh" in line:
                parts = line.split()
                if len(parts) >= 5:
                    return {"active": True, "cron": " ".join(parts[:5])}
    except FileNotFoundError:
        pass
    return {"active": False}


# ---------------------------------------------------------------------------
# 公共 API
# ---------------------------------------------------------------------------

def setup(times_str: str, weekdays_only: bool = False, scripts_dir: str = "") -> dict:
    """配置定时任务

    times_str: "09:00,15:30" 形式
    weekdays_only: True 只在周一到周五运行
    scripts_dir: cli.py 所在目录，默认是 schedule_setup.py 自身的目录
    """
    try:
        times = _parse_times(times_str)
    except ValueError as e:
        return {"ok": False, "error": str(e)}

    if not scripts_dir:
        scripts_dir = DEFAULT_SCRIPTS_DIR

    _write_schedule_script(scripts_dir)

    label_dow = "工作日" if weekdays_only else "每天"
    label_times = "、".join(f"{h:02d}:{mi:02d}" for h, mi in times)
    label = f"{label_dow} {label_times}"

    system = platform.system()
    if system == "Darwin":
        result = _setup_macos(times, weekdays_only)
    elif system == "Linux":
        result = _setup_linux(times, weekdays_only)
    else:
        return {"ok": False, "error": f"暂不支持系统：{system}"}

    if result.get("ok"):
        result["label"] = label
    return result


def remove() -> dict:
    """卸载定时任务"""
    system = platform.system()
    if system == "Darwin":
        return _remove_macos()
    if system == "Linux":
        return _remove_linux()
    return {"ok": False, "error": f"暂不支持系统：{system}"}


def status() -> dict:
    """查询定时任务状态"""
    system = platform.system()
    if system == "Darwin":
        return _status_macos()
    if system == "Linux":
        return _status_linux()
    return {"active": False, "unsupported": True}


# ---------------------------------------------------------------------------
# CLI 入口（被 run.sh 调用）
# ---------------------------------------------------------------------------

def _main() -> int:
    parser = argparse.ArgumentParser(prog="schedule_setup")
    sub = parser.add_subparsers(dest="cmd")

    p_setup = sub.add_parser("setup", help="配置定时任务")
    p_setup.add_argument("--times", required=True, help="HH:MM,HH:MM,...")
    grp = p_setup.add_mutually_exclusive_group()
    grp.add_argument("--weekdays", action="store_true", help="仅工作日")
    grp.add_argument("--everyday", action="store_true", help="每天（默认）")
    p_setup.add_argument("--scripts-dir", default="", help="cli.py 所在目录")

    sub.add_parser("remove", help="卸载定时任务")
    sub.add_parser("status", help="查询定时任务状态")

    args = parser.parse_args()
    if args.cmd == "setup":
        result = setup(args.times, weekdays_only=args.weekdays, scripts_dir=args.scripts_dir)
    elif args.cmd == "remove":
        result = remove()
    elif args.cmd == "status":
        result = status()
    else:
        parser.print_help()
        return 1

    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(_main())
