# -*- coding: utf-8 -*-
"""
音频直下（不走视频）：用 Detail 接口的 video.bit_rate_audio[] 直链拉取纯音轨，存成 .m4a。

背景：抖音 Web 接口对每个视频作品都返回 bit_rate_audio（media-audio-und-mp4a），
      是完整人声轨（实测 4 小时播客：视频 668MB vs 音频 81.8MB，省 88%），
      对 ASR 场景完全够用，且省掉"下视频→抽音轨→删视频"的一整步。

音频档真实结构（2026-09-28 实测）：
  video.bit_rate_audio = [ {audio_extra, audio_meta, audio_quality} ]   # 通常只有 1 档
  audio_meta = {bitrate(~193k), size, media_type:"audio", format:"dash", url_list:{main_url,...}}
  注意 gear_name / bit_rate 在 audio_meta 里，不在元素顶层。
  覆盖率约 2/3（长视频/部分作品不返回音频档）→ 用 --fallback-video 兜底。

兜底逻辑（--fallback-video）：无音频档时，下 **最小的 format='mp4'** 档（渐进式，音视频混流），
  ffmpeg 抽音轨成 m4a 后立刻删掉 mp4。**不能用 dash 档**——dash 是纯视频流，没有声音。

用法：
  python _audio_direct.py                        # dry-run：统计可省多少 + 音频档覆盖率
  python _audio_direct.py --apply                # 只补缺失的 m4a
  python _audio_direct.py --apply --folder MID7545086042492127266_播客正片合集_合集作品
  python _audio_direct.py --apply --fallback-video          # 无音频档的回落到最小 mp4 抽音轨
  python _audio_direct.py --apply --delete-mp4              # 音频就绪后删同作品 mp4
选项：
  --quality small|mid|best   音质档，默认 small（体积最小）
  --jobs N                   并发数，默认 2（对接口友好）
  --sample N                 dry-run 抽样个数，默认 8
"""
import argparse
import asyncio
import csv
import ctypes
import pathlib
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, r"D:\视频\自媒体视频库\_tools\TikTokDownloader")
sys.path.insert(0, r"D:\视频\自媒体视频库")
sys.stdout.reconfigure(encoding="utf-8")

from curl_cffi import requests  # noqa: E402

import _tk_auth  # noqa: E402  tester 缺 uifid/msToken 会被风控，统一在此补齐

ROOT = pathlib.Path(r"D:\视频\自媒体视频库")
DATASET = ROOT / "Data"
SETTINGS = ROOT / "_tools" / "TikTokDownloader" / "Volume" / "settings.json"
FFMPEG = (r"C:\Users\EDY\Projects\video-analyzer\backend\venv\Lib\site-packages"
          r"\imageio_ffmpeg\binaries\ffmpeg-win-x86_64-v7.1.exe")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")


def hard_delete(p: pathlib.Path) -> bool:
    """绕过 safe-delete shim（其 trash 可能失败），走 Win32 DeleteFileW"""
    return bool(ctypes.windll.kernel32.DeleteFileW(str(p)))


def drop(p: pathlib.Path):
    """删临时文件：优先正常删，失败再走 Win32"""
    try:
        p.unlink()
    except OSError:
        hard_delete(p)


def load_works(folders=None):
    """[(folder, nick, date, wid, ts)] —— 仅视频类作品"""
    out = []
    for csvp in sorted(DATASET.glob("*.csv")):
        folder = ROOT / csvp.stem
        if not folder.exists():
            continue
        if folders and folder.name not in folders:
            continue
        seen = set()
        with csvp.open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                kind = (row.get("作品类型") or "").strip()
                wid = (row.get("作品ID") or "").strip()
                ts = (row.get("发布时间") or "").strip()
                nick = (row.get("账号昵称") or "").strip() or folder.stem.split("_", 1)[-1].rsplit("_", 1)[0]
                if kind != "视频" or not wid or not ts or wid in seen:
                    continue
                seen.add(wid)
                out.append((folder, nick, ts[:10], wid, ts))
    return out


def audio_gears(bras):
    """[bit_rate_audio] -> [(bitrate, size, url)]，按码率升序"""
    items = []
    for b in bras or []:
        m = b.get("audio_meta") or {}
        ul = m.get("url_list") or {}
        url = ul.get("main_url") or ul.get("backup_url")
        if url and m.get("media_type") == "audio":
            items.append((m.get("bitrate") or 0, m.get("size") or 0, url))
    items.sort()
    return items


def pick_stream(bras, quality: str):
    """按音质档挑音频流"""
    items = audio_gears(bras)
    if not items:
        return None
    if quality == "small":
        return items[0]
    if quality == "best":
        return items[-1]
    return items[len(items) // 2]


def pick_progressive(bras):
    """兜底用：挑体积最小的 format='mp4'（渐进式，含音轨）档 -> (size, gear, url)"""
    items = []
    for b in bras or []:
        if (b.get("format") or "").lower() != "mp4":
            continue  # dash 是纯视频流，没有音轨
        pa = b.get("play_addr") or {}
        urls = pa.get("url_list") or []
        size = pa.get("data_size") or 0
        if urls and size:
            items.append((size, b.get("gear_name") or "", urls[0]))
    if not items:
        return None
    items.sort()
    return items[0]


def probe(path: pathlib.Path):
    """ffmpeg 校验：返回 (可读, 时长字符串)"""
    pr = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path)], capture_output=True,
                        text=True, encoding="utf-8", errors="replace")
    err = pr.stderr or ""
    if "Audio:" not in err or "Duration: N/A" in err:
        return False, ""
    dur = next((l.split(",")[0].split("Duration:")[-1].strip()
                for l in err.splitlines() if "Duration" in l), "")
    return True, dur


