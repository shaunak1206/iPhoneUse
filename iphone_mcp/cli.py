"""`iphone`: the phone from the terminal or any script.

    iphone status                     iphone connect | stop
    iphone read                       iphone shot screen.jpg
    iphone open Settings              iphone goto storage
    iphone tap "General"              iphone type "hello" --submit
    iphone home | back                iphone url maps.apple.com/?q=coffee
    iphone do '[{"goto":"battery"},{"wait_for":"Battery Health"}]'
    iphone storage [--deep] [--json]  iphone live
    iphone journal                    iphone note "keep the Tesla app: it is the car key"
    iphone mcp                        (run the MCP server on stdio)

Exit status is 0 on success, 1 when the phone refused or the step failed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from . import device, journal, screen, storage
from .bridge import BridgeError
from .phone import SETTINGS, Phone, Refused
from .wda import WdaError


async def run(a: argparse.Namespace) -> str:
    p = Phone()
    c = a.cmd
    if c == "status":
        return await p.status_text()
    if c == "connect":
        return await p.connect(a.engine)
    if c == "stop":
        stopped = await p.disconnect()
        journal.background(journal.flush, "session")
        return "stopped" if stopped else "was not running"
    if c == "read":
        return screen.describe(await p.read())
    if c == "shot":
        s = await p.io(p.eng().capture, image=True, ocr=False)
        with open(a.file, "wb") as f:
            f.write(s.jpg or b"")
        return f"saved {a.file}"
    if c == "tap":
        return await p.after(await p.tap(text=a.text, confirm=a.confirm))
    if c == "type":
        return await p.after(await p.type(a.text, a.submit))
    if c == "open":
        return await p.after(await p.open_app(a.app), settle=0)
    if c == "url":
        return await p.after(await p.open_url(a.url), settle=0)
    if c == "goto":
        return await p.after(await p.goto(a.place))
    if c in ("home", "back"):
        return await p.after(await p.go(c))
    if c == "do":
        steps = json.load(open(a.steps[1:])) if a.steps.startswith("@") else json.loads(a.steps)
        return await p.do(steps)
    if c == "storage":
        r = await p.storage_report(deep=a.deep)
        text = storage.format_report(r)
        journal.background(journal.save_report, r, text)
        return json.dumps(r, indent=1, default=str) if a.json else text
    if c == "live":
        return f"iPhone Live: {await p.watch(open_on_mac=True)}"
    if c == "journal":
        return await asyncio.to_thread(journal.recent, a.limit)
    if c == "note":
        await asyncio.to_thread(journal.note, a.text)
        return "noted"
    raise ValueError(f"unknown command {c}")


def main() -> None:
    ap = argparse.ArgumentParser(prog="iphone", description="Control a real iPhone from the Mac.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("status", "stop", "read", "home", "back", "live", "mcp"):
        sub.add_parser(name)
    sub.add_parser("connect").add_argument("--engine", choices=["auto", "wda", "mirror"], default="auto")
    sub.add_parser("shot").add_argument("file", nargs="?", default="screen.jpg")
    t = sub.add_parser("tap")
    t.add_argument("text")
    t.add_argument("--confirm", action="store_true", help="the user said yes to this exact tap")
    ty = sub.add_parser("type")
    ty.add_argument("text")
    ty.add_argument("--submit", action="store_true")
    sub.add_parser("open").add_argument("app")
    sub.add_parser("url").add_argument("url")
    sub.add_parser("goto").add_argument("place", choices=sorted(SETTINGS))
    sub.add_parser("do").add_argument("steps", help="JSON list of steps, or @file.json")
    st = sub.add_parser("storage")
    st.add_argument("--deep", action="store_true", help="also scan the camera roll (~1 min)")
    st.add_argument("--json", action="store_true")
    sub.add_parser("journal").add_argument("--limit", type=int, default=5)
    sub.add_parser("note").add_argument("text")
    a = ap.parse_args()
    if a.cmd == "mcp":
        from .server import main as serve
        sys.argv = sys.argv[:1]
        serve()
        return
    try:
        out = asyncio.run(run(a))
    except (Refused, ValueError, BridgeError, WdaError, device.DeviceUnavailable) as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    print(out)
    sys.exit(1 if "stopped:" in out and "Remaining steps were not run" in out else 0)


if __name__ == "__main__":
    main()
