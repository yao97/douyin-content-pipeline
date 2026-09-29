#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  公共库 —— 只被 source，不单独执行
#  统一：日志格式、挂载点校验、LLM 配置加载、python 调用约定
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── 挂载点（与 wb_compat.py 的前缀表一一对应，改一处必须改两处）──
WB_VIDEO_LIB="${WB_VIDEO_LIB:-/mnt/video-lib}"
WB_MEDIA_KB="${WB_MEDIA_KB:-/mnt/media-kb}"
WB_VIDEO_DATA="${WB_VIDEO_DATA:-/mnt/videos-data}"
WB_VCA_DIR="${WB_VCA_DIR:-/mnt/vca}"
WB_ALIST_DIR="${WB_ALIST_DIR:-/mnt/alist}"
WB_KB_REPO="${WB_KB_REPO:-/mnt/kb-repo}"

# 各挂载点对应的宿主机 Windows 路径（报错时直接告诉用户该挂哪个目录）
declare -A WB_MOUNT_OWNER=(
  ["$WB_VIDEO_LIB"]='D:\视频\自媒体视频库'
  ["$WB_MEDIA_KB"]='D:\视频\媒体知识库'
  ["$WB_VIDEO_DATA"]='C:\Users\EDY\Videos\data'
  ["$WB_VCA_DIR"]='C:\Users\EDY\Projects\video-analyzer'
  ["$WB_ALIST_DIR"]='D:\alist'
  ["$WB_KB_REPO"]='D:\视频\自媒体脚本知识库'
)

WS="$WB_VIDEO_LIB"                              # 采集侧工作区
TOOL="$WS/_tools/TikTokDownloader"              # 下载工具（含 _pipeline/_repair）
KB="$WB_MEDIA_KB"                               # 知识侧工作区（转写/渲染/看板）

WB_LOG_DIR="${WB_LOG_DIR:-/var/log/wb}"
mkdir -p "$WB_LOG_DIR" 2>/dev/null || true

# ── 日志 ──
log()  { printf '[%s] %s\n' "$(date '+%F %T')" "$*"; }
warn() { printf '[%s] [WARN]  %s\n' "$(date '+%F %T')" "$*" >&2; }
err()  { printf '[%s] [ERROR] %s\n' "$(date '+%F %T')" "$*" >&2; }
die()  { err "$*"; exit 1; }
hr()   { printf '%s\n' "────────────────────────────────────────────────────────────"; }

# ── 目录校验 ──
need_dir() {   # need_dir <路径> [用途说明]
  local p="$1" why="${2:-}"
  if [[ ! -d "$p" ]]; then
    err "缺少目录 $p${why:+（$why）}"
    err "  它应由宿主机 ${WB_MOUNT_OWNER[$p]:-?} 挂载进来；检查 .env 的 WB_HOST_* 与 docker-compose.yml 的 volumes"
    return 1
  fi
  return 0
}

need_file() {  # need_file <路径> [用途说明]
  local p="$1" why="${2:-}"
  if [[ ! -f "$p" ]]; then
    err "缺少文件 $p${why:+（$why）}"
    return 1
  fi
  return 0
}

need_writable() {  # 写权限探测（真正写一个小文件，别只看 ls 权限位）
  local d="$1"
  need_dir "$d" "写权限探测" || return 1
  local probe="$d/.wb_write_probe.$$"
  if ! ( : > "$probe" ) 2>/dev/null; then
    err "$d 不可写（容器当前用户 $(id -u):$(id -g)）"
    err "  解法①：docker-compose.yml 里把 user 设成 \"0:0\"（用 root 跑）"
    err "  解法②：宿主机给该目录补写权限（WSL2 下见 README「权限」一节）"
    return 1
  fi
  rm -f "$probe" 2>/dev/null || true
  return 0
}

# ── LLM / 运行配置：与 Windows 同一份文件（唯一生效位置）──
source_llm_config() {   # source_llm_config [strict]
  local strict="${1:-}"
  local f="${WB_LLM_CONFIG_FILE:-$WB_MEDIA_KB/_llm_config.sh}"

  if [[ -z "$f" || "$f" == "none" ]]; then
    warn "按 WB_LLM_CONFIG_FILE=$f 跳过 source，完全依赖环境变量"
  elif [[ ! -f "$f" ]]; then
    local owner="${WB_MOUNT_OWNER[$WB_MEDIA_KB]:-?}"
    die "LLM 配置文件不存在：$f
      · 正常路径是把 $WB_MEDIA_KB（= ${owner}）挂进来，里面有 _llm_config.sh
      · 或者设 WB_LLM_CONFIG_FILE=none，改用环境变量传 WB_LLM_BACKEND / WB_CLOUD_KEY 等"
  else
    # ⚠ 该文件由 Windows 编辑，可能带 CRLF。直接 source 会让变量值尾部带 \r，
    #   典型症状：WB_SOURCE 变成 "lib\r" 从而**静默退回旧源「关注」**。
    #   所以先去掉 \r 再 source。
    local tmp; tmp="$(mktemp /tmp/wb_llm.XXXXXX)"
    tr -d '\r' < "$f" > "$tmp"
    set -a
    # shellcheck disable=SC1090
    . "$tmp"
    set +a
    rm -f "$tmp"
    log "已加载 LLM 配置：$f（backend=${WB_LLM_BACKEND:-未设} model=${WB_CLOUD_MODEL:-${WB_OLLAMA_MODEL:-未设}}）"
  fi

  if [[ "${WB_SOURCE:-}" != "lib" ]]; then
    local cur="${WB_SOURCE:-未设}"
    local msg="WB_SOURCE='${cur}' 不是 lib —— 转写会退回旧源「关注」(${WB_MOUNT_OWNER[$WB_VIDEO_DATA]:-C:\\Users\\EDY\\Videos\\data}\\关注)，那不是本流水线的数据源"
    if [[ "$strict" == "strict" ]]; then
      die "$msg"
    fi
    warn "$msg"
  fi
}

