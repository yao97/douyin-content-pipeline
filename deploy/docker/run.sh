#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  run.sh —— 给不想记 docker compose 参数的人用的一层薄封装
#
#    ./run.sh init        生成 .env（若不存在）并构建镜像
#    ./run.sh check       跑容器内自检（强烈建议第一次执行）
#    ./run.sh up          启动常驻服务（看板）
#    ./run.sh up-cron     启动常驻服务 + 容器内定时（替代宿主机自动化）
#    ./run.sh logs [svc]  跟踪日志（默认 webui）
#    ./run.sh harvest     手动跑一次采集链
#    ./run.sh transcribe  手动跑一次转写链
#    ./run.sh upload      手动跑一次归档上传
#    ./run.sh dry         整套流程空跑一遍（只打印命令，不真跑）
#    ./run.sh step <名>   跑单个步骤（repair/mix/audio/seed/covers/rename/render/status）
#    ./run.sh shell       进容器 shell（调试用）
#    ./run.sh down        停止并移除容器
#
#  提示：宿主机若已有 18:00/20:00 两条自动化，请**不要**用 up-cron，二选一。
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

cd "$(dirname "$0")"

DC="docker compose"
ENV_FILE=.env
STEP_SERVICES="harvest transcribe render repair audio seed covers mix upload selftest"

need_docker() {
  if ! docker info >/dev/null 2>&1; then
    echo "!! 连不上 Docker 守护进程。" >&2
    echo "   Windows 上请先启动 Docker Desktop（托盘图标变成绿色再试）。" >&2
    echo "   验证：docker info" >&2
    exit 1
  fi
}

need_env() {
  if [[ ! -f "$ENV_FILE" ]]; then
    echo "!! 缺少 $ENV_FILE，先跑：./run.sh init" >&2
    exit 1
  fi
}

case "${1:-help}" in
  init)
    if [[ ! -f "$ENV_FILE" ]]; then
      cp .env.example "$ENV_FILE"
      echo "已生成 $ENV_FILE（按需修改里面的宿主机路径）"
    else
      echo "$ENV_FILE 已存在，保留不动"
    fi
    need_docker
    $DC build
    ;;

  check)  need_docker; need_env; $DC --profile job run --rm selftest ;;
  up)     need_docker; need_env; $DC up -d ;;
  up-cron)
    need_docker; need_env
    echo "⚠ 确认宿主机的 18:00 采集 / 20:00 上传两条自动化已停用（否则会并发写同一份 DB）"
    $DC --profile cron up -d
    ;;
  down)   need_docker; $DC --profile cron --profile job --profile cloud --profile llm down ;;
  logs)   need_docker; $DC logs -f "${2:-webui}" ;;

  harvest|transcribe|render|repair|audio|seed|covers|mix|upload)
    need_docker; need_env
    $DC --profile job run --rm "$1"
    ;;

  dry)
    need_docker; need_env
    echo "── 空跑采集链 / 转写链 / 上传链（只打印命令）──"
    $DC --profile job run --rm -e WB_DRY_RUN=1 harvest
    $DC --profile job run --rm -e WB_DRY_RUN=1 transcribe
    $DC --profile job run --rm -e WB_DRY_RUN=1 upload
    ;;

  step)
    need_docker; need_env
    s="${2:-}"
    if [[ -z "$s" ]]; then
      echo "用法：./run.sh step <$STEP_SERVICES>" >&2
      exit 2
    fi
    case " $STEP_SERVICES " in
      *" $s "*) $DC --profile job run --rm "$s" ;;
      *) echo "未知步骤：$s（可选：$STEP_SERVICES）" >&2; exit 2 ;;
    esac
    ;;

  shell)
    need_docker; need_env
    $DC --profile job run --rm --entrypoint /bin/bash harvest
    ;;

  help|*)
    awk 'NR>1 && /^#/ { sub(/^# ?/, ""); print; next } NR>1 { exit }' "$0"
    ;;
esac
