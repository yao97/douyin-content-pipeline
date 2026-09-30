# -*- coding: utf-8 -*-
"""跨机转写 · 任务交接（外机与本机**不在同一网络**，唯一通道 = 夸克网盘）

为什么是「只交 raw」而不是「交 md」
--------------------------------
`_asr_registry.json` 是增量重跑的唯一真相，**必须只有一个写者**（项目铁律：长跑进程
整表回写会静默抹掉别处的改动）。所以外机**不写注册表、不出 md**，只产
`_asr_raw/<vid>.json`；回传后由本机 `registry` 重建 + stage2 渲 md。
好处：① 天然无并发冲突 ② 关键词 / LLM 摘要 / 封面规范 100% 一致 ③ raw 只有 ~34KB/条。

体积账（2026-09-30 实测：327 条 / 112.1h）
----------------------------------------
- 源（m4a）：**3.68 GB，且已在夸克**（`自媒体视频库/`）→ 外机直接从网盘拉，本机零上传
- 回传 raw：**~11 MB**（327 × 33.9KB）
- 对照：若改「把超长音频推给远端 8766 算」= **12.03 GB**（459 段 × ~27MB）→ 差 400 倍

全流程（三步）
-------------
1. 本机 `python remote_handoff.py export` → `python remote_handoff.py publish`
   （前者出清单，后者把清单推到夸克 `自媒体视频库/_跨机交接/to_remote/`）
2. 外机 按清单从夸克拉 m4a + 封面 → clone 仓库 + 部署引擎（见 docs/11-跨机转写.md）
   → `python asr_batch.py stage1`（**只产 raw**）→ 把 `_asr_raw/*.json` 打 zip 传回夸克
   `自媒体视频库/_跨机交接/from_remote/`
3. 本机 `python remote_handoff.py fetch --apply`
   = 经 alist 拉最新回传 zip → 校验合入 → 用本机源元数据补全（标题/作者/抖音号/**源路径**/封面路径）
   → 重建注册表 → 提示拉起 stage2

为什么本机走 alist 而不是夸克 CLI
--------------------------------
夸克官方开放平台的令牌是**受限视图**：`browse --parent-fid 0` 只能看到 2 个目录，
够不到 `自媒体视频库`（直接按 fid 访问返回 `12005 文件无权限`）。
而 alist 的 Quark 驱动用的是完整 web 会话，读**和**写都通（采集流水线每天在用）。
→ 所以夸克搬运的读写一律经 alist；夸克客户端只用于外机侧人工下载/上传。

用法
----
  python remote_handoff.py export  [DIR] [--out DIR] [--author 名称] [--limit N]
  python remote_handoff.py publish [--to DIR] [--check]
  python remote_handoff.py fetch   [--from DIR] [--apply] [--force] [--keep]
  python remote_handoff.py import  <DIR|ZIP> [--apply] [--force]
  python remote_handoff.py status

（出口目录 `DIR` 位置写法与 `--out DIR` 等价。`import` / `fetch` 默认**只预演**，
  必须显式 `--apply` 才落盘；`--force` 覆盖已存在且已转写的条目。
  `publish` / `fetch` 默认远端目录 = 夸克 `自媒体视频库/_跨机交接/{to_remote,from_remote}`，
  可用 `WB_QUARK_HANDOFF` 覆盖（相对 alist 根）。）
"""
import os
import sys
import json
import time
import glob
import shutil
import tempfile
import zipfile
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import asr_batch as B          # noqa: E402
import lib_source              # noqa: E402

MANIFEST = "to_remote.json"
README_MD = "外机待转写清单.md"
REMOTE_MD = "外机操作说明.md"

# ── 夸克搬运（经 alist；夸克 CLI 是受限视图，够不到 自媒体视频库）──────────
QUARK_HANDOFF = os.environ.get("WB_QUARK_HANDOFF", "自媒体视频库/_跨机交接")
ALIST_DIR = os.environ.get("WB_ALIST_DIR", r"D:\视频\自媒体视频库")
ALIST_SECRET = os.environ.get("WB_ALIST_SECRET",
                              os.path.join(ALIST_DIR, "_tools", "_secrets", "alist.json"))
QUARK_API = os.environ.get("WB_QUARK_API", "https://drive-pc.quark.cn")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"


def _alist():
    """惰性取 alist 客户端（复用采集侧的 _alist.py，纯标准库）。"""
    if ALIST_DIR not in sys.path:
        sys.path.insert(0, ALIST_DIR)
    import _alist as A
    os.environ.setdefault("ALIST_SECRET_JSON", ALIST_SECRET)
    return A.Alist()



