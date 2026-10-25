# iPhoneMCP

Computer use for a real iPhone, driven from a Mac. Claude (or any agent, script
or app) can see the phone's screen, tap, type, open apps and Settings pages,
answer prompts, and report where the storage went. Nothing extra on the phone
beyond Apple's own UI-testing runner.

One core, four ways in:

| | For | How |
|---|---|---|
| **MCP server** | Claude Code and other agents | `phone_*` tools; `skills/iphone/SKILL.md` teaches the fast way to use them |
| **`iphone` CLI** | your terminal and scripts | `iphone goto storage`, `iphone do '[...]'`, `iphone storage --deep` |
| **iPhone Live** | watching and other apps | `iphone live` opens http://127.0.0.1:8780: the phone's screen live, Connect/Stop, every agent action, storage reports; the same URL serves an HTTP API |
| **Run journal** | future sessions | each session's actions, storage reports over time and notes, in a private GitHub repo |

```
Claude ──MCP──▶ iphone_mcp (Python)
                 ├─ WebDriverAgent ─HTTP :8100 (Wi-Fi)─▶ runner app on the phone (phone unlocked, in hand)
                 │     accessibility tree · taps/gestures · launch by bundle id · alerts · URLs
                 ├─ mirrorctl serve (Swift) ─▶ iPhone Mirroring window (phone locked, nearby)
                 │     ScreenCaptureKit capture · Vision OCR · CGEvent mouse/keys · AX menus
                 └─ pymobiledevice3 ─USB/Wi-Fi lockdown─▶ storage, apps, battery, model
```

## Two engines

The tools are the same either way; `phone_status` says which one is driving.

| | WebDriverAgent (preferred) | iPhone Mirroring (fallback) |
|---|---|---|
| Phone state | unlocked, can be in your hand | locked, near the Mac |
| Reads the screen from | the accessibility tree (exact names, icon-only buttons, switch states) | OCR of the mirrored window |
| Opens apps | by bundle id (instant), Spotlight for unknown names | Spotlight |
| System alerts | WebDriverAgent's alert API | layout guess + tap |
| Needs | Xcode, free Apple ID signing, Developer Mode, `scripts/wda_start.sh` running | nothing on the phone |

