"""打印 account/rateLimits/read 的原始结果（额度窗口 + 储备 Reset），只读，不兑换。

用法：python probe_credits.py
"""
import json
import time

from codex_auto_resume import AppServerClient, find_codex

client = AppServerClient(find_codex())
client.start()
try:
    result = client.request("account/rateLimits/read", timeout=30).get("result", {})
finally:
    client.close()

print("now_epoch =", int(time.time()))
print(json.dumps(result, ensure_ascii=False, indent=2))
