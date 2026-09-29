# Docker 部署 —— 抖音自媒体内容流水线

把「**采集视频 → 转换音频 → 文案转写 → 云端归档**」整条链装进容器，一套 `docker compose` 起停。

宿主机现状（Windows + 两条定时自动化）保持不变；这套文件是**平行的一套部署方式**，
删掉 `deploy/docker/` 目录即可完全回到当前状态，不留残留。

---

## 0. 一句话说清设计取舍

这条流水线的代码是**长期在跑的实盘脚本**（每日 18:00 生产任务），而且到处是硬编码的
Windows 绝对路径。容器化有两种做法：

| | 做法 A：改实盘代码 | **做法 B：容器侧兼容层（本方案）** |
|---|---|---|
| 改动面 | 16 个实盘脚本各加环境变量读取 | 0 个实盘脚本 |
| 风险 | 每日生产任务直接受影响，要全量回归 | 零，删目录即回滚 |
| 干净度 | 更干净（显式、可读） | 靠一层 monkey-patch，稍"魔法" |
| 可维护 | 实盘与容器**共用**一份逻辑 | 容器适配集中在 `wb_compat/` |

本项目有一条硬规矩：**实盘是唯一编辑入口，不许为了部署去分叉代码**。
所以选了 B，但把机制、边界、自检、以及"想换成 A 时该怎么做"全部写在下面（见 §5、§11）。
**A 是更长期的正确方向**，等容器跑稳了再走评审切换。

---

## 1. 架构

```
                    宿主机（Windows）                       容器
   ┌───────────────────────────────────┐        ┌──────────────────────────────┐
   │ D:\视频\自媒体视频库       8.7GB+ │ bind → │ /mnt/video-lib               │
   │ D:\视频\媒体知识库         (增长) │ bind → │ /mnt/media-kb                │
   │ C:\Users\EDY\Videos\data          │ bind → │ /mnt/videos-data  (ro)       │
   │ C:\Users\EDY\Projects\video-...   │ bind → │ /mnt/vca          (ro)       │
   │ D:\alist  (alist v3.63, :5244)    │ bind → │ /mnt/alist        (ro)       │
   └───────────────────────────────────┘        │                              │
                                                │  webui      :8770  常驻看板  │
   ┌───────────────────────────────────┐        │  scheduler  容器内定时       │
   │ VideoAnalyzer-Transcribe (nssm)   │ ←socat─│  harvest    采集链    ┐      │
   │ ASR 引擎  127.0.0.1:8766          │        │  transcribe stage1    ├ 单步 │
   └───────────────────────────────────┘        │  render     stage2    │      │
   ┌───────────────────────────────────┐        │  upload     归档上传  ┘      │
   │ alist (D:\alist)  127.0.0.1:5244  │ ←socat─│                              │
   └───────────────────────────────────┘        └──────────────────────────────┘
        ↑ 用 socat 把容器内 127.0.0.1:<port> 桥到宿主机，因为实盘脚本把这两个地址硬编码了
```

链内数据流（与实盘完全一致）：

```
_pipeline.py / _dl_mix.py        下载视频（8 账号 + 1 合集，工具自身做增量去重）
      ↓
_repair.py / _seed_done.py       修库 / 回写（防漏下、防重下）
      ↓
_to_audio.py                     抽音轨（.mp4 → .m4a，无损，删源）
      ↓
asr_batch.py stage1              逐条提交 ASR 引擎（长视频自动切片）
_stage2_daemon.py                并行渲染：关键词 + LLM 总结 → md + 封面
      ↓
_upload_alist.py                 经 alist 增量上传夸克网盘
```

---

## 2. 快速开始

```bash
cd deploy/docker

# ① 生成配置（复制 .env.example，按需改宿主机路径）
./run.sh init

# ② 自检 —— 强烈建议第一次就做，它会把挂载、权限、兼容层、宿主服务逐项验一遍
./run.sh check

# ③ 先空跑一遍看命令对不对（不真跑）
./run.sh dry

# ④ 正式跑（挑需要的）
./run.sh harvest     # 手动跑一次采集链
./run.sh transcribe  # 手动跑一次转写链
./run.sh upload      # 手动跑一次归档上传

# ⑤ 常驻
./run.sh up          # 只看板 :8770
./run.sh up-cron     # 看板 + 容器内定时（替代宿主机那两条自动化）
```

