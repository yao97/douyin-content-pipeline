# -*- coding: utf-8 -*-
"""Windows → POSIX 路径 / 平台兼容层（**容器专用**，仅由 sitecustomize 注入）。

## 为什么需要它

实盘脚本（`自媒体视频库/_*.py`、`媒体知识库/asr_batch.py` 等）里到处是**硬编码的
Windows 绝对路径**，例如：

    ROOT   = pathlib.Path(r"D:\\视频\\自媒体视频库")
    OUT_ROOT = r"D:\\视频\\媒体知识库"
    FFMPEG = r"C:\\Users\\EDY\\Projects\\video-analyzer\\...\\ffmpeg-win-x86_64-v7.1.exe"

本机（Windows）跑得好好的，进 Linux 容器后这些路径全部不存在。而项目有一条硬规矩：
**实盘脚本是唯一编辑入口，不允许为了容器化去分叉/改动实盘代码**（改了就要走评审，
且每日 18:00 的生产任务在跑）。所以容器适配必须 100% 留在这里。

## 它做了什么

在**解释器启动时**（`sitecustomize` → `activate()`）打一层薄补丁，把「以已知 Windows 前缀
开头的路径字符串」在**进入系统调用前**改写成挂载点下的 POSIX 路径。承载面共 8 处，
覆盖了实盘脚本的全部用法：

| # | 挂钟点 | 覆盖的写法 |
|---|---|---|
| 1 | `pathlib.PurePath.__init__` | `Path(r"D:\\…")`、`Path(r"D:\\…")/"x"`、`.rglob()/.iterdir()/.exists()` |
| 2 | `os.path.join` | `os.path.join(r"C:\\…", "x")` |
| 3 | `os.makedirs/remove/unlink/rename/replace/stat/listdir/scandir/walk/mkdir` + `os.path.{exists,isfile,isdir,getsize,getmtime,islink}` | 裸字符串字面量 |
| 4 | `builtins.open` | `open(OUT_ROOT + "/x")`（字符串拼接绕过了 os.path.join） |
| 5 | `glob.glob` / `glob.iglob` | `glob.glob(OUT_ROOT + "/*.json")` |
| 6 | `shutil.{disk_usage,copy2,copy,copytree,move,rmtree}` | `free_gb()` 的 `shutil.disk_usage(r"D:\\")` |
| 7 | `subprocess.Popen.__init__`（`run/call/check_output` 都经它） | `[FFMPEG, "-i", …]`、`[SERVER_PY, "transcribe_server.py", …]`、`cwd=` |
| 8 | `ctypes.windll` → 假 kernel32 | `DeleteFileW`（硬删）、`OpenProcess/CloseHandle`（PID 存活判断） |

## 映射规则（前缀表）

| Windows 前缀 | 环境变量 | 容器默认值 |
|---|---|---|
| `D:\\视频\\自媒体视频库` | `WB_VIDEO_LIB` | `/mnt/video-lib` |
| `D:\\视频\\媒体知识库` | `WB_MEDIA_KB` | `/mnt/media-kb` |
| `D:\\视频\\自媒体脚本知识库` | `WB_KB_REPO` | `/mnt/kb-repo` |
| `D:\\alist` | `WB_ALIST_DIR` | `/mnt/alist` |
| `C:\\Users\\EDY\\Videos\\data` | `WB_VIDEO_DATA` | `/mnt/videos-data` |
| `C:\\Users\\EDY\\Projects\\video-analyzer` | `WB_VCA_DIR` | `/mnt/vca` |

另有 3 条**正则规则**（优先级高于前缀表，因为要落到不同的目标上）：

* `…/imageio_ffmpeg/binaries/ffmpeg-win-*.exe` → `WB_FFMPEG`（默认 `/usr/bin/ffmpeg`）
  —— 两个脚本各自硬编码了**不同**的 Windows ffmpeg 路径，用前缀表会落到不存在的文件上。
* `…/venv/Scripts/python.exe`（含 `.venv` / `asr_venv`）→ `WB_PYTHON`（默认 `sys.executable`）
  —— `asr_batch.ensure_server()` 的自救分支与 `_dl_mix.py` 的 `VENV_PY` 会 exec 它；
  它们指向的是宿主那份 **Windows PE 解释器**，直接 exec 会 `ENOEXEC`。
  注意这一条**两种形态都要管**：带盘符的（`C:\…\venv\Scripts\python.exe`）走正则，
  已被前缀表改写成 POSIX 形态的（`/mnt/video-lib/…/.venv/Scripts/python.exe`）
  在快速路径里用 `endswith('/Scripts/python.exe')` 兜住。
* 裸盘符 `D:\\` / `D:` → `WB_DATA_ROOT`（默认 `/`）
  —— `_pipeline.free_gb(path=r"D:\\")` 的磁盘预检。

## 安全边界（重要）

* **仅当 `os.name != "nt"` 时生效**；在 Windows 上 `activate()` 是空操作，可安全无害加载。
* `rewrite()` 对不含盘符的字符串**零开销直返**（`":" not in p[:4]` 快速路径），
  因此 `--flags`、`http://host:port` 之类的参数不会被误改（`http` 前 4 字符无冒号）。
* 命中前缀但目标是 symlink/不存在时**原样返回**，绝不静默指向别处 —— 让脚本自己报
  「文件不存在」，比悄悄改到错误位置更容易排查。
* 补丁是**幂等**的，被 sitecustomize 重复调用也无副作用。
* 需要看它到底改了什么：`WB_COMPAT_VERBOSE=1`（每条改写打一行到 stderr，最多 500 条）。

## 不覆盖的用法（写新脚本时注意）

* 把 Windows 路径塞进**第三方库**内部（如某个库自己读注册表/配置文件）——修不了，也不需要。
* `os.execv` / `os.spawn*`（实盘脚本没用到；要用请先手动 `rewrite()`）。
* 直接传给 C 扩展的字节串路径。

自检：`python -m wb_compat --selftest`（或 `/opt/wb/scripts/wbctl.sh selftest`）。
"""

