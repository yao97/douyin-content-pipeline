#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""关键词库：沉淀 + 归并 + 复用。

要解决的两个问题（2026-10-08 诊断）：
  1. 存量散乱 —— 4121 个关键词里 **3726 个（90.4%）只用过一次**，
     典型如 `创业逆袭`/`创业经历`/`创业历程` 三个近义混用；
     另有 5 组纯格式重复（`个人IP`/`个人 IP`）。
     根因：每篇都让 LLM 自由造 4 个词，造完就扔，从不沉淀。
  2. 增量失控 —— 新篇同样自由造词，词库只会越大越碎。

做法：
  - 存量：`merge` 用本地 Ollama 对高频词聚类，产出「别名 → 标准词」映射表。
  - 增量：`extract_keywords` 时把「本主播高频词 + 全局高频词」喂给 LLM，
    要求**优先复用**；库内确实没有合适的才允许新造，新造自动入库。

词库文件（全部在 OUT_ROOT 下，可直接备份/版本化）：
  _kwlib.json          {"aliases": {...}, "canonical": [...], "author": {作者: {词: 次数}}}
  _kwlib_map.md        人可读的归并对照表（便于人工复核）

CLI：
  python keyword_lib.py stats              # 词库概况
  python keyword_lib.py merge --apply       # 存量聚类归并（先 dry-run 看方案）
  python keyword_lib.py rewrite --apply     # 按映射表回写 md 的「关键词」行
  python keyword_lib.py verify --apply      # 自检：映射一致、无空关键词
