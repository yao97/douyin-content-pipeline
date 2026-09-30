# -*- coding: utf-8 -*-
"""媒体知识库 WebUI —— 转写进度看板 + 按主播浏览逐字稿（md 渲染为 HTML）

设计要点：
- 只读：不写任何业务数据，安全可长期常驻
- 索引缓存：扫描各作者目录下 *.md 并解析头部字段；带 TTL 缓存，避免每次请求全量读盘
- 逐字稿正文按行渲染成独立 <p>（正文是每行数千字的长段落、行间无空行，
  直接交给 markdown 会被合并成一大段，必须按行拆）
- 长期在线：由 checkpoint.py 每轮巡检兜底拉起（见 webui 单实例锁）

用法：python webui.py [--host 0.0.0.0] [--port 8770]
"""
import os, sys, re, json, time, csv, sqlite3, threading, argparse, html as H

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("WB_SOURCE", "lib")

from flask import (Flask, render_template_string, request, jsonify,
                   send_from_directory, abort)
import markdown as mdlib

import asr_batch as B
import lib_source

OUT_ROOT = B.OUT_ROOT
IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif")
FIELDS = ("作者", "抖音账号", "作品ID", "视频标题", "发布时间", "关键词", "视频封面")
FIELD_RE = re.compile(r"^(" + "|".join(FIELDS) + r")：(.*)$")
COVER_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
VID_RE = re.compile(r"/video/(\d+)")

INDEX_TTL = float(os.environ.get("WB_UI_TTL", "60"))
_lock = threading.Lock()
_cache = {"ts": 0.0, "docs": [], "authors": {}}

app = Flask(__name__)


# ────────────────────────── 索引构建 ──────────────────────────

def _author_dirs():
    if not os.path.isdir(OUT_ROOT):
        return []
    return [d for d in sorted(os.listdir(OUT_ROOT))
            if os.path.isdir(os.path.join(OUT_ROOT, d)) and not d.startswith(("_", "."))]


def parse_md_text(txt):
    """解析逐字稿 md → (头部字段, 总结 markdown, 逐字稿纯文本)"""
    lines = txt.split("\n")

    sep = next((i for i, ln in enumerate(lines) if ln.strip() == "-----"), None)
    head, body = (lines[:sep], lines[sep + 1:]) if sep is not None else (lines, [])

    fields, cur = {}, None
    for ln in head:
        m = FIELD_RE.match(ln)
        if m:
            cur = m.group(1)
            fields[cur] = m.group(2).strip()
        elif cur and ln.strip():
            fields[cur] = (fields[cur] + " " + ln.strip()).strip()   # 标题可能含换行

    sep2 = next((i for i, ln in enumerate(body) if ln.strip() == "-----"), None)
    summary_lines, trans_lines = (body[:sep2], body[sep2 + 1:]) if sep2 is not None else ([], body)

    trans = [ln for ln in trans_lines if ln.strip() and ln.strip() != "逐字稿"]
    return fields, "\n".join(summary_lines).strip(), "\n".join(trans).strip()


_SENT_RE = re.compile(r"[^。！？!?…]+[。！？!?…]+|[^。！？!?…]+$")


def split_paras(text, target=340, hard=900):
    """把逐字稿切成可读段落。

    源 md 的正文**几乎都是一整行**（实测 418 篇里 417 篇只有 1 行，ASR 输出不分段），
    直接渲染就是一个巨型 <p>：读起来是墙，浏览器排版也吃力（长视频可达十几万字）。
    这里先按 。！？!?… 切句，再按目标字数聚合成段；无标点的长串按硬上限兜底切分。
    """
    chunks = []
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        if len(line) <= hard:            # 本身不长的行原样作一段
            chunks.append(line)
            continue
        buf = ""
        for s in _SENT_RE.findall(line):
            s = s.strip()
            if not s:
                continue
            if buf and len(buf) + len(s) > target:
                chunks.append(buf)
                buf = s
            else:
                buf += s
        if buf:
            chunks.append(buf)

    out = []
    for p in chunks:                     # 兜底：无标点的超长串再硬切
        while len(p) > hard * 2:
            out.append(p[:hard * 2])
            p = p[hard * 2:]
        if p:
            out.append(p)
    return out


def build_index(force=False):
    """扫描全部 md 建立索引（TTL 缓存）"""
    now = time.time()
    if not force and _cache["docs"] and now - _cache["ts"] < INDEX_TTL:
        return _cache

    reg = B.load_registry()
    docs, authors = [], {}
    for au in _author_dirs():
        ap = os.path.join(OUT_ROOT, au)
        try:
            names = [f for f in os.listdir(ap) if f.lower().endswith(".md")]
        except Exception:
            continue
        for fn in names:
            fp = os.path.join(ap, fn)
            stem = fn[:-3]
            try:
                txt = open(fp, encoding="utf-8", errors="replace").read()
                st = os.stat(fp)
            except Exception:
                continue
            f, summ, trans = parse_md_text(txt)
            vid = ""
            m = VID_RE.search(f.get("作品ID", ""))
            if m:
                vid = m.group(1)
            if not vid:
                m2 = re.search(r"(\d{15,})", stem)
                vid = m2.group(1) if m2 else ""

            cov = ""
            mc = COVER_RE.search(f.get("视频封面", ""))
            if mc:
                cov = mc.group(1).strip()
            if not cov:
                for ext in IMG_EXT:
                    if os.path.exists(os.path.join(ap, stem + ext)):
                        cov = stem + ext
                        break
            re_e = reg.get(vid, {}) if vid else {}

            d = {
                "author": f.get("作者") or au,
                "stem": stem, "file": fn, "vid": vid,
                "account": f.get("抖音账号", ""),
                "title": " ".join((f.get("视频标题") or stem).split()),
                "pub": f.get("发布时间", ""),
                "keywords": [k.strip() for k in re.split(r"[、,，]", f.get("关键词", "")) if k.strip()],
                "cover": cov,
                "has_cover": bool(cov) and os.path.exists(os.path.join(ap, cov)),
                "dur": re_e.get("duration") or 0,
                "chars": len(re.sub(r"\s", "", trans)),
                "has_summary": bool(summ) and "不足以做内容总结" not in summ,
                "low_content": bool(re_e.get("low_content")),
            }
            docs.append(d)
            a = authors.setdefault(d["author"], {"name": d["author"], "docs": 0, "chars": 0, "dur": 0.0})
            a["docs"] += 1
            a["chars"] += d["chars"]
            a["dur"] += d["dur"] or 0

    docs.sort(key=lambda d: (d["pub"] or "", d["stem"]), reverse=True)
    with _lock:
        _cache["docs"], _cache["authors"], _cache["ts"] = docs, authors, now
    return _cache


def _pid_from_lock(name):
    p = os.path.join(OUT_ROOT, f"_{name}.lock")
    try:
        return int(open(p, encoding="utf-8").read().strip())
    except Exception:
        return None


def _alive(name):
    pid = _pid_from_lock(name)
    return bool(pid) and B._pid_alive(pid)


ASR_API = os.environ.get("WB_ASR_API", "http://127.0.0.1:8766")   # 可用环境变量覆盖（跨机时指外机 8766）
STREAM_MARK = "[Qwen3-ASR] 转写中:"
STREAM_END = "[Qwen3-ASR] 完成:"


def _asr_up():
    try:
        import requests
        s = requests.Session(); s.trust_env = False
        s.get(ASR_API + "/", timeout=3)
        return True
    except Exception:
        return False


def _asr_get(path, timeout=5):
    """请求本地 ASR 服务。**必须 trust_env=False** —— 系统代理(Clash 127.0.0.1:52790)
    会把 127.0.0.1 的请求也劫走，导致连接被拒。"""
    import requests
    s = requests.Session(); s.trust_env = False
    return s.get(ASR_API + path, timeout=timeout).json()


def _stage1_progress():
    """从 stage1 日志解析「已完成条数 / 当前正在处理第几条」。

    stage1 逐条串行、**每条结束才打印一行** `[i/N] SKIP|OK|FAIL vid`，
    所以最后一行是第 i 条，正在处理的就是第 i+1 条。
    日志会累积到几 MB，只读尾部 2MB 足够。
    """
    p = os.path.join(OUT_ROOT, "_asr_stage1.log")
    try:
        size = os.path.getsize(p)
        with open(p, "rb") as f:
            f.seek(max(0, size - 2_000_000))
            tail = f.read().decode("utf-8", "replace")
        silent = max(0.0, time.time() - os.path.getmtime(p))
    except Exception:
        return None
    ms = re.findall(r"^\[(\d+)/(\d+)\]\s+(SKIP|OK|FAIL)\s+(\d+)", tail, re.M)
    if not ms:
        return None
    done, total, _st, last_vid = int(ms[-1][0]), int(ms[-1][1]), ms[-1][2], ms[-1][3]
    return {"done": done, "total": total, "last_vid": last_vid, "silent_sec": silent}


_HIST = {"ts": 0.0, "items": []}


def _asr_history(ttl=3.0):
    """作业历史（带短 TTL 缓存）。

    `live_state()` 会对**每个未完成任务**判断有没有活跃作业，逐个去查等于几百次
    HTTP 请求。这里取一次、缓存几秒，扫描全在本地做。
    """
    now = time.time()
    if _HIST["items"] and now - _HIST["ts"] < ttl:
        return _HIST["items"]
    try:
        items = _asr_get("/api/history/transcribe?limit=60").get("items", []) or []
        _HIST["items"] = items
        _HIST["ts"] = now
    except Exception:
        pass
    return _HIST["items"]


