"""Run journal on GitHub: what each session did, storage reports over time, and
notes worth remembering, kept in a private repo instead of on this Mac so any
future agent (or person) can read the history.

    runs/2026-10-27/0110-session.md   one per session: every action, with times
    reports/storage/2026-10-27-0112.md, reports/storage/history.csv
    NOTES.md                          lessons and facts, newest last

Writes go through `gh api` (the user's GitHub login) on a background thread
and never block or fail a phone action. IPHONE_JOURNAL=0 turns it off.

    python -m iphone_mcp.journal flush    upload the activity since the last upload
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from typing import Any

from .bridge import ROOT, log

REPO = os.environ.get("IPHONE_JOURNAL_REPO", "shaunak1206/iphone-agent-journal")
ACTIVITY = ROOT / "build" / "activity.jsonl"
OFFSET = ROOT / "build" / "journal.offset"
_lock = threading.Lock()


def enabled() -> bool:
    return os.environ.get("IPHONE_JOURNAL", "1") != "0"


def _gh(*args: str, body: dict | None = None) -> Any:
    cmd = ["gh", "api", *args]
    if body is not None:
        cmd += ["--input", "-"]
    r = subprocess.run(cmd, input=json.dumps(body) if body is not None else None, capture_output=True, text=True,
                       timeout=60)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[:300])
    return json.loads(r.stdout) if r.stdout.strip() else None


def ensure_repo() -> None:
    try:
        _gh(f"repos/{REPO}")
        return
    except RuntimeError:
        pass
    owner, name = REPO.split("/", 1)
    subprocess.run(["gh", "repo", "create", REPO, "--private", "--description",
                    "Run journal for iPhoneMCP: sessions, storage reports, notes"], check=True,
                   capture_output=True, text=True, timeout=60)
    put("README.md", "# iPhone agent journal\n\nWritten by [iPhoneMCP](https://github.com/shaunak1206/iPhoneMCP).\n\n"
        "- `runs/`: one file per session, every phone action with its time\n"
        "- `reports/storage/`: storage reports and `history.csv` over time\n"
        "- `NOTES.md`: lessons and facts for future sessions\n", "Start the journal")


def get(path: str) -> tuple[str | None, str | None]:
    try:
        r = _gh(f"repos/{REPO}/contents/{path}")
    except RuntimeError:
        return None, None
    return base64.b64decode(r["content"]).decode(), r["sha"]


def put(path: str, content: str, message: str) -> None:
    _, sha = get(path)
    body: dict[str, Any] = {"message": message, "content": base64.b64encode(content.encode()).decode()}
    if sha:
        body["sha"] = sha
    _gh("-X", "PUT", f"repos/{REPO}/contents/{path}", body=body)


def append(path: str, text: str, message: str, header: str = "") -> None:
    old, _ = get(path)
    put(path, (old if old is not None else header) + text, message)


def _stamp(t: float | None = None) -> datetime:
    return datetime.fromtimestamp(t or time.time())


def flush(kind: str = "session") -> str | None:
    """Upload actions recorded since the last upload as one run file."""
    if not enabled() or not ACTIVITY.exists():
        return None
    with _lock:
        start = int(OFFSET.read_text()) if OFFSET.exists() else 0
        data = ACTIVITY.read_bytes()
        if len(data) < start:
            start = 0  # the log was cleared
        new = [json.loads(line) for line in data[start:].decode(errors="replace").splitlines() if line.strip()]
        if not new:
            return None
        t0, t1 = _stamp(new[0]["t"]), _stamp(new[-1]["t"])
        path = f"runs/{t0:%Y-%m-%d}/{t0:%H%M%S}-{kind}.md"
        failed = sum(1 for e in new if not e.get("ok", True))
        lines = [f"# {kind} {t0:%Y-%m-%d %H:%M}–{t1:%H:%M}", "",
                 f"{len(new)} actions, {failed} stopped", ""]
        lines += [f"- {_stamp(e['t']):%H:%M:%S} {'' if e.get('ok', True) else '**stopped** '}{e['action']}"
                  for e in new]
        ensure_repo()
        put(path, "\n".join(lines) + "\n", f"Run {t0:%Y-%m-%d %H:%M} ({len(new)} actions)")
        OFFSET.write_text(str(len(data)))
        return path


def note(text: str) -> None:
    ensure_repo()
    append("NOTES.md", f"- {_stamp():%Y-%m-%d %H:%M} {text.strip()}\n", "Add a note",
           header="# Notes\n\nFacts and lessons for future sessions, newest last.\n\n")


def save_report(report: dict, text: str) -> str:
    ensure_repo()
    now = _stamp()
    path = f"reports/storage/{now:%Y-%m-%d-%H%M}.md"
    put(path, f"# Storage {now:%Y-%m-%d %H:%M}\n\n```\n{text}\n```\n", f"Storage report {now:%Y-%m-%d %H:%M}")
    cr = (report.get("media") or {}).get("camera_roll") or {}
    top = "; ".join(f"{a['name']} {a['total_bytes'] / 1e9:.2f}" for a in report["apps"][:5])
    row = (f"{now:%Y-%m-%d %H:%M},{report['free'] / 1e9:.2f},{report['capacity'] / 1e9:.2f},"
           f"{(cr.get('bytes') or 0) / 1e9:.2f},\"{top}\"\n")
    append("reports/storage/history.csv", row, "Storage history",
           header="when,free_gb,capacity_gb,camera_roll_gb,top_apps_gb\n")
    return path


def recent(limit: int = 5) -> str:
    """NOTES.md plus the newest run files, for an agent starting a session."""
    out = []
    notes, _ = get("NOTES.md")
    if notes:
        out.append(notes.strip())
    try:
        days = sorted((d["name"] for d in _gh(f"repos/{REPO}/contents/runs") if d["type"] == "dir"), reverse=True)
    except RuntimeError:
        days = []
    runs: list[str] = []
    for day in days:
        files = sorted((f["path"] for f in _gh(f"repos/{REPO}/contents/runs/{day}")), reverse=True)
        runs += files
        if len(runs) >= limit:
            break
    for path in runs[:limit]:
        body, _ = get(path)
        if body:
            head = body.strip().splitlines()
            out.append("\n".join(head[:3] + head[3:][-12:]))  # title, count, last actions
    hist, _ = get("reports/storage/history.csv")
    if hist:
        out.append("Storage history (newest last):\n" + "\n".join(hist.strip().splitlines()[-5:]))
    return ("\n\n---\n\n".join(out) or "The journal is empty.") + f"\n\n(https://github.com/{REPO})"


def background(fn, *a) -> None:
    """Run a journal write without blocking the caller; failures are only logged."""
    if not enabled():
        return

    def go() -> None:
        try:
            fn(*a)
        except Exception as e:
            log("journal write failed:", repr(e))
    threading.Thread(target=go, daemon=False).start()


if __name__ == "__main__":
    if sys.argv[1:2] == ["flush"]:
        print(flush(sys.argv[2] if len(sys.argv) > 2 else "session") or "nothing new")
    elif sys.argv[1:2] == ["recent"]:
        print(recent())
    else:
        print(__doc__)
