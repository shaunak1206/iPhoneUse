"""WebDriverAgent engine: drives the phone from the inside through Apple's UI
testing framework. The WebDriverAgentRunner app on the phone serves HTTP on
port 8100 while scripts/wda_start.sh keeps it running from this Mac.

Unlike Mirroring, the phone can be unlocked and in the user's hand, screens are
read from the real accessibility tree (so icon-only buttons have names), and
apps open by bundle id. Same interface as bridge.Mirror, so the server's tools
don't care which engine is driving.
"""

from __future__ import annotations

import io
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from . import screen as scr
from .bridge import BridgeError, Screen, TextItem, log
from .bridge import run as mirrorctl

ROOT = Path(__file__).resolve().parent.parent
URL_FILE = ROOT / "build" / "wda.url"
LAST_FILE = ROOT / "build" / "wda.last"  # touched on every call; wda_start.sh stops the runner when it goes stale
LOG_FILE = ROOT / "build" / "wda.log"
START = ROOT / "scripts" / "wda_start.sh"
SERVER_URL = re.compile(r"ServerURLHere->(http://[^<\s]+)<-ServerURLHere")
RUNNER = "xcodebuild test-without-building.*WebDriverAgentRunner"  # the process keeping the runner alive

# Apple apps by the name people use. Anything else goes through Spotlight.
APPLE_APPS = {
    "settings": "com.apple.Preferences", "safari": "com.apple.mobilesafari", "photos": "com.apple.mobileslideshow",
    "camera": "com.apple.camera", "messages": "com.apple.MobileSMS", "phone": "com.apple.mobilephone",
    "mail": "com.apple.mobilemail", "maps": "com.apple.Maps", "notes": "com.apple.mobilenotes",
    "calendar": "com.apple.mobilecal", "reminders": "com.apple.reminders", "clock": "com.apple.mobiletimer",
    "weather": "com.apple.weather", "app store": "com.apple.AppStore", "files": "com.apple.DocumentsApp",
    "music": "com.apple.Music", "facetime": "com.apple.facetime", "contacts": "com.apple.MobileAddressBook",
    "wallet": "com.apple.Passbook", "health": "com.apple.Health", "shortcuts": "com.apple.shortcuts",
    "calculator": "com.apple.calculator", "find my": "com.apple.findmy", "podcasts": "com.apple.podcasts",
    "news": "com.apple.news", "home": "com.apple.Home", "fitness": "com.apple.Fitness",
    "translate": "com.apple.Translate", "voice memos": "com.apple.VoiceMemos", "books": "com.apple.iBooks",
    "tv": "com.apple.tv", "stocks": "com.apple.stocks", "measure": "com.apple.measure",
    "compass": "com.apple.compass", "magnifier": "com.apple.Magnifier", "tips": "com.apple.tips",
    "freeform": "com.apple.freeform", "journal": "com.apple.journal", "passwords": "com.apple.Passwords",
    "watch": "com.apple.Bridge",
}

# Element types worth showing. Keyboard keys and layout containers are noise.
SHOWN = {"Button", "StaticText", "TextField", "SecureTextField", "SearchField", "TextView", "Switch", "Toggle",
         "Link", "Icon", "Cell", "Image", "Slider", "Tab", "MenuItem", "PickerWheel", "Stepper", "PageIndicator"}
SKIP = {"Keyboard"}
KEYS = {"return": "\n", "enter": "\n", "delete": "\b", "backspace": "\b", "tab": "\t", "space": " "}