def _asr_job_for(vid, items=None):
    """找该作品对应的 ASR 作业并取实时状态。

    注意：`/api/history/transcribe` 走的是 SQLite **历史快照**，progress 可能是旧的；
    `/api/status/<job>` 才是内存里的实时真相。所以这里两步走：先用历史找 job_id，
    再查 status。优先选还在进行中的那个（同 vid 可能有重复提交的作业）。

    ⚠️ 切片转写的上传名是 `{vid}_{tag}_{k:03d}.mp4`（每段一个 job），**不是** `{vid}.mp4`，
    所以必须同时接受前缀匹配，否则长视频切片期间看板完全看不到作业（显示成空白/卡死）。
    """
    if not vid:
        return None
    items = _asr_history() if items is None else items
    name = str(vid) + ".mp4"
    pref = str(vid) + "_"
    cand = [it for it in items
            if (it.get("filename") or "") == name
            or (it.get("filename") or "").startswith(pref)]
    if not cand:
        return None
    cand.sort(key=lambda x: x.get("created_at") or 0, reverse=True)
    # ⚠️ **必须取「最新提交」的那个作业（cand[0]），不能"优先找活跃的"**。
    # 服务端重启会在内存里留下 `audio_ready` 的**僵尸 job**（状态永远不动），
    # 一旦优先匹配到它，看板就会显示「已跑 107:42 / 音频就绪 91.5% / 引擎无输出」
    # 这种彻底的假象，而真正最新的作业其实早就 `done` 了
    # （实测 2026-09-28：僵尸 `a2cf56f1` 盖住了已完成的 `b1854b34`）。
    # 取最新提交的语义也更清晰：同 vid 的多次提交里，最后一次才是当前有效的。
    pick = cand[0]
    try:
        st = _asr_get("/api/status/" + str(pick["job_id"]))
    except Exception:
        return None
    if st.get("error"):
        return None
    st["_created_at"] = pick.get("created_at")
    return st


def _slice_progress(vid):
    """切片进度：段总数 / 已完成段数 / 已完成音频秒数 / 总音频秒数。

    ⚠️ 段文件的位置是**分层的**：`seg_XXX.wav` 落在 `_slice_tmp/<vid>/<tag>/`，
    而转写结果 `seg_XXX.json` 落在 `_slice_tmp/<vid>/`（vid 直属层）。
    所以**必须按 `seg_XXX` 编号跨目录配对**，不能拿 json 路径直接拼 `.wav`
    （实测那样 getsize 必抛异常 → `done_sec` 恒为 0 → 进度只显示当前段、像卡住）。

    wav 是 16kHz 单声道 s16le → 每秒固定 32000 字节，用文件大小反推时长。
    """
    if not vid:
        return None
    base = os.path.join(B.SLICE_DIR, str(vid))
    if not os.path.isdir(base):
        return None
    wavs = {}       # seg 编号（'seg_000'）→ 秒数
    jsons = set()   # 已完成转写的 seg 编号
    for root, _dirs, files in os.walk(base):
        for fn in files:
            if not fn.startswith("seg_"):
                continue
            if fn.endswith(".wav"):
                try:
                    wavs[fn[:-4]] = os.path.getsize(os.path.join(root, fn)) / 32000.0
                except Exception:
                    wavs[fn[:-4]] = 0.0
            elif fn.endswith(".json"):
                jsons.add(fn[:-5])
    if not wavs:
        return None
    total_sec = sum(wavs.values())
    done_sec = sum(wavs.get(k, 0.0) for k in jsons)
    return {"parts": len(wavs), "done": len(jsons),
            "done_sec": round(done_sec, 1), "total_sec": round(total_sec, 1)}


# ── 「在跑」判据参数（`_active_vid` 用）─────────────────────────────────
# 服务端崩溃重启会在内存/DB 里留下 `audio_ready` 且**永不推进**的僵尸作业
# （见 `asr_batch.ZOMBIE_SEC`）。僵尸的 `created_at` 是几十分钟~几小时前，
# 所以按「活跃窗口」过滤远比只按状态名过滤准。
LIVE_JOB_WIN = {           # 状态 → 超过这个秒数没推进就当僵尸/残留，不算「在跑」（0=不过期）
    "transcribing": 0,     # 真在解码，多久都算在跑
    "waiting": 600,        # 排队中：正常几秒内就会被启动
    "audio_ready": 300,    # 音频已抽好等启动：客户端 120s 内必启动，超时即僵尸
}
LIVE_JOB_WEIGHT = {"transcribing": 2, "waiting": 1, "audio_ready": 0}
# 切片目录「最近一次写入」超过这个秒数就不算在跑（正常每段几百秒就会落一个 seg 文件）
LIVE_SLICE_SEC = float(os.environ.get("WB_LIVE_SLICE_SEC", "3600"))


def _slice_mtime(base):
    """切片目录里**最新一次写入段文件**的时间戳（段 wav/json 都是刚写完就在，很新）。"""
    mt = 0.0
    try:
        for root, _dirs, files in os.walk(base):
            for fn in files:
                if fn.startswith("seg_"):
                    mt = max(mt, os.path.getmtime(os.path.join(root, fn)))
    except Exception:
        pass
    return mt or os.path.getmtime(base)


def _active_vid(tasks, ok, items=None):
    """在未完成任务里找**确实在跑**的那一条（stage1 未运行时的兜底）。

    ⚠️ 为什么需要它：stage1 日志是**累积**的，收工后最后一行仍停在 `[N/N] OK …`。
    如果无脑拿 `prog["done"]+1` 推「当前条目」，stage1 明明没在跑，看板却会
    凭空造出一条假的（实测误报 `7669747470733276282 罗天行`，进度 0%、无切片信息）。

    ⚠️ **绝不能按任务顺序取第一个「有活跃作业」的条目**（实测踩坑 2026-09-28 23:00）：
    被放弃的条目（何广智 `7544952630338997539`）会留下一个 `audio_ready` 僵尸作业，
    它"看起来活跃"却永不推进；只要它在 tasks 里更靠前，看板就会**一直钉在它身上**
    （显示 `86.8% / 音频就绪` 不动，`stream_src=log` 取不到引擎流），
    而 stage1 其实早在跑后面那条 19 段 / 278 分钟的长视频。

    判据（打分排序，取最高分）：
      ① 作业证据 = (状态权重 transcribing>waiting>audio_ready, created_at)，
         且必须落在 `LIVE_JOB_WIN` 活跃窗口内（把僵尸过滤掉）
      ② 切片目录证据 = (0, 目录内最新 seg 文件 mtime)，且不超过 `LIVE_SLICE_SEC`
         —— 覆盖「段已切好、正在逐段转写/合并」的段间空档
    真正在跑的那条必然同时拥有**最新鲜的活动时间戳**，所以它一定胜出；全无 → None。
    """
    items = _asr_history() if items is None else items
    now = time.time()

    live = {}                                          # vid → (权重, 活动时间)
    for it in items:
        st = it.get("status")
        if st not in LIVE_JOB_WEIGHT:
            continue
        ca = it.get("created_at") or 0
        win = LIVE_JOB_WIN.get(st, 0)
        if win and ca and now - ca > win:
            continue                                   # 僵尸/残留作业，不算在跑
        m = re.match(r"^(\d{15,})(?:_|\.mp4$)", it.get("filename") or "")
        if not m:
            continue
        ev = (LIVE_JOB_WEIGHT[st], float(ca))
        if ev > live.get(m.group(1), (-1, 0.0)):
            live[m.group(1)] = ev

    best = None
    for t in tasks:
        vid = str(t.get("vid") or "")
        if not vid or vid in ok:
            continue
        ev = live.get(vid)
        d = os.path.join(B.SLICE_DIR, vid)
        if os.path.isdir(d):
            mt = _slice_mtime(d)
            if mt and now - mt <= LIVE_SLICE_SEC:
                ev2 = (0, mt)
                if ev is None or ev2 > ev:
                    ev = ev2
        if ev is None:
            continue
        if best is None or ev > best[0]:
            best = (ev, t)
    return best[1] if best else None


def _stream_text(limit=4000):
    """取「正在生成」的实时逐字文本。

    原理：引擎的 `_decode(streaming=True)`（默认开）会**逐 token** 把解码结果
    `print(..., end='')` 到服务端 stdout，且标点后自动补换行 —— 也就是服务端日志
    本身就是一路实时文本流，不需要改任何第三方代码。前提是服务端启动时把 stdout
    重定向到 `_asr_server.log`（`asr_batch.ensure_server()` 就是这么起的）。
    """
    p = os.path.join(OUT_ROOT, "_asr_server.log")
    try:
        size = os.path.getsize(p)
        with open(p, "rb") as f:
            f.seek(max(0, size - 400_000))
            tail = f.read().decode("utf-8", "replace")
    except Exception:
        return ""

    i = tail.rfind(STREAM_MARK)
    if i < 0:
        return ""
    seg = tail[i + len(STREAM_MARK):]
    if seg.rfind(STREAM_END) >= 0:      # 最后一条已收尾 → 当前没有活跃流
        return ""
    if "\n" not in seg:                 # 只有标记行、内容还没出来
        return ""
    seg = seg.split("\n", 1)[1]
    # 剔除混进来的服务端日志行（以 [ 开头）与统计块
    lines = [ln for ln in seg.split("\n")
             if ln.strip() and not ln.lstrip().startswith(("[", "📊", "🔹", "===="))]
    txt = "\n".join(lines).strip()
    return txt[-limit:] if len(txt) > limit else txt


RTF = float(os.environ.get("WB_RTF", "0.85"))     # 端到端有效实时率（估进度用）