> **不用 `run.sh` 也行**，等价命令：
> ```bash
> cp .env.example .env
> docker compose build
> docker compose --profile job run --rm selftest
> docker compose --profile job run --rm harvest
> docker compose up -d
> ```

### 前置条件

| 项 | 要求 |
|---|---|
| Docker | Docker Desktop 已启动（`docker info` 能通）。**本机当前守护进程没起**，第一步先启动它 |
| 磁盘 | 容器只做编排，不额外占空间；但 `D:\alist\data\temp` 需要余量（alist 上传前会整文件暂存） |
| Cookie | `D:\视频\自媒体视频库\_tools\_secrets\douyin_cookie.json` 仍在有效期内 |
| LLM | `D:\视频\媒体知识库\_llm_config.sh` 存在（容器启动时 source 它，与 Windows 同一份） |
| 宿主服务 | `VideoAnalyzer-Transcribe`（:8766）与 alist（:5244）在跑，否则转写/上传会失败 |

---

## 3. 服务的分层

| 服务 | profile | 常驻 | 作用 |
|---|---|---|---|
| `webui` | 默认 | 是 | 实时看板 `:8770`（进度 / 逐词流式 / 下载进度） |
| `scheduler` | `cron` | 是 | 容器内按 `18:00 采集 / 19:05 转写 / 20:00 上传` 跑整条链 |
| `selftest` | `job` | 一次性 | 环境自检 |
| `harvest` | `job` | 一次性 | 完整采集链（6 步） |
| `transcribe` | `job` | 一次性 | `stage1` + 并行 `stage2` 渲染 |
| `render` | `job` | 一次性 | 只跑 stage2 |
| `repair` / `mix` / `audio` / `seed` / `covers` / `rename` | `job` | 一次性 | 采集链的单步 |
| `upload` | `job` | 一次性 | 增量上传夸克 |
| `alist` | `cloud` | 可选 | 容器内 alist（**默认不用**，改用宿主机 D:\alist 那份已配好夸克的） |
| `ollama` | `llm` | 可选 | 本地 LLM（**默认不用**，走云端 SiliconFlow） |

⚠ **`up-cron` 与宿主机那两条自动化只能二选一**：两边会并发写同一份
`DouK-Downloader.db` / `_upload_state.db`，DB 与落盘状态会不一致。

---

## 4. 为什么代码不烤进镜像

镜像里**只有运行时**（Python 3.13 + 依赖 + ffmpeg + socat + 兼容层 + 入口脚本），
代码和数据全部 bind mount 进来。理由：

1. **实盘是唯一编辑入口** —— 改一行脚本立刻生效，不用重建镜像；
2. 数据本体 8.7GB 且持续增长，不可能进镜像；
3. 删掉 `deploy/docker/` 即完全回滚，零残留；
4. 知识库仓库里本来就只有实盘脚本的**快照**（`core/`），而且刻意不含
   `asr_batch.py` / `webui.py`（高频改动，复制会分叉）—— 拿它当构建上下文根本跑不起来。

---

## 5. 核心机制：路径兼容层（`wb_compat/`）

### 5.1 问题

实盘脚本里到处是硬编码 Windows 路径：

```python
ROOT     = pathlib.Path(r"D:\视频\自媒体视频库")                        # _rename/_repair/_to_audio/_upload_alist …
OUT_ROOT = r"D:\视频\媒体知识库"                                        # asr_batch.py
LIB_ROOT = r"D:\视频\自媒体视频库"                                      # lib_source.py
FFMPEG   = r"C:\Users\EDY\Projects\video-analyzer\...\ffmpeg-win-x86_64-v7.1.exe"
SERVER_PY= os.path.join(SERVER_DIR, "venv", "Scripts", "python.exe")   # asr_batch.py
```

在 Linux 容器里这些路径**全部不存在**。

### 5.2 做法

`sitecustomize.py` 在解释器启动时自动 `import`（靠 `PYTHONPATH=/opt/wb/compat`），
把「以已知 Windows 前缀开头的字符串」在**进入系统调用前**改写成挂载点路径。
`PYTHONPATH` 会被子进程继承，所以 `_pipeline.py` 再 `subprocess` 起的 `main.py` 也自动生效。

### 5.3 映射表

