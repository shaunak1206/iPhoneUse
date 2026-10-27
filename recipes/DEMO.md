# Demo: Wordle, Spelling Bee and Connections on the phone

About 5 minutes end to end. Every line is one call; nothing waits on a fixed timer
except the Wordle tile flips.

```bash
cd ~/iPhoneMCP && source .venv/bin/activate
```

1. **Phone on the cable, unlocked.** (Campus Wi-Fi: WebDriverAgent needs the cable the
   whole time.)
2. **Preflight (~40 s, read-only):** `python tests/live_check.py --keep`
   Every line should say `ok`. It leaves WebDriverAgent running.
3. **Watch and record:** `iphone live` (the dashboard, live screen + action feed), then
   `iphone record start --name nyt-games`.
4. **Wordle** (fresh incognito tab, so today's puzzle is unplayed):
   ```bash
   python -m recipes.games open wordle
   python -m recipes.games wordle-start      # Play, ad, How-to-play: cleared
   python -m recipes.games wordle-solve      # plays to the end (99/100 in simulation, ~3.7 guesses)
   ```
5. **Spelling Bee** (spellbee.org, the free one):
   ```bash
   python -m recipes.games open bee
   iphone dismiss                            # any popup / cookie banner
   python -m recipes.games bee-run 30        # reads the hive, submits 30 words, pangrams first
   ```
6. **Connections:**
   ```bash
   python -m recipes.games open connections
   iphone dismiss
   python -m recipes.games connections       # the 16 words
   python -m recipes.games connections-submit "W1,W2,W3,W4"   # x4, most certain group first
   ```
7. **Finish:** `iphone record stop` (prints the MP4 path under `build/recordings/`), then
   `iphone stop` (clears the phone's "Automation Running" banner).

If a step stops: `iphone read` shows the screen with tap points; `iphone marks shot.jpg`
numbers every element and `iphone tap "#n"` taps one; `iphone dismiss` clears overlays.
