#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  wbctl —— 流水线调度入口
#
#    wbctl.sh selftest              环境自检（首次部署先跑这个）
#    wbctl.sh harvest               完整采集链：修库→下载→合集→抽音频→回写→封面
#    wbctl.sh transcribe            stage1 转写 + 并行 stage2 渲染
#    wbctl.sh render                只跑 stage2 渲染守护
#    wbctl.sh upload                增量上传夸克网盘
#    wbctl.sh webui                 启动实时看板（前台，:8770）
#    wbctl.sh scheduler             容器内定时调度（前台，常驻）
#
#    单步：repair / mix / rename / audio / seed / covers / status
#
#  环境变量：
#    WB_DRY_RUN=1   只打印将执行的命令，不真跑（先把整套流程过一遍用）
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# shellcheck source=/opt/wb/scripts/common.sh
. "${WB_SCRIPTS_DIR:-/opt/wb/scripts}/common.sh"

# 采集链整链退出码：留给 scheduler 判断
LAST_RC=0

# ═══════════════════════════════════════════════════════════════════════════
#  自检
# ═══════════════════════════════════════════════════════════════════════════
step_selftest() {
  local fail=0
  hr; log "环境自检"; hr

  log "① 容器信息"
  log "   用户 $(id -un)($(id -u):$(id -g))  时区 $(date '+%Z %z')  现在 $(date '+%F %T')"
  log "   python $(python -V 2>&1)  ffmpeg $(ffmpeg -version 2>/dev/null | head -1 | awk '{print $3}')"

  log "② 路径兼容层"
  local probe
  probe="$(python - <<'PY'
import os, sys
sys.path.insert(0, "/opt/wb/compat")
import wb_compat
wb_compat.activate()
print(wb_compat.rewrite(r"D:\视频\自媒体视频库\_tools\TikTokDownloader"))
print(wb_compat.rewrite(r"D:\视频\媒体知识库\_asr_raw"))
print(wb_compat.rewrite(r"C:\Users\EDY\Projects\video-analyzer\backend\venv\Scripts\python.exe"))
PY
)" || { err "   兼容层导入/激活失败"; fail=$((fail+1)); }
  echo "$probe" | sed 's/^/   /'

  log "③ 挂载点"
  local d
  for d in "$WB_VIDEO_LIB" "$WB_MEDIA_KB" "$WB_VIDEO_DATA" "$WB_VCA_DIR" "$WB_ALIST_DIR"; do
    if [[ -d "$d" ]]; then
      local n; n="$(find "$d" -maxdepth 1 -mindepth 1 2>/dev/null | wc -l)"
      log "   [OK] $d  ← ${WB_MOUNT_OWNER[$d]:-?}  顶层条目 $n"
    else
      warn "   [缺少] $d  应为 ${WB_MOUNT_OWNER[$d]:-?}"
      fail=$((fail+1))
    fi
  done

  log "④ 写权限（视频库 / 知识库）"
  need_writable "$WB_VIDEO_LIB" || fail=$((fail+1))
  need_writable "$WB_MEDIA_KB"  || fail=$((fail+1))

  log "⑤ 关键文件"
  local f
  for f in "$TOOL/main.py" "$TOOL/_pipeline.py" "$TOOL/_repair.py" \
           "$TOOL/Volume/settings.json" "$TOOL/Volume/DouK-Downloader.db" \
           "$WS/_tools/_secrets/douyin_cookie.json" \
           "$WS/_to_audio.py" "$WS/_rename.py" "$WS/_seed_done.py" \
           "$WS/_cover_audit_fix.py" "$WS/_dl_mix.py" "$WS/_upload_alist.py" \
           "$KB/asr_batch.py" "$KB/lib_source.py" "$KB/_stage2_daemon.py" "$KB/webui.py"; do
    if [[ -f "$f" ]]; then
      log "   [OK] $f"
    else
      warn "   [缺少] $f"
      fail=$((fail+1))
    fi
  done

  log "⑥ python 依赖"
  local mod
  for mod in curl_cffi aiosqlite openpyxl rich requests flask markdown; do
    if python -c "import $mod" 2>/dev/null; then
      log "   [OK] $mod"
    else
      warn "   [缺少] $mod"
      fail=$((fail+1))
    fi
  done

  log "⑦ LLM 配置"
  if [[ "${WB_LLM_CONFIG_FILE:-}" == "none" || -z "${WB_LLM_CONFIG_FILE:-}" ]]; then
    warn "   已跳过 source，靠环境变量：backend=${WB_LLM_BACKEND:-未设}"
  elif [[ -f "$WB_LLM_CONFIG_FILE" ]]; then
    log "   [OK] $WB_LLM_CONFIG_FILE 存在（值不在自检里回显，避免泄露密钥）"
    source_llm_config || true
    log "   加载后 WB_SOURCE=${WB_SOURCE:-<未设>} backend=${WB_LLM_BACKEND:-未设}"
  else
    warn "   [缺少] $WB_LLM_CONFIG_FILE"
    fail=$((fail+1))
  fi

  log "⑧ 宿主服务可达性（经 socat 中继）"
  local p
  if [[ -z "${WB_RELAY_PORTS:-}" || "${WB_RELAY_PORTS}" == "none" ]]; then
    log "   已按 WB_RELAY_PORTS=${WB_RELAY_PORTS:-<空>} 跳过（改回 8766,5244 才会检查）"
  else
    IFS=',' read -ra _ps <<< "${WB_RELAY_PORTS}"
    for p in "${_ps[@]}"; do
      p="$(printf '%s' "$p" | tr -d ' ')"
      if [[ -z "$p" ]]; then
        continue
      fi
      if ! port_listening "$p"; then
        warn "   中继未监听 127.0.0.1:$p（WB_RELAY_PORTS 里有没有它？socat 起来了吗？）"
        continue
      fi
      if curl -fsS -m 8 -o /dev/null "http://127.0.0.1:$p/" 2>/dev/null; then
        log "   [OK] 127.0.0.1:$p 有 HTTP 响应"
      else
        warn "   中继在听 127.0.0.1:$p，但宿主机无响应（宿主服务没起？）"
      fi
    done
  fi

  hr
  if [[ $fail -eq 0 ]]; then
    log "✅ 自检通过，可以跑 wbctl.sh harvest / transcribe / upload"
    return 0
  fi
  err "❌ 自检有 $fail 项不通过，先按上面提示修（详见 deploy/docker/README.md）"
  return 1
}

