# -*- coding: utf-8 -*-
"""
修复抖音下载记录与实际文件不一致的问题。

原理：TikTokDownloader 的封面和视频共用同一个 aweme_id 写记录（download_file 每下成功
任意一个文件就 update_id），所以「封面成功 + 视频失败/中断」会把作品误标为已下载，
下次运行 is_skip() 命中记录直接跳过，视频永久漏下。

本脚本以 Data/*.csv 为准（CSV 含 作品ID + 发布时间，文件名以 发布时间 开头），
找出「库里有记录但对应 .mp4 不存在」的作品：
  1. 删除 download_data 里这些 ID（下次运行会重新下载）
  2. 删除 Volume/Cache 里这些作品的孤儿半成品（避免续传拼接出损坏文件）

用法：
  python _repair.py           # 只报告，不改任何东西（dry-run）
  python _repair.py --apply   # 真正执行删除

必须在 TikTokDownloader 进程未运行时执行（避免 SQLite 锁冲突）。
"""
import csv
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(r"D:\视频\自媒体视频库")
TOOL = ROOT / "_tools" / "TikTokDownloader"
DB = TOOL / "Volume" / "DouK-Downloader.db"
CACHE = TOOL / "Volume" / "Cache"
DATA = ROOT / "Data"

APPLY = "--apply" in sys.argv

# 源端音频损坏、永久无法下载的作品（见 _skip_ids.json），其库记录不可删除
PERMANENT_SKIP = set()
_skipf = ROOT / "_skip_ids.json"
if _skipf.exists():
    import json
    PERMANENT_SKIP = {x["id"] for x in json.loads(
        _skipf.read_text(encoding="utf-8")).get("ids", [])}


def main():
    if not DB.exists():
        print("DB not found:", DB)
        return 1

    # 1. 从所有账号 CSV 建立 作品ID -> 期望文件前缀 的映射
    #    两种命名都要支持：
    #      a) 已改名（_rename.py 处理过的账号目录）：{发布日期}_{账号昵称}_{作品ID}
    #      b) 工具原始命名（未改名的合集 MID* 目录）：{发布时间冒号换点}-{类型}-...
    #    ⚠ 合集 MID_ 目录的文件不会走 _rename.py，若只按 (a) 判断会被整批误判为"缺失"，
    #      --apply 会删掉全部合集记录 → 下次重下几十 GB。
    expect = {}      # aweme_id -> (改名前缀 Path, 原始命名前缀 Path, 类型)
    ts_prefix = {}   # aweme_id -> 原始命名时间戳前缀（匹配孤儿缓存）
    for csvp in sorted(DATA.glob("*.csv")):
        folder = ROOT / csvp.stem
        with csvp.open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                wid = (row.get("作品ID") or "").strip()
                ts = (row.get("发布时间") or "").strip()
                nick = (row.get("账号昵称") or "").strip()
                kind = (row.get("作品类型") or "").strip()
                if not wid or not ts:
                    continue
                expect[wid] = (
                    folder / f"{ts[:10]}_{nick}_{wid}",
                    folder / f"{ts.replace(':', '.')}-",
                    kind,
                )
                ts_prefix[wid] = ts.replace(":", ".")

    VIDEO_EXT = ("*.mp4", "*.m4a")
    IMAGE_EXT = ("*.jpeg", "*.jpg", "*.webp", "*.png")

    def files_exist(pattern: pathlib.Path, exts) -> bool:
        for ext in exts:
            if any(pattern.parent.glob(pattern.name + ext)):
                return True
        return False

    def files_exist_any_nick(folder: pathlib.Path, wid: str, exts) -> bool:
        """同一作品在 CSV 里可能有两行且「账号昵称」不同（共创 / 引用 / 首作归属变化），
        expect[wid] 只保留最后一行 → 期望前缀与实际文件名不符，会被误判「缺失」，
        --apply 就删掉库记录并重下。这里按「作品ID 唯一」放宽：只要目录下存在
        *_{wid}.<ext> 即视为已落盘（2026-10-03 程前朋友圈 7418113416277101876 实测）。
        """
        for ext in exts:
            if any(folder.glob(f"*_{wid}{ext[1:]}")):
                return True
        return False

    # 2. 逐个判断作品是否真的落盘（视频看 mp4/m4a；图集/实况看图片）
    missing = {}
    for wid, (pat_new, pat_old, kind) in expect.items():
        exts = IMAGE_EXT if kind in ("图集", "实况") else VIDEO_EXT
        if not (
            files_exist(pat_new, exts)
            or files_exist(pat_old, exts)
            or files_exist_any_nick(pat_new.parent, wid, exts)
        ):
            missing[wid] = pat_new

    # 3. 读库
    con = sqlite3.connect(DB)
    try:
        recorded = {r[0] for r in con.execute("SELECT ID FROM download_data")}
    finally:
        con.close()

    stale = sorted((set(missing) & recorded) - PERMANENT_SKIP)
    print(f"CSV 记录作品 {len(expect)} 个（视频+图集+实况）；落盘缺失 {len(missing)} 个；"
          f"其中库里已有记录(会被永久跳过) {len(stale)} 个"
          f"（另有 {len(set(missing) & recorded & PERMANENT_SKIP)} 个永久跳过名单内，不动）")
    for wid in stale:
        print(f"  [缺失] {wid}  期望 {missing[wid].parent.name}\\{missing[wid].name}")

    if not stale:
        print("无需要修复的记录。")
        return 0

    # 4. 找孤儿缓存（与缺失作品旧命名的发布时间前缀相同的半成品）
    orphans = []
    if CACHE.exists():
        for wid in stale:
            pre = ts_prefix.get(wid, "")
            if not pre:
                continue
            for p in CACHE.rglob("*"):
                if p.is_file() and p.name.startswith(pre):
                    orphans.append(p)
    if orphans:
        sz = sum(p.stat().st_size for p in orphans) / 1024 / 1024
        print(f"孤儿缓存 {len(orphans)} 个，共 {sz:.0f} MB：")
        for p in orphans:
            print(f"  [缓存] {p.name[:70]}")

    if not APPLY:
        print("\n(dry-run) 加 --apply 执行修复")
        return 0

    # 5. 执行：删记录 + 删孤儿缓存
    con = sqlite3.connect(DB)
    try:
        con.executemany("DELETE FROM download_data WHERE ID=?", [(w,) for w in stale])
        con.commit()
        print(f"已从 download_data 删除 {len(stale)} 条记录")
    finally:
        con.close()
    for p in orphans:
        try:
            p.unlink()
            print(f"已删除缓存 {p.name[:60]}")
        except OSError as e:
            print(f"删除缓存失败 {p.name[:60]}: {e}")
    print("修复完成，重新运行下载即可补齐缺失视频。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
