#!/usr/bin/env python3
"""Local web console for the Codex auto-resume watchdog.

Read-only for banked resets: this console never redeems them.
"""

import ctypes
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from codex_auto_resume import (  # noqa: E402
    AppServerClient,
    available_credits,
    consume_reset_credit,
    BASE_DIR,
    LOG_FILE,
    PID_FILE,
    STATE_FILE,
    STOP_FILE,
    find_codex,
)

import logging  # noqa: E402

from planner import build_plan  # noqa: E402
import codex_queue as CQ  # noqa: E402
import settings as S  # noqa: E402

# Importing the daemon module installs its file handler; the console must not
# write into the daemon's log.
LOGGER_NAME = "codex-auto-resume"
logging.getLogger(LOGGER_NAME).handlers = [
    handler
    for handler in logging.getLogger(LOGGER_NAME).handlers
    if not isinstance(handler, logging.FileHandler)
]

MAX_UPLOAD = 25 * 1024 * 1024


def queue_status():
    """工单队列：各箱子的工单、允许的工作目录、最近用过的目录。"""
    qdir = S.queue_dir()
    try:
        CQ.ensure_queue(qdir)
    except Exception as exc:
        return {"dir": str(qdir), "error": f"队列目录不可用：{exc}"}
    t = CQ.list_tickets(qdir)
    try:
        roots = CQ.load_config(qdir).get("allowed_roots", [])
    except Exception:
        roots = []
    recent = []
    for r in t["running"] + t["inbox"] + t["done"] + t["failed"]:
        if r["cwd"] and r["cwd"] not in recent:
            recent.append(r["cwd"])
    return {
        "dir": str(qdir),
        "running": t["running"][0]["name"] if t["running"] else None,
        "tickets": t,
        "inbox": [r["name"] for r in t["inbox"]],
        "done": t["doneCount"],
        "failed": [r["name"] for r in t["failed"][:5]],
        "failedCount": t["failedCount"],
        "paused": (qdir / "PAUSE").exists(),
        "roots": roots,
        "recentCwd": recent[:8],
    }
QUOTA_TTL = 120
TELEMETRY = BASE_DIR / "telemetry.jsonl"
CONTROL_FILE = BASE_DIR / "control.json"

DEFAULT_CONTROL = {
    "maxResumes": None,     # None=跟随守护进程启动参数，0=无限，n=上限
    "autoRedeem": False,    # 自动兑换：额度耗尽、离自动重置还早时兑换最早到期的 Reset
    "resetCount": False,    # 一次性标记：下一轮把续跑计数清零
}


def load_control():
    data = read_json(CONTROL_FILE, {}) or {}
    merged = dict(DEFAULT_CONTROL)
    merged.update({k: v for k, v in data.items() if k in DEFAULT_CONTROL})
    return merged


def save_control(control):
    tmp = CONTROL_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(control, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, CONTROL_FILE)



# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------

def pid_alive(pid):
    if not pid:
        return False
    if os.name == "nt":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
        )
        if not handle:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return code.value == 259
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def tail(path, lines=25):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            raw = fh.readlines()[-lines:]
    except Exception:
        return ""
    out = []
    for line in raw:
        line = line.rstrip("\n")
        if len(line) > 200:
            line = line[:197] + "..."
        out.append(line)
    return "\n".join(out)


class QuotaCache:
    """Fetch rate limits through app-server, at most once per QUOTA_TTL."""

    def __init__(self):
        self.lock = threading.Lock()
        self.data = None
        self.fetched_at = 0.0
        self.error = None
        self.codex = None
        try:
            self.codex = find_codex()
        except Exception as exc:
            self.error = str(exc)

    def get(self, max_age=QUOTA_TTL):
        with self.lock:
            if time.time() - self.fetched_at < max_age and self.data is not None:
                return self.data, self.fetched_at, self.error
            if not self.codex:
                return None, 0.0, self.error
            client = AppServerClient(self.codex)
            try:
                client.start()
                response = client.request("account/rateLimits/read", timeout=30)
                if "error" in response:
                    raise RuntimeError(response["error"])
                self.data = response.get("result", {})
                self.fetched_at = time.time()
                self.error = None
            except Exception as exc:
                self.error = str(exc)
            finally:
                client.close()
            return self.data, self.fetched_at, self.error


