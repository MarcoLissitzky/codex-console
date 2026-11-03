# codex-console

给 Codex 排队派活的小工具，跑在 Windows 本机：

- 工单队列：任务写成工单丢进文件夹，或者在网页上提交，它按顺序一张一张交给 Codex。每张工单开一个新会话，撞了额度就等重置，再接着跑同一个会话。
- 自动续跑：只盯一个已有会话，额度重置后替你按一次「继续」。
- 网页控制台：额度、储备 Reset、什么时候兑换最划算，都在一页里。提交工单、改参数、兑换 Reset 也在这里。

最早的守护进程改自 [201224tlx-sketch/codex-auto-resume-watchdog](https://github.com/201224tlx-sketch/codex-auto-resume-watchdog)（MIT）。之后加了工单队列、控制台和兑换规划，改动比较大，所以单独成库。

## 需要什么

- Windows，Python 3.10+（只用标准库；字体工具要 `fonttools`、`brotli`）
- Codex 桌面版。脚本会自动找 `%LOCALAPPDATA%\OpenAI\Codex\bin\*\codex.exe` 里最新的一个，也可以用 `--codex` 指定。npm 装的旧版 `codex` 可能用不了账户默认模型。

## 快速开始

```bat
copy settings.example.json settings.json
rem 按需改 settings.json，见下

open-console.cmd     rem 打开控制台（Edge 独立窗口）；只想起服务就用 console.cmd，再访问 http://127.0.0.1:8770
queue.cmd            rem 启动工单队列
```

第一次打开控制台，先在「工单」一栏的「允许的工作目录」里添加你的仓库目录。Codex 只会在这些目录下面干活。然后填标题、工作目录、任务正文，提交。

## 设置（settings.json）

| 键 | 含义 | 缺省 |
|---|---|---|
| `queueDir` | 工单队列目录，相对路径以脚本目录为基准。控制台里可以点「选择…」改 | `../codex-queue` |
| `subscriptionEnd` | 本期订阅到期时间，兑换规划以它为终点 | 现在 + 7 天 |
| `renewalDays` | 「再续订」方案多算几天 | `30` |
| `port` | 控制台端口 | `8770` |
| `autoRedeemMinWaitMinutes` | 自动兑换：离五小时窗口自动重置不到这么多分钟就不兑换，直接等 | `60` |

## 工单队列

`queue.cmd` 启动 `codex_queue.py`，按文件夹名顺序处理 `inbox` 里的工单：

```
codex-queue/
  inbox/      待派
  running/    正在跑（中途停掉的，下次启动接着续跑原会话）
  done/       完成：result.md（Codex 最后一条消息）、run.jsonl（原始输出）、meta.json（会话号、续跑次数）
  failed/     失败、撤回或续跑次数用完；原因在 meta.json
  PAUSE       存在时，当前这张跑完后不再取新的
  queue_config.json   allowed_roots（允许的工作目录）、default_max_resumes
```

在网页上提交最省事：填标题、工作目录（可以点「选择…」弹出系统的文件夹选择框）、沙箱、续跑上限、正文，可以带附件，提交后立即排队。列表里点开一张工单，能看 Codex 的交付总结和工单原文；待派的可以撤回（挪进 failed，不删除），失败的可以重派（旧输出改名留底，用新会话重跑）。

手写工单也行：在 `inbox` 里建一个文件夹，放 `ticket.md`（或者直接放一个 `<名字>.md`）。开头几行写参数，下面是发给 Codex 的正文：

```
---
cwd: D:\work\my-repo
sandbox: workspace-write        # 或 read-only
max_resumes: 3                  # 可省略
images: a.png, b.png            # 可省略，路径相对工单文件夹
---
任务正文：输入在哪、要做什么、产出放哪、怎么验收。
```

手写的工单要在文件夹 60 秒没有变化后才会被取走，免得同步盘还没同步完就开跑。网页提交的不用等。

几条规矩：

- `cwd` 必须在 `allowed_roots` 里某个目录下面；沙箱只允许 `read-only` 和 `workspace-write`。
- 队列开的是 `codex exec` 非交互会话，中途不会停下来问你，正文要把要求写全。
- 守护进程（`watch.cmd`）和队列（`queue.cmd`）共用 `daemon.pid`，同一时间只能开一个。
- 停止：在 `runtime` 文件夹里建一个 `STOP` 文件，或者点控制台的「停止守护」。

## 只续跑一个会话

```bat
watch.cmd --check                          rem 只看额度和计划的恢复时间
watch.cmd --session <会话ID>               rem 盯住这个会话，重置后自动续跑
watch.cmd --session <ID> --max-resumes 0   rem 一直续到任务结束
```

它通过 `codex app-server` 的 `account/rateLimits/read` 读额度，不靠发消息试探；等到 `primary.resetsAt` 之后再确认一次额度，然后 `codex exec resume` 一次。不给 `--session` 时锁定 `~/.codex/sessions` 里最新的会话。

## 兑换 Reset

账户里的储备 Reset 可以把额度窗口立刻重置。控制台的「参数」一栏里：

- 手动兑换：选一枚 Reset，点「兑换…」。先看两个窗口还剩多少，确认后才真的兑换。至少一个窗口剩余低于 80% 才能兑换。
- 自动兑换：打开后，额度耗尽、而且离五小时窗口自动重置还超过 `autoRedeemMinWaitMinutes` 分钟时，守护进程或队列会兑换最早到期的那枚。永不过期的 Reset 留到最后。

两种都会**真的消耗 Reset**，打开前都会再问一次。接口是 Codex app-server 的 `account/rateLimitResetCredit/consume`，结果会显示成「已兑换」「没有需要重置的窗口」「没有可用的 Reset」之一。

### 什么时候兑换划算

控制台顶上的建议来自 `planner.py`，算的是到订阅结束前一共还能用多少额度：

- 把一个完整五小时窗口的用量记作 1 个单位，周额度约等于 `K` 个单位。`K` 从日志里两个百分比一起变化的幅度反推。
- 假设队列一直有活。要决定的只有什么时候兑换哪一枚，按到期时间从早到晚用。
- 在「被卡住」和「某枚 Reset 快到期」这些时刻上把「兑换 / 不兑换」穷举一遍，取总量最大的方案。
- 同时给两个终点的答案：本期结束就不续订，和再续订 `renewalDays` 天。两个答案一样，说明续不续订只影响总量，不影响现在该怎么做。

细节见 `docs/planner-INTERFACE.md`。自动兑换目前只看「卡住了而且离重置还早」，还没有接 planner 的建议。

## 控制台

两套皮肤，右上角切换：朱墨（宋体、朱砂批注）和赛马（赛马排位表）。参数面板和工单面板嵌在各自的版面里。

字体不随仓库分发。想要设计好的字体，用 `tools/build_fonts.py` 从源字体（Google Fonts / Noto，都是 SIL OFL）生成 `fonts/*.woff2`；不生成也能用，会退回系统字体。

## 文件

| 文件 | 作用 |
|---|---|
| `codex_auto_resume.py` | 守护进程：读额度、等重置、续跑、兑换 |
| `codex_queue.py` | 工单队列 |
| `console.py` / `console.html` | 控制台后端 / 页面 |
| `planner.py` | 兑换规划 |
| `settings.py` | 读写 settings.json |
| `tools/probe_credits.py` | 打印额度和 Reset 的原始数据（只读） |
| `tools/build_fonts.py` | 生成控制台字体 |
| `*.cmd` | Windows 启动脚本 |

运行时文件（日志、状态、`daemon.pid`、`control.json`、`telemetry.jsonl`、`STOP`）都在 `runtime/` 里，第一次运行自动建。它和 `settings.json` 都不进仓库。

## 注意

- 如果你的 `~/.codex/config.toml` 设了 `sandbox_mode = "danger-full-access"`，脚本会用 `--sandbox` 覆盖掉。无人值守时别改成完全访问。
- 电脑睡眠时它也睡；醒来如果已经过了重置时间，会马上接着跑。
- 它不会绕过限额。额度用完就等，重置之后才接着派活。

## 许可

MIT，见 [LICENSE](LICENSE)。
