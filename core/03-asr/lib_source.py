# -*- coding: utf-8 -*-
"""自媒体视频库（D:\\视频\\自媒体视频库）作为 ASR 输入的适配层。

与旧源（C:\\Users\\EDY\\Videos\\data\\关注 的 .mp4）不同：
- 音频是 .m4a（已从 mp4 无损转出），封面是同目录的 .jpeg/.webp
- 元数据来自 Data/UID*_发布作品.csv（含作品描述=标题、发布时间、视频时长、作品类型）
- 文件命名已是 {发布日期}_{账号昵称}_{作品ID}.{ext}

产出（md + 封面）仍写入 D:\\视频\\媒体知识库\\<作者>\\，与既有成果合并；
已识别判断沿用 _asr_registry.json（按作品ID），因此天然支持增量。
"""
import os, re, csv, io, json, time

# ⚠️ 路径全部可用环境变量覆盖 —— 为了「同一套代码跑在第二台机器上」（跨机转写）。
#    默认值 = 本机实盘，**行为零变化**；外机只要设 WB_LIB_ROOT / WB_FACTS 即可。
LIB_ROOT = os.environ.get("WB_LIB_ROOT", r"D:\视频\自媒体视频库")
IMG_EXT = (".jpeg", ".jpg", ".png", ".webp")
VID_RE = re.compile(r"(\d{15,})")
# 合集命名：`<YYYY-MM-DD HH.MM.SS>-<类型>-<账号>-<标题>`（**不带作品ID**，只能靠发布时间对号入座）
TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[ _](\d{2}\.\d{2}\.\d{2})")
# 抖音号(uniqueId) 映射来源；外机没有这个文件也能跑 —— 抖音号可由本机 `asr_batch.py syncmeta` 事后补
# （它把源元数据同步进已落盘 raw，**不重转写**），所以跨机时外机只传 raw 回来即可。
FACTS = os.environ.get("WB_FACTS", r"C:\Users\EDY\Videos\data\.appdata\facts.json")
# 仍在写入的文件（mtime 太新）先不碰，避免把"下到一半"的 mp4 当成品去转写
FRESH_SEC = float(os.environ.get("WB_LIB_FRESH_SEC", "120"))

# 目录名 → (展示/落盘用的作者名, 抖音账号)。合集在 CSV 里的「账号昵称」是「播客正片合集」，
# 但那只是合集名；老板确认这个合集的作者是「罗永浩的十字路口」，抖音账号用它主页的 sec_uid。
AUTHOR_OVERRIDE = {
    "MID7545086042492127266_播客正片合集_合集作品": (
        "罗永浩的十字路口",
        "MS4wLjABAAAA8llnXrhiCk_dYhuE-uFpJpXpA0rInu68S0S8gVDdNnY",
    ),
}

_UID_MAP = None


def _uid_map():
    """作者昵称 -> 抖音号(uniqueId)。CSV 只有数字 UID，抖音号需从旧元数据补。"""
    global _UID_MAP
    if _UID_MAP is not None:
        return _UID_MAP
    m = {}
    try:
        d = json.load(open(FACTS, encoding="utf-8"))
        for aid, a in (d.get("authors") or {}).items():
            uids = a.get("uniqueIds") or []
            for n in (a.get("nicknames") or []):
                if n and uids and uids[0]:
                    m[n] = uids[0]
    except Exception:
        pass
    _UID_MAP = m
    return m


