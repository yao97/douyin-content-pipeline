# -*- coding: utf-8 -*-
"""
把「已落盘的作品」ID 写回工具 DB（Volume/DouK-Downloader.db 的 download_data），
使增量任务跳过，不再重复下载。

为什么需要：
  工具的 is_skip() = is_downloaded(ID) OR is_exists(工具自己的目标路径)
  —— 目标路径是 `{create_time}-视频-{昵称}-{描述}.mp4`，所以库里已有 .m4a 的作品
     两条都不命中，会被当成"没下过"重新下视频。
  把 ID 写进 download_data 后即命中 is_downloaded，永久跳过。
  与 _repair.py 相容：_repair.py 判定"落盘"时接受 .m4a（VIDEO_EXT），
  所以不会把这些记录当"幽灵记录"删掉。

用法：
  python _seed_done.py                      # dry-run：只统计
  python _seed_done.py --apply              # 写入
  python _seed_done.py --apply --folder UID3303366684053592_魏远麟律师 广州_发布作品
"""
import argparse
import csv
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(r"D:\视频\自媒体视频库")
DATA = ROOT / "Data"
DB = ROOT / "_tools" / "TikTokDownloader" / "Volume" / "DouK-Downloader.db"

VIDEO_EXT = ("*.mp4", "*.m4a")
IMAGE_EXT = ("*.jpeg", "*.jpg", "*.webp", "*.png")


def landed(folder: pathlib.Path, nick: str, wid: str, ts: str, kind: str) -> bool:
    """该作品是否已有落盘文件（标准名 或 工具原始名）"""
    exts = IMAGE_EXT if kind in ("图集", "实况") else VIDEO_EXT
    pats = (f"{ts[:10]}_{nick}_{wid}", f"{ts.replace(':', '.')}-")
    for stem in pats:
        for ext in exts:
            if any(folder.glob(stem + ext)):
                return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--folder", nargs="*", help="只处理指定目录名（可多个）")
    args = ap.parse_args()

    expect = {}   # wid -> (folder, nick, ts, kind)
    for csvp in sorted(DATA.glob("*.csv")):
        folder = ROOT / csvp.stem
        if not folder.exists():
            continue
        if args.folder and folder.name not in args.folder:
            continue
        with csvp.open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                wid = (row.get("作品ID") or "").strip()
                ts = (row.get("发布时间") or "").strip()
                nick = (row.get("账号昵称") or "").strip()
                kind = (row.get("作品类型") or "").strip()
                if wid and ts:
                    expect[wid] = (folder, nick, ts, kind)

    done = {w: v for w, v in expect.items()
            if landed(v[0], v[1], w, v[2], v[3])}
    print(f"CSV 作品 {len(expect)} 个；已落盘 {len(done)} 个")

    con = sqlite3.connect(DB)
    try:
        recorded = {r[0] for r in con.execute("SELECT ID FROM download_data")}
    finally:
        con.close()

    need = sorted(set(done) - recorded)
    print(f"库里有记录 {len(recorded)} 个；已落盘但无记录（会被重复下载） {len(need)} 个")
    for w in need[:15]:
        f, n, t, k = done[w]
        print(f"  [补记录] {w}  {f.name[:36]}... {t[:10]} {k}")
    if len(need) > 15:
        print(f"  ... 其余 {len(need)-15} 个")

    if not need:
        print("无需补记录。")
        return 0
    if not args.apply:
        print("\n(dry-run) 加 --apply 写入")
        return 0

    con = sqlite3.connect(DB)
    try:
        con.executemany("INSERT OR REPLACE INTO download_data (ID) VALUES (?)",
                        [(w,) for w in need])
        con.commit()
        total = con.execute("SELECT COUNT(*) FROM download_data").fetchone()[0]
    finally:
        con.close()
    print(f"已写入 {len(need)} 条，download_data 现有 {total} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