# ────────────────────────── 公共 ──────────────────────────

def _todo():
    """本机视角的「待转写」清单（与 stage1 口径完全一致：lib 源 - 注册表 ok）。"""
    return [t for t in B.build_task_list_src() if not B.is_transcribed(t.get("vid"))]


def _cover_of(task):
    """任务的封面路径（lib 源里就是同目录同名图片）。"""
    c = task.get("cover") or ""
    return c if c and os.path.exists(c) else ""


def _rel(path, root):
    """相对路径（清单里一律用相对路径，外机才能映射到自己的根目录）。"""
    try:
        return os.path.relpath(path, root).replace("\\", "/")
    except Exception:
        return os.path.basename(path or "")


def _size(path):
    try:
        return os.path.getsize(path)
    except Exception:
        return 0


def _quark_dir(local_dir):
    """本机库目录名 → 夸克网盘目录名。

    本机：`UID3303366684053592_魏远麟律师 广州_发布作品/`、`MID…_播客正片合集_合集作品/`
    夸克：`魏远麟律师 广州/`、`播客正片合集/`（**少了 UID/MID 前缀与 _发布作品/_合集作品 后缀**）
    规则以采集侧上传器 `_upload_alist.account_of()` 为权威 —— 这里优先直接调它，避免两处规则漂移。
    """
    try:
        if ALIST_DIR not in sys.path:
            sys.path.insert(0, ALIST_DIR)
        from _upload_alist import account_of
        return account_of(local_dir)
    except Exception:
        parts = local_dir.split("_")
        return parts[1] if len(parts) >= 3 else local_dir


