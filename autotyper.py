#!/usr/bin/env python3
"""Auto Typer - types text into whatever window has focus, the way a person would.

Nothing is typed on a fixed rhythm. Key hold times, gaps between keys, key
overlap, bursts, pauses, typos with corrections and later revisions are all
drawn fresh for every run.

Setup:   pip install pynput
Run:     python autotyper.py
Use:     enter text, press Start (or F6), click into the target window
         before the countdown ends. F6 again (or Stop) cancels.

Formatting: **bold**, *italic* and # headings in the text are not typed as
symbols. The marked text is typed with Ctrl+B / Ctrl+I switched on (Cmd on
macOS), or the markers are simply dropped, depending on the Formatting
setting.

Note: corrections assume a plain text field. Turn off autocorrect,
auto-indent and auto-closing brackets in the target, or untick the
arrow-key option, or the result can drift from the text you entered.
"""
import math
import queue
import random
import re
import sys
import threading
import time

HOTKEY_NAME = "F6"

# --- keyboard model (QWERTY) ----------------------------------------------
ROWS = ["1234567890", "qwertyuiop", "asdfghjkl", "zxcvbnm"]
FINGER, NEIGHBORS = {}, {}
for _ri, _row in enumerate(ROWS):
    for _j, _c in enumerate(_row):
        FINGER[_c] = [0, 1, 2, 3, 3, 4, 4, 5, 6, 7][_j]
        near = [_row[k] for k in (_j - 1, _j + 1) if 0 <= k < len(_row)]
        for _rr in (_ri - 1, _ri + 1):
            if 1 <= _rr < len(ROWS):  # letters only: digit slips are rarer
                near += [ROWS[_rr][k] for k in (_j - 1, _j) if 0 <= k < len(ROWS[_rr])]
        NEIGHBORS[_c] = [n for n in near if n.isalpha() == _c.isalpha()]

SHIFT_MAP = {  # US layout: symbol -> key pressed together with Shift
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8",
    "(": "9", ")": "0", "_": "-", "+": "=", "{": "[", "}": "]", "|": "\\", ":": ";",
    '"': "'", "<": ",", ">": ".", "?": "/", "~": "`",
}
COMMON_WORDS = set(
    "the be to of and a in that have i it for not on with he as you do at this but his "
    "by from they we say her she or an will my one all would there their what so up out "
    "if about who get which go me when make can like time no just him know take people "
    "into year your good some could them see other than then now look only come its over "
    "think also back after use two how our work first well way even new want because any "
    "these give day most us is are was were been has had did".split())
COMMON_BIGRAMS = set(
    "th he in er an re on at en nd ti es or te of ed is it al ar st to nt ng se ha as ou "
    "io le ve co me de hi ri ro ic ne ea ra ce li ch ll be ma si om ur".split())
CONFUSABLE = {}
for _a, _b in [("their", "there"), ("then", "than"), ("to", "too"), ("were", "where"),
               ("of", "off"), ("from", "form"), ("quite", "quiet"), ("lose", "loose"),
               ("affect", "effect"), ("accept", "except"), ("its", "it's")]:
    CONFUSABLE[_a], CONFUSABLE[_b] = _b, _a
SKIPPABLE = {"the", "a", "an", "of", "to", "in", "is", "it", "and", "that", "for"}

# median key-hold seconds and gap multipliers per kind of key
HOLD = {"letter": .085, "digit": .105, "punct": .095, "symbol": .11, "space": .075,
        "special": .08}
GAP = {"letter": 1.0, "digit": 1.45, "punct": 1.3, "symbol": 1.7, "space": .9,
       "special": 1.2}
TECHNIQUES = {  # name: (keys may overlap, uses letter-pair fluency, timing spread)
    "Touch typing": (True, True, 0.28),
    "Two-finger": (False, False, 0.40),
    "One-handed": (False, False, 0.33),
}


BOLD, ITALIC = 1, 2
EMPH = re.compile(
    r"(?<!\\)(\*\*\*|___)(?=\S)(.+?)(?<=\S)\1"
    r"|(?<!\\)(\*\*|__)(?=\S)(.+?)(?<=\S)\3"
    r"|(?<![\w*\\])(\*|_)(?=[^\s*_])(.+?)(?<=[^\s*_])\5(?![\w*])")
HEADING = re.compile(r"(#{1,6})[ \t]+(?=\S)")


def parse_markup(text):
    """Read **bold**, *italic*, _italic_, __bold__ and # headings.

    Returns [(index in text, character, style bits)] for every character that
    should actually be typed; the markers themselves are left out.
    """
    out = []

    def plain(s, off, style):
        for i, c in enumerate(s):
            if c == "\\" and s[i + 1:i + 2] in ("*", "_"):
                continue                                    # \* means a real *
            out.append((off + i, c, style))

    def inline(s, off, style):
        pos = 0
        while True:
            m = EMPH.search(s, pos)
            if not m:
                break
            g = 1 if m.group(1) else 3 if m.group(3) else 5
            mark, a = m.group(g), m.start()
            if mark[0] == "_" and ((a > 0 and s[a - 1].isalnum())
                                   or s[m.end():m.end() + 1].isalnum()):
                plain(s[pos:a + 1], off + pos, style)        # snake_case, not emphasis
                pos = a + 1
                continue
            plain(s[pos:a], off + pos, style)
            bits = {3: BOLD | ITALIC, 2: BOLD, 1: ITALIC}[len(mark)]
            inline(m.group(g + 1), off + m.start(g + 1), style | bits)
            pos = m.end()
        plain(s[pos:], off + pos, style)

    off = 0
    for line in text.split("\n"):
        if off:
            out.append((off - 1, "\n", 0))
        h = HEADING.match(line)
        if h:
            inline(line[h.end():], off + h.end(), BOLD)
        else:
            inline(line, off, 0)
        off += len(line) + 1
    return out