# ── python 调用约定 ──
wb_py_env() {
  export PYTHONUTF8=1
  export PYTHONIOENCODING=utf-8
  export PYTHONUNBUFFERED=1
  # 容器内没有 safe-delete shim，但保持一致：万一将来在 WSL/宿主直跑也能用
  export CODEBUDDY_SAFE_DELETE_ENABLED=0
}

run_py() {   # run_py <cwd> <脚本绝对路径> [args...]
  local cwd="$1"; shift
  local script="$1"; shift
  [[ -f "$script" ]] || die "脚本不存在：$script"
  wb_py_env
  if [[ "${WB_DRY_RUN:-0}" == "1" ]]; then
    log "[DRY-RUN] (cd $cwd && python -u $script $*)"
    return 0
  fi
  log "▶ python $script ${*:-}"
  ( cd "$cwd" && exec python -u "$script" "$@" )
}

run_sh() {   # run_sh <cwd> <命令...>   —— 给非 python 的步骤（如 socat）用
  local cwd="$1"; shift
  if [[ "${WB_DRY_RUN:-0}" == "1" ]]; then
    log "[DRY-RUN] (cd $cwd && $*)"
    return 0
  fi
  log "▶ (cd $cwd) $*"
  ( cd "$cwd" && exec "$@" )
}

# 解释器搜索路径：实盘脚本里有 `sys.path.insert(0, r"D:\视频\…\_tools\TikTokDownloader")`
# 这类**裸字符串**（不是 pathlib.Path，兼容层改不到），在容器里会变成不存在的相对路径而被
# import 系统忽略。这里把真正的两个根目录放进 PYTHONPATH 兜住，导入照常能成功。
export PYTHONPATH="/opt/wb/compat:${TOOL:-}:${WS:-}${PYTHONPATH:+:$PYTHONPATH}"

# ── 端口中继：把容器内 127.0.0.1:<port> 转发到宿主机 ──
# 为什么需要：实盘脚本把 ASR(8766) 和 alist(5244) 的地址**硬编码**成 127.0.0.1，
#            容器里那个 loopback 是自己的，必须靠 socat 桥出去，才能做到零代码改动。

# 端口是否已被监听 —— 用 bash 内建 /dev/tcp，免装 iproute2(netstat/ss)
port_listening() {
  local p="$1"
  (exec 3<>"/dev/tcp/127.0.0.1/$p") 2>/dev/null
}

start_relays() {
  local ports="${WB_RELAY_PORTS:-8766,5244}"
  local target="${WB_RELAY_TARGET:-host.docker.internal}"
  # ⚠ 本脚本全程 `set -e`：`[[ … ]] && return` 这种写法在条件为假时整条返回非 0，
  #   会**直接把脚本带走**。所有条件一律写成显式 if。
  if [[ -z "$ports" || "$ports" == "none" ]]; then
    log "按 WB_RELAY_PORTS=$ports 跳过端口中继"
    return 0
  fi

  IFS=',' read -ra _plist <<< "$ports"
  for p in "${_plist[@]}"; do
    p="$(printf '%s' "$p" | tr -d ' ')"
    if [[ -z "$p" ]]; then
      continue
    fi
    if port_listening "$p"; then
      log "中继 127.0.0.1:$p 已在监听，跳过"
      continue
    fi
    socat "TCP-LISTEN:$p,bind=127.0.0.1,fork,reuseaddr" "TCP:$target:$p" \
      >/dev/null 2>&1 &
    log "中继已起：127.0.0.1:$p → $target:$p (pid $!)"
  done
  # 给 socat 一点时间进入 listen 状态（asr_batch.ensure_server 会探端口）
  sleep 1
}

# ── 宿主服务可达性 ──
probe_http() {   # probe_http <url> [超时秒]
  local url="$1" t="${2:-5}"
  curl -fsS -m "$t" -o /dev/null "$url" 2>/dev/null
}
