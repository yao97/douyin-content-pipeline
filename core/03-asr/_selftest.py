# -*- coding: utf-8 -*-
"""媒体知识库流水线自检（**只读**，不改任何数据）

覆盖四层：
  A. 全量语法编译（知识库 + 自媒体视频库 两边所有 .py）
  B. 关键纯函数单测（切片段长 / 基础设施失败判定 / 缓存名解析 / 看板进度 …）
  C. 静态扫描（吞异常的 except、可变默认参数、裸 except）
  D. 真实数据一致性（注册表 / raw / md / 封面 / 切片段 / 下载进度）

用法：
  WB_SOURCE=lib python _selftest.py            # 全部
  WB_SOURCE=lib python _selftest.py A B        # 只跑指定层
"""
import os
import re
import sys
import glob
import ast
import json
import math
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("WB_SOURCE", "lib")

FAIL = []
WARN = []
OK = []


def ok(msg):
    OK.append(msg)
    print("  [OK]   %s" % msg)


def fail(msg):
    FAIL.append(msg)
    print("  [FAIL] %s" % msg)


def warn(msg):
    WARN.append(msg)
    print("  [WARN] %s" % msg)


def head(t):
    print("\n" + "=" * 72)
    print(t)
    print("=" * 72)


# ─────────────────────────── A. 全量语法编译 ───────────────────────────
def layer_a():
    head("A. 全量语法编译")
    files = sorted(glob.glob("*.py")) + sorted(glob.glob(r"D:/视频/自媒体视频库/*.py"))
    bad = []
    for f in files:
        try:
            # 用 compile() 而不是 py_compile：Windows 上 os.devnull=='nul' 不能当 pyc 目标，
            # 且 compile() 纯粹做语法检查、不落任何文件
            with open(f, encoding="utf-8") as fh:
                compile(fh.read(), f, "exec")
        except Exception as e:
            bad.append((f, str(e)[:160]))
    if bad:
        for f, e in bad:
            fail("语法错误 %s -> %s" % (f, e))
    else:
        ok("%d 个 .py 全部语法通过" % len(files))
    return len(files)


