# -*- coding: utf-8 -*-
"""运行子进程并按 utf-8/gbk 自动解码输出到文件，避免 PowerShell 重定向乱码。
用法: python _run_cap.py <out_txt> <cwd> <exe> [args...]
"""
import os
import subprocess
import sys

out_txt, cwd, exe = sys.argv[1], sys.argv[2], sys.argv[3]
args = sys.argv[4:]
env = dict(os.environ)
env["CODEBUDDY_SAFE_DELETE_ENABLED"] = "0"
env["PYTHONIOENCODING"] = "utf-8"
p = subprocess.run([exe] + args, cwd=cwd, env=env,
                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
raw = p.stdout
text = None
for enc in ("utf-8", "gbk"):
    try:
        text = raw.decode(enc)
        used = enc
        break
    except Exception:
        continue
if text is None:
    text = raw.decode("utf-8", errors="replace")
    used = "utf-8(replace)"
with open(out_txt, "w", encoding="utf-8") as f:
    f.write("### exit=%s encoding=%s\n" % (p.returncode, used))
    f.write(text)
print("exit=%s encoding=%s -> %s" % (p.returncode, used, out_txt))
