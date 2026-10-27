"""WebDriverAgent engine: drives the phone from the inside through Apple's UI
testing framework. The WebDriverAgentRunner app on the phone serves HTTP on
port 8100 while scripts/wda_start.sh keeps it running from this Mac.

Unlike Mirroring, the phone can be unlocked and in the user's hand, screens are
read from the real accessibility tree (so icon-only buttons have names), and
apps open by bundle id. Same interface as bridge.Mirror, so the server's tools
don't care which engine is driving.
"""

from __future__ import annotations

import hashlib
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


class Frames:
    """Keeps the newest frame of the phone's MJPEG stream on a background thread,
    so a screenshot costs nothing and "has the screen stopped moving?" can be
    answered by comparing frames (WDA encodes an unchanged screen to the same
    bytes)."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.jpg: bytes | None = None
        self.at = 0.0        # when the newest frame arrived
        self.changed = 0.0   # when the picture last changed
        self._digest = b""
        self._stop = False
        threading.Thread(target=self._run, name="wda-frames", daemon=True).start()

    def stop(self) -> None:
        self._stop = True

    def _run(self) -> None:
        while not self._stop:
            try:
                with httpx.stream("GET", self.url, timeout=httpx.Timeout(10, connect=2)) as r:
                    buf = b""
                    for chunk in r.iter_raw():
                        if self._stop:
                            return
                        buf += chunk
                        while True:
                            a = buf.find(b"\xff\xd8")
                            b = buf.find(b"\xff\xd9", a + 2) if a >= 0 else -1
                            if b < 0:
                                break
                            frame, buf = buf[a:b + 2], buf[b + 2:]
                            now = time.time()
                            d = hashlib.blake2b(frame, digest_size=16).digest()
                            if d != self._digest:
                                self._digest, self.changed = d, now
                            self.jpg, self.at = frame, now
                        if len(buf) > 8_000_000:
                            buf = b""
            except Exception:
                time.sleep(1.0)

    def fresh(self, max_age: float = 0.5) -> bytes | None:
        return self.jpg if self.jpg and time.time() - self.at < max_age else None

    def wait_still(self, quiet: float = 0.2, cap: float = 1.2, min_wait: float = 0.12) -> float:
        """Wait until the picture has not changed for `quiet` seconds (or `cap` runs
        out: animated ads never hold still). Returns seconds waited."""
        start = time.time()
        time.sleep(min_wait)  # let the action start drawing
        while time.time() - start < cap:
            if self.fresh(0.5) is None:
                time.sleep(max(0.0, 0.25 - (time.time() - start)))  # no stream: fall back to a short pause
                break
            if time.time() - self.changed >= quiet:
                break
            time.sleep(0.04)
        return time.time() - start


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
        self._frames: Frames | None = None
        self._gap_ocr: dict[tuple, list[TextItem]] = {}  # OCR of the last unlabeled area, by frame

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
            # Waiting for the app to go quiet before each action costs ~0.1-0.4 s and never ends on pages
            # with animated ads; the tools settle on their own after acting.
            out = self._req("POST", "/session", {"capabilities": {"alwaysMatch": {"shouldWaitForQuiescence": False}}})
            self._sid = (out or {}).get("sessionId")
            if not self._sid:
                raise BridgeError("WebDriverAgent did not open a session")
            try:
                # No waiting for the app to go idle or for animations after actions (animated ads keep
                # both running to their caps); Phone.settle watches the live frames instead. A smoother,
                # sharper live view than the default 10 fps at quality 25.
                self._req("POST", "/appium/settings", {"settings": {
                    "waitForIdleTimeout": 0, "animationCoolOffTimeout": 0,
                    "mjpegServerFramerate": 24, "mjpegScalingFactor": 50, "mjpegServerScreenshotQuality": 55}},
                    session=True)
            except WdaError as e:
                log("session settings not applied:", e)
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
            if self._frames:
                self._frames.stop()
                self._frames = None
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

    def frames(self) -> Frames | None:
        url = self.stream_url
        if not url:
            return None
        if self._frames is None or self._frames.url != url:
            if self._frames:
                self._frames.stop()
            self._frames = Frames(url)
        return self._frames

    def unchanged_since(self, t: float) -> bool | None:
        """Whether the picture has stayed the same since time t; None when the live
        stream is not running (so nothing can be said)."""
        f = self._frames
        if f is None or f.fresh(0.5) is None:
            return None
        return f.changed < t

    def settle(self) -> float:
        """Wait for the screen to stop changing after an action (0.1-1.2 s)."""
        f = self.frames()
        if f is None:
            time.sleep(0.25)
            return 0.25
        return f.wait_still()

    def _png(self) -> bytes:
        """The current screen as image bytes: the newest live frame if the grabber
        has one (free), else one frame read from the stream (~0.13 s), else
        /screenshot (~0.25 s, full resolution)."""
        f = self.frames()
        if f and (jpg := f.fresh(0.4)):
            return jpg
        if self.stream_url:
            try:
                with self._http.stream("GET", self.stream_url, timeout=httpx.Timeout(3, connect=1)) as r:
                    buf = b""
                    for chunk in r.iter_raw():
                        buf += chunk
                        a = buf.find(b"\xff\xd8")
                        b = buf.find(b"\xff\xd9", a + 2) if a >= 0 else -1
                        if b > a >= 0:
                            return buf[a:b + 2]
                        if len(buf) > 4_000_000:
                            break
            except httpx.HTTPError:
                pass
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

        def full(n: dict) -> bool:
            r = n.get("rect") or {}
            return float(r.get("width") or 0) * float(r.get("height") or 0) >= 0.9 * w * h

        def covers(part: list[TextItem]) -> bool:
            # Things spread down the screen, not a toolbar strip (Safari keeps its
            # bottom bar in a full-screen layer drawn after the page).
            return len(part) >= 3 and max(i.y + i.h for i in part) - min(i.y for i in part) >= 0.4 * h

        def walk(n: dict, alert: list | None) -> list[TextItem]:
            t = n.get("type") or ""
            if t in SKIP:
                return []
            here = alert
            if t in ("Alert", "Sheet") and not prompt:
                here = [[], []]
                prompt.append(here)
            kids = n.get("children") or []
            parts = [walk(k, here) for k in kids]
            # Siblings are drawn in order, so a full-screen layer under a later
            # full-screen layer that shows things is covered (Chrome keeps its tab
            # grid under the page). WebDriverAgent's own visibility check would
            # catch this but costs seconds per read.
            top = max((i for i, k in enumerate(kids) if full(k) and covers(parts[i])), default=-1)
            got = [it for i, part in enumerate(parts) if not (full(kids[i]) and i < top) for it in part]
            if t not in SHOWN or (t in ("Cell", "Image", "Icon") and got and t != "Icon"):
                return got
            it = item(n, t)
            if not it:
                return got
            if here is not None:
                (here[1] if t == "Button" else here[0]).append(it)
            return got + [it]

        items = _dedupe(walk(tree, None))
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

    def _ocr(self, png: bytes, fast: bool = False, timeout: float = 10,
             region: tuple[float, float, float, float] | None = None) -> list[TextItem]:
        """Read pixels when the tree has nothing (games, video, canvas). `region` is
        where `png` sits on the screen (x, y, w, h in points) when it is a crop."""
        f = self._tmp / "screen.img"
        f.write_bytes(png)
        try:
            out = mirrorctl("ocr", f, *(["--fast"] if fast else []), timeout=timeout)
        except (BridgeError, subprocess.TimeoutExpired, ValueError):
            return []
        rx, ry, w, h = region or (0.0, 0.0, *self.phone_size())
        return [TextItem(o["text"], o["conf"], rx + o["x"] * w, ry + o["y"] * h, o["w"] * w, o["h"] * h)
                for o in out.get("ocr", [])]

    def _unlabeled(self, items: list[TextItem]) -> tuple[list[TextItem], list[str]]:
        """Part of the screen with no labels but something drawn there (a canvas game,
        a picture, a video): OCR just that part and say so, so the agent knows to look
        at a screenshot. Plain background (a half-empty settings page) is left alone."""
        w, h = self.phone_size()
        top, bottom = 0.06 * h, 0.96 * h  # status bar, home indicator
        gaps, y = [], top
        for a, b in sorted((max(it.y, top), min(it.y + it.h, bottom)) for it in items
                           if it.y + it.h > top and it.y < bottom):
            if a - y >= 0.25 * h:
                gaps.append((y, a))
            y = max(y, b)
        if bottom - y >= 0.25 * h:
            gaps.append((y, bottom))
        if not gaps:
            return [], []
        from PIL import Image, ImageStat
        try:
            png = self._png()
            im = Image.open(io.BytesIO(png)).convert("RGB")
        except Exception:  # no picture to look at: say nothing rather than fail the read
            return [], []
        k = im.width / w
        frame = hashlib.blake2b(png, digest_size=8).digest()
        found: list[TextItem] = []
        notes: list[str] = []
        for y0, y1 in gaps:
            crop = im.crop((0, int(y0 * k), im.width, int(y1 * k)))
            if ImageStat.Stat(crop.convert("L")).stddev[0] < 12:
                continue
            key = (frame, round(y0), round(y1))
            if key not in self._gap_ocr:
                buf = io.BytesIO()
                crop.save(buf, "PNG")
                self._gap_ocr = {key: self._ocr(buf.getvalue(), fast=True, timeout=4, region=(0, y0, w, y1 - y0))}
            for o in self._gap_ocr[key]:
                if not any(o.text == i.text for i in items):
                    found.append(TextItem(o.text, o.conf, o.x, o.y, o.w, o.h, kind="ocr"))
            notes.append(f"the area from y={y0:.0f} to y={y1:.0f} has no labels but shows something "
                         f"(a picture, video or drawn game){'; text in it was read from pixels' if found else ''}. "
                         "Look at it with phone_screenshot.")
        return found, notes

    def capture(self, *, image: bool = True, ocr: bool = True, fast: bool = False,
                image_width: int | None = None, alerts: bool = True) -> Screen:
        """The screen: the tree's labelled items (ocr=True), the picture (image=True).
        alerts=False skips the SpringBoard alert check (~0.55 s), for reads whose
        caller checks it separately or does not need it."""
        self.require()
        t0 = time.time()
        w, h = self.phone_size()
        items: list[TextItem] = []
        notes: list[str] = []
        prompt = None
        png = None
        if ocr:
            items, prompt = self._items(self._tree())
            if alerts:
                prompt = prompt or self._system_alert()
            if len(items) < 3:
                png = self._png()
                items += [o for o in self._ocr(png) if not any(o.text == i.text for i in items)]
            else:
                extra, notes = self._unlabeled(items)
                if extra:
                    items = sorted(items + extra, key=lambda t: (round(t.cy / 6), t.x))
        if image:
            png = png or self._png()
        s = Screen(w, h, items, self._jpg(png, image_width or int(w)) if image and png else None,
                   taken_at=t0, prompt=prompt, notes=notes)
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

    def tap_many(self, points: list[tuple[float, float]], filler: tuple[float, float] | None = None) -> None:
        """Several taps in a row, one request each (~0.6 s per tap). Batching them in
        one W3C actions request does not work on a real phone (iOS 26.4): two taps on
        one finger register nothing, and one finger per tap drops every second finger.

        With `filler`, a point where a stray tap does nothing, the taps go in one
        request per five with a filler tap in every second slot, so the dropped
        fingers are the fillers (5 keys in ~1.4 s instead of ~3.2 s)."""
        if filler is None or len(points) < 2:
            for x, y in points:
                self._req("POST", "/wda/tap", {"x": x, "y": y}, session=True)
            self._done()
            return
        hold, gap = 40, 90
        for i in range(0, len(points), 5):
            seq: list[tuple[float, float]] = []
            for j, pt in enumerate(points[i:i + 5]):
                seq += [filler, pt] if j else [pt]
            fingers = [{"type": "pointer", "id": f"finger{n + 1}", "parameters": {"pointerType": "touch"},
                        "actions": ([{"type": "pause", "duration": n * (hold + gap)}] if n else []) + [
                            {"type": "pointerMove", "duration": 0, "x": round(x), "y": round(y)},
                            {"type": "pointerDown", "button": 0}, {"type": "pause", "duration": hold},
                            {"type": "pointerUp", "button": 0}]}
                       for n, (x, y) in enumerate(seq)]
            self._req("POST", "/actions", {"actions": fingers}, session=True, timeout=60)
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

    def terminate(self, bundle_id: str) -> None:
        self._req("POST", "/wda/apps/terminate", {"bundleId": bundle_id}, session=True)
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