| Windows 前缀 | 环境变量 | 容器默认 |
|---|---|---|
| `D:\视频\自媒体视频库` | `WB_VIDEO_LIB` | `/mnt/video-lib` |
| `D:\视频\媒体知识库` | `WB_MEDIA_KB` | `/mnt/media-kb` |
| `D:\视频\自媒体脚本知识库` | `WB_KB_REPO` | `/mnt/kb-repo` |
| `D:\alist` | `WB_ALIST_DIR` | `/mnt/alist` |
| `C:\Users\EDY\Videos\data` | `WB_VIDEO_DATA` | `/mnt/videos-data` |
| `C:\Users\EDY\Projects\video-analyzer` | `WB_VCA_DIR` | `/mnt/vca` |

3 条**正则规则**（优先级更高，因为要落到不同目标上）：

| 规则 | 指向 | 为什么必须单独处理 |
|---|---|---|
| `…/imageio_ffmpeg/binaries/ffmpeg-win-*.exe` | `WB_FFMPEG`（`/usr/bin/ffmpeg`） | 两个脚本各自硬编码了**不同**的 Windows ffmpeg 路径；镜像里装的是 apt 版 ffmpeg |
| `…/venv/Scripts/python.exe`（含 `.venv` / `asr_venv`） | `WB_PYTHON`（`sys.executable`） | `_dl_mix.py` 与 `asr_batch.ensure_server()` 会 exec 它。**两种形态都要管**：带盘符的走正则；已被前缀表改写成 POSIX 形态的（`/mnt/video-lib/…/.venv/Scripts/python.exe`）靠快速路径里的 `endswith('/Scripts/python.exe')` 兜住。不改就是 `Exec format error`（宿主那份是 Windows PE 二进制） |
| 裸盘符 `D:\` `D:` `D:\\` | `WB_DATA_ROOT`（`/`） | `_pipeline.free_gb(path=r"D:\\")` 的磁盘预检 |

### 5.4 承载面（8 处补丁）

| # | 挂钟点 | 覆盖的写法 |
|---|---|---|
| 1 | `pathlib.PurePath.__init__` | `Path(r"D:\…")`、`Path(r"D:\…")/"x"`、`.rglob()/.exists()` |
| 2 | `os.path.join` | **先 join 再 rewrite**，不能逐参数改（见下） |
| 3 | `os.makedirs/remove/unlink/rename/replace/stat/listdir/scandir/walk` + `os.path.exists/isfile/isdir/…` | 裸字符串字面量 |
| 4 | `builtins.open` | `open(OUT_ROOT + "/x")`（字符串拼接绕过了 join） |
| 5 | `glob.glob` / `iglob` | `glob.glob(OUT_ROOT + "/*.json")` |
| 6 | `shutil.disk_usage/copy2/copytree/move/rmtree/…` | 磁盘预检 |
| 7 | `subprocess.Popen.__init__`（`run/call/check_output` 都经它） | `[FFMPEG, "-i", …]`、`[SERVER_PY, …]`、`cwd=` |
| 8 | `ctypes.windll` → 假 kernel32 | `DeleteFileW`（硬删）、`OpenProcess/CloseHandle`（PID 存活） |

**`os.path.join` 那个坑是真踩到的**（本机自检抓出来）：如果逐参数改写，第一参数
`C:\…\video-analyzer\backend` 会先变成 POSIX 前缀 `/mnt/vca/backend`，后续的
`venv/Scripts/python.exe` 正则**永远不可能命中**，`ensure_server()` 的自救分支就会指向不存在的文件。
所以必须「先 `join` 再 `rewrite`」。

### 5.5 安全边界

* **仅当 `os.name != "nt"` 时生效**；Windows 上 `activate()` 是空操作，可安全无害加载
  （本机用 `WB_COMPAT_FORCE=1` 可强制激活，只为验证补丁有没有挂上）。
* 不含盘符的字符串**零开销直返**（`":" not in p[:4]`），所以 `--flags`、
  `http://host:port` 之类不会被误改。
* 命中前缀但目标不存在时**原样返回**，绝不静默指向别处 —— 让脚本自己报"文件不存在"，
  比悄悄改到错误位置好排查。
* 补丁幂等，被重复 `activate()` 无副作用。
* `WB_COMPAT_VERBOSE=1` 打印每条改写（最多 500 条）到 stderr。