from __future__ import annotations

import os
import re
import sys

__all__ = ["rewrite", "activate", "deactivate", "describe", "MAP"]

# ── 前缀表：(Windows 前缀, 环境变量名, 容器默认值) ──────────────────────────
_PREFIX_TABLE = [
    (r"D:\视频\自媒体视频库", "WB_VIDEO_LIB", "/mnt/video-lib"),
    (r"D:\视频\媒体知识库", "WB_MEDIA_KB", "/mnt/media-kb"),
    (r"D:\视频\自媒体脚本知识库", "WB_KB_REPO", "/mnt/kb-repo"),
    (r"D:\alist", "WB_ALIST_DIR", "/mnt/alist"),
    (r"C:\Users\EDY\Videos\data", "WB_VIDEO_DATA", "/mnt/videos-data"),
    (r"C:\Users\EDY\Projects\video-analyzer", "WB_VCA_DIR", "/mnt/vca"),
]

# 裸盘符：D:\ / D: / D:/ / D:\\ （注意 `_pipeline.free_gb(path=r"D:\\")` 是**双**反斜杠）
_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]*$")

# 正则规则：(图案, 环境变量名, 默认值)
_REGEX_RULES = [
    (re.compile(r"imageio[_-]ffmpeg[\\/]binaries[\\/]ffmpeg-win-[^\\/]*\.exe$", re.I),
     "WB_FFMPEG", "/usr/bin/ffmpeg"),
    (re.compile(r"[\\/]\.?(?:asr_)?venv[\\/]Scripts[\\/]python\.exe$", re.I),
     "WB_PYTHON", None),  # None => sys.executable
]

# 供 --selftest / 文档展示
MAP = {win: (env, default) for win, env, default in _PREFIX_TABLE}

_VERBOSE = os.environ.get("WB_COMPAT_VERBOSE", "") == "1"
_verbose_budget = 500
_activated = False


# ────────────────────────────── 核心改写 ──────────────────────────────
def _resolve(env_name: str, default):
    v = os.environ.get(env_name)
    if v:
        return v.rstrip("/") or "/"
    if env_name == "WB_PYTHON":
        return sys.executable
    return default


