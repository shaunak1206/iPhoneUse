#!/usr/bin/env python3
"""Stand-in for bin/mirrorctl: a tiny scripted phone.

State lives in $FAKE_STATE (JSON). Every call is appended to $FAKE_LOG so the
test can assert on exactly which global coordinates were clicked.
"""

import json
import os
import sys

# Real geometry: 316x696 pt window, phone screen 300x650 pt at (8, 38) inside it.
WIN = {"id": 275, "x": 697.0, "y": 164.0, "w": 316.0, "h": 696.0, "onscreen": True}
CONTENT = {"x": 16, "y": 76, "w": 600, "h": 1300}  # pixels, 2x
PW, PH = 390.0, 844.0

# screen -> list of (text, x, y, w, h) in phone points, and tap transitions
SCREENS = {
    "home": [("Settings", 30, 130, 60, 14), ("Photos", 120, 130, 50, 14), ("Search", 170, 760, 50, 16)],
    "spotlight": [("Search", 40, 80, 60, 18)],
    "settings": [("Settings", 20, 100, 120, 30), ("General", 60, 400, 70, 20), ("Privacy & Security", 60, 450, 160, 20)],
    "general": [("General", 160, 60, 70, 20), ("About", 60, 160, 60, 20), ("iPhone Storage", 60, 330, 130, 20)],
    "storage": [("Instagram", 140, 60, 100, 20), ("Offload App", 130, 400, 120, 20), ("Delete App", 140, 480, 100, 20)],
    "deleted": [("App deleted", 120, 400, 140, 20)],
    "passcode": [("Enter Passcode", 120, 200, 150, 22)],
    "perm": [("Allow \u201cMaps\u201d to use your location?", 70, 300, 250, 20),
             ("Your location is used to show places near you.", 66, 326, 258, 16),
             ("Allow Once", 150, 470, 90, 20), ("Allow While Using App", 110, 514, 170, 20),
             ("Don\u2019t Allow", 150, 558, 90, 20)],
    "maps": [("Search Maps", 40, 760, 120, 18)],
    "confirm_delete": [("Delete \u201cInstagram\u201d?", 120, 330, 150, 20),
                       ("Deleting this app will also delete its data.", 60, 356, 270, 16),
                       ("Cancel", 90, 420, 60, 20), ("OK", 260, 420, 30, 20)],
    "safari": [("Search or enter website name", 66, 780, 260, 18)],
    "web": [("Example Domain", 120, 200, 150, 22)],
    "openprompt": [("Open in \u201cSpotify\u201d?", 120, 380, 150, 20), ("Cancel", 90, 440, 60, 20),
                   ("Open", 255, 440, 45, 20)],
    "spotify": [("Jazz", 60, 140, 40, 18)],
}
TAPS = {
    ("settings", "General"): "general",
    ("general", "iPhone Storage"): "storage",
    ("storage", "Delete App"): "deleted",
    ("home", "Settings"): "settings",
    ("perm", "Allow Once"): "maps",
    ("perm", "Don\u2019t Allow"): "maps",
    ("confirm_delete", "Cancel"): "storage",
    ("confirm_delete", "OK"): "deleted",
    ("openprompt", "Open"): "spotify",
}


def load():
    p = os.environ["FAKE_STATE"]
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return {"screen": "home", "locked": False, "query": "", "screen_access": True, "running": True}


def save(s):
    with open(os.environ["FAKE_STATE"], "w") as f:
        json.dump(s, f)


class Done(Exception):
    def __init__(self, o):
        self.o = o


def out(o):
    raise Done(o)


def item_at(s, gx, gy):
    px = (gx - WIN["x"] - CONTENT["x"] / 2) / (CONTENT["w"] / 2) * PW
    py = (gy - WIN["y"] - CONTENT["y"] / 2) / (CONTENT["h"] / 2) * PH
    for t, x, y, w, h in SCREENS[s["screen"]]:
        if x - 2 <= px <= x + w + 2 and y - 2 <= py <= y + h + 2:
            return t
    return None


def handle(args):
    with open(os.environ["FAKE_LOG"], "a") as f:
        f.write(json.dumps(args) + "\n")
    s = load()
    cmd = args[0]
    if cmd == "state":
        if not s["running"]:
            out({"running": False, "ax_trusted": True, "screen_access": s["screen_access"]})
        if s["locked"]:
            out({"running": True, "ax_trusted": True, "screen_access": s["screen_access"], "active": False,
                 "phone_window": WIN, "view_menu": {"Home Screen": False},
                 "ax": [{"texts": ["iPhone Mirroring Is Locked", "Touch ID or enter the Mac login to continue."],
                         "buttons": []}]})
        out({"running": True, "ax_trusted": True, "screen_access": s["screen_access"], "active": False,
             "phone_window": WIN, "view_menu": {"Home Screen": True}, "ax": []})
    if cmd == "launch":
        s["running"] = True
        save(s)
        out({"ok": True})
    if cmd == "shot":
        a = {args[i]: args[i + 1] for i in range(1, len(args) - 1) if args[i].startswith("--")}
        if "--jpg" in args:
            with open(args[args.index("--jpg") + 1], "wb") as f:
                f.write(b"\xff\xd8\xff\xe0fakejpeg\xff\xd9")
        o = {"window_px_width": 632, "window_px_height": 1392, "width": 632, "height": 1392}
        if "--content" in args:
            o.update(content=CONTENT, width=600, height=1300)
        if "--jpg" in args:
            o["jpg_width"] = int(a.get("--width", 390))
        if "--ocr" in args:
            o["ocr"] = [{"text": t, "conf": 1.0, "x": x / PW, "y": y / PH, "w": w / PW, "h": h / PH}
                        for t, x, y, w, h in SCREENS[s["screen"]]]
        out(o)
    if cmd == "menu":
        if args[1] == "Home Screen":
            s["screen"] = "home"
        elif args[1] == "Spotlight":
            s["screen"], s["query"] = "spotlight", ""
        save(s)
        out({"ok": True})
    if cmd == "type":
        s["query"] += args[1]
        save(s)
        out({"ok": True, "unicode_fallback": []})
    if cmd == "key":
        if args[1] == "return" and s["screen"] == "spotlight" and s["query"].lower() in SCREENS:
            s["screen"] = s["query"].lower()
            save(s)
        elif args[1] == "return" and s["screen"] == "safari":
            s["screen"] = "openprompt" if "spotify:" in s["query"] else "web"
            save(s)
        out({"ok": True})
    if cmd == "click":
        t = item_at(s, float(args[1]), float(args[2]))
        nxt = TAPS.get((s["screen"], t))
        if nxt:
            s["screen"] = nxt
            save(s)
        out({"ok": True, "hit": t})
    if cmd in ("drag", "scroll", "activate", "restore", "warp", "button"):
        out({"ok": True})
    if cmd == "ocr":
        out({"width": 1170, "height": 2532,
             "ocr": [{"text": "SCORE 120", "conf": 0.9, "x": 0.3, "y": 0.1, "w": 0.4, "h": 0.03}]})
    if cmd == "frontmost":
        out({"pid": 4242, "bundle": "com.example.editor"})
    if cmd == "cursor":
        out({"x": 10.0, "y": 20.0})
    out({"error": f"fake: unknown {cmd}"})


def run(args):
    try:
        handle(args)
    except Done as d:
        return d.o
    return {"error": "fake: no output"}


if sys.argv[1:] == ["serve"]:
    for line in sys.stdin:
        print(json.dumps(run(json.loads(line))), flush=True)
else:
    o = run(sys.argv[1:])
    print(json.dumps(o))
    sys.exit(1 if "error" in o else 0)
