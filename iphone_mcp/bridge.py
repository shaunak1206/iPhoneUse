"""Python side of the mirrorctl helper.

`Mirror` owns the coordinate system the tools expose: **phone points**, origin
at the top-left of the phone screen (an iPhone 13 is 390 x 844). It maps them
onto the iPhone Mirroring window in macOS global coordinates, and maps OCR
boxes back.
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "bridge" / "mirrorctl.swift"
# IPHONE_MIRRORCTL points the bridge at a stand-in (the test suite uses a fake).
BIN = Path(os.environ.get("IPHONE_MIRRORCTL", ROOT / "bin" / "mirrorctl"))

# The phone's logical screen size in points. 390 x 844 is iPhone 12/13/14
# (non-mini, non-Max). Set these for other models.
PHONE_WIDTH_PT = float(os.environ.get("IPHONE_WIDTH_PT", "390"))
PHONE_HEIGHT_PT = float(os.environ.get("IPHONE_HEIGHT_PT", "844"))
ASPECT = PHONE_HEIGHT_PT / PHONE_WIDTH_PT
RESTORE_FOCUS = os.environ.get("IPHONE_RESTORE_FOCUS", "1") != "0"


def log(*a: object) -> None:
    print("[iphone-mcp]", *a, file=sys.stderr, flush=True)


class BridgeError(RuntimeError):
    pass


def ensure_built() -> None:
    """Compile mirrorctl if the binary is missing or older than its source."""
    if "IPHONE_MIRRORCTL" in os.environ or BIN.exists() and BIN.stat().st_mtime >= SRC.stat().st_mtime:
        return
    BIN.parent.mkdir(exist_ok=True)
    log("building mirrorctl ...")
    r = subprocess.run(["swiftc", "-O", str(SRC), "-o", str(BIN)], capture_output=True, text=True)
    if r.returncode != 0:
        raise BridgeError(f"swiftc failed:\n{r.stderr[-2000:]}")


class _Helper:
    """One long-lived `mirrorctl serve` process. Keeps Vision's OCR model warm
    (cold start costs ~1.5 s per call) and avoids a spawn per mouse event."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()

    def _spawn(self) -> subprocess.Popen[str]:
        ensure_built()
        return subprocess.Popen([str(BIN), "serve"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=sys.stderr, text=True, bufsize=1)

    def call(self, args: list[str], timeout: float) -> dict:
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                self._proc = self._spawn()
            proc = self._proc
            assert proc.stdin and proc.stdout
            try:
                proc.stdin.write(json.dumps(args) + "\n")
                proc.stdin.flush()
                ready, _, _ = select.select([proc.stdout], [], [], timeout)
                line = proc.stdout.readline() if ready else ""
            except (BrokenPipeError, OSError) as e:
                line, ready = "", [e]
            if not line:
                proc.kill()
                self._proc = None
                why = "timed out" if not ready else "exited"
                raise BridgeError(f"mirrorctl {args[0]} {why}")
            return json.loads(line)


_helper = _Helper()


def run(*args: object, timeout: float = 30) -> dict:
    if args and args[0] == "ocr":
        # Vision stalls for 30-45 s inside the long-lived `serve` helper (seen on macOS 27) but
        # takes ~0.3-0.7 s as a one-off process, so OCR of saved images runs one-off.
        ensure_built()
        r = subprocess.run([str(BIN), *map(str, args)], capture_output=True, text=True, timeout=timeout)
        out = json.loads(r.stdout or "{}")
        if "error" in out:
            raise BridgeError(f"mirrorctl {args[0]}: {out['error']}")
        return out
    out = _helper.call([str(a) for a in args], timeout)
    if "error" in out:
        raise BridgeError(out["error"])
    return out


@dataclass
class TextItem:
    text: str
    conf: float
    # phone points, top-left origin
    x: float
    y: float
    w: float
    h: float
    # element type when read from WebDriverAgent's tree (Button, Switch, ...); "" for OCR text
    kind: str = ""

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    def contains(self, px: float, py: float, pad: float = 4) -> bool:
        return self.x - pad <= px <= self.x + self.w + pad and self.y - pad <= py <= self.y + self.h + pad


@dataclass
class Screen:
    width: float  # phone points
    height: float
    items: list[TextItem] = field(default_factory=list)
    jpg: bytes | None = None
    taken_at: float = field(default_factory=time.time)
    # an alert the engine read directly (WebDriverAgent); else screen.prompt guesses from layout
    prompt: Any = None


@dataclass
class State:
    raw: dict

    @property
    def running(self) -> bool:
        return bool(self.raw.get("running"))

    @property
    def window(self) -> dict | None:
        return self.raw.get("phone_window") or None

    @property
    def connected(self) -> bool:
        # The View menu's Home Screen item is only enabled while a phone is
        # actually being mirrored; it is the most reliable "live" signal.
        return bool(self.window) and bool(self.raw.get("view_menu", {}).get("Home Screen"))

    @property
    def screen_access(self) -> bool:
        return bool(self.raw.get("screen_access"))

    @property
    def messages(self) -> list[str]:
        out = []
        for w in self.raw.get("ax", []):
            texts = [t for t in w.get("texts", []) if t.strip()]
            if texts:
                line = " / ".join(texts)
                if w.get("buttons"):
                    line += f"  [buttons: {', '.join(w['buttons'])}]"
                out.append(line)
        return out

    @property
    def buttons(self) -> list[str]:
        return [b for w in self.raw.get("ax", []) for b in w.get("buttons", [])]

    def problem(self) -> str | None:
        """Why the phone cannot be driven right now, in plain words, or None."""
        if not self.raw.get("ax_trusted"):
            return ("The process running this MCP server lacks Accessibility permission. Grant it in "
                    "System Settings > Privacy & Security > Accessibility.")
        if not self.running:
            return "iPhone Mirroring is not running. Call phone_connect."
        if not self.connected:
            msg = " ".join(self.messages) or "no phone window"
            low = msg.lower()
            if "locked" in low and ("touch" in low or "login" in low or "password" in low):
                return ("iPhone Mirroring is locked and needs the user's Touch ID or Mac password. Ask the "
                        "user to unlock it (never type the password yourself). Tip: iPhone Mirroring > "
                        f"Settings > 'Authenticate Automatically' avoids this. App says: {msg}")
            if "in use" in low:
                return ("The iPhone is being used in hand, which pauses mirroring. Ask the user to lock "
                        f"the phone, then call phone_connect. App says: {msg}")
            return f"iPhone Mirroring is not connected to the phone. App says: {msg}"
        if not self.screen_access:
            return ("Screen Recording permission is missing, so the phone screen cannot be captured. Ask "
                    "the user to enable it for the app hosting this server in System Settings > Privacy & "
                    "Security > Screen & System Audio Recording, then restart that app.")
        return None


class Mirror:
    def __init__(self) -> None:
        self._last: Screen | None = None
        self._tmp = Path(tempfile.mkdtemp(prefix="iphone-mcp-"))
        # (window id, w, h) -> phone screen rect inside the window, in points
        self._content: dict[tuple, tuple[float, float, float, float]] = {}
        # set while hold_focus() keeps Mirroring in front: (prev app, cursor)
        self._held: tuple[dict | None, dict | None] | None = None

    # -- state -------------------------------------------------------------

    def state(self) -> State:
        return State(run("state"))

    def require(self) -> State:
        st = self.state()
        p = st.problem()
        if p:
            raise BridgeError(p)
        return st

    def launch(self) -> None:
        run("launch")

    def press_app_button(self, label: str) -> bool:
        return bool(run("button", label).get("ok"))

    def menu(self, title: str) -> None:
        run("menu", title)

    # -- geometry ----------------------------------------------------------
    #
    # The window is not all phone: a transparent border surrounds the screen
    # and a taller strip sits on top for the hover controls. mirrorctl finds
    # the screen from the capture's alpha channel; it is cached per window
    # size and refreshed by every capture.

    @staticmethod
    def _key(win: dict) -> tuple:
        return (win["id"], round(win["w"]), round(win["h"]))

    def _remember(self, win: dict, out: dict) -> None:
        c = out.get("content")
        if c and out.get("window_px_width"):
            k = win["w"] / out["window_px_width"]
            self._content[self._key(win)] = (c["x"] * k, c["y"] * k, c["w"] * k, c["h"] * k)

    def _content_rect(self, win: dict) -> tuple[float, float, float, float]:
        key = self._key(win)
        if key not in self._content:
            self._remember(win, run("shot", "--window", win["id"], "--content", "--aspect", f"{ASPECT:.5f}"))
        return self._content.get(key, (0.0, 0.0, win["w"], win["h"]))

    def to_global(self, win: dict, px: float, py: float) -> tuple[float, float]:
        cx, cy, cw, ch = self._content_rect(win)
        px = min(max(px, 0), PHONE_WIDTH_PT - 0.5)
        py = min(max(py, 0), PHONE_HEIGHT_PT - 0.5)
        return win["x"] + cx + px * cw / PHONE_WIDTH_PT, win["y"] + cy + py * ch / PHONE_HEIGHT_PT

    def phone_size(self, win: dict | None = None) -> tuple[float, float]:
        return PHONE_WIDTH_PT, PHONE_HEIGHT_PT

    # -- capture -----------------------------------------------------------

    def _shot_args(self, win: dict) -> list[object]:
        return ["shot", "--window", win["id"], "--content", "--aspect", f"{ASPECT:.5f}"]

    def capture(self, *, image: bool = True, ocr: bool = True, fast: bool = False,
                image_width: int | None = None) -> Screen:
        st = self.require()
        win = st.window
        assert win
        pw, ph = self.phone_size()
        args = self._shot_args(win)
        jpg = self._tmp / "screen.jpg"
        if image:
            args += ["--jpg", jpg, "--width", image_width or int(pw)]
        if ocr:
            args.append("--ocr")
            if fast:
                args.append("--fast")
        try:
            out = run(*args, timeout=8 if ocr else 30)
        except BridgeError as e:
            if not ocr or "timed out" not in str(e):
                raise
            # Vision can stall inside the long-lived helper; capture without OCR, then read the
            # picture in a one-off process (~0.5 s).
            shot = self._tmp / "ocr.jpg"
            out = run(*self._shot_args(win), "--jpg", shot, "--width", int(pw * 2))
            out["ocr"] = run("ocr", shot, *(["--fast"] if fast else [])).get("ocr", [])
            if image:
                run(*self._shot_args(win), "--jpg", jpg, "--width", image_width or int(pw))
        self._remember(win, out)
        items = [
            TextItem(o["text"], o["conf"], o["x"] * pw, o["y"] * ph, o["w"] * pw, o["h"] * ph)
            for o in out.get("ocr", [])
        ]
        items.sort(key=lambda t: (round(t.cy / 6), t.x))
        screen = Screen(pw, ph, items, jpg.read_bytes() if image else None)
        if ocr:
            self._last = screen
        return screen

    def zoom(self, x: float, y: float, w: float, h: float, width: int = 780) -> bytes:
        st = self.require()
        win = st.window
        assert win
        pw, ph = self.phone_size()
        crop = f"{x / pw:.5f},{y / ph:.5f},{w / pw:.5f},{h / ph:.5f}"
        jpg = self._tmp / "zoom.jpg"
        self._remember(win, run(*self._shot_args(win), "--crop", crop, "--jpg", jpg, "--width", width))
        return jpg.read_bytes()

    @property
    def last(self) -> Screen | None:
        return self._last

    # -- input -------------------------------------------------------------

    def _hand_back(self, prev: dict | None, cur: dict | None) -> None:
        if prev and prev.get("bundle") != "com.apple.ScreenContinuity":
            time.sleep(0.1)
            run("restore", prev["pid"])
            if cur:
                run("warp", cur["x"], cur["y"])

    def _focused(self, fn) -> None:
        """Bring Mirroring to the front, run fn(window), then hand focus and the
        cursor back to whatever the user had (once at the end under hold_focus)."""
        st = self.require()
        win = st.window
        assert win
        held = self._held is not None
        prev = run("frontmost") if RESTORE_FOCUS and not held else None
        cur = run("cursor") if RESTORE_FOCUS and not held else None
        if not st.raw.get("active"):
            run("activate")
            time.sleep(0.15)
        try:
            fn(win)
        finally:
            if not held:
                self._hand_back(prev, cur)
        self._last = None

    def hold_focus(self) -> None:
        """Keep Mirroring in front across several actions (phone_do) instead of
        flipping focus back after each one. release_focus() ends it."""
        self.require()
        self._held = (run("frontmost") if RESTORE_FOCUS else None, run("cursor") if RESTORE_FOCUS else None)

    def release_focus(self) -> None:
        held, self._held = self._held, None
        if held:
            self._hand_back(*held)

    def tap(self, x: float, y: float, *, hold_ms: float = 60, count: int = 1) -> None:
        def go(win: dict) -> None:
            gx, gy = self.to_global(win, x, y)
            run("click", f"{gx:.1f}", f"{gy:.1f}", "--hold", hold_ms, "--count", count)
        self._focused(go)

    def drag(self, x1: float, y1: float, x2: float, y2: float, *, ms: float = 300, hold_ms: float = 30) -> None:
        def go(win: dict) -> None:
            a = self.to_global(win, x1, y1)
            b = self.to_global(win, x2, y2)
            run("drag", f"{a[0]:.1f}", f"{a[1]:.1f}", f"{b[0]:.1f}", f"{b[1]:.1f}", "--ms", ms, "--hold", hold_ms)
        self._focused(go)

    def scroll(self, x: float, y: float, dy_pt: float, dx_pt: float = 0, steps: int = 12) -> None:
        def go(win: dict) -> None:
            gx, gy = self.to_global(win, x, y)
            scale = self._content_rect(win)[2] / PHONE_WIDTH_PT
            run("scroll", f"{gx:.1f}", f"{gy:.1f}", int(dy_pt * scale), int(dx_pt * scale), "--steps", steps)
        self._focused(go)

    def key(self, name: str, mods: str = "") -> None:
        self._focused(lambda win: run("key", name, *(["--mods", mods] if mods else [])))

    def type_text(self, text: str) -> list[str]:
        out: dict = {}

        def go(win: dict) -> None:
            out.update(run("type", text, timeout=30 + len(text) * 0.1))
        self._focused(go)
        return out.get("unicode_fallback", [])