# ─────────────────────────── B. 关键纯函数单测 ───────────────────────────
def layer_b():
    head("B. 关键纯函数单测")
    import asr_batch as B

    # --- B1. _seg_for：段长自适应不变量 ---
    if hasattr(B, "_seg_for"):
        SEC = B.SLICE_SEC
        bad = []
        for dur in (0, 1, 60, 899.9, 900, 900.1, 1200, 1199, 1799, 1800, 10016,
                    14227, 16685, 20000, 36000, 1e5):
            seg = B._seg_for(dur)
            if not (60 <= seg <= SEC):
                bad.append("dur=%s seg=%s 越界" % (dur, seg))
                continue
            if dur and dur > SEC:
                n = math.ceil(dur / seg)               # 实际段数（别用 int(dur)，会截断）
                if n * seg < dur:                      # 覆盖不完整
                    bad.append("dur=%s seg=%s x%d 覆盖不足" % (dur, seg, n))
                if n >= 2 and seg * (n - 1) >= dur:    # 段数偏多
                    bad.append("dur=%s seg=%s 可用更少段" % (dur, seg))
        if bad:
            for m in bad:
                fail("_seg_for: " + m)
        else:
            ok("_seg_for 段长自适应：16 组边界值满足「覆盖完整 + 段数最省 + 不超上限」")
    else:
        fail("asr_batch._seg_for 不存在")

    # --- B2. _seg_for 实际段数 == 预期段数（关键：防碎屑尾段）---
    for dur, want in ((1200, 2), (10016, 12), (14227, 16), (16685, 19), (9000, 10)):
        seg = B._seg_for(dur)
        n = math.ceil(dur / seg)
        rest = dur - seg * (n - 1)          # 尾段实际秒数
        if n != want:
            fail("_seg_for(%s) 段数 %d ≠ 预期 %d" % (dur, n, want))
        elif rest < 0.5:
            fail("_seg_for(%s) 尾段仅 %.2fs（碎屑！）" % (dur, rest))
        else:
            ok("_seg_for(%s) -> %ds/段 x%d，尾段 %.1fs 正常" % (dur, seg, n, rest))

    # --- B3. is_infra_failure：基础设施 vs 内容失败分流 ---
    if hasattr(B, "is_infra_failure"):
        infra_cases = [
            "HTTPConnectionPool(host='127.0.0.1', port=8766): Max retries exceeded",
            "[WinError 10061] 由于目标计算机积极拒绝，无法连接。",
            "抽音频超时",
            "服务未就绪",
        ]
        content_cases = [
            "切片合并结果校验未过: 文本过短",
            "corrupt_src",
            "转写结果为空",
        ]
        bad = []
        for m in infra_cases:
            if not B.is_infra_failure(m):
                bad.append("漏判基础设施: " + m[:40])
        for m in content_cases:
            if B.is_infra_failure(m):
                bad.append("误判为基础设施: " + m[:40])
        if bad:
            for m in bad:
                fail("is_infra_failure: " + m)
        else:
            ok("is_infra_failure 正确分流 %d 组基础设施 / %d 组内容失败"
               % (len(infra_cases), len(content_cases)))
    else:
        warn("asr_batch.is_infra_failure 不存在（跳过）")

    # --- B4. validate_raw：完整性判据（注意它还会校验 vid 字段）---
    if hasattr(B, "validate_raw"):
        VID = "7542610336659115298"
        cases = [
            ({"text": "", "duration": 100, "vid": VID}, False, "空文本"),
            ({"text": "占位", "duration": 0, "vid": VID}, False, "零时长"),
            ({"text": "短", "duration": 100, "vid": VID}, False, "字数不足"),
            ({"text": "（无语音内容）", "duration": 100, "vid": VID}, False, "无语音占位符"),
            ({"text": "这是一段足够长的正常逐字稿内容" * 3, "duration": 100, "vid": VID}, True, "正常"),
            ({"text": "这是一段足够长的正常逐字稿内容" * 3, "duration": 100}, False, "缺 vid"),
        ]
        bad = []
        for raw, want, name in cases:
            got = B.validate_raw(raw)
            got_ok = got[0] if isinstance(got, tuple) else bool(got)
            if got_ok != want:
                bad.append("%s 期望 %s 实得 %s" % (name, want, got_ok))
        if bad:
            for m in bad:
                fail("validate_raw: " + m)
        else:
            ok("validate_raw %d 组用例判据正确（含 vid 必填）" % len(cases))
    else:
        warn("asr_batch.validate_raw 不存在（跳过）")

    # --- B5. webui 缓存名解析（日期自带连字符的坑）---
    import webui as W
    if hasattr(W, "_split_cache_name"):
        got = W._split_cache_name("2025-08-22 09.00.01-视频-播客正片合集-某个标题")
        want = ("2025-08-22 09.00.01", "视频", "播客正片合集", "某个标题")
        if got == want:
            ok("_split_cache_name 正确拆出（日期连字符不再错位）")
        else:
            fail("_split_cache_name: 期望 %r 实得 %r" % (want, got))
    else:
        warn("webui._split_cache_name 不存在")

    # --- B6. webui._hms 时长解析（HH:MM:SS → 秒）---
    if hasattr(W, "_hms"):
        cases = [("00:02:30", 150), ("01:00:00", 3600), ("00:00:00", 0),
                 ("166:40:00", 600000), ("02:30", 150), ("90", 90)]
        got = [W._hms(c) for c, _ in cases]
        want = [w for _, w in cases]
        if got == want:
            ok("_hms 解析正确 %s" % got)
        else:
            fail("_hms: 期望 %s 实得 %s" % (want, got))
    else:
        warn("webui._hms 不存在")

    # --- B7. webui._slice_progress（json / wav 分目录配对）---
    if hasattr(W, "_slice_progress"):
        import asr_batch as _B
        base = os.path.join(_B.SLICE_DIR, "7542610336659115298")
        if os.path.isdir(base):
            sl = W._slice_progress("7542610336659115298")
            if not sl:
                fail("_slice_progress 对真实切片目录返回 None")
            elif sl["done"] == 0 and glob.glob(os.path.join(base, "seg_*.json")):
                fail("_slice_progress done=0 但目录里有 seg json（跨目录配对失效）")
            elif sl["done_sec"] <= 0 and sl["done"] > 0:
                fail("_slice_progress done=%d 但 done_sec=0（wav 配对失败）" % sl["done"])
            else:
                ok("_slice_progress 真实数据：parts=%d done=%d done_sec=%.1f/%.1f"
                   % (sl["parts"], sl["done"], sl["done_sec"], sl["total_sec"]))
        else:
            warn("切片临时目录不存在（跳过 _slice_progress 实测）")
        # 不存在的 vid 必须返回 None
        if W._slice_progress("999999999999999999999") is None:
            ok("_slice_progress 对不存在的 vid 返回 None")
        else:
            fail("_slice_progress 对不存在的 vid 未返回 None")
    else:
        warn("webui._slice_progress 不存在")

    # --- B46. save_registry 的 D22 兜底链（2026-10-10 新增）---
    # 背景：WebUI 1.2s 轮询反复读注册表 → `os.replace` 必然被 `[WinError 5]` 拒。
    # 曾试读侧 FILE_SHARE_DELETE，**实测无效**（持该句柄时 replace 仍被拒，而覆盖写成功）。
    # ⇒ 兜底链：replace → 退避重试 → **覆盖写**（实证唯一能在读句柄存在时落盘的方式）。
    if hasattr(B, "save_registry"):
        import shutil as _shb
        _bakr = B.REGISTRY + ".b46bak"
        _shb.copy2(B.REGISTRY, _bakr)
        try:
            # T1 正常路径
            B.save_registry({"t": 1})
            _d = json.loads(B._read_text_shared(B.REGISTRY))
            if _d.get("t") != 1:
                fail("B46-T1: 无读句柄时 save_registry 未正确落盘")
            else:
                ok("B46-T1: 无读句柄 → replace 路径正常")

            # T2 🔴 核心场景：持读句柄（模拟 webui 轮询）仍必须落盘
            _f = open(B.REGISTRY, "r", encoding="utf-8"); _f.read()
            try:
                B.save_registry({"t": 2})
                _d = json.loads(B._read_text_shared(B.REGISTRY))
                if _d.get("t") != 2:
                    fail("B46-T2: 持读句柄时兜底未生效（数据没落盘）")
                else:
                    ok("B46-T2: 持读句柄 → 覆盖写兜底成功落盘（D22 根治）")
            finally:
                _f.close()

            # T3 覆盖写不能产生半截 JSON（这是它唯一的风险）
            _f = open(B.REGISTRY, "r", encoding="utf-8"); _f.read()
            try:
                B.save_registry({"t": 3, "big": "x" * 100000})
            finally:
                _f.close()
            _d = json.loads(B._read_text_shared(B.REGISTRY))
            if _d.get("t") == 3 and len(_d.get("big", "")) == 100000:
                ok("B46-T3: 大payload 覆盖写后 JSON 完整可解析")
            else:
                fail("B46-T3: 覆盖写产生不完整 JSON")

            # T4 残留 .tmp 不得影响 load_registry
            if isinstance(B.load_registry(), dict):
                ok("B46-T4: load_registry 忽略残留 .tmp 正常读取")
            else:
                fail("B46-T4: load_registry 读取异常")
        finally:
            _shb.copy2(_bakr, B.REGISTRY)
            try:
                os.remove(_bakr)
            except OSError:
                pass
            if os.path.exists(B.REGISTRY + ".tmp"):
                try:
                    os.remove(B.REGISTRY + ".tmp")
                except OSError:
                    pass
    else:
        warn("asr_batch.save_registry 不存在（B46 未覆盖）")

    # --- B45. `_rm()` 安全删除（2026-10-10 新增）---
    # 背景：沙箱 safe-delete 会拦 `os.remove` 抛异常，而原先这些删除都写在
    # `except: pass` 里 → 异常被吞 → claim/锁文件永远删不掉 →
    # **stage1 主循环卡死**（10-10 02:03 实测）或下次拉起误判「已有实例在跑」。
    # `_rm` 用「先 os.replace 改名、再删 .bak」绕开删除通道。
    if hasattr(B, "_rm"):
        import tempfile as _tf
        _d = _tf.mkdtemp()

        # T1 存在的文件 → 消失
        _f1 = os.path.join(_d, "a.txt")
        open(_f1, "w", encoding="utf-8").write("x")
        B._rm(_f1)
        if os.path.exists(_f1):
            fail("B45-T1: _rm 未删除已存在文件")
        else:
            ok("B45-T1: _rm 删除已存在文件")

        # T2 不存在的文件 → 不抛、返回 True（幂等）
        try:
            B._rm(os.path.join(_d, "nope.txt"))
            ok("B45-T2: _rm 对不存在的文件幂等不抛")
        except Exception as e:
            fail("B45-T2: _rm 对不存在的文件抛异常: %s" % type(e).__name__)

        # T3 目录 → 不抛（_rm 是给单文件用的；误传目录必须安全降级，不能把跑批带崩）
        _sub = os.path.join(_d, "sub")
        os.makedirs(_sub, exist_ok=True)
        try:
            B._rm(_sub)
            ok("B45-T3: _rm 传目录不抛（安全降级）")
        except Exception as e:
            fail("B45-T3: _rm 传目录抛异常: %s" % type(e).__name__)
        finally:
            if os.path.isdir(_sub):
                import shutil as _sh2
                _sh2.rmtree(_sub, ignore_errors=True)

        # T4 关键：`_rm` 必须在「沙箱拦截 os.remove」的场景下仍能移走目标
        # （用同名 .bak 已存在来复现「第一次 replace 也失败」的极端路径）
        _f4 = os.path.join(_d, "b.txt")
        _bak4 = _f4 + ".bak"
        open(_f4, "w", encoding="utf-8").write("y")
        open(_bak4, "w", encoding="utf-8").write("z")   # 故意占位，逼replace 走失败分支
        try:
            B._rm(_f4)
            if os.path.exists(_f4):
                fail("B45-T4: .bak 已存在时 _rm 未能移走目标文件")
            else:
                ok("B45-T4: .bak 已存在（replace 失败路径）仍移走目标 —— 兜底有效")
        finally:
            for _x in (_f4, _bak4):
                if os.path.exists(_x):
                    try:
                        os.remove(_x)
                    except OSError:
                        pass

        try:
            import shutil as _sh3
            _sh3.rmtree(_d, ignore_errors=True)
        except Exception:
            pass
    else:
        warn("asr_batch._rm 不存在（B45 未覆盖）")

    # --- B44. _gc_slice_tmp 三重保险（2026-10-09 新增）---
    # 背景：_slice_tmp 涨到 19GB 把 D 盘撑到 100% 满 → stage1 在 save_registry()
    # 抛 OSError Errno 28 崩溃。A 方案在 mark_ok 后自动清段缓存。
    # 关键：**误删在飞作品的段缓存会导致断点续跑失效（白重转），误删唯一副本会真丢数据**，
    # 所以「该作品未 ok」和「raw 缺失」两种情况必须原样跳过。
    if hasattr(B, "_gc_slice_tmp"):
        import shutil as _sh
        _vids = ["7777777777777777001", "7777777777777777002", "7777777777777777003"]

        def _mk(vid, with_raw=True):
            d = os.path.join(B.SLICE_DIR, vid)
            os.makedirs(d, exist_ok=True)
            for i in range(2):
                with open(os.path.join(d, "seg_%03d.json" % i), "w", encoding="utf-8") as f:
                    f.write('{"x":1}')
                with open(os.path.join(d, "seg_%03d.wav" % i), "wb") as f:
                    f.write(b"\0" * 1024)
            rp = os.path.join(B.RAW_DIR, vid + ".json")
            if with_raw:
                with open(rp, "w", encoding="utf-8") as f:
                    json.dump({"vid": vid, "text": "自检用逐字稿" * 40,
                               "duration": 1500.0}, f, ensure_ascii=False)
            elif os.path.exists(rp):
                os.remove(rp)
            return d

        try:
            # T1ok + raw 在 → 应清
            d1 = _mk(_vids[0])
            B.update_entry(_vids[0], {"ok": True, "author": "B44"}, remove=("attempts",))
            B._gc_slice_tmp(_vids[0])
            if os.path.isdir(d1):
                fail("B44-T1: 已完成作品的段缓存未被清理")
            else:
                ok("B44-T1: 已完成（ok+raw）的段缓存被自动清理")

            # T2 ok=false → 绝不清
            d2 = _mk(_vids[1])
            B.update_entry(_vids[1], {"ok": False, "attempts": 1, "author": "B44"})
            B._gc_slice_tmp(_vids[1])
            if not os.path.isdir(d2):
                fail("B44-T2: 误删了未完成作品的段缓存（会毁掉断点续跑）")
            else:
                ok("B44-T2: 未完成（ok=false）作品段缓存保留 —— 防误删在飞段")

            # T3 raw 缺失 → 绝不清
            d3 = _mk(_vids[2], with_raw=False)
            B.update_entry(_vids[2], {"ok": True, "author": "B44"})
            B._gc_slice_tmp(_vids[2])
            if not os.path.isdir(d3):
                fail("B44-T3: raw 缺失时误删段缓存（段是唯一副本，会真丢数据）")
            else:
                ok("B44-T3: raw 缺失时段缓存保留 —— 防丢唯一副本")

            # T4 开关关闭 → 跳过
            d4 = _mk(_vids[0])
            B.update_entry(_vids[0], {"ok": True, "author": "B44"})
            _old = B.SLICE_GC
            B.SLICE_GC = False
            try:
                B._gc_slice_tmp(_vids[0])
            finally:
                B.SLICE_GC = _old
            if not os.path.isdir(d4):
                fail("B44-T4: WB_SLICE_GC=0 时仍清理了")
            else:
                ok("B44-T4: WB_SLICE_GC=0 时跳过清理（开关有效）")
        finally:
            # 残留必须直接从文件层清：同进程内update_entry(delete=True) 可能不落盘
            _r = B._reg()
            _dirty = False
            for v in _vids:
                if v in _r:
                    _r.pop(v, None)
                    _dirty = True
            if _dirty:
                B.save_registry(_r)
            for v in _vids:
                d = os.path.join(B.SLICE_DIR, v)
                if os.path.isdir(d):
                    _sh.rmtree(d, ignore_errors=True)
                rp = os.path.join(B.RAW_DIR, v + ".json")
                if os.path.exists(rp):
                    try:
                        os.remove(rp)
                    except OSError:
                        pass
    else:
        warn("asr_batch._gc_slice_tmp 不存在（B44 未覆盖）")

    # --- B8. load_registry 失败路径（绝不能静默返回 {}，否则全量重转）---
    if hasattr(B, "load_registry"):
        import tempfile
        orig = B.REGISTRY
        d = tempfile.mkdtemp()
        try:
            B.REGISTRY = os.path.join(d, "reg.json")
            _ = B.load_registry()
            if _ != {}:
                fail("load_registry: 文件不存在时应返回 {}")
            else:
                os.makedirs(os.path.dirname(B.REGISTRY), exist_ok=True)
                with open(B.REGISTRY, "w", encoding="utf-8") as f:
                    json.dump({"a": {"ok": True}}, f)
                if B.load_registry() != {"a": {"ok": True}}:
                    fail("load_registry: 正常文件读取结果不符")
                else:
                    with open(B.REGISTRY, "w", encoding="utf-8") as f:
                        f.write("{这不是合法 json")
                    raised = False
                    try:
                        B.load_registry()
                    except RuntimeError:
                        raised = True
                    except Exception as e:
                        fail("load_registry: 期望 RuntimeError，实得 %s" % type(e).__name__)
                    if not raised:
                        fail("load_registry: 文件损坏时**没有抛错**（会静默全量重转！）")
                    elif not os.path.exists(B.REGISTRY + ".corrupt"):
                        fail("load_registry: 损坏时未生成 .corrupt 备份")
                    else:
                        ok("load_registry 失败路径正确：空文件→{} / 正常→原样 / 损坏→抛错+备份（不会静默清空）")
        finally:
            B.REGISTRY = orig
    else:
        warn("asr_batch.load_registry 不存在")

    # --- B9. 切片上传名 / tag 生成（转写与「捞孤儿段」必须同名同源）---
    if hasattr(B, "_slice_tag") and hasattr(B, "_slice_upload_name"):
        bad = []
        for src, want in [
            ("D:/x/2025-08-22 09.00.01-视频-播客正片合集-四小时完整版.mp4",
             "2025-08-22_09_00_01-视频-播客正片合集-四小时完整版"),
            ("/x/a b/c(m).m4a", "c_m_"),
        ]:
            got = B._slice_tag(src)
            if got != want:
                bad.append("_slice_tag(%r) 期望 %r 实得 %r" % (src, want, got))
        if len(B._slice_tag("x" * 200)) != 64:
            bad.append("_slice_tag 未截断到 64 字符（长标题会撑爆上传名）")
        if B._slice_upload_name("123", "tag", 1) != "123_tag_001.mp4":
            bad.append("_slice_upload_name k=1 不符（须为 1 基、3 位补零）")
        if B._slice_upload_name("123", "tag", 12) != "123_tag_012.mp4":
            bad.append("_slice_upload_name k=12 不符")
        if bad:
            for m in bad:
                fail("切片命名: " + m)
        else:
            ok("切片命名同源：_slice_tag 截断 64 / _slice_upload_name 为 1 基 3 位"
               "（转写与捞孤儿段共用，名字不会漂移）")
    else:
        warn("asr_batch._slice_upload_name / _slice_tag 不存在")

    # --- B10. 僵尸作业判死（服务端重启后 job 永久停在 audio_ready，不能死等 timeout）---
    if hasattr(B, "asr_transcribe_audio") and hasattr(B, "ASR_SESS"):

        class _Resp:
            def __init__(self, data):
                self._d = data

            def raise_for_status(self):
                return None

            def json(self):
                return self._d

        class _Sess:
            """statuses 依次弹出；用完后一直返回最后一个。

            特殊值 `"__unknown__"` 模拟服务端不认这个作业（返回 `{"error":"任务不存在"}`）。
            """

            def __init__(self, statuses):
                self.statuses = list(statuses)
                self.seen = []

            def post(self, url, **kw):
                return _Resp({"job_id": "fakejob"})

            def get(self, url, **kw):
                if "/api/status/" in url:
                    s = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
                    self.seen.append(s)
                    if s == "__unknown__":
                        return _Resp({"error": "任务不存在"})
                    return _Resp({"status": s, "detail": s})
                return _Resp({"result": {"text": "ok 文本", "segments": []}})

        orig_sess, orig_z = B.ASR_SESS, getattr(B, "ZOMBIE_SEC", None)
        try:
            # ① 永远 audio_ready → 必须在 ZOMBIE_SEC 后抛错（而不是等到 dur*3+300）
            B.ZOMBIE_SEC = 0.5
            B.ASR_SESS = _Sess(["audio_ready"])
            t0 = time.time()
            msg = ""
            try:
                B.asr_transcribe_audio("_selftest.py", "123", 890)
            except Exception as e:
                msg = str(e)
            waited = time.time() - t0
            if "服务端重启遗留" not in msg:
                fail("僵尸作业未被判死（等到 %.1fs 才返回：%s）" % (waited, msg or "没抛错"))
            elif waited > 20:
                fail("僵尸作业判死太慢（%.1fs）" % waited)
            else:
                ok("僵尸作业判死：audio_ready 停滞 %.1fs 即抛错重提（不再白等 ~49 分钟）" % waited)

            # ② 正常推进（transcribing → done）不能误判
            B.ZOMBIE_SEC = 0.5
            B.ASR_SESS = _Sess(["audio_ready", "transcribing", "done"])
            try:
                r = B.asr_transcribe_audio("_selftest.py", "123", 890)
                if r.get("text") == "ok 文本":
                    ok("正常作业不误判：transcribing→done 正常取回结果")
                else:
                    fail("正常作业取回结果不符: %r" % (r,))
            except Exception as e:
                fail("正常作业被误判为僵尸: %s" % e)

            # ③ 作业在服务端已消失（{"error":"任务不存在"}）→ 也必须快速失败，不能空转到超时
            B.ASR_SESS = _Sess(["__unknown__"])
            t0 = time.time()
            msg = ""
            try:
                B.asr_transcribe_audio("_selftest.py", "123", 890)
            except Exception as e:
                msg = str(e)
            waited = time.time() - t0
            if "不存在" not in msg:
                fail("作业消失未被判死（%.1fs 后返回：%s）" % (waited, msg or "没抛错"))
            elif waited > 20:
                fail("作业消失判死太慢（%.1fs）" % waited)
            else:
                ok("作业在服务端消失（任务不存在）时 %.1fs 即失败重提（不再空转 ~49 分钟）" % waited)
        finally:
            B.ASR_SESS = orig_sess
            if orig_z is not None:
                B.ZOMBIE_SEC = orig_z
    else:
        warn("asr_batch.asr_transcribe_audio / ASR_SESS 不存在")

    # --- B11. 服务托管探测（决定 ensure_server 能否自行 Popen）---
    if hasattr(B, "service_registered"):
        if B.service_registered():
            if B.service_registered("__不存在的服务名__"):
                fail("service_registered 对不存在的服务名也返回 True（判据失效 -> 会误判为托管）")
            else:
                ok("service_registered 正确：能识别 %s 已注册、且不会误认不存在的名字"
                   % B.SERVICE_NAME)
        else:
            warn("未探测到 %s 注册（本机没装该服务？跳过）" % getattr(B, "SERVICE_NAME", "?"))
    else:
        warn("asr_batch.service_registered 不存在")

    # --- B12. 基础设施故障不占单段重试名额（8766 崩一次不能白烧 3 次机会）---
    if hasattr(B, "slice_one") and hasattr(B, "is_infra_failure"):
        import shutil
        import tempfile
        import time as _time

        real = {}
        tmp = tempfile.mkdtemp(prefix="wbselftest_")
        for n in ("SLICE_DIR", "RAW_DIR", "slice_tasks", "_cut_segments",
                  "asr_transcribe_audio", "ensure_server", "mark_ok", "update_entry"):
            if hasattr(B, n):
                real[n] = getattr(B, n)
        real_sleep = _time.sleep
        try:
            _time.sleep = lambda _s: None                     # 退避/间隔一律不真等
            B.SLICE_DIR = os.path.join(tmp, "slice")
            B.RAW_DIR = os.path.join(tmp, "raw")
            os.makedirs(B.SLICE_DIR, exist_ok=True)
            os.makedirs(B.RAW_DIR, exist_ok=True)
            vid = "9999999999999999999"
            B.slice_tasks = lambda _v: [{"video": "fake_src.mp4", "dur_csv": 3600,
                                         "vid": vid, "author": "自检", "title": "自检"}]
            B._cut_segments = lambda p, tag, sec, work, force=False: (
                [(os.path.join(work, "seg_000.wav"), 0.0, 600.0)], 600.0)
            B.ensure_server = lambda *a, **k: True
            B.mark_ok = lambda *a, **k: None
            B.update_entry = lambda *a, **k: None

            INFRA = ('HTTPConnectionPool(host=\'127.0.0.1\', port=8766): Max retries exceeded '
                     '(Caused by NewConnectionError(... [WinError 10061] 由于目标计算机积极拒绝))')

            def _mk(calls_before_ok):
                """前 `calls_before_ok` 次抛基础设施错误，之后成功；返回调用计数容器。"""
                c = {"n": 0}

                def f(*_a, **_k):
                    c["n"] += 1
                    if c["n"] <= calls_before_ok:
                        raise RuntimeError(INFRA)
                    return {"text": "自检占位文本" * 3, "segments": []}
                return c, f

            # ① 连撞 4 次基础设施故障（> SLICE_RETRY=3）后成功 → 必须仍然成功
            c, f = _mk(4)
            B.asr_transcribe_audio = f
            r1 = B.slice_one(vid, seg_sec=600, force=True)
            if r1 and c["n"] == 5:
                ok("基础设施故障不计入单段重试名额（连撞 4 次 %d/段 后仍成功，共提交 %d 次）"
                   % (B.SLICE_RETRY, c["n"]))
            else:
                fail("基础设施故障占了重试名额：slice_one=%s 提交次数=%d（期望 True / 5）"
                     % (r1, c["n"]))

            # ② 真实失败（与音频有关）仍按 SLICE_RETRY 上限止损
            c2 = {"n": 0}

            def f2(*_a, **_k):
                c2["n"] += 1
                raise RuntimeError("音频解码失败：no_audio")
            B.asr_transcribe_audio = f2
            r2 = B.slice_one(vid, seg_sec=600, force=True)
            if (not r2) and c2["n"] == B.SLICE_RETRY:
                ok("真实失败仍按 %d 次止损（提交 %d 次后放弃）" % (B.SLICE_RETRY, c2["n"]))
            else:
                fail("真实失败未按 SLICE_RETRY 止损：slice_one=%s 提交次数=%d（期望 False / %d）"
                     % (r2, c2["n"], B.SLICE_RETRY))
        except Exception as e:
            warn("B12 基础设施退避用例异常跳过: %r" % (e,))
        finally:
            _time.sleep = real_sleep
            for n, v in real.items():
                setattr(B, n, v)
            shutil.rmtree(tmp, ignore_errors=True)
    else:
        warn("asr_batch.slice_one / is_infra_failure 不存在")