def live_state():
    """实时转写状态：当前条目 + 真实进度 + 服务端作业状态 + 流式文本。

    「真实进度」说明：服务端自己的 progress 只有 2/15/20/80/90/100 几个档位，
    转写过程长达数十分钟一直停在 15，等于没有进度。所以这里用
    `已跑时长 / (音频时长 × RTF)` 算真实百分比 —— 误差来自音频类型差异，
    但对「还有多久」的感知足够。已跑时长取服务端的 elapsed_sec（真实耗时，非估算）。
    """
    prog = _stage1_progress()
    try:
        tasks = lib_source.build_lib_tasks()
    except Exception:
        tasks = []
    reg = B.load_registry()
    ok = {v for v, m in reg.items() if m.get("ok")}

    # 「当前条目」判定：**实证优先**。
    # ⚠️ 日志推导 `prog["done"]+1` 有两个坑：
    #   ① 日志是累积的 —— stage1 收工后最后一行仍停着，无脑推会造出假条目；
    #   ② 当前条目是**长视频切片**时，一条要跑 1~2 小时才落一行 `[i/N] OK`，
    #      这期间日志的 i 严重滞后（实测 stage1 明明在跑 7542610336659115298 的第 4 段，
    #      日志还停在上一次 pass 的 `[650/650]` → 推出第 651 条「罗天行 3 分钟」的假条目）。
    #   ③ `_asr_stage1.log` 的 N 与当前 `len(tasks)` 可能不同（库在增长）→ 序号体系不可比。
    # 所以：先按「活跃 ASR 作业 / `_slice_tmp/<vid>/` 存在」找**实证在跑**的那条；
    # 只有拿不到实证（例如 stage1 正在抽音频/切段、还没提交作业）且日志序号体系一致时，
    # 才退回日志推导。
    s1 = _alive("stage1")
    out = {
        "stage1": s1, "stage2": _alive("stage2"), "asr": _asr_up(),
        "total": len(tasks), "done": sum(1 for t in tasks if t["vid"] in ok),
        "item": None, "job": None, "item_percent": 0.0, "slice": None,
        "stream": "", "stream_chars": 0,
        "silent_sec": round(prog["silent_sec"], 1) if prog else None,
        "ts": time.strftime("%H:%M:%S"),
    }
    cand = _active_vid(tasks, ok)
    if cand is None and s1 and prog and tasks and prog["total"] == len(tasks):
        idx = prog["done"] + 1
        if 1 <= idx <= len(tasks):
            cand = tasks[idx - 1]
    if s1 and prog and prog["total"] == len(tasks):
        out["done"] = max(out["done"], prog["done"])
    if cand is not None:
        idx = next((i + 1 for i, t in enumerate(tasks)
                    if str(t.get("vid")) == str(cand.get("vid"))), 0)
        t = cand
        dur = float(t.get("dur_csv") or 0)
        job = _asr_job_for(t.get("vid"))
        sl = _slice_progress(t.get("vid"))
        pct = 0.0
        eta = None
        if job:
            el = float(job.get("elapsed_sec") or 0)
            st = job.get("status")
            if sl and sl["parts"] > 1:
                # 切片模式：服务端 elapsed 只覆盖**当前段**，按整条时长算会一直显示 ~5%
                # 像卡死 → 用「已完成段音频秒数 + 当前段已跑」除以总音频秒数
                cur = el if st in ("transcribing", "audio_ready", "done") else 0.0
                got = sl["done_sec"] + cur
                pct = min(99.0, max(2.0, got / max(sl["total_sec"], 1) * 100))
                eta = max(0, int((sl["total_sec"] - got) * RTF))
            elif st == "transcribing" and dur > 0:
                pct = min(99.0, max(2.0, el / (dur * RTF) * 100))
                eta = max(0, int(dur * RTF - el))
            elif st == "waiting":
                pct = 1.0
            elif st == "audio_ready":
                pct = 20.0
            elif st == "done":
                pct = 100.0
            job["percent"] = round(pct, 1)
            job["eta_sec"] = eta
        elif sl and sl["parts"] > 1:
            # 段落之间的空档（上一段刚完、下一段还没提交）也要显示累计进度
            pct = min(99.0, max(2.0, sl["done_sec"] / max(sl["total_sec"], 1) * 100))
        out["slice"] = sl
        out["item"] = {
            "idx": idx, "total": len(tasks),
            "author": t.get("author") or "", "vid": str(t.get("vid") or ""),
            "title": (t.get("title") or "").strip(), "pub": t.get("pub") or "",
            "dur_sec": round(dur, 1),
        }
        out["job"] = job
        out["item_percent"] = round(pct, 1)
    # 逐词流式文本：优先取服务端 job 的 current_chunk（服务端把引擎 stdout 的
    # token 流 tee 进了这个字段）；取不到再退回从服务端日志抓（服务端 stdout
    # 恰好被重定向到 _asr_server.log 时才有）。
    job = out.get("job") or {}
    txt = (job.get("current_chunk") or "").strip()
    src = "engine" if txt else ""
    if not txt:
        txt = _stream_text()
        src = "log" if txt else ""
    out["stream"] = txt
    out["stream_chars"] = len(txt)
    out["stream_src"] = src
    return out


# ────────────────────────── 视频下载进度 ──────────────────────────
# 数据源三处（实测全量 ≈0.25s，含巫师财经 18MB CSV）：
#   ① Data/*.csv                → 该账号的**作品清单**（去重作品ID + 类型）＝分母
#   ② <账号目录>/*               → 磁盘上真实存在的文件 → 已下载 / 有音轨 / 有封面
#   ③ Volume/Cache/*            → 正在下载的半成品（mtime 新鲜即视为进行中）
# 另读 Volume/DouK-Downloader.db 的 download_data 表（下载工具自己的记录）做交叉印证。
DL_ROOT = os.environ.get("WB_DL_ROOT", r"D:\视频\自媒体视频库")
DL_DATA = os.path.join(DL_ROOT, "Data")
DL_TOOL = os.path.join(DL_ROOT, "_tools", "TikTokDownloader")
DL_DB = os.path.join(DL_TOOL, "Volume", "DouK-Downloader.db")
DL_CACHE = os.path.join(DL_TOOL, "Volume", "Cache")
DL_SKIP = os.path.join(DL_ROOT, "_skip_ids.json")
DL_TTL = float(os.environ.get("WB_DL_TTL", "20"))
AUDIO_SET = {".m4a", ".mp3", ".aac", ".flac", ".wav"}          # 音轨 → ASR 直接可用
VIDEO_SET = {".mp4", ".mkv", ".mov", ".flv", ".ts", ".webm"}   # 原片 → 下完待抽音轨
IMG_SET = {".jpeg", ".jpg", ".png", ".webp", ".gif"}
ID_RE = re.compile(r"(\d{15,})")
# 「合集」的下载文件名**不带作品ID**，只带 `YYYY-MM-DD HH.MM.SS` 前缀（= 发布时间，冒号换成点）
TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}\.\d{2}\.\d{2})")
# 下载器给 Cache/成品的命名：`<日期> <时间>-<类型>-<账号>-<标题>`
CACHE_NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}\.\d{2}\.\d{2})-(.+?)-(.+?)-(.+)$")


def _split_cache_name(stem):
    """`2025-08-22 09.00.01-视频-播客正片合集-<标题>` → (日期时间, 类型, 账号, 标题)。

    ⚠️ 别用 `split("-", 3)` —— 日期自带连字符（2025-08-22），会把字段全都拆错位
    （实测把「账号」解析成 `17 12.08.27`）。
    """
    m = CACHE_NAME_RE.match(stem)
    if m:
        return (f"{m.group(1)} {m.group(2)}", m.group(3).strip(),
                m.group(4).strip(), m.group(5).strip())
    parts = (stem.split("-", 3) + ["", "", "", ""])[:4]
    return parts[0].strip(), parts[1].strip(), parts[2].strip(), parts[3].strip()

_dl_cache = {"ts": 0.0, "data": None, "key2id": {}, "ids": {}, "vid2label": {}}
_dl_seen = {}          # 缓存文件路径 → (size, ts)，用于算实时下载速度


def _hms(s):
    """`HH:MM:SS`（抖音导出的「视频时长」格式）→ 秒。"""
    try:
        p = [int(x) for x in (s or "").strip().split(":")]
    except Exception:
        return 0.0
    if len(p) == 3:
        return p[0] * 3600 + p[1] * 60 + p[2]
    if len(p) == 2:
        return p[0] * 60 + p[1]
    return float(p[0]) if len(p) == 1 else 0.0


def _read_csv(path):
    """作品清单 CSV → {作品ID: {type,dur,ts,desc}}（自动去重；CSV 里有大量重复行）。"""
    out = {}
    try:
        with open(path, encoding="utf-8-sig", errors="replace", newline="") as f:
            for row in csv.DictReader(f):
                i = (row.get("作品ID") or "").strip()
                if not i or i in out:
                    continue
                out[i] = {"type": (row.get("作品类型") or "视频").strip(),
                          "dur": _hms(row.get("视频时长")),
                          "ts": (row.get("发布时间") or "").strip().replace(":", "."),
                          "desc": (row.get("作品描述") or "").strip()}
    except Exception:
        pass
    return out


def _key2id(ids):
    """两种「文件命名键」都映射到作品ID：① 作品ID ② 发布时间戳（合集用）。"""
    k = {i: i for i in ids}
    for i, r in ids.items():
        if r["ts"]:
            k.setdefault(r["ts"], i)
    return k


def _file_vid(fn, key2id):
    """文件名 → 作品ID。先认文件名里的 15+ 位数字，其次认 `YYYY-MM-DD HH.MM.SS` 前缀。"""
    stem = os.path.splitext(fn)[0]
    m = ID_RE.search(stem)
    if m:
        v = key2id.get(m.group(1))
        if v:
            return v
    m = TS_RE.match(stem)
    if m:
        return key2id.get(m.group(1))
    return None


def _scan_account(d, key2id):
    """扫账号目录 → {作品ID: {"ext": {...}, "bytes": n}}（每个文件归到它所属的作品）。"""
    out = {}
    try:
        names = os.listdir(d)
    except Exception:
        return out
    for fn in names:
        vid = _file_vid(fn, key2id)
        if not vid:
            continue
        fp = os.path.join(d, fn)
        try:
            if not os.path.isfile(fp):
                continue
            sz = os.path.getsize(fp)
        except Exception:
            continue
        r = out.setdefault(vid, {"ext": set(), "bytes": 0})
        r["ext"].add(os.path.splitext(fn)[1].lower())
        r["bytes"] += sz
    return out


def _is_done(rec, f):
    """作品是否「下载完成」。

    ⚠️ 不能只看「有文件」—— 下载器**会先把封面 .jpeg 落盘**，只按文件存在判断会让
    进度虚高（合集实测：9 个原片 + 13 张封面 → 会误报 13/37）。
    所以：视频类必须**有音轨(.m4a) 或 原片(.mp4)**；图集/实况只要有文件就算下完。
    """
    if not f:
        return False
    if rec.get("type") == "视频":
        return bool(f["ext"] & (AUDIO_SET | VIDEO_SET))
    return True