class Humanizer:
    """Turns text into a timed list of key events: [time, seq, 'press'|'release', key].

    Keys are single characters or names like '<shift>', '<backspace>', '<left>'.
    A virtual buffer/cursor tracks what the target window should contain, so every
    typo and revision is undone before the plan ends.
    """

    def __init__(self, wpm=60, error_rate=0.025, pause_scale=1.0,
                 technique="Touch typing", revise=True, us_shift=True, rng=None):
        self.rng = rng or random.Random()
        self.base = 12.0 / max(5.0, wpm)          # seconds per key at nominal speed
        self.error_rate = error_rate
        self.ps = pause_scale
        self.revise = revise
        self.us_shift = us_shift
        self.overlap, self.digraph, self.sigma = TECHNIQUES[technique]
        self.ev, self.t, self.pend = [], 0.0, 0.0
        self.rel, self.recent = {}, []
        self.shift_down, self.shift_press, self.shift_hold, self.shift_rel = False, 0, 0, -1
        self.buf, self.cur, self.defects, self.prev = [], 0, [], None
        self.bufs, self.style, self.block = [], 0, -1.0     # styles typed, style now on
        self.x, self.burstf, self.wordf = 0.0, 1.0, 1.0
        self.burst_left = self._burst_len()

    # --- low level: timing --------------------------------------------------
    def _ln(self, sigma):
        return self.rng.lognormvariate(0, sigma)

    def _emit(self, t, kind, key):
        e = [t, len(self.ev), kind, key]
        self.ev.append(e)
        return e

    def pause(self, seconds):
        self.pend = max(self.pend, seconds)

    def _slow(self):
        return math.exp(self.x) * self.burstf * self.wordf

    def _press(self, key, cls, gap, shift=False, overlap=True):
        u = self.rng.uniform
        tp = self.t + gap + self.pend
        self.pend = 0.0
        tp = max(tp, self.rel.get(key, -1) + 0.02)           # same key must be up first
        tp = max(tp, self.block + .02)                       # Ctrl/Cmd must be up first
        if not (overlap and self.overlap) and self.recent:
            tp = max(tp, max(e[0] for _, e in self.recent) + u(.005, .03))
        hold = HOLD[cls] * self._ln(.25) * min(1.3, max(.8, self._slow())) ** .5
        hold = min(.3, max(.03, hold))
        if self.shift_down and not shift:                    # let go of Shift first
            r = min(tp - u(.006, .03), self.shift_press + self.shift_hold * u(.6, 1.5))
            r = max(r, self.shift_press + .012)
            tp = max(tp, r + .008)
            self._emit(r, "release", "<shift>")
            self.shift_down, self.shift_rel = False, r
        if shift and not self.shift_down:                    # Shift leads the key
            ts = max(tp - .09 * self._ln(.35), self.t + .008, self.shift_rel + .02,
                     self.block + .01)
            tp = max(tp, ts + .02)
            self._emit(ts, "press", "<shift>")
            self.shift_down = True
        for pt, e in self.recent[:-1]:                       # at most 2 keys down at once
            if e[0] > tp - .004:
                e[0] = max(pt + .02, tp - u(.004, .03))
                tp = max(tp, e[0] + .002)
        if shift:
            self.shift_press, self.shift_hold = tp, hold
        self._emit(tp, "press", key)
        e = self._emit(tp + hold, "release", key)
        self.rel[key] = tp + hold
        self.recent = (self.recent + [(tp, e)])[-3:]
        self.t = tp

    def _gap(self, c, cls, shift):
        m = GAP[cls]
        if shift and cls == "letter" and not self.shift_down:
            m *= 1.35
        a, b = (self.prev or "").lower(), c.lower()
        if self.digraph and a in FINGER and b in FINGER:
            if a == b:
                m *= .95
            elif a + b in COMMON_BIGRAMS:
                m *= .78
            elif FINGER[a] == FINGER[b]:
                m *= 1.25
            elif (FINGER[a] < 4) != (FINGER[b] < 4):
                m *= .92
            else:
                m *= 1.05
        if a == " ":
            m *= 1.1
        g = self.base * self._slow() * m * self._ln(self.sigma)
        if self.rng.random() < .015:                         # momentary hesitation
            g += self.rng.uniform(.15, .6) * self.ps
        return max(g, .03)

    def _restyle(self, style):
        """Switch bold/italic with Ctrl+B / Ctrl+I so the next key is typed in `style`."""
        u = self.rng.uniform
        for bit, letter in ((BOLD, "b"), (ITALIC, "i")):
            if not (self.style ^ style) & bit:
                continue
            tp = self.t + u(.12, .45) * self._ln(.3) + self.pend
            self.pend = 0.0
            if self.recent:
                tp = max(tp, max(e[0] for _, e in self.recent) + u(.01, .05))
            tp = max(tp, self.block + .03)
            if self.shift_down:
                r = self.shift_press + max(.012, self.shift_hold * u(.6, 1.5))
                self._emit(r, "release", "<shift>")
                self.shift_down, self.shift_rel = False, r
                tp = max(tp, r + .03)
            self._emit(tp, "press", "<mod>")
            t2 = max(tp + u(.06, .2), self.rel.get(letter, -1) + .02)
            hold = u(.05, .12)
            self._emit(t2, "press", letter)
            self._emit(t2 + hold, "release", letter)
            self.block = t2 + hold + u(.015, .1)
            self._emit(self.block, "release", "<mod>")
            self.rel[letter], self.t, self.recent = t2 + hold, t2, []
        self.style, self.prev = style, None

    def _caret_style(self):
        # After moving or deleting, an editor types in the style of the character
        # before the caret. Errors are only made where that is unambiguous.
        if self.cur > 0:
            self.style = self.bufs[self.cur - 1]

    # --- low level: keys that edit the buffer ---------------------------------
    def ch(self, c, style=0):
        if style != self.style:
            self._restyle(style)
        shift, overlap = False, True
        if c == "\n":
            key, cls, overlap = "<enter>", "special", False
        elif c == "\t":
            key, cls, overlap = "<tab>", "special", False
        elif c == " ":
            key, cls = " ", "space"
        elif c.isascii() and c.isalpha():
            key, cls, shift = c.lower(), "letter", c.isupper()
        elif c.isdigit():
            key, cls = c, "digit"
        elif self.us_shift and c in SHIFT_MAP:
            key, cls, shift = SHIFT_MAP[c], "symbol", True
        elif c.isascii() and not self.us_shift and not c.isalnum():
            key, cls, overlap = c, "symbol", False
        elif c.isascii():
            key, cls = c, "punct"
        else:
            key, cls, overlap = c, "symbol", False           # sent as-is
        self._press(key, cls, self._gap(c, cls, shift), shift, overlap)
        self.buf.insert(self.cur, c)
        self.bufs.insert(self.cur, style)
        self.cur += 1
        self.prev = c

    def _special(self, name, gap):
        self._press(name, "special", gap, overlap=False)
        self.prev = None

    def _nav_gap(self, fast=False):
        lo, hi = (.04, .085) if fast else (.09, .17)
        return self.rng.uniform(lo, hi) * self._ln(.2)

    def backspace(self, n=1):
        for _ in range(n):
            self._special("<backspace>", self._nav_gap())
            self.cur -= 1
            del self.buf[self.cur], self.bufs[self.cur]
        self._caret_style()

    def delete(self, n=1):
        for _ in range(n):
            self._special("<delete>", self._nav_gap())
            del self.buf[self.cur], self.bufs[self.cur]
        self._caret_style()

    def move(self, n):
        """Move the cursor n characters (negative = left)."""
        fast = abs(n) > 6
        for i in range(abs(n)):
            g = self._nav_gap(fast and i > 0) + (self.rng.uniform(.1, .3) if i == 0 else 0)
            self._special("<left>" if n < 0 else "<right>", g)
        self.cur += n
        self._caret_style()

    def type_str(self, s, style=0):
        """Type s; style is one value for all of it or a list with one per character."""
        for i, c in enumerate(s):
            self.ch(c, style if isinstance(style, int) else style[i])

    # --- typos, fixed on the spot -----------------------------------------------
    def _wrong(self, c):
        if c.isupper() and self.rng.random() < .3:
            return c.lower()                                 # missed the Shift key
        near = NEIGHBORS.get(c.lower())
        if not near:
            return None
        w = self.rng.choice(near)
        return w.upper() if c.isupper() else w

    def _notice(self):
        self.pause(self.rng.uniform(.15, .65) * self._ln(.3))

    def type_word(self, w, st=0, first_ok=True):
        rng, i, arrows = self.rng, 0, self.revise
        while i < len(w):
            c, left = w[i], len(w) - i - 1
            p = self.error_rate * (1.4 if self.wordf > 1.1 else 1.0)
            if not (c.isascii() and c.isalnum()) or rng.random() >= p or (i == 0 and not first_ok):
                self.ch(c, st)
                i += 1
                continue
            kind = rng.choice(["sub", "sub", "sub", "double", "omit", "swap"])
            if kind == "omit" and left >= 1:
                k = min(rng.choice([1, 1, 2, 3]), left)
                self.type_str(w[i + 1:i + 1 + k], st)
                self._notice()
                if arrows and rng.random() < .35:
                    self.move(-k); self.ch(c, st); self.move(k)
                else:
                    self.backspace(k); self.type_str(w[i:i + 1 + k], st)
                i += k + 1
            elif kind == "swap" and left >= 1 and w[i + 1] != c and w[i + 1].isalnum():
                k = min(rng.choice([0, 0, 1]), left - 1)
                self.ch(w[i + 1], st); self.ch(c, st); self.type_str(w[i + 2:i + 2 + k], st)
                self._notice()
                self.backspace(2 + k); self.type_str(w[i:i + 2 + k], st)
                i += 2 + k
            elif kind == "double":
                k = min(rng.choice([0, 0, 1]), left)
                self.ch(c, st); self.ch(c, st); self.type_str(w[i + 1:i + 1 + k], st)
                self._notice()
                self.backspace(k + 1); self.type_str(w[i + 1:i + 1 + k], st)
                i += k + 1
            else:
                wrong = self._wrong(c)
                if wrong is None:
                    self.ch(c, st)
                    i += 1
                    continue
                k = min(rng.choice([0, 0, 0, 1, 1, 2, 3]), left)
                self.ch(wrong, st); self.type_str(w[i + 1:i + 1 + k], st)
                self._notice()
                r = rng.random()
                if arrows and k >= 1 and r < .2:
                    self.move(-k); self.backspace(); self.ch(c, st); self.move(k)
                elif arrows and k >= 1 and r < .35:
                    self.move(-k - 1); self.delete(); self.ch(c, st); self.move(k)
                else:
                    self.backspace(k + 1); self.type_str(w[i:i + 1 + k], st)
                i += k + 1

    # --- revisions: slips left behind and fixed on a later read-through ---------
    def _misspell(self, w):
        rng = self.rng
        for _ in range(8):
            i = rng.randrange(1, len(w) - 1)
            kind = rng.choice(["swap", "drop", "double", "sub"])
            if kind == "swap":
                v = w[:i] + w[i + 1] + w[i] + w[i + 2:]
            elif kind == "drop":
                v = w[:i] + w[i + 1:]
            elif kind == "double":
                v = w[:i] + w[i] + w[i:]
            else:
                near = NEIGHBORS.get(w[i].lower())
                v = w[:i] + rng.choice(near) + w[i + 1:] if near else w
            if v != w:
                return v
        return None

    def review(self):
        """Go back over the current line and fix everything left behind."""
        if not self.defects:
            return
        u = self.rng.uniform
        self.pause(u(.6, 2.5) * self.ps + .2)
        for pos, wlen, correct, st in reversed(self.defects):   # last slip first
            if 0 < wlen <= 2 and self.rng.random() < .35:
                self.move(pos - self.cur)
                self.pause(u(.15, .5))
                self.delete(wlen)
            else:
                self.move(pos + wlen - self.cur)
                self.pause(u(.15, .5))
                self.backspace(wlen)
            self.type_str(correct, st)
            self.pause(u(.2, .7) * self.ps + .1)
        self.defects = []
        self.move(len(self.buf) - self.cur)
        self.pause(u(.25, .9) * self.ps + .1)

    # --- the session --------------------------------------------------------------
    def _burst_len(self):
        return int(self.rng.triangular(2, 16, 5))

    def run(self, text, styles=None):
        """Plan the typing of `text`. `styles` optionally gives BOLD/ITALIC bits
        per character; they are switched with Ctrl+B / Ctrl+I as the text goes."""
        rng, u = self.rng, self.rng.uniform
        S = styles or [0] * len(text)
        matches = list(re.finditer(r"\S+|\s", text))
        tokens = [m.group() for m in matches]
        sentence_start, after_clause, skip_space, first = True, False, False, True
        for idx, tok in enumerate(tokens):
            a = matches[idx].start()
            b = a + len(tok)
            if tok.isspace():
                if tok == "\n":
                    self.review()
                    self.ch("\n", S[a])
                    self.pause(u(1.0, 4.5) * self.ps)
                    sentence_start = True
                elif skip_space and tok == " ":
                    pass
                else:
                    self.ch(tok, S[a])
                skip_space = False
                continue
            # Slips are only made where the editor's style after a fix is certain:
            # the word is in one style, and so are the characters around it.
            st, uniform = S[a], len(set(S[a:b])) == 1
            first_ok = S[a - 1] == st if a else st == 0
            settled = uniform and first_ok and (b >= len(S) or S[b] == st)

            # speed drifts over the session, per burst and per word
            self.x += -.08 * self.x + rng.gauss(0, .05)
            bare = tok.strip(".,;:!?\"'()[]").lower()
            hard = not bare.isalpha() or len(bare) >= 9
            self.wordf = (u(.8, .92) if bare in COMMON_WORDS
                          else u(1.12, 1.4) if hard else u(.95, 1.08))

            # pauses: bursts, sentences, clauses, unfamiliar terms
            if self.burst_left <= 0:
                self.pause((u(.35, 1.5) if rng.random() < .85 else u(2.5, 9)) * self.ps)
                self.burst_left = self._burst_len()
                self.burstf = math.exp(rng.gauss(0, .1))
            self.burst_left -= 1
            if first:
                first = False
            elif sentence_start:
                self.pause(min(4.0, .6 * self._ln(.6)) * self.ps)
            elif after_clause and rng.random() < .7:
                self.pause(u(.12, .5) * self.ps)
            elif rng.random() < .08:
                self.pause(u(.15, .6) * self.ps)
            if hard:
                self.pause(u(.2, .9) * self.ps)

            # sometimes leave a slip behind to be revised later
            typed = False
            if (self.revise and settled and tok.isascii()
                    and rng.random() < min(.12, self.error_rate * 1.2)):
                nxt_space = idx + 2 < len(tokens) and tokens[idx + 1] == " "
                if bare in SKIPPABLE and tok.isalpha() and nxt_space and not sentence_start:
                    self.defects.append([self.cur, 0, tok + " ", st])  # word left out
                    skip_space = typed = True
                else:
                    alt = None
                    if tok.lower() in CONFUSABLE:                       # wrong word
                        alt = CONFUSABLE[tok.lower()]
                        alt = alt.capitalize() if tok[0].isupper() else alt
                    elif tok.isalpha() and len(tok) >= 4:               # unnoticed typo
                        alt = self._misspell(tok)
                    if alt:
                        self.defects.append([self.cur, len(alt), tok, st])
                        self.type_str(alt, st)
                        typed = True
            if not typed and uniform:
                self.type_word(tok, st, first_ok)
            elif not typed:
                self.type_str(tok, S[a:b])

            end = tok.rstrip("\"')]")[-1:]
            sentence_start = end in (".", "!", "?")
            after_clause = end in (",", ";", ":")
            if sentence_start and self.defects and (rng.random() < .7 or len(self.defects) >= 3):
                self.review()

        self.review()
        if self.style:
            self._restyle(0)                                 # leave bold/italic off
        if self.shift_down:
            self._emit(self.shift_press + max(.012, self.shift_hold * u(.6, 1.5)),
                       "release", "<shift>")
            self.shift_down = False
        self.ev.sort(key=lambda e: (e[0], e[1]))
        return self.ev


