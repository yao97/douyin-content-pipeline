# -*- coding: utf-8 -*-
"""解释器启动钩子 —— 由 PYTHONPATH 自动引入，把 wb_compat 挂上。

`site` 模块在初始化时会尝试 `import sitecustomize`，只要本目录在 PYTHONPATH 上即可命中。
**任何失败都必须沉默**：sitecustomize 抛异常会让整个解释器启动失败，代价太大，
所以这里整体 try/except 兜住（真出问题看 `WB_COMPAT_VERBOSE=1` 的 stderr）。

设计成「薄壳」而不是把逻辑写在这里，是为了让核心逻辑可以被直接 import 和自检：
    python -m wb_compat --selftest
"""
import os
import sys

try:
    from wb_compat import activate, describe
except Exception as _e:  # noqa: BLE001
    if os.environ.get("WB_COMPAT_VERBOSE") == "1":
        print(f"[sitecustomize] wb_compat 导入失败，已跳过: {type(_e).__name__}: {_e}",
              file=sys.stderr, flush=True)
else:
    try:
        if activate():
            if os.environ.get("WB_COMPAT_VERBOSE") == "1":
                print("[sitecustomize] wb_compat 已激活\n" + describe(),
                      file=sys.stderr, flush=True)
    except Exception as _e:  # noqa: BLE001
        if os.environ.get("WB_COMPAT_VERBOSE") == "1":
            print(f"[sitecustomize] wb_compat 激活失败，继续以原生路径运行: "
                  f"{type(_e).__name__}: {_e}", file=sys.stderr, flush=True)
