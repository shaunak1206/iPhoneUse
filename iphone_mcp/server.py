"""iphone-mcp: computer use for a real iPhone, from the Mac.

Screen control rides on macOS iPhone Mirroring (no app on the phone, no
Xcode, no developer account). Storage and app facts come from the phone's
lockdown services over USB/Wi-Fi when it is paired.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import inspect
import re
import sys
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from fastmcp import FastMCP
from fastmcp.utilities.types import Image

from . import device, screen
from .bridge import BridgeError, Mirror, Screen, log

INSTRUCTIONS = """\
Controls the user's real iPhone through macOS iPhone Mirroring.

Coordinates are phone points: origin top-left of the phone screen, x to the
right, y down (an iPhone 13 is 390 x 844). Every text item in a screen
description is tagged with its tap point, so you can usually act from text
alone and only take phone_screenshot when you need to see icons or images.

Loop: phone_read_screen (or phone_screenshot) -> one action -> read the
screen the action returns -> next action. Prefer phone_tap(text=...) over raw
coordinates. Use phone_open_app to launch apps (it goes through Spotlight) and
phone_open_url for links and app deep links. When you already know several
steps (open app, tap, type, wait), send them as one phone_do call.

Alerts, permission prompts and confirmation sheets are flagged in every screen
readout as "PROMPT on screen". Answer them with phone_alert(button=...);
phone_alert(button="dismiss") backs out with the safe choice.

Rules:
- Mirroring only works while the phone is locked and nearby. If the user is
  holding their phone, the tools say so: wait, don't fight it.
- Never type passcodes or passwords. If a screen asks for one, the user does it.
- Before tapping anything that deletes, buys, sends, posts or signs out, or
  answering a prompt in a way that grants an app access, accepts terms, or says
  yes to a destructive question, ask the user in chat unless their request
  already asked for exactly that. Those taps are refused unless confirm=True.
- The phone's own camera, microphone and Notification Center are unavailable
  while mirroring.
