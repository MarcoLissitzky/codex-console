#!/usr/bin/env python3
# Codex Auto Resume Watchdog

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# ============================================================
# CONFIG
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

# Log, state, pid and switches live here, apart from the code.
RUNTIME_DIR = BASE_DIR / "runtime"
RUNTIME_DIR.mkdir(exist_ok=True)

STATE_FILE = RUNTIME_DIR / "codex_auto_resume_state.json"
LOG_FILE = RUNTIME_DIR / "codex_auto_resume.log"

RESET_SAFETY_BUFFER = 20

# app-server rate-limit query failures
RATE_LIMIT_INITIAL_BACKOFF = 15
RATE_LIMIT_MAX_BACKOFF = 300

# active writer
WRITER_INITIAL_BACKOFF = 10
WRITER_MAX_BACKOFF = 300

# Network and transient errors
TRANSIENT_INITIAL_BACKOFF = 15
TRANSIENT_MAX_BACKOFF = 300


CONTINUE_PROMPT = """
Continue the unfinished original task.

Requirements:
1. Continue from where the task stopped; do not restart it.
2. Preserve completed work, file changes, context, and plans.
3. Check which parts of the original task remain unfinished.
4. Continue until the original task is genuinely complete.
5. Do not start any new or unrelated task.
6. If the original task is already complete, exit immediately.
""".strip()


# Runtime guards, filled in from the command line.
MAX_WAIT_SECONDS = None
# 0 means unlimited.
MAX_RESUMES = 2
# What the process was started with, restored when the console clears the cap.
BASE_MAX_RESUMES = 2

# Create this file to make a running watchdog stop after its current step.
STOP_FILE = RUNTIME_DIR / "STOP"

# PID of the running daemon, written on startup and removed on exit.
PID_FILE = RUNTIME_DIR / "daemon.pid"

# Runtime switches written by the console.
CONTROL_FILE = RUNTIME_DIR / "control.json"

# 永不过期的 Reset（expiresAt 为 null）在排序和规划里按这么远之后到期处理
NO_EXPIRY_SECONDS = 10 * 365 * 86400


# ============================================================
# LOGGING
# ============================================================

logger = logging.getLogger("codex-auto-resume")
logger.setLevel(logging.INFO)

formatter = logging.Formatter(
    "%(asctime)s | %(levelname)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
)

if not logger.handlers:
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)


# ============================================================
# STATE
# ============================================================

DEFAULT_STATE = {
    "target_session": None,
    "target_cwd": None,
    "rollout_file": None,
    "status": "idle",
    "last_reset_at": None,
    "writer_attempt": 0,
    "rate_limit_attempt": 0,
    "transient_attempt": 0,
    "last_error": None,
    "resume_count": 0,
    "updated_at": None,
}


def load_control():
    try:
        with open(CONTROL_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh) or {}
    except Exception:
        return {}


def save_control(control):
    temp = CONTROL_FILE.with_suffix(".tmp")
    with open(temp, "w", encoding="utf-8") as fh:
        json.dump(control, fh, ensure_ascii=False, indent=2)
    os.replace(temp, CONTROL_FILE)


def apply_control(state):
    """Apply the switches the console writes. Safe to call every loop."""
    global MAX_RESUMES

    control = load_control()

    value = control.get("maxResumes", None)
    desired = BASE_MAX_RESUMES if value is None else max(0, int(value))
    if desired != MAX_RESUMES:
        logger.info("Resume cap changed: %s -> %s", MAX_RESUMES, desired)
        MAX_RESUMES = desired

    if control.get("resetCount"):
        state["resume_count"] = 0
        control["resetCount"] = False
        save_control(control)
        save_state(state)
        logger.info("Resume counter cleared from the console.")

    return control


def load_state():
    """Load persistent watchdog state, resetting completed runs."""
    if not STATE_FILE.exists():
        return DEFAULT_STATE.copy()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        state = DEFAULT_STATE.copy()
        state.update(saved)
        if state.get("status") == "completed":
            return DEFAULT_STATE.copy()
        return state
    except Exception as exc:
        logger.warning("Unable to read state file; starting with fresh state: %s", exc)
        return DEFAULT_STATE.copy()