def rewrite(p):
    """把 Windows 路径改写成容器挂载点路径。

    * 非 str / 无盘符字符串 → **原样返回**（零成本）
    * 命中规则 → 返回改写后的 POSIX 字符串
    * 未命中任何规则 → 原样返回
    """
    if not isinstance(p, str):
        return p
    # 快速路径：Windows 绝对路径一定在很靠前的位置出现盘符 + 反斜杠
    head = p[:4]
    if ":" not in head and "\\" not in head:
        # ── POSIX 形态：绝大多数直接放行（零成本），只有一个例外必须强制改写 ──
        # 实盘脚本会把**宿主 Windows venv 里的解释器**拼成路径再 exec：
        #   _dl_mix.py:  VENV_PY = TOOL/".venv"/"Scripts"/"python.exe"
        # 这个路径经过前缀表后已经是 POSIX 形态（/mnt/video-lib/…/.venv/Scripts/python.exe），
        # 但它指向的是宿主那份 **Windows PE 二进制** —— 直接 exec 会 ENOEXEC
        # （Exec format error）。改指容器自己的解释器才对。
        # 安全性：POSIX 虚拟环境的目录名是 `bin/`，只有 Windows venv 才叫 `Scripts/`，
        # 所以这个 suffix 判断不会误伤。
        if p.endswith("/Scripts/python.exe"):
            tgt = _resolve("WB_PYTHON", None)
            _trace(p, tgt, "posix-venv")
            return tgt
        return p

    # 1) 正则规则优先
    for pat, env_name, default in _REGEX_RULES:
        if pat.search(p):
            tgt = _resolve(env_name, default)
            _trace(p, tgt, "regex")
            return tgt

    # 2) 裸盘符
    if _DRIVE_RE.match(p):
        tgt = _resolve("WB_DATA_ROOT", "/")
        _trace(p, tgt, "drive")
        return tgt

    # 3) 前缀表（最长前缀优先）
    norm = p.replace("/", "\\")
    low = norm.lower()
    for win, env_name, default in sorted(_PREFIX_TABLE, key=lambda r: -len(r[0])):
        wl = win.lower()
        if low == wl:
            tgt = _resolve(env_name, default)
            _trace(p, tgt, "prefix")
            return tgt
        if low.startswith(wl + "\\"):
            tail = norm[len(win) + 1:]
            tgt = _resolve(env_name, default).rstrip("/") + "/" + tail.replace("\\", "/")
            _trace(p, tgt, "prefix")
            return tgt

    return p


def _trace(src, dst, kind):
    global _verbose_budget
    if not _VERBOSE or _verbose_budget <= 0:
        return
    _verbose_budget -= 1
    extra = "" if _verbose_budget else "  （后续改写不再打印）"
    print(f"[wb_compat:{kind}] {src} -> {dst}{extra}", file=sys.stderr, flush=True)


# ────────────────────────────── 补丁工厂 ──────────────────────────────
def _wrap_path_fn(fn):
    """包装「第一个位置参数是路径」的函数。"""
    def wrapper(path, *a, **kw):
        return fn(rewrite(path), *a, **kw)
    wrapper.__name__ = getattr(fn, "__name__", "wrapper")
    wrapper.__doc__ = getattr(fn, "__doc__", None)
    wrapper.__wrapped__ = fn
    return wrapper


def _wrap_join(fn):
    """⚠ 必须「先 join 再 rewrite」，不能逐参数 rewrite。

    反例（本机自检抓到的真 bug）：`os.path.join(r"C:\\…\\video-analyzer\\backend",
    "venv", "Scripts", "python.exe")` —— 若先改写第一个参数，得到的已是 POSIX 前缀
    `/mnt/vca/backend`，后续 regex 规则（venv/Scripts/python.exe → 容器解释器）
    **永远不可能命中**，`asr_batch.ensure_server()` 的自救分支就会指向不存在的文件。
    先 join 再改写则整串仍带 Windows 前缀，regex 能正常命中。
    """
    def wrapper(*parts):
        return rewrite(fn(*parts))
    wrapper.__name__ = "join"
    wrapper.__wrapped__ = fn
    return wrapper