CACHE = QuotaCache()


def redeem_preview(credit_id):
    """Validate a redemption without consuming anything."""
    quota, fetched_at, err = CACHE.get()
    if not quota:
        return {"ok": False, "reason": "配额读取失败：%s" % (err or "unknown")}

    credits = available_credits(quota)
    match = None
    for item in credits:
        if item.get("id") == credit_id:
            match = item
            break
    if match is None:
        return {"ok": False, "reason": "找不到这个 Reset（可能已经用掉或过期）"}

    limits = quota.get("rateLimits") or {}
    primary = limits.get("primary") or {}
    secondary = limits.get("secondary") or {}
    p5_left = 100.0 - float(primary.get("usedPercent", 0) or 0)
    wk_left = 100.0 - float(secondary.get("usedPercent", 0) or 0)

    expires = match.get("expiresAt") or 0
    expired = expires <= time.time()
    # 官方规则：至少一项覆盖窗口剩余低于 80% 才能兑换
    eligible = (p5_left < 80.0) or (wk_left < 80.0)

    return {
        "ok": True,
        "creditId": credit_id,
        "expiresAt": expires,
        "expired": expired,
        "eligible": eligible and not expired,
        "wouldReset": {
            "fiveHourLeft": round(p5_left, 1),
            "weeklyLeft": round(wk_left, 1),
        },
        "consumed": False,
    }