def play(events, press, release, stop, clock=time.monotonic, guard=None, on_pause=None):
    """Replay planned events in real time. Returns False if stopped early.

    If `guard` is given, typing holds while guard() is false and carries on
    from the same place once it is true again.
    """
    down, start = [], clock()
    try:
        for t, _, kind, key in events:
            wait = start + t - clock()
            if (wait > 0 and stop.wait(wait)) or stop.is_set():
                return False
            if kind == "press":
                if guard and not guard():
                    mods = [k for k in down if k in ("<shift>", "<mod>")]
                    for k in reversed(down):
                        release(k)
                    del down[:]
                    paused = clock()
                    if on_pause:
                        on_pause(True)
                    while not guard():
                        if stop.wait(0.2):
                            return False
                    if on_pause:
                        on_pause(False)
                    if stop.wait(0.4):               # let the window settle
                        return False
                    start += clock() - paused
                    for k in mods:
                        press(k)
                        down.append(k)
                press(key)
                down.append(key)
            elif key in down:
                release(key)
                down.remove(key)
        return True
    finally:
        for key in reversed(down):   # never leave a key stuck down
            release(key)


def focus_probe():
    """Return a function that identifies the window holding keyboard focus,
    or None where that can't be read."""
    try:
        if sys.platform == "win32":
            import ctypes
            return ctypes.windll.user32.GetForegroundWindow
        if sys.platform == "darwin":
            import Quartz

            def front():
                opts = (Quartz.kCGWindowListOptionOnScreenOnly
                        | Quartz.kCGWindowListExcludeDesktopElements)
                for w in Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID):
                    if w.get("kCGWindowLayer") == 0:
                        return w.get("kCGWindowOwnerPID")
            return front
        from Xlib import X, display
        d = display.Display()
        root, atom = d.screen().root, d.intern_atom("_NET_ACTIVE_WINDOW")

        def active():
            prop = root.get_full_property(atom, X.AnyPropertyType)
            if prop and len(prop.value) and prop.value[0]:
                return int(prop.value[0])
            w = d.get_input_focus().focus
            return getattr(w, "id", w)
        return active
    except Exception:
        return None


