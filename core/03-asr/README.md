# 03-asr · 文案转写

本目录只收录**与本流水线耦合度最高的两个文件**：

| 文件 | 角色 |
|---|---|
| `lib_source.py` | 数据源适配层：把 `自媒体视频库` 的文件系统翻译成转写任务列表 |
| `_stage2_daemon.py` | 渲染守护：raw → md（关键词 + LLM 总结）+ 复制封面 |

---

## 1. 转写主程序在哪里

转写的编排主体在**另一个工作区** `D:\视频\媒体知识库`，未随本仓库分发：

| 文件 | 说明 |
|---|---|
| `asr_batch.py` | 批处理编排。子命令：`stage1` / `stage2` / `registry` / `accept` / `retry` / `syncmeta` / `syncmd` / `slice` / `slice-all` |
| `checkpoint.py` | 诊断 + 尽力拉起（尾行打 `ACTION:`） |
| `progress.py` | 进度快照 |
| `webui.py` | 实时看板（Flask，`:8770`） |
| `verify_all.py` / `rename_covers.py` / `long_videos.py` | 校验与辅助 |
| `_asr_registry.json` | 增量重跑的唯一依据 |
| `_llm_config.sh` | 运行环境变量（**含明文密钥，不入版本库**） |

ASR 引擎服务 `transcribe_server.py`（`:8766`）属于 `video-analyzer` 工程，
由 Windows 服务 `VideoAnalyzer-Transcribe`（nssm）托管。

---

## 2. 两个文件的职责边界

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