def save_state(state):
    """Persist watchdog state atomically."""
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    temp_file = STATE_FILE.with_suffix(".tmp")
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(temp_file, STATE_FILE)


# ============================================================
# CODEX
# ============================================================


def find_codex():
    """Return the Codex CLI executable.

    Prefers the Codex Desktop bundled binary because the npm shim is often
    older than the account's default model requires.
    """
    explicit = os.environ.get("CODEX_CLI")
    if explicit and Path(explicit).exists():
        return explicit

    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if local_app_data:
        bin_root = Path(local_app_data) / "OpenAI" / "Codex" / "bin"
        if bin_root.exists():
            candidates = sorted(
                bin_root.glob("*/codex.exe"), key=lambda p: p.stat().st_mtime, reverse=True
            )
            if candidates:
                return str(candidates[0])

    for name in ("codex", "codex.cmd"):
        candidate = shutil.which(name)
        if candidate:
            return candidate

    raise FileNotFoundError("Codex CLI not found in PATH.")


# ============================================================
# SESSION
# ============================================================

UUID_RE = re.compile(
    r"\b"
    r"[0-9a-fA-F]{8}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{12}"
    r"\b"
)


def extract_session_id(text):
    """Extract the first UUID-shaped Codex session identifier from text."""
    patterns = [
        r'"thread_id"\s*:\s*"([^"]+)"',
        r'"threadId"\s*:\s*"([^"]+)"',
        r"thread\s+(" + UUID_RE.pattern + r")",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)
    match = UUID_RE.search(text)
    if match:
        return match.group(0)
    return None


def get_codex_home():
    """Return CODEX_HOME or the default ~/.codex directory."""
    custom = os.environ.get("CODEX_HOME")
    if custom:
        return Path(custom).expanduser()
    return Path.home() / ".codex"


def find_rollout_file(session_id):
    """Find the newest rollout whose filename contains a session ID."""
    codex_home = get_codex_home()
    roots = [codex_home / "sessions", codex_home / "archived_sessions"]
    matches = []
    for root in roots:
        if not root.exists():
            continue
        try:
            for path in root.rglob(f"*{session_id}*.jsonl"):
                if path.is_file():
                    matches.append(path)
        except OSError as exc:
            logger.warning("Unable to scan rollout directory %s: %s", root, exc)
    if matches:
        matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return matches[0]
    return None


def find_latest_rollout():
    """Return the newest rollout file without resuming any session."""
    candidates = []

    for root in (get_codex_home() / "sessions", get_codex_home() / "archived_sessions"):
        if not root.exists():
            continue

        try:
            candidates.extend(path for path in root.rglob("*.jsonl") if path.is_file())
        except OSError as exc:
            logger.warning("Unable to scan rollout directory %s: %s", root, exc)

    if not candidates:
        return None

    return max(candidates, key=lambda path: path.stat().st_mtime)


def read_session_metadata(rollout_path):
    """Read the session ID and original cwd from a rollout file."""
    rollout_path = Path(rollout_path)
    session_id = None
    original_cwd = None
    first_turn_cwd = None

    with rollout_path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError:
                continue

            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue

            if record.get("type") == "session_meta":
                session_id = (
                    session_id
                    or payload.get("id")
                    or payload.get("thread_id")
                    or payload.get("threadId")
                )
                cwd = payload.get("cwd")
                if original_cwd is None and isinstance(cwd, str) and cwd.strip():
                    original_cwd = cwd.strip()
            elif record.get("type") == "turn_context" and first_turn_cwd is None:
                cwd = payload.get("cwd")
                if isinstance(cwd, str) and cwd.strip():
                    first_turn_cwd = cwd.strip()

            if session_id and original_cwd:
                break

    if not session_id:
        session_id = extract_session_id(rollout_path.name)

    return session_id, original_cwd or first_turn_cwd


