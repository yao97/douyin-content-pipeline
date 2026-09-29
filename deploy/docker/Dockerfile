# syntax=docker/dockerfile:1.7
# ─────────────────────────────────────────────────────────────────────────────
#  抖音自媒体内容流水线 —— 统一运行时镜像
#
#  这条流水线是三段式：采集视频 → 转换音频 → 文案转写，再归档到夸克网盘。
#  镜像里**只放运行时**（Python + 依赖 + ffmpeg + 路径兼容层），
#  **代码与数据全部由宿主机 bind mount 进来**。原因见 README「为什么代码不烤进镜像」：
#    1) 实盘脚本是唯一编辑入口（改了立即可见，无需重建镜像）；
#    2) 数据本体 8.7GB+ 且持续增长，不可能进镜像；
#    3) 删除 deploy/docker/ 即可回到当前状态，零残留。
#
#  阶段：builder（编译依赖）→ runtime（最终镜像，见文件末尾 LABEL）
#  构建：docker build -t douyin-pipeline:5.8 deploy/docker
# ─────────────────────────────────────────────────────────────────────────────

# ── 阶段 1：builder —— 只为把依赖装成 wheel，最终镜像不带编译器 ──────────────
FROM python:3.13-slim-bookworm AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libxml2-dev libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements/harvest.txt requirements/kb.txt ./

# 装到独立前缀，供 runtime 阶段整目录复制过去（与上游 DouK-Downloader 官方
# Dockerfile 同一手法，保证依赖落在 /usr/local 下、不污染 site-packages 布局）
RUN pip install --no-cache-dir --prefix=/install -r harvest.txt -r kb.txt


# ── 阶段 2：runtime ─────────────────────────────────────────────────────────
FROM python:3.13-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="douyin-content-pipeline" \
      org.opencontainers.image.description="抖音账号/合集采集 → 音频提取 → ASR 转写 → 云端归档 的容器化运行时" \
      org.opencontainers.image.source="https://github.com/yao97/douyin-content-pipeline"

# ── 时区（先声明，下面 apt 那步要用它建 /etc/localtime）──
ARG TZ=Asia/Shanghai

# ── 系统依赖 ──
#   ffmpeg   抽音轨（实盘脚本硬编码了 Windows ffmpeg 路径，由 wb_compat 映射到 /usr/bin/ffmpeg）
#   curl     健康检查
#   socat    容器内 127.0.0.1 端口中继 —— 实盘脚本把 ASR(8766)/alist(5244) 都写成 127.0.0.1，
#            容器里用它把 127.0.0.1:8766 转发到 host.docker.internal:8766，实现零代码改动
#   tzdata   容器内定时任务要按北京时间 18:00 / 20:00 跑
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
        socat \
        tzdata \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && ln -snf /usr/share/zoneinfo/${TZ} /etc/localtime \
    && echo "${TZ}" > /etc/timezone

# ── 依赖（builder 的产物） ──
COPY --from=builder /install /usr/local

# ── 运行时文件：路径兼容层 + 入口脚本 ──
COPY wb_compat/ /opt/wb/compat/
COPY scripts/   /opt/wb/scripts/
RUN chmod +x /opt/wb/scripts/*.sh

# ── 环境变量 ──
ENV TZ=${TZ} \
    LANG=C.UTF-8 \
    # 无缓冲 + UTF-8，否则容器日志里的中文会乱码/滞后
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PYTHONIOENCODING=utf-8 \
    # 别往宿主机 bind mount 里写 __pycache__（会污染实盘仓库）
    PYTHONDONTWRITEBYTECODE=1 \
    # ★ 兼容层靠 PYTHONPATH + sitecustomize 自动生效，所有子进程自动继承
    PYTHONPATH=/opt/wb/compat \
    # 挂载点默认值（compose/.env 可覆盖）
    WB_VIDEO_LIB=/mnt/video-lib \
    WB_MEDIA_KB=/mnt/media-kb \
    WB_KB_REPO=/mnt/kb-repo \
    WB_ALIST_DIR=/mnt/alist \
    WB_VIDEO_DATA=/mnt/videos-data \
    WB_VCA_DIR=/mnt/vca \
    WB_FFMPEG=/usr/bin/ffmpeg \
    WB_DATA_ROOT=/ \
    # 端口中继：容器内 127.0.0.1:<port> → host.docker.internal:<port>
    WB_ASR_RELAY=1 \
    WB_ALIST_RELAY=1

WORKDIR /opt/wb

# ── 非 root 运行 ──
# uid/gid 1000：与 Docker Desktop(WSL2) 下宿主机文件的默认属主一致，写 bind mount 一般不踩权限。
# 若遇到 Permission denied，见 README「权限」一节的两种解法（改 user / 改挂载属主）。
ARG WB_UID=1000
ARG WB_GID=1000
RUN groupadd -g ${WB_GID} wb 2>/dev/null || true \
    && useradd -m -u ${WB_UID} -g ${WB_GID} -s /bin/bash wb 2>/dev/null || true \
    && mkdir -p /var/log/wb \
    && chown -R ${WB_UID}:${WB_GID} /var/log/wb
USER wb

EXPOSE 8770

ENTRYPOINT ["/opt/wb/scripts/entrypoint.sh"]
CMD ["/opt/wb/scripts/wbctl.sh", "help"]
