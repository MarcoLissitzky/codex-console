"""用户设置：settings.json（与脚本同目录，不进仓库）。缺省值见 DEFAULTS，示例见 settings.example.json。"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = BASE_DIR / "settings.json"

DEFAULTS = {
    # 工单队列目录；相对路径以本目录为基准
    "queueDir": "../codex-queue",
    # 本期订阅到期时间（本地时间），planner 以它为终点；留空则按 7 天估算
    "subscriptionEnd": None,
    # 「再续订」方案多算的天数
    "renewalDays": 30,
    # 控制台端口
    "port": 8770,
    # 自动兑换：离五小时窗口自动重置还不到这么多分钟时，不动用 Reset，直接等
    "autoRedeemMinWaitMinutes": 60,
}


def load_settings() -> dict:
    merged = dict(DEFAULTS)
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8-sig"))
        merged.update({k: v for k, v in data.items() if k in DEFAULTS})
    except FileNotFoundError:
        pass
    except Exception as exc:  # 写坏了就用缺省值，不让控制台起不来
        print(f"settings.json 读取失败，使用缺省值：{exc}")
    return merged


def save_settings(updates: dict) -> dict:
    current = {}
    try:
        current = json.loads(SETTINGS_FILE.read_text(encoding="utf-8-sig"))
    except Exception:
        pass
    current.update({k: v for k, v in updates.items() if k in DEFAULTS})
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, SETTINGS_FILE)
    return load_settings()


def queue_dir(settings: dict | None = None) -> Path:
    raw = (settings or load_settings()).get("queueDir") or DEFAULTS["queueDir"]
    p = Path(os.path.expandvars(os.path.expanduser(str(raw))))
    return (p if p.is_absolute() else BASE_DIR / p).resolve()


def subscription_end(settings: dict | None = None, now: float | None = None) -> float:
    raw = (settings or load_settings()).get("subscriptionEnd")
    if raw:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(str(raw), fmt).timestamp()
            except ValueError:
                continue
    import time
    return (now or time.time()) + 7 * 86400
