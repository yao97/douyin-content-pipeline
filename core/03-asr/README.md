# 03-asr · 文案转写

本目录收录**转写链的全部代码**（2026-09-30 起补全，之前只放了两个文件）：

| 文件 | 角色 |
|---|---|
| `asr_batch.py` | **转写主程序**。子命令：`stage1` / `stage2` / `registry` / `accept` / `retry` / `syncmeta` / `syncmd` / `slice` / `slice-all` |
| `lib_source.py` | 数据源适配层：把 `自媒体视频库` 的文件系统翻译成转写任务列表 |
| `_stage2_daemon.py` | 渲染守护：raw → md（关键词 + LLM 总结）+ 复制封面 |
| `remote_handoff.py` | **跨机交接**：`export`（生成外机待转写清单）/ `publish`（清单推到夸克）/ `fetch`（从夸克拉回传包并导入）/ `import`（合入 raw + 重建注册表）/ `status` |
| `checkpoint.py` | 诊断 + 尽力拉起（尾行打 `ACTION:`） |
| `progress.py` | 进度快照 |
| `webui.py` | 实时看板（Flask，`:8770`） |

> 这套代码同时是**实盘快照**：外机 `git clone` 下来配好环境变量即可直接跑，
> 不用改代码 —— 所有本机路径都能用 `WB_*` 环境变量覆盖（见下）。

---

## 1. 不在本仓库的东西（跑转写必须先具备）

| 东西 | 说明 |
|---|---|
| `_asr_registry.json` | 增量重跑的**唯一真相**。不在仓库里，按机器各自持有；跨机时**只允许一个写者** |
| `_llm_config.sh` | LLM 运行环境变量（**含明文密钥，不入版本库**）；模板见 `core/00-config/llm_config.example.sh` |
| ASR 引擎 `transcribe_server.py`（`:8766`） | 属于 `video-analyzer` 工程，由 Windows 服务 `VideoAnalyzer-Transcribe`（nssm）托管 |
| 模型 `qwen3_asr_models/`（3.8GB） | ASR 1.3G + Aligner 481M + int4 ONNX；获取方式见 `video-analyzer` 的 README |
| `verify_all.py` / `rename_covers.py` / `long_videos.py` | 校验与辅助脚本，留在实盘工作区 |

---

## 1b. 在第二台机器上跑（跨机转写）

完整流程见 [`docs/11-跨机转写.md`](../../docs/11-跨机转写.md)。两端约定夸克目录
`自媒体视频库/_跨机交接/{to_remote,from_remote}` 做唯一通道。

**本机侧**（两条命令）：

```bash
python remote_handoff.py publish        # 清单 → 夸克 to_remote/（~330 KB）
# …外机跑完并回传 from_remote/xxx.zip…
python remote_handoff.py fetch --apply  # 拉最新 zip → 校验合入 → 重建注册表
```

**外机侧**：

```bash
# 1) 从夸克下 to_remote/ 三个文件；按「外机操作说明.md」下 4 个源目录并改名
# 2) 所有本机路径都用环境变量指过去，代码零改动
set WB_SOURCE=lib
set WB_OUT_ROOT=<外机的知识库目录>
set WB_LIB_ROOT=<外机的视频库目录>
set WB_FFMPEG=<外机 ffmpeg.exe>
python asr_batch.py stage1          # 只产 _asr_raw/*.json
# 3) 把清单里那些 vid 的 json 打 zip 传回夸克 from_remote/
```

⚠️ **外机不要跑 `registry`、不要跑 `stage2`** —— 注册表与 md 的写者只能是本机，
否则两台会互相覆盖（`_asr_registry.json` 整表回写会静默抹掉对方改动）。

> **搬运通道为什么不是一把梭**：夸克官方 CLI 的开放平台令牌是**受限视图**
> （`browse --parent-fid 0` 只看到 2 个目录，直取源库 fid 返回 `12005 文件无权限`）；
> alist 的**上传与列目录可用、下载直链被夸克 CDN 拒**（`RequestDeniedByCallback`）。
> 所以本工具：列目录/上传走 alist，下载走**夸克 web API + alist 里那份 cookie**。
> 细节与验证过程见 `docs/11-跨机转写.md` §3.1。

---

## 2. 各文件的职责边界

### lib_source.py

**输入**：文件系统（`UID*_发布作品/`、`MID*_合集作品/`、`Data/*.csv`）
**输出**：与 `asr_batch.build_task_list()` **同构**的任务字典列表

```python
{
  "aid":      目录名,
  "vid":      作品ID,
  "video":    音频文件绝对路径（.m4a 优先，稳定 .mp4 兜底）,
  "cover":    同目录同名封面路径（可为空串）,
  "author":   作者名（合集走 AUTHOR_OVERRIDE）,
  "account":  抖音号,
  "title":    作品描述,
  "pub":      发布日期 YYYY-MM-DD,
  "kind":     视频 / 图集 / 实况,
  "dur_csv":  时长（**秒，字符串**）,
}
```

三个关键约定：

1. **必须同时扫 `UID*` 和 `MID*`** 两类目录。
2. **`dur_csv` 是时长字段**，不是 `duration` —— 用错会静默得到 0。
3. 合集文件名**没有作品ID**，只能用 `发布时间` 前缀（`YYYY-MM-DD HH.MM.SS`）去 CSV 对号。

### _stage2_daemon.py

**输入**：`_asr_raw/*.json`（stage1 的产物）
**输出**：`<作者>/<日期>_<作者>_<作品ID>.md` + 同名封面

增量逻辑（`md_state()`）：

| md 状态 | 动作 |
|---|---|
| 不存在 | 全量生成 |
| 存在但缺 `## 视频内容总结` | **复用原有文件名关键词**，只补总结后重写 |
| 已含总结 | 跳过 |

> ⚠️ 只有「缺总结」时才复用旧关键词，避免误用占位符（`—`/空 视为无效，重新生成）。

**退出条件**：检测到 stage1 结束（在**本次启动之后**写入的日志里出现「阶段1 完成」）
且无待渲染条目 → 打印统计后退出。

> ⚠️ 必须从「本次启动后的日志偏移」里找那句标志，否则历史日志里已有的同一句话
> 会让守护进程**立刻误判收工退出**。

---

## 3. 调用入口

```bash
cd D:\视频\媒体知识库

# 环境（Windows）
set WB_SOURCE=lib
call _llm_config.sh

# 转写（长视频自动切片）
python asr_batch.py stage1

# 渲染（另开一个进程）
python _stage2_daemon.py
```

> 两个进程跑完一轮会**自行收工退出并释放锁**，这是正常状态。
> `stage1` 只转写、**不渲染 md**；`stage2` 不在跑 → 出了 raw 也不会有 md。

```bash
# 单独核验 lib 模式下的待转写清单
python -c "import sys; sys.path.insert(0,'.'); import asr_batch as B, lib_source; \
ok={v for v,m in B.load_registry().items() if m.get('ok')}; \
todo=[t for t in lib_source.build_lib_tasks() if t['vid'] not in ok]; \
print('待转写', len(todo))"
```

更多细节见 [`docs/04-文案转写.md`](../docs/04-文案转写.md)。