REMOTE_README = """# 外机转写操作说明（跨机交接 · 夸克网盘）

> 由本机 `remote_handoff.py export` 生成于 {ts}。
> 本次任务：**{n} 条 / {hours:.1f} 小时 / 源文件 {gb:.2f} GB**。

## 0. 本目录是什么

本机（跑 md 渲染的那台）把「还没转写的作品」交给你（外机）转写。
本目录即全部输入，一共三个文件：

| 文件 | 用途 |
| --- | --- |
| `to_remote.json` | **机器读**的清单。逐条给出 `vid` / `author` / `pub` / `dur_sec` / `src_rel` / `size` / `cover_rel` |
| `外机待转写清单.md` | **人读**的清单。按作者分组 + 逐条明细，用来核对 |
| `外机操作说明.md` | 本文件 |

`src_rel` / `cover_rel` 是**相对源库根**的路径（形如 `<作者目录>/<文件名>.m4a`），
不含盘符 —— 这样你换到任何盘符都不用改清单。

## 1. 源音频从哪来（⚠️ 目录名对不上，必读）

⚠️ **夸克的目录名 ≠ 本机目录名**，只差前缀和后缀：

| | 目录名 |
| --- | --- |
| **夸克网盘**（`自媒体视频库/` 下） | `魏远麟律师 广州` |
| **本机**（= 清单 `src_rel` 的第一段） | `UID3303366684053592_魏远麟律师 广州_发布作品` |

所以清单里给了**两套路径**，别搞混：

- `src_quark` —— **从哪取**（夸克网盘上相对 `自媒体视频库/` 的位置）
- `src_rel` —— **放到哪**（外机本地相对 `WB_LIB_ROOT` 的位置）
- 封面同理：`cover_quark` / `cover_rel`

### 最省事的做法：先下目录，再改名

**只需下这 {n_dirs} 个夸克目录**（清单里所有条目都落在这几个目录里）：

| 夸克目录 | 下载后改名为 | 条数 | 源体积 |
| --- | --- | ---: | ---: |
{dirs_table}

下载完成后，把 {n_dirs} 个目录**改名**成中间那列（这样 `src_rel` 就能直接对上）：

```powershell
# 在外机执行：把 WB_LIB_ROOT 指向你放 自媒体视频库 的目录
$root = $env:WB_LIB_ROOT
$map = @(
{rename_rows}
)
foreach ($m in $map) {{
    $from = Join-Path $root $m[0]
    $to   = Join-Path $root $m[1]
    if ((Test-Path $from) -and -not (Test-Path $to)) {{ Rename-Item $from $to; Write-Host "改名 $($m[0]) -> $($m[1])" }}
}}
```

- 清单里 **{n} 条**是必需的（{gb:.2f} GB）；整个夸克库是 8.8 GB —— 多下不影响正确性，只是流量
- 封面（`cover_quark`）**一起下**：本机渲 md 时要复制同名封面
- 目录里同名 `.webp` 封面是历史遗留，`.jpeg` 优先，两个都在也无妨

## 2. 环境准备

见公开仓库 `docs/11-跨机转写.md`。要点（`git clone` 远不够）：

1. **引擎**：`Qwen3-ASR-GGUF` 仓库 + 两个模型
   （`Qwen3-ASR-1.7B-gguf` 1.3G、`Qwen3-ForceAligner-0.6B-gguf` 481M）
   ＋ **llama.cpp Windows Vulkan 必须 b8996**（b10229+ 会 access violation）
   ＋ `pip install onnxruntime-directml`（**Windows 独占**）
2. **ffmpeg**（自备，指向 `WB_FFMPEG`）
3. **主程序**：本仓库 `core/03-asr/`

## 3. 环境变量

```bash
export WB_SOURCE=lib
export WB_LIB_ROOT="<外机的 自媒体视频库 绝对路径>"
export WB_OUT_ROOT="<外机产出目录>"
export WB_FFMPEG="<ffmpeg 绝对路径>"
```

`WB_ASR_API` 保持默认 `http://127.0.0.1:8766`（引擎起在本机回环）。
其余 `WB_*` 不需要（旧源 / facts.json / 看板 都是本机专属）。

## 4. 跑转写

```bash
python asr_batch.py stage1
```

- **只跑 stage1**。不要跑 `stage2`（渲 md 由本机做），不要跑 `registry`（注册表只由本机维护）。
- 产出在 `%WB_OUT_ROOT%/_asr_raw/<vid>.json`，一条一个文件。
- **随时可以中断**：已落盘的 raw 就是断点，重跑自动 SKIP。
- 长视频（≥20 分钟）自动切段并合并，断点续跑按段生效——单段失败不会整条白跑。
- 引擎崩溃是常见的（服务端会自愈重启），客户端有重试；看到重试日志属正常，别惊慌。

## 5. 回传

把 `%WB_OUT_ROOT%\\_asr_raw\\` 里**清单中那 {n} 个 vid** 的 json 打成一个 zip。

```powershell
# 在外机执行（PowerShell，假设当前目录是本目录）
$ids   = (Get-Content .\\to_remote.json -Raw | ConvertFrom-Json).items.vid
$src   = "$env:WB_OUT_ROOT\\_asr_raw"
$stage = Join-Path $env:TEMP "from_remote"
Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $stage | Out-Null
Get-ChildItem "$src\\*.json" | Where-Object {{ $ids -contains $_.BaseName }} |
    Copy-Item -Destination $stage
Compress-Archive -Path "$stage\\*" -DestinationPath "$stage.zip" -Force
Write-Host "回传包: $stage.zip  条目 $((Get-ChildItem $stage).Count) / 期望 {n}"
```

然后把 zip 上传到夸克 **`自媒体视频库/_跨机交接/from_remote/`**。

- ✅ 只传 zip，**~{mb:.0f} MB**（raw 平均 34KB/条）
- ❌ 不要传 `_asr_registry.json`
- ❌ 不要传封面（本机自己解析本地源库的封面）
- ❌ 不要传 `_slice_tmp/`（切段临时文件，本机不看）
- 部分失败没关系：转出来的先传，没转出来的留清单下次再跑。**不要伪造内容。**

## 6. 本机侧会做什么

本机 `python remote_handoff.py fetch --apply` 一条命令完成：
经 alist 拉最新 zip → 逐条校验（空文本 / 时长异常的直接拒收）→ 跳过已转写的 →
**用本机源元数据覆盖 `author/pub/title/account/kind/aid/video/cover`**（所以你那条 raw 里的
外机路径会被改回本机路径）→ 原子落盘 → 重建注册表 → stage2 渲 md。

所以你**不需要**关心路径对不对，只要 `vid` 和 `text` 是对的。
"""


def _write_remote_readme(out_dir, payload):
    items = payload.get("items") or []
    dirs = payload.get("dirs") or []
    dirs_table = "\n".join(
        "| `%s` | `%s` | %d | %.2f GB |" % (d.get("quark_dir"), d.get("local_dir"),
                                            d.get("count") or 0,
                                            (d.get("bytes") or 0) / 2 ** 30)
        for d in dirs) or "| — | — | 0 | 0 |"
    rename_rows = "\n".join(
        '    ,@("%s", "%s")' % (d.get("quark_dir"), d.get("local_dir"))
        for d in dirs) or '    ,@("-", "-")'
    txt = REMOTE_README.format(
        ts=payload.get("generated_at") or time.strftime("%Y-%m-%d %H:%M:%S"),
        n=len(items),
        n_dirs=len(dirs),
        hours=(payload.get("total_sec") or 0) / 3600.0,
        gb=(payload.get("total_bytes") or 0) / 2 ** 30,
        mb=len(items) * 34 / 1024.0,
        dirs_table=dirs_table,
        rename_rows=rename_rows,
    )
    p = os.path.join(out_dir, REMOTE_MD)
    with open(p, "w", encoding="utf-8") as f:
        f.write(txt)
    return p



