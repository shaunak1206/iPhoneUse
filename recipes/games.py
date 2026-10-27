"""Word games on the phone, each move one call: Wordle, a free Spelling Bee
(spellbee.org) and Connections, in Chrome. Built on phone.Phone; an example of
a recipe that turns a whole task into a few fast steps.

    python -m recipes.games open wordle            # in Safari (--chrome / --incognito for Chrome)
    python -m recipes.games wordle-run             # all of it in one call, with timings (~40-50 s)
    python -m recipes.games wordle-start           # Play, then clear the ad and How-to-play layers
    python -m recipes.games wordle-solve           # play to the end: most common word that fits
    python -m recipes.games wordle crane           # type a guess, print the colors
    python -m recipes.games wordle-hint            # words that fit every clue so far
    python -m recipes.games bee                    # read the hive, list words (pangrams first)
    python -m recipes.games bee-submit 40          # type the best 40 words
    python -m recipes.games bee-run 40             # read the hive and submit, one call
    python -m recipes.games connections            # read the 16 words
    python -m recipes.games connections-submit "a,b,c,d"

Needs `pip install wordfreq` for the word lists (it ranks words by how common
they are, which is what these puzzles use).
"""

from __future__ import annotations

import asyncio
import re
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from iphone_mcp import screen  # noqa: E402
from iphone_mcp.phone import Phone  # noqa: E402

URLS = {
    "wordle": "nytimes.com/games/wordle/index.html",
    "connections": "nytimes.com/games/connections",
    "bee": "spellbee.org",
}
TILE = re.compile(r"(\d)(?:st|nd|rd|th) letter, ([A-Z])(?:, (.+))?")
MARK = {"correct": "G", "present in another position": "Y", "absent": "-"}


_WORDS: list[str] | None = None


def words(n: int = 80000) -> list[str]:
    """Common English words, most common first. wordfreq ranks them; the system
    dictionary's lower-case entries drop names (Nixon, Nikon) and slang (lovin)."""
    global _WORDS
    if _WORDS is None:
        from wordfreq import top_n_list
        dict_path = Path("/usr/share/dict/words")
        lower = {w.strip() for w in dict_path.read_text().split() if w.islower()} if dict_path.exists() else None
        _WORDS = [w for w in top_n_list("en", n) if w.isalpha() and w.isascii() and (lower is None or w in lower)]
    return _WORDS


# -- Chrome ---------------------------------------------------------------

async def open_in_chrome(p: Phone, url: str, incognito: bool = False) -> str:
    """A new Chrome tab at url (incognito=True for a logged-out one)."""
    await p.open_app("com.google.chrome.ios")
    await p.settle_screen()
    s = await p.read()
    if screen.find(s, "Show tabs"):
        await p.taps(["Show tabs"])
        await p.settle_screen()
    await p.taps(["Incognito tabs" if incognito else "Currently open tabs"])
    await p.settle_screen()
    s = await p.read()
    if incognito and screen.find(s, "Continue in Incognito"):
        # Chrome locks incognito tabs when it leaves the foreground; this asks for Face ID.
        await p.taps(["Continue in Incognito"])
        for _ in range(8):
            await asyncio.sleep(0.5)
            s = await p.read()
            if not screen.find(s, "Continue in Incognito"):
                break
        else:
            return await open_in_chrome(p, url)
    new = (screen.find(s, "Create new Incognito tab") if incognito else None) or screen.find(s, "Create new tab") \
        or screen.find(s, "New tab")
    if new:
        await p.tap(new[0].cx, new[0].cy)
        await p.settle_screen()
    s = await p.read()
    bar = next((h for q in ("Search or type URL", "Search Google or type URL", "Address and search bar")
                for h in screen.find(s, q)), None)
    if bar is None:
        raise ValueError("could not find Chrome's address bar\n\n" + screen.describe(s))
    await p.tap(bar.cx, bar.cy)
    await p.settle_screen()
    await p.type(url, submit=True)
    return await p.after(f"Opened {url} in {'an incognito' if incognito else 'a new'} Chrome tab.", settle=2.0)