# ═══════════════════════════════════════════════════════════════════════════
#  采集侧单步
# ═══════════════════════════════════════════════════════════════════════════
precheck_harvest() {
  need_writable "$WB_VIDEO_LIB" || return 1
  need_file "$TOOL/main.py" "TikTokDownloader 入口" || return 1
  need_file "$TOOL/Volume/settings.json" "工具配置（含 Cookie，必须存在）" || return 1
  need_file "$TOOL/Volume/DouK-Downloader.db" "增量去重库" || return 1
  need_file "$WS/_tools/_secrets/douyin_cookie.json" "抖音 Cookie" || return 1
  return 0
}

step_repair() {
  need_file "$TOOL/_repair.py" || return 1
  need_file "$TOOL/Volume/DouK-Downloader.db" "增量去重库" || return 1
  hr; log "【1/6 修库】_repair.py --apply —— 删「库有记录但媒体缺失」+ 孤儿缓存"
  run_py "$TOOL" "$TOOL/_repair.py" --apply
}

step_mix() {
  need_file "$WS/_dl_mix.py" || return 1
  hr; log "【2/6 合集】_dl_mix.py"
  run_py "$WS" "$WS/_dl_mix.py"
}

step_audio() {
  need_file "$WS/_to_audio.py" || return 1
  hr; log "【3/6 抽音频】_to_audio.py --delete（无损转 m4a 并回收残留 mp4）"
  run_py "$WS" "$WS/_to_audio.py" --delete
}