def _downloading(now, key2id=None, ids=None, vid2label=None):
    """正在下载的半成品（Volume/Cache 里 mtime 5 分钟内的文件），带体积、速度与时长。

    文件名形如 `2025-08-22 09.00.01-视频-<账号>-<标题>.mp4`，拆出账号与标题直接展示。
    注意：下载器按 ~100MB 缓冲块落盘，所以 size 是块级精度、speed 也是块级平均值
    （块之间的空档会算成 0）—— 只作「有没有在动」的参考，不要当成字节级精确值。

    另外把 Cache 里**超过 5 分钟没动**的文件单列为 stalled（疑似停滞，供运维判断）。
    """
    items, stalled = [], []
    try:
        names = os.listdir(DL_CACHE)
    except Exception:
        return items, 0, stalled
    for fn in names:
        if fn.startswith("_"):
            continue
        fp = os.path.join(DL_CACHE, fn)
        try:
            if not os.path.isfile(fp):
                continue
            st = os.stat(fp)
        except Exception:
            continue
        idle = round(now - st.st_mtime, 1)
        date, kind, acct, title = _split_cache_name(fn.rsplit(".", 1)[0])
        vid = _file_vid(fn, key2id) if key2id else None
        if vid and vid2label:
            acct = vid2label.get(vid) or acct    # 统一显示作者名（合集→罗永浩的十字路口）
        rec = (ids or {}).get(vid) or {}
        row = {"file": fn, "date": date, "kind": kind,
               "account": acct, "title": title,
               "size": st.st_size, "speed": None,
               "vid": vid or "", "dur": rec.get("dur") or 0, "idle_sec": idle}
        if idle > 300:
            stalled.append(row)          # 老文件：不再当"在下载"，只提示停滞
            continue
        prev = _dl_seen.get(fp)
        if prev and now - prev[1] >= 1.0 and st.st_size > prev[0]:
            row["speed"] = (st.st_size - prev[0]) / (now - prev[1])
        _dl_seen[fp] = (st.st_size, now)
        items.append(row)
    items.sort(key=lambda x: -x["size"])
    stalled.sort(key=lambda x: -x["size"])
    return items, sum(x["size"] for x in items), stalled


def _skip_ids():
    """永久下不到的作品 → {作品ID: 原因}。

    来源 `D:\\视频\\自媒体视频库\\_skip_ids.json`（与采集技能 douyin-incremental-harvest 同一份名单）：
    源端 AAC 流损坏 / 取下载地址失败（作者删除、私密、仅 APP 可见）。
    这些**不该算进「还没下完」** —— 否则进度永远到不了 100%，天天看着像有欠账。
    """
    out = {}
    try:
        d = json.load(open(DL_SKIP, encoding="utf-8"))
        for r in (d.get("ids") or []):
            i = str(r.get("id") or "").strip()
            if i:
                out[i] = (r.get("reason") or "").strip()
    except Exception:
        pass
    return out


def download_state(force=False):
    """视频下载进度：按账号统计「已下载 / 作品总数」，外加正在下载的半成品列表。

    分母 = Data/*.csv 的作品清单（去重后的作品ID）；分子 = 账号目录里**真实存在文件**的作品ID。
    所以「已下载」是磁盘事实，不是下载工具的自述（`download_data` 表只做交叉印证 recorded）。
    """
    now = time.time()
    cache_ok = (not force) and _dl_cache["data"] and (now - _dl_cache["ts"] < DL_TTL)
    if cache_ok:
        data = dict(_dl_cache["data"])
    else:
        try:
            con = sqlite3.connect(f"file:{DL_DB}?mode=ro", uri=True, timeout=3)
            recorded = {r[0] for r in con.execute("SELECT ID FROM download_data")}
            con.close()
        except Exception:
            recorded = set()

        try:
            csvs = sorted(f for f in os.listdir(DL_DATA) if f.lower().endswith(".csv"))
        except Exception:
            csvs = []

        accounts, all_ids, key2id, vid2label = [], {}, {}, {}
        skips = _skip_ids()
        for fn in csvs:
            tag = fn[:-4]
            d = os.path.join(DL_ROOT, tag)
            if not os.path.isdir(d):
                continue
            ids = _read_csv(os.path.join(DL_DATA, fn))
            if not ids:
                continue
            all_ids.update(ids)
            for k, v in _key2id(ids).items():
                key2id.setdefault(k, v)

            files = _scan_account(d, key2id)
            total = len(ids)
            # 完成判据见 _is_done()：视频类要音轨或原片，图集有文件即算完；
            # 永久跳过名单（源端损坏/取地址失败）不计入未完成
            pending = [i for i in ids
                       if i not in skips and not _is_done(ids[i], files.get(i))]
            skipped = sum(1 for i in ids if i in skips)
            eff = total - skipped          # 有效分母（扣掉永久跳过）
            parts = tag.split("_")
            ov = lib_source.AUTHOR_OVERRIDE.get(tag)      # 合集：CSV 的「账号昵称」只是合集名
            label = ov[0] if ov else (parts[1] if len(parts) > 2 else tag)
            for i in ids:
                vid2label[i] = label
            accounts.append({
                "name": tag, "label": label,
                "kind": "合集" if tag.startswith("MID") else "发布作品",
                "total": total, "downloaded": total - len(pending) - skipped,
                "effective_total": eff, "skipped": skipped,
                "percent": round((total - len(pending) - skipped) / eff * 100, 1) if eff else 0,
                "videos": sum(1 for r in ids.values() if r["type"] == "视频"),
                "audio": sum(1 for f in files.values() if f["ext"] & AUDIO_SET),
                "video": sum(1 for f in files.values() if f["ext"] & VIDEO_SET),
                "cover": sum(1 for f in files.values() if f["ext"] & IMG_SET),
                # 只落到封面、音轨/原片还没来的（进度虚高的来源，单独暴露出来）
                "cover_only": sum(1 for i, f in files.items()
                                  if (f["ext"] & IMG_SET) and not _is_done(ids.get(i, {}), f)),
                "bytes": sum(f["bytes"] for f in files.values()),
                "recorded": sum(1 for i in ids if i in recorded),
                "pending": len(pending),
                "pending_sec": sum(ids[i]["dur"] for i in pending),
                "skip_sec": sum(ids[i]["dur"] for i in ids if i in skips),
                "dir": d,
            })
        # 已下完的账号排前面，新账号/合集（下载度低）排后面；同类按作品数降序
        accounts.sort(key=lambda a: (-a["percent"], -a["total"]))
        tot = sum(a["total"] for a in accounts)
        got = sum(a["downloaded"] for a in accounts)
        sk = sum(a["skipped"] for a in accounts)
        eff = tot - sk
        data = {
            "accounts": accounts,
            "total": tot, "effective_total": eff, "skipped": sk,
            "downloaded": got,
            "percent": round(got / eff * 100, 1) if eff else 0,
            "audio": sum(a["audio"] for a in accounts),
            "video": sum(a["video"] for a in accounts),
            "cover": sum(a["cover"] for a in accounts),
            "bytes": sum(a["bytes"] for a in accounts),
            "pending": tot - got - sk,
            "pending_sec": sum(a["pending_sec"] for a in accounts),
            "cover_only": sum(a["cover_only"] for a in accounts),
            "recorded": sum(a["recorded"] for a in accounts),
            "ts": time.strftime("%H:%M:%S"),
        }
        _dl_cache.update({"data": data, "ts": now, "key2id": key2id, "ids": all_ids,
                          "vid2label": vid2label})

    # 正在下载的部分始终取实时值，不吃 TTL 缓存
    items, nbytes, stalled = _downloading(now, _dl_cache["key2id"], _dl_cache["ids"],
                                          _dl_cache.get("vid2label"))
    data["downloading"] = items
    data["downloading_bytes"] = nbytes
    data["stalled"] = stalled
    data["active"] = bool(items)
    return data


def progress_data():
    c = build_index()
    try:
        tasks = lib_source.build_lib_tasks()
    except Exception:
        tasks = []
    reg = B.load_registry()
    ok = {v for v, m in reg.items() if m.get("ok")}

    total = len(tasks)
    done = sum(1 for t in tasks if t["vid"] in ok)
    todo = [t for t in tasks if t["vid"] not in ok]
    todo_h = sum((t.get("dur_csv") or 0) for t in todo) / 3600.0
    done_h = sum((t.get("dur_csv") or 0) for t in tasks if t["vid"] in ok) / 3600.0

    recent = sorted((dict(m, vid=v) for v, m in reg.items() if m.get("ok")),
                    key=lambda m: m.get("ts", ""), reverse=True)[:12]

    return {
        "total": total, "done": done, "todo": len(todo),
        "percent": round(done / total * 100, 1) if total else 0,
        "todo_h": round(todo_h, 2), "done_h": round(done_h, 2),
        "abandoned": sum(1 for m in reg.values() if m.get("abandoned")),
        "md_total": len(c["docs"]),
        "chars_total": sum(d["chars"] for d in c["docs"]),
        "s1": _alive("stage1"), "s2": _alive("stage2"), "asr": _asr_up(),
        "authors": sorted(c["authors"].values(), key=lambda a: -a["docs"]),
        "recent": recent,
        "index_ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(c["ts"])),
    }


# ────────────────────────── 模板 ──────────────────────────

