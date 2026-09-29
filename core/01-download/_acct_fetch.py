# -*- coding: utf-8 -*-
"""
账号作品清单采集（只取元数据：不下视频、不入下载库）

用工具自带的 Account 接口拉全量 aweme_list（全部分页），产出：
  1) Data/UID{uid}_{昵称}_发布作品.csv   —— 列结构与工具原生 CSV 完全一致的 30 列
  2) <同名目录>/                        —— 自动建目录
  3) --covers 时下载静态封面：{发布日期}_{昵称}_{作品ID}.{jpeg|webp|png}

存在的意义：_audio_direct.py 是按 Data/*.csv 驱动工作的，先有清单才能"只下音频"。
CSV 已存在时不重复写行（按 作品ID 去重），可反复安全执行。

用法：
  python _acct_fetch.py <sec_uid|主页URL>            # dry-run：只报数量
  python _acct_fetch.py <sec_uid|主页URL> --apply    # 写 CSV + 建目录
  python _acct_fetch.py <sec_uid|主页URL> --apply --covers
"""
import argparse
import asyncio
import csv
import pathlib
import re
import sys
from datetime import datetime

sys.path.insert(0, r"D:\视频\自媒体视频库\_tools\TikTokDownloader")
sys.stdout.reconfigure(encoding="utf-8")

ROOT = pathlib.Path(r"D:\视频\自媒体视频库")
DATA = ROOT / "Data"
SETTINGS = ROOT / "_tools" / "TikTokDownloader" / "Volume" / "settings.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

TITLE = ("作品类型", "采集时间", "UID", "SEC_UID", "ID", "作品ID", "作品描述", "作品话题",
         "视频时长", "视频高度", "视频宽度", "作品链接", "发布时间", "视频URI", "账号昵称",
         "年龄", "账号签名", "下载地址", "音乐作者", "音乐标题", "音乐链接", "静态封面",
         "动态封面", "隐藏标签", "点赞数量", "评论数量", "收藏数量", "分享数量", "播放数量",
         "额外信息")

SEC_RE = re.compile(r"(MS4wLjAB[A-Za-z0-9_\-]+)")


def hard_delete(p: pathlib.Path):
    """绕过 safe-delete shim 直接删除（删临时文件用）"""
    import ctypes
    ctypes.windll.kernel32.DeleteFileW(str(p))


def norm_sec_uid(s: str) -> str:
    m = SEC_RE.search(s or "")
    return m.group(1) if m else (s or "").strip()


def time_conversion(ms: int) -> str:
    second = int(ms or 0) // 1000
    return f"{second // 3600:0>2d}:{second % 3600 // 60:0>2d}:{second % 60:0>2d}"


def classify(a: dict) -> str:
    """与工具一致的 视频/图集/实况 判定"""
    images = a.get("images") or []
    if images:
        return "实况" if any((i or {}).get("video") for i in images) else "图集"
    return "视频"


def first_url(obj) -> str:
    if not isinstance(obj, dict):
        return ""
    urls = obj.get("url_list") or []
    return urls[0] if urls else ""


def to_row(a: dict, cleaner, now: str) -> tuple:
    """aweme dict -> 30 列 CSV 行"""
    vid = str(a.get("aweme_id") or "")
    author = a.get("author") or {}
    video = a.get("video") or {}
    stats = a.get("statistics") or {}
    music = a.get("music") or {}
    nick = cleaner.filter_name(author.get("nickname") or "", "无效账号昵称")
    kind = classify(a)
    ct = int(a.get("create_time") or 0)
    ts = datetime.fromtimestamp(ct).strftime("%Y-%m-%d %H:%M:%S") if ct else ""

    if kind == "视频":
        duration = time_conversion(video.get("duration"))
        height = video.get("height") or -1
        width = video.get("width") or -1
        uri = (video.get("play_addr") or {}).get("uri") or ""
        downloads = first_url(video.get("play_addr"))
    else:  # 图集/实况
        duration, height, width, uri, downloads = "00:00:00", -1, -1, "", ""

    topics = " ".join(
        (i or {}).get("hashtag_name") or ""
        for i in (a.get("text_extra") or [])
    ).strip()
    tags = " ".join(
        (i or {}).get("tag_name") or ""
        for i in (video.get("tag") or [])
    ).strip()

    return (
        kind, now, author.get("uid") or "", author.get("sec_uid") or "", "",
        vid, (a.get("desc") or "").strip() or vid, topics, duration,
        height, width, f"https://www.douyin.com/video/{vid}", ts, uri, nick,
        -1, author.get("signature") or "", downloads,
        music.get("author") or "", music.get("title") or "", first_url(music.get("play_url")),
        first_url(video.get("cover")), first_url(video.get("dynamic_cover")), tags,
        stats.get("digg_count", -1), stats.get("comment_count", -1),
        stats.get("collect_count", -1), stats.get("share_count", -1),
        stats.get("play_count", -1), "",
    )