def _wrap_open(fn):
    def wrapper(file, *a, **kw):
        return fn(rewrite(file), *a, **kw)
    wrapper.__name__ = "open"
    wrapper.__wrapped__ = fn
    return wrapper


def _wrap_glob(fn):
    def wrapper(pathname, *a, **kw):
        return fn(rewrite(pathname), *a, **kw)
    wrapper.__name__ = getattr(fn, "__name__", "glob")
    wrapper.__wrapped__ = fn
    return wrapper


def _wrap_popen(cls):
    """subprocess.Popen 是类，需包 __init__（run/call/check_output 都经它）。"""
    orig = cls.__init__

    def __init__(self, args, *a, **kw):
        args = _rewrite_argv(args)
        if isinstance(kw.get("cwd"), str):
            kw["cwd"] = rewrite(kw["cwd"])
        if isinstance(kw.get("executable"), str):
            kw["executable"] = rewrite(kw["executable"])
        orig(self, args, *a, **kw)

    cls.__init__ = __init__


def _rewrite_argv(args):
    if isinstance(args, (list, tuple)):
        return type(args)(rewrite(x) if isinstance(x, str) else x for x in args)
    return rewrite(args) if isinstance(args, str) else args


# ────────────────────────── ctypes.windll 假内核 ──────────────────────────
class _Kernel32:
    """只实现实盘脚本真正调到的三个 Win32 API，语义对齐 Windows 返回值。"""

    @staticmethod
    def DeleteFileW(lpFileName):  # noqa: N802 - 保持 Win32 命名
        """成功返回非 0，失败返回 0（与 Windows 一致）。"""
        try:
            os.remove(lpFileName)
            return 1
        except OSError:
            return 0

    @staticmethod
    def OpenProcess(dwDesiredAccess, bInheritHandle, dwProcessId):  # noqa: N802
        """PID 存活 → 返回非 0 假句柄；不存在 → 0。"""
        try:
            os.kill(int(dwProcessId), 0)
        except ProcessLookupError:
            return 0
        except PermissionError:
            return 1  # 进程存在但无权限 → Windows 也会给出句柄
        except Exception:
            return 0
        return 1

    @staticmethod
    def CloseHandle(hObject):  # noqa: N802
        return 1


class _Windll:
    kernel32 = _Kernel32()


# ────────────────────────────── 激活 ──────────────────────────────
def activate(force=False) -> bool:
    """打上全部补丁。返回是否实际生效（Windows 上返回 False）。

    在 Windows 上可用 `WB_COMPAT_FORCE=1` 强制激活 —— 仅供**本机验证补丁是否挂上**
    （路径语义不同，映射结果不可当真）。
    """
    global _activated
    if _activated and not force:
        return True
    if os.name == "nt" and not force and os.environ.get("WB_COMPAT_FORCE") != "1":
        return False

    import builtins
    import glob as _glob
    import pathlib
    import shutil
    import subprocess

    # 0) 环境默认值补齐（让脚本内部/子进程也能看到统一的挂载点）
    for win, env_name, default in _PREFIX_TABLE:
        os.environ.setdefault(env_name, default)
    os.environ.setdefault("WB_FFMPEG", "/usr/bin/ffmpeg")
    os.environ.setdefault("WB_DATA_ROOT", "/")

    # 1) pathlib
    pathlib.PurePath.__init__ = _wrap_path_fn(pathlib.PurePath.__init__)

    # 2) os.path.join
    os.path.join = _wrap_join(os.path.join)

    # 3) os 家族
    for name in ("makedirs", "mkdir", "remove", "unlink", "rmdir", "rename",
                 "replace", "stat", "listdir", "scandir", "chmod", "utime"):
        if hasattr(os, name):
            setattr(os, name, _wrap_path_fn(getattr(os, name)))
    for name in ("exists", "isfile", "isdir", "islink", "getsize", "getmtime",
                 "getctime", "getatime", "realpath", "abspath"):
        if hasattr(os.path, name):
            setattr(os.path, name, _wrap_path_fn(getattr(os.path, name)))
    # os.walk 的 top 参数
    if hasattr(os, "walk"):
        _owalk = os.walk
        os.walk = lambda top, *a, **kw: _owalk(rewrite(top), *a, **kw)

    # 4) open
    builtins.open = _wrap_open(builtins.open)

    # 5) glob
    _glob.glob = _wrap_glob(_glob.glob)
    _glob.iglob = _wrap_glob(_glob.iglob)

    # 6) shutil
    for name in ("disk_usage", "copy2", "copy", "copytree", "move", "rmtree", "copyfile"):
        if hasattr(shutil, name):
            setattr(shutil, name, _wrap_path_fn(getattr(shutil, name)))

    # 7) subprocess.Popen
    _wrap_popen(subprocess.Popen)

    # 8) ctypes.windll
    import ctypes
    if not hasattr(ctypes, "windll"):
        ctypes.windll = _Windll()  # type: ignore[attr-defined]

    _activated = True
    return True