### 5.6 自检

```bash
# 全量（推荐，容器内能验全部 8 个承载面）
./run.sh check

# 只验兼容层
docker compose --profile job run --rm harvest python -m wb_compat --selftest

# 宿主机上也能验（只验映射逻辑；8 个承载面需 POSIX 语义，会自动跳过）
python deploy/docker/wb_compat/wb_compat.py --selftest
```

---

## 6. 端口中继（为什么需要 socat）

实盘脚本把两个服务地址**硬编码**成 `127.0.0.1`：

```python
ASR_API = "http://127.0.0.1:8766"      # asr_batch.py:26（另有 _listening() 里硬编码的 ("127.0.0.1", 8766)）
url     = "http://127.0.0.1:5244"      # _alist.py：读不到 _secrets/alist.json 时的兜底
```

容器里 `127.0.0.1` 是**容器自己**，所以由 `entrypoint.sh` 起 socat 做桥接：

```
容器 127.0.0.1:8766 ──socat──▶ host.docker.internal:8766  (宿主机 ASR 引擎)
容器 127.0.0.1:5244 ──socat──▶ host.docker.internal:5244  (宿主机 alist)
```

* 端口列表由 `WB_RELAY_PORTS` 控制（默认 `8766,5244`；走本地 Ollama 再加 `11434`）。
* `WB_RELAY_TARGET` 默认 `host.docker.internal`（compose 里已加
  `extra_hosts: host.docker.internal:host-gateway`）。
* 用 socat 而不是 `network_mode: host`，是为了保住 compose 的服务名解析与端口映射；
  真要 host 网络也可以，自行改 compose。
* **副作用（可接受）**：`asr_batch.ensure_server()` 会先探端口，看到 8766 已被监听就
  只等待、不自己 Popen，行为与 Windows 上"由 Windows 服务托管"时一致。
  若宿主机 ASR 真挂了，它会等到 `WB_SERVICE_WAIT`(600s) 超时后失败 —— 不会起僵尸实例。
* 端口探测用的是 bash 内建 `/dev/tcp`，不依赖 `ss`/`netstat`（slim 镜像里没有）。

---

## 7. ASR 引擎的三种放法

| 放法 | 配置 | 说明 |
|---|---|---|
| **① 连宿主机（默认）** | `WB_RELAY_PORTS` 含 `8766` | 复用现有 `VideoAnalyzer-Transcribe`，模型只加载一份，最省内存 |
| ② 引擎也进容器 | `video-analyzer` 仓库自带 `Dockerfile`/`docker-compose.yml`（python:3.11-slim + ffmpeg + Whisper small）。把它 `up -d` 后，把 `WB_RELAY_TARGET` 指到那个服务名，并把 `8766` 从 `WB_RELAY_PORTS` 去掉 | Whisper small 约需 1.5–2GB 内存 |
| ③ 迁到纯 Linux 服务器 | 同 ②，再把挂载点换成 Linux 真实路径（改 `wb_compat` 的前缀表或设 `WB_*` 环境变量） | 长期方案 |

本方案默认 ①：宿主机的引擎是现成的、稳定的，容器只是"另一台客户端"。

---

## 8. 权限

镜像默认用**非 root**（uid/gid `1000`，与 Docker Desktop / WSL2 下宿主机文件的默认属主一致）。

如果 `./run.sh check` 报 `Permission denied`，在 compose 的公共配置里加一行用 root 跑：

```yaml
x-common: &common
  user: "0:0"        # 最快，但降级了安全性
  ...
```

**注意流水线要大量写/删文件**（下载、抽音频删源、改名、清理缓存），写权限是硬前提。
根因是 Windows 侧文件的 ACL 映射，长期方案是给 `D:\视频` 两个目录补写权限。

---

## 9. 定时：容器内 vs 宿主机

| | 宿主机（现状） | 容器内（`scheduler` 服务） |
|---|---|---|
| 实现 | 两条 automation prompt | `scripts/scheduler.sh`（bash 循环 + 当天去重标记） |
| 时间 | 18:00 采集 / 20:00 上传 | `WB_SCHEDULE` 默认 `18:00\|harvest,19:05\|transcribe,20:00\|upload` |
| 漏跑 | 到点没跑就丢 | **自动补跑**（判据是 `now >= 计划时刻 且 今天没跑过`） |
| 依赖 | 需要 WorkBuddy 在跑 | 只需 Docker 在跑 |
| 互斥 | 靠 prompt 里的约定 | `flock` 排它锁（`flock` 缺失则降级为不互斥并告警） |

