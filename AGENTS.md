# iPhoneMCP: notes for agents working in this repo

Computer use for a real iPhone from a Mac. One core (`phone.Phone`) with four
front ends: MCP tools for agents, the `iphone` CLI, the iPhone Live dashboard
(web page plus HTTP API for other apps), and a run journal on GitHub. Using the
phone (not changing this code) is covered by `skills/iphone/SKILL.md`.

## Layout

```
iphone_mcp/
  phone.py       the library: engine choice, every action, phone_do steps, goto, activity log
  server.py      FastMCP tools (thin wrappers over Phone), --check, --list-tools
  cli.py         `iphone` command
  dashboard.py   iPhone Live: Starlette app on 127.0.0.1:8780, MJPEG proxy, /api/*
  assets/dashboard.html
  wda.py         WebDriverAgent engine (HTTP to the runner app on the phone)
  bridge.py      iPhone Mirroring engine (mirrorctl helper: capture, OCR, mouse/keys)
  screen.py      readouts, text matching, prompt detection, safety rules (RISKY/GRANT/DECLINE)
  storage.py     storage report: lockdown app sizes + parallel AFC du of the media partition
  journal.py     run journal in the private repo shaunak1206/iphone-agent-journal (gh api)
  device.py      lockdown helpers (pymobiledevice3)
bridge/mirrorctl.swift   Swift helper for Mirroring (built to bin/, gitignored)
scripts/wda_start.sh     build/re-sign WebDriverAgent, run it, idle auto-stop
vendor/WebDriverAgent    appium/WebDriverAgent v16.15.3 (gitignored, clone per README)
skills/iphone/SKILL.md   how agents should use the phone (symlinked into ~/.claude/skills)
tests/  fake_mirrorctl.py, fake_wda.py, test_smoke.py (offline), live_check.py
```

## Running things

```bash
.venv/bin/python tests/test_smoke.py     # offline suite, no phone needed (110+ checks)
.venv/bin/iphone status                  # what can reach the phone right now
.venv/bin/iphone live                    # iPhone Live dashboard
.venv/bin/iphone storage --deep          # storage report (cable)
~/CanvasMCP/.venv/bin/ruff check iphone_mcp tests --select F,E9,B,UP
```

Run the smoke suite after touching anything in `iphone_mcp/`. It has caught real
bugs: variable shadowing in phone_do, a test overwriting the real live-view page,
scrolls starting at the screen edge.

## Invariants

- **stdout belongs to the MCP transport.** Log with `bridge.log` (stderr).
- **Actions go through `Phone`.** Front ends never call an engine directly for an
  action; Phone serializes them (one finger), applies the safety guard, and records
  the activity the dashboard and journal show.
- **Safety guard stays on every path.** Taps resolve through `Phone.resolve` +
  `Phone.guard`; `confirm=True` is the only way past it. The dashboard API returns
  409 with the refusal text rather than acting.
- **Tests never touch real state.** They redirect `wda.URL_FILE`, `wda.LAST_FILE`,
  `wda.RUNNER`, `phone.ACTIVITY_FILE`, set `IPHONE_JOURNAL=0`, and stub
  `dashboard.ensure_running` and `wda.start`. New files under `build/` need the
  same treatment.
- **The dashboard listens on 127.0.0.1 only**, checks the Host header, and acting
  endpoints require `X-iPhone-Agent: 1`. Do not add CORS.
- **Journal writes are background and best-effort.** Never let GitHub slowness or
  failure block or fail a phone action. Typed text is never logged (actions record
  "Typed N characters").

## Hard-won facts (measured on an iPhone 14, iOS 26.4, Xcode 27.1)

- WebDriverAgent's `isVisible` attribute costs ~5 s per tree read; it is excluded
  and visibility comes from geometry (60% on screen).
- Lowering WDA's `waitForIdleTimeout`/`animationCoolOffTimeout` made each scroll
  ~14 s instead of ~2.6 s. Keep the defaults; HTTP timeout is 60 s.
- A scroll needs a >=150 ms pause before release or iOS flings (200 pt drag moved
  308 pt); ~20 pt go to touch slop. Drags must stay out of the top/bottom 12% or
  they open Notification Center / go home.
- Killing xcodebuild leaves the phone's "Automation Running" banner (testmanagerd
  keeps the session and ignores SIGTERM). Stop with GET `/wda/shutdown`.
- Campus Wi-Fi blocks Bonjour: CoreDevice cannot find the phone, so the runner can
  only be started over USB. Unicast works, so a running runner is reachable over
  Wi-Fi at the address it prints.
- The lockdown install service leaves out app-group data (WhatsApp's 3.6 GB), and
  offloaded apps are not listed. The iPhone Storage screen is the ground truth.
- AFC: Photos' caches are ~45k files; the media scan splits second-level folders
  over 6 connections (43 s for 66k files vs. ~4 min serial).
- Free Apple ID signing: 7-day profiles (wda_start.sh re-signs under 48 h left), at
  most 3 sideloaded apps on the phone, 10 new App IDs per week.

## Adding a phone action

1. Implement it on `Phone` (returns one line saying what happened; call `_did`).
2. If it is a step, add it to `Phone.STEPS` so `phone_do` and `/api/do` get it.
3. Wrap it in `server.py` (`return await p.after(await p.x(...))`) and, if useful,
   the CLI.
4. Fake it in `tests/fake_wda.py` / `fake_mirrorctl.py` and check it in `test_smoke.py`.
5. Mention it in `skills/iphone/SKILL.md` if agents should reach for it.