def read_session_cwd(rollout_path):
    """Return original session_meta.cwd, with earliest turn cwd as fallback."""
    if not rollout_path:
        return None
    rollout_path = Path(rollout_path)
    if not rollout_path.exists():
        return None
    try:
        _, cwd = read_session_metadata(rollout_path)
    except Exception as exc:
        logger.warning("Unable to read rollout metadata: %s", exc)
        return None
    return cwd


def discover_session(state):
    """Lock the newest existing session and persist its original context."""
    rollout = find_latest_rollout()
    if rollout is None:
        return None, None

    try:
        session_id, cwd = read_session_metadata(rollout)
    except OSError as exc:
        logger.warning("Unable to read latest rollout %s: %s", rollout, exc)
        return None, None

    if not session_id:
        return None, None

    state["target_session"] = session_id
    state["rollout_file"] = str(rollout)

    if cwd and Path(cwd).exists():
        state["target_cwd"] = cwd

    save_state(state)
    return session_id, state.get("target_cwd")


def recover_session_context(session_id, state):
    """Recover authoritative rollout cwd, using saved cwd only as fallback."""
    saved_cwd = state.get("target_cwd")
    rollout = find_rollout_file(session_id)
    if not rollout:
        logger.warning("No rollout file was found for session %s", session_id)
        if saved_cwd and Path(saved_cwd).exists():
            logger.warning("Using saved cwd fallback: %s", saved_cwd)
            return saved_cwd
        return None
    state["rollout_file"] = str(rollout)
    logger.info("Found rollout: %s", rollout)
    cwd = read_session_cwd(rollout)
    if not cwd:
        logger.warning("No cwd was found in rollout metadata")
        save_state(state)
        if saved_cwd and Path(saved_cwd).exists():
            logger.warning("Using saved cwd fallback: %s", saved_cwd)
            return saved_cwd
        return None
    if not Path(cwd).exists():
        logger.warning("Original cwd no longer exists: %s", cwd)
        save_state(state)
        return None
    state["target_cwd"] = cwd
    save_state(state)
    logger.info("Restored original session cwd: %s", cwd)
    return cwd


# ============================================================
# ERRORS
# ============================================================

WRITER_MARKERS = ["already has an active writer", "thread-store conflict"]

QUOTA_MARKERS = ["usage limit", "usage_limit", "rate limit", "rate_limit", "try again"]

TRANSIENT_MARKERS = [
    "connection reset",
    "connection refused",
    "timed out",
    "timeout",
    "temporarily unavailable",
    "network error",
    "failed to refresh available models",
]


def contains_marker(text, markers):
    lower = text.lower()
    return any(marker in lower for marker in markers)


def is_writer_error(text):
    return contains_marker(text, WRITER_MARKERS)


def is_quota_error(text):
    return contains_marker(text, QUOTA_MARKERS)


def is_transient_error(text):
    return contains_marker(text, TRANSIENT_MARKERS)


def exponential_backoff(initial, attempt, maximum):
    return min(initial * (2**attempt), maximum)


# ============================================================
# APP SERVER
# ============================================================