def block_clicks():
    """Swallow mouse clicks system-wide (the pointer still moves).
    Returns a function that lets clicks through again."""
    from pynput import mouse
    if sys.platform == "win32":
        clicks = {0x201, 0x202, 0x203, 0x204, 0x205, 0x206, 0x207, 0x208, 0x209,
                  0x20B, 0x20C, 0x20D}

        def filt(msg, data):
            if msg in clicks:
                listener.suppress_event()
        listener = mouse.Listener(win32_event_filter=filt)
    elif sys.platform == "darwin":
        clicks = {1, 2, 3, 4, 25, 26}                # left/right/other down and up
        listener = mouse.Listener(
            darwin_intercept=lambda etype, event: None if etype in clicks else event)
    else:
        listener = mouse.Listener(suppress=True)
    listener.start()

    def unblock():
        listener.stop()
        if sys.platform not in ("win32", "darwin"):
            # On X11 the grab is only dropped at the next pointer event: nudge it.
            pointer = mouse.Controller()
            pointer.move(1, 0)
            pointer.move(-1, 0)
            listener.join(1)
    return unblock


# Colours follow the NEXUS dark theme: near-black navy, slate text, one cyan accent.
BG, PANEL, LINE = "#070a12", "#0b1020", "#1c2536"
FG, FG2, MUTED = "#e6edf7", "#b7c3d6", "#7e8ba3"
ACCENT, DANGER = "#38bdf8", "#f87171"
MONO_FONTS = ["Cascadia Code", "Cascadia Mono", "JetBrains Mono", "Consolas", "Menlo",
              "DejaVu Sans Mono", "Liberation Mono", "Courier New"]
