#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  容器入口：跑任何命令前先做一次统一引导
#    1. 确认路径兼容层已挂上（否则后面所有路径都会找不到，属于致命前提）
#    2. 起 socat 端口中继（把 127.0.0.1:8766 / :5244 桥到宿主机）
#    3. exec 真正的命令
#  ⚠ 只做「全局必需」的事；各步骤自己必需的挂载点由 wbctl.sh 逐步校验。
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# shellcheck source=/opt/wb/scripts/common.sh
. "${WB_SCRIPTS_DIR:-/opt/wb/scripts}/common.sh"

# ── 1. 兼容层自检（致命前提）──
if ! python - <<'PY'
import os, sys
sys.path.insert(0, "/opt/wb/compat")
try:
    import wb_compat
except Exception as e:  # noqa: BLE001
    print(f"!!! wb_compat 无法导入：{type(e).__name__}: {e}", file=sys.stderr)
    raise SystemExit(1)
if os.name == "nt" or not wb_compat.activate():
    print("!!! wb_compat 未激活 —— 实盘脚本里的 Windows 路径将无法解析", file=sys.stderr)
    raise SystemExit(1)
probe = wb_compat.rewrite(r"D:\视频\自媒体视频库\_tools\TikTokDownloader")
if not probe.startswith(os.environ.get("WB_VIDEO_LIB", "/mnt/video-lib")):
    print(f"!!! wb_compat 映射异常：{probe}", file=sys.stderr)
    raise SystemExit(1)
print(f"OK wb_compat 已激活，视频库 → {probe}")
PY
then
  die "路径兼容层未就绪，拒绝继续（检查 PYTHONPATH=/opt/wb/compat 是否被覆盖）"
fi

# ── 2. 端口中继 ──
start_relays

# ── 3. 交给真正的命令 ──
if [[ $# -eq 0 ]]; then
  set -- "${WB_SCRIPTS_DIR:-/opt/wb/scripts}/wbctl.sh" help
fi
exec "$@"
