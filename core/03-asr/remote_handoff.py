# -*- coding: utf-8 -*-
"""跨机转写 · 任务交接（外机与本机**不在同一网络**，唯一通道 = 夸克网盘）

为什么是「只交 raw」而不是「交 md」
--------------------------------
`_asr_registry.json` 是增量重跑的唯一真相，**必须只有一个写者**（项目铁律：长跑进程
整表回写会静默抹掉别处的改动）。所以外机**不写注册表、不出 md**，只产
`_asr_raw/<vid>.json`；回传后由本机 `registry` 重建 + stage2 渲 md。
好处：① 天然无并发冲突 ② 关键词 / LLM 摘要 / 封面规范 100% 一致 ③ raw 只有 ~34KB/条。

体积账（2026-09-30 实测：327 条 / 112.1h）
----------------------------------------
- 源（m4a）：**3.68 GB，且已在夸克**（`自媒体视频库/`）→ 外机直接从网盘拉，本机零上传
- 回传 raw：**~11 MB**（327 × 33.9KB）
- 对照：若改「把超长音频推给远端 8766 算」= **12.03 GB**（459 段 × ~27MB）→ 差 400 倍

全流程（三步）
-------------
1. 本机 `python remote_handoff.py export --out _handoff/to_remote`
2. 外机 按清单从夸克拉 m4a + 封面 → clone 仓库 + 部署引擎（见 docs/11-跨机转写.md）
   → `python asr_batch.py stage1`（**只产 raw**）→ 把 `_asr_raw/*.json` 回传夸克
3. 本机 `python remote_handoff.py import <下载目录> --apply`
   = 校验合入 → 用本机源元数据补全（标题/作者/抖音号/封面路径）→ 重建注册表 → 提示拉起 stage2

用法
----
  python remote_handoff.py export [DIR] [--out DIR] [--author 名称] [--limit N]
  python remote_handoff.py import <DIR> [--apply] [--force]
  python remote_handoff.py status

（出口目录 `DIR` 位置写法与 `--out DIR` 等价；两者都不给则落 `<脚本目录>/_handoff/to_remote`。
  `import` 默认**只预演**，必须显式 `--apply` 才落盘；`--force` 覆盖已存在且已转写的条目。）
"""
import os
import sys
import json
import time
import glob

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import asr_batch as B          # noqa: E402
import lib_source              # noqa: E402

MANIFEST = "to_remote.json"
README_MD = "外机待转写清单.md"


# ────────────────────────── 公共 ──────────────────────────

def _todo():
    """本机视角的「待转写」清单（与 stage1 口径完全一致：lib 源 - 注册表 ok）。"""
    return [t for t in B.build_task_list_src() if not B.is_transcribed(t.get("vid"))]


def _cover_of(task):
    """任务的封面路径（lib 源里就是同目录同名图片）。"""
    c = task.get("cover") or ""
    return c if c and os.path.exists(c) else ""


def _rel(path, root):
    """相对路径（清单里一律用相对路径，外机才能映射到自己的根目录）。"""
    try:
        return os.path.relpath(path, root).replace("\\", "/")
    except Exception:
        return os.path.basename(path or "")


def _size(path):
    try:
        return os.path.getsize(path)
    except Exception:
        return 0


# ────────────────────────── export ──────────────────────────

