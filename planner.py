#!/usr/bin/env python3
"""Decision planner for the Codex auto-resume watchdog.

Maximises the total amount of Codex usage before a horizon, given the current
5-hour window, the weekly window, and the banked resets.

Units: one unit (WU) is one full 5-hour window of usage.
The weekly allowance is `K` WU. K is estimated from the daemon log, because
the two percentages move at different rates.

Model (a deterministic MDP):
- The work queue never runs dry, so Codex burns quota at `rate` whenever both
  windows have room. Idling never helps, so the only decision is when to
  redeem a banked reset.
- A window that has not started (e5/e7 is None) starts at the next moment
  Codex consumes something. A natural refill leaves it not started.
- Redeeming refills every window that is not full and restarts its timer.
- Redeeming is only considered when Codex is blocked, or when the next reset
  is about to expire. Redeeming earlier than that gives the same refill and
  less usage before it.
- Resets are redeemed earliest-expiry first.
The best plan is found by exhaustive search over those decision points.
"""

import json
import re
import time
from datetime import datetime

WEEK = 7 * 86400
FIVE_HOURS = 5 * 3600
RESET_ELIGIBILITY = 0.20  # a window must be below 80% remaining to redeem
EPS = 1e-6

# Confirmed 2026-11-03: a redeem restarts the 5-hour timer.
# Inferred, not observed: it restarts the weekly timer too.
REDEEM_RESTARTS_WEEKLY = True

LOG_RE = re.compile(r"rate limits: (\{.*\})\s*$")


# ---------------------------------------------------------------------------
# telemetry
# ---------------------------------------------------------------------------

