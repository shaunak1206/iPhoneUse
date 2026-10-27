# Demo: Wordle, Spelling Bee and Connections on the phone

About 3-5 minutes end to end. Every line is one call; nothing waits on a fixed timer
except the Wordle tile flips. Before this pass, getting past Wordle's ad and
How-to-play screen alone took about 4 minutes of single steps.

```bash
cd ~/iPhoneMCP && source .venv/bin/activate
```

1. **Phone on the cable, unlocked, in your hand.** (Campus Wi-Fi: WebDriverAgent needs
   the cable the whole time.) Starting WebDriverAgent asks for **Face ID on the phone**
   (iOS turning on automation mode); unanswered, it times out after 60 s. The phone
   also auto-locks while nothing drives it, so keep it awake between steps.
2. **Preflight (~40 s, read-only):** `python tests/live_check.py --keep`
   Every line should say `ok`. It leaves WebDriverAgent running.
3. **Watch and record:** `iphone live` (the dashboard, live screen + action feed), then
   `iphone record start --name nyt-games`.
4. **Wordle** in Safari (the recipes use Safari by default: a browser where today's puzzle
   is unplayed; `--chrome` / `--incognito` for Chrome), one call with timings:
   ```bash
   python -m recipes.games wordle-run        # open + start + solve
   ```
   Measured Oct 27 (BINGO in 5 guesses), about **3 minutes**:

   | phase | measured | where the time goes |
   |---|---|---|
   | open in Safari | 12 s | tap the address bar, type, page load |
   | start | 34 s | Play, the ad's countdown, a Duolingo promo, How to play, each arriving after the last |
   | solve | 129 s for 5 guesses | ~6 key taps at 0.6 s, ~1 s per screen read, tile flips; some guesses waited on a late promo |

   The same in steps: `open wordle`, `wordle-start`, `wordle-solve`.
5. **Spelling Bee** (spellbee.org, the free one):
   ```bash
   python -m recipes.games open bee
   iphone dismiss                            # any popup / cookie banner
   python -m recipes.games bee-run 15        # reads the hive, submits 15 words (82 s), pangrams first
   ```
6. **Connections:**
   ```bash
   python -m recipes.games open connections
   iphone tap Play
   iphone dismiss                            # the ad: "Continue to Connections"
   python -m recipes.games connections       # the 16 words
   python -m recipes.games connections-submit "W1,W2,W3,W4"   # x4, most certain group first, ~9 s each
   ```
7. **Finish:** `iphone record stop` (prints the MP4 path under `build/recordings/`), then
   `iphone stop` (clears the phone's "Automation Running" banner).

If a step stops: `iphone read` shows the screen with tap points; `iphone marks shot.jpg`
numbers every element and `iphone tap "#n"` taps one; `iphone dismiss` clears overlays.