def deactivate() -> None:
    """仅供自检使用：把补丁摘掉（不还原已改写的字符串）。"""
    global _activated
    import builtins
    import glob as _glob
    import pathlib
    import shutil
    import subprocess

    for obj, name in ((pathlib.PurePath, "__init__"), (os.path, "join"),
                      (builtins, "open"), (_glob, "glob"), (_glob, "iglob"),
                      (subprocess.Popen, "__init__")):
        fn = getattr(obj, name)
        if hasattr(fn, "__wrapped__"):
            setattr(obj, name, fn.__wrapped__)
    for mod, names in ((os, ("makedirs", "mkdir", "remove", "unlink", "rmdir",
                             "rename", "replace", "stat", "listdir", "scandir",
                             "chmod", "utime")),
                       (os.path, ("exists", "isfile", "isdir", "islink", "getsize",
                                  "getmtime", "getctime", "getatime", "realpath", "abspath")),
                       (shutil, ("disk_usage", "copy2", "copy", "copytree", "move",
                                 "rmtree", "copyfile"))):
        for n in names:
            fn = getattr(mod, n, None)
            if fn is not None and hasattr(fn, "__wrapped__"):
                setattr(mod, n, fn.__wrapped__)
    _activated = False


def describe() -> str:
    lines = ["wb_compat 前缀映射："]
    for win, env_name, default in _PREFIX_TABLE:
        lines.append(f"  {win:<34} -> {env_name}={_resolve(env_name, default)}")
    lines.append("wb_compat 正则规则：")
    for pat, env_name, default in _REGEX_RULES:
        lines.append(f"  /{pat.pattern}/ -> {env_name}={_resolve(env_name, default)}")
    lines.append(f"  /{_DRIVE_RE.pattern}/ -> WB_DATA_ROOT={_resolve('WB_DATA_ROOT', '/')}")
    return "\n".join(lines)


# ────────────────────────────── 自检 ──────────────────────────────
def _popen_probe():
    """证明 subprocess 的参数确实被改写：不执行真程序，只看报错里的文件名。"""
    import subprocess
    try:
        subprocess.run([rewrite(r"D:\视频\自媒体视频库\_nonexistent_probe.exe"), "--v"],
                       capture_output=True)
    except OSError as e:
        return str(getattr(e, "filename", "") or "")
    return "?"