# ────────────────────────── export ──────────────────────────

def cmd_export(out_dir, author=None, limit=None):
    todo = _todo()
    if author:
        todo = [t for t in todo if author in (t.get("author") or "")]
    if limit:
        todo = todo[:limit]
    if not todo:
        print("没有待转写条目（库与注册表已对齐）")
        return 0

    root = lib_source.LIB_ROOT
    items, by_author, dirs = [], {}, {}
    tot_src = tot_sec = 0
    for t in todo:
        src = t.get("video") or ""
        cov = _cover_of(t)
        sz = _size(src)
        tot_src += sz
        sec = float(t.get("dur_csv") or 0)
        tot_sec += sec
        rel = _rel(src, root)
        loc_dir, _, base = rel.partition("/")
        qdir = _quark_dir(loc_dir)              # 本机目录名 → 夸克目录名（少前缀/后缀）
        d = dirs.setdefault(loc_dir, {"local_dir": loc_dir, "quark_dir": qdir,
                                      "count": 0, "bytes": 0})
        d["count"] += 1
        d["bytes"] += sz
        it = {
            "vid": str(t.get("vid") or ""),
            "author": t.get("author") or "",
            "pub": t.get("pub") or "",
            "dur_sec": round(sec, 1),
            "src_rel": rel,                      # 相对 WB_LIB_ROOT（本机目录结构）—— 放到这里
            "src_quark": "%s/%s" % (qdir, base) if qdir and base else "",  # 夸克网盘上的位置 —— 从这取
            "src_name": os.path.basename(src),
            "size": sz,
            "cover_rel": _rel(cov, root) if cov else "",
            "cover_quark": ("%s/%s" % (qdir, os.path.basename(cov))) if (cov and qdir) else "",
            "cover_name": os.path.basename(cov) if cov else "",
        }
        items.append(it)
        by_author.setdefault(it["author"], []).append(it)
    items.sort(key=lambda x: (x["author"], x["vid"]))

    os.makedirs(out_dir, exist_ok=True)
    payload = {
        "schema": 1,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "generated_by": os.environ.get("COMPUTERNAME", "?"),
        "lib_root_local": root,                  # 仅供参考，外机用自己的根
        "note": "外机只需产出 _asr_raw/<vid>.json（不要写注册表、不要出 md），"
                "回传后由本机 registry 重建 + stage2 渲染。",
        "count": len(items),
        "total_sec": round(tot_sec, 1),
        "total_bytes": tot_src,
        "dirs": sorted(dirs.values(), key=lambda x: -x["count"]),
        "items": items,
    }
    mpath = os.path.join(out_dir, MANIFEST)
    with open(mpath, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)

    lines = [
        "# 外机待转写清单",
        "",
        "> 由本机 `remote_handoff.py export` 生成于 %s。**共 %d 条 / %.1f 小时 / 源文件 %.2f GB**。" % (
            payload["generated_at"], len(items), tot_sec / 3600, tot_src / 2 ** 30),
        "",
        "## 外机要做的三件事",
        "",
        "1. **拉源**：按 `%s` 里的 `src_rel` / `cover_rel`（相对库根），从夸克网盘" % MANIFEST,
        "   `自媒体视频库/` 下拉同名 m4a 与封面，放到外机自己的库根下（保持同目录结构）。",
        "2. **跑转写**：`python asr_batch.py stage1`（长视频会自动切片），**只产 raw**。",
        "   ⚠️ 不要跑 `registry`、不要跑 `stage2` —— 注册表与 md 的写者只能是本机。",
        "3. **回传**：把外机 `_asr_raw/` 里**新增**的 `<vid>.json` 打包传回夸克。",
        "   （只有 ~%.0f MB —— raw 平均 34KB/条，别传 wav / 切片临时目录）"
        % (len(items) * 34 / 1024.0),
        "",
        "## 分作者",
        "",
        "| 作者 | 条数 | 音频时长 | 源体积 |",
        "| --- | ---: | ---: | ---: |",
    ]
    for a, arr in sorted(by_author.items(), key=lambda kv: -len(kv[1])):
        lines.append("| %s | %d | %.1f h | %.2f GB |" % (
            a, len(arr), sum(x["dur_sec"] for x in arr) / 3600,
            sum(x["size"] for x in arr) / 2 ** 30))
    lines += ["", "## 明细", "", "| # | 作品ID | 作者 | 时长(min) | 源文件 | 封面 |", "| ---: | --- | --- | ---: | --- | --- |"]
    for i, it in enumerate(items, 1):
        lines.append("| %d | %s | %s | %.1f | %s | %s |" % (
            i, it["vid"], it["author"], it["dur_sec"] / 60,
            it["src_name"] or "(缺)", it["cover_name"] or "-"))
    rpath = os.path.join(out_dir, README_MD)
    with open(rpath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    gpath = _write_remote_readme(out_dir, payload)

    print("导出完成：%d 条 / %.1f h / 源 %.2f GB" % (len(items), tot_sec / 3600, tot_src / 2 ** 30))
    print("  清单   : %s" % mpath)
    print("  说明书 : %s" % rpath)
    print("  操作说明: %s" % gpath)
    print("  回传预计：raw 约 %.1f MB" % (len(items) * 34 / 1024.0))
    return 0


# ────────────────────────── import ──────────────────────────

def _find_raw_files(src_dir):
    """目录（可嵌套）里所有像转写结果的 json。"""
    out = []
    for p in glob.glob(os.path.join(src_dir, "**", "*.json"), recursive=True):
        base = os.path.basename(p)
        if base in (MANIFEST,) or base.startswith("_"):
            continue
        out.append(p)
    return sorted(out)


def cmd_import(src_dir, apply=False, force=False, _tmp_owner=None):
    # 支持直接喂 zip（外机回传的通常是压缩包）
    if os.path.isfile(src_dir) and src_dir.lower().endswith(".zip"):
        tmp = tempfile.mkdtemp(prefix="from_remote_")
        try:
            with zipfile.ZipFile(src_dir) as z:
                bad = [n for n in z.namelist()
                       if n.startswith("/") or ".." in n.replace("\\", "/").split("/")]
                if bad:
                    print("zip 内含非法路径，拒绝解压：%s" % bad[:3])
                    return 2
                z.extractall(tmp)
            print("已解压 %s → %s" % (os.path.basename(src_dir), tmp))
            rc = cmd_import(tmp, apply=apply, force=force)
        finally:
            if _tmp_owner is None:
                shutil.rmtree(tmp, ignore_errors=True)
        return rc
    if not os.path.isdir(src_dir):
        print("目录不存在: %s" % src_dir)
        return 2
    files = _find_raw_files(src_dir)
    if not files:
        print("在 %s 里没找到任何 .json（应指向解压后的 _asr_raw 目录）" % src_dir)
        return 2

    # 本机源元数据（用本机完整 Data/*.csv + facts.json 补全标题/作者/抖音号/封面）
    meta = {}
    for t in B.build_task_list_src():
        meta[str(t.get("vid"))] = t

    new, dup, bad, nocover = [], [], [], []
    for p in files:
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception as e:
            bad.append((os.path.basename(p), "JSON 解析失败: %s" % e))
            continue
        vid = str(d.get("vid") or os.path.splitext(os.path.basename(p))[0])
        if not vid.isdigit():
            bad.append((os.path.basename(p), "拿不到作品ID"))
            continue
        ok, why = B.validate_raw(d)
        if not ok:
            bad.append((vid, why))
            continue
        if B.is_transcribed(vid) and not force:
            dup.append(vid)
            continue
        d["vid"] = vid
        # 用本机元数据覆盖（外机可能没有 Data/*.csv / facts.json）：
        # · author/pub/title/account/kind → md 头部
        # · aid/video → 源溯源（外机给的是**外机路径**，不换回本机路径则 raw 记录失真）
        # · cover → 必须换回本机真实存在的封面，stage2 靠 os.path.exists 决定复不复制
        t = meta.get(vid)
        if t:
            for k in ("author", "pub", "title", "account", "kind", "aid", "video"):
                if t.get(k):
                    d[k] = t[k]
            c = _cover_of(t)
            if c:
                d["cover"] = c
            else:
                nocover.append(vid)
        else:
            nocover.append(vid)
        new.append((vid, d, p))

    print("扫描 %d 个 json：新增可用 %d / 本机已转写 %d / 不合格 %d"
          % (len(files), len(new), len(dup), len(bad)))
    for vid, why in bad[:10]:
        print("   ✗ %s: %s" % (vid, why))
    if bad[10:]:
        print("   … 另有 %d 条不合格" % (len(bad[10:])))

    if nocover:
        print("   [!] %d 条在本机找不到封面（渲出的 md 会没有封面图）：%s%s"
              % (len(nocover), "、".join(nocover[:5]),
                 " ..." if len(nocover) > 5 else ""))

    if not apply:
        print("\n（dry-run）加 --apply 才会写入 %s 并重建注册表" % B.RAW_DIR)
        return 0
    n = 0
    for vid, d, p in new:
        dst = os.path.join(B.RAW_DIR, vid + ".json")
        tmp = dst + ".part"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, dst)          # 原子落盘：stage2 轮询时不会读到半截文件
        n += 1
    print("\n合入 %d 条 → %s" % (n, B.RAW_DIR))

    if n:
        for vid, d, _p in new:
            _register_one(vid, d)     # 逐条登记（并发安全，见函数注释）
        print("注册表已登记 %d 条（逐条 update_entry，stage1 在跑也不冲突）" % n)
        print("\n下一步（缺一步都不会出 md）：")
        print("  python _stage2_daemon.py    # 渲染 raw → md（关键词 + LLM 摘要 + 同名封面）")
        print("  ⚠️ stage2 若已在跑会自动捡到新 raw，不必手工干预。")
    return 0


