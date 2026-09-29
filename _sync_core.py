# -*- coding: utf-8 -*-
"""
把实盘核心脚本同步到本仓库的 core/ 目录（快照副本）。

设计
----
- 实盘才是唯一编辑入口；core/ 只是可读、可检索的副本。
- 每次同步前做**敏感串扫描**，命中即中止（宁可不同步，也不能把凭据提交上去）。
- 打印每个文件的 sha256 前 8 位，便于人工核对。

用法
----
  python _sync_core.py            # 预演：列出与实盘的差异（默认）
  python _sync_core.py --apply    # 覆盖同步
  python _sync_core.py --check    # 一致性检查：有差异则退出码 1（CI 友好）
"""
import argparse
import hashlib
import pathlib
import re
import shutil
import sys

KB = pathlib.Path(__file__).resolve().parent
VIDEO_LIB = pathlib.Path(r"D:\视频\自媒体视频库")
MEDIA_KB = pathlib.Path(r"D:\视频\媒体知识库")
TOOL = VIDEO_LIB / "_tools" / "TikTokDownloader"

# (源, 仓库内相对路径)
MAPPING = [
    (TOOL / "_pipeline.py",        "core/01-download/_pipeline.py"),
    (VIDEO_LIB / "_dl_mix.py",     "core/01-download/_dl_mix.py"),
    (VIDEO_LIB / "_acct_fetch.py", "core/01-download/_acct_fetch.py"),
    (VIDEO_LIB / "_tk_auth.py",    "core/01-download/_tk_auth.py"),
    (VIDEO_LIB / "_audio_direct.py", "core/01-download/_audio_direct.py"),

    (VIDEO_LIB / "_to_audio.py",   "core/02-audio/_to_audio.py"),

    (MEDIA_KB / "lib_source.py",      "core/03-asr/lib_source.py"),
    (MEDIA_KB / "_stage2_daemon.py",  "core/03-asr/_stage2_daemon.py"),

    (VIDEO_LIB / "_rename.py",        "core/04-maintain/_rename.py"),
    (TOOL / "_repair.py",             "core/04-maintain/_repair.py"),
    (VIDEO_LIB / "_seed_done.py",     "core/04-maintain/_seed_done.py"),
    (VIDEO_LIB / "_cover_audit_fix.py", "core/04-maintain/_cover_audit_fix.py"),
    (VIDEO_LIB / "_mix_verify.py",    "core/04-maintain/_mix_verify.py"),

    (VIDEO_LIB / "_alist.py",         "core/05-cloud/_alist.py"),
    (VIDEO_LIB / "_upload_alist.py",  "core/05-cloud/_upload_alist.py"),

    (VIDEO_LIB / "_run_cap.py",       "core/06-tools/_run_cap.py"),
]

# 敏感串黑名单：命中即中止同步
SECRET_PATTERNS = [
    (r"sk-[A-Za-z0-9]{16,}",                    "疑似 LLM API Key"),
    (r"alist-[0-9a-f]{8}-[0-9a-f\-]{20,}",      "疑似 alist 全局令牌"),
    (r"sid_guard=",                             "疑似抖音 Cookie"),
    (r"passport_csrf_token=",                   "疑似抖音 Cookie"),
    (r"sessionid=",                             "疑似会话 Cookie"),
    (r"jwt_secret",                             "疑似签名密钥"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----",     "私钥"),
]


def sha8(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:8]


def scan(src: pathlib.Path, data: bytes):
    """返回命中的敏感项列表 [(说明, 行号)]"""
    hits = []
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("utf-8", errors="replace")
    for pat, desc in SECRET_PATTERNS:
        for m in re.finditer(pat, text):
            line = text.count("\n", 0, m.start()) + 1
            hits.append((desc, line))
    return hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="覆盖同步")
    ap.add_argument("--check", action="store_true", help="一致性检查，有差异则退出 1")
    args = ap.parse_args()

    if args.apply and args.check:
        print("--apply 与 --check 互斥")
        return 2

    missing_src, same, diff, blocked = [], [], [], []

    for src, rel in MAPPING:
        dst = KB / rel
        if not src.exists():
            missing_src.append((rel, src))
            continue
        new = src.read_bytes()
        hits = scan(src, new)
        if hits:
            blocked.append((rel, hits))
            continue
        old = dst.read_bytes() if dst.exists() else None
        if old == new:
            same.append((rel, new))
        else:
            diff.append((rel, new, old))

    # ── 输出 ────────────────────────────────────────────────
    print(f"实盘源缺失 {len(missing_src)} 个{'' if not missing_src else '：'}")
    for rel, src in missing_src:
        print(f"  ? {rel}  <- {src}")

    if blocked:
        print(f"\n⛔ 敏感串命中 {len(blocked)} 个 —— 已中止这些文件的同步：")
        for rel, hits in blocked:
            for desc, line in hits:
                print(f"  ! {rel}:{line}  {desc}")
        print("   → 请先把凭据移到工作目录外的配置文件，再重跑。")

    print(f"\n一致 {len(same)} 个，差异 {len(diff)} 个。")
    for rel, new, old in diff:
        tag = "新增" if old is None else "更新"
        print(f"  ~ [{tag}] {rel}   {sha8(new)}"
              + (f"  (仓库现为 {sha8(old)})" if old else ""))

    if not diff and not blocked and not missing_src:
        print("\n✅ core/ 与实盘完全一致。")
        return 0

    if args.check:
        print("\n❌ 存在差异（--check 模式）")
        return 1

    if not args.apply:
        print("\n(dry-run) 加 --apply 执行同步")
        return 0

    if blocked:
        print("\n⛔ 存在敏感串命中，拒绝执行 --apply。")
        return 3

    n = 0
    for rel, new, _old in diff:
        dst = KB / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(new)
        n += 1
    print(f"\n已同步 {n} 个文件。")
    for rel, new, _old in diff:
        print(f"  ✔ {rel}   {sha8(new)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