def _selftest() -> int:
    """平台自适应自检：
    * `rewrite()`（纯字符串逻辑）在任何平台都跑 —— 本机 Windows 也能验映射表；
    * 补丁承载面 / ctypes.windll 只在 POSIX 下跑（Windows 上路径语义不同，验了没意义）。
    """
    import pathlib
    import shutil
    import subprocess

    posix = os.name != "nt"
    print(f"os.name = {os.name}  →  {'POSIX 全量自检' if posix else 'Windows：只验 rewrite() 映射逻辑'}")
    print(describe())
    active = activate(force=True)
    print(f"activate() -> {active}\n")

    cases = [
        (r"D:\视频\自媒体视频库", "WB_VIDEO_LIB"),
        (r"D:\视频\自媒体视频库\_tools\TikTokDownloader", "WB_VIDEO_LIB"),
        (r"D:\视频\媒体知识库\_asr_raw", "WB_MEDIA_KB"),
        (r"D:\alist\data\data.db", "WB_ALIST_DIR"),
        (r"C:\Users\EDY\Videos\data\.appdata\facts.json", "WB_VIDEO_DATA"),
        (r"C:\Users\EDY\Projects\video-analyzer\backend", "WB_VCA_DIR"),
        (r"D:\视频\自媒体视频库\_app_log.txt", "WB_VIDEO_LIB"),
    ]
    bad = 0
    print("── 前缀表 ──")
    for src, env_name in cases:
        dst = rewrite(src)
        want_root = _resolve(env_name, "")
        ok = dst.startswith(want_root) and "\\" not in dst
        bad += 0 if ok else 1
        print(f"  [{'OK' if ok else '!!'}] {src}\n        -> {dst}")

    print("── 正则规则 ──")
    for src in (
        r"C:\Users\EDY\Projects\video-analyzer\backend\venv\Lib\site-packages"
        r"\imageio_ffmpeg\binaries\ffmpeg-win-x86_64-v7.1.exe",
        r"C:\Users\EDY\Projects\video-analyzer\backend\asr_venv\Lib\site-packages"
        r"\imageio_ffmpeg\binaries\ffmpeg-win-x86_64-v7.1.exe",
    ):
        dst = rewrite(src)
        ok = dst.endswith("ffmpeg")
        bad += 0 if ok else 1
        print(f"  [{'OK' if ok else '!!'}] …{src[-46:]}\n        -> {dst}")
    dst = rewrite(os.path.join(r"C:\Users\EDY\Projects\video-analyzer\backend",
                              "venv", "Scripts", "python.exe"))
    ok = dst == _resolve("WB_PYTHON", None)
    bad += 0 if ok else 1
    print(f"  [{'OK' if ok else '!!'}] …venv/Scripts/python.exe -> {dst}")
    # POSIX 形态（已被前缀表改写过的 _dl_mix.py VENV_PY）也必须落到容器解释器
    for posix_venv in (
        "/mnt/video-lib/_tools/TikTokDownloader/.venv/Scripts/python.exe",
        "/mnt/vca/backend/venv/Scripts/python.exe",
    ):
        dst = rewrite(posix_venv)
        ok = dst == _resolve("WB_PYTHON", None)
        bad += 0 if ok else 1
        print(f"  [{'OK' if ok else '!!'}] {posix_venv}\n        -> {dst}")
    # 反例：POSIX 虚拟环境用 bin/，绝不能被这条规则误伤
    dst = rewrite("/usr/local/venv/bin/python3")
    ok = dst == "/usr/local/venv/bin/python3"
    bad += 0 if ok else 1
    print(f"  [{'OK' if ok else '!!'}] /usr/local/venv/bin/python3 应原样 -> {dst}")

    print("── 裸盘符 / 零成本直返 ──")
    for src, expect_prefix in ((r"D:\\", "/"), ("D:", "/")):
        dst = rewrite(src)
        ok = dst.startswith(expect_prefix)
        bad += 0 if ok else 1
        print(f"  [{'OK' if ok else '!!'}] {src!r} -> {dst!r}")
    for src in ("--min-age-min", "http://127.0.0.1:8766", "/usr/bin/bash",
                "5 1 1 Q", "", "plainname.mp4"):
        dst = rewrite(src)
        ok = dst == src
        bad += 0 if ok else 1
        print(f"  [{'OK' if ok else '!!'}] {src!r} -> {dst!r}（应原样）")

    print("── 补丁承载面 ──")
    if not posix:
        print("  （Windows 跳过：验证需 POSIX 路径语义）")
        print(f"\n映射逻辑 {'✅ 全部通过' if bad == 0 else f'❌ {bad} 项失败'}"
              f"（补丁承载面未验，请在容器内跑 wbctl.sh selftest）")
        return 0 if bad == 0 else 1

    checks = [
        ("pathlib.Path", lambda: str(pathlib.Path(r"D:\视频\媒体知识库")),
         "/mnt/media-kb"),
        ("pathlib.Path / 子路径",
         lambda: str(pathlib.Path(r"D:\视频\自媒体视频库") / "Data" / "x.csv"),
         "/mnt/video-lib/Data/x.csv"),
        ("os.path.join", lambda: os.path.join(r"D:\视频\自媒体视频库", "Data"),
         "/mnt/video-lib/Data"),
        ("str 拼接 + builtins.open", lambda: _open_probe(),
         "ok"),
        ("shutil.disk_usage", lambda: f"{shutil.disk_usage(r'D:\\').free > 0}",
         "True"),
        ("subprocess argv 改写", _popen_probe, "/mnt/video-lib/_nonexistent_probe.exe"),
        ("os.makedirs + os.path.isdir",
         lambda: _mkdir_probe(r"D:\视频\媒体知识库\_probe_dir"), "True"),
        ("glob.glob", _glob_probe, "1"),
    ]
    for label, fn, want in checks:
        try:
            got = fn()
        except Exception as e:  # noqa: BLE001
            got = f"<{type(e).__name__}: {e}>"
        ok = got == want
        bad += 0 if ok else 1
        print(f"  [{'OK' if ok else '!!'}] {label}: {got!r}（期望 {want!r}）")

    print("── ctypes.windll ──")
    import ctypes
    try:
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False) as fh:
            tmp = fh.name
        r = ctypes.windll.kernel32.DeleteFileW(tmp)
        ok1 = r == 1 and not os.path.exists(tmp)
        r2 = ctypes.windll.kernel32.DeleteFileW(tmp)          # 已删除 → 0
        ok2 = r2 == 0
        alive = ctypes.windll.kernel32.OpenProcess(0x00100000, False, os.getpid())
        dead = ctypes.windll.kernel32.OpenProcess(0x00100000, False, 999999)
        ok3 = alive == 1 and dead == 0
        ctypes.windll.kernel32.CloseHandle(alive)
        for ok, label in ((ok1, "DeleteFileW 成功→1"), (ok2, "DeleteFileW 失败→0"),
                          (ok3, "OpenProcess 存活/不存在")):
            bad += 0 if ok else 1
            print(f"  [{'OK' if ok else '!!'}] {label}")
    except Exception as e:  # noqa: BLE001
        bad += 1
        print(f"  [!!] ctypes.windll 自检异常: {type(e).__name__}: {e}")

    print(f"\n{'✅ 全部通过' if bad == 0 else f'❌ {bad} 项失败'}")
    return 0 if bad == 0 else 1