def _register_one(vid, d):
    """把一条新合入的 raw 登记进 `_asr_registry.json`。

    ⚠️ **这里必须用 `update_entry()`，不能用 `B.build_registry()`**（2026-10-01 改）。
    `build_registry()` 是「扫描 _asr_raw 全量 → `save_registry()` 整表覆盖」，在 stage1
    正在跑的时候用它有两个真实风险：

    1. **抹掉管理字段**：它每条只写 7 个基础字段（ok/author/pub/duration/chars/job_id/ts），
       `abandoned` / `why` / `low_content` / `attempts` / `retry_rounds` / `note` / `sliced`
       全部丢失 → 已被判「放弃」的作品会被当成没转过，跑批重转一遍（代价不可逆）。
    2. **丢新增**：整表覆盖发生在「扫描快照」之后，扫描窗口内 stage1 刚 `update_entry()`
       写入的条目会被这份旧快照盖掉。

    `update_entry()` 的正确姿势是「先重读磁盘 → 只改自己那条 → 原子写回」，与 stage1 的
    写路径同构，谁都不会盖谁。需要全量重建时用 `asr_batch.py registry`（**跑批停止时**执行）。
    """
    B.update_entry(vid, {
        "ok": True,
        "author": d.get("author", ""),
        "pub": d.get("pub", ""),
        "duration": d.get("duration", 0),
        "chars": len((d.get("text") or "").strip()),
        "job_id": d.get("job_id", ""),
        "ts": B.datetime.now(B.CST).strftime("%Y-%m-%d %H:%M:%S"),
    }, remove=("abandoned", "why", "note"), )


