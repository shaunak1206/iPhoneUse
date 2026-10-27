"""iphone-mcp: computer use for a real iPhone, from the Mac, as MCP tools.

The work happens in phone.Phone (also used by the `iphone` CLI and the iPhone
Live dashboard); this module only describes it to agents. Two engines drive
the screen: WebDriverAgent on the phone (unlocked, can be in the user's hand,
accessibility tree) and macOS iPhone Mirroring (locked and nearby, OCR).
Storage and app facts come from the phone's lockdown services.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import inspect
import sys
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from fastmcp import FastMCP
from fastmcp.utilities.types import Image

from . import device, journal, screen, storage
from .bridge import BridgeError, log
from .phone import SETTINGS, Phone, Refused
from .wda import WdaError

INSTRUCTIONS = """\
Controls the user's real iPhone, through WebDriverAgent on the phone (phone
unlocked, can be in the user's hand) or macOS iPhone Mirroring (phone locked
and near the Mac). phone_connect picks; phone_status says which is in use.

Be fast: plan the whole flow, then act in as few calls as possible.
- Task tools first: phone_storage_report (no screen needed), phone_goto for
  Settings pages, phone_open_app / phone_open_url.
- Known steps go in one phone_do call; only read the screen between calls when
  the next step depends on what appears. Wait with wait_for (a text), never a
  fixed sleep.
- An ad, popup, cookie banner or X in the way: phone_dismiss (or a "dismiss"
  step) clears it in one call. On-screen keyboards in apps and games:
  phone_keys("crane", "enter") taps the whole word from one read.
- Every action returns the screen that followed. Don't call phone_read_screen
  right after an action, and take phone_screenshot only to see icons or images.
  A readout NOTE saying an area "has no labels but shows something" means part
  of the screen is a picture, video or drawn game: look with phone_screenshot.
  For a whole task in such an app, phone_observe("image") makes every action
  return a screenshot instead.
- A tap may come back "A system prompt came up after the last action": a
  permission alert appeared. Answer it with phone_alert, then carry on.

Coordinates are phone points (origin top-left; 390 x 844 on an iPhone 14).
Every text item is tagged with its tap point; prefer phone_tap(text=...).
Alerts, permission prompts and confirmation sheets show up as "PROMPT on
screen"; answer with phone_alert(button=...), or button="dismiss".

At the start of a session, phone_journal shows notes and recent runs from
earlier sessions. Record anything a future session should know with
phone_note.

Rules:
- Never type passcodes or passwords. If a screen asks for one, the user does it.
- Before tapping anything that deletes, buys, sends, posts or signs out, or
  answering a prompt in a way that grants an app access, accepts terms, or says
  yes to a destructive question, ask the user in chat unless their request
  already asked for exactly that. Those taps are refused unless confirm=True.
- Mirroring only works while the phone is locked; WebDriverAgent only while it
  is unlocked. If neither can reach the phone, the tools say why: wait.
- While WebDriverAgent runs, the phone shows an "Automation Running" banner.
  Call phone_disconnect when you are done (it also stops by itself after 2
  minutes without a phone call).