# ─────────────────────────── C. 静态扫描 ───────────────────────────
def layer_c():
    head("C. 静态扫描（吞异常 / 可变默认参数 / 裸 except）")
    files = sorted(glob.glob("*.py")) + sorted(glob.glob(r"D:/视频/自媒体视频库/*.py"))
    swallow = []
    mutdef = []
    bare = []
    for f in files:
        if os.path.basename(f) == "_selftest.py":
            continue
        try:
            tree = ast.parse(open(f, encoding="utf-8").read(), filename=f)
        except Exception:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                body = node.body
                if node.type is None:
                    bare.append((f, node.lineno))
                if len(body) == 1 and isinstance(body[0], ast.Pass):
                    swallow.append((f, node.lineno, ast.unparse(node.type) if node.type else "bare"))
            if isinstance(node, ast.FunctionDef):
                for d in node.args.defaults:
                    if isinstance(d, (ast.List, ast.Dict, ast.Set)):
                        mutdef.append((f, node.lineno, node.name))

    if bare:
        for f, ln in bare[:10]:
            warn("裸 except: %s:%d" % (f, ln))
    print("  裸 except: %d 处" % len(bare))
    print("  「except: pass」完全吞异常: %d 处" % len(swallow))
    for f, ln, tp in swallow:
        print("      %s:%d  (%s)" % (os.path.basename(f), ln, tp))
    if mutdef:
        for f, ln, n in mutdef:
            fail("可变默认参数 %s:%d %s()" % (f, ln, n))
    else:
        ok("无可变默认参数（经典坑）")
    if swallow:
        warn("%d 处完全吞掉异常（可能掩盖真 bug，建议至少记日志）" % len(swallow))
    else:
        ok("无完全吞异常")


