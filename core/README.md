# core · 核心代码快照

按流水线阶段分目录存放。这些文件是**实盘脚本的副本**，用途是阅读、检索与二次开发。

> ⚠️ **实盘脚本仍在各自工作目录里运行**（`D:\视频\自媒体视频库`、`D:\视频\媒体知识库`）。
> 修改代码请在实盘改，改完用 `python _sync_core.py --apply` 重新同步到这里。

---

## 目录

| 目录 | 内容 | 对应阶段 |
|---|---|---|
| `00-config/` | 脱敏配置模板（**无真实凭据**） | — |
| `01-download/` | 采集与音频直下 | 下载视频 |
| `02-audio/` | 音视频转换 | 转换音频 |
| `03-asr/` | 转写接口层与渲染守护 | 文案转写 |
| `04-maintain/` | 命名 / 封面 / 数据自愈 | 旁路 |
| `05-cloud/` | 云端归档 | 旁路 |
| `06-tools/` | 通用运行辅助 | — |

---

## 01-download

| 文件 | 说明 |
|---|---|
| `_pipeline.py` | **账号采集主编排**。配置对齐 + 磁盘闸门 `ensure_space()` + 两阶段下载 + 自动改名 |
| `_dl_mix.py` | 合集采集。临时切 `run_command` 为 `"5 5 1 Q"`，跑完恢复 |
| `_acct_fetch.py` | 新账号入库：只拉 30 列元数据 CSV + 封面，不下视频 |
| `_tk_auth.py` | **鉴权参数注入**。给 tester 补 `uifid`/`msToken`，否则只抓到第一页 |
| `_audio_direct.py` | **音频直下**。从 `video.bit_rate_audio[]` 拉纯音轨，含限流退避 + mp4 兜底 |

## 02-audio

| 文件 | 说明 |
|---|---|
| `_to_audio.py` | mp4 → m4a。无损抽流为主、64k 单声道重编码兜底；`--delete` 删源回收磁盘 |

## 03-asr

| 文件 | 说明 |
|---|---|
| `lib_source.py` | **数据源适配层**。扫描 `UID*`/`MID*` 目录 → 产出转写任务列表 |
| `_stage2_daemon.py` | 渲染守护。轮询 raw → 生成 md（关键词 + 总结）+ 复制封面 |
| `README.md` | 转写主程序（`asr_batch.py` 等）的位置与接口说明 |

> ASR 引擎服务 `transcribe_server.py`（`:8766`）属于另一个工程（`video-analyzer`），未随本仓库分发。

## 04-maintain

| 文件 | 说明 |
|---|---|
| `_rename.py` | 命名规范化 `{日期}_{昵称}_{作品ID}.ext`（CSV 映射 + 去重 + 删重复副本） |
| `_repair.py` | **修幽灵记录**：删「有记录无文件」+ 清孤儿缓存（双前缀匹配 + 永久跳过名单） |
| `_seed_done.py` | **回填记录**：已落盘作品写回 DB，防止次日重复下载视频 |
| `_cover_audit_fix.py` | 封面审计 + 三级兜底补齐 + 补标准名 |
| `_mix_verify.py` | 合集核验（**原始命名前缀口径的参考实现**） |

> `_repair.py` 与 `_seed_done.py` **必须成对使用**，详见 `docs/06-数据校验与自愈.md`。

## 05-cloud

| 文件 | 说明 |
|---|---|
| `_alist.py` | alist 客户端（仅标准库）。全局令牌认证 / 列目录 / 建目录 / `PUT /api/fs/put` |
| `_upload_alist.py` | 增量上传器。SQLite 状态库 + 并发 + 目录级校验 + mtime 冷却 |

## 06-tools

| 文件 | 说明 |
|---|---|
| `_run_cap.py` | 子进程输出捕获器。注入环境变量 + 自动 utf-8→gbk 解码后写文件，避免中文乱码 |

---

## 00-config

| 文件 | 说明 |
|---|---|
| `settings.example.json` | TikTokDownloader 配置模板（`root` / `run_command` / `accounts_urls` / `mix_urls` / `static_cover` …） |
| `alist.example.json` | alist 连接信息模板（url + 全局 API 令牌） |
| `llm_config.example.sh` | 运行环境变量模板（`WB_SOURCE`、LLM 通道） |
| `skip_ids.example.json` | 永久不可下载作品名单模板 |

**凭据一律不入版本库**，由脚本从工作目录外读取：

| 凭据 | 读取位置 |
|---|---|
| 抖音 Cookie | `_tools/TikTokDownloader/Volume/settings.json`（`cookie` 字段） |
| alist 令牌 | `_tools/_secrets/alist.json` |
| LLM Key | `媒体知识库/_llm_config.sh` |

---

## 同步机制

```bash
python _sync_core.py            # dry-run：列出与实盘的差异
python _sync_core.py --apply    # 覆盖同步
python _sync_core.py --check    # 只做一致性检查（CI 友好，有差异则非 0 退出）
```

同步时会：

1. 复制实盘脚本到对应目录；
2. 扫描敏感串（`sk-…`、`sid_guard=`、`jwt_secret`、`alist-…` 等），命中则**中止并报错**；
3. 打印每个文件的 sha256 前 8 位，便于人工核对。
