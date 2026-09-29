# 10 · Docker 部署

把「采集 → 音频 → 转写 → 归档」整条链装进容器，一套 `docker compose` 起停。

> **本页是概览。完整机制、已验证/未验证清单、排障表在
> [`deploy/docker/README.md`](../deploy/docker/README.md)。**

---

## 1. 为什么需要"兼容层"这一层东西

这条链的代码是**每日 18:00 在生产跑的实盘脚本**，而且到处是硬编码 Windows 路径：

```python
ROOT     = pathlib.Path(r"D:\视频\自媒体视频库")
OUT_ROOT = r"D:\视频\媒体知识库"
FFMPEG   = r"C:\Users\EDY\Projects\video-analyzer\...\ffmpeg-win-x86_64-v7.1.exe"
SERVER_PY= os.path.join(SERVER_DIR, "venv", "Scripts", "python.exe")
```

容器化的两条路：

| | 改实盘代码（16 个脚本加环境变量读取） | **容器侧兼容层（本方案选的）** |
|---|---|---|
| 实测风险 | 每日生产任务直接受影响，要全量回归 | 零；删 `deploy/docker/` 即回滚 |
| 代码改动 | 16 个脚本 | **0 个** |
| 代价 | 更干净 | 一层 monkey-patch，稍"魔法" |

项目硬规矩「实盘是唯一编辑入口，不许为部署分叉代码」→ 选兼容层。
**长期正确方向仍是改环境变量**，做法与评审要点写在
[deploy/docker/README.md §11](../deploy/docker/README.md)。

---

## 2. 三个关键机制（各用一句话）

| 机制 | 解决什么 | 怎么做 |
|---|---|---|
| **路径兼容层** `wb_compat/` | 硬编码 `D:\…` 在 Linux 里不存在 | `sitecustomize` 在解释器启动时挂 8 处补丁，把已知 Windows 前缀改写成挂载点路径；`PYTHONPATH` 被子进程继承，所以工具起的 `main.py` 也自动生效 |
| **端口中继** socat | 脚本把 `ASR_API`/alist 地址硬编码成 `127.0.0.1` | entrypoint 起 `socat TCP-LISTEN:<port>,bind=127.0.0.1 → host.docker.internal:<port>` |
| **代码不烤进镜像** | 数据 8.7GB+、脚本天天改 | 镜像只放运行时；代码与数据全部 bind mount |

---

## 3. 落地时真踩到的坑（写进代码注释了）

### C1. `os.path.join` 不能逐参数改写

- **症状**：`asr_batch.ensure_server()` 的自救分支指向不存在的解释器。
- **根因**：逐参数改写时，第一参数 `C:\…\video-analyzer\backend` 先变成 POSIX 前缀
  `/mnt/vca/backend`，后续 `venv/Scripts/python.exe` 的正则**永远不可能命中**。
- **修法**：`os.path.join` 必须**先 join 再 rewrite**（整串仍带 Windows 前缀，正则才能命中）。
- **发现方式**：靠 `wb_compat --selftest` 本机跑出来的，不是事后猜的。

### C2. 宿主 Windows venv 的 `python.exe` 被 exec → `Exec format error`

- **根因**：`_dl_mix.py` 的 `VENV_PY = TOOL/".venv"/"Scripts"/"python.exe"` 会直接 `subprocess.run`。
  它经过前缀表后**已是 POSIX 形态**，正则规则够不着，指向的却是宿主的 Windows PE 二进制。
- **修法**：快速路径加 `endswith('/Scripts/python.exe')` 兜住（POSIX 虚拟环境用 `bin/`，
  只有 Windows venv 才叫 `Scripts/`，所以不会误伤）。

### C3. `set -e` 下 `[[ … ]] && cmd` 会静默退出脚本

- **症状**：条件为假时整条返回非 0，`set -e` 直接把脚本带走。
- **修法**：所有条件一律写显式 `if … then … fi`（本轮修了 3 处）。

### C4. 裸盘符 `D:\\` 是**双**反斜杠

- **根因**：`_pipeline.free_gb(path=r"D:\\")` 是 raw 字符串，值是 `D:\\`（两个反斜杠）。
- **修法**：盘符正则写成 `^[A-Za-z]:[\\/]*$`。

### C5. 用 `/dev/tcp` 而不是 `ss`

- **根因**：`python:3.13-slim` 里没有 `iproute2`，`ss`/`netstat` 都没有。
- **修法**：bash 内建 `(exec 3<>/dev/tcp/127.0.0.1/$port)` 探测，零依赖。

### C6. `_llm_config.sh` 若带 CRLF 会让 `WB_SOURCE` 变成 `lib\r`

- **后果**：**静默退回旧源「关注」**，转写跑的完全不是本流水线的数据。
- **修法**：容器里 source 前先 `tr -d '\r'` 到临时文件再 source，并断言 `WB_SOURCE == lib`。

---

## 4. 服务与定时

```
默认      webui      常驻 :8770 看板
cron      scheduler  容器内 18:00 采集 / 19:05 转写 / 20:00 上传
job       selftest / harvest / transcribe / render / upload / repair / mix / audio / seed / covers / rename
cloud     alist      可选（默认用宿主机 D:\alist 那份）
llm       ollama     可选（默认走云端 SiliconFlow）
```

容器内定时器与宿主机那两条 automation **只能二选一**：都会写同一份
`DouK-Downloader.db` / `_upload_state.db`。容器版的优点是**漏跑会自动补跑**
（判据 `now >= 计划时刻 且 今天没跑过`），不依赖 WorkBuddy 在跑。

容器内**不需要** `--max-seconds 540` 轮次循环 —— 那是为规避宿主机 agent 单次调用时长上限。

---

## 5. 快速开始

```bash
cd deploy/docker
./run.sh init       # 生成 .env + 构建镜像
./run.sh check      # 自检（挂载/权限/兼容层/宿主服务逐项验）
./run.sh dry        # 空跑一遍看命令对不对
./run.sh harvest    # 真跑采集链
```

详见 [`deploy/docker/README.md`](../deploy/docker/README.md) 的 §2 快速开始与 §13 排障表。