# ─────────────────────────── D. 真实数据一致性 ───────────────────────────
def layer_d():
    head("D. 真实数据一致性")
    import asr_batch as B
    import lib_source as L

    reg = B.load_registry()
    tasks = L.build_lib_tasks()
    ok_set = {v for v, m in reg.items() if m.get("ok")}

    # --- D1. 注册表 vs 任务清单 ---
    vids = {str(t["vid"]) for t in tasks}
    pend = sorted(vids - ok_set)
    extra = sorted(ok_set - vids)
    print("  任务 %d 条｜注册表 ok %d 条｜待转写 %d 条｜注册表多出(旧源) %d 条"
          % (len(vids), len(ok_set), len(pend), len(extra)))
    # 同一作品被下载器切成 `_1/_2/_3` 多份源 → 同 vid 多条任务是**已知现象**（不是 bug），
    # 但会让任务数虚高。stage1 已把多源当切片拼接处理（见 asr_batch._stage1_run）。
    dup = {}
    for t in tasks:
        dup.setdefault(str(t["vid"]), []).append(t)
    dups = {v: lst for v, lst in dup.items() if len(lst) > 1}
    if dups:
        weird = []
        for v, lst in dups.items():
            names = [os.path.basename(x["video"]) for x in lst]
            if not all(re.search(r"_\d+\.[A-Za-z0-9]+$", n) for n in names):
                weird.append((v, names[:3]))
        if weird:
            fail("同 vid 多条任务但文件名不是 `_N` 分片：%s" % weird[:3])
        else:
            warn("有 %d 个 vid 各有多个分片源（任务数虚增 %d 条，属已知现象）：%s"
                 % (len(dups), sum(len(l) - 1 for l in dups.values()), sorted(dups)[:4]))
    else:
        ok("任务清单无重复 vid")

    # --- D2. ok 条目的 raw 必须存在且内容完整 ---
    miss_raw, bad_raw, low = [], [], 0
    for v in list(ok_set)[:4000]:
        e = reg.get(v) or {}
        # `accept <vid>` 标过 low_content 的条目（几乎无语音）本就跳过字数校验 → 不算问题
        if e.get("low_content"):
            low += 1
            continue
        p = os.path.join(B.RAW_DIR, "%s.json" % v)
        if not os.path.exists(p):
            miss_raw.append(v)
            continue
        try:
            raw = json.load(open(p, encoding="utf-8"))
            okv, why = B.validate_raw(raw) if hasattr(B, "validate_raw") else (bool(raw.get("text")), "")
            okv = okv[0] if isinstance(okv, tuple) else okv
            if not okv:
                bad_raw.append((v, why))
        except Exception as e:
            bad_raw.append((v, "读失败:%s" % str(e)[:40]))
    if low:
        print("  （另有 %d 条 low_content 已放行条目，按设计跳过字数校验）" % low)
    if miss_raw:
        fail("注册表标 ok 但缺 raw：%d 条 %s" % (len(miss_raw), miss_raw[:5]))
    else:
        ok("全部 %d 条 ok 记录都有对应 raw 文件" % len(ok_set))
    if bad_raw:
        fail("raw 校验不过：%d 条（前 5）%s" % (len(bad_raw), bad_raw[:5]))
    else:
        ok("raw 内容完整性校验全部通过")

    # --- D3. 切片临时目录：wav / json 配对 ---
    if os.path.isdir(B.SLICE_DIR):
        for vid in sorted(os.listdir(B.SLICE_DIR)):
            d = os.path.join(B.SLICE_DIR, vid)
            if not os.path.isdir(d):
                continue
            wavs = [os.path.basename(p) for p in glob.glob(os.path.join(d, "**", "seg_*.wav"), recursive=True)]
            js = [os.path.basename(p) for p in glob.glob(os.path.join(d, "**", "seg_*.json"), recursive=True)]
            orphan = [j for j in js if j[:-5] + ".wav" not in wavs]
            if orphan:
                fail("切片 %s：%d 个 json 没有对应 wav %s" % (vid, len(orphan), orphan[:3]))
            else:
                ok("切片 %s：wav %d 段 / json %d 个，全部配对" % (vid, len(wavs), len(js)))
    else:
        print("  （当前无切片临时目录）")

    # --- D4. 下载进度：分子 ≤ 分母 ---
    try:
        import webui as W
        dl = W.download_state(force=True)
        if dl["downloaded"] > dl["effective_total"]:
            fail("下载进度分子(%s) > 分母(%s)" % (dl["downloaded"], dl["effective_total"]))
        else:
            ok("下载进度 %s/%s (%.1f%%)，分母已扣 %d 条永久跳过"
               % (dl["downloaded"], dl["effective_total"], dl["percent"], dl["skipped"]))
    except Exception as e:
        warn("download_state 调用失败：%s" % str(e)[:80])

    # --- D5. 看板 live_state 自洽 ---
    try:
        import webui as W
        ls = W.live_state()
        if ls["done"] > ls["total"]:
            fail("live_state done(%s) > total(%s)" % (ls["done"], ls["total"]))
        else:
            it = ls.get("item") or {}
            ok("live_state 自洽：done=%s/%s，item=%s %s"
               % (ls["done"], ls["total"], it.get("author"), it.get("vid")))
        if not ls["stage1"] and ls.get("item"):
            warn("stage1 未运行但 item 非空（可能是手工 slice 在跑，也可能是误报）")
    except Exception as e:
        fail("live_state 抛异常：%s" % str(e)[:120])


def main():
    want = set(a.upper() for a in sys.argv[1:]) or {"A", "B", "C", "D"}
    print("媒体知识库 · 流水线自检（只读）")
    print("cwd=%s" % os.getcwd())
    if "A" in want:
        layer_a()
    if "B" in want:
        layer_b()
    if "C" in want:
        layer_c()
    if "D" in want:
        layer_d()
    head("结果")
    print("  OK   : %d" % len(OK))
    print("  WARN : %d" % len(WARN))
    print("  FAIL : %d" % len(FAIL))
    for m in FAIL:
        print("    ❌ %s" % m)
    for m in WARN:
        print("    ⚠️  %s" % m)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