"""

phone = Phone()


def tool_errors(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Failures come back as text the agent can act on, not protocol errors."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except Refused as e:
            phone.record(f"refused: {str(e)[:120]}", ok=False)
            return str(e)
        except BridgeError as e:
            return f"Phone not controllable: {e}"
        except WdaError as e:
            return f"The phone refused that action: {e}"
        except device.DeviceUnavailable as e:
            return f"Device layer unavailable: {e}"
        except ValueError as e:
            return f"Invalid input: {e}"
        except Exception as e:  # keep the server alive
            log(f"{fn.__name__} failed:", repr(e))
            return f"Unexpected error in {fn.__name__}: {type(e).__name__}: {e}"

    wrapper.__signature__ = inspect.signature(fn)  # type: ignore[attr-defined]
    return wrapper


def build() -> FastMCP:
    mcp = FastMCP(name="iphone", instructions=INSTRUCTIONS)
    p = phone
    p.background = True  # one long-lived process: alert checks and label reads can run between calls

    async def shown(line: str, settle: float | None = None) -> Any:
        """The action's line and the screen that followed, as phone_observe asks:
        labels with tap points (text), a picture (image), or both."""
        if p.observe == "image":
            text, jpg = await p.after_image(line, settle)
            return [text, Image(data=jpg, format="jpeg")]
        readout = await p.after(line, settle)
        if p.observe == "both":
            s = await p.io(p.eng().capture, image=True, ocr=False)
            return [readout, Image(data=s.jpg, format="jpeg")] if s.jpg else readout
        return readout

    # -- connection --------------------------------------------------------

    @mcp.tool
    @tool_errors
    async def phone_status() -> str:
        """Report whether the iPhone can be controlled right now, which engine is in
        use, and why not if it can't (WebDriverAgent, iPhone Mirroring, macOS
        permissions, and whether storage/app queries can reach the phone)."""
        return await p.status_text()

    @mcp.tool
    @tool_errors
    async def phone_connect(wait_seconds: int = 20, engine: Literal["auto", "wda", "mirror"] = "auto") -> str:
        """Get the phone controllable and say how.

        auto: WebDriverAgent if it is up and the phone is unlocked; else start it
        (~10 s; starting needs the phone plugged in on networks that block device
        discovery, like campus Wi-Fi); else iPhone Mirroring. It cannot get past
        Face ID, Touch ID or a passcode; it reports those."""
        return await p.connect(engine, wait_seconds)

    @mcp.tool
    @tool_errors
    async def phone_record(action: Literal["start", "stop", "status"], name: str | None = None) -> str:
        """Record the phone's screen to an MP4 on the Mac (for demos and bug reports).
        start (optional file name), then stop to save it; status says if recording.
        Needs WebDriverAgent running."""
        if action == "start":
            return f"Recording to {await asyncio.to_thread(p.record_start, name)}. Call phone_record('stop') to save."
        if action == "stop":
            return f"Saved {await asyncio.to_thread(p.record_stop)}"
        return f"Recording to {p.recording()}" if p.recording() else "Not recording."

    @mcp.tool
    @tool_errors
    async def phone_disconnect() -> str:
        """Stop WebDriverAgent when you are done with the phone. That clears the
        "Automation Running" banner and uploads this session's actions to the run
        journal."""
        stopped = await p.disconnect()
        journal.background(journal.flush, "session")
        return ("Stopped WebDriverAgent; the phone's 'Automation Running' banner is gone."
                if stopped else "WebDriverAgent was not running.")

    @mcp.tool
    @tool_errors
    async def phone_watch(open_on_mac: bool = True) -> str:
        """Open iPhone Live on the Mac: the phone's screen live, status, Connect/Stop,
        and every action agents take, so the user can watch. It keeps running
        between sessions."""
        url = await p.watch(open_on_mac)
        return f"iPhone Live is {'open' if open_on_mac else 'running'} at {url}."

    @mcp.tool
    @tool_errors
    async def phone_observe(mode: Literal["text", "image", "both"]) -> str:
        """How every action reports the screen that followed, for the rest of the session.

        text (default): labels with tap points, about 0.4 s; best for apps, Settings
        and forms. image: a screenshot instead, free from the live stream and
        returned without waiting for labels; best for games, maps, photos and
        anything drawn without labels (tap by x, y: 1 pixel = 1 point). both: the two.
        The safety check on taps reads the labels either way."""
        p.observe = mode
        return {"text": "Actions now return the screen as labels with tap points.",
                "image": "Actions now return a screenshot of the screen that followed.",
                "both": "Actions now return labels with tap points and a screenshot."}[mode]

    # -- seeing ------------------------------------------------------------

    @mcp.tool(output_schema=None)
    @tool_errors
    async def phone_screenshot(include_text: bool = True, marks: bool = False) -> list:
        """Screenshot of the phone (1 image pixel = 1 phone point, so pixel coordinates
        are tap coordinates), plus the text with tap points unless include_text is false.
        marks=true draws a numbered box on every element and lists them; then
        phone_tap(text="#12") taps box 12, which is the quickest way to hit icons."""
        if marks:
            jpg, legend = await p.marked()
            return [Image(data=jpg, format="jpeg"), legend]
        s = await p.io(p.eng().capture, image=True, ocr=include_text)
        assert s.jpg is not None
        out: list = [Image(data=s.jpg, format="jpeg")]
        if include_text:
            out.append(screen.describe(s))
        return out

    @mcp.tool
    @tool_errors
    async def phone_read_screen() -> str:
        """Text on the phone screen with a tap point for each item. Actions already
        return the screen that followed; use this only to orient."""
        return screen.describe(await p.read())

    @mcp.tool(output_schema=None)
    @tool_errors
    async def phone_zoom(x: float, y: float, width: float, height: float) -> Image:
        """High-resolution crop of a region (phone points), for small text or icons."""
        if width <= 0 or height <= 0:
            raise ValueError("width and height must be positive")
        return Image(data=await p.io(p.eng().zoom, x, y, width, height), format="jpeg")

    @mcp.tool
    @tool_errors
    async def phone_find(text: str, scroll: bool = False, max_scrolls: int = 8,
                         direction: Literal["down", "up"] = "down") -> str:
        """Find text on screen. With scroll=true, keep scrolling the current view until
        it appears (or the view stops moving). Returns match tap points."""
        try:
            return await p.find(text, scroll, max_scrolls, direction)
        except ValueError as e:
            return str(e)

    # -- acting ------------------------------------------------------------

    @mcp.tool(output_schema=None)
    @tool_errors
    async def phone_tap(x: float | None = None, y: float | None = None, text: str | None = None,
                        occurrence: int = 1, exact: bool = False, double: bool = False,
                        confirm: bool = False) -> Any:
        """Tap at (x, y) phone points, or on visible text (text=..., the Nth match top to
        bottom with occurrence=N). Returns the screen afterwards.

        Taps on delete/buy/send/sign-out style controls, and prompt answers that
        grant access or confirm a destructive question, are refused unless
        confirm=True, which you may only pass after the user said yes in chat
        (or their request asked for exactly that)."""
        return await shown(await p.tap(x, y, text, occurrence, exact, double, confirm))

    @mcp.tool(output_schema=None)
    @tool_errors
    async def phone_long_press(x: float | None = None, y: float | None = None, text: str | None = None,
                               seconds: float = 1.0, occurrence: int = 1, confirm: bool = False) -> Any:
        """Press and hold (context menus, home-screen edit mode, drag handles)."""
        return await shown(await p.long_press(x, y, text, seconds, occurrence, confirm))

    @mcp.tool(output_schema=None)
    @tool_errors
    async def phone_swipe(direction: Literal["up", "down", "left", "right"] | None = None,
                          x: float | None = None, y: float | None = None,
                          to_x: float | None = None, to_y: float | None = None,
                          distance: float = 0.5, duration_ms: int = 300) -> Any:
        """Drag a finger across the screen. direction is the way the finger moves
        (swipe 'left' pages a carousel forward). Starts at the screen center
        unless x/y given; distance is a fraction of the screen. Or give an exact
        path with x, y, to_x, to_y."""
        return await shown(await p.swipe(direction, x, y, to_x, to_y, distance, duration_ms))

    @mcp.tool(output_schema=None)
    @tool_errors
    async def phone_scroll(direction: Literal["down", "up", "left", "right"] = "down", amount: float = 0.5,
                           x: float | None = None, y: float | None = None) -> Any:
        """Scroll content exactly (no fling). 'down' reveals what is below. amount is a
        fraction of the screen height (0.5 = half a screen). x/y pick which
        scrollable area, default the center."""
        return await shown(await p.scroll(direction, amount, x, y))

    @mcp.tool(output_schema=None)
    @tool_errors
    async def phone_type(text: str, submit: bool = False) -> Any:
        """Type into the focused field (tap the field first). submit=true presses Return.
        Never use this for passcodes or passwords."""
        return await shown(await p.type(text, submit))

    @mcp.tool(output_schema=None)
    @tool_errors
    async def phone_key(key: str, modifiers: str = "") -> Any:
        """Press a key: return, delete, escape, tab, space, or one character.
        Modifiers (cmd, shift) and arrows need iPhone Mirroring."""
        return await shown(await p.key(key, modifiers))

    @mcp.tool(output_schema=None)
    @tool_errors
    async def phone_go(where: Literal["home", "app_switcher", "spotlight", "back"]) -> Any:
        """System navigation: home screen, app switcher, Spotlight search, or 'back'
        (left-edge swipe, the iOS back gesture)."""
        return await shown(await p.go(where))

    @mcp.tool(output_schema=None)
    @tool_errors
    async def phone_open_app(name: str) -> Any:
        """Open an app by name or bundle id (instant under WebDriverAgent; Spotlight
        otherwise). Returns the app's first screen."""
        return await shown(await p.open_app(name), settle=0)

    @mcp.tool(output_schema=None)
    @tool_errors
    async def phone_open_url(url: str) -> Any:
        """Open a web page or an app deep link (e.g. 'maps.apple.com/?q=coffee',
        'spotify:search:jazz'). Links that hand off to an app may show an
        'Open in ...?' prompt: answer it with phone_alert(button='Open')."""
        return await shown(await p.open_url(url), settle=0)

    @mcp.tool(output_schema=None, description="Jump straight to a Settings page in one call instead of tapping through. "
                          "Places: " + ", ".join(sorted(SETTINGS)) + ".")
    @tool_errors
    async def phone_goto(place: str) -> Any:
        return await shown(await p.goto(place))

    @mcp.tool(output_schema=None)
    @tool_errors
    async def phone_alert(button: str | None = None, confirm: bool = False) -> Any:
        """Read or answer the alert, permission prompt or confirmation sheet on screen.

        No button: describe the prompt (question and buttons). button='Allow Once',
        'OK', 'Delete', ...: tap that answer. button='dismiss': tap the safe way out
        (Cancel / Don't Allow / Not Now). Answers that grant access, accept terms,
        or say yes to a destructive question need confirm=True, which you may only
        pass after the user said yes in chat (or their request asked for exactly that)."""
        if button is None:
            s = await p.read()
            pr = screen.prompt(s)
            return pr.describe() if pr else ("No alert, permission prompt or confirmation sheet detected.\n\n"
                                             + screen.describe(s))
        return await shown(await p.alert(button, confirm))

    @mcp.tool(output_schema=None)
    @tool_errors
    async def phone_dismiss() -> Any:
        """Clear whatever is in the way in one call: system prompts (the safe answer),
        ads ("Continue to ...", "Skip", waits out "Skip in 5 s"), close buttons and
        X's (labelled or drawn), cookie banners (Reject). Returns the screen after."""
        return await shown(await p.dismiss())

    @mcp.tool(output_schema=None)
    @tool_errors
    async def phone_keys(keys: str, then: str | None = None) -> Any:
        """Type on a keyboard drawn by the app or web page (Wordle, keypads, games),
        where phone_type cannot reach: keys="crane" taps c, r, a, n, e from one screen
        read; then="enter" (or "backspace") presses a named key after."""
        seq: list[str] = list(keys) + ([then] if then else [])
        return await shown(await p.keys(seq))

    @mcp.tool
    @tool_errors
    async def phone_wait(seconds: float = 1.0, for_text: str | None = None, timeout: float = 15) -> str:
        """Wait a fixed time, or until text appears (polling up to timeout seconds)."""
        if not for_text:
            await p.sleep(seconds)
            return screen.describe(await p.read())
        try:
            msg = await p.wait_for(for_text, timeout)
        except ValueError as e:
            return str(e)
        return f"{msg}\n\n{screen.describe(p.eng().last or await p.read())}"

    @mcp.tool
    @tool_errors
    async def phone_do(steps: list[dict[str, Any]]) -> str:
        """Run several actions in one call, stopping at the first one that fails or is
        refused. The fastest way to do anything with known steps. Each step is one
        action key plus options, for example:

          [{"goto": "storage"}, {"wait_for": "Recommendations", "timeout": 20},
           {"tap": "Review Large Attachments"}]

        Actions: open_app, open_url, goto, tap (text or [x, y]; occurrence, exact,
        double), taps ([labels or points] from one read), keys (on-screen keyboard:
        "crane" or ["c", "enter"]), long_press, type (submit), key (modifiers), go,
        scroll (amount), swipe (distance), alert (button or 'dismiss'), dismiss (true:
        ads, popups, X's, cookie banners), find (scroll), wait_for (timeout), wait
        (seconds; prefer wait_for). A step that needs the user's yes takes
        "confirm": true, under the same rule as phone_tap. Returns one line per
        step and the final screen."""
        return await p.do(steps)

    # -- memory ------------------------------------------------------------

    @mcp.tool
    @tool_errors
    async def phone_journal(limit: int = 5) -> str:
        """Notes and recent runs from earlier sessions (kept in a private GitHub repo),
        plus storage history. Read it at the start of a session for context."""
        return await asyncio.to_thread(journal.recent, limit)

    @mcp.tool
    @tool_errors
    async def phone_note(text: str) -> str:
        """Save a fact or lesson for future sessions (e.g. 'Spotify cache is cleared
        from Settings > Storage', 'user keeps Tesla app: it is their car key')."""
        await asyncio.to_thread(journal.note, text)
        return f"Noted in https://github.com/{journal.REPO}/blob/main/NOTES.md"

    # -- device layer (USB / Wi-Fi lockdown) --------------------------------

    @mcp.tool
    @tool_errors
    async def phone_storage_report(deep: bool = False) -> str:
        """Where the storage went, in one call and without touching the screen:
        free space, every app's size (app vs. documents & data), and with deep=true
        a scan of the camera roll and Photos' caches (~1 min: types, Live Photo
        motion, largest videos, years, screenshots). Ends with concrete ways to get
        space back. Read-only. Needs the cable or Wi-Fi sync. Saved to the journal."""
        r = await p.storage_report(deep=deep)
        text = storage.format_report(r)
        journal.background(journal.save_report, r, text)
        return text

    @mcp.tool
    @tool_errors
    async def device_info() -> str:
        """Model, iOS version, battery, and storage totals. Needs the phone paired
        over USB once (works over Wi-Fi sync after that)."""
        d = await device.info()
        v, b, disk = d["values"], d["battery"], d["disk"]
        cap = disk.get("TotalDataCapacity") or disk.get("TotalDiskCapacity")
        free = disk.get("AmountDataAvailable") or disk.get("TotalDataAvailable")
        lines = [
            f"Name: {v.get('DeviceName')}",
            f"Model: {device.model_name(v.get('ProductType'))} ({v.get('ProductType')}, {v.get('ModelNumber', '')})",
            f"iOS: {v.get('ProductVersion')} ({v.get('BuildVersion')})",
            f"Battery: {b.get('BatteryCurrentCapacity', '?')}%{' charging' if b.get('BatteryIsCharging') else ''}",
            f"Storage: {device.gb(free)} free of {device.gb(cap)}"
            + (f" ({100 * (1 - free / cap):.0f}% used)" if cap and free else ""),
        ]
        return "\n".join(lines)

    @mcp.tool
    @tool_errors
    async def device_apps(query: str = "", include_system: bool = False) -> str:
        """List installed apps (name, bundle id, version, size). Filter with query."""
        apps = await device.apps("Any" if include_system else "User", sizes=True)
        q = query.lower()
        apps = [a for a in apps if not q or q in a["name"].lower() or q in a["bundle_id"].lower()]
        apps.sort(key=lambda a: a["name"].lower())
        if not apps:
            return "No matching apps."
        return f"{len(apps)} app(s):\n" + "\n".join(
            f"  {a['name']} {a['version']}  {device.gb(a['total_bytes'])}  `{a['bundle_id']}`" for a in apps)

    return mcp


def main() -> None:
    ap = argparse.ArgumentParser(prog="iphone-mcp")
    ap.add_argument("--check", action="store_true", help="print readiness and exit")
    ap.add_argument("--list-tools", action="store_true")
    a = ap.parse_args()
    mcp = build()
    if a.list_tools:
        async def names() -> list[str]:
            return [t.name for t in await mcp.list_tools()]
        print("\n".join(asyncio.run(names())))
        return
    if a.check:
        print(asyncio.run(phone.status_text()))
        return
    log("starting (stdio)")
    mcp.run(show_banner=False)


if __name__ == "__main__":
    sys.exit(main())