"""

mirror = Mirror()
_lock = asyncio.Lock()


class Refused(Exception):
    """A tap the safety rails stopped; the message tells the agent what to ask."""
SETTLE = 0.8  # seconds to let iOS animate before reading the result


def tool_errors(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Failures come back as text the agent can act on, not protocol errors."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except Refused as e:
            return str(e)
        except BridgeError as e:
            return f"Phone not controllable: {e}"
        except device.DeviceUnavailable as e:
            return f"Device layer unavailable: {e}"
        except ValueError as e:
            return f"Invalid input: {e}"
        except Exception as e:  # keep the server alive
            log(f"{fn.__name__} failed:", repr(e))
            return f"Unexpected error in {fn.__name__}: {type(e).__name__}: {e}"

    wrapper.__signature__ = inspect.signature(fn)  # type: ignore[attr-defined]
    return wrapper


async def _io(fn, *a, **kw):
    """Bridge calls are blocking subprocesses; keep them off the event loop
    and one at a time (two agents racing the mouse ends badly)."""
    async with _lock:
        return await asyncio.to_thread(fn, *a, **kw)


async def _read(image: bool = False, settle: float = 0.0) -> Screen:
    if settle:
        await asyncio.sleep(settle)
    return await _io(mirror.capture, image=image, ocr=True)


async def _after(action: str, settle: float = SETTLE) -> str:
    s = await _read(settle=settle)
    return f"{action}\n\n{screen.describe(s)}"


def _url(u: str) -> str:
    u = u.strip()
    return u if re.match(r"^[a-zA-Z][\w+.-]*:", u) else "https://" + u


def _img(s: Screen) -> Image:
    assert s.jpg is not None
    return Image(data=s.jpg, format="jpeg")


async def _resolve_target(x: float | None, y: float | None, text: str | None, occurrence: int,
                          exact: bool) -> tuple[float, float, list[screen.TextItem], str, screen.Prompt | None]:
    """Turn (x, y) or text into a tap point, the labels under it, and the prompt
    on screen (if any) so a prompt answer is judged with its question."""
    if text:
        s = await _read()
        hits = screen.find(s, text, exact=exact)
        if not hits:
            raise ValueError(f"no text matching {text!r} on screen.\n\n{screen.describe(s)}")
        if occurrence > len(hits):
            raise ValueError(f"only {len(hits)} match(es) for {text!r}: "
                             + "; ".join(f"({h.cx:.0f},{h.cy:.0f}) {h.text}" for h in hits))
        it = hits[occurrence - 1]
        return it.cx, it.cy, [it], f"'{it.text}' at ({it.cx:.0f},{it.cy:.0f})", screen.prompt(s)
    if x is None or y is None:
        raise ValueError("give x and y, or text")
    s = mirror.last or await _read()
    return x, y, screen.at(s, x, y), f"({x:.0f},{y:.0f})", screen.prompt(s)


def _guard(labels: list[screen.TextItem], p: screen.Prompt | None, confirm: bool) -> None:
    if confirm:
        return
    for it in labels:
        if (why := screen.needs_confirm(it, p)):
            raise Refused(f"Refused: the target looks like an irreversible, outward-facing or access-granting "
                          f"control ({why!r}). Ask the user in chat whether to proceed, then repeat the call "
                          "with confirm=True.")


def build() -> FastMCP:
    mcp = FastMCP(name="iphone", instructions=INSTRUCTIONS)

    # -- connection --------------------------------------------------------

    @mcp.tool
    @tool_errors
    async def phone_status() -> str:
        """Report whether the iPhone can be controlled right now, and why not if it can't.

        Covers iPhone Mirroring (running, connected, locked, in use), macOS
        permissions, and whether the phone is reachable for storage/app queries.
        """
        st = await _io(mirror.state)
        lines = [
            f"iPhone Mirroring running: {st.running}",
            f"Phone connected (mirroring live): {st.connected}",
            f"Accessibility permission: {st.raw.get('ax_trusted')}",
            f"Screen Recording permission: {st.screen_access}",
        ]
        if st.window:
            w, h = mirror.phone_size(st.window)
            lines.append(f"Phone screen: {w:.0f} x {h:.0f} pt")
        for m in st.messages:
            lines.append(f"Mirroring app says: {m}")
        try:
            devs = await device.devices()
            lines.append("Device layer: " + (", ".join(f"{d.serial} via {d.connection_type}" for d in devs)
                                             or "no phone on USB/Wi-Fi sync (storage tools unavailable)"))
        except device.DeviceUnavailable as e:
            lines.append(f"Device layer: {e}")
        p = st.problem()
        lines.append(f"\nReady: {'no. ' + p if p else 'yes'}")
        return "\n".join(lines)

    @mcp.tool
    @tool_errors
    async def phone_connect(wait_seconds: int = 20) -> str:
        """Launch iPhone Mirroring if needed and wait until the phone is live.

        Presses the app's own 'Try Again' / 'Connect' buttons when shown. It cannot
        get past Touch ID or a phone that is in the user's hand; it reports those.
        """
        st = await _io(mirror.state)
        if not st.running:
            await _io(mirror.launch)
        deadline = time.time() + max(1, wait_seconds)
        pressed: list[str] = []
        while True:
            st = await _io(mirror.state)
            if st.connected:
                break
            for label in ("Try Again", "Connect", "Continue", "Get Started"):
                if label in st.buttons and label not in pressed:
                    await _io(mirror.press_app_button, label)
                    pressed.append(label)
            if time.time() > deadline:
                break
            await asyncio.sleep(1.0)
        p = st.problem()
        if p:
            return f"Not ready: {p}" + (f"\n(pressed: {', '.join(pressed)})" if pressed else "")
        return "Connected.\n\n" + screen.describe(await _read())

    # -- seeing ------------------------------------------------------------

    @mcp.tool(output_schema=None)
    @tool_errors
    async def phone_screenshot(include_text: bool = True) -> list:
        """Screenshot of the phone. The image is scaled so 1 image pixel = 1 phone point,
        so pixel coordinates in it are tap coordinates. Also lists recognized text
        with tap points unless include_text is false."""
        s = await _io(mirror.capture, image=True, ocr=include_text)
        out: list = [_img(s)]
        if include_text:
            out.append(screen.describe(s))
        return out

    @mcp.tool
    @tool_errors
    async def phone_read_screen() -> str:
        """Text on the phone screen with a tap point for each item. Cheaper than a
        screenshot; use it to orient and to check what an action did."""
        return screen.describe(await _read())

    @mcp.tool(output_schema=None)
    @tool_errors
    async def phone_zoom(x: float, y: float, width: float, height: float) -> Image:
        """High-resolution crop of a region (phone points), for small text or icons."""
        if width <= 0 or height <= 0:
            raise ValueError("width and height must be positive")
        return Image(data=await _io(mirror.zoom, x, y, width, height), format="jpeg")

    @mcp.tool
    @tool_errors
    async def phone_find(text: str, scroll: bool = False, max_scrolls: int = 8,
                         direction: Literal["down", "up"] = "down") -> str:
        """Find text on screen. With scroll=true, keep scrolling the current view until
        it appears (or the view stops moving). Returns match tap points."""
        for i in range(max_scrolls + 1 if scroll else 1):
            s = await _read(settle=0.5 if i else 0)
            hits = screen.find(s, text)
            if hits:
                found = "; ".join(f"({h.cx:.0f},{h.cy:.0f}) {h.text}" for h in hits[:8])
                return f"Found {len(hits)} match(es) for {text!r}" + (f" after {i} scroll(s)" if i else "") + f": {found}"
            if not scroll or i == max_scrolls:
                break
            before = [it.text for it in s.items]
            await _scroll(direction, 0.6, None, None)
            after = await _read(settle=0.6)
            if [it.text for it in after.items] == before:
                return f"{text!r} not found; reached the end of the view.\n\n{screen.describe(after)}"
        return f"{text!r} not found.\n\n{screen.describe(mirror.last or await _read())}"

    # -- acting ------------------------------------------------------------
    #
    # Each action is a core that does the work and returns one line saying what
    # happened; the tools add the resulting screen, and phone_do chains cores.

    async def _tap(x: float | None = None, y: float | None = None, text: str | None = None,
                   occurrence: int = 1, exact: bool = False, double: bool = False, confirm: bool = False) -> str:
        tx, ty, labels, where, p = await _resolve_target(x, y, text, occurrence, exact)
        _guard(labels, p, confirm)
        await _io(mirror.tap, tx, ty, count=2 if double else 1)
        return f"Tapped {where}."

    async def _long_press(x: float | None = None, y: float | None = None, text: str | None = None,
                          seconds: float = 1.0, occurrence: int = 1, confirm: bool = False) -> str:
        tx, ty, labels, where, p = await _resolve_target(x, y, text, occurrence, False)
        _guard(labels, p, confirm)
        await _io(mirror.tap, tx, ty, hold_ms=max(0.3, seconds) * 1000)
        return f"Long-pressed {where} for {seconds:.1f}s."

    async def _swipe(direction: str | None = None, x: float | None = None, y: float | None = None,
                     to_x: float | None = None, to_y: float | None = None,
                     distance: float = 0.5, duration_ms: int = 300) -> str:
        st = await _io(mirror.require)
        w, h = mirror.phone_size(st.window)
        sx = w / 2 if x is None else x
        sy = h / 2 if y is None else y
        if to_x is not None and to_y is not None:
            ex, ey = to_x, to_y
        elif direction:
            dx = {"left": -1, "right": 1}.get(direction, 0) * distance * w
            dy = {"up": -1, "down": 1}.get(direction, 0) * distance * h
            ex, ey = sx + dx, sy + dy
        else:
            raise ValueError("give direction, or to_x and to_y")
        await _io(mirror.drag, sx, sy, ex, ey, ms=duration_ms)
        return f"Swiped ({sx:.0f},{sy:.0f}) → ({ex:.0f},{ey:.0f})."

    async def _scroll(direction: str = "down", amount: float = 0.5, x: float | None = None,
                      y: float | None = None) -> str:
        if direction not in ("down", "up", "left", "right"):
            raise ValueError("direction must be down, up, left or right")
        if not 0 < amount <= 0.9:
            raise ValueError("amount must be in (0, 0.9]")
        st = await _io(mirror.require)
        w, h = mirror.phone_size(st.window)
        cx = w / 2 if x is None else x
        cy = h / 2 if y is None else y
        dist = amount * h
        # A slow drag with a pause before release moves content by the drag
        # distance without a momentum fling, so "amount" means what it says.
        sign = {"down": -1, "up": 1, "right": -1, "left": 1}[direction]
        if direction in ("down", "up"):
            y1 = cy - sign * dist / 2
            await _io(mirror.drag, cx, y1, cx, y1 + sign * dist, ms=450, hold_ms=40)
        else:
            x1 = cx - sign * amount * w / 2
            await _io(mirror.drag, x1, cy, x1 + sign * amount * w, cy, ms=450, hold_ms=40)
        return f"Scrolled {direction} {amount:.2f} screen."

    async def _type(text: str, submit: bool = False) -> str:
        fallback = await _io(mirror.type_text, text)
        if submit:
            await _io(mirror.key, "return")
        note = f" (sent as unicode, verify: {''.join(fallback)!r})" if fallback else ""
        return f"Typed {len(text)} characters{note}{' and pressed Return' if submit else ''}."

    async def _key(key: str, modifiers: str = "") -> str:
        await _io(mirror.key, key, modifiers)
        return f"Pressed {modifiers + '+' if modifiers else ''}{key}."

    async def _go(where: str) -> str:
        if where == "back":
            st = await _io(mirror.require)
            w, h = mirror.phone_size(st.window)
            await _io(mirror.drag, 1, h * 0.45, w * 0.75, h * 0.45, ms=280)
        else:
            titles = {"home": "Home Screen", "app_switcher": "App Switcher", "spotlight": "Spotlight"}
            if where not in titles:
                raise ValueError("where must be home, app_switcher, spotlight or back")
            await _io(mirror.require)
            await _io(mirror.menu, titles[where])
        return f"Went {where}."

    async def _open_app(name: str) -> str:
        await _io(mirror.require)
        await _io(mirror.menu, "Home Screen")
        await asyncio.sleep(0.6)
        await _io(mirror.menu, "Spotlight")
        await asyncio.sleep(0.9)
        await _io(mirror.key, "a", "cmd")  # clear a previous query
        await _io(mirror.key, "delete")
        await _io(mirror.type_text, name)
        await asyncio.sleep(1.2)
        results = await _read()
        await _io(mirror.key, "return")
        s = await _read(settle=1.6)
        if any(it.text.lower().strip() == "search" for it in s.items) and screen.find(s, name):
            raise ValueError(f"Spotlight did not launch {name!r}; results were:\n\n{screen.describe(results)}\n\n"
                             "Tap the right result with phone_tap(text=...).")
        return f"Opened {name!r} (via Spotlight)."

    async def _open_url(url: str) -> str:
        u = _url(url)
        await _open_app("Safari")
        await asyncio.sleep(0.5)
        await _io(mirror.key, "l", "cmd")  # Safari: focus the address bar
        await asyncio.sleep(0.5)
        await _io(mirror.key, "a", "cmd")
        await _io(mirror.type_text, u)
        await _io(mirror.key, "return")
        await asyncio.sleep(1.5)
        return f"Opened {u} in Safari (a link to an app may ask to open it: answer with phone_alert)."

    async def _alert(button: str, confirm: bool = False) -> str:
        s = await _read()
        p = screen.prompt(s)
        if not p:
            raise ValueError("no alert, permission prompt or confirmation sheet detected.\n\n" + screen.describe(s))
        if button.strip().lower() == "dismiss":
            hit = next((b for b in p.buttons if screen.clean(b.text) in screen.DECLINE), None)
            if not hit:
                raise ValueError(f"this prompt has no cancel-style button; pick one.\n\n{p.describe()}")
        else:
            hit = screen.pick(p.buttons, button)
            if not hit:
                raise ValueError(f"no button {button!r} on the prompt.\n\n{p.describe()}")
        _guard([hit], p, confirm)
        await _io(mirror.tap, hit.cx, hit.cy)
        return f"Answered the prompt {('“' + p.title + '” ') if p.title else ''}with '{hit.text}'."

    async def _wait_for(text: str, timeout: float = 15) -> str:
        end = time.time() + min(timeout, 60)
        while True:
            s = await _read()
            hits = screen.find(s, text)
            if hits:
                h = hits[0]
                return f"{text!r} appeared at ({h.cx:.0f},{h.cy:.0f})."
            if time.time() > end:
                raise ValueError(f"{text!r} did not appear within {timeout:.0f}s.\n\n{screen.describe(s)}")
            await asyncio.sleep(0.7)

    async def _sleep(seconds: float) -> str:
        await asyncio.sleep(min(max(seconds, 0), 30))
        return f"Waited {seconds:.1f}s."

    @mcp.tool
    @tool_errors
    async def phone_tap(x: float | None = None, y: float | None = None, text: str | None = None,
                        occurrence: int = 1, exact: bool = False, double: bool = False,
                        confirm: bool = False) -> str:
        """Tap at (x, y) phone points, or on visible text (text=..., the Nth match top to
        bottom with occurrence=N). Returns the screen afterwards.

        Taps on delete/buy/send/sign-out style controls, and prompt answers that
        grant access or confirm a destructive question, are refused unless
        confirm=True, which you may only pass after the user said yes in chat
        (or their request asked for exactly that)."""
        return await _after(await _tap(x, y, text, occurrence, exact, double, confirm))

    @mcp.tool
    @tool_errors
    async def phone_long_press(x: float | None = None, y: float | None = None, text: str | None = None,
                               seconds: float = 1.0, occurrence: int = 1, confirm: bool = False) -> str:
        """Press and hold (context menus, home-screen edit mode, drag handles)."""
        return await _after(await _long_press(x, y, text, seconds, occurrence, confirm))

    @mcp.tool
    @tool_errors
    async def phone_swipe(direction: Literal["up", "down", "left", "right"] | None = None,
                          x: float | None = None, y: float | None = None,
                          to_x: float | None = None, to_y: float | None = None,
                          distance: float = 0.5, duration_ms: int = 300) -> str:
        """Drag a finger across the screen. direction is the way the finger moves
        (swipe 'left' pages a carousel forward). Starts at the screen center
        unless x/y given; distance is a fraction of the screen. Or give an exact
        path with x, y, to_x, to_y."""
        return await _after(await _swipe(direction, x, y, to_x, to_y, distance, duration_ms))

    @mcp.tool
    @tool_errors
    async def phone_scroll(direction: Literal["down", "up", "left", "right"] = "down", amount: float = 0.5,
                           x: float | None = None, y: float | None = None) -> str:
        """Scroll content. 'down' reveals what is below (like scrolling a web page down).
        amount is a fraction of the screen height (0.5 = half a screen). x/y pick
        which scrollable area, default the center."""
        return await _after(await _scroll(direction, amount, x, y))

    @mcp.tool
    @tool_errors
    async def phone_type(text: str, submit: bool = False) -> str:
        """Type into the focused field (tap the field first). submit=true presses Return.
        Never use this for passcodes or passwords."""
        return await _after(await _type(text, submit))

    @mcp.tool
    @tool_errors
    async def phone_key(key: str, modifiers: str = "") -> str:
        """Press a key: return, delete, escape, tab, space, up/down/left/right, or one
        character. modifiers like 'cmd' or 'cmd,shift' (cmd+a selects all in a field)."""
        return await _after(await _key(key, modifiers), settle=0.5)

    @mcp.tool
    @tool_errors
    async def phone_go(where: Literal["home", "app_switcher", "spotlight", "back"]) -> str:
        """System navigation: home screen, app switcher, Spotlight search, or 'back'
        (left-edge swipe, the iOS back gesture)."""
        return await _after(await _go(where))

    @mcp.tool
    @tool_errors
    async def phone_open_app(name: str) -> str:
        """Open an app by name via Spotlight (works for any installed app, including
        Settings and App Store). Returns the app's first screen."""
        return await _after(await _open_app(name), settle=0)

    @mcp.tool
    @tool_errors
    async def phone_open_url(url: str) -> str:
        """Open a web page or an app deep link (e.g. 'maps.apple.com/?q=coffee',
        'spotify:search:jazz') through Safari. Links that hand off to an app show an
        'Open in ...?' prompt: answer it with phone_alert(button='Open')."""
        return await _after(await _open_url(url), settle=0)

    @mcp.tool
    @tool_errors
    async def phone_alert(button: str | None = None, confirm: bool = False) -> str:
        """Read or answer the alert, permission prompt or confirmation sheet on screen.

        No button: describe the prompt (question and buttons). button='Allow Once',
        'OK', 'Delete', ...: tap that answer. button='dismiss': tap the safe way out
        (Cancel / Don't Allow / Not Now). Answers that grant access, accept terms,
        or say yes to a destructive question need confirm=True, which you may only
        pass after the user said yes in chat (or their request asked for exactly that)."""
        if button is None:
            s = await _read()
            p = screen.prompt(s)
            return p.describe() if p else ("No alert, permission prompt or confirmation sheet detected.\n\n"
                                           + screen.describe(s))
        return await _after(await _alert(button, confirm))

    @mcp.tool
    @tool_errors
    async def phone_wait(seconds: float = 1.0, for_text: str | None = None, timeout: float = 15) -> str:
        """Wait a fixed time, or until text appears (polling up to timeout seconds)."""
        if not for_text:
            await _sleep(seconds)
            return screen.describe(await _read())
        try:
            msg = await _wait_for(for_text, timeout)
        except ValueError as e:
            return str(e)
        return f"{msg}\n\n{screen.describe(mirror.last or await _read())}"

    steps_doc = {
        "open_app": "name", "open_url": "url", "tap": "text, or [x, y]", "long_press": "text, or [x, y]",
        "type": "text (+ submit: true)", "key": "key (+ modifiers)", "go": "home|back|app_switcher|spotlight",
        "scroll": "down|up|left|right (+ amount)", "swipe": "up|down|left|right (+ distance)",
        "alert": "button name or 'dismiss'", "wait_for": "text (+ timeout)", "wait": "seconds",
    }

    @mcp.tool
    @tool_errors
    async def phone_do(steps: list[dict[str, Any]]) -> str:
        """Run several actions in one call, stopping at the first one that fails or is
        refused. Each step is one action key plus options, for example:

          [{"open_app": "Settings"}, {"tap": "General"}, {"tap": "iPhone Storage"},
           {"wait_for": "Recommendations", "timeout": 20}]

        Actions: open_app, open_url, tap (text or [x, y]; occurrence, exact, double),
        long_press, type (submit), key (modifiers), go, scroll (amount), swipe
        (distance), alert (button or 'dismiss'), wait_for (timeout), wait (seconds).
        A step that needs the user's yes takes "confirm": true, under the same rule
        as phone_tap. Returns one line per step and the final screen."""
        if not steps:
            raise ValueError("give at least one step")
        done: list[str] = []
        await _io(mirror.hold_focus)
        try:
            for i, step in enumerate(steps, 1):
                acts = [k for k in step if k in steps_doc]
                if len(acts) != 1:
                    raise ValueError(f"step {i} needs exactly one of {', '.join(steps_doc)}: {step}")
                act, arg = acts[0], step[acts[0]]
                opt = {k: v for k, v in step.items() if k != act}
                try:
                    if act in ("tap", "long_press"):
                        where = {"text": arg} if isinstance(arg, str) else {"x": arg[0], "y": arg[1]}
                        fn = _tap if act == "tap" else _long_press
                        line = await fn(**where, **opt)
                    else:
                        fn = {"open_app": _open_app, "open_url": _open_url, "type": _type, "key": _key, "go": _go,
                              "scroll": _scroll, "swipe": _swipe, "alert": _alert, "wait_for": _wait_for,
                              "wait": _sleep}[act]
                        line = await fn(arg, **opt)
                except TypeError as e:
                    raise ValueError(f"step {i} ({act}) has a bad option: {e}") from None
                except (Refused, ValueError, BridgeError) as e:
                    done.append(f"{i}. {act} stopped: {e}")
                    return "\n".join(done) + "\n\nRemaining steps were not run."
                done.append(f"{i}. {line}")
                if act not in ("wait", "wait_for"):
                    await asyncio.sleep(SETTLE)
        finally:
            await _io(mirror.release_focus)
        return "\n".join(done) + "\n\n" + screen.describe(await _read())

    # -- device layer (USB / Wi-Fi lockdown) --------------------------------

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
        extra = {k: v for k, v in disk.items() if k.endswith("Usage") and isinstance(v, int) and v > 0}
        if extra:
            lines.append("Usage by category: " + ", ".join(f"{k[:-5]} {device.gb(n)}" for k, n in
                                                          sorted(extra.items(), key=lambda kv: -kv[1])))
        return "\n".join(lines)

    @mcp.tool
    @tool_errors
    async def device_storage(top: int = 25) -> str:
        """Where the storage went: free space plus installed apps ranked by size
        (app binary vs. documents & data). Read-only. Use it to pick what to
        offload or clean, then do the cleanup through Settings with the phone tools."""
        d = await device.info()
        disk = d["disk"]
        cap = disk.get("TotalDataCapacity") or disk.get("TotalDiskCapacity")
        free = disk.get("AmountDataAvailable") or disk.get("TotalDataAvailable")
        apps = sorted(await device.apps("User", sizes=True), key=lambda a: -a["total_bytes"])
        total_apps = sum(a["total_bytes"] for a in apps)
        lines = [f"Free: {device.gb(free)} of {device.gb(cap)}. {len(apps)} user apps use {device.gb(total_apps)}.",
                 "", "Largest apps (total = app + documents/data):"]
        for a in apps[:top]:
            lines.append(f"  {device.gb(a['total_bytes']):>8}  {a['name']}  (app {device.gb(a['app_bytes'])}, "
                         f"data {device.gb(a['data_bytes'])})  `{a['bundle_id']}`")
        lines.append("\nData-heavy apps can often be trimmed in-app (caches, downloads, chat media). "
                     "Offloading (Settings > General > iPhone Storage > app > Offload App) frees the app "
                     "size but keeps its data. Deleting is the user's call; confirm before tapping.")
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
        async def check() -> str:
            from fastmcp import Client
            async with Client(mcp) as c:
                r = await c.call_tool("phone_status", {})
                return r.content[0].text
        print(asyncio.run(check()))
        return
    log("starting (stdio)")
    mcp.run(show_banner=False)


if __name__ == "__main__":
    sys.exit(main())
