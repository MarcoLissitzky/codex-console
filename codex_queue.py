#!/usr/bin/env python3
"""Codex 工单队列：按顺序给 Codex 派活，每张工单一个新会话，撞额度后续跑同一会话。

目录（settings.json 的 queueDir，默认是本脚本上一级的 codex-queue/；控制台里可以改）：
  inbox/<工单>/ticket.md   待派（也可以直接放 inbox/<工单>.md，会自动收进同名文件夹）
  running/<工单>/          正在跑
  done/<工单>/             完成：result.md（Codex 最后一条消息）、run.jsonl、meta.json
  failed/<工单>/           失败或续跑次数用完，meta.json 里有原因
  PAUSE                    存在时不再取新工单（当前这张会跑完）
  queue_config.json        allowed_roots：允许的工作目录（控制台里可以加减）；default_max_resumes

ticket.md 格式：
  ---
  cwd: D:\\work\\my-repo
  sandbox: workspace-write        # 或 read-only
  max_resumes: 3                  # 可省略
  images: a.png, b.png            # 可省略，相对工单文件夹
  ---
  （正文 = 发给 Codex 的任务）

运行：queue.cmd 或 python codex_queue.py [--queue 目录] [--once]
停止：在 runtime/ 里建 STOP 文件（和守护进程同一个开关）。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

import codex_auto_resume as car
import settings as S
from codex_auto_resume import (
    CONTINUE_PROMPT,
    STOP_FILE,
    AppServerClient,
    ensure_app_server,
    exponential_backoff,
    extract_session_id,
    find_codex,
    is_quota_error,
    is_transient_error,
    is_writer_error,
    load_state,
    logger,
    save_state,
    wait_for_quota,
)

BOXES = ("inbox", "running", "done", "failed")
READY_MARK = ".ready"  # 控制台在本机直接写入的工单带这个标记，不用等同步静置
ALLOWED_SANDBOXES = {"read-only", "workspace-write"}
SETTLE_SECONDS = 60  # 工单文件夹多久没变化才算同步完
POLL_SECONDS = 60
TRANSIENT_CAP = 5
ARG_LIMIT = 20000

TICKET_HEADER = """[工单 {name}]
工单文件夹：{folder}
（输入材料在这里，只读；产出写到工作目录 {cwd} 下，不要写进工单文件夹。）
完成后，最后一条消息写成交付总结：做了什么、产出文件的完整路径、没做完或有疑问的地方。