**二选一，不要同时开。** 迁移步骤：
1. 停掉宿主机那两条 automation（`62e4768f…` 18:00 采集、`81d9de27…` 20:00 上传）；
2. `./run.sh up-cron`；
3. `./run.sh logs scheduler` 观察一轮，确认时间点与结果符合预期。

容器内跑**不需要** `--max-seconds 540` 轮次循环 —— 那是为了规避宿主机 agent
单次调用时长上限，容器里没有这个限制，`_upload_alist.py` 一次跑完即可（脚本本身天然续传）。

---

## 10. 已验证 / 未验证（诚实清单）

**已在宿主机验证**（本轮实际跑过）

* `wb_compat` 的**映射逻辑**：前缀表 7 例、正则规则 5 例、裸盘符、零成本直返反例 —— 全绿
* `sitecustomize` 能被自动加载、补丁确实挂上（`WB_COMPAT_FORCE=1` + `WB_COMPAT_VERBOSE=1` 实证）
* 修掉两个**真 bug**：`os.path.join` 先改写导致正则失效；裸盘符 `D:\\`（双反斜杠）不被 `_DRIVE_RE` 匹配
* 全部 shell 脚本 `bash -n` 语法通过（含修掉 `[[ … ]] && cmd` 在 `set -e` 下静默退出的陷阱 ×3）
* `scheduler.sh`：补跑触发、当天去重、每个计划点只跑一次（桩测试：2 个计划点各触发 1 次，多 tick 不重复）
* `wbctl.sh`：`help` / 未知步骤 / `harvest` / `transcribe` / `upload` / `render` 的
  `WB_DRY_RUN=1` 全链空跑通过，链路顺序与退出码正确
* 缺文件、缺 LLM 配置等错误分支的提示文案（中途确实触发过，可用）

**未验证（需要 Docker 起来 + 容器内 POSIX 环境）**

* **镜像能否构建成功**（依赖 wheel 在 `python:3.13-slim` 上的可用性；`build-essential` 已备）
* 补丁承载面中依赖 POSIX 语义的部分：`os.makedirs/remove/stat/…`、`shutil.disk_usage`、
  `subprocess` argv 改写、`ctypes.windll` 假内核（容器内 `wb_compat --selftest` 会全跑）
* 权限模型（uid 1000 写 bind mount）
* `ctypes.windll` 假内核在真实脚本里的行为（`_rename/_acct_fetch/_audio_direct` 的硬删、
  `_upload_alist` 的 PID 存活判断）
* 端到端真实跑一轮（下载 → 转写 → 上传）
* Docker Desktop 上 `host.docker.internal` 的实际可达性（`extra_hosts` 已配）

> 本轮**没有构建、没有运行**（按「先只要文件，不急着跑」的要求），以上未验证项都是
> 第一次 `./run.sh check` 会替你把关的。建议第一次部署严格按 §2 走 ①②③④。

---

## 11. 备选路线：把路径改成环境变量（更干净，但要改实盘）

如果将来想让容器化"名正言顺"、去掉兼容层这层魔法，做法是给实盘脚本做**向后兼容**的小改动：

```python
# 以 _rename.py 为例（其余同构）
import os
ROOT = pathlib.Path(os.environ.get("WB_VIDEO_LIB") or r"D:\视频\自媒体视频库")
```

要点：

* 用 `or` 兜默认值 → **Windows 上行为完全不变**（环境变量不设时走原路径），零回归风险；
* 要改的位置：16 个实盘脚本的模块级 `ROOT`/`WS`/`OUT_ROOT`/`LIB_ROOT`/`FACTS`/`FFMPEG`/
  `ALIST_DB`/`ALIST_CONFIG`，以及 `asr_batch.py` 的 `ASR_API`/`OLLAMA`/`ROOT`/`APPDATA`/
  `OUT_ROOT`/`FFMPEG`/`SERVER_DIR`（当前这些**全是硬编码，不读环境变量**）；
* 改完要在 Windows 上完整跑一轮 18:00 链路回归（这是为什么建议**先让容器跑稳**再动）；
* 属于"改实盘代码"，按项目约定**必须走评审**；
* 改完跑 `python _sync_core.py --apply` 把这些文件同步回本仓库 `core/`。