async def open_in_safari(p: Phone, url: str) -> str:
    """url in Safari's current tab (the bottom address bar)."""
    await p.open_app("com.apple.mobilesafari")
    await p.settle_screen()
    await p.taps(["Address"])
    await p.settle_screen()
    await p.type(url, submit=True)
    return await p.after(f"Opened {url} in Safari.", settle=2.0)


async def open_page(p: Phone, url: str, args: list[str] = ()) -> str:
    """Safari by default; --chrome (and --incognito) for Chrome."""
    if "--chrome" in args or "--incognito" in args:
        return await open_in_chrome(p, url, incognito="--incognito" in args)
    return await open_in_safari(p, url)


# -- Wordle ---------------------------------------------------------------

def board(s) -> list[str]:
    """Filled rows as 'C- R- A- NY E-' (G green, Y yellow, - gray)."""
    rows: dict[int, list[tuple[int, str]]] = {}
    for it in s.items:
        if (m := TILE.fullmatch(it.text)) and m.group(3):
            rows.setdefault(round(it.cy / 10), []).append((int(m.group(1)), m.group(2) + MARK.get(m.group(3), "?")))
    return [" ".join(c for _, c in sorted(r)) for _, r in sorted(rows.items())]


async def wordle_guess(p: Phone, word: str) -> list[str]:
    for attempt in range(3):
        s = await p.read()
        before = board(s)
        typed = sum(1 for it in s.items if (m := TILE.fullmatch(it.text)) and not m.group(3))  # not yet sent
        try:
            await p.keys(["backspace"] * typed + list(word.lower()) + ["enter"])
            break
        except ValueError as e:  # a late promo over the keyboard
            if "no on-screen key" not in str(e) or attempt == 2:
                raise
            await p.dismiss(rounds=3, wait_ads=10)
    await asyncio.sleep(1.4)  # five tiles flip one after another
    await p.settle_screen()
    s = await p.read()
    rows = board(s)
    if len(rows) <= len(before) and (screen.find(s, "Congratulations") or screen.find(s, "GUESS DISTRIBUTION")):
        return before + [" ".join(c.upper() + "G" for c in word)]  # the win's stats modal covers the board
    return rows


async def wordle_start(p: Phone) -> str:
    """From the Wordle start page: Play (or Continue, for a game already started in
    this browser), then clear the ad and How-to-play layers."""
    s = await p.read()
    for label in ("Play", "Continue"):
        if screen.find(s, label, exact=True):
            await p.tap(text=label, exact=True)
            await p.settle_screen()
            break
    # The ad, a promo and How to play arrive one after another with gaps, so keep
    # clearing until a pass finds nothing and the keyboard is showing.
    end = time.time() + 45
    while True:
        cleared = await p.dismiss(rounds=4, wait_ads=15)
        s = await p.read()
        if cleared == "Nothing to dismiss." and screen.find(s, "enter", exact=True):
            return "Wordle is ready (keyboard showing)."
        if time.time() > end:
            raise ValueError("Wordle's keyboard did not come up within 45s.\n\n" + screen.describe(s))
        if cleared == "Nothing to dismiss.":
            await asyncio.sleep(0.5)


def fits(word: str, rows: list[str]) -> bool:
    for row in rows:
        cells = row.split()
        guess = "".join(c[0].lower() for c in cells)
        marks = "".join(c[1] for c in cells)
        need = Counter(g for g, m in zip(guess, marks, strict=True) if m in "GY")
        for i, (g, m) in enumerate(zip(guess, marks, strict=True)):
            if m == "G" and word[i] != g:
                return False
            if m == "Y" and (word[i] == g or g not in word):
                return False
            if m == "-" and word[i] == g:
                return False
        have = Counter(word)
        for g, m in zip(guess, marks, strict=True):
            if m == "-" and have[g] > need[g]:
                return False  # gray means no more copies than the colored ones
        if any(have[g] < n for g, n in need.items()):
            return False
    return True


