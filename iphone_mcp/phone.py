"""The phone as a library: everything the MCP tools, the `iphone` CLI and the
dashboard can do, in one place.

    from iphone_mcp.phone import Phone
    phone = Phone()
    await phone.connect()
    print(await phone.do([{"goto": "storage"}, {"wait_for": "Recommendations"}]))

Every action returns one line saying what happened; `after()` adds the screen
that followed. Actions are serialized (one finger at a time) and recorded in
the activity log the dashboard shows.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time
from collections import deque
from typing import Any

from . import device, screen
from .bridge import ROOT, BridgeError, Mirror, Screen, log
from .wda import Wda, WdaError

ACTIVITY_FILE = ROOT / "build" / "activity.jsonl"
RECORDINGS = ROOT / "build" / "recordings"
RECORD_PID = ROOT / "build" / "record.pid"

# Settings pages by plain name, as the rows to tap from the top of Settings.
# (iOS 26 ignores the path in App-prefs: links, so pages are reached by tapping;
# Settings is relaunched first so it starts at its top level.)
SETTINGS = {
    "settings": [],
    "general": ["General"],
    "storage": ["General", "iPhone Storage"],
    "about": ["General", "About"],
    "software update": ["General", "Software Update"],
    "background refresh": ["General", "Background App Refresh"],
    "vpn & device management": ["General", "VPN & Device Management"],
    "wifi": ["Wi-Fi"],
    "bluetooth": ["Bluetooth"],
    "cellular": ["Cellular"],
    "battery": ["Battery"],
    "notifications": ["Notifications"],
    "screen time": ["Screen Time"],
    "privacy": ["Privacy & Security"],
    "location": ["Privacy & Security", "Location Services"],
    "display": ["Display & Brightness"],
    "apps": ["Apps"],
    "photos settings": ["Apps", "Photos"],
    "messages settings": ["Apps", "Messages"],
    "icloud": ["Apple Account", "iCloud"],
}


# Controls that close an overlay, best first. Cookie banners get the
# privacy-preserving answer. Single-letter "X" closers come last.
CLOSERS = [r"continue to .+", r"skip( ad)?", r"close( ad)?", r"dismiss", r"no,? thanks?( you)?", r"not now",
           r"maybe later", r"later", r"got it", r"reject( all)?", r"(use )?necessary( cookies)? only", r"decline",
           r"[x×✕✖]"]
COUNTDOWN = re.compile(r"(skip|close|continue)[^0-9]{0,20}\d+\s*(s|sec|seconds?)\b|ad will (end|close)", re.IGNORECASE)


def _key_pattern(k: str) -> re.Pattern:
    """An on-screen keyboard key for k: 'q', 'add q', 'q absent', 'Q key', ..."""
    return re.compile(rf"(?:add |key |type |letter )?{re.escape(k)}(?:,? (?:absent|present|correct|key|button))?",
                      re.IGNORECASE)


class Refused(Exception):
    """A tap the safety rails stopped; the message tells the agent what to ask."""


class Phone:
    def __init__(self, engine: str | None = None) -> None:
        self.mirror = Mirror()
        self.wda = Wda()
        self.engine = engine or os.environ.get("IPHONE_ENGINE", "auto")  # auto | wda | mirror
        self._lock = asyncio.Lock()
        self._installed: dict[str, str] | None = None
        self.activity: deque[dict] = deque(maxlen=200)
        self.marks: list[screen.TextItem] = []  # from the last marked screenshot, for tap "#n"

    # -- plumbing ----------------------------------------------------------

    def eng(self) -> Mirror | Wda:
        """WebDriverAgent when it is up and the phone is unlocked, else iPhone Mirroring."""
        if self.engine == "mirror":
            return self.mirror
        if self.engine == "wda" or self.wda.ready():
            return self.wda
        return self.mirror

    @property
    def settle(self) -> float:
        """A fixed pause after an action, for engines that cannot watch the screen."""
        return 0.25 if isinstance(self.eng(), Wda) else 0.8

    async def settle_screen(self) -> None:
        """After an action: under WebDriverAgent wait until the live frames stop
        changing (0.1-1.2 s), otherwise a fixed pause."""
        e = self.eng()
        if isinstance(e, Wda):
            await asyncio.to_thread(e.settle)
        else:
            await asyncio.sleep(self.settle)

    async def io(self, fn, *a, **kw):
        """Engine calls block (subprocesses, HTTP); keep them off the event loop
        and one at a time (two agents racing one finger ends badly)."""
        async with self._lock:
            return await asyncio.to_thread(fn, *a, **kw)

    def record(self, action: str, ok: bool = True) -> None:
        ev = {"t": time.time(), "action": action, "ok": ok}
        self.activity.append(ev)
        try:
            ACTIVITY_FILE.parent.mkdir(exist_ok=True)
            with ACTIVITY_FILE.open("a") as f:
                f.write(json.dumps(ev) + "\n")
        except OSError:
            pass

    async def read(self, image: bool = False, settle: float = 0.0) -> Screen:
        if settle:
            await asyncio.sleep(settle)
        return await self.io(self.eng().capture, image=image, ocr=True)

    async def after(self, action: str, settle: float | None = None) -> str:
        if settle is None:
            await self.settle_screen()
        elif settle:
            await asyncio.sleep(settle)
        s = await self.read()
        return f"{action}\n\n{screen.describe(s)}"

    async def fresh(self, max_age: float = 1.5) -> Screen:
        """The last screen read if nothing acted since and it is recent, else a new read."""
        s = self.eng().last
        if s is not None and time.time() - s.taken_at < max_age:
            return s
        return await self.read()

    async def installed_bundle(self, name: str) -> str | None:
        """Bundle id of an installed app by its name (lockdown, when the phone is paired)."""
        if self._installed is None:
            try:
                self._installed = {a["name"].lower(): a["bundle_id"] for a in await device.apps("User", sizes=False)}
            except Exception as e:  # not on USB / Wi-Fi sync: Spotlight will do
                log("app list unavailable:", repr(e))
                self._installed = {}
        return self._installed.get(name.strip().lower())

    @staticmethod
    def url(u: str) -> str:
        u = u.strip()
        return u if re.match(r"^[a-zA-Z][\w+.-]*:", u) else "https://" + u

    async def resolve(self, x: float | None, y: float | None, text: str | None, occurrence: int, exact: bool
                      ) -> tuple[float, float, list[screen.TextItem], str, screen.Prompt | None]:
        """Turn (x, y) or text into a tap point, the labels under it, and the prompt
        on screen (if any) so a prompt answer is judged with its question."""
        if text and re.fullmatch(r"#\d+", text.strip()):
            n = int(text.strip()[1:])
            if not 1 <= n <= len(self.marks):
                raise ValueError(f"no mark {text}; take phone_screenshot(marks=true) first "
                                 f"({len(self.marks)} marks now)")
            it = self.marks[n - 1]
            return it.cx, it.cy, [it], f"#{n} '{it.text}' at ({it.cx:.0f},{it.cy:.0f})", None
        if text:
            s = await self.fresh()
            hits = screen.find(s, text, exact=exact)
            if not hits and s is self.eng().last:
                s = await self.read()  # the cached screen may be stale; look again before failing
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
        s = self.eng().last or await self.read()
        return x, y, screen.at(s, x, y), f"({x:.0f},{y:.0f})", screen.prompt(s)

    @staticmethod
    def guard(labels: list[screen.TextItem], p: screen.Prompt | None, confirm: bool) -> None:
        if confirm:
            return
        for it in labels:
            if (why := screen.needs_confirm(it, p)):
                raise Refused(f"Refused: the target looks like an irreversible, outward-facing or access-granting "
                              f"control ({why!r}). Ask the user in chat whether to proceed, then repeat the call "
                              "with confirm=True.")

    # -- connection --------------------------------------------------------

    async def status(self) -> dict[str, Any]:
        w = await asyncio.to_thread(self.wda.status) if self.engine != "mirror" else {"configured": False}
        st = await self.io(self.mirror.state)
        out: dict[str, Any] = {
            "wda": w, "mirroring": {"running": st.running, "connected": st.connected,
                                    "messages": st.messages, "problem": st.problem()},
            "permissions": {"accessibility": st.raw.get("ax_trusted"), "screen_recording": st.screen_access},
        }
        if w.get("reachable") and not w.get("locked"):
            out["ready"], out["engine"] = True, "wda"
        else:
            out["ready"], out["engine"] = st.problem() is None, "mirror"
        if out["ready"]:
            out["size"] = await self.io(self.eng().phone_size)
        try:
            out["usb"] = [f"{d.serial} via {d.connection_type}" for d in await device.devices()]
        except device.DeviceUnavailable as e:
            out["usb"] = str(e)
        return out

    async def status_text(self) -> str:
        s = await self.status()
        w = s["wda"]
        if not w.get("configured"):
            wline = "not started (phone_connect starts it when the phone is plugged in or on Wi-Fi)"
        elif not w.get("reachable"):
            wline = f"not reachable at {w.get('url')}: {w.get('error')}"
        else:
            wline = (f"up at {w['url']}, iOS {w.get('ios')}, phone {'LOCKED' if w.get('locked') else 'unlocked'}, "
                     f"front app {w.get('app')}; live view with phone_watch")
        m = s["mirroring"]
        lines = [f"WebDriverAgent: {wline}", f"iPhone Mirroring running: {m['running']}",
                 f"Phone connected (mirroring live): {m['connected']}",
                 f"Accessibility permission: {s['permissions']['accessibility']}",
                 f"Screen Recording permission: {s['permissions']['screen_recording']}"]
        if s.get("size"):
            lines.append(f"Phone screen: {s['size'][0]:.0f} x {s['size'][1]:.0f} pt")
        lines += [f"Mirroring app says: {x}" for x in m["messages"]]
        usb = s["usb"]
        lines.append("Device layer: " + (", ".join(usb) if isinstance(usb, list) and usb else
                                         usb if isinstance(usb, str) else
                                         "no phone on USB/Wi-Fi sync (storage tools unavailable)"))
        if s["engine"] == "wda":
            lines.append("\nReady: yes, through WebDriverAgent (the phone can stay in the user's hand)")
        else:
            lines.append(f"\nReady: {'no. ' + m['problem'] if m['problem'] else 'yes, through iPhone Mirroring'}")
        return "\n".join(lines)

    async def connect(self, engine: str = "auto", wait_seconds: int = 20) -> str:
        notes: list[str] = []
        if engine != "mirror" and self.engine != "mirror":
            if not await asyncio.to_thread(self.wda.ready):
                st = await asyncio.to_thread(self.wda.status)
                if st.get("reachable") and st.get("locked"):
                    notes.append("WebDriverAgent is up but the phone is locked (unlock it with Face ID to use it).")
                else:
                    try:
                        await asyncio.to_thread(self.wda.start, max(45, wait_seconds))
                    except BridgeError as e:
                        notes.append(f"WebDriverAgent: {e}")
            if await asyncio.to_thread(self.wda.ready):
                self.record("Connected through WebDriverAgent")
                return ("Connected through WebDriverAgent (the phone can stay in your hand).\n\n"
                        + screen.describe(await self.read()))
            if engine == "wda":
                st = await asyncio.to_thread(self.wda.status)
                if st.get("reachable") and st.get("locked"):
                    return "Not ready: the phone is locked. Ask the user to unlock it (Face ID); never type the passcode."
                return "Not ready: " + " ".join(notes)
        st = await self.io(self.mirror.state)
        if not st.running:
            await self.io(self.mirror.launch)
        deadline = time.time() + max(1, wait_seconds)
        pressed: list[str] = []
        while True:
            st = await self.io(self.mirror.state)
            if st.connected:
                break
            for label in ("Try Again", "Connect", "Continue", "Get Started"):
                if label in st.buttons and label not in pressed:
                    await self.io(self.mirror.press_app_button, label)
                    pressed.append(label)
            if time.time() > deadline:
                break
            await asyncio.sleep(1.0)
        p = st.problem()
        extra = ("\n" + "\n".join(notes)) if notes else ""
        if p:
            return f"Not ready: {p}" + (f"\n(pressed: {', '.join(pressed)})" if pressed else "") + extra
        self.record("Connected through iPhone Mirroring")
        return "Connected through iPhone Mirroring." + extra + "\n\n" + screen.describe(await self.read())

    async def disconnect(self) -> bool:
        stopped = await asyncio.to_thread(self.wda.stop)
        if stopped:
            self.record("Stopped WebDriverAgent")
        return stopped

    # -- acting ------------------------------------------------------------

    async def tap(self, x: float | None = None, y: float | None = None, text: str | None = None,
                  occurrence: int = 1, exact: bool = False, double: bool = False, confirm: bool = False) -> str:
        tx, ty, labels, where, p = await self.resolve(x, y, text, occurrence, exact)
        self.guard(labels, p, confirm)
        await self.io(self.eng().tap, tx, ty, count=2 if double else 1)
        return self._did(f"Tapped {where}.")

    async def long_press(self, x: float | None = None, y: float | None = None, text: str | None = None,
                         seconds: float = 1.0, occurrence: int = 1, confirm: bool = False) -> str:
        tx, ty, labels, where, p = await self.resolve(x, y, text, occurrence, False)
        self.guard(labels, p, confirm)
        await self.io(self.eng().tap, tx, ty, hold_ms=max(0.3, seconds) * 1000)
        return self._did(f"Long-pressed {where} for {seconds:.1f}s.")

    async def swipe(self, direction: str | None = None, x: float | None = None, y: float | None = None,
                    to_x: float | None = None, to_y: float | None = None,
                    distance: float = 0.5, duration_ms: int = 300) -> str:
        await self.io(self.eng().require)
        w, h = await self.io(self.eng().phone_size)
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
        await self.io(self.eng().drag, sx, sy, ex, ey, ms=duration_ms)
        return self._did(f"Swiped ({sx:.0f},{sy:.0f}) → ({ex:.0f},{ey:.0f}).")

    async def scroll(self, direction: str = "down", amount: float = 0.5, x: float | None = None,
                     y: float | None = None) -> str:
        if direction not in ("down", "up", "left", "right"):
            raise ValueError("direction must be down, up, left or right")
        if not 0 < amount <= 0.9:
            raise ValueError("amount must be in (0, 0.9]")
        await self.io(self.eng().require)
        w, h = await self.io(self.eng().phone_size)
        cx = w / 2 if x is None else x
        cy = h / 2 if y is None else y
        # A slow drag with a pause before release moves content by the drag
        # distance without a momentum fling, so "amount" means what it says.
        # Drags stay inside the middle of the screen: one that starts at the top
        # edge pulls down Notification Center, at the bottom it goes home. Longer
        # scrolls are split into several drags.
        sign = {"down": -1, "up": 1, "right": -1, "left": 1}[direction]
        vertical = direction in ("down", "up")
        size, center = (h, cy) if vertical else (w, cx)
        lo, hi = (0.12 * size, 0.88 * size) if vertical else (0.08 * size, 0.92 * size)
        left = amount * size
        while left > 1:
            d = min(left, hi - lo)
            left -= d
            a = min(center + d / 2, hi) if sign < 0 else max(center - d / 2, lo)
            b = a + sign * d
            if vertical:
                await self.io(self.eng().drag, cx, a, cx, b, ms=450, hold_ms=40)
            else:
                await self.io(self.eng().drag, a, cy, b, cy, ms=450, hold_ms=40)
        return self._did(f"Scrolled {direction} {amount:.2f} screen.")

    async def type(self, text: str, submit: bool = False) -> str:
        fallback = await self.io(self.eng().type_text, text)
        if submit:
            await self.io(self.eng().key, "return")
        note = f" (sent as unicode, verify: {''.join(fallback)!r})" if fallback else ""
        return self._did(f"Typed {len(text)} characters{note}{' and pressed Return' if submit else ''}.")

    async def key(self, key: str, modifiers: str = "") -> str:
        await self.io(self.eng().key, key, modifiers)
        return self._did(f"Pressed {modifiers + '+' if modifiers else ''}{key}.")

    async def go(self, where: str) -> str:
        if where == "back":
            await self.io(self.eng().require)
            w, h = await self.io(self.eng().phone_size)
            await self.io(self.eng().drag, 1, h * 0.45, w * 0.75, h * 0.45, ms=280)
        else:
            titles = {"home": "Home Screen", "app_switcher": "App Switcher", "spotlight": "Spotlight"}
            if where not in titles:
                raise ValueError("where must be home, app_switcher, spotlight or back")
            await self.io(self.eng().require)
            await self.io(self.eng().menu, titles[where])
        return self._did(f"Went {where}.")

    async def open_app(self, name: str) -> str:
        e = self.eng()
        await self.io(e.require)
        if isinstance(e, Wda):
            bid = e.bundle_for(name) or await self.installed_bundle(name)
            if bid:
                try:
                    await self.io(e.launch, bid)
                    await asyncio.sleep(0.6)
                    return self._did(f"Opened {name!r} (`{bid}`).")
                except WdaError as err:
                    log(f"launch {bid} failed, trying Spotlight:", err)
            await self.io(e.menu, "Spotlight")
            await asyncio.sleep(0.9)
        else:
            await self.io(e.menu, "Home Screen")
            await asyncio.sleep(0.6)
            await self.io(e.menu, "Spotlight")
            await asyncio.sleep(0.9)
            await self.io(e.key, "a", "cmd")  # clear a previous query
            await self.io(e.key, "delete")
        await self.io(e.type_text, name)
        await asyncio.sleep(1.2)
        results = await self.read()
        await self.io(e.key, "return")
        s = await self.read(settle=1.6)
        if any(it.text.lower().strip() == "search" for it in s.items) and screen.find(s, name):
            raise ValueError(f"Spotlight did not launch {name!r}; results were:\n\n{screen.describe(results)}\n\n"
                             "Tap the right result with phone_tap(text=...).")
        return self._did(f"Opened {name!r} (via Spotlight).")

    async def open_url(self, url: str) -> str:
        u = self.url(url)
        e = self.eng()
        if isinstance(e, Wda):
            await self.io(e.require)
            await self.io(e.open_url, u)
            await asyncio.sleep(1.0)
            return self._did(f"Opened {u}.")
        await self.open_app("Safari")
        await asyncio.sleep(0.5)
        await self.io(self.eng().key, "l", "cmd")  # Safari: focus the address bar
        await asyncio.sleep(0.5)
        await self.io(self.eng().key, "a", "cmd")
        await self.io(self.eng().type_text, u)
        await self.io(self.eng().key, "return")
        await asyncio.sleep(1.5)
        return self._did(f"Opened {u} in Safari (a link to an app may ask to open it: answer with phone_alert).")

    async def goto(self, place: str) -> str:
        """Jump to a Settings page by name ('storage', 'battery', 'wifi', ...)."""
        key = place.strip().lower()
        if key not in SETTINGS:
            raise ValueError(f"unknown place {place!r}; known: {', '.join(sorted(SETTINGS))}")
        e = self.eng()
        await self.io(e.require)
        if isinstance(e, Wda):
            await self.io(e.terminate, "com.apple.Preferences")  # reopen at the top, not the last page
            await self.io(e.launch, "com.apple.Preferences")
            await asyncio.sleep(0.6)
        else:
            await self.open_app("Settings")
        for label in SETTINGS[key]:
            s = await self.read()
            if not screen.find(s, label):
                await self.find(label, scroll=True)
            await self.tap(text=label)
            await asyncio.sleep(self.settle + 0.3)
        return self._did(f"Went to Settings > {place}.")

    async def alert(self, button: str, confirm: bool = False) -> str:
        s = await self.read()
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
        self.guard([hit], p, confirm)
        if hit.kind == "alert":  # a system alert read through WebDriverAgent's alert API
            await self.io(self.eng().answer_alert, hit.text)
        else:
            await self.io(self.eng().tap, hit.cx, hit.cy)
        return self._did(f"Answered the prompt {('“' + p.title + '” ') if p.title else ''}with '{hit.text}'.")

    async def wait_for(self, text: str, timeout: float = 15) -> str:
        end = time.time() + min(timeout, 60)
        while True:
            s = await self.read()
            hits = screen.find(s, text)
            if hits:
                h = hits[0]
                return f"{text!r} appeared at ({h.cx:.0f},{h.cy:.0f})."
            if time.time() > end:
                raise ValueError(f"{text!r} did not appear within {timeout:.0f}s.\n\n{screen.describe(s)}")
            await asyncio.sleep(0.5)

    async def sleep(self, seconds: float) -> str:
        await asyncio.sleep(min(max(seconds, 0), 30))
        return f"Waited {seconds:.1f}s."

    async def find(self, text: str, scroll: bool = False, max_scrolls: int = 8, direction: str = "down") -> str:
        for i in range(max_scrolls + 1 if scroll else 1):
            s = await self.read(settle=0.3 if i else 0)
            hits = screen.find(s, text)
            if hits:
                found = "; ".join(f"({h.cx:.0f},{h.cy:.0f}) {h.text}" for h in hits[:8])
                return (f"Found {len(hits)} match(es) for {text!r}" + (f" after {i} scroll(s)" if i else "")
                        + f": {found}")
            if not scroll or i == max_scrolls:
                break
            before = [it.text for it in s.items]
            await self.scroll(direction, 0.6)
            after = await self.read(settle=0.3)
            if [it.text for it in after.items] == before:
                raise ValueError(f"{text!r} not found; reached the end of the view.\n\n{screen.describe(after)}")
        raise ValueError(f"{text!r} not found.\n\n{screen.describe(self.eng().last or await self.read())}")

    async def taps(self, targets: list, confirm: bool = False) -> str:
        """Tap several labels (or [x, y] points) in order, resolved from one screen
        read, without reading in between: menus, pickers, a row of buttons."""
        def resolve(s: Screen) -> list[tuple[float, float, str]]:
            p = screen.prompt(s)
            out = []
            for t in targets:
                if isinstance(t, (list, tuple)):
                    out.append((float(t[0]), float(t[1]), f"({t[0]},{t[1]})"))
                    continue
                hits = screen.find(s, str(t))
                if not hits:
                    raise ValueError(f"no text matching {t!r} on screen.\n\n{screen.describe(s)}")
                self.guard([hits[0]], p, confirm)
                out.append((hits[0].cx, hits[0].cy, hits[0].text))
            return out

        points = await self._from_screen(resolve)
        await self._tap_points([(x, y) for x, y, _ in points])
        return self._did("Tapped " + ", ".join(f"'{lbl}'" for _, _, lbl in points) + ".")

    async def _tap_points(self, points: list[tuple[float, float]]) -> None:
        """Tap in order, without a screen read between taps."""
        e = self.eng()
        if isinstance(e, Wda) and len(points) > 1:
            await self.io(e.tap_many, points)
        else:
            for x, y in points:
                await self.io(e.tap, x, y)

    async def _from_screen(self, resolve):
        """resolve(screen) on the cached read; if it fails there, once more on a new read
        (the page may have changed by itself since)."""
        s = await self.fresh()
        try:
            return resolve(s)
        except ValueError:
            if s is not self.eng().last:
                raise
            return resolve(await self.read())

    async def keys(self, keys: str | list[str]) -> str:
        """Type on an on-screen keyboard drawn by the app or page (games like Wordle,
        PIN-less keypads): "crane" taps c, r, a, n, e; a list may add named keys like
        "enter" or "backspace". One screen read, then every tap back to back."""
        seq = list(keys) if isinstance(keys, str) else keys

        def resolve(s: Screen) -> list[tuple[float, float]]:
            cands = [it for it in s.items if len(it.text) <= 24]
            out = []
            for k in seq:
                pat = _key_pattern(k) if len(k) == 1 else re.compile(rf"{re.escape(k)}( key)?", re.IGNORECASE)
                hit = next((it for it in cands if pat.fullmatch(it.text.strip())), None)
                if not hit:
                    raise ValueError(f"no on-screen key for {k!r}.\n\n{screen.describe(s)}")
                out.append((hit.cx, hit.cy))
            return out

        points = await self._from_screen(resolve)
        await self._tap_points(points)
        return self._did(f"Pressed {''.join(k if len(k) == 1 else f' [{k}] ' for k in seq).strip()} on the on-screen keys.")

    async def dismiss(self, rounds: int = 4, wait_ads: float = 12) -> str:
        """Clear whatever is in the way: system prompts (safe answer), ads ("Continue",
        "Skip", waiting out countdowns), close buttons and X's, cookie banners (Reject).
        Several layers in one call."""
        done: list[str] = []
        waited = 0.0
        e = self.eng()
        for _ in range(rounds + int(wait_ads)):
            s = await self.read()
            p = screen.prompt(s)
            if p and any(screen.clean(b.text) in screen.DECLINE for b in p.buttons):
                done.append(await self.alert("dismiss"))
                await asyncio.sleep(self.settle)
                continue
            hit = None
            for pat in CLOSERS:
                rx = re.compile(pat, re.IGNORECASE)
                hit = next((it for it in s.items if rx.fullmatch(screen.clean(it.text))
                            and not screen.needs_confirm(it, p)), None)
                if hit:
                    break
            if hit is None and isinstance(e, Wda):  # an X drawn without a label: read the pixels
                ocr = await asyncio.to_thread(e._ocr, await self.io(e._png), True, 5)
                hit = next((it for it in ocr if it.text.strip() in ("X", "×", "✕", "✖") and it.cy < 0.4 * s.height), None)
            if hit is not None:
                await self.io(e.tap, hit.cx, hit.cy)
                done.append(self._did(f"Closed '{hit.text}' at ({hit.cx:.0f},{hit.cy:.0f})."))
                await self.settle_screen()
                if len(done) >= rounds:
                    break
                continue
            if waited < wait_ads and any(COUNTDOWN.search(it.text) for it in s.items):
                await asyncio.sleep(1.0)
                waited += 1.0
                continue
            break
        return " ".join(done) if done else "Nothing to dismiss."

    def _did(self, line: str) -> str:
        self.record(line)
        return line

    STEPS = {
        "open_app": "name", "open_url": "url", "goto": "settings page", "tap": "text, or [x, y]",
        "long_press": "text, or [x, y]", "type": "text (+ submit: true)", "key": "key (+ modifiers)",
        "go": "home|back|app_switcher|spotlight", "scroll": "down|up|left|right (+ amount)",
        "swipe": "up|down|left|right (+ distance)", "alert": "button name or 'dismiss'",
        "find": "text (+ scroll: true)", "wait_for": "text (+ timeout)", "wait": "seconds",
        "taps": "[label or [x, y], ...] from one read", "keys": "on-screen keyboard: 'crane' or ['c', 'enter']",
        "dismiss": "close ads, popups, X's, cookie banners (true)",
    }

    async def do(self, steps: list[dict[str, Any]]) -> str:
        """Run steps in order; stop at the first that fails or is refused. Returns
        one line per step and the final screen."""
        if not steps:
            raise ValueError("give at least one step")
        done: list[str] = []
        active = self.eng()
        await self.io(active.hold_focus)
        try:
            for i, step in enumerate(steps, 1):
                acts = [k for k in step if k in self.STEPS]
                if len(acts) != 1:
                    raise ValueError(f"step {i} needs exactly one of {', '.join(self.STEPS)}: {step}")
                act, arg = acts[0], step[acts[0]]
                opt = {k: v for k, v in step.items() if k != act}
                try:
                    if act in ("tap", "long_press"):
                        where = {"text": arg} if isinstance(arg, str) else {"x": arg[0], "y": arg[1]}
                        line = await getattr(self, act)(**where, **opt)
                    elif act == "dismiss":
                        line = await self.dismiss(**opt)
                    else:
                        fn = {"wait": self.sleep}.get(act) or getattr(self, act)
                        line = await fn(arg, **opt)
                except TypeError as e:
                    raise ValueError(f"step {i} ({act}) has a bad option: {e}") from None
                except (Refused, ValueError, BridgeError) as e:
                    done.append(f"{i}. {act} stopped: {e}")
                    self.record(f"{act} stopped: {str(e).splitlines()[0]}", ok=False)
                    return "\n".join(done) + "\n\nRemaining steps were not run."
                done.append(f"{i}. {line}")
                if act not in ("wait", "wait_for", "find", "dismiss"):
                    await self.settle_screen()
        finally:
            await self.io(active.release_focus)
        return "\n".join(done) + "\n\n" + screen.describe(await self.read())

    # -- seeing -------------------------------------------------------------

    async def marked(self) -> tuple[bytes, str]:
        """Screenshot with a numbered box on every element (set-of-marks), plus the
        legend. Tap one with tap(text="#n")."""
        s = await self.io(self.eng().capture, image=True, ocr=True)
        assert s.jpg is not None
        self.marks = list(s.items)
        from PIL import Image, ImageDraw
        import io as _io
        im = Image.open(_io.BytesIO(s.jpg)).convert("RGB")
        k = im.width / s.width
        d = ImageDraw.Draw(im)
        for n, it in enumerate(self.marks, 1):
            box = (it.x * k, it.y * k, (it.x + it.w) * k, (it.y + it.h) * k)
            d.rectangle(box, outline=(255, 45, 85), width=2)
            tag = str(n)
            tw = 7 * len(tag) + 4
            d.rectangle((box[0], box[1], box[0] + tw, box[1] + 13), fill=(255, 45, 85))
            d.text((box[0] + 2, box[1]), tag, fill=(255, 255, 255))
        out = _io.BytesIO()
        im.save(out, "JPEG", quality=80)
        legend = "\n".join(f"#{n} {screen._tag(it)}{it.text}" for n, it in enumerate(self.marks, 1))
        return out.getvalue(), legend

    # -- recording ---------------------------------------------------------

    def record_start(self, name: str | None = None) -> str:
        """Record the phone's live screen (WebDriverAgent's stream) to an MP4 with
        ffmpeg, in the background until record_stop()."""
        import shutil
        if self.recording():
            raise ValueError("already recording; stop it first")
        src = self.wda.stream_url
        if not src or not self.wda.ready():
            raise ValueError("recording needs WebDriverAgent running (phone_connect)")
        ff = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
        if not os.path.exists(ff):
            raise ValueError("ffmpeg is not installed (brew install ffmpeg)")
        RECORDINGS.mkdir(parents=True, exist_ok=True)
        path = RECORDINGS / f"{name or time.strftime('%Y-%m-%d-%H%M%S')}.mp4"
        proc = subprocess.Popen([ff, "-y", "-loglevel", "error", "-f", "mpjpeg", "-i", src, "-vf",
                                 "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-r", "24", "-c:v", "libx264", "-preset",
                                 "veryfast", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(path)],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
        RECORD_PID.write_text(f"{proc.pid}\n{path}")
        self.record(f"Started recording {path.name}")
        return str(path)

    def recording(self) -> str | None:
        if not RECORD_PID.exists():
            return None
        pid, path = RECORD_PID.read_text().split("\n", 1)
        try:
            os.kill(int(pid), 0)
        except (OSError, ValueError):
            RECORD_PID.unlink(missing_ok=True)
            return None
        return path

    def record_stop(self) -> str:
        path = self.recording()
        if not path:
            raise ValueError("not recording")
        pid = int(RECORD_PID.read_text().split("\n", 1)[0])
        import signal
        os.kill(pid, signal.SIGINT)  # ffmpeg finishes the file on SIGINT
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except OSError:
                break
            time.sleep(0.1)
        RECORD_PID.unlink(missing_ok=True)
        self.record(f"Saved recording {os.path.basename(path)}")
        return path

    # -- watching ----------------------------------------------------------

    async def watch(self, open_on_mac: bool = True) -> str:
        """Open the iPhone Live dashboard (live screen, status, activity)."""
        from . import dashboard
        url = await asyncio.to_thread(dashboard.ensure_running)
        if open_on_mac:
            await asyncio.to_thread(subprocess.run, ["open", url], check=False)
        return url

    # -- storage -----------------------------------------------------------

    async def storage_report(self, deep: bool = False) -> dict[str, Any]:
        """Where the space went, without touching the screen: capacity, apps by app
        size vs. documents & data, and with deep=True a du of the media partition
        (photos, videos, Live Photo motion, screenshots, Photos' own caches)."""
        from . import storage
        return await storage.report(deep=deep)