step_seed() {
  need_file "$WS/_seed_done.py" || return 1
  hr; log "【4/6 回写】_seed_done.py --apply —— 把已落盘但库无记录的作品写回 DB"
  run_py "$WS" "$WS/_seed_done.py" --apply
}

step_covers() {
  need_file "$WS/_cover_audit_fix.py" || return 1
  hr; log "【5/6 封面】_cover_audit_fix.py —— 审计 + 补齐标准名封面"
  run_py "$WS" "$WS/_cover_audit_fix.py"
}

step_rename() {
  need_file "$WS/_rename.py" || return 1
  hr; log "【重命名】_rename.py"
  run_py "$WS" "$WS/_rename.py"
}

# 完整采集链（严格对齐宿主机 18:00 自动化）
step_harvest() {
  local rc=0 t0=$SECONDS
  hr; log "═══ 采集链开始 ═══"
  if ! precheck_harvest; then
    err "采集前置检查不通过，终止"
    LAST_RC=1; return 1
  fi

  step_repair  || warn "_repair 非 0 退出（继续，避免因个别条目卡住整链）"

  hr; log "【下载】_pipeline.py（7 个账号全量/增量）"
  set +e
  run_py "$TOOL" "$TOOL/_pipeline.py"
  rc=$?
  set -e
  if [[ $rc -eq 2 ]]; then
    # 脚本内置磁盘闸门：剩余空间低于 MIN_FREE_GB(默认20) 就主动跳过
    log "[磁盘不足] _pipeline 主动跳过（exit=2）。按约定**不要重试**，先清盘再跑。"
    LAST_RC=2; return 2
  fi
  [[ $rc -eq 0 ]] || { warn "_pipeline 退出码 $rc（继续跑后续步骤，最后看终检）"; }
  LAST_RC=$rc

  step_mix    || warn "_dl_mix 非 0 退出"
  step_audio  || warn "_to_audio 非 0 退出"
  step_seed   || warn "_seed_done 非 0 退出"
  step_covers || warn "_cover_audit_fix 非 0 退出"

  hr; log "【6/6 终检】_repair（只读）+ _seed_done（只读）—— 两项都应为 0"
  set +e
  run_py "$TOOL" "$TOOL/_repair.py"
  run_py "$WS"   "$WS/_seed_done.py"
  set -e

  hr; log "═══ 采集链结束：用时 $((SECONDS - t0))s，_pipeline 退出码 $LAST_RC ═══"
  return $LAST_RC
}

# ═══════════════════════════════════════════════════════════════════════════
#  转写 / 渲染
# ═══════════════════════════════════════════════════════════════════════════
precheck_kb() {
  need_writable "$WB_MEDIA_KB" || return 1
  need_dir "$WB_VIDEO_LIB" "数据源（lib 模式）" || return 1
  for f in "$KB/asr_batch.py" "$KB/lib_source.py"; do
    need_file "$f" || return 1
  done
  return 0
}

step_render() {
  need_file "$KB/_stage2_daemon.py" || return 1
  hr; log "【渲染】_stage2_daemon.py（raw → md + 封面；检测到 stage1 收工后自行退出）"
  run_py "$KB" "$KB/_stage2_daemon.py"
}

