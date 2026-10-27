"""Live check on the real phone, with timings. Read-only: it opens Settings and
Chrome, looks around, records a few seconds, and goes home. Nothing is tapped
that changes anything.

    .venv/bin/python tests/live_check.py          (phone plugged in and unlocked)

Prints one line per step (ok / FAIL, seconds) and a summary. Run it before a
demo: if every line is ok, the demo will work.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from iphone_mcp import screen, storage  # noqa: E402
from iphone_mcp.phone import Phone  # noqa: E402

results: list[tuple[str, bool, float, str]] = []


async def step(label: str, coro, expect=None) -> object:
    t0 = time.time()
    try:
        out = await coro
        ok = expect(out) if expect else True
        note = "" if ok else f"unexpected: {str(out)[:160]}"
    except Exception as e:
        out, ok, note = None, False, f"{type(e).__name__}: {str(e)[:160]}"
    dt = time.time() - t0
    results.append((label, ok, dt, note))
    print(f"  {'ok  ' if ok else 'FAIL'} {dt:5.2f}s  {label}{'  -> ' + note if note else ''}", flush=True)
    return out


async def main() -> int:
    p = Phone(engine="wda")
    print("live check")
    await step("connect (starts WebDriverAgent if needed)", p.connect("wda", 60), lambda r: r.startswith("Connected"))
    await step("read the screen", p.read(), lambda s: len(s.items) > 3)
    await step("go home", p.go("home"))
    await step("open Settings by bundle id", p.open_app("Settings"))
    await step("settle (frames stop changing)", p.settle_screen())
    await step("goto battery", p.goto("battery"))
    await step("battery page is showing", p.wait_for("Battery", 8))
    await step("goto storage (scrolls to General)", p.goto("storage"))
    await step("storage page is showing", p.wait_for("iPhone Storage", 10))
    await step("screenshot from the live stream", p.io(p.eng().capture, image=True, ocr=False), lambda s: s.jpg)
    await step("marked screenshot", p.marked(), lambda r: r[1].startswith("#1"))
    await step("dismiss (nothing should be in the way)", p.dismiss(), lambda r: r.startswith("Nothing"))
    await step("open Chrome", p.open_app("com.google.chrome.ios"))
    await step("read Chrome (no hidden tab list)", p.read(),
               lambda s: not screen.find(s, "Inactive tabs", exact=True))
    await step("record 3 s", _record(p), lambda path: path and Path(path).stat().st_size > 10_000)
    await step("go home", p.go("home"))
    await step("storage report (no screen)", storage.report(deep=False), lambda r: r["capacity"] > 0)
    taps = [r for r in results if r[0] in ("go home", "open Settings by bundle id")]
    print(f"\n{sum(r[1] for r in results)}/{len(results)} ok; "
          f"typical action {sum(r[2] for r in taps) / max(len(taps), 1):.2f}s")
    if "--keep" not in sys.argv:
        await p.disconnect()
    return 0 if all(r[1] for r in results) else 1


async def _record(p: Phone) -> str:
    await asyncio.to_thread(p.record_start, "live-check")
    await asyncio.sleep(3)
    return await asyncio.to_thread(p.record_stop)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