def wordle_candidates(rows: list[str], limit: int = 15) -> list[str]:
    found = [w for w in words() if len(w) == 5 and fits(w, rows)]
    if not found:  # a rarer answer: fall back to every dictionary word
        d = Path("/usr/share/dict/words")
        found = [w for w in (d.read_text().split() if d.exists() else []) if len(w) == 5 and w.islower()
                 and fits(w, rows)]
    return found[:limit]


def best_guess(rows: list[str], cands: list[str]) -> str:
    """The most common fitting word, unless several words differ in only a letter
    or two (_ATCH, _OWER): then a probe word covering as many of the letters that
    tell them apart as possible, so the answer is pinned down in one more guess."""
    if len(cands) <= 2 or len(rows) >= 5:
        return cands[0]
    shared = set.intersection(*(set(w) for w in cands))
    freq = Counter(ch for w in cands for ch in set(w) if ch not in shared)
    pool = [w for w in words()[:25000] if len(w) == 5]

    def score(w: str) -> float:
        return sum(freq[ch] for ch in set(w)) + (0.5 if w in cands else 0)
    probe = max(pool, key=score)
    return probe if score(probe) > score(cands[0]) + 1 else cands[0]


def solved(rows: list[str]) -> bool:
    return bool(rows) and all(c.endswith("G") for c in rows[-1].split())


async def wordle_solve(p: Phone, first: str = "crane", guess=None) -> list[str]:
    """Play until solved: the most common word that fits every clue so far. A word
    Wordle rejects (row did not fill) is skipped. `guess` is injectable for tests."""
    guess = guess or (lambda w: wordle_guess(p, w))
    rows = board(await p.read()) if p else []
    tried: set[str] = set()
    while len(rows) < 6 and not solved(rows) and len(tried) < 10:
        cands = [w for w in wordle_candidates(rows, 300) if w not in tried]
        if not rows and first not in tried:
            word = first
        elif not cands:
            break
        else:
            word = best_guess(rows, cands)
            if word in tried:
                word = cands[0]
        tried.add(word)
        new = await guess(word)
        if len(new) > len(rows):
            rows = new
            if p:
                p.record(f"Wordle: {word.upper()} -> {rows[-1]}")
    return rows


# -- Spelling Bee (spellbee.org) ------------------------------------------

def hive(s) -> tuple[str, list[str]]:
    """(center, outer letters) from the honeycomb: single letters in links, the
    center being the one nearest the middle of the others."""
    cells = [it for it in s.items if len(it.text.strip()) == 1 and it.text.strip().isalpha()
             and it.kind in ("Link", "Button", "StaticText", "")]
    if len(cells) < 7:
        raise ValueError("could not find the 7 hive letters\n\n" + screen.describe(s))
    cells = sorted(cells, key=lambda c: c.cy)[:7] if len(cells) > 7 else cells
    cx = sum(c.cx for c in cells) / 7
    cy = sum(c.cy for c in cells) / 7
    center = min(cells, key=lambda c: (c.cx - cx) ** 2 + (c.cy - cy) ** 2)
    return center.text.strip().lower(), [c.text.strip().lower() for c in cells if c is not center]


def bee_words(center: str, outer: list[str], limit: int = 60) -> list[str]:
    letters = set(outer) | {center}
    found = [w for w in words() if len(w) >= 4 and center in w and set(w) <= letters]
    pangrams = [w for w in found if set(w) == letters]
    return (pangrams + [w for w in found if w not in pangrams])[:limit]


async def bee_submit(p: Phone, ws: list[str]) -> str:
    """Read the hive once, then each word is one touch request (letters + Enter)."""
    s = await p.read()
    cells = {it.text.strip().lower(): (it.cx, it.cy) for it in s.items
             if len(it.text.strip()) == 1 and it.text.strip().isalpha()}
    enter = next(((it.cx, it.cy) for it in s.items if it.text.strip().lower() == "enter"), None)
    if enter is None:
        raise ValueError("no Enter button on screen\n\n" + screen.describe(s))
    sent = 0
    for w in ws:
        if not all(ch in cells for ch in w):
            continue
        await p._tap_points([cells[ch] for ch in w] + [enter])
        await asyncio.sleep(0.25)  # the word clears / "Nice!" toast
        sent += 1
    p.record(f"Spelling Bee: submitted {sent} words")
    return f"Submitted {sent} words: {', '.join(ws[:sent])}."


