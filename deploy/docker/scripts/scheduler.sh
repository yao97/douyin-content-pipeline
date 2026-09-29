#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  容器内定时调度 —— 替代宿主机那两条自动化
#
#  为什么不用 cron：容器里 cron 的日志很别扭（要么进 syslog 要么丢），
#  也不便于「上一轮还没跑完就别再叠一轮」。这里用一个极简调度器：
#    · 每 20s 醒一次，按**当天**为粒度判断某个时刻是否已执行；
#    · 用「now >= 计划时刻 且 今天还没跑过」的判据 —— 这样即使上一轮跑到深夜，
#      漏掉的时刻会在下一 tick **自动补跑**，不会像 cron 那样直接丢弃；
#    · 每轮拿一把 flock 排它锁，天然防止同一容器起两份 scheduler。
#
#  计划表 WB_SCHEDULE 形如：18:00|harvest,19:05|transcribe,20:00|upload
#  （与宿主机现有自动化对齐：18:00 采集链 / 19:05 转写链 / 20:00 上传）
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# shellcheck source=/opt/wb/scripts/common.sh
. "${WB_SCRIPTS_DIR:-/opt/wb/scripts}/common.sh"

SCHEDULE="${WB_SCHEDULE:-18:00|harvest,19:05|transcribe,20:00|upload}"
JITTER="${WB_SCHEDULE_JITTER:-0}"
TICK="${WB_SCHEDULE_TICK:-20}"
LOCK="${WB_SCHEDULE_LOCK:-/tmp/wb_scheduler.lock}"

# 同一容器只允许一份调度器。flock 来自 util-linux；万一镜像里没有就降级为「不互斥」
# （自己别起两份即可），不要因此整个调度挂掉。
if command -v flock >/dev/null 2>&1; then
  exec 9>"$LOCK"
  if ! flock -n 9; then
    die "已有 scheduler 在跑（$LOCK 被占用），本进程退出"
  fi
else
  warn "未找到 flock，跳过调度器互斥检查（请勿同时起两份 scheduler）"
fi

# 解析计划表 → 两个数组
declare -a S_HHMM=() S_STEP=()
IFS=',' read -ra _items <<< "$SCHEDULE"
for it in "${_items[@]}"; do
  hm="${it%%|*}"; st="${it##*|}"
  hm="$(printf '%s' "$hm" | tr -d ' ')"
  st="$(printf '%s' "$st" | tr -d ' ')"
  if [[ ! "$hm" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]]; then
    warn "计划表条目格式不对，已跳过：$it（应为 HH:MM|步骤）"
    continue
  fi
  S_HHMM+=("$hm"); S_STEP+=("$st")
done
[[ ${#S_STEP[@]} -gt 0 ]] || die "WB_SCHEDULE 解析后为空：$SCHEDULE"

# 已执行标记：<今天>_<HH:MM>_<步骤>
declare -A DONE=()
LAST_DAY=""

log "调度器就绪，共 ${#S_STEP[@]} 个计划点，tick=${TICK}s，抖动=${JITTER}s"
for i in "${!S_STEP[@]}"; do
  log "   ${S_HHMM[$i]}  →  ${S_STEP[$i]}"
done

secs_of() {   # "HH:MM" → 当日秒数
  printf '%s\n' "$((10#${1:0:2} * 3600 + 10#${1:3:2} * 60))"
}

while true; do
  today="$(date '+%F')"
  if [[ "$today" != "$LAST_DAY" ]]; then
    DONE=(); LAST_DAY="$today"
    log "新的一天：$today，已执行标记已清空"
  fi

  now_s=$(( $(date '+%s') - $(date -d "today 00:00" '+%s') ))

  for i in "${!S_STEP[@]}"; do
    hm="${S_HHMM[$i]}"; st="${S_STEP[$i]}"
    key="${today}_${hm}_${st}"
    if [[ -n "${DONE[$key]:-}" ]]; then
      continue
    fi
    plan_s="$(secs_of "$hm")"
    if (( now_s >= plan_s )); then
      DONE[$key]=1
      if (( JITTER > 0 )); then
        j=$(( RANDOM % (JITTER + 1) ))
        if (( j > 0 )); then
          log "按抖动设置先停 ${j}s"
          sleep "$j"
        fi
      fi
      hr
      log "⏰ 触发计划点 $hm → $st（当前 $(date '+%T')）"
      set +e
      "${WB_SCRIPTS_DIR:-/opt/wb/scripts}/wbctl.sh" "$st"
      rc=$?
      set -e
      log "⏰ $st 结束，退出码 $rc"
      if [[ "$st" == "harvest" ]] && (( rc == 2 )); then
        warn "采集因磁盘不足被跳过（exit=2），按约定不重试"
      fi
    fi
  done

  sleep "$TICK"
done