# ────────────────── 夸克搬运（经 alist；CLI 够不到源库）──────────────────

def _remote_paths():
    base = QUARK_HANDOFF.strip("/")
    return "/" + base + "/to_remote", "/" + base + "/from_remote"


def _open_alist():
    try:
        return _alist(), None
    except Exception as e:
        return None, ("连不上 alist：%s\n"
                      "  alist 是夸克读写的**唯一**通道 —— 夸克官方 CLI 的令牌是受限视图，\n"
                      "  `browse --parent-fid 0` 只看到 2 个目录，按 fid 直取返回 12005 文件无权限。\n"
                      "  检查 alist 是否在跑：curl --noproxy '*' http://127.0.0.1:5244/ping" % e)


def _alist_download(A, remote_path, dst):
    """下载 alist 里的一个文件。

    ⚠️ 不能用 alist 的 `/d/` 直链：这些 Quark 存储的 raw_url 指向夸克 CDN
       （`dl-pc-zb.pds.quark.cn`），实测被拦 —— `RequestDeniedByCallback` /
       `Callback deny this request reason: require login`（**既有问题，与本工具无关**；
       alist 的上传/列目录是好的，只有下载直链不通）。
    可用通道：**alist `fs/get` 拿到的 `id` 就是夸克的文件 FID**，拿它调夸克 web 的
    `file/download` 换一次性直链，再带上夸克 cookie 取流。实测 200 + 完整字节。
    """
    d = A.api("/api/fs/get", {"path": remote_path, "password": ""})
    if d.get("code") != 200:
        raise RuntimeError("fs/get %s: %s" % (remote_path, d.get("message")))
    fid = (d.get("data") or {}).get("id")
    if not fid:
        raise RuntimeError("alist 没给出文件 id（%s）" % remote_path)
    ck = _quark_cookie_for(remote_path, A)
    url = _quark_dl_url(fid, ck)
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, headers={"Cookie": ck, "User-Agent": UA})
    got = 0
    with op.open(req, timeout=1800) as r, open(dst, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
    return got


def _quark_cookie_for(path, A):
    """取挂载点覆盖该路径的 Quark 存储的 cookie（夸克 web 会话）。"""
    d = A.api("/api/admin/storage/list", None, "GET")
    best = None
    for s in ((d.get("data") or {}).get("content") or []):
        if s.get("driver") != "Quark":
            continue
        m = (s.get("mount_path") or "/").rstrip("/") or "/"
        if path == m or path.startswith(m + "/"):
            if best is None or len(m) > len(best[0]):
                best = (m, s)
    if best is None:
        raise RuntimeError("没找到覆盖 %s 的 Quark 存储" % path)
    try:
        a = json.loads(best[1].get("addition") or "{}")
    except Exception:
        a = {}
    ck = (a.get("cookie") or "").strip()
    if not ck:
        raise RuntimeError("Quark 存储 %s 没有 cookie" % best[0])
    return ck


def _quark_dl_url(fid, cookie):
    """夸克 web：文件 FID → 一次性下载直链。"""
    body = json.dumps({"fids": [fid]}).encode()
    req = urllib.request.Request(
        QUARK_API + "/1/clouddrive/file/download?pr=ucpro&fr=pc", data=body,
        headers={"Cookie": cookie, "Content-Type": "application/json",
                 "User-Agent": UA})
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with op.open(req, timeout=60) as r:
            j = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raise RuntimeError("夸克 file/download HTTP %s" % e.code)
    if j.get("code") != 0:
        raise RuntimeError("夸克 file/download code=%s msg=%s"
                           % (j.get("code"), j.get("message")))
    lst = j.get("data") or []
    url = (lst[0] if isinstance(lst, list) and lst else {}).get("download_url") or ""
    if not url:
        raise RuntimeError("夸克没返回 download_url（cookie 可能已过期）")
    return url


def cmd_publish(to_dir=None, check=False):
    """把清单（清单 json + 两份说明）推到夸克，供外机拉取。"""
    out_dir = os.path.join(HERE, "_handoff", "to_remote")
    need = [MANIFEST, README_MD, REMOTE_MD]
    miss = [n for n in need if not os.path.exists(os.path.join(out_dir, n))]
    if miss:
        print("本地清单不齐（缺 %s）。先跑：python remote_handoff.py export" % "、".join(miss))
        return 2
    dest, _ = _remote_paths()
    if to_dir:
        dest = to_dir
    A, err = _open_alist()
    if err:
        print(err)
        return 2

    if check:
        try:
            cur = A.list_dir(dest, refresh=True)
        except Exception as e:
            print("远端目录读取失败 %s：%s" % (dest, e))
            return 2
        print("远端 %s 现有 %d 项：" % (dest, len(cur)))
        for it in cur:
            print("   %s%s" % ("[D] " if it.get("is_dir") else "    ", it.get("name")))
        local_new = max(os.path.getmtime(os.path.join(out_dir, n)) for n in need)
        names = {it.get("name") for it in cur}
        stale = [n for n in need if n not in names]
        print("本地清单最新修改：%s" % time.strftime("%Y-%m-%d %H:%M:%S",
                                                   time.localtime(local_new)))
        print("远端缺少：" + ("、".join(stale) if stale else "无（看起来已是最新）"))
        return 0

    r = A.ensure_dir(dest)
    print("远端目录 %s%s" % (dest, "（新建）" if r else "（已存在）"))
    # 顺便把回传目录也建好，外机上传时不用自己找位置
    _to2, back = _remote_paths()
    try:
        A.ensure_dir(back)
    except Exception as e:
        print("   ⚠️ 回传目录 %s 创建失败：%s" % (back, e))
    total = 0
    for n in need:
        lp = os.path.join(out_dir, n)
        sz = os.path.getsize(lp)
        try:
            ok, msg = A.put_file(lp, dest + "/" + n)
        except Exception as e:
            ok, msg = False, e
        if ok:
            total += sz
            print("   OK   %-24s %8.1f KB" % (n, sz / 1024.0))
        else:
            print("   FAIL %-24s %s" % (n, msg))
            return 1
    print("\n已发布到夸克：%s（%d 个文件 / %.1f KB）" % (dest, len(need), total / 1024.0))
    print("外机打开夸克 → %s 即可取走清单。" % QUARK_HANDOFF.replace("/", " / "))
    return 0


def cmd_fetch(from_dir=None, apply=False, force=False, pick=None):
    """经 alist 从夸克拉回最新回传包并导入（本机侧一键收包）。"""
    _to, src = _remote_paths()
    if from_dir:
        src = from_dir
    A, err = _open_alist()
    if err:
        print(err)
        return 2
    try:
        items = A.list_dir(src, refresh=True)
    except Exception as e:
        print("读远端目录失败 %s：%s" % (src, e))
        return 2

    zips = [it for it in items
            if not it.get("is_dir") and (it.get("name") or "").lower().endswith(".zip")
            and not (it.get("name") or "").startswith(".")]
    if pick:
        zips = [it for it in zips if pick in (it.get("name") or "")]
    if not zips:
        print("远端 %s 里没有 .zip（外机还没回传？）" % src)
        for it in items[:20]:
            print("   %s%s" % ("[D] " if it.get("is_dir") else "    ", it.get("name")))
        return 2
    zips.sort(key=lambda it: (it.get("modified") or "", it.get("name") or ""), reverse=True)
    tgt = zips[0]
    name = tgt.get("name")
    size = tgt.get("size") or 0
    print("远端 %s 有 %d 个回传包，取最新：%s（%.2f MB，%s）"
          % (src, len(zips), name, size / 2 ** 20, tgt.get("modified") or "?"))

    dl = os.path.join(HERE, "_handoff", "from_remote")
    os.makedirs(dl, exist_ok=True)
    local = os.path.join(dl, name)
    try:
        got = _alist_download(A, src + "/" + name, local)
    except Exception as e:
        print("下载失败：%s" % e)
        return 2
    print("已下载 → %s（%.2f MB）" % (local, got / 2 ** 20))

    if not apply:
        print("\n（dry-run）下面是预演；确认无误后加 --apply 真正合入")
    return cmd_import(local, apply=apply, force=force)


# ────────────────────────── status ──────────────────────────

def cmd_status():
    todo = _todo()
    raw = len(glob.glob(os.path.join(B.RAW_DIR, "*.json")))
    ok = sum(1 for m in B.load_registry().values() if m.get("ok"))
    sec = sum(float(t.get("dur_csv") or 0) for t in todo)
    print("本机库任务   : %d" % len(B.build_task_list_src()))
    print("注册表 ok    : %d" % ok)
    print("待转写       : %d 条 / %.1f h" % (len(todo), sec / 3600))
    print("_asr_raw     : %d 个 json" % raw)
    print("夸克交接目录 : %s/{to_remote,from_remote}（经 alist）" % QUARK_HANDOFF)
    hd = os.path.join(HERE, "_handoff")
    if os.path.isdir(hd):
        for d in sorted(glob.glob(os.path.join(hd, "*"))):
            mf = os.path.join(d, MANIFEST)
            if os.path.exists(mf):
                try:
                    j = json.load(open(mf, encoding="utf-8"))
                    print("最近导出     : %s → %d 条（%s）"
                          % (os.path.basename(d), j.get("count", 0), j.get("generated_at", "")))
                except Exception:
                    pass
    return 0


def main():
    argv = sys.argv[1:]
    mode = argv[0] if argv else "status"
    rest = argv[1:]

    # 严格分词：`--flag value` 的 value 归入 opts，不再漏进 positional
    #（旧实现 `args = [a for a in rest if not a.startswith("--")]` 会把 --out 的值
    #  也当成位置参数，导致 `export <dir>` 的位置写法被**静默忽略** → 写到默认目录）
    positional, flags, opts = [], set(), {}
    i = 0
    while i < len(rest):
        a = rest[i]
        if a.startswith("--"):
            if i + 1 < len(rest) and not rest[i + 1].startswith("--"):
                opts[a] = rest[i + 1]
                i += 2
                continue
            flags.add(a)
        else:
            positional.append(a)
        i += 1
    args = positional

    def opt(name, default=None):
        return opts.get(name, default)

    if mode == "export":
        out = opt("--out") or (args[0] if args else None) \
            or os.path.join(HERE, "_handoff", "to_remote")
        lim = opt("--limit")
        return cmd_export(out, opt("--author"), int(lim) if lim else None)
    if mode == "publish":
        return cmd_publish(opt("--to"), check=("--check" in flags))
    if mode == "fetch":
        return cmd_fetch(opt("--from"), apply=("--apply" in flags),
                         force=("--force" in flags), pick=opt("--pick"))
    if mode == "import":
        if not args:
            print("用法: python remote_handoff.py import <解压后的 _asr_raw 目录|zip> [--apply]")
            return 2
        return cmd_import(args[0], apply=("--apply" in flags), force=("--force" in flags))
    if mode == "status":
        return cmd_status()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