class AppServerClient:
    """Minimal JSON-RPC client for Codex app-server over stdio."""

    def __init__(self, codex_path):
        self.codex_path = codex_path
        self.process = None
        self.stdout_thread = None
        self.responses = {}
        self.notifications = []
        self.lock = threading.Lock()
        self.cv = threading.Condition(self.lock)
        self.request_id = 0

    def start(self):
        if self.process is not None:
            return

        cmd = [self.codex_path, "app-server", "--listen", "stdio://"]

        logger.info("Starting Codex app-server")

        self.process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )

        self.stdout_thread = threading.Thread(target=self._reader_loop, daemon=True)

        self.stdout_thread.start()

        # initialize
        response = self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "codex_auto_resume_watchdog",
                    "title": "Codex Auto Resume Watchdog",
                    "version": "1.0.0",
                }
            },
            timeout=30,
        )

        if "error" in response:
            raise RuntimeError(f"app-server initialize failed: {response}")

        self.notify("initialized", {})

        logger.info("Codex app-server initialized")

    def _reader_loop(self):
        assert self.process is not None
        assert self.process.stdout is not None
        for line in self.process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            with self.cv:
                if "id" in obj:
                    self.responses[obj["id"]] = obj
                    self.cv.notify_all()
                else:
                    self.notifications.append(obj)

    def _send(self, obj):
        assert self.process is not None
        assert self.process.stdin is not None
        payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        self.process.stdin.write(payload + "\n")
        self.process.stdin.flush()

    def request(self, method, params=None, timeout=30):
        with self.cv:
            self.request_id += 1
            request_id = self.request_id
        obj = {"method": method, "id": request_id}
        if params is not None:
            obj["params"] = params
        self._send(obj)
        deadline = time.time() + timeout
        with self.cv:
            while True:
                if request_id in self.responses:
                    return self.responses.pop(request_id)
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise TimeoutError(f"app-server timeout: {method}")
                self.cv.wait(timeout=remaining)

    def notify(self, method, params=None):
        obj = {"method": method}
        if params is not None:
            obj["params"] = params
        self._send(obj)

    def read_rate_limits(self):
        response = self.request("account/rateLimits/read", timeout=30)
        if "error" in response:
            raise RuntimeError(response["error"])
        result = response.get("result", {})
        return result.get("rateLimits", {})

    def close(self):
        if self.process is None:
            return
        try:
            self.process.terminate()
            self.process.wait(timeout=5)
        except Exception:
            try:
                self.process.kill()
            except Exception:
                pass
        self.process = None


# ============================================================
# RATE LIMIT
# ============================================================


def ensure_app_server(app_server):
    """Restart the app-server if it has exited."""
    process = app_server.process
    if process is not None and process.poll() is None:
        return
    logger.warning("app-server is not running; restarting it.")
    app_server.close()
    app_server.start()


def _window_full(window):
    used = window.get("usedPercent") if isinstance(window, dict) else None
    return isinstance(used, (int, float)) and used >= 100


def quota_available(rate_limits):
    """Return whether both the 5-hour (primary) and weekly (secondary) windows are available."""
    if rate_limits.get("rateLimitReachedType"):
        return False
    return not any(_window_full(rate_limits.get(k)) for k in ("primary", "secondary"))


def get_blocking_reset_at(rate_limits):
    """Return (resetsAt, window name) of the window that blocks us.

    Full windows win, and with both full the later reset counts. With none at
    100% (blocked only by rateLimitReachedType) the fuller window is taken.
    """
    windows = [
        (name, rate_limits.get(name))
        for name in ("primary", "secondary")
        if isinstance(rate_limits.get(name), dict)
        and isinstance(rate_limits[name].get("resetsAt"), (int, float))
    ]
    full = [(w["resetsAt"], name) for name, w in windows if _window_full(w)]
    if full:
        reset_at, name = max(full)
        return int(reset_at), name
    if windows:
        name, w = max(windows, key=lambda nw: nw[1].get("usedPercent") or 0)
        return int(w["resetsAt"]), name
    return None, None