改完之后，`wb_compat/` 就可以整体删掉，`PYTHONPATH` 那行也能从 `Dockerfile` 里去掉。

---

## 12. 文件清单

```
deploy/docker/
├── README.md                    本文件
├── Dockerfile                   两阶段构建（builder 装依赖 → runtime 装 ffmpeg/socat）
├── docker-compose.yml           整条链的服务编排（默认/cron/job/cloud/llm 五种 profile）
├── .env.example                 配置模板（无凭据；LLM 密钥仍在宿主机 _llm_config.sh）
├── .dockerignore
├── run.sh                       命令封装（init/check/up/up-cron/dry/step/shell/down/logs）
├── requirements/
│   ├── harvest.txt              采集侧依赖（与实盘工具 requirements.txt 钉死版本一致）
│   └── kb.txt                   转写/渲染/看板侧（requests + flask + markdown；**不含 whisper**）
├── wb_compat/                   ★ 路径兼容层（容器适配的全部内容都在这里）
│   ├── wb_compat.py             映射表 + 8 处补丁 + ctypes.windll 假内核 + 自检
│   └── sitecustomize.py         解释器启动钩子（薄壳，失败必须沉默）
└── scripts/
    ├── common.sh                日志 / 挂载校验 / LLM 配置加载 / run_py / 端口中继
    ├── entrypoint.sh            统一入口（先验兼容层 → 起中继 → exec）
    ├── wbctl.sh                 ★ 调度入口（selftest / harvest / transcribe / upload / 单步 …）
    └── scheduler.sh             容器内定时（补跑 + 当天去重 + flock）
```

---

## 13. 排障

| 症状 | 原因 / 处理 |
|---|---|
| `连不上 Docker 守护进程` | Docker Desktop 没启动。托盘图标变绿后重试 `docker info` |
| 启动就报 `路径兼容层未就绪` | `PYTHONPATH` 被覆盖了。检查是否有人在 compose/命令里改了它；`-e WB_COMPAT_VERBOSE=1` 看细节 |
| 脚本报 `No such file or directory: D:\视频\...` | 兼容层没生效（同上）。若路径形如 `/mnt/video-lib/...` 却不存在，是对应目录没挂进来，看 `.env` 的 `WB_HOST_*` |
| `Permission denied` 写 `D:\视频\…` | 见 §8，改成 `user: "0:0"` 或给目录补写权限 |
| 转写报连不上 8766 | 宿主机 `VideoAnalyzer-Transcribe` 服务没起；或 `WB_RELAY_PORTS` 里没有 `8766`。`./run.sh check` 的第⑧项会告诉你 |
| 上传报 alist 401 / 连不上 | 宿主机 alist 没起，或 `D:\alist\data\temp` 没空间。另：写操作必须走 alist，夸克直写会 401 |
| `Exec format error` | 又是什么地方 exec 了宿主 Windows venv 的 `python.exe`。把 `WB_COMPAT_VERBOSE=1` 打出来看改写记录，补进 `wb_compat` 的正则/快速路径 |
| 转写跑成了旧源「关注」 | `WB_SOURCE` 不是 `lib`。`_llm_config.sh` 里必须有 `export WB_SOURCE=lib`（容器会先 `tr -d '\r'` 再 source，防 CRLF 把值变成 `lib\r`） |
| 采集直接退出（无日志） | `_pipeline.py` 内置磁盘闸门：剩余空间低于 `MIN_FREE_GB`(默认 20) 会 `exit=2` 并记日志「已跳过（磁盘不足）」。**exit=2 不要重试**，先清盘 |
| 看板打不开 | `docker compose ps` 看 `wb-webui` 是否 healthy；端口是否被占（改 `.env` 的 `WB_UI_PORT`） |

---

## 14. 卸载 / 回滚

```bash
./run.sh down                       # 停掉所有 profile 的容器
docker compose --profile cloud --profile llm down -v   # 连卷一起删（alist-data / ollama-data）
rm -rf deploy/docker                # 删掉整套部署文件
```

宿主机上的**数据与脚本一行未改**，回滚后状态与现在完全一致。
唯一需要自己清理的是 `docker rmi douyin-pipeline:5.8`（可选）。
