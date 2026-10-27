"""Offline suite: drives every phone tool through a real FastMCP client against
tests/fake_mirrorctl.py. No phone, no permissions needed.

    .venv/bin/python tests/test_smoke.py
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="iphone-mcp-test-"))
os.environ["IPHONE_MIRRORCTL"] = str(HERE / "fake_mirrorctl.py")
os.environ["FAKE_STATE"] = str(TMP / "state.json")
os.environ["FAKE_LOG"] = str(TMP / "calls.log")
os.environ["IPHONE_JOURNAL"] = "0"  # never write to the real GitHub journal
sys.path.insert(0, str(HERE.parent))

from fastmcp import Client  # noqa: E402

import iphone_mcp.wda  # noqa: E402
from iphone_mcp import screen  # noqa: E402
from iphone_mcp.bridge import Screen, TextItem  # noqa: E402

iphone_mcp.wda.URL_FILE = TMP / "wda.url"  # never pick up a real runner's URL
iphone_mcp.wda.LAST_FILE = TMP / "wda.last"  # nor keep a real one alive
iphone_mcp.wda.RUNNER = "no-such-process-iphone-mcp-test"  # and never stop a real one
os.environ.pop("IPHONE_WDA_URL", None)
import iphone_mcp.dashboard  # noqa: E402
import iphone_mcp.phone  # noqa: E402
from iphone_mcp import server  # noqa: E402

iphone_mcp.phone.ACTIVITY_FILE = TMP / "activity.jsonl"  # keep test actions out of the real activity log
iphone_mcp.dashboard.ensure_running = lambda wait=10: "http://127.0.0.1:8780"  # no real dashboard process
from iphone_mcp.server import build  # noqa: E402

from fake_wda import FakeWda  # noqa: E402


def _no_runner(wait: float = 0) -> str:
    raise server.BridgeError("iPhone not reachable (test)")


server.phone.wda.start = _no_runner  # never launch the real WebDriverAgent from the tests

# phone screen origin in global points, and points per phone point
ORIGIN = (697.0 + 8, 164.0 + 38)
SCALE = 300 / 390
failures: list[str] = []


def check(cond: bool, label: str, detail: object = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"\n         {detail}"))
    if not cond:
        failures.append(label)


def set_state(**kw) -> None:
    p = Path(os.environ["FAKE_STATE"])
    s = json.loads(p.read_text()) if p.exists() else {
        "screen": "home", "locked": False, "query": "", "screen_access": True, "running": True}
    s.update(kw)
    p.write_text(json.dumps(s))


def calls(cmd: str) -> list[list[str]]:
    log = Path(os.environ["FAKE_LOG"])
    if not log.exists():
        return []
    return [c for c in map(json.loads, log.read_text().splitlines()) if c[0] == cmd]


def reset_log() -> None:
    Path(os.environ["FAKE_LOG"]).write_text("")


def text(r) -> str:
    return "\n".join(c.text for c in r.content if getattr(c, "type", "") == "text")


def unit_tests() -> None:
    print("screen helpers")
    s = Screen(390, 844, [
        TextItem("Wi-Fi", 1, 20, 300, 40, 20), TextItem("Home", 1, 300, 302, 50, 16),
        TextItem("Bluetooth", 1, 20, 360, 80, 20), TextItem("Delete App", 1, 140, 480, 100, 20),
    ])
    rows = screen.lines(s)
    check([len(r) for r in rows] == [2, 1, 1], "rows group by vertical center", rows)
    d = screen.describe(s)
    check("(40,310) Wi-Fi   |   (325,310) Home" in d, "describe tags tap points per row", d)
    check(screen.find(s, "wifi")[0].text == "Wi-Fi" and screen.find(s, "wi-fi", exact=True)[0].text == "Wi-Fi"
          and screen.find(s, "wifi", exact=True) == [], "find is exact/substring/fuzzy")
    check(screen.find(s, "Bluetoth")[0].text == "Bluetooth", "fuzzy find tolerates OCR typos")
    check(screen.risky_label(screen.at(s, 190, 490)) == "Delete App", "risky label detected under point")
    check(screen.risky_label(screen.at(s, 40, 310)) is None, "benign label passes")
    s2 = Screen(390, 844, [TextItem("Enter Passcode", 1, 100, 200, 150, 20)])
    check("Only the user may enter it" in screen.describe(s2), "passcode screens flagged")

    print("prompt detection")
    perm = Screen(390, 844, [
        TextItem("Allow \u201cMaps\u201d to use your location?", 1, 70, 300, 250, 20),
        TextItem("Your location is used to show places near you.", 1, 66, 326, 258, 16),
        TextItem("Allow Once", 1, 150, 470, 90, 20), TextItem("Allow While Using App", 1, 110, 514, 170, 20),
        TextItem("Don\u2019t Allow", 1, 150, 558, 90, 20)])
    p = screen.prompt(perm)
    check(p is not None and p.title.startswith("Allow \u201cMaps\u201d") and "places near you" in p.message
          and [b.text for b in p.buttons] == ["Allow Once", "Allow While Using App", "Don\u2019t Allow"],
          "permission prompt: title, message, stacked buttons", p)
    check("PROMPT on screen" in screen.describe(perm), "describe flags the prompt")
    check(screen.needs_confirm(p.buttons[0], p) and screen.needs_confirm(p.buttons[2], p) is None,
          "granting access needs confirm, declining does not")
    dele = Screen(390, 844, [
        TextItem("Delete \u201cInstagram\u201d?", 1, 120, 330, 150, 20),
        TextItem("Deleting this app will also delete its data.", 1, 60, 356, 270, 16),
        TextItem("Cancel", 1, 90, 420, 60, 20), TextItem("OK", 1, 260, 420, 30, 20)])
    p = screen.prompt(dele)
    ok, cancel = screen.pick(p.buttons, "ok"), screen.pick(p.buttons, "cancel")
    check(p and ok and cancel and screen.needs_confirm(ok, p) and screen.needs_confirm(cancel, p) is None,
          "'OK' to a destructive question needs confirm, 'Cancel' does not", p)
    home = Screen(390, 844, [TextItem(t, 1, x, 130, 50, 14) for t, x in
                             (("Mail", 20), ("Calendar", 110), ("Photos", 200), ("Camera", 290))]
                  + [TextItem(t, 1, x, 220, 50, 14) for t, x in (("Clock", 20), ("Settings", 110))])
    check(screen.prompt(home) is None, "home-screen labels are not a prompt")
    row = Screen(390, 844, [TextItem("Allow Notifications", 1, 20, 300, 160, 20)])
    check(screen.needs_confirm(row.items[0], screen.prompt(row)) is None,
          "a Settings row named Allow is not a grant")
    nav = Screen(390, 844, [TextItem("Cancel", 1, 16, 60, 60, 20), TextItem("Done", 1, 330, 60, 44, 20)])
    check(screen.prompt(nav) is None, "nav-bar Cancel/Done is not a prompt")


async def tool_tests() -> None:
    mcp = build()
    async with Client(mcp) as c:
        tools = {t.name for t in await c.list_tools()}
        print(f"tools ({len(tools)})")
        expected = {"phone_status", "phone_connect", "phone_screenshot", "phone_read_screen", "phone_zoom",
                    "phone_find", "phone_tap", "phone_long_press", "phone_swipe", "phone_scroll", "phone_type",
                    "phone_key", "phone_go", "phone_open_app", "phone_wait", "device_info", "device_apps",
                    "phone_alert", "phone_open_url", "phone_do", "phone_disconnect", "phone_watch", "phone_goto",
                    "phone_storage_report", "phone_journal", "phone_note", "phone_dismiss", "phone_keys"}
        check(expected <= tools, "all tools registered", expected - tools)

        set_state(screen="home")
        r = text(await c.call_tool("phone_status", {}))
        check("Ready: yes" in r and "390 x 844 pt" in r, "status ready + phone size", r)

        r = text(await c.call_tool("phone_read_screen", {}))
        check("(60,137) Settings" in r, "read_screen lists items with centers", r)

        reset_log()
        r = text(await c.call_tool("phone_open_app", {"name": "Settings"}))
        check("Opened 'Settings'" in r and "General" in r, "open_app via Spotlight lands in app", r)
        check(["menu", "Spotlight"] in calls("menu") and calls("type")[0][1] == "Settings", "spotlight + typed name")

        reset_log()
        r = text(await c.call_tool("phone_tap", {"text": "general"}))
        check("Tapped 'General'" in r and "iPhone Storage" in r, "tap by text navigates", r)
        click = calls("click")[0]
        gx, gy = float(click[1]), float(click[2])
        check(abs(gx - (ORIGIN[0] + 95 * SCALE)) < 1 and abs(gy - (ORIGIN[1] + 410 * 650 / 844)) < 1,
              "phone points map onto the window", click)
        check(calls("activate") and calls("restore")[0][1] == "4242" and calls("warp")[0][1:] == ["10.0", "20.0"],
              "focus and cursor handed back to the user", (calls("activate"), calls("restore"), calls("warp")))

        await c.call_tool("phone_tap", {"text": "iPhone Storage"})
        reset_log()
        r = text(await c.call_tool("phone_tap", {"text": "Delete App"}))
        check(r.startswith("Refused") and not calls("click"), "destructive tap by text refused", r)
        r = text(await c.call_tool("phone_tap", {"x": 190, "y": 490}))
        check(r.startswith("Refused") and not calls("click"), "destructive tap by coordinates refused", r)
        r = text(await c.call_tool("phone_tap", {"text": "Offload App"}))
        check(r.startswith("Refused"), "offload also needs confirm", r)
        r = text(await c.call_tool("phone_tap", {"text": "Delete App", "confirm": True}))
        check("App deleted" in r and calls("click"), "confirm=True lets it through", r)

        r = text(await c.call_tool("phone_tap", {"text": "Nonexistent"}))
        check("no text matching" in r, "missing text explained with current screen", r)

        r = await c.call_tool("phone_screenshot", {})
        kinds = [getattr(x, "type", "") for x in r.content]
        check(kinds[:2] == ["image", "text"], "screenshot returns image + text", kinds)
        shot = calls("shot")[-1]
        check(shot[shot.index("--width") + 1] == "390" and "--content" in shot, "screenshot is phone screen, 1px = 1pt", shot)

        reset_log()
        await c.call_tool("phone_scroll", {"direction": "down", "amount": 0.5})
        d = calls("drag")[0]
        check(float(d[4]) < float(d[2]), "scroll down = finger moves up", d)
        reset_log()
        await c.call_tool("phone_scroll", {"direction": "up", "amount": 0.9})
        ds = calls("drag")
        ys = [float(v) for d in ds for v in (d[2], d[4])]
        top_edge = ORIGIN[1] + 0.12 * 650 - 1
        check(len(ds) == 2 and min(ys) >= top_edge, "long scroll split, never starts at the top edge", ds)
        reset_log()
        await c.call_tool("phone_swipe", {"direction": "left"})
        d = calls("drag")[0]
        check(float(d[3]) < float(d[1]) and d[2] == d[4], "swipe left = finger moves left", d)
        reset_log()
        await c.call_tool("phone_go", {"where": "back"})
        d = calls("drag")[0]
        check(abs(float(d[1]) - (ORIGIN[0] + 1 * SCALE)) < 1, "back gesture starts at left edge", d)
        reset_log()
        r = text(await c.call_tool("phone_go", {"where": "home"}))
        check(["menu", "Home Screen"] in calls("menu") and "Photos" in r, "home via View menu", r)

        reset_log()
        r = text(await c.call_tool("phone_type", {"text": "hello", "submit": True}))
        check(calls("type")[0][1] == "hello" and calls("key")[0][1] == "return", "type + submit", calls("key"))

        r = text(await c.call_tool("phone_find", {"text": "Photos"}))
        check("Found 1 match" in r and "(145,137)" in r, "find returns tap point", r)
        r = text(await c.call_tool("phone_wait", {"for_text": "Photos", "timeout": 2}))
        check("appeared" in r, "wait_for text", r)

        r = await c.call_tool("phone_zoom", {"x": 0, "y": 0, "width": 195, "height": 100})
        crop = calls("shot")[-1]
        check(crop[crop.index("--crop") + 1] == "0.00000,0.00000,0.50000,0.11848", "zoom crop normalized", crop)

        print("prompts")
        set_state(screen="perm")
        r = text(await c.call_tool("phone_read_screen", {}))
        check("PROMPT on screen" in r and "Allow Once" in r, "read_screen flags the prompt", r)
        r = text(await c.call_tool("phone_alert", {}))
        check(r.startswith("PROMPT on screen") and "Don\u2019t Allow" in r, "alert with no button describes it", r)
        reset_log()
        r = text(await c.call_tool("phone_alert", {"button": "allow once"}))
        check(r.startswith("Refused") and "grants access" in r and not calls("click"), "granting access refused", r)
        r = text(await c.call_tool("phone_tap", {"text": "Allow While Using App"}))
        check(r.startswith("Refused") and not calls("click"), "tap on a grant answer is refused too", r)
        r = text(await c.call_tool("phone_alert", {"button": "dismiss"}))
        check("with 'Don\u2019t Allow'" in r and "Search Maps" in r, "dismiss picks the safe answer", r)
        set_state(screen="perm")
        r = text(await c.call_tool("phone_alert", {"button": "Allow Once", "confirm": True}))
        check("with 'Allow Once'" in r and "Search Maps" in r, "confirm=True grants", r)

        set_state(screen="confirm_delete")
        reset_log()
        r = text(await c.call_tool("phone_alert", {"button": "OK"}))
        check(r.startswith("Refused") and "confirms" in r and not calls("click"), "OK to 'Delete ...?' refused", r)
        r = text(await c.call_tool("phone_alert", {"button": "Nope"}))
        check("no button 'Nope'" in r and "Cancel" in r, "unknown answer lists the real buttons", r)
        r = text(await c.call_tool("phone_tap", {"text": "Cancel"}))
        check("Tapped 'Cancel'" in r and "Offload App" in r, "cancel goes through", r)
        set_state(screen="general")
        r = text(await c.call_tool("phone_alert", {"button": "OK"}))
        check("no alert" in r, "no prompt explained", r)

        print("links")
        r = text(await c.call_tool("phone_open_url", {"url": "spotify:search:jazz"}))
        check("Opened spotify:search:jazz in Safari" in r and "PROMPT on screen" in r and "Open in" in r,
              "deep link through Safari raises the open-in-app prompt", r)
        check(["key", "l", "--mods", "cmd"] in calls("key"), "address bar focused with cmd+L", calls("key"))
        r = text(await c.call_tool("phone_alert", {"button": "Open"}))
        check("Jazz" in r, "open-in-app prompt answered", r)
        r = text(await c.call_tool("phone_open_url", {"url": "example.com"}))
        check("Opened https://example.com" in r and "Example Domain" in r, "bare domains get https://", r)

        print("batches")
        reset_log()
        r = text(await c.call_tool("phone_do", {"steps": [
            {"open_app": "Settings"}, {"tap": "General"}, {"tap": "iPhone Storage"},
            {"wait_for": "Delete App", "timeout": 2}]}))
        check("1. Opened 'Settings'" in r and "3. Tapped 'iPhone Storage'" in r and "4. 'Delete App' appeared" in r
              and "Offload App" in r, "batch runs every step and returns the final screen", r)
        check(len(calls("frontmost")) == 1 and len(calls("restore")) == 1, "batch hands focus back once",
              (calls("frontmost"), calls("restore")))
        reset_log()
        r = text(await c.call_tool("phone_do", {"steps": [{"tap": "Delete App"}, {"go": "home"}]}))
        check("1. tap stopped: Refused" in r and "not run" in r and not calls("click") and not calls("menu"),
              "batch stops at a refused step", r)
        check(len(calls("restore")) == 1, "focus handed back after a stopped batch", calls("restore"))
        r = text(await c.call_tool("phone_do", {"steps": [{"tap": "Delete App", "confirm": True}]}))
        check("1. Tapped 'Delete App'" in r and "App deleted" in r, "confirm per step", r)
        r = text(await c.call_tool("phone_do", {"steps": [{"fly": "away"}]}))
        check("needs exactly one of" in r, "unknown step explained", r)
        r = text(await c.call_tool("phone_do", {"steps": [{"tap": [195, 400], "bogus": 1}]}))
        check("bad option" in r, "bad step option explained", r)

        set_state(screen="passcode")
        r = text(await c.call_tool("phone_read_screen", {}))
        check("Only the user may enter it" in r, "passcode screen flagged through tool", r)

        set_state(locked=True)
        r = text(await c.call_tool("phone_tap", {"x": 10, "y": 10}))
        check("Touch ID" in r and "never type the password" in r, "locked mirroring explained", r)
        r = text(await c.call_tool("phone_connect", {"wait_seconds": 1}))
        check(r.startswith("Not ready") and "Touch ID" in r, "connect reports lock", r)
        set_state(locked=False, screen_access=False)
        r = text(await c.call_tool("phone_read_screen", {}))
        check("Screen Recording" in r, "missing screen permission explained", r)
        set_state(screen_access=True, running=False)
        r = text(await c.call_tool("phone_connect", {"wait_seconds": 2}))
        check(r.startswith("Connected") and calls("launch"), "connect launches mirroring", r)


async def wda_tests() -> None:
    print("webdriveragent engine")
    fake = FakeWda()
    os.environ["IPHONE_WDA_MJPEG_PORT"] = str(fake.mjpeg_port)
    server.phone.wda._url = fake.url
    server.phone.wda._ok_until = 0
    set_state(screen="home", locked=False, running=True, screen_access=True)
    try:
        async with Client(build()) as c:
            r = text(await c.call_tool("phone_status", {}))
            check(f"WebDriverAgent: up at {fake.url}" in r and "Ready: yes, through WebDriverAgent" in r
                  and "390 x 844 pt" in r, "status reports WebDriverAgent ready", r)
            r = text(await c.call_tool("phone_connect", {}))
            check(r.startswith("Connected through WebDriverAgent") and "[app] Settings" in r,
                  "connect uses the running agent; icons tagged", r)

            r = text(await c.call_tool("phone_open_app", {"name": "Settings"}))
            check(fake.called("/wda/apps/launch") == [{"bundleId": "com.apple.Preferences"}]
                  and "com.apple.Preferences" in r, "Apple app opened by bundle id", r)
            check("[switch] Airplane Mode [off]" in r and "General" in r and "Hidden row" not in r
                  and "(20,635)" not in r, "tree readout: switch state, offscreen rows and keyboard hidden", r)
            check("(330,202) [switch] Airplane Mode [off]" in r and r.count("Airplane Mode") == 1,
                  "switch row: one item, tap point on the toggle", r)
            check("[button] Bluetooth, On" in r and "(100,241) Bluetooth" not in r and "(325,242) On" not in r,
                  "row button replaces its child labels", r)
            check(r.count("General") == 1, "a label mostly below the screen edge is dropped", r)
            r = text(await c.call_tool("phone_tap", {"text": "General"}))
            check(fake.called("/wda/tap")[-1] == {"x": 100.0, "y": 251.0} and "iPhone Storage" in r,
                  "tap by text taps the element center", (fake.called("/wda/tap"), r))
            await c.call_tool("phone_tap", {"text": "iPhone Storage"})
            r = text(await c.call_tool("phone_tap", {"text": "Delete App"}))
            check(r.startswith("Refused") and len(fake.called("/wda/tap")) == 2, "risky tap refused under WDA", r)
            r = text(await c.call_tool("phone_tap", {"text": "Delete App", "confirm": True}))
            check("PROMPT on screen" in r and "Delete \u201cInstagram\u201d?" in r and "data." in r,
                  "in-app alert read from the tree", r)
            r = text(await c.call_tool("phone_alert", {"button": "Delete"}))
            check(r.startswith("Refused"), "deleting through the alert needs confirm", r)
            r = text(await c.call_tool("phone_alert", {"button": "dismiss"}))
            check("with 'Cancel'" in r and "Offload App" in r, "dismiss taps Cancel on the alert", r)

            fake.screen = "maps"
            fake.sysalert = {"text": "Allow \u201cMaps\u201d to use your location?\nShows places near you.",
                             "buttons": ["Allow Once", "Allow While Using App", "Don\u2019t Allow"], "then": "maps"}
            r = text(await c.call_tool("phone_read_screen", {}))
            check("PROMPT on screen" in r and "Allow While Using App" in r and "[search field] Search Maps" in r,
                  "system permission prompt read through the alert API", r)
            r = text(await c.call_tool("phone_alert", {"button": "Allow Once"}))
            check(r.startswith("Refused") and not fake.called("/alert/accept"), "grant refused without confirm", r)
            r = text(await c.call_tool("phone_alert", {"button": "dismiss"}))
            check(fake.called("/alert/accept") == [{"name": "Don\u2019t Allow"}] and fake.sysalert is None,
                  "system alert answered by name", (fake.called("/alert/accept"), r))

            r = text(await c.call_tool("phone_open_url", {"url": "example.com"}))
            check(fake.called("/url") == [{"url": "https://example.com"}] and "Example Domain" in r,
                  "URL opened directly by the agent", r)
            r = text(await c.call_tool("phone_type", {"text": "hi", "submit": True}))
            check(fake.typed == "hi\n", "typing goes through /wda/keys", fake.typed)
            r = text(await c.call_tool("phone_key", {"key": "a", "modifiers": "cmd"}))
            check("need iPhone Mirroring" in r, "modifier keys explained", r)
            await c.call_tool("phone_scroll", {"direction": "down", "amount": 0.5})
            moves = [a for a in fake.called("/actions")[-1]["actions"][0]["actions"] if a["type"] == "pointerMove"]
            check(moves[1]["y"] < moves[0]["y"], "scroll down = finger moves up (W3C touch actions)", moves)
            pause = [a for a in fake.called("/actions")[-1]["actions"][0]["actions"] if a["type"] == "pause"][-1]
            check(pause["duration"] >= 150 and moves[0]["y"] - moves[1]["y"] == 422 + 20,
                  "scroll pauses before lifting (no fling) and covers touch slop", (moves, pause))
            r = text(await c.call_tool("phone_go", {"where": "home"}))
            check(fake.called("/wda/homescreen") and "[app] Photos" in r, "home button", r)

            fake.screen = "login"
            r = text(await c.call_tool("phone_read_screen", {}))
            check("Password [password field]" in r and "Only the user may enter it" in r, "password field flagged", r)
            fake.screen = "game"
            r = text(await c.call_tool("phone_read_screen", {}))
            check("SCORE 120" in r, "empty tree falls back to OCR of the screenshot", r)
            r = text(await c.call_tool("phone_watch", {"open_on_mac": False}))
            check("iPhone Live is running at http://127.0.0.1:8780" in r, "phone_watch points at the dashboard", r)
            r = await c.call_tool("phone_screenshot", {"include_text": False})
            check([getattr(x, "type", "") for x in r.content] == ["image"], "screenshot from the agent", r.content)

            fake.screen, fake.session = "springboard", "S2"  # the runner restarted: old session is gone
            r = text(await c.call_tool("phone_do", {"steps": [{"open_app": "Settings"}, {"tap": "General"}]}))
            check("2. Tapped 'General'" in r and "iPhone Storage" in r, "batch under WDA; session re-created", r)

            r = text(await c.call_tool("phone_open_app", {"name": "Some Game"}))
            check(fake.typed.endswith("Some Game\n"), "unknown app goes through Spotlight", (fake.typed, r))

            fake.screen = "general"  # Settings left on some other page
            r = text(await c.call_tool("phone_goto", {"place": "storage"}))
            check(fake.called("/wda/apps/terminate")[-1] == {"bundleId": "com.apple.Preferences"}
                  and "Went to Settings > storage" in r and "Delete App" in r,
                  "goto relaunches Settings at the top and taps through to the page", r)
            r = text(await c.call_tool("phone_goto", {"place": "nowhere"}))
            check("unknown place" in r and "storage" in r, "unknown place lists the known ones", r)

            print("dashboard")
            import warnings
            warnings.filterwarnings("ignore", message=".*httpx.*starlette.testclient.*")
            from starlette.testclient import TestClient
            web = TestClient(iphone_mcp.dashboard.app(server.phone), base_url="http://127.0.0.1:8780")
            r1 = web.get("/")
            check(r1.status_code == 200 and "iPhone Live" in r1.text and "iphone-agent-journal" in r1.text,
                  "dashboard page served", r1.status_code)
            st = web.get("/api/status").json()
            check(st.get("engine") == "wda" and st.get("ready") is True, "status API", st)
            r2 = web.post("/api/do", json={"steps": [{"go": "home"}]})
            check(r2.status_code == 403, "acting without the X-iPhone-Agent header is refused", r2.status_code)
            r3 = web.post("/api/do", json={"steps": [{"go": "home"}]}, headers={"X-iPhone-Agent": "1"})
            check(r3.status_code == 200 and "1. Went home." in r3.json()["result"], "do API acts with the header",
                  r3.text[:200])
            r4 = TestClient(iphone_mcp.dashboard.app(server.phone), base_url="http://evil.example").get("/api/status")
            check(r4.status_code == 403, "foreign Host header refused (DNS rebinding)", r4.status_code)
            r5 = web.post("/api/do", json={"steps": [{"tap": "Nope"}]}, headers={"X-iPhone-Agent": "1"})
            check("stopped" in r5.json().get("result", ""), "failed step reported, not raised", r5.text[:200])
            acts = web.get("/api/activity").json()
            check(any(a["action"] == "Went home." for a in acts) and any(not a["ok"] for a in acts),
                  "activity feed has actions and stops", acts[-3:])
            os.environ["IPHONE_WDA_MJPEG_PORT"] = "9"  # nothing listens there
            r6 = web.get("/stream")
            check(r6.status_code == 503, "stream says when the phone's stream is unreachable", r6.status_code)
            os.environ["IPHONE_WDA_MJPEG_PORT"] = str(fake.mjpeg_port)

            print("speed tools")
            fake.screen = "chrome"
            r = text(await c.call_tool("phone_read_screen", {}))
            check("Inactive tabs" not in r and "Tabs not used" not in r and "Play" in r,
                  "a full-screen layer under the page (Chrome's tab grid) is left out", r)
            fake.screen = "home2"
            r = text(await c.call_tool("phone_read_screen", {}))
            check("[app] Weather" in r and "[app] Home" in r, "an empty layer on top hides nothing", r)
            fake.screen, fake.ad_reads = "ad", 0
            r = text(await c.call_tool("phone_dismiss", {}))
            check("Closed 'Continue to Wordle'" in r and "n present" in r, "dismiss waits out an ad countdown", r)
            fake.screen = "cookies"
            r = text(await c.call_tool("phone_dismiss", {}))
            check("Closed 'Reject'" in r and "Example Domain" in r, "cookie banner gets Reject, never Accept", r)
            r = text(await c.call_tool("phone_dismiss", {}))
            check(r.startswith("Nothing to dismiss"), "nothing in the way, nothing tapped", r)
            fake.screen, fake.pressed = "wordle", []
            r = text(await c.call_tool("phone_keys", {"keys": "crane", "then": "enter"}))
            check(fake.pressed == ["c absent", "r absent", "a absent", "n present", "e absent", "enter"]
                  and "Pressed crane [enter]" in r, "keys taps a word on a page keyboard, labels with states too",
                  (fake.pressed, r[:120]))
            reads = len(fake.called("/source"))
            fake.pressed = []
            r = text(await c.call_tool("phone_do", {"steps": [{"keys": "zq"}, {"taps": ["add x", "enter"]}]}))
            check(fake.pressed == ["add z", "add q", "add x", "enter"] and "2. Tapped 'add x', 'enter'" in r,
                  "keys and taps steps in a batch", (fake.pressed, r[:200]))
            check(len(fake.called("/source")) - reads <= 3, "each batch of taps costs one screen read",
                  len(fake.called("/source")) - reads)
            n_actions = len(fake.called("/actions"))
            await c.call_tool("phone_keys", {"keys": "crane", "then": "enter"})
            check(len(fake.called("/actions")) - n_actions == 1, "a whole word goes in one touch request",
                  len(fake.called("/actions")) - n_actions)
            r = text(await c.call_tool("phone_keys", {"keys": "7"}))
            check("no on-screen key for '7'" in r, "missing key explained", r[:80])

            print("frames, marks, recording")
            import time as _t
            w = server.phone.wda
            w.frames()
            for _ in range(50):
                if w._frames.fresh():
                    break
                _t.sleep(0.05)
            t0 = _t.time()
            png = w._png()
            check(png[:2] == b"\xff\xd8" and _t.time() - t0 < 0.05, "screenshot is the newest live frame (no request)",
                  _t.time() - t0)
            fake.moving = 0
            waited = w.settle()
            check(0.1 <= waited < 0.5, "settle returns soon once the picture holds still", waited)
            fake.moving = 30  # about one second of changing frames
            waited = w.settle()
            check(waited >= 0.8, "settle waits while the picture changes (capped)", waited)
            fake.screen = "settings"
            r = await c.call_tool("phone_screenshot", {"marks": True})
            kinds = [getattr(x, "type", "") for x in r.content]
            legend = r.content[1].text if len(r.content) > 1 else ""
            check(kinds == ["image", "text"] and "#1 " in legend and "General" in legend, "marked screenshot + legend",
                  legend[:200])
            n = next(int(line.split()[0][1:]) for line in legend.splitlines() if line.endswith("General"))
            r = text(await c.call_tool("phone_tap", {"text": f"#{n}"}))
            check(f"Tapped #{n} 'General'" in r and "iPhone Storage" in r, "tap by mark number", r[:160])
            r = text(await c.call_tool("phone_tap", {"text": "#999"}))
            check("no mark #999" in r, "unknown mark explained", r[:80])
            import shutil
            if shutil.which("ffmpeg") or os.path.exists("/opt/homebrew/bin/ffmpeg"):
                iphone_mcp.phone.RECORDINGS = TMP / "rec"
                iphone_mcp.phone.RECORD_PID = TMP / "record.pid"
                r = text(await c.call_tool("phone_record", {"action": "start", "name": "t"}))
                await asyncio.sleep(1.5)
                r2 = text(await c.call_tool("phone_record", {"action": "stop"}))
                f = TMP / "rec" / "t.mp4"
                check(r.startswith("Recording to") and r2.startswith("Saved") and f.exists() and f.stat().st_size > 1000,
                      "records the live screen to an MP4", (r, r2, f.exists() and f.stat().st_size))
                r = text(await c.call_tool("phone_record", {"action": "stop"}))
                check("not recording" in r, "stop when not recording explained", r)

            fake.locked = True
            server.phone.wda._ok_until = 0
            r = text(await c.call_tool("phone_status", {}))
            check("phone LOCKED" in r and "Ready: yes, through iPhone Mirroring" in r,
                  "locked phone falls back to Mirroring", r)
            r = text(await c.call_tool("phone_connect", {"engine": "wda"}))
            check("phone is locked" in r and "Face ID" in r, "connect(engine=wda) explains the lock", r)
            iphone_mcp.wda.URL_FILE.write_text(fake.url)
            r = text(await c.call_tool("phone_disconnect", {}))
            check(r.startswith("Stopped WebDriverAgent") and fake.shut and not iphone_mcp.wda.URL_FILE.exists(),
                  "disconnect ends the test session through /wda/shutdown", r)
    finally:
        fake.stop()
        server.phone.wda._url = None
        server.phone.wda._ok_until = 0


def storage_and_journal_tests() -> None:
    print("storage report")
    from iphone_mcp import journal, storage
    files = [["/DCIM/100APPLE/IMG_1.HEIC", 3_000_000, "2022-05-01"], ["/DCIM/100APPLE/IMG_1.MOV", 2_500_000, "2022-05-01"],
             ["/DCIM/100APPLE/IMG_2.MOV", 900_000_000, "2023-01-01"], ["/DCIM/100APPLE/IMG_3.PNG", 400_000, "2026-01-01"],
             ["/DCIM/101APPLE/IMG_9.MOV", 900_000_000, "2023-01-02"], ["/PhotoData/Photos.sqlite", 1_000_000_000, "2026-10-01"]]
    m = storage.analyze_media(files)
    cr = m["camera_roll"]
    check(cr["live_motion"] == {"count": 1, "bytes": 2_500_000} and cr["videos"]["count"] == 2
          and cr["screenshots"]["count"] == 1 and cr["same_size_copies_bytes"] == 900_000_000
          and m["photos_caches"] == 1_000_000_000, "media analysis: live motion, videos, shots, copies", cr)
    r = {"device": "Test", "ios": "26", "capacity": 128e9, "free": 2e9, "seconds": 1, "media": m, "apps": [
        {"name": "Spotify", "bundle_id": "com.spotify.client", "app_bytes": 3e8, "data_bytes": 3.2e9, "total_bytes": 3.5e9},
        {"name": "Brawl Stars", "bundle_id": "com.supercell.laser", "app_bytes": 1.9e9, "data_bytes": 1e8,
         "total_bytes": 2e9}]}
    r["suggestions"] = storage.suggestions(r)
    text = storage.format_report(r)
    check("2.00 GB free" in text and "Spotify > Settings > Storage" in text and "Offload App" in text
          and "Live Photo motion" in text, "report text with app-specific tips", text)

    print("journal")
    puts: dict[str, str] = {}

    def fake_gh(*args, body=None):
        import base64
        path = args[-1].split("/contents/", 1)[-1] if "/contents/" in args[-1] else args[-1]
        if body is not None:
            puts[path] = base64.b64decode(body["content"]).decode()
            return {}
        if path in puts:
            return {"content": base64.b64encode(puts[path].encode()).decode(), "sha": "x"}
        if args[-1].endswith(journal.REPO):
            return {"visibility": "private"}
        raise RuntimeError("404")

    old_gh, old_act, old_off, old_on = journal._gh, journal.ACTIVITY, journal.OFFSET, os.environ.get("IPHONE_JOURNAL")
    journal._gh, journal.ACTIVITY, journal.OFFSET = fake_gh, TMP / "j-activity.jsonl", TMP / "j.offset"
    os.environ["IPHONE_JOURNAL"] = "1"
    try:
        journal.ACTIVITY.write_text(json.dumps({"t": 1761540000, "action": "Went home.", "ok": True}) + "\n"
                                    + json.dumps({"t": 1761540005, "action": "tap stopped: no text", "ok": False}) + "\n")
        path = journal.flush("session")
        body = puts.get(path or "", "")
        check(bool(path) and path.startswith("runs/") and "2 actions, 1 stopped" in body and "**stopped**" in body,
              "flush uploads one run file per session", (path, body))
        check(journal.flush("session") is None, "flush uploads nothing twice")
        journal.note("first lesson")
        journal.note("second lesson")
        check(puts["NOTES.md"].index("first lesson") < puts["NOTES.md"].index("second lesson"),
              "notes append, newest last", puts.get("NOTES.md"))
        journal.save_report(r, text)
        check("history.csv" in " ".join(puts) and "2.00,128.00" in puts["reports/storage/history.csv"],
              "storage history row", puts.get("reports/storage/history.csv"))
    finally:
        journal._gh, journal.ACTIVITY, journal.OFFSET = old_gh, old_act, old_off
        os.environ["IPHONE_JOURNAL"] = old_on or "0"


def main() -> int:
    unit_tests()
    storage_and_journal_tests()
    asyncio.run(tool_tests())
    asyncio.run(wda_tests())
    print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all passed'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
