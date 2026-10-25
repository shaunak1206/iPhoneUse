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

# Settings pages by plain name. WebDriverAgent opens these as URLs, so one call
# replaces three or four taps; under Mirroring they are reached by tapping.
SETTINGS = {
    "settings": ("App-prefs:", []),
    "general": ("App-prefs:General", ["General"]),
    "storage": ("App-prefs:General&path=STORAGE_MGMT", ["General", "iPhone Storage"]),
    "about": ("App-prefs:General&path=About", ["General", "About"]),
    "software update": ("App-prefs:General&path=SOFTWARE_UPDATE_LINK", ["General", "Software Update"]),
    "background refresh": ("App-prefs:General&path=AUTO_CONTENT_DOWNLOAD", ["General", "Background App Refresh"]),
    "vpn & device management": ("App-prefs:General&path=ManagedConfigurationList",
                                ["General", "VPN & Device Management"]),
    "wifi": ("App-prefs:WIFI", ["Wi-Fi"]),
    "bluetooth": ("App-prefs:Bluetooth", ["Bluetooth"]),
    "cellular": ("App-prefs:MOBILE_DATA_SETTINGS_ID", ["Cellular"]),
    "battery": ("App-prefs:BATTERY_USAGE", ["Battery"]),
    "notifications": ("App-prefs:NOTIFICATIONS_ID", ["Notifications"]),
    "screen time": ("App-prefs:SCREEN_TIME", ["Screen Time"]),
    "privacy": ("App-prefs:Privacy", ["Privacy & Security"]),
    "location": ("App-prefs:Privacy&path=LOCATION", ["Privacy & Security", "Location Services"]),
    "display": ("App-prefs:DISPLAY", ["Display & Brightness"]),
    "photos settings": ("App-prefs:Photos", ["Apps", "Photos"]),
    "messages settings": ("App-prefs:MESSAGES", ["Apps", "Messages"]),
    "icloud": ("App-prefs:CASTLE", []),
}


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
        # WebDriverAgent already waits for the app to go idle after each action.
        return 0.25 if isinstance(self.eng(), Wda) else 0.8

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
        s = await self.read(settle=self.settle if settle is None else settle)
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
        link, taps = SETTINGS[key]
        e = self.eng()
        if isinstance(e, Wda):
            try:
                await self.io(e.require)
                await self.io(e.open_url, link)
                await asyncio.sleep(0.8)
                return self._did(f"Went to Settings > {place} (`{link}`).")
            except WdaError as err:
                log(f"settings link {link} refused, tapping instead:", err)
        await self.open_app("Settings")
        for label in taps:
            await self.find(label, scroll=True)
            await self.tap(text=label, exact=True)
            await asyncio.sleep(self.settle)
        return self._did(f"Went to Settings > {place} (by tapping).")

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

    def _did(self, line: str) -> str:
        self.record(line)
        return line

    STEPS = {
        "open_app": "name", "open_url": "url", "goto": "settings page", "tap": "text, or [x, y]",
        "long_press": "text, or [x, y]", "type": "text (+ submit: true)", "key": "key (+ modifiers)",
        "go": "home|back|app_switcher|spotlight", "scroll": "down|up|left|right (+ amount)",
        "swipe": "up|down|left|right (+ distance)", "alert": "button name or 'dismiss'",
        "find": "text (+ scroll: true)", "wait_for": "text (+ timeout)", "wait": "seconds",
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
                if act not in ("wait", "wait_for", "find"):
                    await asyncio.sleep(self.settle)
        finally:
            await self.io(active.release_focus)
        return "\n".join(done) + "\n\n" + screen.describe(await self.read())

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