class Wda:
    def __init__(self, url: str | None = None) -> None:
        self._url = url or os.environ.get("IPHONE_WDA_URL")
        self._http = httpx.Client(timeout=httpx.Timeout(30, connect=1.5))
        self._sid: str | None = None
        self._size: tuple[float, float] | None = None
        self._last: Screen | None = None
        self._ok_until = 0.0  # ready() result cache
        self._ok = False
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._tmp = Path(tempfile.mkdtemp(prefix="iphone-wda-"))

    # -- connection --------------------------------------------------------

    @property
    def url(self) -> str | None:
        if self._url:
            return self._url
        if URL_FILE.exists():
            return URL_FILE.read_text().strip() or None
        return None

    def _req(self, method: str, path: str, body: Any = None, *, session: bool = False,
             timeout: float | None = None, params: dict | None = None) -> Any:
        base = self.url
        if not base:
            raise BridgeError("WebDriverAgent is not running (phone_connect starts it)")
        try:
            LAST_FILE.touch()
        except OSError:
            pass
        for attempt in (1, 2):
            p = path
            if session:
                p = f"/session/{self._session()}{path}"
            try:
                # Actions wait for the app to go idle (up to 10 s, plus 2 s of animations) on WDA's side.
                # Shortening those waits made each scroll take ~14 s instead of ~2.6 s, so allow for them here.
                r = self._http.request(method, base.rstrip("/") + p, json=body, params=params,
                                       timeout=timeout or 60)
            except httpx.HTTPError as e:
                self._ok_until = 0
                raise BridgeError(f"WebDriverAgent at {base} is unreachable ({type(e).__name__}). Is the phone on "
                                  "the same Wi-Fi, and is scripts/wda_start.sh still running?") from None
            data = r.json() if r.content else {}
            val = data.get("value") if isinstance(data, dict) else None
            if r.status_code < 400:
                return val
            err = val if isinstance(val, dict) else {}
            if session and attempt == 1 and err.get("error") == "invalid session id":
                self._sid = None
                continue
            raise WdaError(err.get("error", f"HTTP {r.status_code}"), err.get("message", str(val))[:300])
        raise AssertionError("unreachable")

    def _session(self) -> str:
        if not self._sid:
            out = self._req("POST", "/session", {"capabilities": {"alwaysMatch": {}}})
            self._sid = (out or {}).get("sessionId")
            if not self._sid:
                raise BridgeError("WebDriverAgent did not open a session")
            try:  # a smoother live view (default is 10 fps at full size, low quality)
                self._req("POST", "/appium/settings", {"settings": {
                    "mjpegServerFramerate": 15, "mjpegScalingFactor": 50, "mjpegServerScreenshotQuality": 50}},
                    session=True)
            except WdaError as e:
                log("stream settings not applied:", e)
        return self._sid

    def status(self) -> dict:
        """Reachable, locked and the app in front, for phone_status."""
        if not self.url:
            return {"configured": False}
        try:
            st = self._req("GET", "/status", timeout=3) or {}
            locked = bool(self._req("GET", "/wda/locked", timeout=5))
            app = self._req("GET", "/wda/activeAppInfo", timeout=5) or {}
        except (BridgeError, WdaError) as e:
            return {"configured": True, "reachable": False, "url": self.url, "error": str(e)}
        return {"configured": True, "reachable": True, "url": self.url, "locked": locked,
                "app": app.get("bundleId"), "ios": (st.get("os") or {}).get("version")}

    def ready(self) -> bool:
        """WebDriverAgent is up and the phone is unlocked. Cached for a few seconds."""
        if time.time() < self._ok_until:
            return self._ok
        st = self.status() if self.url else {}
        self._ok = bool(st.get("reachable") and not st.get("locked"))
        self._ok_until = time.time() + (5 if self._ok else 2)
        return self._ok

    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self, wait: float = 60) -> str:
        """Run scripts/wda_start.sh in the background (it outlives this server) and
        wait for the runner to print its URL. Returns the URL."""
        with self._lock:
            if not self.running():
                LOG_FILE.parent.mkdir(exist_ok=True)
                URL_FILE.unlink(missing_ok=True)
                self._url = os.environ.get("IPHONE_WDA_URL")
                log("starting WebDriverAgent:", START)
                self._proc = subprocess.Popen([str(START)], stdout=open(LOG_FILE, "w"), stderr=subprocess.STDOUT,
                                              stdin=subprocess.DEVNULL, start_new_session=True, cwd=ROOT)
        end = time.time() + wait
        while time.time() < end:
            text = LOG_FILE.read_text(errors="replace") if LOG_FILE.exists() else ""
            if (m := SERVER_URL.search(text)):
                URL_FILE.write_text(m.group(1))
                self._ok_until = 0
                return m.group(1)
            if self._proc and self._proc.poll() is not None:
                tail = " / ".join(line.strip() for line in text.strip().splitlines()[-4:])
                raise BridgeError(f"WebDriverAgent did not start: {tail or 'no output'}")
            time.sleep(1)
        raise BridgeError(f"WebDriverAgent was still starting after {wait:.0f}s (log: {LOG_FILE})")

    def stop(self) -> bool:
        """Stop the runner and end its test session, which clears the phone's
        "Automation Running" banner. True if something was running.

        Killing xcodebuild is not enough: the phone's testmanagerd keeps the
        session (and the banner) open. /wda/shutdown lets the test finish
        normally; SIGINT (xcodebuild's own cancel) and SIGTERM are fallbacks."""
        stopped = False
        with self._lock:
            if self.url:
                try:
                    self._http.get(self.url.rstrip("/") + "/wda/shutdown", timeout=5)
                    stopped = True
                except httpx.HTTPError:
                    pass
            # give the clean shutdown time to end xcodebuild, then escalate
            for sig, wait in ((None, 10.0), ("-INT", 8.0), ("-TERM", 3.0)):
                if sig:
                    subprocess.run(["pkill", sig, "-f", RUNNER], capture_output=True)
                    stopped = True
                end = time.time() + wait
                while time.time() < end and self._runner_alive():
                    time.sleep(0.5)
                if not self._runner_alive():
                    break
            URL_FILE.unlink(missing_ok=True)
            self._sid, self._ok, self._ok_until = None, False, 0.0
        return stopped

    def _runner_alive(self) -> bool:
        if self._proc and self._proc.poll() is None:
            return True
        return subprocess.run(["pgrep", "-f", RUNNER], capture_output=True).returncode == 0

    @property
    def stream_url(self) -> str | None:
        """WebDriverAgent's live MJPEG view of the screen (port 9100 next to the API)."""
        if not self.url:
            return None
        host = httpx.URL(self.url).host
        return f"http://{host}:{os.environ.get('IPHONE_WDA_MJPEG_PORT', '9100')}"

    def require(self) -> None:
        if not self.ready():
            st = self.status()
            if not st.get("configured"):
                raise BridgeError("WebDriverAgent is not running (phone_connect starts it)")
            if not st.get("reachable"):
                raise BridgeError(st.get("error", "WebDriverAgent is unreachable"))
            raise BridgeError("the phone is locked. Ask the user to unlock it (Face ID), or lock it and leave it "
                              "near the Mac to use iPhone Mirroring instead. Never type the passcode.")

    # -- geometry ----------------------------------------------------------

    def phone_size(self, win: dict | None = None) -> tuple[float, float]:
        if not self._size:
            v = self._req("GET", "/window/size") or {}
            self._size = (float(v.get("width") or 390), float(v.get("height") or 844))
        return self._size

    # -- seeing ------------------------------------------------------------

    def _png(self) -> bytes:
        import base64
        return base64.b64decode(self._req("GET", "/screenshot") or "")

    def _jpg(self, png: bytes, width: int, crop: tuple[float, float, float, float] | None = None) -> bytes:
        from PIL import Image
        im = Image.open(io.BytesIO(png)).convert("RGB")
        if crop:
            w, h = self.phone_size()
            k = im.width / w
            x, y, cw, ch = crop
            im = im.crop((int(x * k), int(y * k), int((x + cw) * k), int((y + ch) * k)))
        if im.width > width:
            im = im.resize((width, max(1, round(im.height * width / im.width))), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=80)
        return buf.getvalue()

    def _tree(self) -> dict:
        # WebDriverAgent's own visibility check costs ~5 s on a busy screen (0.3-1 s without);
        # on-screen geometry in _items does the job.
        return self._req("GET", "/source", params={
            "format": "json", "excluded_attributes": "frame,nativeFrame,traits,nativeAccessibilityElement,"
                                                     "visible,accessible,enabled,focused"}) or {}

    def _items(self, tree: dict) -> tuple[list[TextItem], scr.Prompt | None]:
        w, h = self.phone_size()
        items: list[TextItem] = []
        prompt: list[Any] = []  # [title texts, buttons] of the first alert/sheet

        def item(n: dict, t: str) -> TextItem | None:
            r = n.get("rect") or {}
            x, y, rw, rh = (float(r.get(k) or 0) for k in ("x", "y", "width", "height"))
            if n.get("isVisible") in ("0", 0, False) or rw <= 1 or rh <= 1:
                return None
            # mostly on screen: other home-screen pages, rows scrolled under a bar
            vis_w = min(x + rw, w) - max(x, 0)
            vis_h = min(y + rh, h) - max(y, 0)
            if vis_w < 0.6 * rw or vis_h < 0.6 * rh:
                return None
            label = (n.get("label") or "").strip()
            value = n.get("value")
            text = label or (str(value).strip() if value not in (None, "") and t != "SecureTextField" else "")
            if t in ("TextField", "SearchField", "TextView") and not text:
                text = (n.get("placeholderValue") or n.get("name") or "").strip()
            if t in ("Switch", "Toggle") and value in ("0", "1"):
                text = f"{label} [{'on' if value == '1' else 'off'}]"
            if t == "SecureTextField":
                text = f"{label or 'Password'} [password field]"
            if not text:
                return None
            return TextItem(text[:200], 1.0, max(x, 0), max(y, 0), min(rw, w - max(x, 0)), min(rh, h - max(y, 0)),
                            kind=t)

        def walk(n: dict, alert: list | None) -> int:
            t = n.get("type") or ""
            if t in SKIP:
                return 0
            here = alert
            if t in ("Alert", "Sheet") and not prompt:
                here = [[], []]
                prompt.append(here)
            kids = n.get("children") or []
            shown = sum(walk(k, here) for k in kids)
            if t not in SHOWN or (t in ("Cell", "Image", "Icon") and shown and t != "Icon"):
                return shown
            it = item(n, t)
            if not it:
                return shown
            items.append(it)
            if here is not None:
                (here[1] if t == "Button" else here[0]).append(it)
            return shown + 1

        walk(tree, None)
        items = _dedupe(items)
        items.sort(key=lambda t: (round(t.cy / 6), t.x))
        p = None
        if prompt and prompt[0][1]:
            texts, btns = prompt[0]
            texts = [t.text for t in sorted(texts, key=lambda t: (t.cy, t.x))]
            p = scr.Prompt(title=texts[0] if texts else "", message=" ".join(texts[1:]),
                           buttons=sorted(btns, key=lambda t: (t.cy, t.cx)))
        return items, p

    def _system_alert(self) -> scr.Prompt | None:
        """Permission prompts belong to SpringBoard and may be missing from the app's tree."""
        try:
            text = self._req("GET", "/alert/text", timeout=5)
        except WdaError:
            return None
        if not text:
            return None
        try:
            names = self._req("GET", "/wda/alert/buttons", session=True, timeout=5) or []
        except WdaError:
            names = []
        lines = [s for s in str(text).splitlines() if s.strip()]
        return scr.Prompt(title=lines[0] if lines else "", message=" ".join(lines[1:]),
                          buttons=[TextItem(b, 1.0, 0, 0, 0, 0, kind="alert") for b in names])

    def _ocr(self, png: bytes) -> list[TextItem]:
        """Read pixels when the tree has nothing (games, video, canvas)."""
        f = self._tmp / "screen.png"
        f.write_bytes(png)
        try:
            out = mirrorctl("ocr", f)
        except BridgeError:
            return []
        w, h = self.phone_size()
        return [TextItem(o["text"], o["conf"], o["x"] * w, o["y"] * h, o["w"] * w, o["h"] * h)
                for o in out.get("ocr", [])]

    def capture(self, *, image: bool = True, ocr: bool = True, fast: bool = False,
                image_width: int | None = None) -> Screen:
        self.require()
        w, h = self.phone_size()
        items: list[TextItem] = []
        prompt = None
        png = None
        if ocr:
            items, prompt = self._items(self._tree())
            prompt = prompt or self._system_alert()
            if len(items) < 3:
                png = self._png()
                items += [o for o in self._ocr(png) if not any(o.text == i.text for i in items)]
        if image:
            png = png or self._png()
        s = Screen(w, h, items, self._jpg(png, image_width or int(w)) if image and png else None, prompt=prompt)
        if ocr:
            self._last = s
        return s

    def zoom(self, x: float, y: float, w: float, h: float, width: int = 780) -> bytes:
        self.require()
        return self._jpg(self._png(), width, crop=(x, y, w, h))

    @property
    def last(self) -> Screen | None:
        return self._last

    # -- acting ------------------------------------------------------------

    def _done(self) -> None:
        self._last = None

    def tap(self, x: float, y: float, *, hold_ms: float = 60, count: int = 1) -> None:
        if hold_ms >= 300:
            self._req("POST", "/wda/touchAndHold", {"x": x, "y": y, "duration": hold_ms / 1000}, session=True)
        elif count == 2:
            self._req("POST", "/wda/doubleTap", {"x": x, "y": y}, session=True)
        else:
            self._req("POST", "/wda/tap", {"x": x, "y": y}, session=True)
        self._done()

    def drag(self, x1: float, y1: float, x2: float, y2: float, *, ms: float = 300, hold_ms: float = 30) -> None:
        if hold_ms >= 40:
            # A scroll: iOS still flings after a 40 ms pause here (a 200 pt drag moved 308 pt) but not after
            # 150 ms, and the first ~20 pt of a drag go to touch slop. Measured on an iPhone 14, iOS 26.4.
            hold_ms = max(hold_ms, 150)
            d = max(((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5, 1)
            x2, y2 = x2 + (x2 - x1) / d * 20, y2 + (y2 - y1) / d * 20
        else:
            hold_ms = 0  # a swipe: let it fling
        acts = [{"type": "pointerMove", "duration": 0, "x": round(x1), "y": round(y1)},
                {"type": "pointerDown", "button": 0}, {"type": "pause", "duration": 40},
                {"type": "pointerMove", "duration": int(ms), "x": round(x2), "y": round(y2)}]
        if hold_ms:
            acts.append({"type": "pause", "duration": int(hold_ms)})
        acts.append({"type": "pointerUp", "button": 0})
        self._req("POST", "/actions", {"actions": [{"type": "pointer", "id": "finger1",
                                                    "parameters": {"pointerType": "touch"}, "actions": acts}]},
                  session=True)
        self._done()

    def key(self, name: str, mods: str = "") -> None:
        if mods:
            raise ValueError("modifier keys (cmd, shift...) need iPhone Mirroring's hardware keyboard; "
                             "with WebDriverAgent, tap the control instead")
        k = name.lower()
        if k == "escape":
            self._req("POST", "/wda/keyboard/dismiss", {}, session=True)
        elif k in KEYS or len(name) == 1:
            self._req("POST", "/wda/keys", {"value": [KEYS.get(k, name)]}, session=True)
        else:
            raise ValueError(f"key {name!r} is not available without a hardware keyboard")
        self._done()

    def type_text(self, text: str) -> list[str]:
        self._req("POST", "/wda/keys", {"value": list(text)}, session=True, timeout=30 + len(text) * 0.2)
        self._done()
        return []

    def menu(self, title: str) -> None:
        w, h = self.phone_size()
        if title in ("Home Screen", "Spotlight"):
            self._req("POST", "/wda/homescreen", {})
        if title == "Spotlight":
            time.sleep(0.6)
            self.drag(w / 2, h * 0.3, w / 2, h * 0.6, ms=250, hold_ms=0)
        elif title == "App Switcher":
            # swipe up from the bottom edge and pause mid-screen
            self.drag(w / 2, h - 2, w / 2, h * 0.6, ms=450, hold_ms=700)
        self._done()

    def launch(self, bundle_id: str) -> None:
        self._req("POST", "/wda/apps/launch", {"bundleId": bundle_id}, session=True, timeout=60)
        self._done()

    def bundle_for(self, name: str) -> str | None:
        n = name.strip()
        if re.fullmatch(r"[A-Za-z0-9-]+(\.[A-Za-z0-9-]+){2,}", n):
            return n
        return APPLE_APPS.get(n.lower())

    def open_url(self, url: str) -> None:
        self._req("POST", "/url", {"url": url}, session=True, timeout=30)
        self._done()

    def answer_alert(self, name: str) -> None:
        self._req("POST", "/alert/accept", {"name": name}, timeout=15)
        self._done()

    # Mirroring needs focus juggling; this engine doesn't.
    def hold_focus(self) -> None:
        pass

    def release_focus(self) -> None:
        pass


def _inside(a: TextItem, b: TextItem, pad: float = 2) -> bool:
    return (a.x >= b.x - pad and a.y >= b.y - pad and a.x + a.w <= b.x + b.w + pad
            and a.y + a.h <= b.y + b.h + pad)


def _bare(t: str) -> str:
    return re.sub(r"\s*\[(on|off)\]$", "", t).strip().lower()


def _dedupe(items: list[TextItem]) -> list[TextItem]:
    """A Settings row is a button or switch whose label repeats its child texts
    ("Bluetooth, On" over "Bluetooth" and "On"). Keep the row, drop the
    children. A switch row's tap point moves onto its toggle, because tapping
    the label does not flip it."""
    rows = [i for i in items if i.kind in ("Button", "Switch", "Toggle", "Cell", "Link") and i.text]
    keep: list[TextItem] = []
    seen: set[tuple] = set()
    for it in items:
        key = (it.text, round(it.x), round(it.y), round(it.w), round(it.h))
        if key in seen:
            continue
        seen.add(key)
        parent = next((r for r in rows if r is not it and r.w * r.h > it.w * it.h and _inside(it, r)
                       and _bare(it.text) in _bare(r.text)), None)
        if parent is None:
            keep.append(it)
        elif it.kind in ("Switch", "Toggle") and parent.kind in ("Switch", "Toggle"):
            parent.x, parent.y, parent.w, parent.h = it.x, it.y, it.w, it.h
    return keep


class WdaError(RuntimeError):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(f"{kind}: {message}")
        self.kind = kind