def read_existing_ids(path: pathlib.Path) -> set:
    if not path.exists() or path.stat().st_size == 0:
        return set()
    ids = set()
    with path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            wid = (row.get("作品ID") or "").strip()
            if wid:
                ids.add(wid)
    return ids


def sniff_ext(data: bytes) -> str:
    """按魔数判断真实图片格式"""
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[8:12] == b"WEBP":
        return "webp"
    if data[:2] == b"BM":
        return "bmp"
    return "jpeg"


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("target", help="sec_uid 或账号主页 URL / 分享链接")
    ap.add_argument("--apply", action="store_true", help="真正写 CSV（默认 dry-run）")
    ap.add_argument("--covers", action="store_true", help="同时下载静态封面")
    ap.add_argument("--nick", default="", help="覆盖昵称（目录/文件名用）")
    args = ap.parse_args()

    sec_uid = norm_sec_uid(args.target)
    if not sec_uid.startswith("MS4wLjAB"):
        print(f"✘ 无法从 {args.target!r} 解析 sec_uid")
        return 2

    import logging
    logging.disable(logging.CRITICAL)
    from src.interface.account import Account
    from src.testers import Params
    from src.tools import Cleaner, create_client

    import _tk_auth

    cleaner = Cleaner()

    async with Params() as params:
        params.max_pages = 99999
        params.timeout = 30
        # tester 的 cookie 可能过期、且缺 uifid/msToken —— 缺了就只放第一页（见 _tk_auth 说明）
        auth = await _tk_auth.inject(params)
        cookie_str = _tk_auth.read_cookie()[0]

        try:  # 默认客户端 timeout 只有 5s，全分页拉取容易超时
            params.client = create_client(
                timeout=params.timeout, proxy=None, impersonate=params.impersonate)
        except Exception as e:
            print(f"[警告] 重建客户端失败，沿用默认: {e}")

        acct = Account(params, sec_user_id=sec_uid, earliest="", pages=99999)
        works, _earliest, _latest = await acct.run()
        print(f"[鉴权] {auth}")

    works = works or []
    print(f"接口返回作品 {len(works)} 个")
    if not works:
        print("✘ 没有取到作品：检查 Cookie 是否失效 / 账号是否私密")
        return 3

    first = works[0]
    author = first.get("author") or {}
    uid = str(author.get("uid") or "")
    nick = args.nick or cleaner.filter_name(author.get("nickname") or "", "无效账号昵称")
    folder_name = f"UID{uid}_{nick}_发布作品"
    folder = ROOT / folder_name
    csv_path = DATA / f"{folder_name}.csv"

    kinds = {}
    for a in works:
        k = classify(a)
        kinds[k] = kinds.get(k, 0) + 1
    print(f"账号：{nick}  uid={uid}")
    print(f"类型分布：{kinds}")
    print(f"目标目录：{folder}")
    print(f"目标 CSV：{csv_path}")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = [to_row(a, cleaner, now) for a in works]

    if not args.apply:
        print("\n(dry-run) 前 3 条：")
        for r in rows[:3]:
            print(f"  {r[0]} {r[5]} {r[12]} 封面={'有' if r[21] else '无'}")
        print("确认后加 --apply 执行")
        return 0

    folder.mkdir(parents=True, exist_ok=True)
    DATA.mkdir(parents=True, exist_ok=True)
    exist = read_existing_ids(csv_path)
    new_rows = [r for r in rows if r[5] not in exist]

    if not csv_path.exists() or csv_path.stat().st_size == 0:
        with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(TITLE)
            w.writerows(new_rows)
    elif new_rows:
        with csv_path.open("a", encoding="utf-8-sig", newline="") as f:
            csv.writer(f).writerows(new_rows)
    print(f"\nCSV：新增 {len(new_rows)} 行（原有 {len(exist)} 条）-> {csv_path.name}")

    if args.covers:
        from curl_cffi import requests
        sess = requests.Session(impersonate="chrome124")
        headers = {"User-Agent": UA, "Referer": "https://www.douyin.com/",
                   "Cookie": cookie_str}
        ok = skip = fail = 0
        for r in rows:
            kind, wid, ts, cover = r[0], r[5], r[12], r[21]
            if not cover or not ts:
                fail += 1
                continue
            stem = f"{ts[:10]}_{nick}_{wid}"
            if any(folder.glob(f"{stem}.*")):
                skip += 1
                continue
            try:
                resp = sess.get(cover, headers=headers, timeout=60, verify=False)
                if resp.status_code != 200 or len(resp.content) < 1024:
                    fail += 1
                    continue
                ext = sniff_ext(resp.content)
                (folder / f"{stem}.{ext}").write_bytes(resp.content)
                ok += 1
                await asyncio.sleep(0.1)
            except Exception as e:
                fail += 1
                print(f"  [封面失败] {wid} {type(e).__name__}: {str(e)[:60]}")
        print(f"封面：新增 {ok}，已存在跳过 {skip}，失败 {fail}")

    print("done")
    return 0


if __name__ == "__main__":
    asyncio.run(main())