def record_sample(quota):
    """Append one usage sample so the burn-rate estimate improves over time."""
    try:
        limits = (quota or {}).get("rateLimits") or {}
        primary = limits.get("primary") or {}
        secondary = limits.get("secondary") or {}
        pu = primary.get("usedPercent")
        su = secondary.get("usedPercent")
        if pu is None or su is None:
            return
        entry = {"ts": time.time(), "p": float(pu), "s": float(su)}
        last = None
        if TELEMETRY.exists():
            with open(TELEMETRY, "r", encoding="utf-8") as fh:
                lines = fh.readlines()[-1:]
            if lines:
                try:
                    last = json.loads(lines[0])
                except Exception:
                    last = None
        if last and (entry["p"] == last.get("p") and entry["s"] == last.get("s")):
            return
        if last and entry["ts"] - float(last.get("ts", 0)) < 60:
            return
        with open(TELEMETRY, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def build_status():
    pid = None
    try:
        pid = int(PID_FILE.read_text(encoding="ascii").strip())
    except Exception:
        pid = None

    state = read_json(STATE_FILE, {}) or {}
    quota, fetched_at, quota_error = CACHE.get()

    now = time.time()
    credits = [
        {
            "id": c.get("id"), "status": c.get("status"), "title": c.get("title"),
            "expiresAt": c["expiresAt"], "noExpiry": c["noExpiry"],
            "secondsLeft": int(c["expiresAt"] - now),
        }
        for c in available_credits(quota, now)
    ] if quota else []

    settings = S.load_settings()
    horizon = S.subscription_end(settings, now)

    plan = None
    if quota:
        try:
            record_sample(quota)
            plan = build_plan(
                time.time(),
                (quota.get("rateLimits") or {}),
                credits,
                horizon,
                horizon + float(settings.get("renewalDays") or 30) * 86400,
                LOG_FILE,
                TELEMETRY,
            )
        except Exception as exc:
            plan = {"ok": False, "reason": str(exc)}

    return {
        "now": time.time(),
        "daemon": {
            "pid": pid,
            "running": pid_alive(pid),
            "stopRequested": STOP_FILE.exists(),
        },
        "state": state,
        "quota": quota,
        "quotaAge": int(time.time() - fetched_at) if fetched_at else None,
        "quotaError": quota_error,
        "credits": credits,
        "plan": plan,
        "horizon": horizon,
        "control": load_control(),
        "settings": settings,
        "renewalDays": settings.get("renewalDays") or 30,
        "log": tail(LOG_FILE),
        "queue": queue_status(),
    }


# ----------------------------------------------------------------------------
# page
# ----------------------------------------------------------------------------

PAGE_FILE = BASE_DIR / "console.html"     # three switchable skins
FONT_DIR = BASE_DIR / "fonts"


# ----------------------------------------------------------------------------
# server
# ----------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        if self.path in ("/", "/index.html") or self.path.startswith("/?"):
            # read on every request so page edits show up without a restart
            self._send(200, PAGE_FILE.read_text(encoding="utf-8"), "text/html")
        elif self.path.startswith("/fonts/"):
            name = Path(self.path.split("?")[0]).name
            font = FONT_DIR / name
            if name.endswith(".woff2") and font.is_file():
                data = font.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "font/woff2")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "max-age=86400")
                self.end_headers()
                self.wfile.write(data)
            else:
                self._send(404, "not found", "text/plain")
        elif self.path.startswith("/api/status"):
            self._send(200, json.dumps(build_status(), ensure_ascii=False), "application/json")
        elif self.path.startswith("/api/ticket?"):
            from urllib.parse import parse_qs, urlsplit
            q = parse_qs(urlsplit(self.path).query)
            try:
                data = CQ.read_ticket(S.queue_dir(), q.get("box", [""])[0], q.get("name", [""])[0])
                self._send(200, json.dumps({"ok": True, **data}, ensure_ascii=False), "application/json")
            except Exception as exc:
                self._send(400, json.dumps({"ok": False, "reason": str(exc)}, ensure_ascii=False), "application/json")
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self):
        if self.path.startswith("/api/stop"):
            STOP_FILE.write_text("stop\n", encoding="utf-8")
            self._send(200, json.dumps({"ok": True, "stopRequested": True}), "application/json")
        elif self.path.startswith("/api/refresh"):
            CACHE.get(max_age=0)
            self._send(200, json.dumps({"ok": True}), "application/json")
        elif self.path.startswith("/api/control"):
            self._handle_control()
        elif self.path.startswith("/api/redeem"):
            self._handle_redeem()
        elif self.path.startswith("/api/settings"):
            self._handle_settings()
        elif self.path.startswith("/api/pick-folder"):
            self._handle_pick_folder()
        elif self.path.startswith("/api/tickets/"):
            self._handle_tickets(self.path.split("?")[0].rsplit("/", 1)[-1])
        else:
            self._send(404, "not found", "text/plain")

    # -- control ---------------------------------------------------------

    def _handle_control(self):
        body = self._json_body()
        control = load_control()

        if "maxResumes" in body:
            value = body["maxResumes"]
            if value is None:
                control["maxResumes"] = None
            else:
                try:
                    value = int(value)
                except Exception:
                    self._send(400, json.dumps({"ok": False, "reason": "maxResumes 必须是整数"}), "application/json")
                    return
                control["maxResumes"] = None if value < 0 else value

        if "autoRedeem" in body:
            control["autoRedeem"] = bool(body["autoRedeem"])

        if body.get("resetCount"):
            control["resetCount"] = True

        if "queuePause" in body and (S.queue_dir() / "inbox").is_dir():
            pause = S.queue_dir() / "PAUSE"
            if body["queuePause"]:
                pause.write_text("pause\n", encoding="utf-8")
            else:
                pause.unlink(missing_ok=True)

        save_control(control)
        self._send(200, json.dumps({"ok": True, "control": control}, ensure_ascii=False), "application/json")

    # -- tickets ---------------------------------------------------------

    def _handle_tickets(self, action):
        import base64
        if int(self.headers.get("Content-Length") or 0) > MAX_UPLOAD:
            self._send(413, json.dumps({"ok": False, "reason": "附件太大（上限 25 MB）"}, ensure_ascii=False), "application/json")
            return
        body = self._json_body()
        try:
            if action == "submit":
                files = [(f.get("name", ""), base64.b64decode(f.get("b64", ""))) for f in body.get("files") or []]
                name = CQ.submit_ticket(S.queue_dir(), body.get("title", ""), body.get("cwd", ""), body.get("sandbox", "workspace-write"),
                                        body.get("maxResumes"), body.get("body", ""), files)
                result = {"ok": True, "name": name}
            elif action == "withdraw":
                CQ.withdraw_ticket(S.queue_dir(), body.get("name", ""))
                result = {"ok": True}
            elif action == "retry":
                CQ.retry_ticket(S.queue_dir(), body.get("name", ""))
                result = {"ok": True}
            else:
                result = {"ok": False, "reason": "未知操作"}
        except Exception as exc:
            result = {"ok": False, "reason": str(exc)}
        self._send(200 if result["ok"] else 400, json.dumps(result, ensure_ascii=False), "application/json")

    # -- redeem ----------------------------------------------------------

    def _handle_redeem(self):
        """不带 confirm 时只返回预览；带 confirm="REDEEM" 且满足条件才真的兑换。"""
        body = self._json_body()
        credit_id = body.get("creditId") or ""
        preview = redeem_preview(credit_id)
        if not preview.get("ok") or body.get("confirm") != "REDEEM":
            preview["dryRun"] = True
            self._send(200, json.dumps(preview, ensure_ascii=False), "application/json")
            return
        if not preview["eligible"]:
            self._send(200, json.dumps({**preview, "ok": False, "reason": "两个窗口剩余都不低于 80%，现在兑换不了"},
                                       ensure_ascii=False), "application/json")
            return
        result = consume_reset_credit(CACHE.codex, credit_id)
        CACHE.get(max_age=0)
        self._send(200, json.dumps({"creditId": credit_id, "dryRun": False, **result}, ensure_ascii=False), "application/json")

    # -- settings --------------------------------------------------------

    def _handle_pick_folder(self):
        """在本机弹出系统的「选择文件夹」对话框（控制台只监听 127.0.0.1，弹窗出现在跑控制台的这台电脑上）。"""
        body = self._json_body()
        start = str(body.get("initial") or "")
        code = (
            "import sys, tkinter as tk\n"
            "from tkinter import filedialog\n"
            "r = tk.Tk(); r.withdraw(); r.attributes('-topmost', True)\n"
            "p = filedialog.askdirectory(parent=r, initialdir=sys.argv[1] or None, title=sys.argv[2], mustexist=False)\n"
            "sys.stdout.write(p or '')\n"
        )
        try:
            out = subprocess.run(
                [sys.executable, "-c", code, start, str(body.get("title") or "选择文件夹")],
                capture_output=True, text=True, encoding="utf-8", timeout=600,
            )
        except Exception as exc:
            self._send(200, json.dumps({"ok": False, "reason": f"弹不出选择框：{exc}"}, ensure_ascii=False), "application/json")
            return
        if out.returncode != 0:
            self._send(200, json.dumps({"ok": False, "reason": "弹不出选择框：" + (out.stderr.strip().splitlines() or ["未知错误"])[-1]},
                                       ensure_ascii=False), "application/json")
            return
        path = out.stdout.strip()
        if os.name == "nt":
            path = path.replace("/", "\\")
        self._send(200, json.dumps({"ok": True, "path": path, "cancelled": not path}, ensure_ascii=False), "application/json")

    def _handle_settings(self):
        body = self._json_body()
        updates = {}
        if "allowedRoots" in body:
            roots = [str(r) for r in body.get("allowedRoots") or []]
            bad = [r for r in roots if not Path(r).is_dir()]
            if bad:
                self._send(400, json.dumps({"ok": False, "reason": "这些目录不存在：" + "、".join(bad)}, ensure_ascii=False),
                           "application/json")
                return
            CQ.save_allowed_roots(CQ.ensure_queue(S.queue_dir()), roots)
        if "queueDir" in body:
            raw = str(body["queueDir"] or "").strip()
            if not raw:
                self._send(400, json.dumps({"ok": False, "reason": "队列目录不能为空"}, ensure_ascii=False), "application/json")
                return
            updates["queueDir"] = raw
            target = S.queue_dir({"queueDir": raw})
            try:
                for sub in CQ.BOXES:
                    (target / sub).mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                self._send(400, json.dumps({"ok": False, "reason": f"建不了这个目录：{exc}"}, ensure_ascii=False), "application/json")
                return
        saved = S.save_settings(updates)
        self._send(200, json.dumps({"ok": True, "settings": saved, "queueDir": str(S.queue_dir(saved))}, ensure_ascii=False),
                   "application/json")


def main():
    port = int(S.load_settings().get("port") or 8770)
    if "--print-port" in sys.argv:   # open-console.cmd 用来读端口
        print(port)
        return
    if len(sys.argv) > 1:
        port = int(sys.argv[1])

    if os.name == "nt":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    CACHE.get(max_age=0)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"console: http://127.0.0.1:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