def sleep_until(timestamp):
    """Sleep until a reset timestamp plus the configured safety buffer."""
    target = timestamp + RESET_SAFETY_BUFFER

    dt = datetime.fromtimestamp(target).astimezone()

    logger.info("Planned resume time: %s", dt.strftime("%Y-%m-%d %H:%M:%S %Z"))

    if MAX_WAIT_SECONDS is not None:
        remaining = target - time.time()
        if remaining > MAX_WAIT_SECONDS:
            raise SystemExit(
                "Required wait of %.0f seconds exceeds --max-wait %s; "
                "stopping without resuming." % (remaining, MAX_WAIT_SECONDS)
            )

    last_log_at = 0.0
    last_category = None
    auto_was_on = bool(load_control().get("autoRedeem"))

    while True:
        remaining = target - time.time()

        if remaining <= 0:
            return

        if STOP_FILE.exists():
            elapsed = (target - time.time()) / 3600

            logger.warning(
                "STOP file found while waiting (%.2f hours still to go); exiting.", elapsed
            )

            raise SystemExit("Stopped by STOP file before the reset.")

        if remaining > 3600:
            message = "Quota reset is %.2f hours away" % (remaining / 3600)
            category = "h"
        elif remaining > 60:
            message = "Quota reset is %.1f minutes away" % (remaining / 60)
            category = "m"
        else:
            message = "Quota reset is %.0f seconds away" % remaining
            category = "s"

        now_ts = time.time()
        if category != last_category or now_ts - last_log_at >= 600:
            logger.info(message)
            last_log_at = now_ts
            last_category = category

        # 睡眠中在控制台打开了自动兑换：提前醒来重新判断
        if not auto_was_on and load_control().get("autoRedeem"):
            logger.info("auto-redeem switched on; re-checking quota now.")
            return

        # Keep the sleep short so a STOP file is noticed quickly.
        time.sleep(min(remaining, 30))


def available_credits(quota_result, now=None):
    """account/rateLimits/read 的结果 → 可用的 Reset，按到期时间排序。

    expiresAt 为 null 表示永不过期（noExpiry=True，expiresAt 换成很远的将来）。
    """
    now = now or time.time()
    raw = ((quota_result or {}).get("rateLimitResetCredits") or {}).get("credits") or []
    out = []
    for c in raw:
        if c.get("status") not in (None, "available"):
            continue
        exp = c.get("expiresAt")
        no_exp = exp is None
        exp = now + NO_EXPIRY_SECONDS if no_exp else exp
        if exp <= now:
            continue
        out.append({**c, "expiresAt": exp, "noExpiry": no_exp})
    out.sort(key=lambda c: c["expiresAt"])
    return out


REDEEM_OUTCOME_ZH = {
    "reset": "已兑换，额度窗口已重置",
    "nothingToReset": "没有需要重置的窗口（剩余都不低于 80%），Reset 没有被消耗",
    "noCredit": "账户里没有可用的 Reset",
    "alreadyRedeemed": "这次请求之前已经兑换成功",
}


def consume_reset_credit(codex_path, credit_id=None, idempotency_key=None):
    """真的兑换一个 Reset。credit_id 为空时由服务端挑下一个可用的。"""
    import uuid

    params = {"idempotencyKey": idempotency_key or str(uuid.uuid4())}
    if credit_id:
        params["creditId"] = credit_id
    client = AppServerClient(codex_path)
    try:
        client.start()
        response = client.request("account/rateLimitResetCredit/consume", params, timeout=60)
    except Exception as exc:
        return {"ok": False, "outcome": None, "reason": f"调用失败：{exc}"}
    finally:
        client.close()
    if "error" in response:
        err = response["error"]
        return {
            "ok": False,
            "outcome": None,
            "reason": f"接口返回错误：{err.get('message', err) if isinstance(err, dict) else err}",
        }
    outcome = (response.get("result") or {}).get("outcome")
    return {
        "ok": outcome in ("reset", "alreadyRedeemed"),
        "outcome": outcome,
        "reason": REDEEM_OUTCOME_ZH.get(outcome, f"未知结果：{outcome}"),
    }


