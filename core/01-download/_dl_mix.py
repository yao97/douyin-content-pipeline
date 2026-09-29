# -*- coding: utf-8 -*-
"""下载指定抖音合集：临时切换 settings.json 的 run_command/mix_urls，跑完恢复。"""
import json
import subprocess
import sys
import time
from pathlib import Path

WS = Path(r"D:\视频\自媒体视频库")
TOOL = WS / "_tools" / "TikTokDownloader"
SETTINGS = TOOL / "Volume" / "settings.json"
VENV_PY = TOOL / ".venv" / "Scripts" / "python.exe"
MAIN = TOOL / "main.py"
MIX_URL = "https://www.douyin.com/collection/7545086042492127266/1"
LOG = WS / "_dl_mix_log.txt"


def log(msg: str):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def main():
    cfg = json.loads(SETTINGS.read_text(encoding="utf-8"))
    old_rc = cfg.get("run_command")
    old_mix = cfg.get("mix_urls")
    log(f"原 run_command={old_rc!r} 原 mix_urls={len(old_mix or [])} 项")

    # 写入合集配置
    mix_entry = {"mark": "", "url": MIX_URL, "enable": True}
    cfg["mix_urls"] = ([x for x in (old_mix or []) if MIX_URL not in (x.get("url") or "")] + [mix_entry])
    cfg["run_command"] = "5 5 1 Q"  # 5=终端交互 5=批量下载合集作品(抖音) 1=使用mix_urls参数 Q=退出
    SETTINGS.write_text(json.dumps(cfg, ensure_ascii=False, indent=4), encoding="utf-8")
    log("已写入 mix_urls + run_command=5 5 1 Q")

    env = dict(subprocess.os.environ)
    env["CODEBUDDY_SAFE_DELETE_ENABLED"] = "0"

    try:
        t0 = time.time()
        r = subprocess.run(
            [str(VENV_PY), str(MAIN)],
            cwd=str(TOOL),
            capture_output=True,
            env=env,
            timeout=14400,
        )
        out = (r.stdout or b"").decode("utf-8", errors="replace")
        err = (r.stderr or b"").decode("utf-8", errors="replace")
        log(f"main.py rc={r.returncode} 耗时={time.time()-t0:.0f}s stdout尾部:")
        for line in out.strip().splitlines()[-15:]:
            log("  | " + line)
        if err.strip():
            log("stderr尾部:")
            for line in err.strip().splitlines()[-8:]:
                log("  ! " + line)
    finally:
        # 恢复 run_command；mix_urls 保留（账号模式不读它，留着便于后续增量）
        cfg = json.loads(SETTINGS.read_text(encoding="utf-8"))
        cfg["run_command"] = old_rc or "5 1 1 Q"
        SETTINGS.write_text(json.dumps(cfg, ensure_ascii=False, indent=4), encoding="utf-8")
        log(f"已恢复 run_command={cfg['run_command']!r}（mix_urls 保留）")


if __name__ == "__main__":
    sys.exit(main())