def cmd_export(out_dir, author=None, limit=None):
    todo = _todo()
    if author:
        todo = [t for t in todo if author in (t.get("author") or "")]
    if limit:
        todo = todo[:limit]
    if not todo:
        print("没有待转写条目（库与注册表已对齐）")
        return 0

    root = lib_source.LIB_ROOT
    items, by_author = [], {}
    tot_src = tot_sec = 0
    for t in todo:
        src = t.get("video") or ""
        cov = _cover_of(t)
        sz = _size(src)
        tot_src += sz
        sec = float(t.get("dur_csv") or 0)
        tot_sec += sec
        it = {
            "vid": str(t.get("vid") or ""),
            "author": t.get("author") or "",
            "pub": t.get("pub") or "",
            "dur_sec": round(sec, 1),
            "src_rel": _rel(src, root),          # 相对 WB_LIB_ROOT，外机映射到自己的根
            "src_name": os.path.basename(src),
            "size": sz,
            "cover_rel": _rel(cov, root) if cov else "",
            "cover_name": os.path.basename(cov) if cov else "",
        }
        items.append(it)
        by_author.setdefault(it["author"], []).append(it)
    items.sort(key=lambda x: (x["author"], x["vid"]))

    os.makedirs(out_dir, exist_ok=True)
    payload = {
        "schema": 1,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "generated_by": os.environ.get("COMPUTERNAME", "?"),
        "lib_root_local": root,                  # 仅供参考，外机用自己的根
        "note": "外机只需产出 _asr_raw/<vid>.json（不要写注册表、不要出 md），"
                "回传后由本机 registry 重建 + stage2 渲染。",
        "count": len(items),
        "total_sec": round(tot_sec, 1),
        "total_bytes": tot_src,
        "items": items,
    }
    mpath = os.path.join(out_dir, MANIFEST)
    with open(mpath, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)

    lines = [
        "# 外机待转写清单",
        "",
        "> 由本机 `remote_handoff.py export` 生成于 %s。**共 %d 条 / %.1f 小时 / 源文件 %.2f GB**。" % (
            payload["generated_at"], len(items), tot_sec / 3600, tot_src / 2 ** 30),
        "",
        "## 外机要做的三件事",
        "",
        "1. **拉源**：按 `%s` 里的 `src_rel` / `cover_rel`（相对库根），从夸克网盘" % MANIFEST,
        "   `自媒体视频库/` 下拉同名 m4a 与封面，放到外机自己的库根下（保持同目录结构）。",
        "2. **跑转写**：`python asr_batch.py stage1`（长视频会自动切片），**只产 raw**。",
        "   ⚠️ 不要跑 `registry`、不要跑 `stage2` —— 注册表与 md 的写者只能是本机。",
        "3. **回传**：把外机 `_asr_raw/` 里**新增**的 `<vid>.json` 打包传回夸克。",
        "   （只有 ~%.0f MB —— raw 平均 34KB/条，别传 wav / 切片临时目录）"
        % (len(items) * 34 / 1024.0),
        "",
        "## 分作者",
        "",
        "| 作者 | 条数 | 音频时长 | 源体积 |",
        "| --- | ---: | ---: | ---: |",
    ]
    for a, arr in sorted(by_author.items(), key=lambda kv: -len(kv[1])):
        lines.append("| %s | %d | %.1f h | %.2f GB |" % (
            a, len(arr), sum(x["dur_sec"] for x in arr) / 3600,
            sum(x["size"] for x in arr) / 2 ** 30))
    lines += ["", "## 明细", "", "| # | 作品ID | 作者 | 时长(min) | 源文件 | 封面 |", "| ---: | --- | --- | ---: | --- | --- |"]
    for i, it in enumerate(items, 1):
        lines.append("| %d | %s | %s | %.1f | %s | %s |" % (
            i, it["vid"], it["author"], it["dur_sec"] / 60,
            it["src_name"] or "(缺)", it["cover_name"] or "-"))
    rpath = os.path.join(out_dir, README_MD)
    with open(rpath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print("导出完成：%d 条 / %.1f h / 源 %.2f GB" % (len(items), tot_sec / 3600, tot_src / 2 ** 30))
    print("  清单   : %s" % mpath)
    print("  说明书 : %s" % rpath)
    print("  回传预计：raw 约 %.1f MB" % (len(items) * 34 / 1024.0))
    return 0


# ────────────────────────── import ──────────────────────────

def _find_raw_files(src_dir):
    """目录（可嵌套）里所有像转写结果的 json。"""
    out = []
    for p in glob.glob(os.path.join(src_dir, "**", "*.json"), recursive=True):
        base = os.path.basename(p)
        if base in (MANIFEST,) or base.startswith("_"):
            continue
        out.append(p)
    return sorted(out)


def cmd_import(src_dir, apply=False, force=False):
    if not os.path.isdir(src_dir):
        print("目录不存在: %s" % src_dir)
        return 2
    files = _find_raw_files(src_dir)
    if not files:
        print("在 %s 里没找到任何 .json（应指向解压后的 _asr_raw 目录）" % src_dir)
        return 2

    # 本机源元数据（用本机完整 Data/*.csv + facts.json 补全标题/作者/抖音号/封面）
    meta = {}
    for t in B.build_task_list_src():
        meta[str(t.get("vid"))] = t

    new, dup, bad = [], [], []
    for p in files:
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception as e:
            bad.append((os.path.basename(p), "JSON 解析失败: %s" % e))
            continue
        vid = str(d.get("vid") or os.path.splitext(os.path.basename(p))[0])
        if not vid.isdigit():
            bad.append((os.path.basename(p), "拿不到作品ID"))
            continue
        ok, why = B.validate_raw(d)
        if not ok:
            bad.append((vid, why))
            continue
        if B.is_transcribed(vid) and not force:
            dup.append(vid)
            continue
        d["vid"] = vid
        # 用本机元数据覆盖（外机可能没有 Data/*.csv / facts.json）
        t = meta.get(vid)
        if t:
            for k in ("author", "pub", "title", "account", "kind"):
                if t.get(k):
                    d[k] = t[k]
            c = _cover_of(t)
            if c:
                d["cover"] = c
        new.append((vid, d, p))

    print("扫描 %d 个 json：新增可用 %d / 本机已转写 %d / 不合格 %d"
          % (len(files), len(new), len(dup), len(bad)))
    for vid, why in bad[:10]:
        print("   ✗ %s: %s" % (vid, why))
    if bad[10:]:
        print("   … 另有 %d 条不合格" % (len(bad[10:])))

    if not apply:
        print("\n（dry-run）加 --apply 才会写入 %s 并重建注册表" % B.RAW_DIR)
        return 0

    n = 0
    for vid, d, p in new:
        dst = os.path.join(B.RAW_DIR, vid + ".json")
        tmp = dst + ".part"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, dst)          # 原子落盘：stage2 轮询时不会读到半截文件
        n += 1
    print("\n合入 %d 条 → %s" % (n, B.RAW_DIR))

    if n:
        B.build_registry()            # 重建注册表（唯一写者 = 本机）
        print("\n下一步（缺一步都不会出 md）：")
        print("  python _stage2_daemon.py    # 渲染 raw → md（关键词 + LLM 摘要 + 同名封面）")
        print("  ⚠️ stage2 若已在跑会自动捡到新 raw，不必手工干预。")
    return 0