def maybe_auto_redeem(app_server, state, reset_at=None):
    """控制台打开「自动兑换」时，在额度耗尽后兑换最早到期的 Reset。

    离五小时窗口自动重置不到 autoRedeemMinWaitMinutes 分钟时不兑换，直接等。
    """
    control = load_control()
    if not control.get("autoRedeem"):
        return False

    from settings import load_settings

    min_wait = float(load_settings().get("autoRedeemMinWaitMinutes") or 0) * 60
    if reset_at and reset_at - time.time() < min_wait:
        logger.info(
            "auto-redeem: 离自动重置只剩 %.0f 分钟，不动用 Reset。",
            max(reset_at - time.time(), 0) / 60,
        )
        return False

    try:
        client = AppServerClient(app_server.codex_path)
        client.start()
        try:
            response = client.request("account/rateLimits/read", timeout=30)
        finally:
            client.close()
        live = available_credits(response.get("result", {}))
    except Exception as exc:
        logger.warning("auto-redeem: 读取 Reset 失败：%s", exc)
        return False

    if not live:
        logger.info("auto-redeem: 没有可用的 Reset。")
        return False

    target = live[0]
    logger.warning("auto-redeem: 兑换 %s（%s）", target.get("id"), target.get("title") or "Reset")
    result = consume_reset_credit(app_server.codex_path, target.get("id"))
    logger.warning("auto-redeem: %s", result["reason"])
    if result["outcome"] == "reset":
        state["last_redeem_at"] = time.time()
        save_state(state)
        return True
    return False


def wait_for_quota(app_server, state):
    """Wait for quota using app-server without mutating the target thread."""
    attempt = 0
    redeemed = False
    while True:
        try:
            rate_limits = app_server.read_rate_limits()
            attempt = 0
            logger.info("rate limits: %s", json.dumps(rate_limits, ensure_ascii=False))
            if quota_available(rate_limits):
                logger.info("Quota is available.")
                state["last_reset_at"] = None
                state["rate_limit_attempt"] = 0
                save_state(state)
                return
            reset_at, window = get_blocking_reset_at(rate_limits)
            state["status"] = "waiting_for_quota"
            state["last_reset_at"] = reset_at
            save_state(state)
            if reset_at:
                reset_dt = datetime.fromtimestamp(reset_at).astimezone()
                logger.warning("Quota is exhausted.")
                logger.info("%s.resetsAt: %s", window, reset_dt.strftime("%Y-%m-%d %H:%M:%S %Z"))

                # 兑换过一次还卡着（多半是周额度没被 Reset 解开）：不再兑换，免得把 Reset 连着用光
                if redeemed:
                    logger.warning(
                        "auto-redeem: 兑换后仍卡在 %s 窗口，本轮不再兑换，等它自己重置。", window
                    )
                elif maybe_auto_redeem(app_server, state, reset_at):
                    redeemed = True
                    time.sleep(30)  # 等兑换生效再重读
                    continue
                sleep_until(reset_at)

                # Re-read rate limits after reset propagation; do not resume yet.
                continue
            delay = exponential_backoff(RATE_LIMIT_INITIAL_BACKOFF, attempt, RATE_LIMIT_MAX_BACKOFF)
            attempt += 1
            logger.warning(
                "Quota is unavailable without resetsAt; checking again in %s seconds.", delay
            )
            time.sleep(delay)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            delay = exponential_backoff(RATE_LIMIT_INITIAL_BACKOFF, attempt, RATE_LIMIT_MAX_BACKOFF)
            attempt += 1
            logger.warning("Unable to read rate limits: %s", exc)
            try:
                ensure_app_server(app_server)
            except Exception as restart_exc:
                logger.warning("Unable to restart app-server: %s", restart_exc)
            logger.warning("Retrying app-server in %s seconds.", delay)
            time.sleep(delay)


# ============================================================
# RESUME
# ============================================================


def run_resume(codex_path, target_session, target_cwd):
    """Resume exactly one locked session and return its process output."""
    if not target_session:
        raise ValueError("A target session is required.")
    cmd = [codex_path, "exec", "--json", "--skip-git-repo-check", "--sandbox", "workspace-write"]
    if target_cwd:
        cmd.extend(["-C", target_cwd])
    cmd.append("resume")
    cmd.append(target_session)
    cmd.append(CONTINUE_PROMPT)
    logger.info("Executing resume")
    logger.info("Session: %s", target_session)
    if target_cwd:
        logger.info("CWD: %s", target_cwd)
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    output = []
    assert process.stdout is not None
    for line in process.stdout:
        print(line, end="", flush=True)
        output.append(line)
    return_code = process.wait()
    return (return_code, "".join(output))


