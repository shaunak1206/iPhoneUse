"""Turning OCR boxes into something an agent can act on."""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from .bridge import Screen, TextItem

# Labels that usually mean an irreversible or outward-facing action. Tapping
# one requires confirm=True so the agent has to have asked the user first.
RISKY = re.compile(
    r"\b(delete|erase|reset|remove|uninstall|offload|buy|purchase|pay|subscribe|send|post|"
    r"transfer|sign out|log ?out|empty|clear (history|data|all)|confirm|unpair|forget|discard|don'?t save)\b",
    re.IGNORECASE,
)

# Prompt answers that grant an app access or accept terms. Like RISKY, they
# need the user's yes; they only count on a detected prompt, so a Settings row
# such as "Allow Notifications" is not caught.
GRANT = re.compile(r"^((always )?allow\b|trust$|accept|agree|i agree|share\b)", re.IGNORECASE)

# Answers that back out of a prompt. Safe to tap without asking.
DECLINE = {"cancel", "don't allow", "not now", "later", "remind me later", "close", "dismiss", "no",
           "no thanks", "skip", "keep", "don't trust", "ask app not to track", "don't allow paste",
           "limit access", "select photos", "keep current selection", "back"}
AFFIRM = {"ok", "okay", "open", "continue", "done", "got it", "yes", "retry", "try again", "settings",
          "open settings", "go to settings", "save", "update", "install", "view", "learn more", "join", "next",
          "turn on", "turn off", "enable", "use password"}
OTHER = {"allow", "allow once", "allow while using app", "allow full access", "allow paste", "always allow",
         "trust", "accept", "agree", "i agree", "delete", "delete app", "remove", "remove from home screen",
         "remove app", "erase", "sign out", "log out", "send", "buy", "subscribe", "confirm", "discard",
         "don't save", "replace", "reset", "unsubscribe", "block", "report", "leave", "end", "share"}
BUTTONS = DECLINE | AFFIRM | OTHER
# A lone button only makes a prompt if it is one of these ("Settings" alone is
# just as likely a home-screen icon label).
SINGLE = {"ok", "okay", "close", "dismiss", "continue", "got it", "done", "try again", "retry", "cancel", "not now"}

# Screens where only the user may type (passcodes, Apple ID passwords).
SECRET_PROMPT = re.compile(r"(enter (your )?passcode|enter password|apple (id|account) password|"
                           r"password for|sign in to (the )?(itunes|app store|apple))", re.IGNORECASE)


def lines(screen: Screen) -> list[list[TextItem]]:
    """Group items into visual rows (items whose vertical centers are close)."""
    rows: list[list[TextItem]] = []
    for it in sorted(screen.items, key=lambda t: t.cy):
        if rows and abs(rows[-1][0].cy - it.cy) <= max(6.0, 0.45 * min(rows[-1][0].h, it.h)):
            rows[-1].append(it)
        else:
            rows.append([it])
    for r in rows:
        r.sort(key=lambda t: t.x)
    return rows


def describe(screen: Screen, limit: int = 120) -> str:
    """One row per visual line; every text item tagged with its tap point."""
    if not screen.items:
        return (f"Screen {screen.width:.0f}x{screen.height:.0f} pt: no text recognized "
                "(home screen icons, images or a blank/black screen). Use phone_screenshot to look.")
    out = [f"Screen {screen.width:.0f}x{screen.height:.0f} pt, {len(screen.items)} text items "
           "as (x,y) tap point → text:"]
    n = 0
    for row in lines(screen):
        cells = [f"({it.cx:.0f},{it.cy:.0f}) {_tag(it)}{it.text}" for it in row]
        out.append("  " + "   |   ".join(cells))
        n += len(row)
        if n >= limit:
            out.append(f"  ... {len(screen.items) - n} more items")
            break
    out += [f"NOTE: {n}" for n in screen.notes]
    if any(SECRET_PROMPT.search(it.text) or it.kind == "SecureTextField" for it in screen.items):
        out.append("NOTE: this screen asks for a passcode or password. Only the user may enter it.")
    if (p := prompt(screen)):
        out.append(p.describe())
    return "\n".join(out)


# Element types worth naming in a readout; plain text stays untagged.
TAGGED = {"Button": "button", "Link": "link", "Switch": "switch", "Toggle": "switch", "TextField": "field",
          "SearchField": "search field", "TextView": "field", "SecureTextField": "field", "Icon": "app",
          "Cell": "row", "Tab": "tab", "Slider": "slider", "Image": "image", "MenuItem": "menu item",
          "ocr": "read from pixels"}


def _tag(it: TextItem) -> str:
    t = TAGGED.get(it.kind)
    return f"[{t}] " if t else ""


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def clean(s: str) -> str:
    """Button text as a key: straight quotes, no trailing ellipsis, lower case."""
    s = s.replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')
    return _norm(re.sub(r"(\.\.\.|\u2026)\s*$", "", s))