CSS = """
*{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#f5f6f8;--card:#fff;--line:#e5e7eb;--txt:#1f2328;--dim:#6b7280;
 --accent:#2563eb;--ok:#16a34a;--bad:#dc2626;--soft:#f3f4f6}
body{background:var(--bg);color:var(--txt);
 font:15px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei","PingFang SC",sans-serif}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.wrap{max-width:1180px;margin:0 auto;padding:22px 20px 70px}
header.top{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:20px}
header.top h1{font-size:20px;font-weight:700}
header.top .sp{flex:1}
.badge{background:var(--soft);color:var(--dim);border-radius:999px;padding:3px 11px;font-size:12.5px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;margin-bottom:16px}
.card h2{font-size:15px;font-weight:700;margin-bottom:14px;display:flex;align-items:center;gap:9px;flex-wrap:wrap}
.grid{display:grid;gap:14px}
.g4{grid-template-columns:repeat(auto-fit,minmax(155px,1fr))}
.stat{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px}
.stat .k{font-size:12.5px;color:var(--dim);margin-bottom:6px}
.stat .v{font-size:26px;font-weight:700;letter-spacing:-.5px}
.stat .s{font-size:12.5px;color:var(--dim);margin-top:3px}
.bar{height:10px;background:var(--soft);border-radius:999px;overflow:hidden;margin:7px 0 8px}
.bar>i{display:block;height:100%;background:linear-gradient(90deg,#3b82f6,#22c55e);border-radius:999px;transition:width .4s}
.pill{display:inline-flex;align-items:center;gap:6px;border-radius:999px;padding:4px 11px;font-size:12.5px;background:var(--soft);color:var(--dim)}
.dot{width:8px;height:8px;border-radius:50%;background:#9ca3af;display:inline-block}
.dot.on{background:var(--ok);box-shadow:0 0 0 3px rgba(22,163,74,.16)}
.dot.off{background:var(--bad)}
.agrid{display:grid;gap:12px;grid-template-columns:repeat(auto-fill,minmax(200px,1fr))}
.acard{display:block;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:15px 16px;transition:.15s;color:inherit}
.acard:hover{border-color:#bfdbfe;box-shadow:0 4px 14px rgba(37,99,235,.09);text-decoration:none;transform:translateY(-1px)}
.acard .n{font-weight:700;font-size:15px;margin-bottom:6px}
.acard .big{font-size:22px;font-weight:700;color:var(--accent);line-height:1.3}
.acard .m{font-size:12.5px;color:var(--dim);display:flex;gap:12px;flex-wrap:wrap;margin-top:2px}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:10px;border-bottom:1px solid var(--line);font-size:14px;vertical-align:top}
th{font-size:12.5px;color:var(--dim);font-weight:600;background:var(--soft)}
tr:hover td{background:#fafbfc}
.tags{display:flex;gap:6px;flex-wrap:wrap}
.tag{background:#eff6ff;color:#1d4ed8;border-radius:6px;padding:2px 8px;font-size:12px;white-space:nowrap}
.tag.gray{background:var(--soft);color:var(--dim)}
input.q{width:100%;max-width:420px;padding:9px 13px;border:1px solid var(--line);border-radius:9px;font-size:14px;background:#fff;color:var(--txt)}
input.q:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px rgba(37,99,235,.12)}
.btn{display:inline-block;padding:8px 15px;border-radius:9px;background:var(--accent);color:#fff;font-size:14px;border:none;cursor:pointer}
.btn:hover{background:#1d4ed8;text-decoration:none}
.btn.ghost{background:var(--soft);color:var(--txt)}
.muted{color:var(--dim);font-size:13px}
.crumb{margin-bottom:14px;font-size:14px}
.dochead{display:flex;gap:22px;flex-wrap:wrap}
.dochead .meta{flex:1;min-width:280px}
img.cover{width:210px;max-height:300px;object-fit:cover;border-radius:10px;border:1px solid var(--line);background:var(--soft)}
.kv{display:flex;gap:8px;font-size:13.5px;margin:5px 0}
.kv .k{color:var(--dim);min-width:70px;flex-shrink:0}
h1.dt{font-size:19px;line-height:1.55;font-weight:700;margin-bottom:14px;word-break:break-word}
.md h2{font-size:16.5px;margin:22px 0 10px;padding-bottom:7px;border-bottom:1px solid var(--line)}
.md h3{font-size:15px;margin:17px 0 8px;color:#111827}
.md p{margin:10px 0}
.md ul{margin:10px 0 10px 22px}.md li{margin:6px 0}
.trans p{margin:14px 0;line-height:1.95;text-align:justify;color:#24292f}
.pager{display:flex;gap:10px;justify-content:space-between;margin-top:20px}
.note{background:#fffbeb;border:1px solid #fde68a;color:#92400e;border-radius:10px;padding:12px 14px;font-size:13.5px}
.empty{color:var(--dim);padding:26px 0;text-align:center}
.rowlink{display:block}
/* ── 实时转写面板 ── */
.lv-title{font-size:15px;font-weight:600;margin-bottom:4px;word-break:break-word;line-height:1.5}
.lv-bar{height:14px;margin:9px 0 6px}
.lv-bar>i{background:linear-gradient(90deg,#f59e0b,#ef4444)}
.lv-row{display:flex;gap:10px;align-items:center;margin:10px 0 12px;flex-wrap:wrap}
.lv-stream-wrap{border:1px solid var(--line);border-radius:10px;overflow:hidden}
.lv-stream-hd{background:var(--soft);padding:7px 12px;font-size:12.5px;color:var(--dim);
 display:flex;gap:8px;align-items:center;flex-wrap:wrap}
pre.lv-stream{margin:0;padding:12px 14px;max-height:270px;overflow-y:auto;
 font:13px/1.9 "Cascadia Mono","Consolas",Monaco,monospace;white-space:pre-wrap;
 word-break:break-word;background:#fbfcfd;color:#1f2937}
.lv-live{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--bad);
 animation:blink 1.1s infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.25}}
/* ── 视频下载进度 ── */
.dl-bar{height:14px;margin:9px 0 6px}
.dl-bar>i{background:linear-gradient(90deg,#0ea5e9,#14b8a6)}
.dl-fly{border:1px solid var(--line);border-radius:10px;padding:11px 13px;margin:12px 0 2px;background:#fbfcfd}
.dl-fly-hd{font-size:12.5px;color:var(--dim);margin-bottom:9px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.dl-fi{display:flex;gap:11px;align-items:center;margin:8px 0;flex-wrap:wrap;font-size:13px}
.dl-fi .t{flex:1;min-width:190px;word-break:break-word;line-height:1.45}
.dl-fi .mb{font-variant-numeric:tabular-nums;color:var(--dim);white-space:nowrap}
.dl-acct{background:#eef2ff;color:#4338ca;border-radius:5px;padding:1px 7px;font-size:12px;white-space:nowrap}
.dl-run{flex:0 0 96px;height:7px;border-radius:999px;overflow:hidden;background:#dbeafe;position:relative}
.dl-run>i{position:absolute;top:0;left:0;height:100%;width:42%;border-radius:999px;
 background:linear-gradient(90deg,#3b82f6,#22d3ee);animation:dlrun 1.35s ease-in-out infinite}
@keyframes dlrun{0%{left:-44%}100%{left:102%}}
.dl-stall{background:#fffbeb;border:1px solid #fde68a;color:#92400e;border-radius:9px;
 padding:9px 12px;font-size:12.5px;margin-top:10px;line-height:1.75}
tr.dl-pend td{background:#fffdf5}
"""

HEAD = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title><style>__CSS__</style></head><body><div class="wrap">
<header class="top"><h1><a href="/" style="color:inherit">📚 媒体知识库</a></h1>
<span class="badge">抖音逐字稿 · 转写看板</span><span class="sp"></span>
<a class="badge" href="/search">🔍 搜索</a></header>
"""
FOOT = """</div>__SCRIPT__</body></html>"""


@app.template_filter("fsize")
def _f_fsize(n):
    """字节数 → 人类可读（B/KB/MB/GB/TB）。"""
    try:
        n = float(n or 0)
    except Exception:
        return "—"
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return ("%.0f %s" % (n, u)) if u in ("B", "KB") else ("%.2f %s" % (n, u))
        n /= 1024.0
    return "%.2f TB" % n


def page(title, body, script="", **ctx):
    """只把 body 交给 Jinja 渲染，外壳（CSS/JS）用字符串拼接。

    为什么不让整页过 Jinja：CSS/JS 里出现 `{{` 或 `{%` 会被当成模板语法报错，
    而它们跟业务无关，没必要参与渲染。
    """
    inner = render_template_string(body, **ctx)
    shell = (HEAD.replace("__TITLE__", H.escape(title)).replace("__CSS__", CSS)
             + inner + FOOT.replace("__SCRIPT__", script))
    return shell


HOME_BODY = """
<div class="grid g4">
  <div class="stat"><div class="k">转写进度</div>
    <div class="v" id="s-done">{{ p.done }}<span style="font-size:15px;color:#6b7280"> / {{ p.total }}</span></div>
    <div class="bar"><i id="s-bar" style="width:{{ p.percent }}%"></i></div>
    <div class="s" id="s-pct">已完成 {{ p.percent }}%</div></div>
  <div class="stat"><div class="k">逐字稿篇数</div><div class="v">{{ p.md_total }}</div>
    <div class="s">共 {{ '{:,}'.format(p.chars_total) }} 字</div></div>
  <div class="stat"><div class="k">已完成音频</div>
    <div class="v">{{ '%.1f'|format(p.done_h) }}<span style="font-size:15px;color:#6b7280"> h</span></div>
    <div class="s">剩余 {{ '%.2f'|format(p.todo_h) }} h · {{ p.todo }} 条</div></div>
  <div class="stat"><div class="k">组件状态</div>
    {% set idle = (p.todo == 0) %}
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:4px">
      <span class="pill"><i class="dot {{ 'on' if p.s1 else ('off' if not idle else '') }}" id="d-s1"></i>stage1<span id="t-s1">{% if not p.s1 and idle %} 已收工{% endif %}</span></span>
      <span class="pill"><i class="dot {{ 'on' if p.s2 else ('off' if not idle else '') }}" id="d-s2"></i>stage2<span id="t-s2">{% if not p.s2 and idle %} 已收工{% endif %}</span></span>
      <span class="pill"><i class="dot {{ 'on' if p.asr else 'off' }}" id="d-asr"></i>ASR</span>
    </div>
    <div class="s" style="margin-top:7px">索引更新于 {{ p.index_ts }}</div></div>
</div>

{% if p.todo %}<div class="note" style="margin:16px 0">还有 {{ p.todo }} 条待转写（剩余音频
{{ '%.2f'|format(p.todo_h) }} 小时）。{% if p.abandoned %}其中 {{ p.abandoned }} 条已标记为失败或待处理。{% endif %}</div>{% endif %}