# -- Connections ----------------------------------------------------------

def connections_board(s) -> list[str]:
    """The word tiles: short upper-case labels laid out in the 4x4 grid."""
    tiles = [it for it in s.items if re.fullmatch(r"[A-Z0-9][A-Z0-9 '\-.&!]{0,22}", it.text.strip())
             and it.kind in ("Button", "StaticText", "Link", "")]
    rows: dict[int, list] = {}
    for t in tiles:
        rows.setdefault(round(t.cy / 20), []).append(t)
    grid = [r for r in rows.values() if len(r) == 4]
    return [t.text.strip() for r in grid for t in sorted(r, key=lambda t: t.cx)]


async def connections_submit(p: Phone, group: list[str]) -> str:
    await p.taps(group + ["Submit"])
    await asyncio.sleep(2.0)
    return await p.after(f"Submitted {', '.join(group)}.", settle=0)


# -- CLI ------------------------------------------------------------------

async def main(argv: list[str]) -> str:
    p = Phone()
    cmd, args = argv[0], argv[1:]
    if cmd not in ("help", "-h", "--help") and not await asyncio.to_thread(p.wda.ready):
        print((await p.connect("wda", 60)).splitlines()[0], file=sys.stderr)  # the runner idled out: restart it
    if cmd == "open":
        return await open_page(p, URLS.get(args[0], args[0]), args)
    if cmd == "wordle":
        rows = await wordle_guess(p, args[0])
        return "\n".join(rows) + "\n\nfits: " + ", ".join(wordle_candidates(rows, 12))
    if cmd == "wordle-run":
        t0 = time.time()
        marks = []
        await open_page(p, URLS["wordle"], args)
        marks.append(("open", time.time() - t0))
        await wordle_start(p)
        marks.append(("start (Play, ad, How to play)", time.time() - t0 - sum(m[1] for m in marks)))
        first = [a for a in args if not a.startswith("--")]
        rows = await wordle_solve(p, first[0] if first else "crane")
        marks.append((f"solve ({len(rows)} guesses)", time.time() - t0 - sum(m[1] for m in marks)))
        times = "\n".join(f"  {label:32s} {sec:5.1f}s" for label, sec in marks)
        return ("\n".join(rows) + ("\n\nsolved" if solved(rows) else "\n\nnot solved")
                + f"\n\n{times}\n  {'total':32s} {time.time() - t0:5.1f}s")
    if cmd == "wordle-start":
        return await wordle_start(p)
    if cmd == "wordle-solve":
        rows = await wordle_solve(p, args[0] if args else "crane")
        return "\n".join(rows) + ("\n\nsolved" if solved(rows) else "\n\nnot solved")
    if cmd == "bee-run":
        center, outer = hive(await p.read())
        ws = bee_words(center, outer, int(args[0]) if args else 40)
        return f"center {center.upper()}, outer {' '.join(outer).upper()}\n" + await bee_submit(p, ws)
    if cmd == "wordle-hint":
        rows = board(await p.read())
        return "\n".join(rows) + "\n\nfits: " + ", ".join(wordle_candidates(rows, 25))
    if cmd == "bee":
        center, outer = hive(await p.read())
        ws = bee_words(center, outer, 80)
        return f"center {center.upper()}, outer {' '.join(outer).upper()}\n{len(ws)} words: {', '.join(ws)}"
    if cmd == "bee-submit":
        center, outer = hive(await p.read())
        n = int(args[0]) if args else 40
        return await bee_submit(p, bee_words(center, outer, n))
    if cmd == "connections":
        return " | ".join(connections_board(await p.read()))
    if cmd == "connections-submit":
        return await connections_submit(p, [w.strip() for w in args[0].split(",")])
    raise SystemExit(__doc__)


if __name__ == "__main__":
    print(asyncio.run(main(sys.argv[1:] or ["help"])))