@dataclass
class Prompt:
    """An alert, permission prompt or action sheet, read from its layout."""

    title: str
    message: str
    buttons: list[TextItem]

    @property
    def context(self) -> str:
        return f"{self.title} {self.message}".strip()

    def describe(self) -> str:
        head = f"\u201c{self.title}\u201d" if self.title else "(no title)"
        if self.message:
            head += f" {self.message}"
        btns = "   |   ".join(f"({b.cx:.0f},{b.cy:.0f}) {b.text}" for b in self.buttons)
        return f"PROMPT on screen: {head}\n  buttons: {btns}\n  Answer it with phone_alert(button=...)."


def prompt(screen: Screen) -> Prompt | None:
    """Find a system-style prompt: known button labels stacked or side by side
    near the middle of the screen, with centered title text above them.
    Nav-bar buttons (top strip) and keyboard toolbar buttons (edges) don't count."""
    if screen.prompt is not None:
        return screen.prompt
    w, h = screen.width, screen.height
    cands = sorted((it for it in screen.items if clean(it.text) in BUTTONS
                    and 0.12 * h < it.cy < 0.97 * h and 0.1 * w < it.cx < 0.9 * w),
                   key=lambda t: (t.cy, t.cx))
    if not cands:
        return None
    clusters: list[list[TextItem]] = [[cands[0]]]
    for it in cands[1:]:
        if it.cy - clusters[-1][-1].cy <= 70:
            clusters[-1].append(it)
        else:
            clusters.append([it])
    btns = max(clusters, key=lambda c: (len(c), c[0].cy))
    top = btns[0].cy
    # title and message: centered rows just above the buttons, stopping at a gap
    above: list[list[TextItem]] = []
    for row in reversed(lines(screen)):
        cy = row[0].cy
        if cy >= top - 8 or any(i in btns for i in row):
            continue
        gap = (above[-1][0].cy if above else top) - cy
        if top - cy > 260 or gap > (180 if not above else 48):
            break
        rc = (min(i.x for i in row) + max(i.x + i.w for i in row)) / 2
        if len(row) <= 2 and abs(rc - w / 2) <= 0.16 * w:  # a row of icon labels is not a title
            above.append(row)
    above.reverse()
    if len(btns) == 1 and (clean(btns[0].text) not in SINGLE or not above or top - above[-1][0].cy > 120
                           or abs(btns[0].cx - w / 2) > 0.15 * w):
        return None
    if len(btns) >= 2 and any(abs(b.cx - w / 2) > 0.3 * w for b in btns):
        return None
    text = [" ".join(i.text for i in row) for row in above]
    return Prompt(title=text[0] if text else "", message=" ".join(text[1:]), buttons=btns)


def pick(items: list[TextItem], query: str) -> TextItem | None:
    """Best item for a button name: exact, then prefix, then substring, then fuzzy."""
    q = clean(query)
    best: tuple[float, TextItem] | None = None
    for it in items:
        t = clean(it.text)
        score = (3.0 if t == q else 2.5 if t.startswith(q) else 2.0 if q in t
                 else SequenceMatcher(None, q, t).ratio())
        if score >= 0.8 and (best is None or score > best[0]):
            best = (score, it)
    return best[1] if best else None


def needs_confirm(item: TextItem, p: Prompt | None) -> str | None:
    """Why tapping `item` needs the user's yes first, or None. On a prompt, an
    answer that grants access, or that says yes to a destructive question
    ("Delete 'Instagram'?" -> OK), counts too."""
    if RISKY.search(item.text):
        return item.text
    if p is None or item not in p.buttons:
        return None
    c = clean(item.text)
    if GRANT.search(c):
        return f"{item.text} (grants access / accepts: {p.context[:90]})"
    if c not in DECLINE and RISKY.search(p.context):
        return f"{item.text} (confirms: {p.context[:90]})"
    return None


def find(screen: Screen, query: str, exact: bool = False) -> list[TextItem]:
    """Items matching `query`, best first: exact, then substring, then fuzzy."""
    q = _norm(query)
    scored: list[tuple[float, TextItem]] = []
    for it in screen.items:
        t = _norm(it.text)
        if t == q:
            score = 3.0
        elif exact:
            continue
        elif q in t:
            score = 2.0 + len(q) / max(len(t), 1)
        else:
            ratio = SequenceMatcher(None, q, t).ratio()
            if ratio < 0.8:
                continue
            score = ratio
        scored.append((score, it))
    # stable top-to-bottom order among equally good matches
    scored.sort(key=lambda s: (-s[0], s[1].cy, s[1].cx))
    return [it for _, it in scored]


def at(screen: Screen, x: float, y: float) -> list[TextItem]:
    """Text items at (or right beside) a point."""
    hits = [it for it in screen.items if it.contains(x, y)]
    if hits:
        return hits
    # a tap on an icon or a row: take text on the same row within reach
    return [it for it in screen.items if abs(it.cy - y) < 14 and abs(it.cx - x) < 120]


def risky_label(items: list[TextItem]) -> str | None:
    for it in items:
        if RISKY.search(it.text):
            return it.text
    return None
