"""Stand-in for WebDriverAgent on the phone: a tiny HTTP server with a scripted
accessibility tree. Every request is recorded so the tests can assert on the
exact calls (tap points, typed keys, launched bundle ids)."""

from __future__ import annotations

import base64
import io
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def node(t: str, label: str = "", rect=(0, 0, 0, 0), value=None, children=(), visible=True, **kw) -> dict:
    x, y, w, h = rect
    d = {"type": t, "label": label or None, "name": label or None, "value": value,
         "rect": {"x": x, "y": y, "width": w, "height": h}, "isVisible": "1" if visible else "0",
         "isEnabled": "1", "children": list(children)}
    d.update(kw)
    return d


def app(*children) -> dict:
    return node("Application", "", (0, 0, 390, 844), children=children)


ALERT = node("Alert", "Delete “Instagram”?", (60, 300, 270, 180), children=[
    node("StaticText", "Delete “Instagram”?", (80, 320, 230, 22)),
    node("StaticText", "Deleting this app will also delete its data.", (80, 346, 230, 36)),
    node("Button", "Cancel", (60, 430, 135, 44)), node("Button", "Delete", (195, 430, 135, 44))])

SCREENS = {
    "springboard": app(node("Icon", "Settings", (20, 110, 64, 80)), node("Icon", "Photos", (110, 110, 64, 80)),
                       node("Icon", "Maps", (200, 110, 64, 80)),
                       node("Other", "", (0, 0, 390, 844), children=[node("Button", "Search", (160, 760, 70, 30))])),
    "settings": app(node("StaticText", "Settings", (20, 90, 150, 40)),
                    node("Switch", "Airplane Mode", (16, 180, 358, 44), value="0"),
                    node("Cell", "", (16, 230, 358, 44), children=[node("StaticText", "General", (60, 240, 80, 22))]),
                    node("Cell", "Hidden row", (16, 2000, 358, 44)),
                    node("Keyboard", "", (0, 600, 390, 244), children=[node("Key", "q", (0, 610, 39, 50))])),
    "general": app(node("Button", "Settings", (8, 50, 90, 44)), node("StaticText", "General", (160, 60, 70, 22)),
                   node("Cell", "", (16, 300, 358, 44), children=[node("StaticText", "iPhone Storage", (60, 310, 130, 22))])),
    "storage": app(node("StaticText", "Instagram", (140, 60, 100, 22)), node("Button", "Offload App", (16, 380, 358, 44)),
                   node("Button", "Delete App", (16, 440, 358, 44))),
    "confirm": app(node("StaticText", "Instagram", (140, 60, 100, 22)), node("Button", "Delete App", (16, 440, 358, 44)),
                   ALERT),
    "deleted": app(node("StaticText", "App deleted", (120, 400, 150, 22))),
    "maps": app(node("SearchField", "", (20, 760, 350, 40), placeholderValue="Search Maps")),
    "login": app(node("TextField", "Email", (20, 300, 350, 44)), node("SecureTextField", "Password", (20, 360, 350, 44))),
    "game": app(),
    "web": app(node("StaticText", "Example Domain", (120, 200, 150, 22))),
}
TAPS = {("springboard", "Settings"): "settings", ("settings", "General"): "general",
        ("general", "iPhone Storage"): "storage", ("storage", "Delete App"): "confirm",
        ("confirm", "Delete"): "deleted", ("confirm", "Cancel"): "storage"}
BUNDLES = {"com.apple.Preferences": "settings", "com.apple.Maps": "maps", "com.example.game": "game"}


def _png() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (1170, 2532), (20, 20, 20)).save(buf, "PNG")
    return buf.getvalue()


class FakeWda:
    def __init__(self) -> None:
        self.screen = "springboard"
        self.locked = False
        self.session = "S1"
        self.sysalert: dict | None = None  # {"text": ..., "buttons": [...], "then": screen}
        self.calls: list[tuple[str, str, dict]] = []
        self.typed = ""
        self._png = base64.b64encode(_png()).decode()
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def _send(self, code: int, value) -> None:
                body = json.dumps({"value": value, "sessionId": fake.session}).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _handle(self, method: str) -> None:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
                path = self.path.split("?")[0]
                fake.calls.append((method, re.sub(r"^/session/[^/]+", "", path), body))
                code, value = fake.route(method, path, body)
                self._send(code, value)

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.server.shutdown()

    def called(self, path_re: str) -> list[dict]:
        return [b for _, p, b in self.calls if re.fullmatch(path_re, p)]

    def _hit(self, x: float, y: float) -> str | None:
        def walk(n):
            for k in reversed(n.get("children") or []):  # topmost (alerts) last in the tree
                if (r := walk(k)):
                    return r
            r = n["rect"]
            if n.get("label") and n["type"] != "Application" and \
                    r["x"] <= x <= r["x"] + r["width"] and r["y"] <= y <= r["y"] + r["height"]:
                return n["label"]
            return None
        return walk(SCREENS[self.screen])

    def route(self, method: str, path: str, body: dict):
        no_alert = (404, {"error": "no such alert", "message": "An attempt was made to operate on a modal dialog "
                                                               "when one was not open"})
        if m := re.fullmatch(r"/session/([^/]+)(/.*)", path):
            if m.group(1) != self.session:
                return 404, {"error": "invalid session id", "message": "Session does not exist"}
            path = m.group(2)
        if path == "/status":
            return 200, {"ready": True, "os": {"version": "26.4.2"}}
        if path == "/session" and method == "POST":
            return 200, {"sessionId": self.session, "capabilities": {}}
        if path == "/wda/locked":
            return 200, self.locked
        if path == "/wda/activeAppInfo":
            return 200, {"bundleId": "com.apple.springboard" if self.screen == "springboard" else "com.example"}
        if path == "/window/size":
            return 200, {"width": 390, "height": 844}
        if path == "/source":
            return 200, SCREENS[self.screen]
        if path == "/screenshot":
            return 200, self._png
        if path == "/alert/text":
            return (200, self.sysalert["text"]) if self.sysalert else no_alert
        if path == "/wda/alert/buttons":
            return (200, self.sysalert["buttons"]) if self.sysalert else no_alert
        if path == "/alert/accept":
            if not self.sysalert or body.get("name") not in self.sysalert["buttons"]:
                return no_alert
            self.screen, self.sysalert = self.sysalert["then"], None
            return 200, None
        if path in ("/wda/tap", "/wda/doubleTap", "/wda/touchAndHold"):
            t = self._hit(body["x"], body["y"])
            if (nxt := TAPS.get((self.screen, t))):
                self.screen = nxt
            return 200, None
        if path == "/wda/homescreen":
            self.screen = "springboard"
            return 200, None
        if path == "/wda/apps/launch":
            if body.get("bundleId") not in BUNDLES:
                return 500, {"error": "unknown error", "message": f"App with bundle identifier "
                                                                  f"'{body.get('bundleId')}' unknown"}
            self.screen = BUNDLES[body["bundleId"]]
            return 200, None
        if path == "/url":
            self.screen = "web"
            return 200, None
        if path == "/wda/keys":
            self.typed += "".join(body.get("value") or [])
            return 200, None
        if path in ("/actions", "/wda/keyboard/dismiss"):
            return 200, None
        return 404, {"error": "unknown command", "message": f"{method} {path}"}
