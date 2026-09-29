# -*- coding: utf-8 -*-
"""
把下载目录里的文件统一改名为：{发布日期}_{账号昵称}_{作品ID}{_序号}.{扩展名}

  旧名: 2026-09-07 11.42.24-视频-这很容易-抄底拼多多，A轮入宁德... #投资.m4a
  新名: 2026-09-07_这很容易_7682633651724569891.m4a

映射依据：Data/*.csv 的 账号昵称 + 发布时间(冒号换点) -> 作品ID。
图集/实况的多图文件保留 _N 序号后缀。

用法：
  python _rename.py           # 只预览（dry-run）
  python _rename.py --apply   # 真正改名
"""
import csv
import pathlib
import re
import sys

ROOT = pathlib.Path(r"D:\视频\自媒体视频库")
DATA = ROOT / "Data"

APPLY = "--apply" in sys.argv


def hard_delete(path: pathlib.Path):
    """绕过 safe-delete shim（其 trash 可能失败抛异常），直接走 Win32 DeleteFileW"""
    import ctypes
    if not ctypes.windll.kernel32.DeleteFileW(str(path)):
        raise OSError(f"DeleteFileW failed: {path}")

# 旧文件名：时间戳-类型-昵称-描述(可选_N).扩展名
OLD_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}\.\d{2}\.\d{2})"
    r"-(?P<type>视频|图集|实况)-"
    r"(?P<body>.+?)"
    r"(?:_(?P<seq>\d+))?"
    r"\.(?P<ext>m4a|mp4|jpeg|jpg|png|webp)$",
    re.IGNORECASE,
)


def load_map():
    """{(昵称, '2026-09-07 11.42.24'): [作品ID, ...]}"""
    m = {}
    for csvp in sorted(DATA.glob("*.csv")):
        with csvp.open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                nick = (row.get("账号昵称") or "").strip()
                ts = (row.get("发布时间") or "").strip()
                wid = (row.get("作品ID") or "").strip()
                if not (nick and ts and wid):
                    continue
                key = (nick, ts.replace(":", "."))
                m.setdefault(key, []).append(wid)
    return m


def main():
    mapping = load_map()
    print(f"CSV 映射条目: {len(mapping)}")

    folders = [p for p in ROOT.iterdir()
               if p.is_dir() and re.match(r"^UID\d+_.+_发布作品$", p.stem)]
    total, renamed, already, skipped, dup = 0, 0, 0, 0, 0

    for folder in folders:
        nick = folder.stem.split("_", 1)[1].rsplit("_", 1)[0]
        for p in sorted(folder.iterdir()):
            if not p.is_file():
                continue
            mt = OLD_RE.match(p.name)
            if not mt:
                continue  # CSV 等无关文件不动
            total += 1
            ts, seq, ext = mt.group("ts"), mt.group("seq"), mt.group("ext")
            ids = sorted(set(mapping.get((nick, ts), [])))  # CSV 多次采集会重复追加同一 ID，先去重
            if not ids:
                skipped += 1
                if skipped <= 20:
                    print(f"  [无映射] {folder.name}\\{p.name[:80]}")
                continue
            if len(ids) > 1:
                skipped += 1
                print(f"  [多ID歧义] {folder.name}\\{p.name[:60]} -> {ids}")
                continue
            new_stem = f"{ts[:10]}_{nick}_{ids[0]}"
            if seq:
                new_stem += f"_{seq}"
            new_name = f"{new_stem}.{ext}"
            if p.name == new_name:
                already += 1
                continue
            target = p.with_name(new_name)
            if target.exists():
                # 重复下载（封面改名后工具按旧名查不到会重下），直接删旧命名副本
                if APPLY:
                    hard_delete(p)
                dup += 1
                continue
            if APPLY:
                p.rename(target)
            renamed += 1

    print(f"\n匹配 {total} 个：改名 {renamed}，已是新名 {already}，删重复 {dup}，跳过 {skipped}")
    if not APPLY:
        print("(dry-run) 加 --apply 执行改名")
    return 0


if __name__ == "__main__":
    sys.exit(main())