def _open_probe():
    """故意用「字符串拼接」绕开 os.path.join，验证 builtins.open 也被兜住。"""
    p = r"D:\视频\媒体知识库" + "/_probe_open.txt"
    os.makedirs(rewrite(r"D:\视频\媒体知识库"), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("x")
    ok = os.path.isfile("/mnt/media-kb/_probe_open.txt")
    try:
        os.remove(p)
    except OSError:
        pass
    return "ok" if ok else "miss"


def _mkdir_probe(p):
    os.makedirs(p, exist_ok=True)
    ok = os.path.isdir(p)
    if ok:
        try:
            os.rmdir(p)
        except OSError:
            pass
    return str(ok)


def _glob_probe():
    import glob
    import shutil as _sh
    d = os.path.join(r"D:\视频\媒体知识库", "_probe_glob")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "a.json"), "w", encoding="utf-8") as fh:
        fh.write("{}")
    # 注意这里又是「join + 字符串拼接」，专门验 glob 的兜底
    n = len(glob.glob(os.path.join(r"D:\视频\媒体知识库", "_probe_glob") + "/*.json"))
    _sh.rmtree(d, ignore_errors=True)
    return str(n)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    if "--describe" in sys.argv:
        print(describe())
        raise SystemExit(0)
    print(__doc__)
