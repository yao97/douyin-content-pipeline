# -*- coding: utf-8 -*-
"""
封面审计+补齐（通用版）：
  以 Data/*.csv 为准，检查每个作品是否有对应封面图（{date}_{nick}_{id} 前缀任意图片）；
  缺失的依次尝试：CSV 静态封面 → CSV 动态封面 → Detail 接口 images → Detail 接口 video.cover
命名：单图 {date}_{nick}_{id}.jpeg；多图 {date}_{nick}_{id}_{n}.jpeg
用法：python _cover_audit_fix.py [--check]   # --check 只报告不下载
"""
import asyncio, csv, pathlib, shutil, sys, urllib.request
sys.path.insert(0, r"D:\视频\自媒体视频库\_tools\TikTokDownloader")
sys.stdout.reconfigure(encoding="utf-8")

ROOT = pathlib.Path(r"D:\视频\自媒体视频库")
IMG = {".jpeg", ".jpg", ".webp", ".png"}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
CHECK_ONLY = "--check" in sys.argv


def fetch(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://www.douyin.com/"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r, dest.open("wb") as f:
            shutil.copyfileobj(r, f)
        if dest.stat().st_size > 3000:
            return True
        dest.unlink(missing_ok=True)
    except Exception:
        pass
    return False


def load_works():
    """返回 [(folder, nick, date, wid, kind, 静态封面, 动态封面, 完整发布时间)]"""
    out = []
    for csvp in sorted((ROOT / "Data").glob("*.csv")):
        folder = ROOT / csvp.stem
        if not folder.exists():
            continue
        nick = folder.stem.split("_", 1)[1].rsplit("_", 1)[0]
        seen = set()
        with csvp.open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                wid = (row.get("作品ID") or "").strip()
                ts = (row.get("发布时间") or "").strip()
                if not wid or not ts or wid in seen:
                    continue
                seen.add(wid)
                out.append((folder, nick, ts[:10], wid, (row.get("作品类型") or "").strip(),
                            (row.get("静态封面") or "").strip(), (row.get("动态封面") or "").strip(),
                            ts))
    return out


async def api_detail(wid):
    import logging
    logging.disable(logging.CRITICAL)
    from src.testers import Params
    from src.interface.detail import Detail
    import _tk_auth
    async with Params() as p:
        await _tk_auth.inject(p, verbose=False)  # tester 缺 uifid/msToken 会被风控
        return await Detail(p, detail_id=wid).run()


def g(obj, *keys):
    for k in keys:
        if obj is None:
            return None
        obj = obj.get(k) if isinstance(obj, dict) else getattr(obj, k, None)
    return obj


async def main():
    works = load_works()
    missing, fixed, failed = [], 0, []
    for folder, nick, date, wid, kind, st, dy, ts in works:
        stem = f"{date}_{nick}_{wid}"
        cvs = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMG]
        # 命中条件（任一）：
        #   a) 已改名封面 {date}_{nick}_{id}[_N].*
        #   b) 工具原始命名封面 {发布时间冒号换点}-...（合集 MID_ 目录不走 _rename.py，
        #      必须算命中，否则每轮都误报"缺封面"并重复下载）
        if any(p.name.startswith(stem) for p in cvs):
            continue
        if any(p.stem.startswith(ts.replace(":", ".")) for p in cvs):
            continue
        missing.append((stem, kind))
        if CHECK_ONLY:
            continue
        dest = folder / f"{stem}.jpeg"
        done = False
        for url in (st, dy):
            if url and fetch(url, dest):
                done = True
                break
        if not done:
            try:
                d = await api_detail(wid)
            except Exception as e:
                print(f"  [{stem}] 接口失败 {type(e).__name__}")
                d = None
            if d is not None:
                imgs = g(d, "images") or []
                for n, it in enumerate(imgs[:20], 1):
                    urls = g(it, "url_list") or []
                    if not urls:
                        continue
                    name = f"{stem}.jpeg" if len(imgs) == 1 else f"{stem}_{n}.jpeg"
                    if fetch(urls[0], folder / name):
                        done = True
                if not done:
                    cov = (g(d, "video", "cover", "url_list")
                           or g(d, "video", "origin_cover", "url_list") or [])
                    if cov and fetch(cov[0], dest):
                        done = True
        if done:
            fixed += 1
            print(f"  ✔ {stem}")
        else:
            failed.append(stem)

    print(f"\n作品总数 {len(works)}；缺封面 {len(missing)}；本次补齐 {fixed}；仍失败 {len(failed)}")
    for s in failed:
        print("  ✘", s)

    # 第二遍：只有 _N 后缀图（图集/实况）、缺标准名 {date}_{nick}_{id}.{ext} 的，用首图复制一份
    copied = 0
    for folder, nick, date, wid, kind, _st, _dy, _ts in works:
        stem = f"{date}_{nick}_{wid}"
        imgs = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMG]
        if any(p.stem == stem for p in imgs):
            continue
        suffixed = sorted(p for p in imgs if p.stem.startswith(stem + "_"))
        if suffixed and not CHECK_ONLY:
            dest = folder / (stem + suffixed[0].suffix)
            shutil.copy2(suffixed[0], dest)
            copied += 1
            print(f"  ＋ 补标准名 {dest.name} (<- {suffixed[0].name})")
        elif suffixed:
            print(f"  [缺标准名] {folder.name}\\{stem}")
    print(f"补标准名封面 {copied} 个")


asyncio.run(main())