step_transcribe() {
  hr; log "═══ 转写链开始 ═══"
  precheck_kb || { LAST_RC=1; return 1; }
  source_llm_config strict

  local rc=0 dpid=""

  # stage2 渲染守护必须**先于** stage1 启动：它靠「本次启动之后写入的日志里出现
  # 『阶段1 完成』」来判定收工 —— 晚启动会漏掉那句标志而空转。
  if [[ -f "$KB/_stage2_daemon.py" && "${WB_DRY_RUN:-0}" != "1" ]]; then
    ( cd "$KB" && python -u _stage2_daemon.py 2>&1 | sed -u 's/^/[render] /' ) &
    dpid=$!
    log "渲染守护已后台启动 pid=$dpid"
    sleep 2
  fi

  hr; log "【转写】asr_batch.py stage1"
  set +e
  run_py "$KB" "$KB/asr_batch.py" stage1
  rc=$?
  set -e
  [[ $rc -eq 0 ]] || warn "stage1 退出码 $rc"

  if [[ -n "$dpid" ]]; then
    log "等待渲染守护收工（最多 ${WB_RENDER_WAIT:-1800}s）…"
    local waited=0
    while kill -0 "$dpid" 2>/dev/null && (( waited < ${WB_RENDER_WAIT:-1800} )); do
      sleep 10; waited=$((waited + 10))
    done
    if kill -0 "$dpid" 2>/dev/null; then
      warn "渲染守护仍未收工，结束它（raw 已落盘，下次跑会续）"
      kill "$dpid" 2>/dev/null || true
    else
      wait "$dpid" 2>/dev/null || true
      log "渲染守护已收工"
    fi
  fi

  hr; log "═══ 转写链结束：stage1 退出码 $rc ═══"
  LAST_RC=$rc
  return $rc
}

# ═══════════════════════════════════════════════════════════════════════════
#  归档上传
# ═══════════════════════════════════════════════════════════════════════════
step_upload() {
  need_file "$WS/_upload_alist.py" || return 1
  need_file "$WS/_alist.py" || return 1
  hr; log "═══ 归档上传开始 ═══"

  # 容器内**不需要** --max-seconds 轮次循环：那是为了规避宿主机 agent 单次调用时长上限，
  # 容器里没有这个限制，一次跑完即可（脚本本身天然续传、可反复跑）。
  set +e
  run_py "$WS" "$WS/_upload_alist.py" --apply --jobs "${WB_UPLOAD_JOBS:-3}"
  LAST_RC=$?
  set -e
  hr; log "═══ 归档上传结束：退出码 $LAST_RC ═══"
  return $LAST_RC
}

# ═══════════════════════════════════════════════════════════════════════════
#  常驻
# ═══════════════════════════════════════════════════════════════════════════
step_webui() {
  need_file "$KB/webui.py" || return 1
  local port="${WB_UI_PORT:-8770}"
  hr; log "启动实时看板 http://0.0.0.0:$port （容器内）"
  start_relays
  run_py "$KB" "$KB/webui.py" --host 0.0.0.0 --port "$port"
}

step_scheduler() {
  hr; log "启动容器内定时调度：${WB_SCHEDULE:-18:00|harvest,19:05|transcribe,20:00|upload}"
  warn "⚠ 请确认宿主机的两条自动化（18:00 采集 / 20:00 上传）已停用，否则会并发写同一份 DB"
  exec "${WB_SCRIPTS_DIR:-/opt/wb/scripts}/scheduler.sh"
}

step_status() {
  hr; log "状态快照"
  need_file "$WS/_st.py" || return 0
  run_py "$WS" "$WS/_st.py" || true
  local s="$WS/_status.txt"
  if [[ -f "$s" ]]; then
    hr; cat "$s"
  fi
}

# ═══════════════════════════════════════════════════════════════════════════
step_help() {
  # 直接回显文件头的注释块（遇到第一行非注释就停），改注释不会让帮助失真
  awk 'NR>1 && /^#/ { sub(/^# ?/, ""); print; next } NR>1 { exit }' "$0"
}

# ═══════════════════════════════════════════════════════════════════════════
case "${1:-help}" in
  selftest)   step_selftest ;;
  harvest)    step_harvest ;;
  repair)     step_repair ;;
  mix)        step_mix ;;
  audio)      step_audio ;;
  seed)       step_seed ;;
  covers)     step_covers ;;
  rename)     step_rename ;;
  transcribe) step_transcribe ;;
  render)     step_render ;;
  upload)     step_upload ;;
  webui)      step_webui ;;
  scheduler)  step_scheduler ;;
  status)     step_status ;;
  help|-h|--help) step_help ;;
  *)
    err "未知步骤：$1"
    step_help
    exit 2
    ;;
esac
