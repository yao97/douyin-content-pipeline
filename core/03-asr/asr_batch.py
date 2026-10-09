# -*- coding: utf-8 -*-
"""
关注目录视频 → 逐字稿批量生成
阶段1: 调本地 ASR(8766) 转写，原始结果落盘 _asr_raw/<videoId>.json
阶段2: Ollama 提关键词 → 生成 markdown + 复制封面

用法:
  python asr_batch.py stage1 [limit]    # 只跑转写
  python asr_batch.py stage2 [limit]    # 只跑关键词+渲染
  python asr_batch.py all [limit]
  python asr_batch.py slice <vid> [每段秒数]   # 指定作品切片转写并合并（可断点续跑）
  python asr_batch.py slice-all [门槛秒数]     # 批量切片处理「未完成且超长」的作品

长视频策略（2026-09-28 起）:
  时长 ≥ `WB_SLICE_MIN`(默认 1200s = **20 分钟**) 视为**长视频**，
  stage1 会**自动**走「本地 ffmpeg 切段 → 逐段 ASR → 按序拼接成一条逐字稿」，
  每段结果独立落盘 `_slice_tmp/<vid>/<tag>/seg_XXX.json`，崩了能断点续跑。
  可用 `WB_SLICE_AUTO=0` 关掉自动切片（退回整条提交）。
"""
import os, sys, json, time, re, shutil, subprocess, base64, gzip, glob, math, threading
from datetime import datetime, timezone, timedelta

import requests

# ── 配置 ──
# ⚠️ 下面这些「本机实盘路径/地址」全部可用环境变量覆盖，默认值 = 本机原值 → **行为零变化**。
#    目的是让**第二台机器**（跨机转写）clone 同一套代码后，只靠环境变量就能跑起来，
#    不必去改代码（改代码会造成「实盘与仓库分叉」，见仓库 docs/10-Docker部署.md 的取舍）。
#      WB_ASR_API      ASR 服务地址（外机为它自己的 127.0.0.1:8766，一般不用改）
#      WB_OUT_ROOT     知识库根目录（外机改成自己的盘符/路径）
#      WB_FFMPEG       ffmpeg 可执行文件绝对路径（imageio-ffmpeg 的静态二进制）
#      WB_SERVER_DIR   video-analyzer/backend（仅「自行 Popen 拉起 8766」时用；外机装了服务则用不到）
#      WB_LEGACY_ROOT / WB_APPDATA  旧源「关注」与 facts.json（lib 模式下几乎用不到）
ASR_API   = os.environ.get("WB_ASR_API", "http://127.0.0.1:8766")
OLLAMA    = os.environ.get("WB_OLLAMA", "http://127.0.0.1:11434")
LLM_MODEL = "Qwen3.5-4B:latest"
ROOT      = os.environ.get("WB_LEGACY_ROOT", r"C:\Users\EDY\Videos\data\关注")
APPDATA   = os.environ.get("WB_APPDATA", r"C:\Users\EDY\Videos\data\.appdata")
OUT_ROOT  = os.environ.get("WB_OUT_ROOT", r"D:\视频\媒体知识库")
AUTHORS_DIR = os.path.join(OUT_ROOT, "博主")   # 博主子目录（2026-10-03：博主文件夹统一挪到这里，根目录只留 _ 系统文件）
RAW_DIR   = os.path.join(OUT_ROOT, "_asr_raw")
FFMPEG    = os.environ.get(
    "WB_FFMPEG",
    r"C:\Users\EDY\Projects\video-analyzer\backend\asr_venv\Lib\site-packages"
    r"\imageio_ffmpeg\binaries\ffmpeg-win-x86_64-v7.1.exe")
CST       = timezone(timedelta(hours=8))
# 服务端崩溃重启后，内存里的 job 会被 DB 恢复成 `audio_ready` 且**永不推进**（僵尸作业）。
# 客户端等 `done` 时若发现状态停在 audio_ready 超过这个秒数，就判死并重提（否则要白等
# `dur*3+300`，890s 的段 ≈ 49 分钟）。见 `asr_transcribe_audio` 的等 done 循环。
ZOMBIE_SEC = float(os.environ.get("WB_ZOMBIE_SEC", "120"))
# 8766 由 Windows 服务托管时，崩溃后等它自动重启的**最长**秒数（nssm 重启可能有退避延迟）。
SERVICE_WAIT = float(os.environ.get("WB_SERVICE_WAIT", "600"))

os.makedirs(RAW_DIR, exist_ok=True)
os.makedirs(AUTHORS_DIR, exist_ok=True)

# ── 转写注册表 ──
# 只靠「_asr_raw/<vid>.json 是否存在」判断跳过是脆弱的：文件可能是空的、截断的、
# 或一次失败转写留下的残缺结果。这里登记每条「完整且成功」的转写，重跑时据此跳过；
# 不完整的一律重新进入转写流程。
REGISTRY  = os.path.join(OUT_ROOT, "_asr_registry.json")
REG_LOCK  = REGISTRY + ".lock"      # 注册表「读-改-写」互斥锁（见 _reg_guard）
MIN_CHARS    = int(os.environ.get("WB_MIN_CHARS", "10"))     # 有效转写的最少字符数
MAX_ATTEMPTS = int(os.environ.get("WB_MAX_ATTEMPTS", "2"))   # 单条最多重试几次后放弃

_reg_cache = None
_reg_stamp = None     # 上次加载时注册表文件的 (mtime, size)，用于检测外部改动


def _file_stamp(path):
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except Exception:
        return None


