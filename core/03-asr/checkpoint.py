# -*- coding: utf-8 -*-
"""检查点：诊断跑批健康度 → 打印进度快照 → 必要时拉起 stage1/stage2/webui。

被「每 2 小时同步进度」的定时任务调用。设计为幂等：
- 已运行的不重复拉起（靠 _stage1.lock / _stage2.lock / _webui.lock 单实例锁）
- 拉起是尽力而为（Popen detached）；若本进程随会话退出而被回收，
  定时任务里的 agent 还会用后台方式再拉一次（有锁兜底，不会重复）

退出码：
  0 = 全部正常
  1 = 有组件缺失（尾行会打印 ACTION: 行，供 agent 用后台方式重启）
"""
import os, sys, subprocess, time

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("WB_SOURCE", "lib")
sys.path.insert(0, HERE)

PY = os.environ.get("WB_PY") or os.path.join(
    os.path.expanduser("~"),
    ".workbuddy", "binaries", "python", "envs", "video-analyzer", "Scripts", "python.exe")

import asr_batch as B          # noqa: E402
import progress                # noqa: E402
import lib_source              # noqa: E402

CREATE_FLAGS = 0
if os.name == "nt":
    CREATE_FLAGS = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | 0x08000000  # NO_WINDOW


def _launch(cmd, logpath, env_extra=None):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    f = open(logpath, "ab")
    subprocess.Popen(cmd, cwd=HERE, stdout=f, stderr=subprocess.STDOUT,
                     stdin=subprocess.DEVNULL, env=env, creationflags=CREATE_FLAGS)


def _last_source():
    """从 stage1 日志里取最后一次启动打印的 '[源] xxx'，用于校验数据源没跑错"""
    p = os.path.join(HERE, "_asr_stage1.log")
    try:
        txt = open(p, encoding="utf-8", errors="replace").read()
    except Exception:
        return None
    import re
    ms = re.findall(r"\[源\]\s*(\w+)", txt)
    return ms[-1] if ms else None


def main():
    snap = progress.snapshot()
    actions = []
    env_extra = {}
    want = os.environ.get("WB_SOURCE", "lib")
    cur_src = _last_source()
    if cur_src and cur_src != want:
        actions.append(f"⚠️ stage1 当前数据源='{cur_src}'，期望='{want}' —— 跑错了！需停掉后用 WB_SOURCE={want} 重启")
    # 复用 _llm_config.sh 里的 LLM 配置（若存在，从 shell 里取）
    cfg = os.path.join(HERE, "_llm_config.sh")
    if os.path.exists(cfg):
        try:
            out = subprocess.run(["bash", "-c", f'. "{cfg}" >/dev/null 2>&1; '
                                  'env | grep -E "^WB_LLM_BACKEND=|^WB_CLOUD_KEY=|^WB_CLOUD_MODEL=|^WB_CLOUD_DISABLE_THINK=|^WB_OLLAMA|^WB_SOURCE="'],
                                 capture_output=True, timeout=30).stdout.decode("utf-8", "replace")
            for line in out.splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    env_extra[k.strip()] = v.strip()
        except Exception:
            pass
    env_extra.setdefault("WB_SOURCE", want)

    # 放行因 ASR 服务掉线等基础设施原因被 abandoned 的条目（有轮次上限），
    # 否则这些条目会永久留在注册表外，最终交付就是缺的。
    try:
        freed = B.retry_infra(3)
        if freed:
            actions.append(f"放行 {len(freed)} 条基础设施类失败条目，重新进入转写队列")
    except Exception as e:
        print(f"[retry] 执行失败（忽略）: {e}")

    if not snap["server"]:
        actions.append("ASR 服务 8766 离线（nssm 服务 'VideoAnalyzer-Transcribe' 常驻守护，"
                       "可 `Restart-Service VideoAnalyzer-Transcribe`；stage1 也会经 ensure_server 自愈，"
                       "但它只在**端口无监听**时才起进程，避免与服务抢出重复实例）")

    if not snap["stage1_alive"]:
        _launch([PY, os.path.join(HERE, "asr_batch.py"), "stage1"],
                os.path.join(HERE, "_asr_stage1.log"), env_extra)
        actions.append("stage1 未运行 → 已尝试拉起")

    if not snap["stage2_alive"]:
        _launch([PY, os.path.join(HERE, "_stage2_daemon.py")],
                os.path.join(HERE, "_asr_stage2.log"), env_extra)
        actions.append("stage2 未运行 → 已尝试拉起")

    # WebUI（转写看板）要求长期在线：同样纳入巡检兜底。
    # 判定用「HTTP 可达」而非仅看 pid —— 进程还在但端口没起来（如启动时被占）也算缺失。
    if not snap.get("webui_alive"):
        _launch([PY, os.path.join(HERE, "webui.py")],
                os.path.join(HERE, "_asr_webui.log"), env_extra)
        actions.append("WebUI 未运行 → 已尝试拉起（http://127.0.0.1:8770/）")

    time.sleep(5)   # 给刚拉起的进程一点启动时间

    print("\n[checkpoint] 诊断结论:")
    if actions:
        for a in actions:
            print("  ACTION: " + a)
        print("\nACTION-HINT: 若上面这些组件在 5 秒后仍未运行(见上方 pid=未运行)，"
              "请用后台方式重新执行对应脚本：")
        print(f"  stage1 → {PY} asr_batch.py stage1   (cwd={HERE}, 追加输出到 _asr_stage1.log)")
        print(f"  stage2 → {PY} _stage2_daemon.py      (cwd={HERE}, 追加输出到 _asr_stage2.log)")
        print(f"  webui  → {PY} webui.py               (cwd={HERE}, 追加输出到 _asr_webui.log)")
        return 1
    print("  全部健康，无需干预。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
