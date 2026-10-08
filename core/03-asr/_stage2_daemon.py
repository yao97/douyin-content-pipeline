# -*- coding: utf-8 -*-
"""stage2 守护进程：边转写边渲染（关键词 + 内容总结 + md + 封面）
- md 不存在 → 全量生成
- md 存在但缺「## 视频内容总结」 → 复用原关键词，只补总结后重写
- md 已含总结 → 跳过
"""
import os, sys, json, time, shutil, re
# ⚠️ 用**自身所在目录**入 sys.path（原来是硬编码 r"D:\视频\媒体知识库"）——
#    这样整个目录拷到第二台机器/别的盘符也能 import 到 asr_batch，不必改代码。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import asr_batch as B

STAGE1_LOG = os.path.join(B.OUT_ROOT, "_asr_stage1.log")


def md_state(md_path):
    """返回 (是否需要渲染, 原有文件名关键词)
    注意：只有缺总结时才复用旧关键词，避免误用占位符"""
    if not os.path.exists(md_path):
        return True, None
    old = open(md_path, encoding="utf-8").read()
    if "## 视频内容总结" in old:
        return False, None
    m = re.search(r"(?m)^关键词：(.*)$", old)
    kw = m.group(1).strip() if m else None
    if kw in ("—", "", None):
        kw = None
    return True, kw


done = fail = 0

# 单实例锁：定时任务重复拉起时直接退出，避免对同一批文件重复调 LLM
if not B.acquire_lock("stage2"):
    print("[lock] 已有 stage2 在运行，本次退出", flush=True)
    raise SystemExit(0)

# 只在「本次启动之后」新写入的日志里找 '阶段1 完成'，
# 否则历史日志里已有的这句会让守护进程立刻误判收工而退出。
try:
    _start_off = os.path.getsize(STAGE1_LOG) if os.path.exists(STAGE1_LOG) else 0
except Exception:
    _start_off = 0

try:
    while True:
        raws = [f for f in os.listdir(B.RAW_DIR) if f.endswith(".json")]
        pending = []
        for fn in sorted(raws):
            try:
                data = json.load(open(os.path.join(B.RAW_DIR, fn), encoding="utf-8"))
            except Exception:
                continue
            outdir = os.path.join(B.AUTHORS_DIR, data["author"])
            md_path = os.path.join(outdir, f"{data['pub']}_{data['author']}_{data['vid']}.md")
            need, old_kw = md_state(md_path)
            if need:
                pending.append((data, outdir, md_path, old_kw))

        for data, outdir, md_path, old_kw in pending:
            vid = data["vid"]
            try:
                os.makedirs(outdir, exist_ok=True)
                kws = old_kw or B.extract_keywords(data.get("title", ""), data.get("text", ""))
                summary = B.extract_summary(data.get("title", ""), data.get("text", ""))
                md, cover_name = B.render_md(data, kws, summary)
                if os.path.exists(data["cover"]):
                    shutil.copy2(data["cover"], os.path.join(outdir, cover_name))
                open(md_path, "w", encoding="utf-8").write(md)
                done += 1
                flag = "含总结" if summary else "无总结(降级)"
                print(f"[render] OK {vid} {data['author']} [{flag}] | {kws}", flush=True)
            except Exception as e:
                fail += 1
                print(f"[render] FAIL {vid}: {e}", flush=True)

        stage1_over = False
        try:
            if os.path.exists(STAGE1_LOG):
                with open(STAGE1_LOG, encoding="utf-8", errors="replace") as fh:
                    fh.seek(_start_off)
                    txt = fh.read()
                stage1_over = "阶段1 完成" in txt
        except Exception:
            pass
        if stage1_over and not pending:
            print(f"\n[render] 全部完成: 新增{done} 失败{fail}", flush=True)
            break
        time.sleep(20)
finally:
    B.release_lock("stage2")
    print("[lock] stage2 锁已释放", flush=True)