"""


# ------------------------------------------------------------
# 工单
# ------------------------------------------------------------

def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def parse_ticket(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8-sig")
    meta: dict = {}
    body = text
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                for ln in lines[1:i]:
                    ln = ln.split(" #", 1)[0]  # 行尾注释
                    if ":" in ln:
                        k, v = ln.split(":", 1)
                        meta[k.strip()] = v.strip()
                body = "\n".join(lines[i + 1 :]).strip()
                break
    return meta, body


def load_config(qdir: Path) -> dict:
    cfg_path = qdir / "queue_config.json"
    if not cfg_path.exists():
        cfg = {"allowed_roots": [], "default_max_resumes": 3}
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.warning("已生成 %s：allowed_roots 为空，先在控制台或这个文件里添加允许的工作目录。", cfg_path)
    return json.loads(cfg_path.read_text(encoding="utf-8"))


def save_allowed_roots(qdir: Path, roots: list[str]) -> list[str]:
    cfg = load_config(qdir)
    clean = []
    for r in roots:
        r = str(r).strip()
        if r and r not in clean:
            clean.append(r)
    cfg["allowed_roots"] = clean
    (qdir / "queue_config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return clean


def ensure_queue(qdir: Path) -> Path:
    for sub in BOXES:
        (qdir / sub).mkdir(parents=True, exist_ok=True)
    return qdir


def cwd_allowed(cwd: str, roots: list[str]) -> bool:
    c = os.path.normcase(os.path.abspath(cwd))
    for r in roots:
        r = os.path.normcase(os.path.abspath(r))
        if c == r or c.startswith(r.rstrip("\\/") + os.sep):
            return True
    return False


def newest_mtime(folder: Path) -> float:
    return max((p.stat().st_mtime for p in folder.rglob("*")), default=folder.stat().st_mtime)


def collect_inbox(qdir: Path) -> list[Path]:
    inbox = qdir / "inbox"
    for f in sorted(inbox.glob("*.md")):  # 单文件工单 → 同名文件夹
        if time.time() - f.stat().st_mtime < SETTLE_SECONDS:
            continue
        d = inbox / f.stem
        d.mkdir(exist_ok=True)
        shutil.move(str(f), str(d / "ticket.md"))
    ready = []
    for d in sorted(p for p in inbox.iterdir() if p.is_dir() and not p.name.startswith(".")):
        if not (d / "ticket.md").exists():
            continue
        if (d / READY_MARK).exists() or time.time() - newest_mtime(d) >= SETTLE_SECONDS:
            ready.append(d)
    return ready


def write_meta(folder: Path, meta: dict) -> None:
    (folder / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def read_meta(folder: Path) -> dict:
    try:
        return json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def finish(qdir: Path, folder: Path, meta: dict, status: str, reason: str = "") -> None:
    meta.update(status=status, reason=reason, finished_at=now_iso())
    write_meta(folder, meta)
    dest_root = qdir / ("done" if status == "done" else "failed")
    dest = dest_root / folder.name
    if dest.exists():
        dest = dest_root / f"{folder.name}_{datetime.now():%Y%m%d%H%M%S}"
    shutil.move(str(folder), str(dest))
    logger.info("工单 %s → %s %s", folder.name, dest_root.name, reason)


# ------------------------------------------------------------
# 控制台用：列出 / 查看 / 提交 / 撤回 / 重派
# ------------------------------------------------------------

NAME_BAD = set('\\/:*?"<>|')


def _items(qdir: Path, box: str) -> list[Path]:
    d = qdir / box
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir() if not p.name.startswith(".") and (p.is_dir() or p.suffix == ".md"))


def list_tickets(qdir: Path, done_limit: int = 12) -> dict:
    out = {}
    for box in BOXES:
        rows = []
        for p in _items(qdir, box):
            meta = read_meta(p) if p.is_dir() else {}
            title = p.stem if p.is_file() else p.name
            rows.append({
                "name": p.name, "title": title,
                "mtime": p.stat().st_mtime,
                "cwd": meta.get("cwd") or (parse_ticket(p / "ticket.md")[0].get("cwd") if p.is_dir() and (p / "ticket.md").exists() else ""),
                "status": meta.get("status", box), "reason": meta.get("reason", ""),
                "resumes": meta.get("resumes", 0), "finished": meta.get("finished_at"),
                "hasResult": p.is_dir() and (p / "result.md").exists(),
            })
        if box in ("done", "failed"):
            rows.sort(key=lambda r: r["mtime"], reverse=True)
            out[box + "Count"] = len(rows)
            rows = rows[:done_limit]
        out[box] = rows
    return out


def _find(qdir: Path, box: str, name: str) -> Path:
    if box not in BOXES or not name or any(c in NAME_BAD for c in name) or name.startswith("."):
        raise ValueError("工单名不合法")
    p = qdir / box / name
    if not p.exists():
        raise FileNotFoundError(f"{box}/{name} 不存在")
    return p


def read_ticket(qdir: Path, box: str, name: str) -> dict:
    p = _find(qdir, box, name)
    if p.is_file():
        return {"name": name, "box": box, "ticket": p.read_text(encoding="utf-8-sig"), "result": "", "meta": {}, "files": []}
    rd = lambda f: (p / f).read_text(encoding="utf-8-sig", errors="replace") if (p / f).exists() else ""
    files = [f.name for f in sorted(p.iterdir()) if f.is_file() and not f.name.endswith(".old")
             and f.name not in ("ticket.md", "result.md", "meta.json", "run.jsonl", READY_MARK)]
    return {"name": name, "box": box, "ticket": rd("ticket.md"), "result": rd("result.md"), "meta": read_meta(p), "files": files}


def next_number(qdir: Path) -> int:
    n = 0
    for box in BOXES:
        for p in _items(qdir, box):
            head = p.name.split("_", 1)[0]
            if head.isdigit():
                n = max(n, int(head))
    return n + 1


def submit_ticket(qdir: Path, title: str, cwd: str, sandbox: str, max_resumes, body: str, files: list[tuple[str, bytes]]) -> str:
    cfg = load_config(qdir)
    title = "".join("_" if c in NAME_BAD else c for c in (title or "").strip()).strip(". ") or "未命名"
    if not body.strip():
        raise ValueError("正文不能为空")
    if sandbox not in ALLOWED_SANDBOXES:
        raise ValueError(f"不允许的 sandbox：{sandbox}")
    if not cfg.get("allowed_roots"):
        raise ValueError("还没有设置允许的工作目录：先在「队列设置」里添加")
    if not cwd_allowed(cwd, cfg.get("allowed_roots", [])):
        raise ValueError(f"工作目录不在允许范围内：{cwd}")
    name = f"{next_number(qdir)}_{title}"[:80]
    tmp = qdir / "inbox" / f".tmp_{name}"
    tmp.mkdir(parents=True)
    head = f"---\ncwd: {cwd}\nsandbox: {sandbox}\n"
    if str(max_resumes or "").strip():
        head += f"max_resumes: {int(max_resumes)}\n"
    imgs = []
    for fname, data in files:
        fname = Path(fname).name
        if not fname or fname.startswith(".") or fname in ("ticket.md", "result.md", "meta.json", "run.jsonl"):
            continue
        (tmp / fname).write_bytes(data)
        if fname.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
            imgs.append(fname)
    if imgs:
        head += "images: " + ", ".join(imgs) + "\n"
    (tmp / "ticket.md").write_text(head + "---\n" + body.strip() + "\n", encoding="utf-8")
    (tmp / READY_MARK).write_text("", encoding="utf-8")
    tmp.rename(qdir / "inbox" / name)
    return name


def withdraw_ticket(qdir: Path, name: str) -> None:
    """待派工单撤回 → failed（不删除，留底）。"""
    p = _find(qdir, "inbox", name)
    if p.is_file():
        d = qdir / "inbox" / p.stem
        d.mkdir(exist_ok=True)
        shutil.move(str(p), str(d / "ticket.md"))
        p = d
    finish(qdir, p, read_meta(p), "failed", "已从控制台撤回")


def retry_ticket(qdir: Path, name: str) -> None:
    """失败工单重派：旧输出改名留底，以新会话重新排队。"""
    p = _find(qdir, "failed", name)
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    for f in ("meta.json", "result.md", "run.jsonl"):
        if (p / f).exists():
            (p / f).rename(p / f"{f}.{stamp}.old")
    (p / READY_MARK).write_text("", encoding="utf-8")
    dest = qdir / "inbox" / name
    if dest.exists():
        raise FileExistsError(f"inbox 里已有同名工单：{name}")
    shutil.move(str(p), str(dest))


# ------------------------------------------------------------
# 执行
# ------------------------------------------------------------

def last_agent_message(output: str) -> str:
    last = ""
    for line in output.splitlines():
        try:
            ev = json.loads(line)
        except Exception:
            continue
        item = ev.get("item") or {}
        if ev.get("type") == "item.completed" and item.get("type") == "agent_message":
            last = item.get("text") or last
        msg = ev.get("msg") or {}
        if msg.get("type") == "agent_message":
            last = msg.get("message") or last
    return last


def thread_started_id(line: str) -> str | None:
    """codex exec --json 的 {"type": "thread.started", "thread_id": ...} 行 → 会话号。"""
    if "thread.started" not in line:
        return None
    try:
        ev = json.loads(line)
    except ValueError:
        return None
    if isinstance(ev, dict) and ev.get("type") == "thread.started":
        return ev.get("thread_id")
    return None


def session_from_run_log(folder: Path) -> str | None:
    """run.jsonl 里第一条 thread.started 的会话号（首次运行中途断掉时用来找回）。"""
    try:
        with open(folder / "run.jsonl", "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                sid = thread_started_id(line)
                if sid:
                    return sid
    except OSError:
        pass
    return None


def run_codex(codex: str, folder: Path, cwd: str, sandbox: str, session: str | None, prompt: str, images: list[str],
              on_session=None):
    cmd = [codex, "exec", "--json", "--skip-git-repo-check", "--sandbox", sandbox, "-C", cwd]
    if session:
        cmd += ["resume", session, CONTINUE_PROMPT]
    else:
        for img in images:
            cmd += ["-i", str(folder / img)]
        # 正文默认放命令行参数（守护进程的续跑提示也是这么传的）；太长才走 stdin，Windows 命令行上限 32767 字符。
        # 退回到 npm 的 codex.cmd 时一律走 stdin：cmd.exe 会在参数里的第一个换行处截断
        use_stdin = len(prompt) > ARG_LIMIT or codex.lower().endswith((".cmd", ".bat"))
        cmd += ["-" if use_stdin else prompt]
    logger.info("codex exec %s（%s）", "resume " + session if session else "新会话", folder.name)
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    assert proc.stdin and proc.stdout
    if not session and use_stdin:
        proc.stdin.write(prompt)
    proc.stdin.close()
    out = []
    with open(folder / "run.jsonl", "a", encoding="utf-8") as log:
        for line in proc.stdout:
            out.append(line)
            log.write(line)
            log.flush()
            # 会话号一出现就交给调用方存盘，不等进程结束
            if on_session and (sid := thread_started_id(line)):
                on_session(sid)
                on_session = None
    return proc.wait(), "".join(out)


def work_ticket(qdir: Path, folder: Path, codex: str, app_server, state: dict, cfg: dict) -> None:
    meta = read_meta(folder)
    tmeta, body = parse_ticket(folder / "ticket.md")
    cwd = tmeta.get("cwd", "")
    sandbox = tmeta.get("sandbox", "workspace-write")
    max_resumes = int(tmeta.get("max_resumes", cfg.get("default_max_resumes", 3)))
    images = [x.strip() for x in tmeta.get("images", "").split(",") if x.strip()]
    meta.setdefault("ticket", folder.name)
    meta.setdefault("started_at", now_iso())
    meta.setdefault("resumes", 0)
    meta.setdefault("transient", 0)
    meta.update(cwd=cwd, sandbox=sandbox, max_resumes=max_resumes)

    if not body:
        return finish(qdir, folder, meta, "failed", "ticket.md 没有正文")
    if sandbox not in ALLOWED_SANDBOXES:
        return finish(qdir, folder, meta, "failed", f"不允许的 sandbox：{sandbox}")
    if not cwd or not Path(cwd).is_dir():
        return finish(qdir, folder, meta, "failed", f"cwd 不存在：{cwd!r}")
    if not cwd_allowed(cwd, cfg.get("allowed_roots", [])):
        return finish(qdir, folder, meta, "failed", f"cwd 不在 allowed_roots 内：{cwd}")
    missing = [i for i in images if not (folder / i).exists()]
    if missing:
        return finish(qdir, folder, meta, "failed", f"缺图片：{missing}")

    prompt = TICKET_HEADER.format(name=folder.name, folder=folder, cwd=cwd) + body
    if not meta.get("session") and (sid := session_from_run_log(folder)):
        meta["session"] = sid
        write_meta(folder, meta)
        logger.warning("工单 %s 上次没跑完就断了，从 run.jsonl 找回会话 %s，接着续跑。", folder.name, sid)

    def keep_session(sid):
        meta["session"] = sid
        write_meta(folder, meta)
    # 控制台的「续跑上限 / 清零」对队列同样生效：工单里的 max_resumes 是默认值，控制台设了就以控制台为准
    car.BASE_MAX_RESUMES = car.MAX_RESUMES = max_resumes
    while True:
        state["resume_count"] = meta["resumes"]
        car.apply_control(state)
        meta["resumes"] = state["resume_count"]
        if STOP_FILE.exists():
            write_meta(folder, meta)
            raise SystemExit("STOP file found; 工单留在 running/，下次启动接着跑。")
        state.update(status="checking_quota", target_session=meta.get("session"), target_cwd=cwd, queue_ticket=folder.name)
        save_state(state)
        ensure_app_server(app_server)
        wait_for_quota(app_server, state)

        session = meta.get("session")
        if session:
            cap = car.MAX_RESUMES
            if cap > 0 and meta["resumes"] >= cap:
                return finish(qdir, folder, meta, "failed", f"续跑 {cap} 次仍未完成")
            meta["resumes"] += 1
        state.update(status="running", target_session=session, resume_count=meta["resumes"])
        save_state(state)
        write_meta(folder, meta)

        code, out = run_codex(codex, folder, cwd, sandbox, session, prompt, images,
                              None if session else keep_session)
        if not meta.get("session"):
            # 没见到 thread.started（输出格式变了？）：退回旧办法，从整段输出里找
            meta["session"] = extract_session_id(out)
        msg = last_agent_message(out)
        if msg:
            (folder / "result.md").write_text(msg, encoding="utf-8")
        write_meta(folder, meta)

        if is_writer_error(out):
            time.sleep(exponential_backoff(car.WRITER_INITIAL_BACKOFF, 0, car.WRITER_MAX_BACKOFF))
            meta["resumes"] = max(0, meta["resumes"] - 1)  # 没真正跑，不算次数
            continue
        if is_quota_error(out):
            logger.warning("工单 %s 撞额度，等重置后续跑。", folder.name)
            continue
        if code == 0:
            return finish(qdir, folder, meta, "done")
        if is_transient_error(out) and meta["transient"] < TRANSIENT_CAP:
            meta["transient"] += 1
            time.sleep(exponential_backoff(car.TRANSIENT_INITIAL_BACKOFF, meta["transient"] - 1, car.TRANSIENT_MAX_BACKOFF))
            continue
        return finish(qdir, folder, meta, "failed", f"codex 退出码 {code}；末尾输出见 run.jsonl")


# ------------------------------------------------------------
# 主循环
# ------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Codex 工单队列")
    ap.add_argument("--queue", help="队列目录；不给就用 settings.json 的 queueDir（控制台里可改，改了下一轮生效）")
    ap.add_argument("--codex")
    ap.add_argument("--once", action="store_true", help="队列清空就退出，不常驻")
    args = ap.parse_args()

    fixed = Path(args.queue).resolve() if args.queue else None
    qdir = ensure_queue(fixed or S.queue_dir())

    car.acquire_pid_lock()

    if args.codex:
        os.environ["CODEX_CLI"] = args.codex
    codex = find_codex()
    logger.info("Codex 工单队列启动：%s（codex=%s）", qdir, codex)

    state = load_state()
    app_server = AppServerClient(codex)
    app_server.start()
    try:
        while True:
            if STOP_FILE.exists():
                logger.warning("STOP file found; 队列退出。")
                return
            new_dir = fixed or S.queue_dir()
            if new_dir != qdir:
                logger.info("队列目录改为 %s", new_dir)
                qdir = new_dir
            ensure_queue(qdir)
            cfg = load_config(qdir)
            running = sorted(p for p in (qdir / "running").iterdir() if p.is_dir())
            todo = running or ([] if (qdir / "PAUSE").exists() else collect_inbox(qdir))
            if not todo:
                state.update(status="queue_idle", queue_ticket=None)
                save_state(state)
                if args.once:
                    return
                time.sleep(POLL_SECONDS)
                continue
            folder = todo[0]
            if folder.parent.name == "inbox":
                dest = qdir / "running" / folder.name
                shutil.move(str(folder), str(dest))
                folder = dest
            work_ticket(qdir, folder, codex, app_server, state, cfg)
    finally:
        app_server.close()


if __name__ == "__main__":
    main()
