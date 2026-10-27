# Demo: Wordle, Spelling Bee and Connections on the phone

About 3-5 minutes end to end. Every line is one call; nothing waits on a fixed timer
except the Wordle tile flips. Before this pass, getting past Wordle's ad and
How-to-play screen alone took about 4 minutes of single steps.

```bash
cd ~/iPhoneMCP && source .venv/bin/activate
```

1. **Phone on the cable, unlocked.** (Campus Wi-Fi: WebDriverAgent needs the cable the
   whole time.)
2. **Preflight (~40 s, read-only):** `python tests/live_check.py --keep`
   Every line should say `ok`. It leaves WebDriverAgent running.
3. **Watch and record:** `iphone live` (the dashboard, live screen + action feed), then
   `iphone record start --name nyt-games`.
4. **Wordle** (fresh incognito tab, so today's puzzle is unplayed), one call with timings:
   ```bash
   python -m recipes.games wordle-run        # open + start + solve
   ```
   Expected, about **40-50 s** in all:

   | phase | estimate | where the time goes |
   |---|---|---|
   | open in a new incognito Chrome tab | 8-12 s | ~4 taps, typing the address, page load |
   | start | 10-12 s | mostly the ad's 5 s "Skip ad" countdown, then the How-to-play close |
   | solve | 16-20 s | ~4 guesses (3.7 average in simulation) at 4-5 s: ~2 s to type a word in one request, ~2 s of tile flips |

   The same in steps: `open wordle`, `wordle-start`, `wordle-solve`.
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
