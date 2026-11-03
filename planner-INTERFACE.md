# planner.py 交接说明

这个文件只负责算决策，不碰网络、不碰文件，输入输出都是普通 Python 对象。
`console.py` 是唯一调用方，只要你保持下面的返回结构不变，改完重启控制台就能看到效果。

## 调用方式

```python
from planner import build_plan

plan = build_plan(
    now,              # float，当前 epoch
    quota,            # dict，app-server 返回的 result["rateLimits"]
    credits,          # list[dict]，每项至少含 expiresAt / secondsLeft
    horizon_short,    # float，订阅到期 epoch（settings.json 的 subscriptionEnd）
    horizon_long,     # float，horizon_short + renewalDays 天
    log_path,         # 守护进程日志，里面夹着历史 rate limits 行
    telemetry_path,   # telemetry.jsonl，控制台每次轮询追加的样本
)
```

## 返回结构（console.py 依赖这些字段）

```python
{
  "ok": True,
  "k": 6.39,            # 周额度 ≈ 几个五小时窗口
  "rate": 0.35,         # 消耗速度，窗口/小时
  "weeklyLeftWU": 1.47,
  "windowLeftWU": 0.13,
  "short": {
    "horizonHours": 82.6,
    "best": {"action": "redeem:0", "label": "…", "value": 14.32, "delta": 0.0},
    "alternatives": [ … ],
    "allTied": False,
    "timeline": [
      {"t": 1793…, "kind": "now|redeem|blocked|weekly|expire|gap|end",
       "label": "兑换 Reset A", "reason": "现在执行"},
    ],
  },
  "long": { … 同上 … },
}
```

`kind` 只用于前端配色，新增取值会退化成灰色，不会报错。
`label` 直接显示在卡片标题上，保持短。

## 当前建模假设（都写在代码里的常量）

| 常量 | 含义 | 当前值 |
|---|---|---|
| `FIVE_HOURS` | 五小时窗口长度 | 5 小时 |
| `WEEK` | 周额度周期 | 7 天 |
| `RESET_ELIGIBILITY` | 可兑换阈值：某项剩余低于 80% | 0.20 |
| `REDEEM_RESTARTS_WEEKLY` | 兑换后周额度是否也重新计时（推断，未实测） | True |
| `DEFAULT_RATE` | 采样不足时的默认消耗速度（11-03 实测约 2.2） | 2.0 窗口/小时 |
| `MIN_RATE` / `MAX_RATE` | 速度估计的钳制范围 | 0.05 / 6.0 |

单位：1 WU = 一个完整五小时窗口的用量。周额度容量记作 `K` WU，
`K = Σ Δprimary / Σ Δsecondary`（只用两边都没重置的相邻样本），
`rate = Σ Δprimary / Σ Δt`（只用五小时计数在涨的相邻样本，空闲只会让它偏低）。

## 模型（2026-11-03 重写）

确定性 MDP：
- 队列一直有活，两个窗口都有余量时就按 `rate` 消耗；闲着没有好处，所以唯一的决策是**何时兑换 Reset**。
- 窗口还没开始时（`e5`/`e7` 为 None），下一次消耗时开始计时；自然重置后回到「没开始」。
  API 在 0% 时也会给 `resetsAt = 现在 + 窗口长度`，所以 0% 当作没开始。
- 兑换：没满的窗口补到 100%，并重新计时（五小时已确认，周额度见上表）。
- 决策点只有两种：被卡住时（任一窗口用完），和下一个 Reset 即将到期时。
  在没卡住时提前兑换，补回的量一样、之前少用一段，所以不会更好。
- 兑换顺序：最早到期优先。
- 求解：在这些决策点上做穷举搜索加记忆化（`_Search`），得到精确最优。
  11-03 用 30 分钟网格的暴力枚举对照过，1–2 个 Reset 的四组结果完全一致。
- 时间线里的原因栏：如果 K ±15% 或速度减半/翻倍会改变建议，会写「… 时结论会变」。

返回结构和以前一样；`best.action` 现在只有 `use` 和 `redeem:i`（去掉了总和 `use` 并列的 `wait`）。

## 已知问题

1. `REDEEM_RESTARTS_WEEKLY` 是推断。改成 False 后，11-03 的长期面板从 49.44 变成 51.44，
   兑换时间也会变。第一次真兑换时，记下兑换前后 `secondary.resetsAt`，就能定下来。
2. `K` 和 `rate` 仍是点估计；样本少时只能当量级参考，敏感性提示能部分弥补。

## 怎么调试

`debug_plan.py` 是离线跑一次的脚本，不依赖网络和状态文件：

```bat
python debug_plan.py
```

改动后先跑它，再看控制台：

```bat
rem 控制台进程不会自动重载代码，改完要重启
powershell -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*console.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
console.cmd
```

## 依赖关系

```
planner.py  ← 纯计算，无副作用
   ↑
console.py  ← HTTP + 页面，调用 build_plan，展示结果
   ↑
codex_auto_resume.py  ← 守护进程，只负责等待和续跑，不参与决策
```

改 `planner.py` 不需要动守护进程，正在跑的续跑任务不受影响。