<div class="card">
 <h2><span class="lv-live"></span> 实时转写
   <span class="badge" id="lv-badge">{% if l.item %}{{ l.item.idx }} / {{ l.item.total }} 条{% else %}空闲{% endif %}</span>
   <span style="flex:1"></span>
   <span class="muted" id="lv-clock">更新于 {{ l.ts }}</span></h2>
 <div class="lv-title" id="lv-title">{% if l.item %}{{ l.item.title or l.item.vid }}{% else %}当前没有在转写的条目{% endif %}</div>
 <div class="muted" id="lv-meta">{% if l.item %}{{ l.item.author }} · {{ l.item.pub }} · 作品ID {{ l.item.vid }} · 音频 {{ '%.1f'|format(l.item.dur_sec/60) }} 分钟{% else %}已完成 {{ l.done }} / {{ l.total }} 条{% endif %}</div>
 <div class="bar lv-bar"><i id="lv-bar" style="width:{{ l.item_percent }}%"></i></div>
 <div class="lv-row">
   <span class="pill" id="lv-status">{% if l.job %}{{ l.job.status }} · {{ l.item_percent }}%{% else %}待命{% endif %}</span>
   <span class="pill" id="lv-seg"{% if not l.slice %} style="display:none"{% endif %}>✂️ 切段 {{ l.slice.done if l.slice else 0 }} / {{ l.slice.parts if l.slice else 0 }}</span>
   <span class="muted" id="lv-elapsed">{% if l.job and l.job.elapsed_sec %}已跑 {{ (l.job.elapsed_sec|int)//60 }}:{{ '%02d'|format((l.job.elapsed_sec|int)%60) }}{% endif %}</span>
   <span style="flex:1"></span>
   <span class="muted" id="lv-eta">{% if l.job and l.job.eta_sec %}预计还需 {{ (l.job.eta_sec|int)//60 }}:{{ '%02d'|format((l.job.eta_sec|int)%60) }}{% endif %}</span>
 </div>
 <div class="lv-stream-wrap">
   <div class="lv-stream-hd">🎙️ 引擎实时识别输出 <span id="lv-chars">{% if l.stream_chars %}{{ l.stream_chars }} 字{% endif %}</span>
     <span style="flex:1"></span><span id="lv-note"></span></div>
   <pre class="lv-stream" id="lv-stream">{{ l.stream or '（转写开始后，引擎逐词输出的文字会实时出现在这里）' }}</pre>
 </div>
</div>

<div class="card">
 <h2>📥 视频下载进度
   <span class="badge" id="dl-badge">已下载 {{ dl.downloaded }} / {{ dl.effective_total }} 条</span>
   <span class="badge" id="dl-pend">{% if dl.pending %}剩余 {{ dl.pending }} 条 · 约 {{ '%.1f'|format(dl.pending_sec/3600) }} h{% else %}已下完{% endif %}</span>
   <span class="badge" id="dl-skip"{% if not dl.skipped %} style="display:none"{% endif %}>永久跳过 {{ dl.skipped }}</span>
   <span style="flex:1"></span>
   <span class="muted" id="dl-clock">更新于 {{ dl.ts }}</span></h2>
 <div class="bar dl-bar"><i id="dl-bar" style="width:{{ dl.percent }}%"></i></div>
 <div class="muted" id="dl-line">总进度 {{ dl.percent }}% · 音轨就绪 {{ dl.audio }} · 封面 {{ dl.cover }} · 占用 {{ dl.bytes|fsize }}</div>

 <div class="dl-fly" id="dl-fly"{% if not dl.active %} style="display:none"{% endif %}>
   <div class="dl-fly-hd">⚡ 正在下载 <span id="dl-fly-n">{{ dl.downloading|length }}</span> 个文件 ·
     合计 <span id="dl-fly-b">{{ dl.downloading_bytes|fsize }}</span>
     <span style="flex:1"></span><span>（下载器按 100MB 缓冲块落盘，体积/速度为块级精度）</span></div>
   <div id="dl-fly-body">
   {% for x in dl.downloading %}
     <div class="dl-fi">
       <span class="dl-run"><i></i></span>
       <span class="t">{{ x.title or x.file }}</span>
       <span class="mb">{% if x.account %}<span class="dl-acct">{{ x.account }}</span> {% endif %}{{ x.size|fsize }}{% if x.speed %} · {{ '%.1f'|format(x.speed/1048576) }} MB/s{% endif %}{% if x.dur %} · 时长 {{ '%d:%02d'|format((x.dur|int)//3600, ((x.dur|int)%3600)//60) }}{% endif %}</span>
     </div>
   {% endfor %}
   </div>
 </div>
 <div class="dl-stall" id="dl-stall"{% if not dl.stalled %} style="display:none"{% endif %}>⏸ 疑似停滞（Cache 里
   <span id="dl-stall-n">{{ dl.stalled|length }}</span> 个文件超过 5 分钟没有增长，可能已卡住或排队）：
   <span id="dl-stall-list">{% for x in dl.stalled %}{{ x.account }} · {{ x.title or x.file }}（{{ x.size|fsize }}）{% if not loop.last %}；{% endif %}{% endfor %}</span></div>

 <table style="margin-top:14px"><thead><tr>
   <th>账号 / 合集</th><th style="width:210px">下载进度</th>
   <th style="width:110px">已下载</th><th style="width:150px">剩余</th><th style="width:86px">占用</th>
 </tr></thead><tbody id="dl-tb">
 {% for a in dl.accounts %}
  <tr{% if a.pending %} class="dl-pend"{% endif %}>
   <td><b>{{ a.label }}</b>{% if a.kind == '合集' %} <span class="dl-acct">合集</span>{% endif %}
     <div class="muted" style="font-size:12px">{{ a.total }} 条{% if a.pending %} · 音轨 {{ a.audio }} · 封面 {{ a.cover }}{% if a.cover_only %} · 仅封面 {{ a.cover_only }}{% endif %}{% endif %}{% if a.skipped %} · 永久跳过 {{ a.skipped }}{% endif %}</div></td>
   <td><div class="bar" style="margin:2px 0 0"><i style="width:{{ a.percent }}%{% if a.percent == 0 %};background:#cbd5e1{% endif %}"></i></div>
     <div class="muted" style="font-size:12px">{{ a.percent }}%</div></td>
   <td class="muted">{{ a.downloaded }} / {{ a.effective_total }}</td>
   <td class="muted">{% if a.pending %}{{ a.pending }} 条 · {{ '%.1f'|format(a.pending_sec/3600) }} h{% else %}✅ 完成{% endif %}</td>
   <td class="muted">{{ a.bytes|fsize }}</td>
  </tr>
 {% endfor %}
 </tbody></table>
 <div class="muted" style="margin-top:11px">「已下载」= 磁盘上真实存在该作品的文件（对比 <code>Data/*.csv</code> 作品清单）；
 「音轨就绪」= 已有 <code>.m4a</code>，转写流水线可直接使用；原片 <code>.mp4</code> 下完抽音轨后转写。</div>
</div>

<div class="card"><h2>按主播分布 <span class="badge">{{ p.authors|length }} 位</span></h2>
 <div class="agrid">
 {% for a in p.authors %}
   <a class="acard" href="/author/{{ a.name }}">
     <div class="n">{{ a.name }}</div>
     <div class="big">{{ a.docs }}<span style="font-size:13px;color:#6b7280;font-weight:400"> 篇</span></div>
     <div class="m"><span>{{ '{:,}'.format(a.chars) }} 字</span><span>{{ '%.1f'|format(a.dur/3600) }} h</span></div>
   </a>
 {% endfor %}
 </div>
</div>

<div class="card"><h2>最近完成 <span class="badge">最新 12 条</span></h2>
 <table><thead><tr><th style="width:150px">完成时间</th><th style="width:140px">作者</th>
 <th>作品</th><th style="width:80px">时长</th></tr></thead><tbody>
 {% for r in p.recent %}
  <tr><td class="muted">{{ r.ts }}</td><td>{{ r.author }}</td>
   <td><a href="https://www.douyin.com/video/{{ r.vid }}" target="_blank" rel="noopener">{{ r.vid }}</a></td>
   <td class="muted">{{ '%.1f'|format((r.duration or 0)/60) }} 分</td></tr>
 {% endfor %}
 </tbody></table>
 <div class="muted" style="margin-top:12px">共 {{ p.md_total }} 篇逐字稿：点上方主播卡片进入浏览，或
 <a href="/search">搜索标题 / 关键词 / 全文</a>。</div>
</div>
"""

HOME_SCRIPT = """
<script>
function lvFmt(s){ if(s===null||s===undefined||isNaN(s)) return '—';
  s=Math.max(0,Math.round(s)); var m=Math.floor(s/60); return m+':'+String(s%60).padStart(2,'0'); }
var LV_LAST = null;
function lvTick(){
  fetch('/api/live').then(function(r){return r.json()}).then(function(d){
    function set(id,v){ var e=document.getElementById(id); if(e) e.textContent=v; }
    var badge=document.getElementById('lv-badge');
    var bar=document.getElementById('lv-bar');
    var stm=document.getElementById('lv-stream');
    set('lv-clock','更新于 '+d.ts);
    if(d.item){
      badge.textContent = d.item.idx+' / '+d.item.total+' 条';
      set('lv-title', d.item.title || d.item.vid);
      set('lv-meta', d.item.author+' · '+d.item.pub+' · 作品ID '+d.item.vid
          +' · 音频 '+(d.item.dur_sec/60).toFixed(1)+' 分钟');
      bar.style.width = d.item_percent+'%';
      var j=d.job||{};
      var sm={transcribing:'正在识别',audio_ready:'音频就绪，待启动',waiting:'排队等待',
              done:'已完成',error:'出错',no_audio:'无音频'};
      set('lv-status',(sm[j.status]||j.status||'准备中')+(j.percent!=null?' '+j.percent+'%':''));
      set('lv-elapsed', j.elapsed_sec!=null ? '已跑 '+lvFmt(j.elapsed_sec) : '');
      set('lv-eta', j.eta_sec!=null ? '预计还需 '+lvFmt(j.eta_sec) : '');
      var sg=document.getElementById('lv-seg');
      if(sg){ if(d.slice && d.slice.parts>1){
                sg.style.display='';
                sg.textContent='✂️ 切段 '+d.slice.done+' / '+d.slice.parts
                  +' · 音频 '+(d.slice.done_sec/60).toFixed(1)+'/'+(d.slice.total_sec/60).toFixed(1)+' 分钟';
              } else { sg.style.display='none'; } }
    } else {
      badge.textContent='空闲';
      set('lv-title','当前没有在转写的条目');
      set('lv-meta','已完成 '+d.done+' / '+d.total+' 条');
      bar.style.width='0%';
      set('lv-status','待命'); set('lv-elapsed',''); set('lv-eta','');
      var sg0=document.getElementById('lv-seg'); if(sg0) sg0.style.display='none';
    }
    var txt = d.stream || '';
    if(txt !== LV_LAST){
      LV_LAST = txt;
      var near = stm.scrollHeight - stm.scrollTop - stm.clientHeight < 80;
      stm.textContent = txt || (d.item
        ? '（引擎尚未输出到日志，见右侧提示）'
        : '（转写开始后，引擎逐词输出的文字会实时出现在这里）');
      if(near) stm.scrollTop = stm.scrollHeight;
    }
    set('lv-chars', d.stream_chars ? d.stream_chars+' 字' : '');
    set('lv-note', (d.item && d.item_percent > 3 && !d.stream)
        ? '⏳ 引擎加载中 / 首个分片识别中…' : '');
    // 组件指示灯（stage1/stage2 跑完会自行收工退出 → 无待办时显示灰色"已收工"而非红色）
    var idle = d.total > 0 && d.done >= d.total;
    function dot(id, on){ var e=document.getElementById(id);
      if(e) e.className = 'dot' + (on ? ' on' : (idle ? '' : ' off')); }
    dot('d-s1', d.stage1); dot('d-s2', d.stage2); dot('d-asr', d.asr);
    set('t-s1', (!d.stage1 && idle) ? ' 已收工' : '');
    set('t-s2', (!d.stage2 && idle) ? ' 已收工' : '');
    var dn=document.getElementById('s-done');
    if(dn) dn.innerHTML=d.done+'<span style="font-size:15px;color:#6b7280"> / '+d.total+'</span>';
    var pct=d.total?(d.done/d.total*100):0;
    var bb=document.getElementById('s-bar'); if(bb) bb.style.width=pct.toFixed(1)+'%';
    var pp=document.getElementById('s-pct'); if(pp) pp.textContent='已完成 '+pct.toFixed(1)+'%';
  }).catch(function(){});
}
lvTick(); setInterval(lvTick, 1200);

/* ── 视频下载进度：每 5s 拉一次 ── */
function esc(s){ return String(s==null?'':s).replace(/[&<>"]/g,function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }
function fsz(n){ n=+n||0; var u=['B','KB','MB','GB'];
  for(var i=0;i<u.length;i++){ if(n<1024) return (i<2 ? Math.round(n)+u[i] : n.toFixed(2)+u[i]); n/=1024; }
  return n.toFixed(2)+'TB'; }
function dlDur(s){ s=Math.max(0,Math.round(s||0));
  return Math.floor(s/3600)+':'+String(Math.floor(s%3600/60)).padStart(2,'0'); }
function dlTick(){
  fetch('/api/download').then(function(r){return r.json()}).then(function(d){
    var set=function(id,v){var e=document.getElementById(id); if(e)e.textContent=v;};
    set('dl-badge','已下载 '+d.downloaded+' / '+d.effective_total+' 条');
    set('dl-pend', d.pending ? ('剩余 '+d.pending+' 条 · 约 '+(d.pending_sec/3600).toFixed(1)+' h') : '已下完');
    var sk=document.getElementById('dl-skip');
    if(sk){ if(d.skipped){ sk.style.display=''; sk.textContent='永久跳过 '+d.skipped; }
            else { sk.style.display='none'; } }
    set('dl-clock','更新于 '+d.ts);
    var b=document.getElementById('dl-bar'); if(b) b.style.width=d.percent+'%';
    set('dl-line','总进度 '+d.percent+'% · 音轨就绪 '+d.audio+' · 封面 '+d.cover+' · 占用 '+fsz(d.bytes));
    var fly=document.getElementById('dl-fly');
    if(d.downloading && d.downloading.length){
      fly.style.display='';
      set('dl-fly-n',d.downloading.length); set('dl-fly-b',fsz(d.downloading_bytes));
      document.getElementById('dl-fly-body').innerHTML = d.downloading.map(function(x){
        var mb = fsz(x.size) + (x.speed ? (' · '+(x.speed/1048576).toFixed(1)+' MB/s') : '')
               + (x.dur ? (' · 时长 '+dlDur(x.dur)) : '');
        return '<div class="dl-fi"><span class="dl-run"><i></i></span><span class="t">'
          + esc(x.title||x.file)+'</span><span class="mb">'
          + (x.account ? ('<span class="dl-acct">'+esc(x.account)+'</span> ') : '')+mb+'</span></div>';
      }).join('');
    } else {
      fly.style.display='none';
      document.getElementById('dl-fly-body').innerHTML='';
    }
    var st=document.getElementById('dl-stall');
    if(st){
      if(d.stalled && d.stalled.length){
        st.style.display='';
        set('dl-stall-n', d.stalled.length);
        set('dl-stall-list', d.stalled.map(function(x){
          return x.account+' · '+(x.title||x.file)+'（'+fsz(x.size)+'）'; }).join('；'));
      } else { st.style.display='none'; }
    }
    var tb=document.getElementById('dl-tb');
    if(tb) tb.innerHTML = d.accounts.map(function(a){
      var bar='<div class="bar" style="margin:2px 0 0"><i style="width:'+a.percent+'%'
        + (a.percent ? '' : ';background:#cbd5e1')+'"></i></div>'
        + '<div class="muted" style="font-size:12px">'+a.percent+'%</div>';
      var sub=a.total+' 条'+(a.pending ? (' · 音轨 '+a.audio+' · 封面 '+a.cover
        +(a.cover_only ? (' · 仅封面 '+a.cover_only) : '')) : '')
        +(a.skipped ? (' · 永久跳过 '+a.skipped) : '');
      var rem=a.pending ? (a.pending+' 条 · '+(a.pending_sec/3600).toFixed(1)+' h') : '✅ 完成';
      return '<tr'+(a.pending?' class="dl-pend"':'')+'><td><b>'+esc(a.label)+'</b>'
        + (a.kind==='合集' ? ' <span class="dl-acct">合集</span>' : '')
        + '<div class="muted" style="font-size:12px">'+esc(sub)+'</div></td>'
        + '<td>'+bar+'</td><td class="muted">'+a.downloaded+' / '+a.effective_total+'</td>'
        + '<td class="muted">'+esc(rem)+'</td><td class="muted">'+fsz(a.bytes)+'</td></tr>';
    }).join('');
  }).catch(function(){});
}
dlTick(); setInterval(dlTick, 5000);
</script>
"""

AUTH_BODY = """
<div class="crumb"><a href="/">← 返回看板</a></div>
<div class="card">
  <h2>{{ name }} <span class="badge">{{ docs|length }} 篇</span>
      <span class="badge">{{ '{:,}'.format(a.chars) }} 字</span>
      <span class="badge">{{ '%.1f'|format(a.dur/3600) }} 小时音频</span></h2>
  <input class="q" id="filter" placeholder="在本主播内筛选标题 / 关键词…">
</div>
<div class="card">
 <table id="tbl"><thead><tr>
   <th style="width:106px">发布日期</th><th>标题</th>
   <th style="width:212px">关键词</th><th style="width:76px">时长</th><th style="width:74px">字数</th>
 </tr></thead><tbody>
 {% for d in docs %}
  <tr data-s="{{ (d.title ~ ' ' ~ (d.keywords|join(' ')))|lower }}">
   <td class="muted">{{ d.pub or '—' }}</td>
   <td><a href="/doc/{{ name }}/{{ d.stem }}">{{ d.title }}</a>
     {% if not d.has_summary %}<span class="tag gray" style="margin-left:6px">无摘要</span>{% endif %}</td>
   <td><div class="tags">{% for k in d.keywords[:4] %}<span class="tag">{{ k }}</span>{% endfor %}</div></td>
   <td class="muted">{% if d.dur %}{{ '%d:%02d'|format((d.dur|int)//60, (d.dur|int)%60) }}{% else %}—{% endif %}</td>
   <td class="muted">{{ '{:,}'.format(d.chars) }}</td>
  </tr>
 {% endfor %}
 </tbody></table>
 <div class="empty" id="nores" style="display:none">没有匹配的条目</div>
</div>
"""

AUTH_SCRIPT = """
<script>
(function(){
  var f=document.getElementById('filter');
  var rows=[].slice.call(document.querySelectorAll('#tbl tbody tr'));
  f.addEventListener('input', function(){
    var q=(f.value||'').trim().toLowerCase(), n=0;
    rows.forEach(function(r){
      var hit=!q || r.getAttribute('data-s').indexOf(q)>=0;
      r.style.display=hit?'':'none'; if(hit) n++;
    });
    document.getElementById('nores').style.display = n?'none':'';
  });
})();
</script>
"""

DOC_BODY = """
<div class="crumb"><a href="/">看板</a> / <a href="/author/{{ name }}">{{ name }}</a> / 逐字稿</div>
<div class="card">
 <h1 class="dt">{{ f.get('视频标题') or stem }}</h1>
 <div class="dochead">
   {% if has_cover %}<img class="cover" src="/media/{{ name }}/{{ cover }}" alt="封面" loading="lazy">{% endif %}
   <div class="meta">
     <div class="kv"><span class="k">作者</span><span>{{ f.get('作者','—') }}</span></div>
     <div class="kv"><span class="k">抖音账号</span><span>{{ f.get('抖音账号','—') }}</span></div>
     <div class="kv"><span class="k">发布时间</span><span>{{ f.get('发布时间','—') }}</span></div>
     <div class="kv"><span class="k">时长</span><span>{% if d.dur %}
       {{ '%d:%02d'|format((d.dur|int)//60, (d.dur|int)%60) }}{% else %}—{% endif %}
       &nbsp;·&nbsp; 逐字稿 {{ '{:,}'.format(d.chars) }} 字</span></div>
     <div class="kv"><span class="k">关键词</span>
       <span class="tags">{% for k in kws %}<span class="tag">{{ k }}</span>{% endfor %}</span></div>
     {% if vid %}<div style="margin-top:13px">
       <a class="btn ghost" href="https://www.douyin.com/video/{{ vid }}" target="_blank" rel="noopener">在抖音打开 ↗</a>
       <span class="muted" style="margin-left:10px">作品ID {{ vid }}</span></div>{% endif %}
   </div>
 </div>
</div>

{% if summary_html %}<div class="card"><h2>📌 视频内容总结</h2><div class="md">{{ summary_html|safe }}</div></div>{% endif %}

<div class="card"><h2>📝 逐字稿 <span class="badge">{{ '{:,}'.format(d.chars) }} 字</span></h2>
<div class="trans">{{ trans_html|safe }}</div></div>

<div class="pager">
  <div>{% if prev_doc %}<a class="btn ghost" href="/doc/{{ name }}/{{ prev_doc.stem }}">← 更早 {{ prev_doc.pub }}</a>{% endif %}</div>
  <div>{% if next_doc %}<a class="btn ghost" href="/doc/{{ name }}/{{ next_doc.stem }}">更新 {{ next_doc.pub }} →</a>{% endif %}</div>
</div>
"""

DOC_SCRIPT = """
<script>
(function(){
  var b=document.createElement('a');
  b.href='#'; b.textContent='↑ 顶部';
  b.style.cssText='position:fixed;right:22px;bottom:26px;display:none;padding:9px 14px;border-radius:999px;'
    +'background:#2563eb;color:#fff;font-size:13px;box-shadow:0 4px 14px rgba(37,99,235,.3);z-index:9';
  document.body.appendChild(b);
  window.addEventListener('scroll', function(){ b.style.display = window.scrollY>600?'block':'none'; });
})();
</script>
"""

SEARCH_BODY = """
<div class="card">
 <h2>🔍 搜索逐字稿</h2>
 <form method="get" action="/search" style="display:flex;gap:10px;flex-wrap:wrap;align-items:center">
   <input class="q" name="q" value="{{ q }}" placeholder="标题 / 关键词 / 全文…" autofocus>
   <label class="muted" style="display:flex;align-items:center;gap:6px">
     <input type="checkbox" name="full" value="1" {{ 'checked' if fulltext }}> 同时搜全文</label>
   <button class="btn" type="submit">搜索</button>
 </form>
 {% if q %}<div class="muted" style="margin-top:10px">找到 {{ results|length }} 条{% if results|length >= 300 %}（仅显示前 300 条）{% endif %}</div>{% endif %}
</div>
{% if results %}
<div class="card"><table><thead><tr>
  <th style="width:106px">发布日期</th><th style="width:132px">作者</th><th>标题</th>
</tr></thead><tbody>
{% for d, s in results %}
 <tr><td class="muted">{{ d.pub or '—' }}</td>
  <td><a href="/author/{{ d.author }}">{{ d.author }}</a></td>
  <td><a href="/doc/{{ d.author }}/{{ d.stem }}">{{ d.title }}</a>
    {% if s %}<div class="muted">{{ s }}</div>{% endif %}</td></tr>
{% endfor %}
</tbody></table></div>
{% elif q %}<div class="card"><div class="empty">没有匹配结果</div></div>{% endif %}
"""


# ────────────────────────── 路由 ──────────────────────────

@app.route("/")
def page_home():
    # 首屏直接把实时数据服务端渲染出来（避免 JS 拉取前的"加载中"闪烁），
    # 之后由 /api/live 每 1.2s、/api/download 每 5s 接管刷新。
    return page("媒体知识库 · 转写进度", HOME_BODY, HOME_SCRIPT,
                p=progress_data(), l=live_state(), dl=download_state())


@app.route("/author/<name>")
def page_author(name):
    c = build_index()
    docs = [d for d in c["docs"] if d["author"] == name]
    if not docs:
        abort(404)
    a = c["authors"].get(name, {"name": name, "docs": len(docs), "chars": 0, "dur": 0})
    return page(f"{name} · 逐字稿", AUTH_BODY, AUTH_SCRIPT, name=name, docs=docs, a=a)


@app.route("/doc/<name>/<stem>")
def page_doc(name, stem):
    ap = os.path.join(OUT_ROOT, name)
    fp = os.path.join(ap, stem + ".md")
    if not os.path.isfile(fp):
        abort(404)
    txt = open(fp, encoding="utf-8", errors="replace").read()
    fields, summary_md, trans = parse_md_text(txt)

    # md 的总结块自带一行 `## 视频内容总结`，而卡片标题已经是它了 → 去掉避免重复
    summary_md = re.sub(r"^\s*#{1,6}\s*视频内容总结\s*$", "", summary_md,
                        count=1, flags=re.M).strip()
    summary_html = mdlib.markdown(summary_md, extensions=["extra", "sane_lists"]) if summary_md else ""
    paras = split_paras(trans)
    trans_html = "".join(f"<p>{H.escape(p)}</p>" for p in paras)

    c = build_index()
    same = [d for d in c["docs"] if d["author"] == name]
    idx = next((i for i, d in enumerate(same) if d["stem"] == stem), None)
    prev_doc = same[idx + 1] if idx is not None and idx + 1 < len(same) else None
    next_doc = same[idx - 1] if idx is not None and idx > 0 else None
    d0 = next((d for d in same if d["stem"] == stem), {"dur": 0, "chars": len(trans)})

    m = VID_RE.search(fields.get("作品ID", ""))
    vid = m.group(1) if m else ""
    mc = COVER_RE.search(fields.get("视频封面", ""))
    cover = mc.group(1).strip() if mc else ""
    kws = [k.strip() for k in re.split(r"[、,，]", fields.get("关键词", "")) if k.strip()]

    return page((fields.get("视频标题") or stem)[:40], DOC_BODY, DOC_SCRIPT,
                name=name, stem=stem, f=fields, vid=vid, cover=cover,
                has_cover=bool(cover) and os.path.exists(os.path.join(ap, cover)),
                summary_html=summary_html, trans_html=trans_html, kws=kws,
                d=d0, prev_doc=prev_doc, next_doc=next_doc)


@app.route("/search")
def page_search():
    q = (request.args.get("q") or "").strip()
    fulltext = request.args.get("full") == "1"
    results = []
    if q:
        c = build_index()
        ql = q.lower()
        for d in c["docs"]:
            hit = ql in (d["title"] or "").lower() or any(ql in k.lower() for k in d["keywords"])
            snip = ""
            if not hit and fulltext:
                try:
                    t = open(os.path.join(OUT_ROOT, d["author"], d["file"]),
                             encoding="utf-8", errors="replace").read()
                    i = t.lower().find(ql)
                    if i >= 0:
                        hit = True
                        snip = "…" + t[max(0, i - 60):i + 130].replace("\n", " ") + "…"
                except Exception:
                    pass
            if hit:
                results.append((d, snip))
            if len(results) >= 300:
                break
    return page("搜索逐字稿", SEARCH_BODY, q=q, fulltext=fulltext, results=results)


@app.route("/media/<name>/<path:fname>")
def media(name, fname):
    ap = os.path.join(OUT_ROOT, name)
    if not os.path.isdir(ap) or "/" in fname or "\\" in fname or ".." in fname:
        abort(404)
    if not os.path.isfile(os.path.join(ap, fname)):
        abort(404)
    return send_from_directory(ap, fname)


@app.route("/api/progress")
def api_progress():
    return jsonify(progress_data())


@app.route("/api/live")
def api_live():
    """实时状态：当前转写条目 + 真实进度 + 引擎逐词输出。首页每 1.2s 轮询一次。"""
    return jsonify(live_state())


@app.route("/api/download")
def api_download():
    """视频下载进度：按账号聚合「已下载 / 作品总数」+ 正在下载的半成品。首页每 5s 轮询。

    全量扫描（含巫师财经 18MB CSV）实测 ≈0.3s，结果按 WB_DL_TTL(20s) 缓存；
    「正在下载」那一块不吃缓存，每次都是实时值。
    """
    return jsonify(download_state())


@app.route("/api/refresh")
def api_refresh():
    build_index(force=True)
    return jsonify({"ok": True})


@app.route("/healthz")
def healthz():
    return "ok", 200


@app.route("/api/quit", methods=["POST"])
def api_quit():
    """让看板**自己退出**（仅用于重启生效）；只允许本机调用。

    ⚠️ 为什么需要它：WorkBuddy 会话把 Console 子进程放在**同一个 job object** 里，
    从外部 `taskkill` 任何一个会**连坐**干掉同一 job 里的其他长跑进程 ——
    实测 2026-09-28：为了重启看板 taskkill 掉 WebUI，正在跑的 stage1（pid 20096）
    一起消失，跑批白停 1 小时（8766 因为跑在 Services 会话里才没受影响）。
    让 WebUI **自己 `os._exit`** 不触发 job 清理 → 不再连坐。
    """
    if request.remote_addr not in ("127.0.0.1", "::1"):
        return jsonify({"ok": False, "why": "仅限本机调用"}), 403
    threading.Timer(0.3, lambda: os._exit(0)).start()
    return jsonify({"ok": True, "hint": "看板将在 0.3s 后退出，用 run_in_background 重新拉起"})


def _lan_urls(port):
    """列出本机可用的访问地址（排除回环/链路本地）。

    本机有两块网卡（以太网 192.168.0.9 / WLAN 192.168.0.8），不列出来很容易拿错 IP —— 
    而且局域网能否访问除了监听 0.0.0.0，*还必须*有防火墙入站规则放行该端口
    （见 README 备注：规则名 'Video Analyzer 8770'）。
    """
    import socket
    first = ("127.0.0.1", None)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))          # 不发包，只为让内核选出口地址
        first = (s.getsockname()[0], "主")
        s.close()
    except Exception:
        pass
    ips = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip.startswith(("127.", "169.254.")) or ip in ips:
                continue
            ips.append(ip)
    except Exception:
        pass
    if first[0] not in ips:
        ips.insert(0, first[0])
    return [(ip, f"http://{ip}:{port}/") for ip in ips]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("WB_UI_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("WB_UI_PORT", "8770")))
    a = ap.parse_args()

    if not B.acquire_lock("webui"):
        print("[lock] 已有 webui 在运行，本次退出", flush=True)
        raise SystemExit(0)
    try:
        print(f"[webui] 本机   http://127.0.0.1:{a.port}/", flush=True)
        for ip, url in _lan_urls(a.port):
            print(f"[webui] 局域网 {url}   ({ip})", flush=True)
        print("[webui] 局域网打不开先查防火墙入站规则 'Video Analyzer %d'" % a.port, flush=True)
        app.run(host=a.host, port=a.port, threaded=True, debug=False, use_reloader=False)
    finally:
        B.release_lock("webui")


if __name__ == "__main__":
    main()