WebDriverAgent ([appium/WebDriverAgent](https://github.com/appium/WebDriverAgent),
pinned in `vendor/`) is a UI-test runner: it exists only while an `xcodebuild
test-without-building` process on the Mac keeps it alive. `phone_connect`
starts that in the background (`scripts/wda_start.sh`, log in `build/wda.log`,
URL in `build/wda.url`). It outlives the MCP server, so later sessions reuse it.
Free signing lasts 7 days; the start script re-signs when under 2 days remain.

### WebDriverAgent setup (one time)

```bash
git clone --depth 1 --branch v16.15.3 https://github.com/appium/WebDriverAgent.git vendor/WebDriverAgent
scripts/wda_start.sh --build   # signs with your Personal Team as com.shaunakkapur.WebDriverAgentRunner
```

Then plug the phone in once and call `phone_connect` (or run `scripts/wda_start.sh`).
The first launch may ask you to trust the developer on the phone (Settings >
General > VPN & Device Management). After the first cable start, the phone is
reachable over the same Wi-Fi. `IPHONE_ENGINE=mirror|wda` forces an engine;
`IPHONE_WDA_URL` points at a runner started some other way.

## Why this design

| Option | Verdict |
|---|---|
| **iPhone Mirroring + macOS automation** (chosen) | Built into macOS 15+/iOS 18+. No phone app, no Xcode, no developer account, works on every app including Settings. Costs: the phone must be locked and nearby while driven, and control is by pixels and OCR, not an element tree. |
| WebDriverAgent / XCUITest (Appium, mobile-mcp) | Best fidelity (real accessibility tree, phone usable in hand). Needs full Xcode (~12 GB+; this Mac had 16 GB free), an Apple ID signing team (free certs expire every 7 days), and Developer Mode. Upgrade path if those become acceptable. |
| Lockdown only (pymobiledevice3) | Device facts, app sizes, file access. No touch input; screenshots need Developer Mode + a root tunnel on iOS 17+. Used here as the read-only device layer. |
| Bluetooth HID (ESP32 mouse + AssistiveTouch) | Blind without a video feed; hardware. |
| Shortcuts / jailbreak | No general UI control / not acceptable. |

## Setup (one time)

1. **iPhone Mirroring**: open it on the Mac, follow the prompt on the phone.
   In iPhone Mirroring > Settings choose *Authenticate Automatically* so it
   doesn't stop for Touch ID each session.
2. **macOS permissions** for the app that runs the server (Claude Code shows as
   `claude`): *Accessibility* (to click/type) and *Screen & System Audio
   Recording* (to see the phone). System Settings > Privacy & Security.
3. **Device layer (optional)**: plug the phone in by USB once, tap *Trust*.
   Then Finder > iPhone > *Show this iPhone when on Wi-Fi* to drop the cable.
4. Register with Claude Code:

   ```bash
   claude mcp add --scope user iphone /Users/shaunkapoor/iPhoneMCP/run.sh
   ```

`run.sh` uses `.venv` (`uv venv && uv pip install -r requirements.txt`). The
Swift helper rebuilds itself from `bridge/mirrorctl.swift` when the source is
newer than `bin/mirrorctl` (needs only the Command Line Tools).

## Using it

Lock the phone and leave it near the Mac. Mirroring pauses whenever the phone is
in your hand (`phone_status` says so); lock it again and call `phone_connect`.

Coordinates are **phone points** (iPhone 13: 390 x 844, origin top-left).
Screenshots are scaled so one image pixel is one point, and every OCR'd text
item comes with its tap point, so most steps don't need an image at all.

| Tool | What it does |
|---|---|
| `phone_status`, `phone_connect`, `phone_disconnect` | readiness and why not; start an engine; stop WebDriverAgent (clears "Automation Running") |
| `phone_watch` | open iPhone Live on the Mac |
| `phone_do` | many steps in one call: `[{"goto": "storage"}, {"tap": "General"}, {"wait_for": "..."}]`; stops at the first failure or refusal |
| `phone_goto` | jump to a Settings page by name (storage, battery, wifi, about, software update, ...) in one call |
| `phone_open_app`, `phone_open_url` | apps by name or bundle id; web pages and app deep links |
| `phone_tap`, `phone_long_press` | by visible `text` or `x,y` |
| `phone_swipe`, `phone_scroll` | finger drags; scroll moves exactly the amount asked |
| `phone_type`, `phone_key` | typing into the focused field |
| `phone_go` | `home`, `app_switcher`, `spotlight`, `back` |
| `phone_alert` | read or answer an alert / permission prompt / confirmation sheet; `button="dismiss"` is the safe way out |
| `phone_read_screen`, `phone_screenshot`, `phone_zoom`, `phone_find`, `phone_wait` | seeing and waiting |
| `phone_storage_report` | free space, every app (app vs. data), and with `deep=true` a camera roll scan; ends with ways to get space back. No screen needed |
| `phone_journal`, `phone_note` | read earlier sessions' notes and runs; save a note for later |
| `device_info`, `device_apps` | model, iOS, battery; installed apps |

### HTTP API (iPhone Live, 127.0.0.1:8780)

```
GET  /api/status   /api/screen   /api/activity?since=<unix time>   /api/storage?deep=1   /stream (MJPEG)
POST /api/connect  /api/disconnect  /api/do {"steps": [...]}  /api/goto {"place": "storage"}
```

POST endpoints need the header `X-iPhone-Agent: 1` (so web pages on other
origins cannot drive the phone). Refusals come back as 409 with the reason.

```bash
curl -s -X POST localhost:8780/api/do -H 'X-iPhone-Agent: 1' -H 'Content-Type: application/json' \
  -d '{"steps": [{"goto": "battery"}, {"wait_for": "Battery Health"}]}'
```

### Run journal

`phone_disconnect` (and the idle auto-stop) uploads the session's actions to
`runs/` in the private repo `shaunak1206/iphone-agent-journal`; every storage
report goes to `reports/storage/` and a row in `history.csv`; `phone_note`
appends to `NOTES.md`. Typed text is never logged. `IPHONE_JOURNAL=0` turns it
off, `IPHONE_JOURNAL_REPO=owner/name` points it elsewhere.

### Why not a hosted (Vercel) viewer

The phone sits on a private network (campus Wi-Fi) and streams over plain HTTP.
A page served from the cloud cannot reach it, and browsers block an HTTPS page
from loading an HTTP stream from a private address. iPhone Live runs on the Mac
next to the phone instead; exposing it remotely would need an authenticated
tunnel, which is deliberately not set up.

### Safety rails

- Taps on controls labelled delete / erase / reset / remove / offload / buy /
  pay / send / post / sign out / confirm / discard are refused unless
  `confirm=True`, which the agent may pass only after the user says yes in chat
  (or the request asked for exactly that).
- Prompts are read from their layout (centered question, known button labels
  stacked or side by side) and flagged as `PROMPT on screen` in every readout.
  Answering one is judged with its question: *Allow* / *Trust* / *Accept*
  (granting access, accepting terms) and *OK* / *Continue* to a destructive
  question ("Delete 'Instagram'?") also need `confirm=True`. *Cancel*,
  *Don't Allow*, *Not Now* never do.
- Passcode and password screens are flagged; the agent never types them.
- The device layer is read-only. Cleanup happens through the phone's own UI,
  where it is visible (Recently Deleted keeps photos 30 days; *Offload App*
  keeps app data).
- After every action, focus and the mouse cursor go back to where you had them
  (`IPHONE_RESTORE_FOCUS=0` to disable).

### Other phone sizes

Set `IPHONE_WIDTH_PT` / `IPHONE_HEIGHT_PT` in the server env (e.g. 430 x 932
for Pro Max models).

## Tests

```bash
.venv/bin/python tests/test_smoke.py   # offline: every tool via FastMCP against tests/fake_mirrorctl.py
.venv/bin/python tests/live_check.py   # live, read-only walk through Settings on the real phone
./run.sh --check                       # readiness report
```