def load_samples(log_path, telemetry_path=None, limit=400):
    """Pull (timestamp, primary_used, secondary_used) out of the daemon log."""
    samples = []
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if "rate limits:" not in line:
                    continue
                m = LOG_RE.search(line.strip())
                if not m:
                    continue
                try:
                    payload = json.loads(m.group(1))
                except Exception:
                    continue
                primary = payload.get("primary") or {}
                secondary = payload.get("secondary") or {}
                pu = primary.get("usedPercent")
                su = secondary.get("usedPercent")
                if pu is None or su is None:
                    continue
                stamp = line.split(" | ")[0].strip()
                try:
                    ts = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").timestamp()
                except Exception:
                    continue
                samples.append({"ts": ts, "p": float(pu), "s": float(su)})
    except Exception:
        pass

    if telemetry_path:
        try:
            with open(telemetry_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        item = json.loads(line)
                    except Exception:
                        continue
                    if {"ts", "p", "s"} <= set(item):
                        samples.append({"ts": float(item["ts"]),
                                        "p": float(item["p"]),
                                        "s": float(item["s"])})
        except Exception:
            pass

    samples.sort(key=lambda x: x["ts"])

    # keep only strictly increasing timestamps
    out = []
    for item in samples:
        if out and item["ts"] <= out[-1]["ts"]:
            continue
        out.append(item)
    return out[-limit:]


def estimate_k(samples):
    """Estimate weekly capacity in 5-hour-window units.

    Both percentages are integers, so a single step of 1% on the weekly
    counter can be off by half. Summing all burn steps before dividing keeps
    that rounding from dominating.
    """
    dp_sum = ds_sum = 0.0
    n = 0
    for a, b in zip(samples, samples[1:]):
        dp = b["p"] - a["p"]
        ds = b["s"] - a["s"]
        if dp <= 0 or ds < 0:
            continue          # no burn, or a counter reset in between
        dp_sum += dp
        ds_sum += ds
        n += 1
    if ds_sum < 5:
        return None, n
    return dp_sum / ds_sum, n


DEFAULT_RATE = 2.0    # WU per hour; 11-03 log shows about 2.2 (a window in ~25 min)
MIN_RATE = 0.05
MAX_RATE = 6.0


def estimate_rate(samples):
    """Estimate how fast Codex burns quota while working, in WU per hour.

    Sums every step where the 5-hour counter went up. Idle time inside such a
    step can only pull the estimate down, so this errs on the slow side.
    """
    dp_sum = dt_sum = 0.0
    n = 0
    for a, b in zip(samples, samples[1:]):
        dp = b["p"] - a["p"]
        if dp <= 0:
            continue          # window reset or idle, not a burn measurement
        dp_sum += dp / 100.0
        dt_sum += (b["ts"] - a["ts"]) / 3600.0
        n += 1
    if dt_sum < 0.25:
        return DEFAULT_RATE, n
    return min(max(dp_sum / dt_sum, MIN_RATE), MAX_RATE), n


# ---------------------------------------------------------------------------
# model
# ---------------------------------------------------------------------------

def _eligible(h, w):
    return h < (1 - RESET_ELIGIBILITY) or w < (1 - RESET_ELIGIBILITY)


def _redeem(h, w, e5, e7):
    # A reset refills covered windows that are not already full and restarts
    # their timers. It never touches a window that is already at 100%.
    if h < 1.0:
        h, e5 = 1.0, None
    if w < 1.0:
        w = 1.0
        if REDEEM_RESTARTS_WEEKLY:
            e7 = None
    return h, w, e5, e7


def _log_block(log, t):
    # one entry per weekly block, not one per 5-hour tick inside it
    if log and log[-1]["kind"] == "blocked":
        return
    log.append({"t": t, "kind": "blocked",
                "label": "周额度耗尽，等待恢复", "reason": ""})


def _advance(st, stop, horizon, k, rate, can_redeem, log=None):
    """Run Codex flat out from state `st` until a decision point.

    st = (t, h, w, e5, e7). Stops when Codex is blocked and a redeem is
    possible, when t reaches `stop` (next reset expiry), or at the horizon.
    Returns (state, used, why) with why in "blocked" / "stop" / "end".
    """
    t, h, w, e5, e7 = st
    used = 0.0
    end = min(stop, horizon)
    while True:
        if e5 is not None and t >= e5:
            h, e5 = 1.0, None
        if e7 is not None and t >= e7:
            w, e7 = 1.0, None
            if log is not None:
                log.append({"t": t, "kind": "weekly",
                            "label": "周额度自然重置", "reason": ""})
        if t >= end:
            return (t, h, w, e5, e7), used, ("end" if t >= horizon else "stop")

        room = min(h, w * k)
        if room <= EPS:
            if can_redeem:
                return (t, h, w, e5, e7), used, "blocked"
            nxt = min(x for x in (e5, e7, end) if x is not None and x > t)
            if log is not None and w * k <= EPS:
                _log_block(log, t)
            t = nxt
            continue

        if e5 is None:
            e5 = t + FIVE_HOURS
        if e7 is None:
            e7 = t + WEEK
        t_next = min(t + room / rate * 3600.0, e5, e7, end)
        q = min(room, (t_next - t) / 3600.0 * rate)
        used += q
        h -= q
        w -= q / k
        t = t_next


def _wait_step(st, stop, horizon):
    """Blocked, choose not to redeem: jump to the next thing that changes."""
    t, h, w, e5, e7 = st
    nxt = min(x for x in (e5, e7, stop, horizon) if x is not None and x > t)
    return (nxt, h, w, e5, e7)


class _Search:
    def __init__(self, credits, horizon, k, rate):
        self.exp = [c["expiresAt"] for c in credits]
        self.horizon = horizon
        self.k = k
        self.rate = rate
        self.memo = {}

    def best(self, st, avail):
        """Best total WU from state st, with `avail` resets (earliest expiry
        first). Returns (value, redeems) where redeems = ((t, idx), ...)."""
        t = st[0]
        avail = tuple(i for i in avail if self.exp[i] >= t)
        key = (round(t), round(st[1], 4), round(st[2], 4),
               st[3] and round(st[3]), st[4] and round(st[4]), avail)
        hit = self.memo.get(key)
        if hit is not None:
            return hit

        stop = self.exp[avail[0]] if avail else self.horizon
        st2, used, why = _advance(st, stop, self.horizon, self.k, self.rate,
                                  bool(avail))
        if why == "end":
            result = (used, ())
        else:
            i = avail[0]
            t2, h, w, e5, e7 = st2
            options = []
            if _eligible(h, w):
                v, plan = self.best((t2,) + _redeem(h, w, e5, e7), avail[1:])
                options.append((v, ((t2, i),) + plan))
            if why == "stop":
                # reset i lapses unused
                options.append(self.best(st2, avail[1:]))
            else:
                options.append(self.best(_wait_step(st2, stop, self.horizon),
                                         avail))
            v, plan = max(options, key=lambda x: x[0])
            result = (used + v, plan)

        self.memo[key] = result
        return result


def _replay(st, avail, redeems, credits, horizon, k, rate):
    """Walk one plan again and collect timeline events."""
    exp = [c["expiresAt"] for c in credits]
    todo = dict((i, t) for t, i in redeems)
    log = []
    total = 0.0
    for i in list(avail):
        if i in todo and abs(todo[i] - st[0]) < 1.0:   # redeem right now
            t, h, w, e5, e7 = st
            st = (t,) + _redeem(h, w, e5, e7)
            avail = tuple(j for j in avail if j != i)
            log.append({"t": t, "kind": "redeem",
                        "label": "兑换 Reset " + chr(65 + i), "reason": ""})
    while True:
        avail = tuple(i for i in avail if exp[i] >= st[0])
        stop = exp[avail[0]] if avail else horizon
        st, used, why = _advance(st, stop, horizon, k, rate, bool(avail), log)
        total += used
        if why == "end":
            break
        i = avail[0]
        if i in todo and abs(todo[i] - st[0]) < 1.0:
            t, h, w, e5, e7 = st
            st = (t,) + _redeem(h, w, e5, e7)
            avail = avail[1:]
            log.append({"t": t, "kind": "redeem",
                        "label": "兑换 Reset " + chr(65 + i),
                        "reason": "额度耗尽时兑换" if why == "blocked"
                        else "到期前兑换"})
        elif why == "stop":
            avail = avail[1:]
        else:
            if st[2] * k <= EPS:
                _log_block(log, st[0])
            st = _wait_step(st, stop, horizon)
    return total, log


def _root_values(now, st, credits, horizon, k, rate):
    """Value of each action available right now."""
    order = sorted(range(len(credits)), key=lambda i: credits[i]["expiresAt"])
    order = [i for i in order if credits[i]["expiresAt"] > now]
    search = _Search(credits, horizon, k, rate)
    out = []

    # keep going without redeeming now
    t, h, w, e5, e7 = st
    if min(h, w * k) <= EPS:
        stop = credits[order[0]]["expiresAt"] if order else horizon
        v, plan = search.best(_wait_step(st, stop, horizon), tuple(order))
    else:
        v, plan = search.best(st, tuple(order))
    out.append({"action": "use", "label": "继续使用，暂不兑换",
                "value": v, "plan": plan})

    if _eligible(h, w):
        for i in order:
            rest = tuple(j for j in order if j != i)
            v, plan = search.best((t,) + _redeem(h, w, e5, e7), rest)
            out.append({"action": f"redeem:{i}",
                        "label": f"兑换 Reset {chr(65 + i)}",
                        "value": v, "plan": ((t, i),) + plan})
    return out


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------

def _state_from_quota(now, quota):
    primary = (quota.get("primary") or {}) if quota else {}
    secondary = (quota.get("secondary") or {}) if quota else {}
    if not primary or not secondary:
        return None
    pu = float(primary.get("usedPercent", 0))
    su = float(secondary.get("usedPercent", 0))
    # At 0% the API still reports resetsAt = now + window: not started yet.
    e5 = primary.get("resetsAt") if pu > 0 else None
    e7 = secondary.get("resetsAt") if su > 0 else None
    h = max(1.0 - pu / 100.0, 0.0)
    w = max(1.0 - su / 100.0, 0.0)
    return (now, h, w, e5, e7)


def build_plan(now, quota, credits, horizon_short, horizon_long, log_path,
               telemetry_path=None):
    """Return the short-horizon and long-horizon best actions.

    short: subscription is not renewed, so everything must be spent before T.
    long:  the subscription is renewed for one more month, so each reset's own
           expiry becomes a real constraint and the order starts to matter.
    """
    samples = load_samples(log_path, telemetry_path)
    k, n = estimate_k(samples)
    rate, rate_n = estimate_rate(samples)

    st = _state_from_quota(now, quota)
    if st is None:
        return {"ok": False, "reason": "拿不到配额数据"}

    if k is None:
        return {
            "ok": False,
            "reason": "样本不足，无法估计周额度换算系数",
            "samples": len(samples),
        }

    short = _plan_one(now, st, credits, k, horizon_short, rate)
    long_ = _plan_one(now, st, credits, k, horizon_long, rate)

    return {
        "ok": True,
        "k": round(k, 2),
        "kSamples": n,
        "rate": round(rate, 2),
        "rateSamples": rate_n,
        "samples": len(samples),
        "weeklyLeftWU": round(st[2] * k, 2),
        "windowLeftWU": round(st[1], 2),
        "short": short,
        "long": long_,
        "resetsEnabled": False,
    }


def _best_action(now, st, credits, k, horizon, rate):
    ranked = _root_values(now, st, credits, horizon, k, rate)
    return max(ranked, key=lambda x: x["value"])["action"]


def _plan_one(now, st, credits, k, horizon, rate):
    ranked = _root_values(now, st, credits, horizon, k, rate)
    ranked.sort(key=lambda x: -x["value"])

    grouped = []
    for item in ranked:
        if grouped and abs(item["value"] - grouped[-1]["value"]) < 0.01:
            grouped[-1]["ties"].append(item["label"])
        else:
            item["ties"] = [item["label"]]
            grouped.append(item)

    for item in grouped:
        if len(item["ties"]) > 1:
            if all(t.startswith("兑换 Reset") for t in item["ties"]):
                item["label"] = "兑换任一 Reset（等价）"
            else:
                item["label"] = " 或 ".join(item["ties"])

    base = grouped[0]["value"]
    for item in grouped:
        item["delta"] = item["value"] - base

    best = grouped[0]
    order = sorted((i for i, c in enumerate(credits) if c["expiresAt"] > now),
                   key=lambda i: credits[i]["expiresAt"])
    _, events = _replay(st, tuple(order), best["plan"], credits, horizon,
                        k, rate)

    # K and rate are estimates: does the advice survive if they are off?
    flips = []
    for name, kk, rr in (("K 低 15%", k * 0.85, rate),
                         ("K 高 15%", k * 1.15, rate),
                         ("速度减半", k, rate * 0.5),
                         ("速度翻倍", k, rate * 2.0)):
        other = _best_action(now, st, credits, kk, horizon, rr)
        if other.split(":")[0] != best["action"].split(":")[0]:
            flips.append(name)
    robust = "；".join(flips) + " 时结论会变" if flips else ""

    timeline = list(events)
    if best["action"].startswith("redeem"):
        # the redemption already sits at "now"; mark it instead of duplicating
        for event in timeline:
            if event["kind"] == "redeem":
                event["reason"] = robust or "现在执行"
                event["kind"] = "now"
                break
    else:
        timeline.insert(0, {"t": now, "kind": "now",
                            "label": best["label"], "reason": robust or "现在"})

    # warn about resets that expire unused inside this horizon
    used_idx = {i for _, i in best["plan"]}
    for i, credit in enumerate(credits):
        if i in used_idx:
            continue
        exp = credit["expiresAt"]
        if now < exp < horizon:
            timeline.append({
                "t": exp, "kind": "expire",
                "label": "Reset " + chr(65 + i) + " 到期（未使用）", "reason": "",
            })

    timeline.append({"t": horizon, "kind": "end", "label": "订阅结束", "reason": ""})
    timeline.sort(key=lambda e: e["t"])

    # keep it readable: first few points, a gap marker, then the ending
    if len(timeline) > 10:
        hidden = len(timeline) - 8
        gap = {
            "t": timeline[6]["t"],
            "kind": "gap",
            "label": "… 省略 %d 个节点" % hidden,
            "reason": "",
        }
        timeline = timeline[:6] + [gap] + timeline[-2:]

    redeems = [{"credit": i, "t": t} for t, i in best["plan"]]
    lapses = [i for i, c in enumerate(credits)
              if i not in used_idx and now < c["expiresAt"] < horizon]
    for item in grouped:
        item.pop("plan", None)

    return {
        "horizonHours": round(max(horizon - now, 0) / 3600, 1),
        "best": best,
        "alternatives": grouped[1:],
        "allTied": len(grouped) == 1,
        "timeline": timeline,
        "redeems": redeems,      # full plan; the timeline above is trimmed
        "lapses": lapses,        # credits that expire unused before the horizon
    }
