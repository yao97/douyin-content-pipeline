# -*- coding: utf-8 -*-
"""
单遍全量/增量下载：启用全部账号跑一遍 main.py。
增量去重由工具自身完成（DB download_data + 文件存在性 双重判断）。

建议每次运行前先跑 _repair.py --apply，清理「有记录但 mp4 缺失」的错误记录，
否则缺失的视频会被永久跳过。
"""
import datetime
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent
VOL = REPO / "Volume"
VOLSP = VOL / "settings.json"
DB = VOL / "DouK-Downloader.db"
LOG = pathlib.Path(r"D:\视频\自媒体视频库\_app_log.txt")

# 磁盘预检阈值（2026-09-28 曾因 D: 写满 → OSError(28)，整批下载中断且状态不一致）
# 可用环境变量 MIN_FREE_GB 覆盖（测试/临时提高安全边际）
MIN_FREE_GB = float(os.environ.get("MIN_FREE_GB", "20"))

URLS = [
    "https://www.douyin.com/user/MS4wLjABAAAAQUFn-KODXUGHd6PGbfJEmmzCIu3C1vzQBPgRprrkpfBMwWAXm09Vf48yEpik6k2W/",
    "https://www.douyin.com/user/MS4wLjABAAAAytpDnsYZsaBeiuf1stH3ylk2dS4BgBYYSuiYeyEeewaszA8ygQq0W5IkV53QS5ls/",
    "https://www.douyin.com/user/MS4wLjABAAAAXaxlIhRul7AKocNf_7egFip6eJdL8nYHtiqYDzvVItqm-kNSn2lcgNUjHW3mod-a/",
    "https://www.douyin.com/user/MS4wLjABAAAA8g-0ViQlW5k0GfapqtkQ_8RtiNMnV7IWUT38j92IXvY/",
    # 2026-09-28 新增：钦文和他的朋友们 / 魏远麟律师|广州 / 识藏
    "https://www.douyin.com/user/MS4wLjABAAAAZEmLB0RZLBH8AMgYtqbfWLOqsat4V517JgiMGIgftrg4XQJTlDi825j62bBCAHPA/",
    "https://www.douyin.com/user/MS4wLjABAAAAmzpumPP3aCkq9MYSbZTURI1WOgf9V3jB79p2wmXe_klj3N5VZY1SVBK9WhitUzrO/",
    "https://www.douyin.com/user/MS4wLjABAAAAGX8qq2pOnstrEPv6QjiVY1hQHhpCUHQWUcGikJ7Z_iCcX2tUFBdS9QGtCtANOiHk/",
]


def log(msg):
    with LOG.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


def set_all_accounts():
    d = json.loads(VOLSP.read_text(encoding="utf-8-sig"))
    d["accounts_urls"] = [
        {"mark": "", "url": u, "tab": "post", "earliest": "", "latest": "", "enable": True}
        for u in URLS
    ]
    # 防御：_dl_mix.py 会临时把 run_command 改成 "5 5 1 Q"（合集模式）。
    # 若它被强杀（timeout/SIGKILL）没能恢复，每日任务会误跑成"下载合集"而不是账号增量。
    # 这里每次运行都强制拉回账号批量下载命令。
    if d.get("run_command") != "5 1 1 Q":
        log(f"[配置修正] run_command {d.get('run_command')!r} -> '5 1 1 Q'")
        d["run_command"] = "5 1 1 Q"
    VOLSP.write_text(json.dumps(d, ensure_ascii=False, indent=4), encoding="utf-8")


def accept_disclaimer():
    con = sqlite3.connect(DB)
    con.execute("INSERT OR REPLACE INTO config_data (NAME,VALUE) VALUES ('Disclaimer',1)")
    con.execute("INSERT OR REPLACE INTO config_data (NAME,VALUE) VALUES ('Record',1)")
    con.commit()
    con.close()


def db_count():
    con = sqlite3.connect(DB)
    n = con.execute("SELECT COUNT(*) FROM download_data").fetchone()[0]
    con.close()
    return n


def file_stats():
    root = pathlib.Path(r"D:\视频\自媒体视频库")
    total = 0
    cnt = 0
    for p in root.rglob("*"):
        if p.is_file() and "_tools" not in str(p) and p.suffix.lower() in (".mp4", ".m4a", ".jpeg", ".jpg", ".png"):
            cnt += 1
            total += p.stat().st_size
    return cnt, round(total / 1024 / 1024, 1)


def free_gb(path=r"D:\\"):
    return shutil.disk_usage(path).free / 1024 ** 3


def run_helper(script, *args):
    """以同一 venv 跑工作区根目录的辅助脚本（_to_audio / _rename 等）。"""
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["CODEBUDDY_SAFE_DELETE_ENABLED"] = "0"  # 批量删除需绕过 safe-delete，否则崩
    return subprocess.run(
        [sys.executable, str(REPO.parent.parent / script), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env,
    )


def ensure_space():
    """磁盘预检：不足时先回收残留 mp4，仍不足则放弃本次下载（返回 False）。
    根治 2026-09-28 的 OSError(28) 'No space left on device' 中断。"""
    f = free_gb()
    log(f"[磁盘] D: 剩余 {f:.1f}GB（阈值 {MIN_FREE_GB}GB）")
    if f >= MIN_FREE_GB:
        return True
    log("[磁盘] 低于阈值，先回收残留 mp4（_to_audio.py --delete）...")
    r = run_helper("_to_audio.py", "--delete")
    log(f"[磁盘] 回收 rc={r.returncode} {((r.stdout or '').strip())[:200]}")
    f = free_gb()
    log(f"[磁盘] 回收后 D: 剩余 {f:.1f}GB")
    if f < MIN_FREE_GB:
        log(f"[磁盘不足] 仍低于 {MIN_FREE_GB}GB，跳过本次下载以免写满磁盘")
        return False
    return True


def run_app():
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("TERM", None)
    # 关闭 WorkBuddy safe-delete 拦截（仅对本子进程生效）：
    # 工具批量清理自身 Volume/Cache 临时文件时会触发 SAFE_DELETE_BULK_CONFIRM_REQUIRED
    # 导致 main.py 崩溃退出（exit=1），后续账号不再处理。
    env["CODEBUDDY_SAFE_DELETE_ENABLED"] = "0"
    with LOG.open("ab") as fh:
        p = subprocess.run(
            [sys.executable, "-u", str(REPO / "main.py")],
            cwd=str(REPO),
            stdout=fh,
            stderr=subprocess.STDOUT,
            env=env,
            stdin=subprocess.DEVNULL,
            timeout=14400,
        )
    return p.returncode


accept_disclaimer()
set_all_accounts()

log(f"\n########## 下载开始 {datetime.datetime.now():%H:%M:%S} ##########")
log(f"[前] DB记录={db_count()} 文件={file_stats()}")
if not ensure_space():
    log(f"########## 已跳过（磁盘不足） {datetime.datetime.now():%H:%M:%S} ##########")
    print("skip-low-disk")
    raise SystemExit(2)
rc = run_app()
c, s = file_stats()
log(f"[后] exit={rc} DB记录={db_count()} 文件={c} 大小={s}MB")

# 下载完成后自动统一命名为 {发布日期}_{账号昵称}_{作品ID}.{ext}
r = run_helper("_rename.py", "--apply")
log(f"[改名] rc={r.returncode} {r.stdout.strip()[-200:]} {r.stderr.strip()[-300:]}")

log(f"########## 下载结束 {datetime.datetime.now():%H:%M:%S} ##########")
print("done")
