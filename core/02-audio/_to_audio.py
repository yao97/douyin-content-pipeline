# -*- coding: utf-8 -*-
"""
把已下载的 .mp4 抽成音频 .m4a（给 ASR 用），可选删除原视频。

优先无损抽流（-c:a copy，秒级完成），失败则重编码 64k 单声道 AAC 兜底。
用法：
  python _to_audio.py            # 只转换，不删除 mp4
  python _to_audio.py --delete   # 转换成功后删除 mp4
"""
import argparse
import pathlib
import subprocess

ROOT = pathlib.Path(r"D:\视频\自媒体视频库")
FFMPEG = (r"C:\Users\EDY\Projects\video-analyzer\backend\venv\Lib\site-packages"
          r"\imageio_ffmpeg\binaries\ffmpeg-win-x86_64-v7.1.exe")

LOG = ROOT / "_audio_log.txt"


def to_audio(mp4: pathlib.Path) -> bool:
    m4a = mp4.with_suffix(".m4a")
    if m4a.exists() and m4a.stat().st_size > 1024:
        return True  # 已转换过
    tmp = mp4.with_name(mp4.stem + ".__tmp.m4a")
    if tmp.exists():
        try:
            tmp.unlink()
        except OSError:
            pass
    try:
        # 1) 无损抽流
        subprocess.run(
            [FFMPEG, "-y", "-i", str(mp4), "-vn", "-acodec", "copy", str(tmp)],
            capture_output=True, timeout=600,
        )
        if not (tmp.exists() and tmp.stat().st_size > 1024):
            # 2) 兜底：重编码 64k 单声道（ASR 足够）
            subprocess.run(
                [FFMPEG, "-y", "-i", str(mp4), "-vn", "-acodec", "aac",
                 "-b:a", "64k", "-ac", "1", str(tmp)],
                capture_output=True, timeout=600,
            )
        if not (tmp.exists() and tmp.stat().st_size > 1024):
            if tmp.exists():
                tmp.unlink()
            return False
        tmp.replace(m4a)
        return True
    except Exception as e:
        LOG.open("a", encoding="utf-8").write(f"ERR {mp4.name[:60]} :: {e}\n")
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--delete", action="store_true", help="转换成功后删除原 mp4")
    args = ap.parse_args()

    done = fail = kept = 0
    before = after = 0
    # 目录：账号发布作品 `UID*_发布作品` + 合集作品 `MID*`（合集原先被漏掉，
    # 结果 37 个作品的 mp4 一直留着不转 —— 只抽音轨后能省 80%+ 磁盘）
    for folder in sorted(set(ROOT.glob("UID*_发布作品")) | set(ROOT.glob("MID*"))):
        for mp4 in sorted(folder.glob("*.mp4")):
            size = mp4.stat().st_size
            if to_audio(mp4):
                done += 1
                before += size
                after += mp4.with_suffix(".m4a").stat().st_size
                if args.delete:
                    try:
                        mp4.unlink()
                    except OSError:
                        kept += 1
            else:
                fail += 1

    rep = (f"转换成功 {done}，失败 {fail}，删除失败(保留) {kept}\n"
           f"视频 {before/1024/1024:.0f}MB -> 音频 {after/1024/1024:.0f}MB "
           f"(压缩 {before/max(after,1):.0f}x)\n"
           f"delete={args.delete}")
    print(rep)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(rep + "\n")


if __name__ == "__main__":
    main()