# ============================================================
# MAIN
# ============================================================


def parse_args():
    parser = argparse.ArgumentParser(
        description="Wait for the Codex usage limit to reset, then resume one session."
    )
    parser.add_argument(
        "--session", help="Session/thread id to lock. Defaults to the newest rollout on disk."
    )
    parser.add_argument(
        "--codex", help="Path to codex executable. Defaults to the newest Codex Desktop build."
    )
    parser.add_argument(
        "--max-resumes",
        type=int,
        default=2,
        help="Stop after this many resume attempts. 0 means unlimited (default: 2).",
    )
    parser.add_argument(
        "--max-wait",
        type=int,
        default=None,
        help="Abort instead of sleeping longer than this many seconds.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Print the quota state and planned resume time, then exit.",
    )
    parser.add_argument(
        "--reset-state",
        action="store_true",
        help="Ignore any saved state file and lock the session again.",
    )
    return parser.parse_args()


def main():
    """Run the persistent quota-wait and same-session resume state machine."""
    global MAX_WAIT_SECONDS, MAX_RESUMES, BASE_MAX_RESUMES
    args = parse_args()
    MAX_WAIT_SECONDS = args.max_wait
    MAX_RESUMES = args.max_resumes
    BASE_MAX_RESUMES = args.max_resumes
    PID_FILE.write_text(str(os.getpid()), encoding="ascii")
    import atexit

    atexit.register(lambda: PID_FILE.unlink(missing_ok=True))
    if args.codex:
        os.environ["CODEX_CLI"] = args.codex
    if args.reset_state and STATE_FILE.exists():
        STATE_FILE.unlink()
    try:
        codex_path = find_codex()
    except Exception as exc:
        logger.error("%s", exc)
        sys.exit(1)
    state = load_state()
    target_session = state.get("target_session")
    target_cwd = state.get("target_cwd")
    if args.session and args.session != target_session:
        logger.info("Overriding locked session from command line: %s", args.session)
        target_session = args.session
        target_cwd = None
        state["target_session"] = target_session
        state["target_cwd"] = None
        state["rollout_file"] = None
        state["resume_count"] = 0
        save_state(state)
    logger.info("Codex Auto Resume Watchdog started")
    logger.info("Codex CLI: %s", codex_path)
    if target_session:
        logger.info("Restored locked session from state: %s", target_session)
        target_cwd = recover_session_context(target_session, state)
    else:
        target_session, target_cwd = discover_session(state)
        if not target_session:
            logger.error("No existing Codex session rollout was found.")
            sys.exit(1)
        logger.info("Locked latest existing session: %s", target_session)
        target_cwd = recover_session_context(target_session, state)
    if not target_cwd:
        logger.error("Unable to recover an existing working directory for the session.")
        sys.exit(1)

    # --------------------------------------------------------
    # Start app-server only after the session and original cwd are locked.
    # --------------------------------------------------------
    app_server = AppServerClient(codex_path)
    try:
        app_server.start()
    except Exception as exc:
        logger.error("Unable to start app-server: %s", exc)
        sys.exit(1)
    if args.check:
        try:
            rate_limits = app_server.read_rate_limits()
            primary = rate_limits.get("primary") or {}
            secondary = rate_limits.get("secondary") or {}
            reset_at, window = get_blocking_reset_at(rate_limits)
            logger.info("Session : %s", target_session)
            logger.info("CWD     : %s", target_cwd)
            logger.info("Codex   : %s", codex_path)
            logger.info("planType: %s", rate_limits.get("planType"))
            logger.info(
                "primary : %s%% used, window %s min",
                primary.get("usedPercent"),
                primary.get("windowDurationMins"),
            )
            logger.info(
                "weekly  : %s%% used, window %s min",
                secondary.get("usedPercent"),
                secondary.get("windowDurationMins"),
            )
            if reset_at:
                reset_dt = datetime.fromtimestamp(reset_at).astimezone()
                logger.info("resetsAt: %s (%s)", reset_dt.strftime("%Y-%m-%d %H:%M:%S %Z"), window)
                logger.info("wait    : %.2f hours", max(reset_at - time.time(), 0) / 3600)
            logger.info(
                "quota   : %s", "available" if quota_available(rate_limits) else "exhausted"
            )
        finally:
            app_server.close()
        return
    try:
        while True:
            apply_control(state)
            if STOP_FILE.exists():
                logger.warning("STOP file found at %s; exiting without resuming.", STOP_FILE)
                state["status"] = "stopped_by_user"
                save_state(state)
                return
            if MAX_RESUMES > 0 and state.get("resume_count", 0) >= MAX_RESUMES:
                logger.error(
                    "Reached --max-resumes %s; stopping without another resume.", MAX_RESUMES
                )
                state["status"] = "resume_cap_reached"
                save_state(state)
                return
            try:
                ensure_app_server(app_server)
            except Exception as exc:
                logger.warning("Unable to ensure app-server is running: %s", exc)

            # =================================================
            # 1. Check quota without mutating the session.
            # =================================================
            state["status"] = "checking_quota"
            save_state(state)
            wait_for_quota(app_server, state)

            # re-read the console switches: they may have changed while sleeping
            apply_control(state)

            # =================================================
            # 2. Send one continuation only after quota is confirmed available.
            # =================================================
            state["resume_count"] = state.get("resume_count", 0) + 1
            save_state(state)
            try:
                return_code, output = run_resume(codex_path, target_session, target_cwd)
            except KeyboardInterrupt:
                logger.info("Watchdog interrupted by user")
                save_state(state)
                return

            # =================================================
            # 4. active writer
            # =================================================
            if is_writer_error(output):
                state["status"] = "waiting_for_writer"
                state["writer_attempt"] += 1
                save_state(state)
                attempt = state["writer_attempt"] - 1
                delay = exponential_backoff(WRITER_INITIAL_BACKOFF, attempt, WRITER_MAX_BACKOFF)
                logger.warning("Session still has an active writer; retrying in %s seconds.", delay)
                time.sleep(delay)
                continue
            state["writer_attempt"] = 0

            # =================================================
            # 5. If the resumed task hits quota again, return to app-server.
            # =================================================
            if is_quota_error(output):
                logger.warning("The resumed task exhausted quota again.")
                logger.info(
                    "Returning to the side-effect-free rate-limit watcher; "
                    "no duplicate continuation will be sent while quota is exhausted."
                )
                state["status"] = "waiting_for_quota"
                save_state(state)
                continue

            # =================================================
            # 6. SUCCESS
            # =================================================
            if return_code == 0:
                state["status"] = "completed"
                state["last_error"] = None
                save_state(state)
                logger.info("The original task completed.")
                logger.info("Session: %s", target_session)
                logger.info("CWD: %s", target_cwd)
                logger.info("Watchdog exiting normally.")
                return

            # =================================================
            # 7. TRANSIENT
            # =================================================
            if is_transient_error(output):
                state["status"] = "transient_error"
                state["transient_attempt"] += 1
                attempt = state["transient_attempt"] - 1
                delay = exponential_backoff(
                    TRANSIENT_INITIAL_BACKOFF, attempt, TRANSIENT_MAX_BACKOFF
                )
                state["last_error"] = output[-1000:]
                save_state(state)
                logger.warning("Transient error; retrying in %s seconds.", delay)
                time.sleep(delay)
                continue

            # =================================================
            # 8. FATAL
            # =================================================
            state["status"] = "fatal_error"
            state["last_error"] = output[-3000:]
            save_state(state)
            logger.error("Unknown error; stopping watchdog.")
            logger.error("return code = %s", return_code)
            sys.exit(return_code if return_code != 0 else 1)
    finally:
        app_server.close()


if __name__ == "__main__":
    main()
