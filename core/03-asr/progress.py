# -*- coding: utf-8 -*-
"""进度快照：一次性打印当前跑批状态，供人工/定时任务每 2 小时查阅。

输出内容包括：
- 数据源、总任务数、已转写(注册表 ok)、剩余
- 剩余音频时长与预计耗时（按端到端有效 RTF）
- md / 封面 产出统计（按作者）
- stage1 / stage2 进程与 ASR 服务健康状态
- 两段流水线日志的最后一行
- stage1 当前在跑第几条 / 该条音频时长 / 预计耗时 / 日志静默时长（用于区分
  "长视频在跑" 与 "真的卡死"）

用法：python progress.py
环境变量：WB_SOURCE(lib|guanzhu)  WB_RTF(默认0.85)
"""
import os, sys, json, glob, subprocess, collections, re, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("WB_SOURCE", "lib")

import asr_batch as B           # noqa: E402
import lib_source               # noqa: E402

RTF = float(os.environ.get("WB_RTF", "0.85"))   # 端到端有效 RTF（含排队/渲染开销）


def _tail(path, n=1):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = [x.rstrip() for x in f if x.strip()]
        return " | ".join(lines[-n:]) if lines else "(空)"
    except Exception:
        return "(无)"


def _lock_owner(name):
    """返回 (pid, alive)"""
    p = os.path.join(HERE, f"_{name}.lock")
    if not os.path.exists(p):
        return None, False
    try:
        pid = int(open(p, encoding="utf-8").read().strip())
    except Exception:
        return None, False
    return pid, B._pid_alive(pid)


def _server_up(port=8766):
    """判断本机服务是否在线。

    为什么不能只发一次 HTTP GET：WebUI(8770) 是 Flask dev server，浏览器看板会每秒
    poll /api/live，单次 GET 很容易挤在 3s 超时之外 → 反复误报「WebUI 未运行」
    （2026-09-28/29 连续多轮巡检均为此误报）。所以：GET 重试 2 次，仍失败则退化为
    TCP 连接探测（只证明端口在监听，不读响应体）。
    """
    import socket
    try:
        import requests
        s = requests.Session(); s.trust_env = False
        for _ in range(2):
            try:
                s.get(f"http://127.0.0.1:{port}/", timeout=3)
                return True
            except Exception:
                time.sleep(0.4)
    except Exception:
        pass
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except Exception:
        return False


def _inflight(tasks):
    """推断 stage1 当前正在处理哪一条，并给出该条时长 / 预计耗时 / 日志静默时长。

    为什么需要：stage1 是逐条串行的，处理长视频（如 4 小时合集）时日志会长时间
    不输出任何行，看起来像"卡死"，实际只是单条很长（静默时长 < 该条预计耗时即正常）。
    没有这个区块，每 2 小时的进度同步很容易把"长视频在跑"误判成"跑批挂了"。

    日志按 1..k 连续打印（每条结束时才输出一行），故静默期间的条目就是第 k+1 条。
    """
    p = os.path.join(HERE, "_asr_stage1.log")
    try:
        silent = max(0.0, time.time() - os.path.getmtime(p))
        txt = open(p, encoding="utf-8", errors="replace").read()
    except Exception:
        return None
    # 为什么要匹配 SLICE：一个条目只有在「结束」时才打印 OK/FAIL/SKIP，而长视频切片
    # 从 [i/N] SLICE 那一刻起就要跑几小时。若只认 OK/FAIL，会把已 FAIL 出队的旧条目
    # 误当成"正在跑"（2026-09-29 02:07 实况：日志已到 [7/994] SLICE，却报"在跑第 3 条"）。
    ms = re.findall(r"^\[(\d+)/(\d+)\]\s+(SKIP|OK|FAIL|SLICE)\b", txt, re.M)
    if not ms or not tasks:
        return None
    last_i, total, last_kind = int(ms[-1][0]), int(ms[-1][1]), ms[-1][2]
    # SLICE 行 = 该条正在处理；OK/FAIL/SKIP 行 = 该条已结束 → 下一条
    i = last_i if last_kind == "SLICE" else last_i + 1
    if i > len(tasks) or i > total:
        return {"idx": None, "total": total, "silent_min": silent / 60}
    t = tasks[i - 1]
    dur = t.get("dur_csv") or 0
    eta = dur * RTF / 60 if dur else 0
    return {"idx": i, "total": total, "author": t.get("author", "?"),
            "vid": t.get("vid", "?"), "dur_min": dur / 60, "eta_min": eta,
            "silent_min": silent / 60}


