# 运行配置 + LLM 通道配置模板
# 复制为 _llm_config.sh 并填入真实值；该文件只留在本机，**绝不要提交到版本库**。
#
# 用途：被 stage1 / stage2 / 定时任务 source，统一注入运行环境。

# ── 数据源开关 ───────────────────────────────────────────────
# lib     = D:\视频\自媒体视频库（本流水线使用）
# guanzhu = 旧源 C:\Users\EDY\Videos\data\关注（默认值，本流水线不要用）
export WB_SOURCE=lib

# ── LLM 通道（用于生成关键词与内容总结）─────────────────────
export WB_LLM_BACKEND=cloud
export WB_CLOUD_BASE="https://api.siliconflow.com/v1"
export WB_CLOUD_KEY="<你的 API Key，通常形如 sk-...>"
export WB_CLOUD_MODEL="deepseek-ai/DeepSeek-V4-Flash"
export WB_CLOUD_DISABLE_THINK=1

# ── 可选：调参 ───────────────────────────────────────────────
# export WB_RTF=0.85            # 端到端有效实时率（估时用）
# export WB_MIN_CHARS=10        # 判定「转写成功」的最小字数
# export WB_MAX_ATTEMPTS=2      # 内容失败重试上限
# export WB_SLICE_MIN=1200      # 触发切片的最小秒数（20 分钟）
# export WB_ZOMBIE_SEC=120      # 状态停滞超时 → 抛错重提