# ────────────────────────── status ──────────────────────────

def cmd_status():
    todo = _todo()
    raw = len(glob.glob(os.path.join(B.RAW_DIR, "*.json")))
    ok = sum(1 for m in B.load_registry().values() if m.get("ok"))
    sec = sum(float(t.get("dur_csv") or 0) for t in todo)
    print("本机库任务   : %d" % len(B.build_task_list_src()))
    print("注册表 ok    : %d" % ok)
    print("待转写       : %d 条 / %.1f h" % (len(todo), sec / 3600))
    print("_asr_raw     : %d 个 json" % raw)
    hd = os.path.join(HERE, "_handoff")
    if os.path.isdir(hd):
        for d in sorted(glob.glob(os.path.join(hd, "*"))):
            mf = os.path.join(d, MANIFEST)
            if os.path.exists(mf):
                try:
                    j = json.load(open(mf, encoding="utf-8"))
                    print("最近导出     : %s → %d 条（%s）"
                          % (os.path.basename(d), j.get("count", 0), j.get("generated_at", "")))
                except Exception:
                    pass
    return 0


def main():
    argv = sys.argv[1:]
    mode = argv[0] if argv else "status"
    rest = argv[1:]

    # 严格分词：`--flag value` 的 value 归入 opts，不再漏进 positional
    #（旧实现 `args = [a for a in rest if not a.startswith("--")]` 会把 --out 的值
    #  也当成位置参数，导致 `export <dir>` 的位置写法被**静默忽略** → 写到默认目录）
    positional, flags, opts = [], set(), {}
    i = 0
    while i < len(rest):
        a = rest[i]
        if a.startswith("--"):
            if i + 1 < len(rest) and not rest[i + 1].startswith("--"):
                opts[a] = rest[i + 1]
                i += 2
                continue
            flags.add(a)
        else:
            positional.append(a)
        i += 1
    args = positional

    def opt(name, default=None):
        return opts.get(name, default)

    if mode == "export":
        out = opt("--out") or (args[0] if args else None) \
            or os.path.join(HERE, "_handoff", "to_remote")
        lim = opt("--limit")
        return cmd_export(out, opt("--author"), int(lim) if lim else None)
    if mode == "import":
        if not args:
            print("用法: python remote_handoff.py import <解压后的 _asr_raw 目录> [--apply]")
            return 2
        return cmd_import(args[0], apply=("--apply" in flags), force=("--force" in flags))
    if mode == "status":
        return cmd_status()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