def load_registry():
    if not os.path.exists(REGISTRY):
        return {}
    try:
        with open(REGISTRY, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        time.sleep(0.2)                       # 可能的瞬时占用，重试一次
        try:
            with open(REGISTRY, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
        # ⚠️ **绝不能静默返回 {}**（旧实现就是 `except: return {}`）—— 那等于"所有作品都没转过"，
        # 跑批会把全部作品（当前 650+ 条 / 上百小时）重转一遍，代价不可逆。
        # 也不该只是退出：先留个损坏副本便于人工修复，再抛错让上层明确停跑。
        # （写入侧 `save_registry` 已是 tmp + os.replace 原子写，正常不会产生半截文件。）
        bak = REGISTRY + ".corrupt"
        try:
            shutil.copy2(REGISTRY, bak)
        except Exception:
            pass
        raise RuntimeError(
            "注册表读取失败（%s）：%s —— 已备份到 %s，请人工检查后重启跑批"
            % (REGISTRY, e, bak)) from e
    return {}


def save_registry(reg):
    tmp = REGISTRY + ".tmp"
    json.dump(reg, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.replace(tmp, REGISTRY)


# ── 注册表互斥用的跨平台「内核字节锁」原语 ──
# Windows: msvcrt.locking(LK_NBLCK) ；POSIX: fcntl.flock(LOCK_EX|LOCK_NB)
# 两者都由**内核**在句柄关闭 / 进程退出时自动释放 → 进程被 kill 也不会留死锁。
try:                                     # pragma: no cover - 平台分支
    import msvcrt

    def _lock_fd(fd):
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock_fd(fd):
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
except ImportError:                      # pragma: no cover - 平台分支
    import fcntl

    def _lock_fd(fd):
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_fd(fd):
        fcntl.flock(fd, fcntl.LOCK_UN)


_REG_RLOCK = threading.RLock()    # 进程内：同进程重入放行 / 多线程串行
_REG_DEPTH = [0]                  # 进程内已持文件锁的层数（可重入计数）


class _RegGuard:
    """注册表「读 → 改 → 写」跨进程互斥（2026-10-01 新增）。

    为什么必须有：`update_entry()` 虽然已经「先重读磁盘、只改自己那条」，但它仍是
    **load → modify → save 三步**。两个进程改**不同** vid 时，若 A 的 load 与 B 的 load
    都发生在对方 save 之前，后 save 的那份表就不含对方的改动 → **静默丢更新**。
    窗口 = 读写一次 772 条 JSON 的时间 —— 单次跑批撞上的概率低，
    但「守候式 fetch / checkpoint / stage1 / stage2」长期同时跑时会被反复撞。
    隔离压测（8 进程 × 40 次各写自己的 vid）：**无锁丢 270/320 = 84.4%，加锁零丢失**。

    三个真实写者（都会与 stage1 并行）：
      · stage1 每条转完 `update_entry()`
      · checkpoint.py 定期 `retry_infra()`（detached 进程！）
      · 人工 `accept` / `retry` / `registry`，以及 `remote_handoff.py fetch --apply`

    ⚠️ 实现踩过的坑（别退回 O_EXCL 文件锁）：最初用「`os.open(O_EXCL)` 建锁文件 +
    `os.remove` 释放」，在 Windows 上必然翻车 —— `os.remove` 是**标记删除**
    （delete pending），窗口期别的进程 open 同一路径直接 `PermissionError: Errno 13`，
    这不是"锁被占"，却会被当成异常抛出；而且进程被 kill 时锁文件残留，得靠 stale 超时
    （120s）兜底，跑批要白等。
    → 改用**内核字节锁**：Windows `msvcrt.locking(LK_NBLCK)` / POSIX `fcntl.flock`。
      优点：① 占用与否是"锁不上"而不是"抛异常"；② **进程退出/被杀由 OS 自动释放**，
      没有陈旧锁；③ 锁文件**永不删除**（常驻，无害），彻底避开 pending-delete。

    **降级策略**：拿不到锁就自旋等，超过 `WB_REG_LOCK_SEC`(20) 秒则放弃加锁直接执行
      —— 宁可保留极小概率的丢更新，也不能让跑批卡死。
    环境变量：`WB_REG_LOCK_SEC`(20) / `WB_REG_LOCK_POLL`(0.005)
    """

    def __init__(self, timeout=None):
        self.timeout = float(os.environ.get("WB_REG_LOCK_SEC", "20")
                             if timeout is None else timeout)
        self.poll = float(os.environ.get("WB_REG_LOCK_POLL", "0.005"))
        self.fd = None
        self.waited = 0.0
        self.degraded = False
        self.reentrant = False

    def __enter__(self):
        # ① 进程内：RLock 保证同进程重入直接放行、多线程串行（否则嵌套调用会白等超时）
        _REG_RLOCK.acquire()
        if _REG_DEPTH[0] > 0:
            _REG_DEPTH[0] += 1
            self.reentrant = True
            return self
        # ② 跨进程：内核字节锁
        t0 = time.time()
        try:
            fd = os.open(REG_LOCK, os.O_CREAT | os.O_RDWR)
        except OSError:
            _REG_DEPTH[0] = 1            # 连文件都开不了 → 直接干，别卡跑批
            self.degraded = True
            return self
        while True:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                _lock_fd(fd)              # 非阻塞；已被占用 → OSError
                self.fd = fd
                _REG_DEPTH[0] = 1
                return self
            except OSError:
                if time.time() - t0 > self.timeout:
                    self.degraded = True
                    self.waited = time.time() - t0
                    try:
                        os.close(fd)
                    except Exception:
                        pass
                    _REG_DEPTH[0] = 1     # 降级：不阻塞跑批
                    return self
                # 轮询粒度：注册表写是**低频**操作（stage1 每条转写才写一次），
                # 但批处理里会有连续写。5ms 兼顾「不空转 CPU」与「不放大串行总耗时」。
                time.sleep(self.poll)

    def __exit__(self, *exc):
        try:
            _REG_DEPTH[0] -= 1
            if getattr(self, "reentrant", False) or _REG_DEPTH[0] > 0:
                return False              # 外层还持着，别释放
            if self.fd is not None:
                try:
                    os.lseek(self.fd, 0, os.SEEK_SET)
                    _unlock_fd(self.fd)
                except Exception:
                    pass
                try:
                    os.close(self.fd)
                except Exception:
                    pass
                self.fd = None
        finally:
            try:
                _REG_RLOCK.release()
            except Exception:
                pass
        return False


def _reg_guard(timeout=None):
    return _RegGuard(timeout)


def _reg():
    """读注册表（带缓存，但会检测文件是否被其它进程改过而自动重载）。

    为什么必须检测：stage1 是长跑进程，若只读一次就缓存住，
    ① 期间用 `accept`/`retry` 在别的进程改的内容它看不到；
    ② 它后续 save_registry 会把旧整表回写，**直接覆盖掉**那些改动。
    """
    global _reg_cache, _reg_stamp
    cur = _file_stamp(REGISTRY)
    if _reg_cache is None or cur != _reg_stamp:
        _reg_cache = load_registry()
        _reg_stamp = cur
    return _reg_cache


def update_entry(vid, patch=None, remove=(), delete=False, guarded=True):
    """只更新注册表里某一条：**先重读磁盘**再改，避免长跑进程用旧缓存整表回写。

    这是"并发安全"的关键：多个进程（stage1 / checkpoint / 人工 CLI）同时改注册表时，
    各自只动自己那条，不会互相抹掉。

    ⚠️ 但「只改自己那条」本身**不够**：load → modify → save 三步里，两个进程改**不同**
    vid 且 load 都早于对方 save 时，后写的表不含对方改动 → 仍会静默丢更新。
    所以整个序列要套 `_reg_guard()`（跨进程互斥）。`guarded=False` 仅供**已持锁的调用方**
    （如 `retry_infra` 批量放行）避免自锁，别在别处用。
    """
    global _reg_cache, _reg_stamp
    if not guarded:
        return _update_entry_locked(vid, patch=patch, remove=remove, delete=delete)
    with _reg_guard():
        return _update_entry_locked(vid, patch=patch, remove=remove, delete=delete)


def _update_entry_locked(vid, patch=None, remove=(), delete=False):
    """`update_entry` 的实际实现 —— **必须在 `_reg_guard()` 保护下调用**。"""
    global _reg_cache, _reg_stamp
    fresh = load_registry()          # 永远基于磁盘最新内容
    if delete:
        fresh.pop(vid, None)
    else:
        e = fresh.get(vid) or {}
        for k in remove:
            e.pop(k, None)
        if patch:
            e.update(patch)
        if e:
            fresh[vid] = e
        else:
            fresh.pop(vid, None)
    save_registry(fresh)
    _reg_cache = fresh
    _reg_stamp = _file_stamp(REGISTRY)
    return fresh.get(vid) or {}


def validate_raw(data):
    """判断一条转写结果是否『完整且成功』，返回 (ok, 原因)"""
    if not isinstance(data, dict):
        return False, "内容不是 dict"
    text = (data.get("text") or "").strip()
    if not text:
        return False, "text 为空"
    if text in ("（无语音内容）", "（无内容）"):
        return False, "无语音占位符"
    dur = data.get("duration") or 0
    if not dur or dur <= 0:
        return False, "duration 无效"
    if len(text) < MIN_CHARS:
        return False, f"文本过短({len(text)}字)"
    if not data.get("vid"):
        return False, "缺少 vid"
    return True, "ok"


def is_transcribed(vid):
    """该视频是否已完整转写成功（可跳过）。注册表为准，同时核对磁盘文件仍在。"""
    e = _reg().get(vid)
    if not e:
        return False
    if not (e.get("ok") or e.get("abandoned")):
        return False
    p = os.path.join(RAW_DIR, vid + ".json")
    if not os.path.exists(p):
        return False
    try:
        data = json.load(open(p, encoding="utf-8"))
    except Exception:
        return False
    # 「低内容」已人工确认（本就几乎无语音）→ 不再套用字数校验
    if e.get("low_content"):
        return True
    ok, _ = validate_raw(data)
    return ok


def mark_ok(vid, data):
    """登记一条成功的转写"""
    update_entry(vid, {
        "ok": True,
        "author": data.get("author", ""),
        "pub": data.get("pub", ""),
        "duration": data.get("duration", 0),
        "chars": len((data.get("text") or "").strip()),
        "job_id": data.get("job_id", ""),
        "ts": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
    }, remove=("attempts", "why", "abandoned", "low_content", "note"))
    _gc_slice_tmp(vid)


# ── 切片缓存自动清理（A 方案，2026-10-09）──
# 背景：`_slice_tmp/<vid>/` 存的是切片转写时的段 wav + seg_XXX.json，只用于**断点续跑**。
# 全段完成后合并成标准 raw 并 mark_ok → 这些段就是纯垃圾，且不清理会长到把盘撑爆：
# 实测 2026-10-09 涨到 **19GB**，把 D 盘（214G）撑到 **100% 满 / Avail=0**，
# 随后 stage1 在 `save_registry()` 抛 `OSError: [Errno 28]` 崩溃（注册表就在 D 盘）。
# 所以必须挂在 mark_ok 之后自动清，而不是等人工想起来。
SLICE_GC = os.environ.get("WB_SLICE_GC", "1") == "1"   # 开关，留 0 可临时关闭


def _gc_slice_tmp(vid):
    """删掉某作品的切片段缓存（作品已 ok 之后才调）。

    三重保险，任何一条不满足就**原样跳过、绝不删**：
      ① `WB_SLICE_GC=0` 开关关着→ 跳过
      ② 注册表该条 `ok` 不为真 → 跳过（**防正在转写的段被误删**，最关键的一条）
      ③ raw 主文件不存在 → 跳过（raw 没了、段缓存是唯一副本，删了就真丢）

    删失败只打印不抛：清理是「锦上添花」，绝不能因为删不掉而让一次成功的转写报失败。
    """
    if not SLICE_GC:
        return
    try:
        if not (entry(vid) or {}).get("ok"):
            return                      # 保险②：还没成功，段缓存还得留着续跑
        raw_p = os.path.join(RAW_DIR, vid + ".json")
        if not os.path.exists(raw_p):
            return                      # 保险③：raw 不在，段是唯一副本，不动
        d = os.path.join(SLICE_DIR, vid)
        if not os.path.isdir(d):
            return
        freed = 0
        for root, _, files in os.walk(d):
            for f in files:
                try:
                    freed += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        import shutil
        shutil.rmtree(d)
        print(f"[gc] 已清切片缓存 {vid} 释放 {freed/2**30:.2f}GB", flush=True)
    except Exception as e:
        print(f"[gc] 清理切片缓存失败 {vid}（忽略）: {type(e).__name__}: {str(e)[:80]}",
              flush=True)


def mark_bad(vid, why=""):
    """把一条不完整的转写从注册表里标为无效"""
    update_entry(vid, delete=True)


def mark_fail(vid, why):
    """记录一次失败的转写。达到 MAX_ATTEMPTS 后标记为 abandoned，不再反复重试
    （避免"本就几乎无语音"的视频每次重跑都被无限重试）
    """
    e = entry(vid)                      # 从磁盘取最新，避免用旧缓存的 attempts
    n = int(e.get("attempts") or 0) + 1
    patch = {"ok": False, "attempts": n, "why": why,
             "ts": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")}
    if n >= MAX_ATTEMPTS:
        patch["abandoned"] = True
    update_entry(vid, patch)


def mark_low_content(vid, note=""):
    """确认『本就几乎无语音』：标记为已转写成功(低内容)，不再重试。
    用于多次独立转写结果一致、可判定为源视频本身无有效语音的情况。
    """
    update_entry(vid, {
        "ok": True, "low_content": True, "note": note,
        "ts": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
    }, remove=("abandoned", "attempts", "why"))
    print(f"[registry] {vid} 已标记为『低内容·转写成功』，后续跳过: {note}", flush=True)


def entry(vid):
    return _reg().get(vid) or {}


# 判定"失败原因属于基础设施故障"（ASR 服务掉线 / 代理 / 超时），而非源文件本身有问题。
# 这类失败重试有意义：修好服务再来一次通常就过了。
INFRA_MARKERS = (
    "HTTPConnectionPool", "NewConnectionError", "积极拒绝", "Max retries",
    "ProxyError", "ReadTimeout", "ConnectionReset", "ConnectionError",
    "抽音频超时", "转写超时", "服务", "502", "503", "504",
)


def is_infra_failure(why):
    w = str(why or "")
    return any(m in w for m in INFRA_MARKERS)


def retry_infra(max_rounds=3):
    """放行因『基础设施故障』被 abandoned 的条目，让它们重新进入转写流程。

    只处理失败原因是连接类/超时的条目；每条最多放行 max_rounds 次
    （retry_rounds 计数），防止真正损坏的文件被无限重试。
    返回被放行的 vid 列表。

    注意：**从磁盘重读**而不是用模块缓存 —— 该函数可能被 checkpoint 在
    stage1 长跑期间调用，用旧缓存会把 stage1 刚写的状态覆盖掉。
    **2026-10-01 起**：整个「读全表 → 批量改 → 写回」套 `_reg_guard()` 互斥 ——
    `checkpoint.py` 是 **detached 进程**，与 stage1 真并行，旧写法（无锁整表回写）
    会把 stage1 在窗口内新写的条目静默盖掉。
    """
    global _reg_cache, _reg_stamp
    with _reg_guard():
        return _retry_infra_locked(max_rounds)


def _retry_infra_locked(max_rounds):
    """`retry_infra` 的实际实现 —— **必须在 `_reg_guard()` 保护下调用**。"""
    global _reg_cache, _reg_stamp
    reg = load_registry()
    freed, exhausted = [], []
    for vid, e in list(reg.items()):
        if e.get("ok") or not e.get("abandoned"):
            continue
        if not is_infra_failure(e.get("why")):
            continue
        rounds = int(e.get("retry_rounds") or 0)
        if rounds >= max_rounds:
            exhausted.append(vid)
            continue
        e.pop("abandoned", None)
        e["ok"] = False
        e["attempts"] = 0            # 重置本轮的尝试预算
        e["retry_rounds"] = rounds + 1
        e["ts"] = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
        reg[vid] = e
        freed.append(vid)
    if freed:
        save_registry(reg)
        _reg_cache = reg
        _reg_stamp = _file_stamp(REGISTRY)
    print(f"[retry] 放行基础设施类失败 {len(freed)} 条（上限 {max_rounds} 轮）:", flush=True)
    for v in freed:
        print(f"  {v}  {reg[v].get('author','')}  (第 {reg[v]['retry_rounds']} 轮)  "
              f"上次失败: {str(reg[v].get('why',''))[:60]}", flush=True)
    if exhausted:
        print(f"[retry] 另 {len(exhausted)} 条已达放行上限（{max_rounds} 轮），需人工处置: "
              f"{', '.join(exhausted[:8])}", flush=True)
    return freed


# ── 单实例锁 ──
# 并发跑两个 stage1 会互相抢 ASR 那唯一的显存槽（MAX_CONCURRENT_TRANSCRIBE=1）；
# 并发两个 stage2 会对同一批文件重复调 LLM。定时任务拉起前必须先过这道锁。
def _lock_path(name):
    return os.path.join(OUT_ROOT, f"_{name}.lock")


LOCK_GRACE_SEC = 15   # 刚被 O_EXCL 创建、pid 尚未落盘的锁，在此窗口内视为「已被持有」


def _pid_alive(pid):
    # 注意：中文 Windows 的 tasklist 输出是 GBK，用 text=True 会按 utf-8 解码
    # 抛 UnicodeDecodeError（且发生在 reader 线程里，报错嘈杂）。改为字节 + replace 解码。
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, timeout=20).stdout.decode("utf-8", "replace")
        return str(pid) in out
    except Exception:
        return False


def acquire_lock(name="stage1"):
    """拿到锁返回 True；已有同名实例在跑返回 False。

    ⚠️ 必须用 O_EXCL **原子创建**，不能「先 exists/读内容判活、再 open("w") 写」——
    那样存在 check-then-write 竞态：checkpoint 的 detached Popen 与 agent 的后台拉起
    几乎同时启动时，两个进程会同时判定「无锁」并双双写锁 → 两个 stage1 并行、对同一
    条视频重复提交 ASR 作业（2026-09-28 实测踩到，白跑约 11 分钟/条）。
    另有「新建后 pid 尚未落盘」的瞬间：期间读到空锁**不能**当陈旧锁删（否则并发
    8 进程实测仍有 3 个拿到锁），用 LOCK_GRACE_SEC 宽限期让位。
    """
    p = _lock_path(name)
    for _ in range(2):
        try:
            fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            old, fresh = 0, False
            try:
                fresh = (time.time() - os.stat(p).st_mtime) < LOCK_GRACE_SEC
                old = int(open(p, encoding="utf-8").read().strip())
            except Exception:
                pass
            if old and _pid_alive(old):
                return False                       # 持有者还活着 → 让位
            if not old and fresh:
                return False                       # 别人刚创建、pid 还没写入的瞬间 → 让位
            try:
                os.remove(p)                       # 陈旧锁（pid 已死 / 内容非法）→ 清掉重试
                continue
            except OSError:
                try:                               # 删不掉（沙箱/权限拦截）→ 退化为覆盖，不能让陈旧锁卡死跑批
                    open(p, "w", encoding="utf-8").write(str(os.getpid()))
                    return True
                except Exception:
                    return False
        except OSError:
            return False
        try:
            os.write(fd, str(os.getpid()).encode("utf-8"))
        finally:
            os.close(fd)
        return True
    return False


def release_lock(name="stage1"):
    try:
        p = _lock_path(name)
        if os.path.exists(p):
            if open(p, encoding="utf-8").read().strip() == str(os.getpid()):
                os.remove(p)
    except Exception:
        pass


def is_abandoned(vid):
    return bool(entry(vid).get("abandoned"))


def build_registry():
    """扫描 _asr_raw 全量重建注册表：把现有成果登记进来，不完整的剔除

    ⚠️ **整表覆盖**，是全项目最危险的注册表操作（会把没扫到的条目全删掉）。
    `_reg_guard()` 锁**覆盖「扫描 → 写回」全程**（不是只锁 save）：
    否则扫描途中 stage1 新 `update_entry()` 的条目会被这份旧快照盖掉。
    也正因此，**跑批进行中不要手工跑 `registry`**（脚本 CLI 会先警告）。
    """
    with _reg_guard(timeout=float(os.environ.get("WB_REG_LOCK_SEC", "20"))):
        return _build_registry_locked()


def _build_registry_locked():
    """`build_registry` 的实际实现 —— **必须在 `_reg_guard()` 保护下调用**。"""
    reg = {}
    ok = bad = 0
    bads = []
    for fn in sorted(os.listdir(RAW_DIR)):
        if not fn.endswith(".json"):
            continue
        vid = os.path.splitext(fn)[0]
        try:
            data = json.load(open(os.path.join(RAW_DIR, fn), encoding="utf-8"))
        except Exception as e:
            bad += 1
            bads.append((vid, f"解析失败: {e}"))
            continue
        good, why = validate_raw(data)
        if good:
            reg[vid] = {
                "ok": True,
                "author": data.get("author", ""),
                "pub": data.get("pub", ""),
                "duration": data.get("duration", 0),
                "chars": len((data.get("text") or "").strip()),
                "job_id": data.get("job_id", ""),
                "ts": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
            }
            ok += 1
        else:
            bad += 1
            bads.append((vid, why))
    save_registry(reg)
    print(f"注册表重建完成：成功 {ok} / 不完整 {bad} → {REGISTRY}", flush=True)
    for v, w in bads[:20]:
        print(f"  [不完整] {v}: {w}", flush=True)
    if len(bads) > 20:
        print(f"  ... 另 {len(bads)-20} 条", flush=True)
    return reg


# 本机 ASR 服务必须绕过系统代理：
# 环境里存在 http_proxy/https_proxy（Clash 等），requests 默认 trust_env=True 会把
# 127.0.0.1:8766 的请求也甩给代理，导致 502 / ProxyError。
# 云端 API 仍需走代理，故只对本地会话关闭 trust_env。
ASR_SESS = requests.Session()
ASR_SESS.trust_env = False


# ── 元数据 ──
def load_json_b64(path):
    s = open(path, encoding="utf-8").read()
    m = re.search(r'"([A-Za-z0-9+/=]+)"', s)
    return json.loads(gzip.decompress(base64.b64decode(m.group(1))))


def load_meta():
    facts = json.load(open(os.path.join(APPDATA, "facts.json"), encoding="utf-8"))
    return facts["authors"], facts["videos"], facts["videoDescriptions"]


def probe_duration(path):
    try:
        p = subprocess.run([FFMPEG, "-i", path, "-hide_banner"],
                           capture_output=True, timeout=60)
        s = p.stderr.decode("utf-8", "replace")
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", s)
        if m:
            return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except Exception:
        pass
    return None


def build_task_list():
    """返回 [(aid, vid, video_path, cover_path, duration, 作者名, 抖音号, 标题, 发布时间)]"""
    authors, videos, descs = load_meta()
    tasks = []
    for aid in sorted(os.listdir(ROOT)):
        vdir = os.path.join(ROOT, aid, "视频")
        cdir = os.path.join(ROOT, aid, "封面")
        if not os.path.isdir(vdir):
            continue
        an = authors.get(aid, {})
        nickname = (an.get("nicknames") or [aid])[0]
        uids = an.get("uniqueIds") or []
        account = uids[0] if uids and uids[0] else ""
        for f in sorted(os.listdir(vdir)):
            if not f.lower().endswith(".mp4"):
                continue
            vid = os.path.splitext(f)[0]
            vp = os.path.join(vdir, f)
            cp = os.path.join(cdir, vid + ".jpg")
            vm = videos.get(vid, {})
            ct = vm.get("createTime")
            pub = datetime.fromtimestamp(ct, CST).strftime("%Y-%m-%d") if ct else "未知日期"
            tasks.append({
                "aid": aid, "vid": vid, "video": vp, "cover": cp,
                "author": nickname, "account": account,
                "title": descs.get(vid, ""), "pub": pub,
                "createTime": ct,
            })
    return tasks


# ── 数据源开关 ──
# guanzhu = 旧源 C:\Users\EDY\Videos\data\关注（.mp4 + 封面目录 + facts.json）
# lib     = 自媒体视频库 D:\视频\自媒体视频库（.m4a + 同目录封面 + Data/*.csv）
SOURCE = os.environ.get("WB_SOURCE", "guanzhu")


def _author_filter(tasks):
    """按 `WB_ONLY_AUTHORS` / `WB_SKIP_AUTHORS` 过滤任务表（逗号分隔，作者名**子串**匹配）。

    为什么必须有：外机与本机跑**同一份库**时必须切分，否则两边做同一批。
    2026-10-01 实测：`export` 出的 327 条清单里，**310 条与本机当时未完成完全重叠**
    （清单 ∩ 未完成 = 310/310）—— 两台机器做同一批活儿，先完成者的成果才作数
    （回传合入时后到的那份被去重跳过），另一台就是**纯白烧**
    （按本次口径 ≈ 3.6 天算力，切分后总工期从 86h 降到 ~53h）。

    默认两者都为空 → **行为零变化**（与项目「本机路径全用 `WB_*` 覆盖、默认=本机原值」的约定一致）。
    典型用法（外机已下好 4 个账号的源，本机专心跑超长合集）：
      · 本机：`WB_SKIP_AUTHORS="魏远麟律师 广州,钦文和他的朋友们,识藏,播客正片合集"`
      · 或外机：`WB_ONLY_AUTHORS="…"`（两台都设、或只设一台，效果等价）
    """
    only = [s.strip() for s in os.environ.get("WB_ONLY_AUTHORS", "").split(",") if s.strip()]
    skip = [s.strip() for s in os.environ.get("WB_SKIP_AUTHORS", "").split(",") if s.strip()]
    if not only and not skip:
        return tasks

    def keep(t):
        a = t.get("author") or ""
        if only and not any(k in a for k in only):
            return False
        if skip and any(k in a for k in skip):
            return False
        return True

    kept = [t for t in tasks if keep(t)]
    print("[filter] WB_ONLY_AUTHORS=%s WB_SKIP_AUTHORS=%s → 任务 %d → %d 条"
          % (only or "-", skip or "-", len(tasks), len(kept)), flush=True)
    return kept


def build_task_list_src():
    """全部调用方（stage1 / progress / remote_handoff / webui…）的唯一任务表入口。

    ⚠️ 跨机切分只在这里生效 —— 别在个别调用点各写一份过滤（会造成口径漂移）。
    """
    if SOURCE == "lib":
        import lib_source
        return _author_filter(lib_source.build_lib_tasks())
    return _author_filter(build_task_list())


# ── ASR ──
SERVER_DIR = os.environ.get("WB_SERVER_DIR", r"C:\Users\EDY\Projects\video-analyzer\backend")
SERVER_PY  = os.path.join(SERVER_DIR, "venv", "Scripts", "python.exe")
SERVER_LOG = os.path.join(OUT_ROOT, "_asr_server.log")


SERVICE_NAME = os.environ.get("WB_SERVICE_NAME", "VideoAnalyzer-Transcribe")


def service_registered(name=None):
    """8766 是否由**已注册的 Windows 服务**托管（默认 `VideoAnalyzer-Transcribe`）。

    用注册表判断（`winreg` 标准库）—— **不能用 `sc`/`wmic`**（沙箱拦截）。
    用途：由服务托管时 `ensure_server()` **绝不自己 Popen**，否则会多起一个实例。
    """
    if os.name != "nt":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Services\%s" % (name or SERVICE_NAME)):
            return True
    except Exception:
        return False


def ensure_server(wait_ready=300):
    """健康检查；8766 挂了就等待/拉起（批量任务自愈，避免整批无效失败）

    ⚠️ 2026-09-28 重要修正 ①：8766 实际由 Windows 服务 `VideoAnalyzer-Transcribe`
    （nssm 托管）常驻守护。原来只判 `_alive()`（HTTP 200）就决定是否 Popen，
    在服务"已监听但还没起来"的几秒窗口里会**再拉起一个重复实例** —— 两个模型实例
    同时驻留、端口互相抢占、谁接活不确定。现在先探**端口是否已被监听**：已监听就只等待。

    ⚠️ 2026-09-28 重要修正 ②（真踩过，代价 3.16GB 内存 + 一次 native 崩溃）：
    上面那条**还不够** —— 实测崩溃后 nssm 还没把服务拉起来的那几秒里，`_listening()`
    也是 False，于是 stage1 自己 Popen 了一个实例（ppid 就是 stage1）；nssm 的服务随后
    起来抢走端口，那个 Popen 出来的实例就变成**白占 3.16GB 却接不到活的僵尸服务**
    （在只有 16GB 内存的机器上直接把内存压力推到崩溃边缘，形成「崩→起重复实例→更缺内存→再崩」死循环）。
    → 因此：**只要注册表里存在这个服务，就绝不 Popen，只等它自己重启。**
    """
    def _alive():
        try:
            return ASR_SESS.get(f"{ASR_API}/", timeout=10).status_code == 200
        except Exception:
            return False

    def _listening():
        import socket
        s = socket.socket(); s.settimeout(2)
        try:
            return s.connect_ex(("127.0.0.1", 8766)) == 0
        except Exception:
            return False
        finally:
            s.close()

    def _wait(limit, tag):
        t0 = time.time()
        while time.time() - t0 < limit:
            if _alive():
                print(f"[Server] 服务已就绪（{tag}）", flush=True)
                return True
            time.sleep(5)
        return False

    if _alive():
        return True

    if service_registered():
        limit = max(wait_ready, SERVICE_WAIT)
        print(f"[Server] 8766 无响应，但它由 Windows 服务 {SERVICE_NAME} 托管 → "
              f"只等它自动重启（最长 {limit:.0f}s），**不自行拉起**"
              f"（自行 Popen 会多出一个白占 3GB+ 内存、还抢不到端口的僵尸实例）…", flush=True)
        if _wait(limit, "Windows 服务恢复"):
            return True
        print(f"[Server] 等待 {SERVICE_NAME} 恢复超时（请检查该服务 / nssm 配置）", flush=True)
        return False

    if _listening():
        print("[Server] 8766 端口已有进程监听（可能正在启动），等待就绪…", flush=True)
        if _wait(max(wait_ready, SERVICE_WAIT), "端口已监听"):
            return True
        print("[Server] 等待就绪超时（端口被占但无响应）", flush=True)
        return False

    print("[Server] 8766 无响应且无服务托管，尝试自动拉起服务...", flush=True)
    try:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        subprocess.Popen(
            [SERVER_PY, "transcribe_server.py", "--host", "0.0.0.0", "--port", "8766"],
            cwd=SERVER_DIR,
            stdout=open(SERVER_LOG, "ab"), stderr=subprocess.STDOUT,
            creationflags=flags,
        )
    except Exception as e:
        print(f"[Server] 拉起失败: {e}", flush=True)
        return False
    if _wait(max(wait_ready, SERVICE_WAIT), "自行拉起"):
        print("[Server] 服务已恢复", flush=True)
        return True
    print("[Server] 等待就绪超时", flush=True)
    return False


def asr_transcribe(video_path, vid):
    """上传→抽音频→转写→取文本，返回 {"text","segments","duration"}"""
    dur = probe_duration(video_path)
    if not dur or dur <= 0:
        raise RuntimeError("无法解析视频时长")
    return asr_transcribe_audio(video_path, vid, dur)


def asr_transcribe_audio(audio_path, vid, dur, upload_name=None):
    """同 asr_transcribe，但时长由调用方给定、上传名可自定义（切片转写用）。

    注意 1：服务端会用 `duration` 做 `ffmpeg -t` 截断，所以切片时必须传**该段的真实时长**，
    不能传整条作品的总时长。
    注意 2：上传名**不能以 .wav 结尾**！服务端把上传文件存成 `TEMP/{job}{后缀}`，
    而抽音频的输出固定是 `TEMP/{job}.wav` —— 同名同路径会让 ffmpeg 边读边写、必然失败
    （现象是 job 直接变 no_audio）。所以段文件虽是 wav，也要用 .mp4 名字上传，
    ffmpeg 按内容识别格式，不看扩展名。
    """
    if not dur or dur <= 0:
        raise RuntimeError("无法解析视频时长")
    dur = min(float(dur) + 1.0, 36000)
    name = upload_name or (vid + ".mp4")
    if name.lower().endswith(".wav"):
        name = name[:-4] + ".mp4"

    with open(audio_path, "rb") as fh:
        r = ASR_SESS.post(
            f"{ASR_API}/api/transcribe",
            files={"video": (name, fh, "video/mp4")},
            data={"duration": f"{dur:.3f}"},
            timeout=1800,
        )
    r.raise_for_status()
    job = r.json()["job_id"]

    # 等 audio_ready
    t0 = time.time()
    while True:
        st = ASR_SESS.get(f"{ASR_API}/api/status/{job}", timeout=30).json()
        s = st.get("status")
        if s == "audio_ready":
            break
        if s in ("error", "done"):
            raise RuntimeError(f"抽音频异常: {st}")
        # no_audio：源无音轨/音频无法解码——**必须立刻失败**，
        # 否则会白等到 `抽音频超时`（最长 30 分钟）才报错
        if s == "no_audio":
            raise RuntimeError(f"无音频({st.get('detail') or st.get('error')})")
        if not s:
            # 服务端**不认这个 job**（形如 `{"error":"任务不存在"}`）：通常是服务端崩溃重启/换了实例，
            # 作业只活在那个已死进程的内存里。必须立刻失败重提，否则会一路空转到 `转写超时`。
            raise RuntimeError(f"服务端不认识该作业（{st.get('error') or st}），将重提")
        if time.time() - t0 > max(600, dur * 2):
            raise RuntimeError("抽音频超时")
        time.sleep(2)

    # 启动转写
    r = ASR_SESS.post(f"{ASR_API}/api/transcribe-start",
                      json={"job_id": job, "engine": "qwen3_asr"}, timeout=60)
    r.raise_for_status()

    # 等 done
    # ⚠️ `audio_ready` 停滞必须判死（2026-09-28 事故）：`transcribe-start` 已经发过了，
    # 状态却**停在 audio_ready**（`detail=音频提取完成，请选择识别模型开始转写`）——只有一种解释：
    # 服务端在加载模型时崩了并重启，内存里的作业状态丢失、DB 把 job 恢复成 `audio_ready`
    # （实测 job `0ef60057` 停了 20 分钟、服务端实际空闲，客户端却还在等，890s 的段要白等
    # `dur*3+300`≈49 分钟）。发现停滞就立刻抛错让上层重提新 job，别死等。
    t0 = time.time()
    timeout = max(600, dur * 3 + 300)
    stuck_since = None
    while True:
        st = ASR_SESS.get(f"{ASR_API}/api/status/{job}", timeout=30).json()
        s = st.get("status")
        if s == "done":
            break
        if s == "error":
            raise RuntimeError(f"转写失败: {st.get('error')}")
        if not s:
            # 作业在服务端**消失了**（`{"error":"任务不存在"}`）——服务端崩溃重启后换了个实例，
            # 作业只存在于那个已死进程的内存里。原实现没有这个分支 → 会一路空转到 `转写超时`
            # （890s 段 ≈ 49 分钟）。实测 2026-09-28 `734b2842` 就是这个形状。
            raise RuntimeError(f"作业 {job} 在服务端不存在（{st.get('error') or st}），将重提")
        if s == "audio_ready":
            if stuck_since is None:
                stuck_since = time.time()
            elif time.time() - stuck_since > ZOMBIE_SEC:
                raise RuntimeError(
                    f"作业 {job} 疑似服务端重启遗留（audio_ready 停滞 "
                    f"{ZOMBIE_SEC}s 未推进），重提新作业")
        else:
            stuck_since = None          # 只要推进过（waiting/transcribing）就重置
        if time.time() - t0 > timeout:
            raise RuntimeError(f"转写超时({timeout}s)")
        time.sleep(5)

    res = ASR_SESS.get(f"{ASR_API}/api/result/{job}", timeout=60).json()
    result = res.get("result") or {}
    return {"text": result.get("text", ""),
            "segments": result.get("segments", []),
            "duration": round(dur, 1), "job_id": job}


REUSE_JOB_H = float(os.environ.get("WB_REUSE_JOB_H", "12"))   # 孤儿作业复用：只看这么多小时内完成的


def _reuse_done_job(match, max_age_h=None):
    """在服务历史里捞「孤儿作业」：status=done、但结果没人取的 job，按 `match(item)` 过滤后复用。

    纯尽力而为：服务不可用 / 历史里没有 / 结果为空 → 一律返回 None，调用方走正常转写。
    """
    try:
        items = ASR_SESS.get(f"{ASR_API}/api/history/transcribe", timeout=30).json().get("items", [])
    except Exception:
        return None
    now = time.time()
    for it in items:                       # 服务端按 created_at 倒序返回 → 先命中的就是最新那次
        try:
            if not match(it) or it.get("status") != "done" or not it.get("finished_at"):
                continue
            if now - float(it["finished_at"]) > (max_age_h or REUSE_JOB_H) * 3600:
                continue
            result = (ASR_SESS.get(f"{ASR_API}/api/result/{it['job_id']}", timeout=60).json()
                      .get("result") or {})
        except Exception:
            continue
        text = (result.get("text") or "").strip()
        if not text:
            continue
        return {"text": text, "segments": result.get("segments", []),
                "duration": round(float(it.get("duration") or 0), 1), "job_id": it["job_id"],
                "reused": True}
    return None


def find_done_job(vid, max_age_h=None):
    """捞「整条作品」的孤儿作业（上传名就是 `{vid}.mp4`）。

    背景（2026-09-28 实测）：checkpoint 的 detached Popen 起了第二个 stage1，提交完 job
    就被会话回收杀掉，服务端的 job 却照跑完 → 结果无人认领，下轮又要重转一遍（白跑 ~11 分钟）。
    锁竞态已修，这里再兜一层：转写前先在服务历史里找同 vid、status=done、较新的 job 直接复用。
    """
    return _reuse_done_job(lambda it: it.get("filename") == vid + ".mp4", max_age_h)


def _slice_tag(video):
    """源文件 → 切片目录名/上传名里的 tag（非单词字符换 `_`，截 64 字符）。"""
    return re.sub(r"[^\w\-]", "_", os.path.splitext(os.path.basename(video))[0])[:64]


def _slice_upload_name(vid, tag, k):
    """切片段在服务端的**上传文件名**（唯一标识一个段作业）。

    ⚠️ 转写时（`slice_one`）与捞孤儿段时（`find_done_job_by_name`）必须走**同一个函数**：
    历史 bug 就是这两处各写各的格式字符串，导致切片场景永远对不上名字、孤儿段捞不回来。
    """
    return f"{vid}_{tag}_{k:03d}.mp4"


def find_done_job_by_name(upload_name, max_age_h=None):
    """捞「切片段」的孤儿作业：客户端被杀/退出，但服务端该段已跑完、seg_XXX.json 没落盘。

    不兜这一层的话，每个孤儿段都要重转一遍（一段 15 分钟音频 ≈ 白跑 12~13 分钟）。
    """
    return _reuse_done_job(lambda it: it.get("filename") == upload_name, max_age_h)


# ── 切片转写（超长音频：单个 job 过长会把 ASR 服务整挂）──
# 背景：巫师财经 4.45h 合集（16015s）连续 4 次让 8766 硬崩（WinError 10061，服务端零 traceback），
# 每次白跑 45min~3h。方案：本地 ffmpeg 按 SEG_SEC 切段 → 逐段送 ASR → 合并成一条完整逐字稿。
# 设计要点：
#   ① 每段结果独立落盘（seg_XXX.json）→ 中途崩溃也能断点续跑，不再"整条白跑"
#   ② 单段失败（基础设施类）自动 ensure_server + 重试 SLICE_RETRY 次
#   ③ 合并后写标准 raw 并 mark_ok，下游 stage2 渲染零改动
SLICE_DIR   = os.path.join(OUT_ROOT, "_slice_tmp")
SLICE_SEC   = int(os.environ.get("WB_SLICE_SEC", "900"))      # 每段**上限**秒数（默认 15 分钟）
SLICE_RETRY = int(os.environ.get("WB_SLICE_RETRY", "3"))      # 单段最多重试次数（音频/引擎本身失败）
# 基础设施类失败（8766 崩溃重启导致拒连等）**不占**上面那 3 次名额，单独给一份预算。
# ⚠️ 为什么必须分开（实测 2026-09-28 23:05）：8766 因 `GGML_ASSERT` 原生 abort 崩一次，
# 客户端当场就白烧 2 次名额（第 1 次撞上崩溃、第 2 次撞上 nssm 的 5s 重启窗口），
# 19 段的长视频只要崩 2 次就有段被永久判死。基础设施故障是流水线自己的问题，
# 不该算在「这段音频转不动」头上。
SLICE_INFRA = int(os.environ.get("WB_SLICE_INFRA", "6"))      # 基础设施类失败最多重提次数
SLICE_MIN   = int(os.environ.get("WB_SLICE_MIN", "1200"))     # 「长视频」门槛：≥20 分钟就走切片
SLICE_AUTO  = os.environ.get("WB_SLICE_AUTO", "1") == "1"     # stage1 是否对长视频**自动**切片

# 最近一次 slice_one 失败的原因（给 stage1 归类用；slice_one 返回 bool 不改签名）
_SLICE_LAST_ERR = None


def _seg_for(dur):
    """给一条作品挑每段秒数：不超过 SLICE_SEC，且**尽量均分**。

    固定 900s 切 20 分钟的视频会得到「15 分钟 + 5 分钟」，尾段过短不划算；
    按段数均分后 20 分钟 → 2 段各 10 分钟，4.6h → 19 段各 ~14.6 分钟。

    ⚠️ 每段秒数必须**向上取整**：向下取整会留下 `dur - n*seg` 的碎屑
    （实测 16685s 会多出一个 3s 段、1199s 会多出一个 1s 段 → 白跑一次 ASR）。

    ⚠️ 也别用 `-(-int(dur) // SEC)` —— `int()` 会**截断小数**：
    900.1s 会算出 1 段（只覆盖 900s，丢掉 0.1s 尾音），1200.9s 会算出 2×600=1200 < 1200.9。
    一律用 `math.ceil` 按**浮点**向上取整。
    """
    dur = float(dur or 0)
    if dur <= SLICE_SEC:
        return SLICE_SEC
    n = int(math.ceil(dur / SLICE_SEC))    # 段数：向上取整
    seg = int(math.ceil(dur / n))          # 每段秒数：再向上取整 → 正好 n 段，不留尾巴
    return int(max(60, min(SLICE_SEC, seg)))


def slice_tasks(vid, pool=None):
    """取该作品的全部源文件并按分片序号排序。

    同一作品可能有多个源文件（下载器把长视频/图文切成 `_1/_2/_3`），
    它们 vid 相同，必须全部纳入并**按序拼接**，否则逐字稿只覆盖第一片。

    `pool` 可传入已取好的任务列表 —— stage1 主循环里逐条调用会退化成 O(n²)
    地重扫整个源目录，传 `tasks` 就能避免。
    """
    ts = [t for t in (pool if pool is not None else build_task_list_src()) if t["vid"] == vid]
    if not ts:
        raise RuntimeError(f"源里找不到 {vid}")

    def key(t):
        stem = os.path.splitext(os.path.basename(t["video"]))[0]
        m = re.search(r"_(\d+)$", stem)
        return (int(m.group(1)) if m else 0, stem)

    ts.sort(key=key)
    return ts


def _cut_segments(src, tag, seg_sec, outdir, force=False):
    """把一个源文件切成 seg_sec 的 wav 段，返回 [(路径, 起始秒, 时长秒)]。

    用 segment 复用器一次解码出全部段（比逐段 -ss 快得多，18 段只需过一遍音频）。
    幂等：已切好且校验通过的段不会重切。
    """
    tagdir = os.path.join(outdir, tag)
    os.makedirs(tagdir, exist_ok=True)
    existing = sorted(glob.glob(os.path.join(tagdir, "seg_*.wav")))
    if force:
        for f in existing:
            try:
                os.remove(f)
            except Exception:
                pass
        existing = []

    if not existing:
        pattern = os.path.join(tagdir, "seg_%03d.wav")
        cmd = [FFMPEG, "-y", "-v", "error", "-i", src,
               "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
               "-f", "segment", "-segment_time", str(seg_sec),
               "-reset_timestamps", "1", pattern]
        p = subprocess.run(cmd, capture_output=True, timeout=3600)
        if p.returncode != 0:
            raise RuntimeError(f"切片失败(ffmpeg rc={p.returncode}): "
                               f"{p.stderr.decode('utf-8', 'replace')[:300]}")
        existing = sorted(glob.glob(os.path.join(tagdir, "seg_*.wav")))
    if not existing:
        raise RuntimeError(f"切片后没有产生任何段: {src}")

    # 逐段探测实际时长，起始位移用累计值（比 i*seg_sec 更准）
    parts, off = [], 0.0
    for idx, f in enumerate(existing):
        d = probe_duration(f)
        # ⚠️ 顺序踩过坑（2026-10-08）：原来先 `if not d: raise`，再 `if d <= 0.5: 丢碎段`。
        # 但**恰好为 0.00s 的残段**（ffmpeg 边界舍入，实测 156 字节 / 16 帧）
        # 会被 probe_duration 判成假值 → 直接抛「段文件探测不到时长」，
        # **永远走不到丢弃分支**，整条作品因此 abandoned。
        # 实证：13 个残段里有 2 个是 0.00s，导致 2 条作品（均为「钦文和他的朋友��」长视频）
        # 在 attempts=2 后放弃。修法：把「碎段」判断提到前面，用 `d == 0` 一并覆盖。
        if not d or d <= 0.5:
            # 只有**末尾**的碎屑段可以丢（ffmpeg 段边界舍入残留，送了也是白跑一次 ASR）
            if idx == len(existing) - 1:
                print(f"[slice]   丢弃末尾碎段 {os.path.basename(f)}"
                      f"（{d:.2f}s）", flush=True)
                try:
                    os.remove(f)
                except Exception:
                    pass
                continue
            if not d:
                raise RuntimeError(f"段文件探测不到时长: {f}")
            raise RuntimeError(f"段文件异常(时长{d}): {f}")
        parts.append((f, off, d))
        off += d
    if not parts:
        raise RuntimeError(f"切片后没有可用的段: {src}")
    return parts, off


def slice_one(vid, seg_sec=None, force=False):
    """对一条超长作品做切片转写并合并落盘。返回 True=合并成功。

    失败原因会写进模块级 `_SLICE_LAST_ERR`（保持返回 bool 不改签名，方便 CLI 与 stage1 复用）。
    """
    global _SLICE_LAST_ERR
    _SLICE_LAST_ERR = None
    tasks = slice_tasks(vid)
    # 没显式给段长 → 按该作品总时长自适应（与 stage1 自动切片同一套规则）
    if not seg_sec:
        _tot = sum((t.get("dur_csv") or 0) for t in tasks)
        seg_sec = _seg_for(_tot) if _tot else SLICE_SEC
        print(f"[slice] 未指定段长 → 按总时长 {_tot/60:.1f} 分钟自适应为 {seg_sec}s/段", flush=True)
    seg_sec = int(seg_sec)
    # 防御：vid 本身是纯数字，CLI 上极易被误当成 seg_sec 传进来（曾踩过），
    # 一旦越界就回落默认值，避免"一段切完全片"或切成碎片
    if not (60 <= seg_sec <= 3600):
        print(f"[slice] 每段秒数 {seg_sec} 不合理（应在 60~3600），回落为默认 {SLICE_SEC}s", flush=True)
        seg_sec = SLICE_SEC
    t0 = time.time()
    work = os.path.join(SLICE_DIR, vid)
    os.makedirs(work, exist_ok=True)

    print(f"[slice] 作品 {vid} 共 {len(tasks)} 个源文件，按 {seg_sec}s({seg_sec/60:.0f}分钟) 切段",
          flush=True)

    # ① 切段（含同一作品多分片的顺序拼接）
    all_parts, src_off = [], 0.0
    for t in tasks:
        tag = _slice_tag(t["video"])
        parts, total = _cut_segments(t["video"], tag, seg_sec, work, force=force)
        for f, off, d in parts:
            all_parts.append((f, src_off + off, d, tag))
        src_off += total
        print(f"[slice]   源 {tag}: {total/60:.1f} 分钟 → {len(parts)} 段", flush=True)
    print(f"[slice] 合计 {len(all_parts)} 段 / {src_off/60:.1f} 分钟", flush=True)

    # ② 逐段转写（幂等：已有的段结果直接复用，中断后可续跑）
    texts, segs_out = [], []
    for k, (path, gstart, dur, tag) in enumerate(all_parts, 1):
        rj = os.path.join(work, os.path.splitext(os.path.basename(path))[0] + ".json")
        upname = _slice_upload_name(vid, tag, k)
        res = None
        if not force and os.path.exists(rj):
            try:
                res = json.load(open(rj, encoding="utf-8"))
                if not (res.get("text") or "").strip():
                    res = None
            except Exception:
                res = None
        if res is not None:
            print(f"[slice] [{k}/{len(all_parts)}] SKIP {os.path.basename(path)}（已转写，复用）",
                  flush=True)
        elif not force and (rec := find_done_job_by_name(upname)) is not None:
            # 孤儿段：上一次被巡检/会话杀掉时，服务端这个段的 job 已跑完但结果没人取 →
            # 直接认领落盘，省下整段重转（一段 12~13 分钟）
            res = rec
            try:
                json.dump(res, open(rj, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            except Exception:
                pass
            print(f"[slice] [{k}/{len(all_parts)}] RECOVER {os.path.basename(path)}"
                  f"（服务端作业 {rec['job_id']} 已完成 → 认领复用）", flush=True)
        else:
            err = None
            att = 0        # 真实尝试次数（音频/引擎本身的问题）
            infra = 0      # 基础设施失败次数（8766 掉线/重启），不占 SLICE_RETRY 名额
            while True:
                att += 1
                try:
                    if not ensure_server():
                        raise RuntimeError("ASR 服务不可用")
                    ta = time.time()
                    res = asr_transcribe_audio(path, vid, dur, upload_name=upname)
                    json.dump(res, open(rj, "w", encoding="utf-8"),
                              ensure_ascii=False, indent=1)
                    print(f"[slice] [{k}/{len(all_parts)}] OK {os.path.basename(path)} "
                          f"音频{dur:.0f}s 文本{len(res['text'])}字 耗时{time.time()-ta:.0f}s",
                          flush=True)
                    break
                except Exception as e:
                    err = e
                    res = None
                    if is_infra_failure(e):
                        # 8766 崩了/正在被 nssm 重启 → 等它回来再提，且不计入真实尝试次数。
                        # 退避 10/20/30…最长 60s，确保不会在「服务还没起来」的窗口里空提。
                        att -= 1
                        infra += 1
                        if infra > SLICE_INFRA:
                            print(f"[slice] [{k}/{len(all_parts)}] 基础设施失败已达上限 "
                                  f"{SLICE_INFRA} 次，放弃该段（{str(e)[:120]}）", flush=True)
                            break
                        wait = min(60, 10 * infra)
                        print(f"[slice] [{k}/{len(all_parts)}] 基础设施故障 "
                              f"{infra}/{SLICE_INFRA}（不计入 {SLICE_RETRY} 次尝试；"
                              f"{wait}s 后等 8766 恢复再提交）: {str(e)[:150]}", flush=True)
                        time.sleep(wait)
                    else:
                        print(f"[slice] [{k}/{len(all_parts)}] 第{att}/{SLICE_RETRY}次失败 "
                              f"{os.path.basename(path)}: {e}", flush=True)
                        if att >= SLICE_RETRY:
                            break
                        time.sleep(15)
            if res is None:
                print(f"[slice] 中断：{os.path.basename(path)} 失败 {att}/{SLICE_RETRY} 次"
                      f"（其中基础设施故障 {infra} 次），最后错误（{err}）。"
                      f"已完成的段已落盘，修好服务后重跑同一条命令会自动续跑。", flush=True)
                _SLICE_LAST_ERR = f"切片第{k}/{len(all_parts)}段失败{att}/{SLICE_RETRY}次: {err}"
                return False

        texts.append((res.get("text") or "").strip())
        for s in (res.get("segments") or []):
            try:
                segs_out.append({"start": round(float(s.get("start", 0)) + gstart, 2),
                                 "end": round(float(s.get("end", 0)) + gstart, 2),
                                 "text": s.get("text", "")})
            except Exception:
                pass

    # ③ 合并成一条完整逐字稿，写标准 raw（下游 stage2 照常渲染）
    text = "\n".join([x for x in texts if x])
    total_dur = sum(d for _, _, d, _ in all_parts)
    payload = dict(tasks[0], **{
        "text": text,
        "segments": segs_out,
        "duration": round(total_dur, 1),
        "job_id": f"slice{len(all_parts)}x{seg_sec}s",
        "sliced": {"seg_sec": seg_sec, "parts": len(all_parts),
                   "sources": [os.path.basename(t["video"]) for t in tasks]},
    })
    ok, why = validate_raw(payload)
    raw_path = os.path.join(RAW_DIR, vid + ".json")
    json.dump(payload, open(raw_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    if not ok:
        print(f"[slice] 合并结果校验未过：{why}", flush=True)
        _SLICE_LAST_ERR = f"切片合并结果校验未过: {why}"
        return False

    mark_ok(vid, payload)
    update_entry(vid, remove=("retry_rounds",))     # 已成功，清掉放行计数
    print(f"[slice] 合并完成 {vid}: {len(all_parts)} 段 / {total_dur/60:.1f} 分钟 / "
          f"{len(text)} 字 / 耗时{time.time()-t0:.0f}s → 已登记，stage2 会自动渲染 md", flush=True)
    return True


def slice_all(min_sec=None):
    """把「未完成且时长 ≥ min_sec」的作品全部切片转写（门槛默认 20 分钟 = SLICE_MIN）

    注：stage1 现在已对长视频**自动**切片，这个命令主要留给「手动补跑/指定更高门槛」用。
    """
    min_sec = int(min_sec or SLICE_MIN)
    seen, todo = set(), []
    for t in build_task_list_src():
        v = t["vid"]
        if v in seen:
            continue
        seen.add(v)
        if (entry(v) or {}).get("ok"):
            continue
        d = t.get("dur_csv") or probe_duration(t["video"]) or 0
        if d >= min_sec:
            todo.append((v, d))
    if not todo:
        print(f"[slice] 没有需要切片的作品（门槛 {min_sec}s = {min_sec/60:.0f} 分钟）", flush=True)
        return
    print(f"[slice] 待切片 {len(todo)} 条: "
          f"{[(v, f'{d/60:.1f}min') for v, d in todo]}", flush=True)
    for v, d in todo:
        if not slice_one(v, _seg_for(d)):
            print(f"[slice] 该条未完成，停止本批（可稍后重跑续跑）: {v}", flush=True)
            return


def stage1(limit=None):
    """带单实例锁的入口：已有 stage1 在跑就立刻退出，避免重复转写。"""
    if not acquire_lock():
        print("[lock] 已有 stage1 在运行，本次退出（避免重复转写）", flush=True)
        return
    try:
        _stage1_run(limit)
    finally:
        release_lock()


def _stage1_run(limit=None):
    tasks = build_task_list_src()
    print(f"[源] {SOURCE} / 共 {len(tasks)} 条", flush=True)
    authors_seen = {}
    done = skipped = failed = repaired = 0
    total = len(tasks)
    for i, t in enumerate(tasks, 1):
        vid = t["vid"]
        authors_seen[t["author"]] = authors_seen.get(t["author"], 0) + 1
        raw_path = os.path.join(RAW_DIR, vid + ".json")

        # 已完整转写成功 → 直接跳过
        if is_transcribed(vid):
            skipped += 1
            print(f"[{i}/{total}] SKIP {vid} (已转写成功)", flush=True)
            continue

        # 已重试到上限仍不完整 → 放弃，不再反复重试
        if is_abandoned(vid):
            skipped += 1
            e = entry(vid)
            print(f"[{i}/{total}] SKIP {vid} "
                  f"(已重试{e.get('attempts')}次仍不完整，放弃: {e.get('why')})", flush=True)
            continue

        # 有残留文件 → 先校验，通过则补登记并跳过，不完整则重新进入转写流程
        if os.path.exists(raw_path):
            try:
                old = json.load(open(raw_path, encoding="utf-8"))
                good, why = validate_raw(old)
            except Exception as e:
                good, why = False, f"解析失败: {e}"
            if good:
                mark_ok(vid, old)
                skipped += 1
                print(f"[{i}/{total}] SKIP {vid} (已转写成功·补登记)", flush=True)
                continue
            att = int(entry(vid).get("attempts") or 0)
            repaired += 1
            print(f"[{i}/{total}] 重转 {vid} "
                  f"(第{att+1}次，旧结果不完整: {why})", flush=True)

        if limit and done >= limit:
            print(f"达到 limit={limit}，停止", flush=True)
            break
        t0 = time.time()
        try:
            if not ensure_server():
                failed += 1
                print(f"[{i}/{total}] SKIP {vid} 服务不可用，放弃本条", flush=True)
                continue

            # ── 长视频（≥ SLICE_MIN，默认 20 分钟）→ 自动切段转写再拼接 ──
            # 为什么不再整条丢给引擎：整条跑时 8766 对超长音频会硬崩（WinError 10061、
            # 服务端零 traceback），一崩就"整条白跑"（实测每条白跑 45min~3h）。
            # 切片后每段独立落盘 → 段级断点续跑，崩了只需重跑未完成的那几段。
            #
            # ⚠️ 同作品可能被下载器切成 `_1/_2/_3` 多个源（vid 相同）：只取 t["video"] 会
            # **只转第一片、丢掉后面的内容**；时长也必须按**全部源之和**算，否则
            # 「第一片没超 20 分钟、整体超了」的作品会被误判成不必切片 → 同样丢内容。
            # 所以多源一律走切片路径（只有它实现了按序号拼接）。
            srcs = slice_tasks(vid, tasks)
            multi = len(srcs) > 1
            dur_est = sum((x.get("dur_csv") or probe_duration(x["video"]) or 0) for x in srcs)
            if SLICE_AUTO and (dur_est >= SLICE_MIN or multi):
                seg = _seg_for(dur_est)
                nseg = int(math.ceil(dur_est / seg)) if seg else 0
                why_slice = ("多源%d个" % len(srcs)) if multi else (">=%ds" % SLICE_MIN)
                print(f"[{i}/{total}] SLICE {vid} {t['author']} 音频{dur_est:.0f}s "
                      f"{why_slice} → 切 {nseg} 段 x {seg}s", flush=True)
                if slice_one(vid, seg):
                    done += 1
                    e = entry(vid)
                    # ⚠️ 行首必须是 `[i/N] OK <vid>`：看板按 `^\[i/N\] (SKIP|OK|FAIL)\s+(\d+)` 解析
                    # 已完成条数，写成 `OK(切片)` 会让计数卡住不推进
                    print(f"[{i}/{total}] OK {vid} {t['author']} "
                          f"文本{e.get('chars', 0)}字 耗时{time.time()-t0:.0f}s (切片{nseg}段)",
                          flush=True)
                else:
                    failed += 1
                    why = _SLICE_LAST_ERR or "切片转写失败"
                    # 基础设施类失败（服务崩/超时）不消耗"内容失败"预算 —— 否则 4.6h 的片子
                    # 崩两轮就被标 abandoned，其实源文件完全正常。留给下次自动重跑。
                    if is_infra_failure(why):
                        print(f"[{i}/{total}] FAIL(切片·基础设施) {vid} {t['author']}: {why}"
                              f" —— 注册表不变，下次运行会自动重试（已完成段会复用）", flush=True)
                    else:
                        mark_fail(vid, why)
                        print(f"[{i}/{total}] FAIL(切片) {vid} {t['author']}: {why}", flush=True)
                continue

            r = find_done_job(vid)      # 孤儿作业（客户端被判死、服务端已跑完）→ 复用，省一次转写
            if r:
                print(f"[{i}/{total}] 复用 ASR 历史作业 {r['job_id']} {vid} {t['author']} "
                      f"音频{r['duration']:.0f}s / 文本{len(r['text'])}字（此前客户端中断，服务端已完成）",
                      flush=True)
            else:
                r = asr_transcribe(t["video"], vid)
            payload = dict(t, **r)
            ok, why = validate_raw(payload)
            # 即使不完整也落盘，保留本次结果，避免下次从零开始
            json.dump(payload, open(raw_path, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=1)
            if not ok:
                failed += 1
                mark_fail(vid, why)
                print(f"[{i}/{total}] FAIL {vid} {t['author']}: 转写结果不完整({why})",
                      flush=True)
                continue
            mark_ok(vid, payload)
            el = time.time() - t0
            done += 1
            print(f"[{i}/{total}] OK {vid} {t['author']} "
                  f"音频{r['duration']:.0f}s 文本{len(r['text'])}字 耗时{el:.0f}s "
                  f"(RTF={el/max(r['duration'],1):.2f})", flush=True)
        except Exception as e:
            failed += 1
            mark_fail(vid, f"异常: {e}")
            print(f"[{i}/{total}] FAIL {vid} {t['author']}: {e}", flush=True)
    print(f"\n阶段1 完成: 新增{done} 跳过{skipped} 重转{repaired} 失败{failed} / 共{total}",
          flush=True)


# ── 关键词 ──
KW_PROMPT = (
    "你是关键词提取助手。根据下面这条短视频的标题和逐字稿，提炼 4 个最核心的中文关键词，"
    "用于内容检索和归类。要求：\n"
    "1. 每个关键词 2-6 个字，是具体概念或主题，不要虚词、不要整句；\n"
    "2. 4 个关键词之间语义不要重复；\n"
    "3. 只输出关键词本身，用中文顿号「、」分隔，不要编号、不要解释、不要引号。\n"
)


# ── LLM 通道（可切换：ollama 本地 / cloud 云端）──
LLM_BACKEND  = os.environ.get("WB_LLM_BACKEND", "ollama")       # ollama | cloud
OLLAMA_MODEL = os.environ.get("WB_OLLAMA_MODEL", "Qwen3.5-4B:latest")
LLM_NUM_GPU  = int(os.environ.get("WB_LLM_NUM_GPU", "0"))       # 0=CPU（ASR 占用显存时）
CLOUD_BASE   = os.environ.get("WB_CLOUD_BASE", "https://api.siliconflow.com/v1")
CLOUD_KEY    = os.environ.get("WB_CLOUD_KEY", "")
CLOUD_MODEL  = os.environ.get("WB_CLOUD_MODEL", "Qwen/Qwen3.5-122B-A10B")
CLOUD_NOTHK  = os.environ.get("WB_CLOUD_DISABLE_THINK", "1") == "1"
CLOUD_MAXFAIL = int(os.environ.get("WB_CLOUD_MAX_FAILS", "3"))   # 连续失败几次后锁定降级

_cloud_fail = 0
_cloud_locked = False


def _ollama_call(prompt, num_predict, temperature, num_ctx, timeout=7200):
    """本地 Ollama 调用（也是云端失效时的降级通道）"""
    r = requests.post(f"{OLLAMA}/api/chat", json={
        "model": OLLAMA_MODEL, "think": False, "stream": False,
        "messages": [{"role": "user", "content": prompt}],
        "options": {"num_gpu": LLM_NUM_GPU, "temperature": temperature,
                    "num_ctx": num_ctx, "num_predict": num_predict},
    }, timeout=timeout)
    r.raise_for_status()
    return ((r.json().get("message") or {}).get("content", "") or "").strip()


def call_llm(prompt, num_predict=900, temperature=0.3, num_ctx=8192, timeout=3600):
    """统一 LLM 入口：cloud 优先，失败自动降级到本地 Ollama。

    连续失败 CLOUD_MAXFAIL 次后锁定降级，避免每条都白等云端超时。
    """
    global _cloud_fail, _cloud_locked

    if LLM_BACKEND == "cloud" and not _cloud_locked:
        try:
            body = {"model": CLOUD_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": temperature, "max_tokens": num_predict,
                    "stream": False}
            if CLOUD_NOTHK:
                body["enable_thinking"] = False
            url = f"{CLOUD_BASE}/chat/completions"
            hdr = {"Authorization": f"Bearer {CLOUD_KEY}",
                   "Content-Type": "application/json"}
            r = requests.post(url, headers=hdr, json=body, timeout=timeout)
            if r.status_code == 400 and "enable_thinking" in body:
                body.pop("enable_thinking", None)   # 该模型不支持该参数 → 去掉重试
                r = requests.post(url, headers=hdr, json=body, timeout=timeout)
            r.raise_for_status()
            msg = (r.json().get("choices") or [{}])[0].get("message", {}) or {}
            out = (msg.get("content") or "").strip()
            if not out and msg.get("reasoning_content"):
                out = (msg.get("reasoning_content") or "").strip()   # thinking 未关的兜底
            if not out:
                raise RuntimeError("云端返回空内容")
            _cloud_fail = 0
            return out
        except Exception as e:
            _cloud_fail += 1
            print(f"[LLM] 云端调用失败 #{_cloud_fail}（{type(e).__name__}: {str(e)[:120]}）", flush=True)
            if _cloud_fail >= CLOUD_MAXFAIL:
                _cloud_locked = True
                print(f"[LLM] 云端连续失败 {_cloud_fail} 次 → 锁定降级为本地 Ollama({OLLAMA_MODEL})，"
                      f"本次批量剩余任务全部走本地", flush=True)
            else:
                print(f"[LLM] 本条降级为本地 Ollama 生成", flush=True)
            # 降级：CPU 推理较慢，压缩上下文以控时
            return _ollama_call(prompt, num_predict, temperature,
                                min(num_ctx, 8192), timeout=7200)

    return _ollama_call(prompt, num_predict, temperature, num_ctx, timeout)


def extract_keywords(title, text, author=""):
    """关键词提取。**优先复用词库**（`keyword_lib`），库内无合适词才新造。

    为什么加这层：原来每篇都让 LLM 自由造 4 个词，造完就扔 →
    实测 4176 个词里 90.5% 只用过一次，同一主播自己都在造近义词
    （程前朋友圈同时有 创业逆袭/创业经历/创业历程/创业故事），检索根本归拢不了。

    词库缺失或加载失败时**自动退化为原行为**，不影响跑批。
    可用环境变量 `WB_KW_REUSE=0` 关闭（对照用）。
    """
    if os.environ.get("WB_KW_REUSE", "1") != "0":
        try:
            import keyword_lib
            lib = keyword_lib.load_lib()
            if lib.get("counts"):
                kws = keyword_lib.extract_keywords(title, text, author=author, lib=lib)
                if kws:
                    # 新造词入库，让词库自我收敛（下次可被复用）
                    try:
                        if keyword_lib.record_new(lib, author, kws.split("、")):
                            keyword_lib.save_lib(lib)
                    except Exception as e:
                        print(f"[kw] 词库写入失败（忽略）: {type(e).__name__}", flush=True)
                    return kws
        except Exception as e:
            print(f"[kw] 词库复用不可用，退回自由造词: {type(e).__name__}: {str(e)[:100]}", flush=True)
    return _extract_keywords_free(title, text)


def _extract_keywords_free(title, text):
    """原实现：不查词库，自由造词（降级路径 / 对照基线）。"""
    body = text[:800] if text else ""
    user = f"标题：{title}\n逐字稿：{body}" if body else f"标题：{title}\n（无逐字稿）"
    content = call_llm(KW_PROMPT + "\n" + user, num_predict=64,
                       temperature=0.2, num_ctx=4096, timeout=900)
    content = re.sub(r"^(关键词[:：]?\s*)", "", content)
    content = content.replace("，", "、").replace(",", "、").replace("\n", "、")
    kws = [k.strip(" 。.、\"'“”‘’") for k in content.split("、")]
    kws = [k for k in kws if k][:4]
    return "、".join(kws) if kws else ""


SUMMARY_PROMPT = """认真通读整篇视频逐字稿，自动过滤口头禅、停顿、重复、水词、无效口播废话，完全基于原文内容，用自然、平实、人工复盘的口吻做总结，不要模板感、不要机械分句式、不要生硬 AI 书面腔。
输出内容分为三部分，语言通俗流畅、贴合真人工作总结风格：
1. 视频核心主旨：一段话，讲清楚这条视频到底想讲什么、传递什么核心观点、解决观众什么问题。控制在 80～150 字。
2. 核心干货要点：梳理视频里真正有用的知识点、方法、结论、关键信息，用生活化简洁语句罗列，不堆砌、不生硬、不重复。整体控制在 120～250 字。
3. 内容整体逻辑：顺着视频讲述顺序，自然说明视频的叙事结构，比如开篇引入、中间讲解、结尾总结升华的完整逻辑，不用机械术语。控制在 80～150 字。
全程不脑补、不扩写、不套 AI 话术，只精简提纯原文内容，整体呈现人工整理笔记的质感。允许小幅浮动，优先保证信息完整，不要为凑字数删减关键内容。

重要约束：只能使用逐字稿里真实出现的信息。逐字稿没提到的细节一律不写，严禁根据标题推测或补齐情节；逐字稿与标题不一致时，一律以逐字稿为准。

严格按下面格式输出，三个标题原样保留，标题下直接写内容，不要额外说明、不要用代码块：

### 视频核心主旨
（内容）

### 核心干货要点
（内容）

### 内容整体逻辑
（内容）"""


def _parse_summary(out):
    """把 LLM 输出的三段式解析成 dict(main, points, logic)"""
    def seg(name, nxt):
        if nxt:
            pat = rf"###+\s*{name}[^\n]*\n(.*?)(?=###+\s*{nxt}|\Z)"
        else:
            pat = rf"###+\s*{name}[^\n]*\n(.*)\Z"
        m = re.search(pat, out, re.S)
        return re.sub(r"^\s*[（(].*?[)）]\s*$", "", m.group(1).strip()).strip() if m else ""

    main = seg("视频核心主旨", "核心干货要点")
    points = seg("核心干货要点", "内容整体逻辑")
    logic = seg("内容整体逻辑", None)
    if not (main or points or logic):
        return {"main": out.strip(), "points": "", "logic": ""}
    return {"main": main, "points": points, "logic": logic}


# ── 长逐字稿摘要（map-reduce）──
# 长视频逐字稿可达数万字，一次性塞给 LLM 会超上下文（本地 num_ctx=12288，云端也有上限）
# 且容易只总结到后半段。方案：分块提炼要点 → 再汇总成三段式。
LONG_TEXT_CHARS = int(os.environ.get("WB_LONG_TEXT_CHARS", "9000"))   # 超过则走 map-reduce
SUM_CHUNK_CHARS = int(os.environ.get("WB_SUM_CHUNK_CHARS", "6000"))   # 每块字符数
LLM_CACHE_DIR   = os.path.join(OUT_ROOT, "_llm_cache")

CHUNK_PROMPT = """这是同一条长视频逐字稿的第 {i}/{n} 段。请认真通读这一段，用要点式提炼其中真正有用的信息：讲了什么事、给出哪些事实/数据/结论/方法。

约束：只能使用本段原文里真实出现的信息，严禁脑补或根据常识补充；不要写开场白和收尾话，直接列要点。

视频标题：{title}

逐字稿第 {i} 段：
{chunk}"""


def _cached_llm(cache_key, prompt, **kw):
    """带磁盘缓存的 LLM 调用：长视频分块结果落盘，重跑不必重复烧 token"""
    os.makedirs(LLM_CACHE_DIR, exist_ok=True)
    p = os.path.join(LLM_CACHE_DIR, cache_key + ".txt")
    if os.path.exists(p):
        try:
            old = open(p, encoding="utf-8").read().strip()
            if old:
                return old
        except Exception:
            pass
    out = call_llm(prompt, **kw)
    if out:
        try:
            open(p, "w", encoding="utf-8").write(out)
        except Exception:
            pass
    return out


def _chunk_text(t, size):
    """按段落边界把长文切成 ≤size 的块（单段超长则硬切）"""
    parts, cur = [], ""
    for line in (t or "").split("\n"):
        while len(line) > size:
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(line[:size])
            line = line[size:]
        if cur and len(cur) + len(line) + 1 > size:
            parts.append(cur)
            cur = ""
        cur += ("\n" if cur else "") + line
    if cur.strip():
        parts.append(cur)
    return [p for p in parts if p.strip()]


def _summary_long(title, t):
    """map-reduce：分块提炼 → 汇总成三段式"""
    chunks = _chunk_text(t, SUM_CHUNK_CHARS)
    print(f"[LLM] 长逐字稿 {len(t)} 字 → 分 {len(chunks)} 块提炼后再汇总", flush=True)
    notes = []
    for i, c in enumerate(chunks, 1):
        import hashlib
        key = "chunk_" + hashlib.md5(c.encode("utf-8")).hexdigest()[:16]
        try:
            out = _cached_llm(key,
                              CHUNK_PROMPT.format(i=i, n=len(chunks), title=title, chunk=c),
                              num_predict=700, temperature=0.3, num_ctx=8192, timeout=1800)
        except Exception as e:
            print(f"[LLM] 第 {i}/{len(chunks)} 块提炼失败: {e}", flush=True)
            out = ""
        if out:
            notes.append(f"【第 {i}/{len(chunks)} 段要点】\n{out}")
            print(f"[LLM] 分块 {i}/{len(chunks)} 完成（{len(out)} 字）", flush=True)
    if not notes:
        return None
    joined = "\n\n".join(notes)
    print(f"[LLM] 汇总 {len(notes)} 块要点（{len(joined)} 字）→ 生成三段式总结", flush=True)
    out = call_llm(
        f"{SUMMARY_PROMPT}\n\n补充说明：本条视频很长（逐字稿约 {len(t)} 字），"
        f"下面是按时间顺序整理的各段落要点。请据此输出三段式总结，"
        f"「视频核心主旨」要能概括整条视频的完整脉络，不要只写后半段。\n\n"
        f"视频标题：{title}\n\n各段落要点：\n{joined}",
        num_predict=1200, temperature=0.35, num_ctx=12288, timeout=3600)
    return _parse_summary(out) if out else None


def extract_summary(title, text):
    """返回 dict(main, points, logic)；失败返回 None"""
    t = (text or "").strip()
    if len(t) < 60:
        # 语音过少：如实标注，禁止拿标题脑补
        return {"main": f"本条视频语音内容极少（逐字稿仅 {len(t)} 字），不足以做内容总结，为避免臆测不生成摘要。",
                "points": "", "logic": ""}
    if len(t) > LONG_TEXT_CHARS:
        return _summary_long(title, t)
    out = call_llm(f"{SUMMARY_PROMPT}\n\n视频标题：{title}\n\n逐字稿：\n{t}",
                   num_predict=1000, temperature=0.35, num_ctx=12288, timeout=3600)
    if not out:
        return None
    return _parse_summary(out)


MD_TEMPLATE = """作者：{author}
抖音账号：{account}
作品ID：https://www.douyin.com/video/{vid}
视频标题：{title}
发布时间：{pub}
关键词：{keywords}
视频封面：![封面]({cover})

-----

## 视频内容总结

### 视频核心主旨

{summary_main}

### 核心干货要点

{summary_points}

### 内容整体逻辑

{summary_logic}

-----

逐字稿

{text}
"""

MD_TEMPLATE_NOSUM = """作者：{author}
抖音账号：{account}
作品ID：https://www.douyin.com/video/{vid}
视频标题：{title}
发布时间：{pub}
关键词：{keywords}
视频封面：![封面]({cover})

-----

逐字稿

{text}
"""


def render_md(data, kws, summary):
    """生成 md 文本，返回 (内容, 封面文件名)
    封面与 md 同名，仅扩展名不同：<发布时间>_<作者>_<作品id>.jpg
    """
    ext = os.path.splitext(data["cover"])[1] or ".jpg"
    cover_name = f"{data['pub']}_{data['author']}_{data['vid']}{ext}"
    text = (data.get("text") or "（无语音内容）").strip()
    base = dict(author=data["author"], account=data.get("account") or "—",
                vid=data["vid"], title=data.get("title") or "—", pub=data["pub"],
                keywords=kws or "—", cover=cover_name, text=text)
    if summary:
        return MD_TEMPLATE.format(
            summary_main=summary.get("main") or "—",
            summary_points=summary.get("points") or "—",
            summary_logic=summary.get("logic") or "—", **base), cover_name
    return MD_TEMPLATE_NOSUM.format(**base), cover_name


def sync_meta():
    """把数据源里的元数据（抖音号/作者/标题/发布时间）同步进已落盘的 raw json。
    只更新元数据字段，不动 text/segments，因此无需重新转写。
    元数据规则升级后（例如补上抖音号）用它刷一遍即可。
    """
    tasks = build_task_list_src()
    n = miss = 0
    for t in tasks:
        p = os.path.join(RAW_DIR, t["vid"] + ".json")
        if not os.path.exists(p):
            miss += 1
            continue
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        before = {k: d.get(k) for k in ("account", "author", "title", "pub")}
        for k in ("account", "author", "title", "pub"):
            if t.get(k):
                d[k] = t[k]
        after = {k: d.get(k) for k in ("account", "author", "title", "pub")}
        if before != after:
            json.dump(d, open(p, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=1)
            n += 1
    print(f"元数据同步：更新 {n} 条，无 raw {miss} 条", flush=True)


def sync_md_header():
    """按 raw json 整块重建已生成 md 的头部（作者/抖音账号/作品ID/视频标题/发布时间/关键词/视频封面）。
    只重建 ----- 之前的头部，正文与总结原样保留，因此不消耗 LLM。
    标题可能含换行，故必须整块重建，不能逐行替换（否则会留下重复的续行）。
    """
    n = 0
    for author in sorted(os.listdir(AUTHORS_DIR)):
        d = os.path.join(AUTHORS_DIR, author)
        if not os.path.isdir(d) or author.startswith("_") or author.startswith("."):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".md"):
                continue
            vid = fn.rsplit("_", 1)[-1][:-3]
            p = os.path.join(RAW_DIR, vid + ".json")
            if not os.path.exists(p):
                continue
            path = os.path.join(d, fn)
            try:
                raw = json.load(open(p, encoding="utf-8"))
                s = open(path, encoding="utf-8").read()
            except Exception:
                continue
            # 保留已有正文：以行内 '-----' 分隔符为界
            parts = re.split(r"(?m)^-----$", s, maxsplit=1)
            body = ("-----" + parts[1]) if len(parts) > 1 else "\n"
            m_kw = re.search(r"(?m)^关键词：(.*)$", s)
            m_cv = re.search(r"!\[封面\]\(([^)]*)\)", s)
            cover = m_cv.group(1) if m_cv else ""
            if not cover:            # 回退：按同名文件推断
                for ext in (".jpg", ".jpeg", ".png", ".webp"):
                    if os.path.exists(os.path.join(d, fn[:-3] + ext)):
                        cover = fn[:-3] + ext
                        break
            head = (f"作者：{raw.get('author') or author}\n"
                    f"抖音账号：{raw.get('account') or '—'}\n"
                    f"作品ID：https://www.douyin.com/video/{vid}\n"
                    f"视频标题：{raw.get('title') or '—'}\n"
                    f"发布时间：{raw.get('pub') or '未知日期'}\n"
                    f"关键词：{m_kw.group(1).strip() if m_kw else '—'}\n"
                    f"视频封面：![封面]({cover})\n\n")
            new = head + body
            if new != s:
                open(path, "w", encoding="utf-8").write(new)
                n += 1
    print(f"md 头部重建：更新 {n} 篇", flush=True)


def stage2(limit=None):
    files = sorted(os.listdir(RAW_DIR))
    done = skipped = failed = 0
    total = len([f for f in files if f.endswith(".json")])
    shown = 0
    for i, fn in enumerate([f for f in files if f.endswith(".json")], 1):
        if limit and shown >= limit:
            break
        data = json.load(open(os.path.join(RAW_DIR, fn), encoding="utf-8"))
        author, vid = data["author"], data["vid"]
        outdir = os.path.join(AUTHORS_DIR, author)
        md_path = os.path.join(outdir, f"{data['pub']}_{author}_{vid}.md")
        if os.path.exists(md_path):
            skipped += 1
            print(f"[{i}/{total}] SKIP {vid}", flush=True)
            continue
        try:
            os.makedirs(outdir, exist_ok=True)
            kws = extract_keywords(data.get("title", ""), data.get("text", ""), author=author)
            summary = extract_summary(data.get("title", ""), data.get("text", ""))
            md, cover_name = render_md(data, kws, summary)
            if os.path.exists(data["cover"]):
                shutil.copy2(data["cover"], os.path.join(outdir, cover_name))
            open(md_path, "w", encoding="utf-8").write(md)
            done += 1
            shown += 1
            print(f"[{i}/{total}] OK {vid} 关键词: {kws}", flush=True)
        except Exception as e:
            failed += 1
            print(f"[{i}/{total}] FAIL {vid}: {e}", flush=True)
    print(f"\n阶段2 完成: 新增{done} 跳过{skipped} 失败{failed}", flush=True)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"

    def _int_or_none(s):
        try:
            return int(s)
        except Exception:
            return None

    # 注意：accept/slice 等模式的 argv[2] 是 vid 而非数字，必须容错解析（否则 int(vid) 直接抛错）
    lim = _int_or_none(sys.argv[2]) if len(sys.argv) > 2 else None
    if mode == "registry":
        # ⚠️ 整表回写：跑批在跑时它会与 stage1 抢注册表（已加互斥锁，但语义上仍是"用旧快照覆盖"）。
        #    加锁只保证不写坏文件、不丢并发写，**不保证**扫描期间 stage1 刚写的条目不被剔除。
        if os.path.exists(_lock_path("stage1")) or os.path.exists(_lock_path("stage2")):
            print("[warn] stage1/stage2 锁存在（跑批进行中）。registry 是**整表覆盖**，"
                  "虽然有互斥锁不会写坏，但仍可能剔除扫描窗口内新增的条目。\n"
                  "       建议：等跑批结束再重建；只想登记新作品请用 remote_handoff.py import/fetch。",
                  flush=True)
        build_registry()          # 全量扫描 _asr_raw，重建转写注册表
    if mode == "accept" and len(sys.argv) > 2:
        # 确认某条『本就几乎无语音』，登记为已转写成功，不再重试
        mark_low_content(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "")
    if mode == "syncmeta":
        sync_meta()               # 把源里的元数据同步进已落盘的 raw（不重转写）
    if mode == "syncmd":
        sync_md_header()          # 按 raw 刷新已生成 md 的头部（不重跑 LLM）
    if mode == "retry":
        # 放行因 ASR 服务掉线等基础设施原因被 abandoned 的条目（可带最大轮次，默认 3）
        retry_infra(lim if lim else 3)
    if mode == "slice" and len(sys.argv) > 2:
        # 超长音频切片转写：python asr_batch.py slice <vid> [每段秒数]
        # vid 可用逗号分隔一次跑多条。注意别用 lim：vid 是纯数字，会被误当秒数。
        _seg = _int_or_none(sys.argv[3]) if len(sys.argv) > 3 else None
        for _v in sys.argv[2].split(","):
            _v = _v.strip()
            if _v and not slice_one(_v, _seg):
                print(f"[slice] 未完成，后续条目跳过: {_v}", flush=True)
                break
    if mode == "slice-all":
        # 把所有「未完成且时长 ≥ N 秒」的作品切片转写（默认 30 分钟）
        slice_all(_int_or_none(sys.argv[2]) if len(sys.argv) > 2 else None)
    if mode in ("stage1", "all"):
        stage1(lim)
    if mode in ("stage2", "all"):
        stage2(lim)