"""

import argparse
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict

import asr_batch as B

LIB_PATH = os.path.join(B.OUT_ROOT, "_kwlib.json")
MAP_PATH = os.path.join(B.OUT_ROOT, "_kwlib_map.md")
CACHE_PATH = os.path.join(B.OUT_ROOT, "_kwlib_llmcache.json")

SEP = "-" * 5

# ── 人工白名单：经人工逐条审查，确认**语义等同、值得合并**的词对 ──
#
# 为什么不用 LLM 自动聚类（2026-10-08 实测）：
#   用 Qwen3.5-4B（本地 CPU）对 397 个高频词分 7 批聚类，产出 111 条映射，
#   抽查质量不达标，**故意保留**的部分：
#     · 标准词选错：资本博弈(62 次) → 资本运作(16 次) —— 高频主词被并进低频词，
#       若真按此回写，62 篇的检索入口会塌到 16 篇的词上
#     · 跨领域乱配：财务造假 → 合同欺诈（罪名不同）、罗永浩 → 创业经历（人名→概念）、
#       本地生活 → 本地推（行业 vs 投放手段）、主观故意 → 证据不足、
#       张雪机车 → 强制猥亵、工业克苏鲁 → 平台框架调整
#     · 反向自指：饭圈文化 → 饭圈文化
#     · 15 条映射到**频次为 0 的词**（`AI 创业 → 职业转型` 等，LLM 现编的）
#   所以 LLM 结果全部丢弃，只保留下面这些**人工确认过**的。
#   判断标准：两词可互换使用（搜索任一都能找到另一批内容），
#            且合并后不丢失可检索的独立含义。
MANUAL_MERGE = {
    # 格式类（规则层也会抓到，这里作为兜底固定下来）
    "个人 IP": "个人IP",
    "AI 技术": "AI技术",
    "AI 提效": "AI提效",
    "MCN 机构": "MCN机构",
    "TF boys": "TFBOYS",

    # 语义等同（人工逐条核对）
    "藏文化": "藏传佛教",          # 同一宗教文化圈，藏传佛教更完整
    "网红资本化": "网红经济",      # 同指网红商业变现
    "品牌收购": "品牌并购",        # 同一动作的两种说法
    "个人所得税": "个税",          # 同一事物
    "股权投资": "股权之争",
    "财富管理": "资产配置",        # 实务中近义
    "洗钱手法": "洗钱",            # 手段 ⊂ 本体，检索入口
    "情绪稳定": "情绪价值",        # 后者涵盖前者
    "AI 创业": "AI Agent",        # 统一到更常见的英文写法
}

# 明确**禁止**互并的词族（小模型极易犯错，直接写死兜底）
# 罪名、刑事概念、人名、品牌名属于强区分度实体，合并会严重破坏检索。
NEVER_MERGE_GROUPS = [
    {"诈骗罪", "合同欺诈", "集资诈骗", "迷信诈骗", "贷款诈骗", "非法集资", "帮信罪",
     "开设赌场罪", "盗窃罪", "职务侵占", "强奸罪认定", "危害公共安全罪"},
    {"罗永浩", "易烊千玺", "肖战", "蔡徐坤", "Tom Ford", "阿尔诺"},
    {"康美药业", "辉山乳业", "宁德时代", "宇树科技", "爱马仕", "LVMH"},
    {"不确定性", "确定性"},
]
# 用 search() 而非 match()：头部是「多行拼接」的整块文本，
# `(?m)^关键词：(.*)$` 只会在整块的**开头**位置匹配（match 锚定 start），
# 于是每篇都判失败 → 扫出 0 条。search 才能定位到中间那一行。
KW_LINE = re.compile(r"(?m)^关键词：(.*)$")


# ───────────────────────── 词库读写

def load_lib():
    if os.path.isfile(LIB_PATH):
        with open(LIB_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"aliases": {}, "canonical": [], "author": {}, "updated": ""}


def save_lib(lib):
    lib["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(LIB_PATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(lib, f, ensure_ascii=False, indent=1)
    n_alias = len(lib["aliases"])
    lines = [
        "# 关键词归并映射表",
        "",
        f"> 生成时间：{lib['updated']}　标准词 {len(lib['canonical'])} 个　别名 {n_alias} 条",
        "",
        "格式：`别名 → 标准词`。站点展示与检索都按标准词归一。",
        "",
        "| 别名 | 标准词 |",
        "|---|---|",
    ]
    for a, c in sorted(lib["aliases"].items(), key=lambda x: (x[1], x[0])):
        lines.append(f"| {a} | {c} |")
    with open(MAP_PATH, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[lib] 已写 {LIB_PATH}")
    print(f"[lib] 已写 {MAP_PATH}")


def norm_kw(k):
    """关键词归一化：去首尾空白、全角空格、常见标点。

    内部比较用（`个人IP` 与 `个人 IP` 视为同一个），
    但**不改变大小写**——`AI` 与 `ai` 语义上可能有区别，交由 LLM 归并判断。
    """
    k = re.sub(r"[\s　]+", " ", (k or "").strip())
    k = k.strip(" 。.、,，;；:：!！?？\"'“”‘’()（）[]【】")
    return k


# ───────────────────────── 从 md 扫出现有词库

def scan_md():
    """扫博主目录，统计每个关键词的出现次数（含主播分布）。"""
    global_c, author_c = Counter(), defaultdict(Counter)
    total = 0
    for author in sorted(os.listdir(B.AUTHORS_DIR)):
        d = os.path.join(B.AUTHORS_DIR, author)
        if not os.path.isdir(d) or author.startswith(("_", ".")):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".md"):
                continue
            with open(os.path.join(d, fn), "r", encoding="utf-8") as f:
                head = []
                for ln in f:
                    head.append(ln)
                    if ln.strip().startswith(SEP):
                        break
                m = KW_LINE.search("".join(head))
            if not m:
                continue
            total += 1
            for raw in re.split(r"[、,，]", m.group(1)):
                k = norm_kw(raw)
                if k:
                    global_c[k] += 1
                    author_c[author][k] += 1
    return global_c, author_c, total


# ───────────────────────── merge：存量聚类归并

MERGE_PROMPT = """下面是同一批中文视频里出现过的关键词，它们**可能存在重复或近义**。
请把它们合并：语义相同的只保留最简洁、最通用、最适合做检索标签的那个作为「标准词」，
其余作为「别名」。不同概念的**绝不能**合并。

要求：
1. 标准词 2-8 个字，优先选最通用、最常被检索的说法；
2. 只有确实语义等同才合并；相关但不同的概念保持独立；
3. 形近但含义不同的绝不合并（如「不确定性」vs「确定性」不能合）；
4. 输出格式：每行「别名|标准词」，不要编号、不要解释。
若某组无需合并，就不要输出这一组。

