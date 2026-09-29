# -*- coding: utf-8 -*-
"""合集核验：CSV 37 条 vs 落盘 mp4，按 发布时间 前缀匹配；输出缺失 ID 并可选清理 DB 幻影记录"""
import csv, re, sqlite3, sys
from pathlib import Path

MID = "7545086042492127266"
WS = Path(r"D:\视频\自媒体视频库")
CSV = WS / "Data" / f"MID{MID}_播客正片合集_合集作品.csv"
FOLDER = WS / f"MID{MID}_播客正片合集_合集作品"
DB = WS / "_tools" / "TikTokDownloader" / "Volume" / "DouK-Downloader.db"

rows = list(csv.reader(open(CSV, encoding="utf-8-sig")))
header = rows[0]
i_id = header.index("作品ID")
i_time = header.index("发布时间")
i_desc = header.index("作品描述")

records = {}  # id -> (time, desc)
for r in rows[1:]:
    if len(r) <= i_time:
        continue
    records.setdefault(r[i_id], (r[i_time], r[i_desc]))

mp4 = list(FOLDER.glob("*.mp4"))
names = [p.name for p in mp4]


def prefix(ts: str) -> str:
    """'2025-10-17 12:08:27' -> '2025-10-17 12.08.27'"""
    d, _, t = ts.partition(" ")
    return f"{d} {t.replace(':', '.')}"


missing = []
for wid, (ts, desc) in records.items():
    p = prefix(ts)
    if not any(n.startswith(p) for n in names):
        missing.append((wid, ts, desc[:30]))

print(f"CSV 去重后作品数: {len(records)}")
print(f"落盘 mp4: {len(mp4)}")
print(f"缺失: {len(missing)}")
for wid, ts, desc in missing:
    print(f"  {wid}  {ts}  {desc}")

if "--apply" in sys.argv and missing:
    con = sqlite3.connect(DB)
    cur = con.cursor()
    ids = [m[0] for m in missing]
    cur.executemany("DELETE FROM download_data WHERE ID = ?", [(i,) for i in ids])
    print(f"已删除 {cur.rowcount if cur.rowcount > 0 else len(ids)} 条幻影记录（实际提交 {len(ids)} 个 ID）")
    con.commit()
    con.close()