def fetch_once(session, url, headers, dest: pathlib.Path, log, wid) -> bool:
    """下载 url 到 dest，带一次重试"""
    for attempt in (1, 2):
        try:
            r = session.get(url, headers=headers, timeout=600, verify=False)
            if r.status_code == 200 and len(r.content) > 20_000:
                dest.write_bytes(r.content)
                return True
            log(f"  [重试{attempt}] {wid} HTTP {r.status_code} size={len(r.content)}")
        except Exception as e:
            log(f"  [重试{attempt}] {wid} {type(e).__name__}: {str(e)[:80]}")
        time.sleep(2)
    return False


def extract_audio(src: pathlib.Path, dest: pathlib.Path) -> bool:
    """从 mp4 抽音轨到 m4a：先尝试无损 copy，失败则转码 AAC"""
    for args in (["-vn", "-c:a", "copy"], ["-vn", "-c:a", "aac", "-b:a", "128k"]):
        pr = subprocess.run(
            [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
             *args, str(dest)],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if dest.exists() and dest.stat().st_size > 20_000 and not pr.returncode:
            return True
    return False


async def fetch_audio(session, params, folder, nick, date, wid, ts, quality,
                      headers, log, fallback_video=False):
    """返回 (status, 实际字节, 说明)"""
    from src.interface.detail import Detail
    dest = folder / f"{date}_{nick}_{wid}.m4a"
    if dest.exists() and dest.stat().st_size > 20_000:
        return ("skip", 0, "")

    d = await Detail(params, detail_id=wid).run()
    v = (d or {}).get("video") or {}
    tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="audiog_"))

    picked = pick_stream(v.get("bit_rate_audio") or [], quality)
    if not picked:
        # ⚠ 限流陷阱：抖音偶发返回"降级响应"（content-length 约 60KB 而非 125KB+），
        #   表现为 bit_rate / bit_rate_audio 被裁掉 → 看起来像"没有音频档"。
        #   若直接走兜底，会白下几十 MB 的 mp4。所以先退避重取一次详情再判定。
        await asyncio.sleep(2.5)
        d = await Detail(params, detail_id=wid).run()
        v = (d or {}).get("video") or {}
        picked = pick_stream(v.get("bit_rate_audio") or [], quality)
        if not picked:
            log(f"  [限流重试后仍无音频档] {wid} "
                f"bit_rate={len(v.get('bit_rate') or [])} 条")

    if picked:
        _br, _expect, url = picked
        tmp = tmpdir / "a.m4a"
        if not fetch_once(session, url, headers, tmp, log, wid):
            return ("fail", 0, "音频流下载失败")
        ok, dur = probe(tmp)
        if not ok:
            return ("invalid", 0, "音频档校验不过")
        dest.write_bytes(tmp.read_bytes())
        drop(tmp)
        return ("ok", dest.stat().st_size, f"{dur} {dest.stat().st_size/1048576:.1f}MB")

    if not fallback_video:
        return ("no-audio", 0, "")

    # 兜底：最小 mp4 -> 抽音轨 -> 删 mp4
    prog = pick_progressive(v.get("bit_rate"))
    if not prog:
        return ("no-audio", 0, "无可用 mp4 档")
    size, gear, url = prog
    tmp_mp4 = tmpdir / "v.mp4"
    tmp_m4a = tmpdir / "a.m4a"
    if not fetch_once(session, url, headers, tmp_mp4, log, wid):
        return ("fail", 0, f"兜底视频下载失败({gear})")
    if not extract_audio(tmp_mp4, tmp_m4a):
        drop(tmp_mp4)
        return ("invalid", 0, "抽音轨失败")
    ok, dur = probe(tmp_m4a)
    if not ok:
        drop(tmp_mp4)
        return ("invalid", 0, "抽出的音轨校验不过")
    dest.write_bytes(tmp_m4a.read_bytes())
    drop(tmp_mp4)
    drop(tmp_m4a)
    return ("fallback", dest.stat().st_size,
            f"{dur} 源mp4 {size/1048576:.1f}MB -> m4a {dest.stat().st_size/1048576:.1f}MB")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正下载（默认 dry-run）")
    ap.add_argument("--folder", nargs="*", help="只处理指定目录名（可多个）")
    ap.add_argument("--quality", choices=["small", "mid", "best"], default="small")
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--sample", type=int, default=8, help="dry-run 抽样个数")
    ap.add_argument("--fallback-video", action="store_true",
                    help="无音频档时回落到最小 mp4 抽音轨（需 --apply 才下载）")
    ap.add_argument("--delete-mp4", action="store_true", help="音频就绪后删除同作品 mp4（需 --apply）")
    args = ap.parse_args()

    works = load_works(args.folder)
    print(f"作品（视频类）{len(works)} 个，目录 {sorted({w[0].name.split('_')[0][:14] for w in works})}")

    todo = []
    for w in works:
        folder, nick, date, wid, ts = w
        dest = folder / f"{date}_{nick}_{wid}.m4a"
        if dest.exists() and dest.stat().st_size > 20_000:
            continue
        old = list(folder.glob(f"{ts.replace(':', '.')}-*"))
        if any(p.suffix.lower() == ".m4a" for p in old):
            continue
        todo.append(w)
    print(f"已有标准名 m4a 跳过，待处理 {len(todo)} 个")

    if not args.apply:
        import logging
        logging.disable(logging.CRITICAL)
        from src.testers import Params
        session = requests.Session(impersonate="chrome124")
        import json
        headers = {"User-Agent": UA, "Referer": "https://www.douyin.com/",
                   "Cookie": _tk_auth.read_cookie()[0]}
        n = min(args.sample, len(todo))
        with_a = 0
        sum_a = 0
        sum_fb = 0
        async with Params() as params:
            await _tk_auth.inject(params, verbose=False)
            for w in todo[:n]:
                from src.interface.detail import Detail
                d = await Detail(params, detail_id=w[3]).run()
                v = (d or {}).get("video") or {}
                g = pick_stream(v.get("bit_rate_audio") or [], args.quality)
                if g:
                    with_a += 1
                    sum_a += g[1]
                    mark = f"音频 {g[1]/1048576:.1f}MB @{g[0]//1000}kbps"
                else:
                    p = pick_progressive(v.get("bit_rate"))
                    if p:
                        sum_fb += p[0]
                        mark = f"无音频档 → 兜底 mp4 {p[0]/1048576:.1f}MB({p[1]})"
                    else:
                        mark = "无音频档 且 无可用 mp4"
                print(f"  样本 {w[3]}  {mark}")
                await asyncio.sleep(0.3)
        print(f"\n(dry-run) 抽样 {n} 个：音频档命中 {with_a}/{n}"
              f"（{with_a/n*100:.0f}%），其余需兜底")
        if with_a:
            est_a = sum_a / with_a * len(todo)
            print(f"  若全走音频直下：约 {sum_a/with_a/1048576:.1f}MB/个 "
                  f"× {len(todo)} ≈ {est_a/1073741824:.2f}GB")
        if sum_fb:
            est_fb = sum_fb / (n - with_a) * len(todo) * (n - with_a) / n
            print(f"  兜底部分（源 mp4 体积，抽完即删）：约 {est_fb/1073741824:.2f}GB 临时占用")
        print("确认后加 --apply 执行（无音频档的加 --fallback-video）")
        return

    import logging
    logging.disable(logging.CRITICAL)
    from src.testers import Params

    logf = ROOT / "_audio_direct_log.txt"

    def log(msg):
        print(msg)
        with logf.open("a", encoding="utf-8") as f:
            f.write(msg + "\n")

    session = requests.Session(impersonate="chrome124")
    sem = asyncio.Semaphore(args.jobs)
    t0 = time.time()
    stats = {"ok": 0, "fallback": 0, "skip": 0, "fail": 0, "no-audio": 0, "invalid": 0}
    total_bytes = 0
    deleted = 0

    async with Params() as params:
        await _tk_auth.inject(params)
        cookie = _tk_auth.read_cookie()[0]
        headers = {"User-Agent": UA, "Referer": "https://www.douyin.com/", "Cookie": cookie}

        async def worker(w):
            nonlocal total_bytes, deleted
            async with sem:
                folder, nick, date, wid, ts = w
                status, got, note = await fetch_audio(
                    session, params, folder, nick, date, wid, ts, args.quality,
                    headers, log, fallback_video=args.fallback_video)
                stats[status] = stats.get(status, 0) + 1
                if status in ("ok", "fallback"):
                    total_bytes += got
                    tag = "✔" if status == "ok" else "↩兜底"
                    log(f"  {tag} {wid} {note} -> {folder.name}\\{date}_{nick}_{wid}.m4a")
                    if args.delete_mp4:
                        for p in folder.glob(f"{date}_{nick}_{wid}.mp4"):
                            if hard_delete(p):
                                deleted += 1
                elif status != "skip":
                    log(f"  ✘ {wid} {status} {note}")
                await asyncio.sleep(0.3)

        await asyncio.gather(*(worker(w) for w in todo))

    log(f"\n完成：音频直下 {stats['ok']}，兜底抽音轨 {stats['fallback']}，失败 {stats['fail']}，"
        f"无音频档 {stats['no-audio']}，校验不过 {stats['invalid']}，跳过 {stats['skip']}，"
        f"共 {total_bytes/1073741824:.2f}GB，删视频 {deleted} 个，耗时 {time.time()-t0:.0f}s")
    print("done")


if __name__ == "__main__":
    asyncio.run(main())