FORMATS = {  # label: how **bold** / *italic* markers in the text are handled
    "Apply bold / italic": "apply",
    "Drop markers, type plain": "strip",
    "Type exactly as written": "literal",
}


class App:
    def __init__(self, root):
        import tkinter as tk
        from tkinter import font as tkfont
        from tkinter import ttk
        from pynput import keyboard
        self.tk = tk
        self.kb = keyboard
        self.controller = keyboard.Controller()
        K = keyboard.Key
        self.keymap = {"<shift>": K.shift, "<backspace>": K.backspace, "<delete>": K.delete,
                       "<left>": K.left, "<right>": K.right, "<enter>": K.enter,
                       "<tab>": K.tab,
                       "<mod>": K.cmd if sys.platform == "darwin" else K.ctrl}
        self.root = root
        self.stop = threading.Event()
        self.worker = None
        self.events = queue.Queue()  # messages from other threads -> Tk thread
        self._last_hotkey = 0.0
        self._restyle_job = None

        # --- look ---------------------------------------------------------------
        have = set(tkfont.families(root))
        mono = next((f for f in MONO_FONTS if f in have), "TkFixedFont")
        f_ui, f_small = (mono, 10), (mono, 9)
        f_bold, f_head = (mono, 10, "bold"), (mono, 15, "bold")
        root.title("Auto Typer")
        root.configure(bg=BG)
        root.minsize(620, 600)
        root.option_add("*TCombobox*Listbox.background", PANEL)
        root.option_add("*TCombobox*Listbox.foreground", FG)
        root.option_add("*TCombobox*Listbox.selectBackground", LINE)
        root.option_add("*TCombobox*Listbox.selectForeground", ACCENT)
        root.option_add("*TCombobox*Listbox.font", f_ui)
        st = ttk.Style(root)
        st.theme_use("clam")
        st.configure(".", background=BG, foreground=FG, font=f_ui, bordercolor=LINE,
                     lightcolor=BG, darkcolor=BG, troughcolor=PANEL, focuscolor=BG,
                     insertcolor=ACCENT, selectbackground=LINE, selectforeground=FG)
        st.configure("Head.TLabel", foreground=FG, font=f_head)
        st.configure("Accent.TLabel", foreground=ACCENT, font=f_bold)
        st.configure("Dim.TLabel", foreground=MUTED, font=f_small)
        st.configure("Status.TLabel", foreground=FG2)
        st.configure("TEntry", fieldbackground=PANEL, foreground=FG, bordercolor=LINE,
                     lightcolor=PANEL, darkcolor=PANEL, padding=4)
        st.map("TEntry", bordercolor=[("focus", ACCENT)], lightcolor=[("focus", PANEL)])
        st.configure("TCombobox", fieldbackground=PANEL, background=PANEL, foreground=FG,
                     arrowcolor=ACCENT, bordercolor=LINE, lightcolor=PANEL,
                     darkcolor=PANEL, padding=4)
        st.map("TCombobox", fieldbackground=[("readonly", PANEL)],
               foreground=[("readonly", FG)], selectbackground=[("readonly", PANEL)],
               selectforeground=[("readonly", FG)], bordercolor=[("focus", ACCENT)],
               background=[("active", LINE)])
        st.configure("TCheckbutton", foreground=FG2, indicatorbackground=PANEL,
                     indicatorforeground=ACCENT, upperbordercolor=MUTED,
                     lowerbordercolor=MUTED, padding=2)
        st.map("TCheckbutton", background=[("active", BG)], foreground=[("active", FG)],
               indicatorbackground=[("active", PANEL), ("selected", PANEL)],
               upperbordercolor=[("selected", ACCENT)],
               lowerbordercolor=[("selected", ACCENT)])
        st.configure("TButton", background=PANEL, foreground=ACCENT, bordercolor=ACCENT,
                     lightcolor=PANEL, darkcolor=PANEL, font=f_bold, padding=(18, 7))
        st.map("TButton", background=[("pressed", LINE), ("active", "#101a30")],
               lightcolor=[("pressed", LINE), ("active", "#101a30")],
               darkcolor=[("pressed", LINE), ("active", "#101a30")])

        frm = ttk.Frame(root, padding=(18, 14, 18, 14))
        frm.pack(fill="both", expand=True)

        head = ttk.Frame(frm)
        head.pack(fill="x")
        ttk.Label(head, text=">_", style="Accent.TLabel", font=f_head).pack(side="left")
        ttk.Label(head, text=" AUTO TYPER", style="Head.TLabel").pack(side="left")
        ttk.Label(head, text=f"[{HOTKEY_NAME}] start / stop", style="Dim.TLabel").pack(
            side="right", pady=(6, 0))
        tk.Frame(frm, bg=LINE, height=1).pack(fill="x", pady=(10, 12))

        def section(title, hint=""):
            row = ttk.Frame(frm)
            row.pack(fill="x", pady=(0, 5))
            ttk.Label(row, text="// " + title, style="Accent.TLabel").pack(side="left")
            ttk.Label(row, text=hint, style="Dim.TLabel").pack(side="right")

        section("INPUT", "**bold**   *italic*   # heading")
        box = tk.Frame(frm, bg=LINE, highlightthickness=0)
        box.pack(fill="both", expand=True, pady=(0, 14))
        self.text = tk.Text(
            box, height=9, wrap="word", undo=True, bg=PANEL, fg=FG, insertbackground=ACCENT,
            selectbackground=LINE, selectforeground=FG, relief="flat", bd=0, padx=10,
            pady=8, font=(mono, 11), highlightthickness=1, highlightbackground=LINE,
            highlightcolor=ACCENT, insertwidth=2, spacing1=1, spacing3=1)
        self.text.pack(fill="both", expand=True)
        self.text.tag_configure("b", font=(mono, 11, "bold"), foreground="#ffffff")
        self.text.tag_configure("i", font=(mono, 11, "italic"))
        self.text.tag_configure("bi", font=(mono, 11, "bold italic"), foreground="#ffffff")
        self.text.tag_configure("mark", foreground="#3b4a63")
        self.text.bind("<<Modified>>", self._on_modified)

        section("PARAMETERS")
        opts = ttk.Frame(frm)
        opts.pack(fill="x")
        self.wpm = tk.StringVar(value="60")
        self.errors = tk.StringVar(value="2.5")
        self.pauses = tk.StringVar(value="100")
        self.start_s = tk.StringVar(value="3")
        self.repeat = tk.StringVar(value="1")
        self.technique = tk.StringVar(value="Touch typing")
        self.format = tk.StringVar(value=next(iter(FORMATS)))
        self.revise = tk.BooleanVar(value=True)
        self.us_shift = tk.BooleanVar(value=True)
        self.enter_after = tk.BooleanVar(value=False)
        self.lock = tk.BooleanVar(value=False)
        fields = [
            ("speed", "wpm, approx", self.wpm),
            ("mistakes", "% of keys", self.errors),
            ("pauses", "% of normal", self.pauses),
            ("countdown", "seconds", self.start_s),
            ("repeat", "0 = forever", self.repeat),
        ]
        for i, (label, unit, var) in enumerate(fields):
            r, c = i // 2, (i % 2) * 3
            ttk.Label(opts, text=label.upper(), style="Dim.TLabel").grid(
                row=r, column=c, sticky="w", pady=4)
            ttk.Entry(opts, textvariable=var, width=7, font=f_ui).grid(
                row=r, column=c + 1, sticky="w", padx=(10, 8))
            ttk.Label(opts, text=unit, style="Dim.TLabel").grid(
                row=r, column=c + 2, sticky="w", padx=(0, 28))
        ttk.Label(opts, text="TECHNIQUE", style="Dim.TLabel").grid(row=2, column=3, sticky="w")
        ttk.Combobox(opts, textvariable=self.technique, values=list(TECHNIQUES),
                     state="readonly", width=24, font=f_ui).grid(
            row=2, column=4, columnspan=2, sticky="w", padx=(10, 0))
        ttk.Label(opts, text="FORMATTING", style="Dim.TLabel").grid(
            row=3, column=0, sticky="w", pady=4)
        fmt = ttk.Combobox(opts, textvariable=self.format, values=list(FORMATS),
                           state="readonly", width=24, font=f_ui)
        fmt.grid(row=3, column=1, columnspan=3, sticky="w", padx=(10, 0))
        fmt.bind("<<ComboboxSelected>>", lambda e: self._restyle_text())
        checks = [
            ("revise with arrow keys and Delete (off = Backspace only)", self.revise),
            ("US keyboard layout (hold Shift for symbols)", self.us_shift),
            ("press Enter after each repeat", self.enter_after),
            ("lock onto the text box: ignore mouse clicks while typing", self.lock),
        ]
        for i, (label, var) in enumerate(checks):
            ttk.Checkbutton(opts, text=label, variable=var).grid(
                row=4 + i, column=0, columnspan=6, sticky="w", pady=(10 if i == 0 else 1, 0))

        tk.Frame(frm, bg=LINE, height=1).pack(fill="x", pady=(14, 12))
        bar = ttk.Frame(frm)
        bar.pack(fill="x")
        self.button = ttk.Button(bar, text="[ START ]", command=self.toggle, cursor="hand2")
        self.button.pack(side="left")
        ttk.Label(bar, text=">", style="Accent.TLabel").pack(side="left", padx=(14, 6))
        self.status = tk.StringVar(value=f"ready. {HOTKEY_NAME} starts and stops from any window")
        self.status_label = ttk.Label(bar, textvariable=self.status, style="Status.TLabel")
        self.status_label.pack(side="left")

        self._dark_title_bar()
        self.listener = keyboard.Listener(on_press=self._on_key)
        self.listener.daemon = True
        self.listener.start()
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._poll()

    def _dark_title_bar(self):
        if sys.platform != "win32":
            return
        try:
            import ctypes
            self.root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            on = ctypes.c_int(1)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(on), 4)
        except Exception:
            pass

    # --- formatting preview in the text box -----------------------------------
    def _on_modified(self, _event=None):
        if self.text.edit_modified():
            self.text.edit_modified(False)
            if self._restyle_job:
                self.root.after_cancel(self._restyle_job)
            self._restyle_job = self.root.after(120, self._restyle_text)

    def _restyle_text(self):
        """Show marked text as bold/italic and grey out the markers themselves."""
        self._restyle_job = None
        t = self.text
        for tag in ("b", "i", "bi", "mark"):
            t.tag_remove(tag, "1.0", "end")
        if FORMATS[self.format.get()] == "literal":
            return
        src = t.get("1.0", "end-1c")
        tags = ["mark"] * len(src)
        for i, _c, style in parse_markup(src):
            tags[i] = {0: None, BOLD: "b", ITALIC: "i"}.get(style, "bi")
        start = 0
        for i in range(1, len(src) + 1):
            if i == len(src) or tags[i] != tags[start]:
                if tags[start]:
                    t.tag_add(tags[start], f"1.0+{start}c", f"1.0+{i}c")
                start = i

    # --- cross-thread plumbing -------------------------------------------
    def _on_key(self, key):
        # Debounced: some systems report one physical press twice.
        if key == self.kb.Key.f6 and time.monotonic() - self._last_hotkey > 0.3:
            self._last_hotkey = time.monotonic()
            self.events.put(("toggle", None))

    def _poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "toggle":
                    self.toggle()
                elif kind == "status":
                    self._say(value)
                elif kind == "done":
                    self.button.config(text="[ START ]")
                    self._say(value)
        except queue.Empty:
            pass
        self.root.after(50, self._poll)

    def _say(self, message):
        self.status.set(message[:1].lower() + message[1:])
        bad = message.startswith(("Error", "Nothing", "Settings"))
        self.status_label.configure(foreground=DANGER if bad else FG2)

    # --- actions ------------------------------------------------------------
    def running(self):
        return self.worker is not None and self.worker.is_alive()

    def toggle(self):
        if self.running():
            self.stop.set()
            return
        text = self.text.get("1.0", "end-1c")
        if not text:
            self._say("Nothing to type.")
            return
        try:
            opts = dict(
                wpm=min(250.0, max(5.0, float(self.wpm.get()))),
                error_rate=min(20.0, max(0.0, float(self.errors.get()))) / 100,
                pause_scale=min(5.0, max(0.0, float(self.pauses.get()) / 100)),
                technique=self.technique.get(),
                revise=self.revise.get(),
                us_shift=self.us_shift.get())
            countdown = max(0, int(float(self.start_s.get())))
            repeat = max(0, int(float(self.repeat.get())))
        except ValueError:
            self._say("Settings must be numbers.")
            return
        self.stop.clear()
        mode, styles = FORMATS[self.format.get()], None
        if mode != "literal":
            parsed = parse_markup(text)
            text = "".join(c for _, c, _ in parsed)
            if mode == "apply" and any(s for _, _, s in parsed):
                styles = [s for _, _, s in parsed]
        self.button.config(text="[ STOP ]")
        self.worker = threading.Thread(
            target=self._run, daemon=True,
            args=(text, styles, opts, countdown, repeat, self.enter_after.get(),
                  self.lock.get()))
        self.worker.start()

    def _run(self, text, styles, opts, countdown, repeat, enter_after, lock):
        for s in range(countdown, 0, -1):
            self.events.put(("status", f"Starting in {s}... click the target window"))
            if self.stop.wait(1):
                self.events.put(("done", "Cancelled."))
                return
        press = lambda k: self.controller.press(self.keymap.get(k, k))
        release = lambda k: self.controller.release(self.keymap.get(k, k))
        guard, unblock, typing, note = None, None, "Typing", ""
        if lock:
            # The target is whatever has focus the moment typing begins.
            probe = focus_probe()
            if probe:
                target, seen = probe(), [0.0, True]

                def guard():
                    if time.monotonic() - seen[0] > 0.1:
                        seen[:] = [time.monotonic(), probe() == target]
                    return seen[1]
            try:
                unblock = block_clicks()
                typing = "Locked"
            except Exception:
                note = " (could not block clicks)"

        def on_pause(paused):
            self.events.put(("status", "Paused - go back to the target window"
                             if paused else f"{typing} - {HOTKEY_NAME} to stop"))
        n = 0
        try:
            while not self.stop.is_set() and (repeat == 0 or n < repeat):
                plan = Humanizer(**opts).run(text, styles)   # a fresh rhythm every time
                secs = int(plan[-1][0]) if plan else 0
                self.events.put(("status", f"{typing}, about {secs // 60}:{secs % 60:02d}"
                                           f" - {HOTKEY_NAME} to stop{note}"))
                if not play(plan, press, release, self.stop, guard=guard, on_pause=on_pause):
                    break
                if enter_after:
                    self.stop.wait(random.uniform(.2, .7))
                    self.controller.tap(self.kb.Key.enter)
                n += 1
                if repeat == 0 or n < repeat:
                    self.stop.wait(random.uniform(.6, 2.5))
        except Exception as e:  # e.g. a character the keyboard layout can't produce
            self.events.put(("done", f"Error: {e}"))
            return
        finally:
            if unblock:
                unblock()
        self.events.put(("done", "Stopped." if self.stop.is_set() else "Done."))

    def close(self):
        self.stop.set()
        self.listener.stop()
        self.root.destroy()


def main():
    try:
        import pynput  # noqa: F401
    except ImportError:
        raise SystemExit("Missing dependency. Run:  pip install pynput")
    import tkinter as tk
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