def snapshot():
    reg = B.load_registry()
    ok_map = {v: m for v, m in reg.items() if m.get("ok")}

    src = B.SOURCE
    if src == "lib":
        tasks = lib_source.build_lib_tasks()
    else:
        tasks = B.build_task_list()

    total = len(tasks)
    done = [t for t in tasks if t["vid"] in ok_map]
    todo = [t for t in tasks if t["vid"] not in ok_map]
    todo_dur = sum(t.get("dur_csv") or 0 for t in todo)
    done_dur = sum(t.get("dur_csv") or 0 for t in done)

    # 产出统计
    # ⚠️ 2026-10-08 修正：博主目录已重构为 `OUT_ROOT/博主/<作者>/`（2026-10-03 起）。
    # 原来这里 glob(HERE/*/*.md) 是**根目录下一层**，重构后 md 实际在**两层**下，
    # 于是恒定匹配 0 篇 → 快照里「逐字稿 md 总数」永远是 0（与 verify_all.py 同一类毛病：
    # 都写死了旧源口径、忽略 WB_SOURCE）。改用 asr_batch 的 AUTHORS_DIR 常量，别再自己拼路径。
    md_files = glob.glob(os.path.join(B.AUTHORS_DIR, "*", "*.md"))
    md_total = len(md_files)
    md_by_author = collections.Counter()
    for f in md_files:
        md_by_author[os.path.basename(os.path.dirname(f))] += 1

    s1_pid, s1_alive = _lock_owner("stage1")
    s2_pid, s2_alive = _lock_owner("stage2")
    ui_pid, ui_alive = _lock_owner("webui")
    srv = _server_up()
    ui_srv = _server_up(8770)

    print("=" * 62)
    print(f"[进度快照] {B.datetime.now(B.CST).strftime('%Y-%m-%d %H:%M:%S')}")
    print("-" * 62)
    print(f"数据源        : {src}  ({'自媒体视频库' if src=='lib' else '关注 m4a/mp4'})")
    print(f"任务总数      : {total}")
    print(f"已转写成功    : {len(done)}  ({len(done)/total*100:.1f}%)" if total else "")
    print(f"剩余待转写    : {len(todo)}  ({len(todo)/total*100:.1f}%)" if total else "")
    print(f"剩余音频时长  : {todo_dur/3600:.2f} h   已完成 {done_dur/3600:.2f} h")
    if todo_dur > 0:
        eta = todo_dur * RTF
        print(f"预计剩余耗时  : {eta/3600:.1f} h  (≈{eta/86400:.1f} 天, 有效RTF={RTF})")
    print("-" * 62)
    print(f"逐字稿 md 总数: {md_total}")
    for a, c in md_by_author.most_common():
        print(f"    {c:>4}  {a}")
    print("-" * 62)
    print(f"stage1 进程   : pid={s1_pid}  {'运行中' if s1_alive else '未运行'}")
    print(f"stage2 进程   : pid={s2_pid}  {'运行中' if s2_alive else '未运行'}")
    print(f"ASR 服务 8766 : {'在线' if srv else '离线'}")
    print(f"WebUI 8770    : {'在线' if ui_srv else '离线'}  pid={ui_pid} "
          f"(http://127.0.0.1:8770/)")
    print("-" * 62)
    print("stage1 末行   : " + _tail(os.path.join(HERE, "_asr_stage1.log"), 1))
    print("stage2 末行   : " + _tail(os.path.join(HERE, "_asr_stage2.log"), 1))
    inf = None
    try:
        inf = _inflight(tasks)
    except Exception as e:
        print(f"stage1 在跑   : (解析失败: {e})")
    if inf:
        if inf.get("idx"):
            print("-" * 62)
            print(f"stage1 在跑   : 第 {inf['idx']}/{inf['total']} 条 "
                  f"({inf['author']} {inf['vid']})")
            if inf["dur_min"]:
                print(f"                该条音频 {inf['dur_min']:.1f} 分钟 "
                      f"→ 预计处理 ~{inf['eta_min']:.0f} 分钟 "
                      f"(RTF {RTF})；本条已跑 {inf['silent_min']:.0f} 分钟")
                budget = max(inf["eta_min"] * 1.6, inf["eta_min"] + 15)
                if inf["silent_min"] > budget:
                    print(f"  ⚠️  静默 {inf['silent_min']:.0f} 分钟已远超该条预算 "
                          f"{budget:.0f} 分钟 —— 疑似卡死/长跑异常，需人工确认")
                else:
                    print("                （长视频单条未完成时日志本就长时间无输出，属正常）")
            else:
                print(f"                本条时长未知；日志已静默 {inf['silent_min']:.0f} 分钟")
        else:
            print("-" * 62)
            print(f"stage1 在跑   : 队列已到最后一条，日志静默 {inf['silent_min']:.0f} 分钟")
    print("=" * 62)
    return {
        "total": total, "done": len(done), "todo": len(todo),
        "todo_hours": round(todo_dur / 3600, 2),
        "stage1_alive": s1_alive, "stage2_alive": s2_alive, "server": srv,
        "webui_alive": ui_srv,
    }


if __name__ == "__main__":
    snapshot()