def _read_csv(path):
    """读 DouK-Downloader 导出的 CSV，返回 作品ID -> 行字典"""
    rows = {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            vid = (r.get("作品ID") or "").strip()
            if vid:
                rows.setdefault(vid, r)
    return rows


def _hms(s):
    """'00:12:35' -> 秒"""
    try:
        p = [int(x) for x in (s or "").strip().split(":")]
        while len(p) < 3:
            p.insert(0, 0)
        return p[0] * 3600 + p[1] * 60 + p[2]
    except Exception:
        return 0.0


def build_lib_tasks(lib_root=LIB_ROOT):
    """返回与 asr_batch.build_task_list() 同构的 task 列表。

    扫描 `UID*`（账号发布作品）与 `MID*`（合集作品）两类目录 —— 合集目录本来被漏掉了。
    音频文件：优先 `.m4a`；同一作品没有 m4a 时，才用**已稳定**（mtime 够老）的 `.mp4` 兜底。
    作品ID：文件名里有 15+ 位数字就直接用；合集文件名没有 ID，则用 `发布时间` 前缀去 CSV 对号。
    """
    tasks = []
    now = time.time()
    for d in sorted(os.listdir(lib_root)):
        p = os.path.join(lib_root, d)
        if not (os.path.isdir(p) and d.startswith(("UID", "MID"))):
            continue
        csv_path = os.path.join(lib_root, "Data", d + ".csv")
        meta = _read_csv(csv_path) if os.path.exists(csv_path) else {}
        # 发布时间戳 → 作品ID（给合集那种文件名里没 ID 的用）
        ts2vid = {}
        for vid, r in meta.items():
            ts = (r.get("发布时间") or "").strip()
            if ts:
                ts2vid[ts.replace(":", ".")] = vid

        ov_author, ov_account = AUTHOR_OVERRIDE.get(d, ("", ""))
        names = os.listdir(p)
        have_m4a = set()
        for fn in names:
            m = VID_RE.search(fn)
            if fn.lower().endswith(".m4a") and m:
                have_m4a.add(m.group(1))

        for fn in names:
            low = fn.lower()
            if not low.endswith((".m4a", ".mp4")):
                continue
            stem = os.path.splitext(fn)[0]          # 账号：{日期}_{昵称}_{作品ID} / 合集：{日期 时间}-…-{标题}
            try:
                fresh = (now - os.path.getmtime(os.path.join(p, fn))) < FRESH_SEC
            except Exception:
                fresh = False

            m = VID_RE.search(fn)
            vid = m.group(1) if m else ""
            if not vid:
                mt = TS_RE.match(stem)
                if mt:
                    vid = ts2vid.get(f"{mt.group(1)} {mt.group(2)}", "")
            if not vid:
                continue
            if low.endswith(".mp4"):
                if vid in have_m4a:
                    continue          # 已有音轨，别重复排任务
                if fresh:
                    continue          # 还在下/还在抽音轨，等稳定了再来

            parts = stem.split("_")
            pub = parts[0] if parts else "未知日期"

            r = meta.get(vid, {})
            author = (r.get("账号昵称") or "").strip()
            if not author:
                author = parts[1] if len(parts) > 1 else d
            if ov_author:
                author = ov_author
            title = (r.get("作品描述") or "").strip()
            kind = (r.get("作品类型") or "视频").strip()
            # 发布时间优先用 CSV（合集的文件名前缀就是它，等价但 CSV 更权威）
            ct = (r.get("发布时间") or "").strip()
            if ct[:10]:
                pub = ct[:10]
            # 抖音号：优先封面覆盖 → 旧元数据的 uniqueId → CSV 的数字 UID
            account = ov_account or _uid_map().get(author) or (r.get("UID") or "").strip()
            dur = _hms(r.get("视频时长"))

            # 封面：同目录，同名不同扩展名
            cover = ""
            for ext in IMG_EXT:
                c = os.path.join(p, stem + ext)
                if os.path.exists(c):
                    cover = c
                    break

            tasks.append({
                "aid": d, "vid": vid, "video": os.path.join(p, fn),
                "cover": cover, "author": author, "account": account,
                "title": title, "pub": pub, "createTime": None,
                "kind": kind, "dur_csv": dur,
            })
    return tasks


if __name__ == "__main__":
    import collections
    ts = build_lib_tasks()
    print("总 m4a:", len(ts))
    c = collections.Counter(t["author"] for t in ts)
    k = collections.Counter(t["kind"] for t in ts)
    print("按作者:", dict(c))
    print("按类型:", dict(k))
    tot = sum(t["dur_csv"] for t in ts)
    print(f"CSV 时长合计: {tot/3600:.2f} h")
    nz = [t for t in ts if t["dur_csv"] > 0]
    print(f"有时长的: {len(nz)} 条，合计 {sum(t['dur_csv'] for t in nz)/3600:.2f} h")
    img = [t for t in ts if not t["cover"]]
    print("缺封面:", len(img))
    print("样例:", {k2: v[:40] if isinstance(v, str) else v for k2, v in list(ts[0].items())})
