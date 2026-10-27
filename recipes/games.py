"""Word games on the phone, each move one call: Wordle, a free Spelling Bee
(spellbee.org) and Connections, in Chrome. Built on phone.Phone; an example of
a recipe that turns a whole task into a few fast steps.

    python -m recipes.games open wordle            # fresh incognito tab in Chrome
    python -m recipes.games wordle crane           # type a guess, print the colors
    python -m recipes.games wordle-hint            # words that fit every clue so far
    python -m recipes.games bee                    # read the hive, list words (pangrams first)
    python -m recipes.games bee-submit 40          # type the best 40 words
    python -m recipes.games connections            # read the 16 words
    python -m recipes.games connections-submit "a,b,c,d"

Needs `pip install wordfreq` for the word lists (it ranks words by how common
they are, which is what these puzzles use).
"""

from __future__ import annotations

import asyncio
import re
import sys
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

async def open_in_chrome(p: Phone, url: str, incognito: bool = True) -> str:
    """A fresh tab (incognito keeps puzzles unplayed and logged out) at url."""
    await p.open_app("com.google.chrome.ios")
    await p.settle_screen()
    if incognito:
        await p.taps(["Show tabs"])
        await p.settle_screen()
        await p.taps(["Incognito tabs"])
        await p.settle_screen()
        s = await p.read()
        new = screen.find(s, "Create new tab") or screen.find(s, "New tab") or screen.find(s, "New Incognito Tab")
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
    return await p.after(f"Opened {url} in {'an incognito' if incognito else 'a'} Chrome tab.", settle=2.0)


# -- Wordle ---------------------------------------------------------------

def board(s) -> list[str]:
    """Filled rows as 'C- R- A- NY E-' (G green, Y yellow, - gray)."""
    rows: dict[int, list[tuple[int, str]]] = {}
    for it in s.items:
        if (m := TILE.fullmatch(it.text)) and m.group(3):
            rows.setdefault(round(it.cy / 10), []).append((int(m.group(1)), m.group(2) + MARK.get(m.group(3), "?")))
    return [" ".join(c for _, c in sorted(r)) for _, r in sorted(rows.items())]


async def wordle_guess(p: Phone, word: str) -> list[str]:
    await p.keys(["backspace"] * 5 + list(word.lower()) + ["enter"])
    await asyncio.sleep(2.2)  # five tiles flip one after another
    return board(await p.read())


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
    return [w for w in words() if len(w) == 5 and fits(w, rows)][:limit]


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
    for w in ws:
        await p.keys(list(w) + ["Enter"])
        await asyncio.sleep(0.35)  # the "Nice!" toast
    return f"Submitted {len(ws)} words."


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
    if cmd == "open":
        return await open_in_chrome(p, URLS.get(args[0], args[0]), incognito="--normal" not in args)
    if cmd == "wordle":
        rows = await wordle_guess(p, args[0])
        return "\n".join(rows) + "\n\nfits: " + ", ".join(wordle_candidates(rows, 12))
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