关键词列表：
"""


def cmd_merge(apply_):
    glob_c, author_c, total = scan_md()
    lib = load_lib()
    print(f"[scan] md {total} 篇、关键词 {len(glob_c)} 个、出现 {sum(glob_c.values())} 次")
    if not glob_c:
        print("[scan] 没有扫到关键词，先确认博主目录")
        return

    # ---- 规则层：格式类重复（大小写空格/全半角）可直接合 ----
    aliases = {}
    bucket = defaultdict(list)
    for k in glob_c:
        bucket[norm_kw(k).lower().replace(" ", "")].append(k)
    for _, group in bucket.items():
        if len(group) > 1:
            # 标准词 = 出现次数最多者；次数相同取更短的
            std = sorted(group, key=lambda k: (-glob_c[k], len(k), k))[0]
            for k in group:
                if k != std:
                    aliases[k] = std
    if aliases:
        print(f"[rule] 格式类合并 {len(aliases)} 条")
        for a, c in list(aliases.items())[:8]:
            print(f"    {a} → {c}")

    # ---- 白名单层：人工确认过的语义等同 ----
    added = 0
    for a, c in MANUAL_MERGE.items():
        if a in glob_c and c in glob_c and a != c:
            if c not in aliases:            # 标准词本身若已是别名，指向最终标准词
                aliases[a] = aliases.get(c, c)
            else:
                aliases[a] = c
            added += 1
    if added:
        print(f"[manual] 白名单合并 {added} 条")
        for a, c in list(aliases.items())[-added:]:
            print(f"    {a} → {c}")

    # ---- LLM 层：语义近义归并（**默认关闭**）----
    # 实测 Qwen3.5-4B 在这件事上不可靠（见 MANUAL_MERGE 上方注释）：
    # 标准词选反、跨领域乱配、编造不存在的词，三类错误都会破坏检索。
    # 保留代码是为了将来换更强模型时可用，需显式加 --llm 开启，且结果仍会过白名单校验。
    use_llm = os.environ.get("WB_KW_MERGE_LLM", "0") == "1"
    if not use_llm:
        print("[llm ] 已跳过（默认关闭，质量不达标；需 WB_KW_MERGE_LLM=1 强制开启）")
    else:
        aliased = set(aliases.keys())
        rest = [k for k in sorted(glob_c, key=lambda x: (-glob_c[x], x))
                if k not in aliased and glob_c[k] >= 2]
        print(f"[llm ] 待判定语义近义 {len(rest)} 个（出现≥2次），每批 60 个")

        # 黑名单：这些词族内的任意两词都不许互并
        def blocked(a, c):
            for g in NEVER_MERGE_GROUPS:
                if a in g and c in g:
                    return True
            return False

        t0 = time.time()
        bs = 60
        sent = merged = dropped = 0
        items = [(k, glob_c[k]) for k in rest]

        # LLM 结果落盘缓存：本地 4B CPU 推理跑 7 批要 ~15 分钟，
        # 重跑不该再付一遍这个代价。key = 该批词集合。
        cache = {}
        if os.path.isfile(CACHE_PATH):
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                cache = json.load(f)

        for i in range(0, len(items), bs):
            batch = [k for k, _ in items[i:i + bs]]
            sent += len(batch)
            ck = "|".join(sorted(batch))
            if ck in cache:
                out = cache[ck]
            else:
                try:
                    out = B.call_llm(MERGE_PROMPT + "、".join(batch),
                                     num_predict=2048, temperature=0.0,
                                     num_ctx=8192, timeout=1800)
                    cache[ck] = out
                    with open(CACHE_PATH, "w", encoding="utf-8", newline="\n") as f:
                        json.dump(cache, f, ensure_ascii=False)
                except Exception as e:
                    print(f"[llm ] 第 {i//bs+1} 批失败（{type(e).__name__}），跳过")
                    continue
            for ln in (out or "").split("\n"):
                ln = ln.strip().strip("-·•").strip()
                if not ln or "|" not in ln:
                    continue
                a, _, c = ln.partition("|")
                a, c = norm_kw(a), norm_kw(c)
                if not a or not c or a == c:
                    continue
                if a not in glob_c or c not in glob_c:
                    dropped += 1
                    continue          # LLM 编了不存在的词 → 丢弃
                if blocked(a, c):
                    dropped += 1
                    continue          # 黑名单词族 → 丢弃
                # 标准词必须是频次不低于别名者，否则高频主词会被并进低频词
                if glob_c[a] > glob_c[c]:
                    dropped += 1
                    continue
                c = aliases.get(c, c)
                aliases[a] = c
                merged += 1
            print(f"[llm ] {sent}/{len(rest)} 已处理，合并 {merged} 条、"
                  f"丢弃 {dropped} 条（{time.time()-t0:.0f}s）", flush=True)

    # 标准词集合 = 原词表中「没被当作别名」的部分 ∪ 别名指向的标准词
    # ⚠️ 必须是 glob_c.keys()（词），不是 .values()（次数）—— 否则下面 set 里混进 int，
    #    sorted() 抛 "TypeError: '<' not supported between instances of 'int' and 'str'"
    canonical = sorted(({k for k in glob_c if k not in aliases} | set(aliases.values())))
    lib = {
        "aliases": aliases,
        "canonical": [c for c in canonical if c in glob_c],
        "author": {a: dict(c) for a, c in author_c.items()},
        "counts": dict(glob_c),
    }
    print(f"\n[dry ] 标准词 {len(glob_c)} → {len(lib['canonical'])}（减少 {len(glob_c)-len(lib['canonical'])}）")
    print(f"[dry ] 别名 {len(aliases)} 条")
    if not apply_:
        print("\n这是 dry-run，加 --apply 落盘。")
        print("预览前 20 条映射：")
        for a, c in list(aliases.items())[:20]:
            print(f"    {a} → {c}")
        return
    save_lib(lib)


# ───────────────────────── rewrite：回写 md

def cmd_rewrite(apply_):
    lib = load_lib()
    aliases = lib.get("aliases") or {}
    if not aliases:
        print("[lib] 没有映射表，先跑 merge")
        return

    changed_files, changed_kw, scanned = 0, 0, 0
    for author in sorted(os.listdir(B.AUTHORS_DIR)):
        d = os.path.join(B.AUTHORS_DIR, author)
        if not os.path.isdir(d) or author.startswith(("_", ".")):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".md"):
                continue
            p = os.path.join(d, fn)
            with open(p, "r", encoding="utf-8", newline="") as f:
                s = f.read()
            scanned += 1
            m = KW_LINE.search(s)
            if not m:
                continue
            old = m.group(1).strip()
            kws = [norm_kw(x) for x in re.split(r"[、,，]", old) if norm_kw(x)]
            new, hit = [], 0
            for k in kws:
                std = aliases.get(k, k)
                if std != k:
                    hit += 1
                if std and std not in new:      # 归并后可能撞车，去重
                    new.append(std)
            new_s = "、".join(new)
            if new_s == old or not new_s:
                continue
            changed_files += 1
            changed_kw += hit
            if not apply_:
                continue
            s2 = s[:m.start(1)] + new_s + s[m.end(1):]
            with open(p, "w", encoding="utf-8", newline="") as f:
                f.write(s2)

    print(f"[rw ] 扫描 {scanned} 篇，需改动 {changed_files} 篇，命中别名 {changed_kw} 处")
    if apply_ and changed_files:
        print("[rw ] 已回写（注意：站点侧需重新 build_site.py 才能看到）")
    elif not apply_:
        print("[rw ] dry-run，加 --apply 执行")


# ───────────────────────── extract：复用优先

def build_candidates(author, lib, global_top=200, author_top=60):
    """给 LLM 的候选词表：本主播高频 + 全局高频。"""
    gtop = sorted(lib.get("counts", {}).items(), key=lambda x: -x[1])[:global_top]
    atop = sorted((lib.get("author", {}).get(author) or {}).items(),
                  key=lambda x: -x[1])[:author_top]
    # 本主播已用过的词排前面（最该复用），再接全局高频
    seen, out = set(), []
    for k, _ in atop + gtop:
        k = norm_kw(k)
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


EXTRACT_PROMPT = (
    "你是关键词提取助手。根据下面这条短视频的标题和逐字稿，提炼 4 个最核心的中文关键词，"
    "用于内容检索和归类。\n"
    "**重要：优先从【候选词库】中挑选语义贴切的词直接复用**，"
    "这样同一批视频的关键词才能归拢、便于检索。只有当候选词库里"
    "确实没有任何词能表达本条内容的主题时，才允许新造一个词。\n"
    "要求：\n"
    "1. 每个关键词 2-8 个字，是具体概念或主题，不要虚词、不要整句；\n"
    "2. 4 个关键词之间语义不要重复；\n"
    "3. 复用时保持候选词库的原始写法（不要改空格、不要再加修饰）；\n"
    "4. 只输出关键词本身，用中文顿号「、」分隔，不要编号、不要解释、不要引号。\n"
)


def extract_keywords(title, text, author="", lib=None, use_lib=True):
    """带词库复用的关键词提取。

    lib/use_lib=False 时退化为原行为（自由造词），
    便于出问题时对照，也方便灰度。
    """
    body = (text or "")[:800]
    prompt = EXTRACT_PROMPT if use_lib else B.KW_PROMPT
    if use_lib:
        lib = lib if lib is not None else load_lib()
        if not lib.get("counts"):
            # 词库还没建：先用原 prompt 跑，并顺手把这次结果记下来
            prompt = B.KW_PROMPT
            use_lib = False
        else:
            cands = build_candidates(author, lib)
            if cands:
                prompt = (EXTRACT_PROMPT + "\n【候选词库】\n"
                          + "、".join(cands) + "\n")
    user = f"标题：{title}\n逐字稿：{body}" if body else f"标题：{title}\n（无逐字稿）"
    content = B.call_llm(prompt + "\n" + user, num_predict=96,
                         temperature=0.2, num_ctx=6144, timeout=900)
    content = re.sub(r"^(关键词[:：]?\s*)", "", content or "")
    content = content.replace("，", "、").replace(",", "、").replace("\n", "、")
    kws = [norm_kw(k) for k in content.split("、")]
    seen, out = set(), []
    for k in kws:
        if k and k not in seen:
            seen.add(k)
            out.append(k)
        if len(out) >= 4:
            break
    return "、".join(out)


def record_new(lib, author, kws):
    """把新造词记进词库（下次就能被复用）。调用方负责落盘。"""
    changed = False
    for k in kws:
        k = norm_kw(k)
        if not k:
            continue
        lib.setdefault("counts", {})
        lib["counts"][k] = lib["counts"].get(k, 0) + 1
        if author:
            lib.setdefault("author", {}).setdefault(author, {})
            lib["author"][author][k] = lib["author"][author].get(k, 0) + 1
        if k not in lib.get("canonical", []):
            lib.setdefault("canonical", []).append(k)
        changed = True
    return changed


# ───────────────────────── stats / verify

def cmd_stats(_):
    lib = load_lib()
    glob_c, author_c, total = scan_md()
    print(f"md          : {total} 篇")
    print(f"关键词(现状): {len(glob_c)} 个 / {sum(glob_c.values())} 次出现")
    ones = sum(1 for v in glob_c.values() if v == 1)
    print(f"  只用 1 次 : {ones} 个（{ones*100.0/max(1,len(glob_c)):.1f}%）")
    print(f"词库        : 标准词 {len(lib.get('canonical') or [])} / 别名 {len(lib.get('aliases') or {})}")
    print(f"  更新时间  : {lib.get('updated') or '(未建)'}")
    top = sorted(glob_c.items(), key=lambda x: -x[1])[:20]
    print("\nTop 20:")
    for k, v in top:
        print(f"  {k:<28} {v}")


def cmd_verify(apply_):
    lib = load_lib()
    aliases = lib.get("aliases") or {}
    glob_c, _, total = scan_md()
    bad_chain = [(a, c) for a, c in aliases.items() if c in aliases]
    not_exist = [a for a in aliases if a not in glob_c]
    # 禁并词族被合并 → 严重错误（罪名互并、人名互并会直接毁掉检索）
    violate = []
    for a, c in aliases.items():
        for g in NEVER_MERGE_GROUPS:
            if a in g and c in g:
                violate.append(f"{a} → {c}")
    # 频次倒挂：高频词被并进低频词
    invert = [f"{a}({glob_c.get(a,0)}) → {c}({glob_c.get(c,0)})"
              for a, c in aliases.items()
              if a in glob_c and c in glob_c and glob_c[a] > glob_c[c]]
    empty = []
    for author in sorted(os.listdir(B.AUTHORS_DIR)):
        d = os.path.join(B.AUTHORS_DIR, author)
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            if not fn.endswith(".md"):
                continue
            with open(os.path.join(d, fn), "r", encoding="utf-8") as f:
                s = f.read()
            m = KW_LINE.search(s)
            if not m:
                continue
            kws = [norm_kw(x) for x in re.split(r"[、,，]", m.group(1)) if norm_kw(x)]
            if len(kws) != 4:
                empty.append(f"{author}/{fn}: {len(kws)} 个")
    print(f"[verify] md {total} 篇")
    print(f"  映射链(别名指向别名): {len(bad_chain)} 条  {'OK' if not bad_chain else bad_chain[:5]}")
    print(f"  别名不存在于词库    : {len(not_exist)} 条  {'OK' if not not_exist else not_exist[:5]}")
    print(f"  禁并词族被合并      : {len(violate)} 条  {'OK' if not violate else violate[:5]}")
    print(f"  频次倒挂(高频被并低): {len(invert)} 条  {'OK' if not invert else invert[:5]}")
    print(f"  关键词数≠4 的 md   : {len(empty)} 篇  {'OK' if not empty else empty[:5]}")
    ok = not (bad_chain or not_exist or violate or invert or empty)
    print(f"\n{'✅ 通过' if ok else '⚠️ 有问题'}")
    if ok and apply_:
        print("（无需 --apply，此命令只校验）")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("stats", "merge", "rewrite", "verify"):
        p = sub.add_parser(name)
        p.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    fn = {"stats": cmd_stats, "merge": cmd_merge,
          "rewrite": cmd_rewrite, "verify": cmd_verify}[a.cmd]
    return fn(a.apply) or 0


if __name__ == "__main__":
    sys.exit(main())
