"""Live end-to-end check on the real phone. Read-only: it navigates Settings
and comes back home, but taps nothing that changes anything.

    .venv/bin/python tests/live_check.py [outdir]

Needs iPhone Mirroring connected (phone locked and nearby).
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastmcp import Client  # noqa: E402

from iphone_mcp.server import build  # noqa: E402

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/iphone-live")
OUT.mkdir(parents=True, exist_ok=True)
results: list[tuple[str, bool, float]] = []


async def step(c: Client, label: str, tool: str, args: dict, expect: list[str] | None = None,
               shot: str | None = None) -> str:
    t0 = time.time()
    r = await c.call_tool(tool, args)
    dt = time.time() - t0
    text = "\n".join(x.text for x in r.content if getattr(x, "type", "") == "text")
    for x in r.content:
        if getattr(x, "type", "") == "image" and shot:
            import base64
            (OUT / shot).write_bytes(base64.b64decode(x.data))
    ok = all(e.lower() in text.lower() for e in (expect or []))
    results.append((label, ok, dt))
    print(f"\n=== {'ok  ' if ok else 'FAIL'} {label}  ({dt:.1f}s)\n{text[:1500]}")
    return text


async def main() -> int:
    async with Client(build()) as c:
        await step(c, "status", "phone_status", {})
        await step(c, "connect", "phone_connect", {"wait_seconds": 15}, ["Connected"])
        await step(c, "home", "phone_go", {"where": "home"}, ["Went home"])
        await step(c, "screenshot home", "phone_screenshot", {}, ["Screen 390x844"], shot="home.jpg")
        await step(c, "open Settings", "phone_open_app", {"name": "Settings"}, ["General"])
        await step(c, "screenshot settings", "phone_screenshot", {}, [], shot="settings.jpg")
        await step(c, "find General (scrolling)", "phone_find", {"text": "General", "scroll": True}, ["Found"])
        await step(c, "tap General", "phone_tap", {"text": "General", "exact": True}, ["About"])
        await step(c, "tap About", "phone_tap", {"text": "About", "exact": True}, ["iOS Version"])
        await step(c, "screenshot about", "phone_screenshot", {}, [], shot="about.jpg")
        await step(c, "scroll down", "phone_scroll", {"direction": "down", "amount": 0.5}, [])
        await step(c, "scroll up", "phone_scroll", {"direction": "up", "amount": 0.5}, ["iOS Version"])
        await step(c, "back gesture", "phone_go", {"where": "back"}, ["iPhone Storage"])
        await step(c, "tap iPhone Storage (read-only screen)", "phone_tap", {"text": "iPhone Storage"}, ["iPhone Storage"])
        await step(c, "wait for storage list", "phone_wait", {"seconds": 4}, [])
        await step(c, "screenshot storage", "phone_screenshot", {}, [], shot="storage.jpg")
        await step(c, "zoom top", "phone_zoom", {"x": 0, "y": 0, "width": 390, "height": 200}, [], shot="zoom.jpg")
        await step(c, "back to General", "phone_go", {"where": "back"}, ["About"])
        await step(c, "app switcher", "phone_go", {"where": "app_switcher"}, [])
        await step(c, "home again", "phone_go", {"where": "home"}, [])
    print("\n" + "\n".join(f"{'ok  ' if ok else 'FAIL'} {dt:5.1f}s  {label}" for label, ok, dt in results))
    print(f"screenshots in {OUT}")
    return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
