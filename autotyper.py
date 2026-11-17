#!/usr/bin/env python3
"""Auto Typer - types text into whatever window has focus, the way a person would.

Nothing is typed on a fixed rhythm. Key hold times, gaps between keys, key
overlap, bursts, pauses, typos with corrections and later revisions are all
drawn fresh for every run.

Setup:   pip install pynput
Run:     python autotyper.py
Use:     enter text, press Start (or F6), click into the target window
         before the countdown ends. F6 again (or Stop) cancels.

Note: corrections assume a plain text field. Turn off autocorrect,
auto-indent and auto-closing brackets in the target, or untick the
arrow-key option, or the result can drift from the text you entered.
"""
import math
import queue
import random
import re
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
            ts = max(tp - .09 * self._ln(.35), self.t + .008, self.shift_rel + .02)
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

    # --- low level: keys that edit the buffer ---------------------------------
    def ch(self, c):
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
            del self.buf[self.cur]

    def delete(self, n=1):
        for _ in range(n):
            self._special("<delete>", self._nav_gap())
            del self.buf[self.cur]

    def move(self, n):
        """Move the cursor n characters (negative = left)."""
        fast = abs(n) > 6
        for i in range(abs(n)):
            g = self._nav_gap(fast and i > 0) + (self.rng.uniform(.1, .3) if i == 0 else 0)
            self._special("<left>" if n < 0 else "<right>", g)
        self.cur += n

    def type_str(self, s):
        for c in s:
            self.ch(c)

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

    def type_word(self, w):
        rng, i, arrows = self.rng, 0, self.revise
        while i < len(w):
            c, left = w[i], len(w) - i - 1
            p = self.error_rate * (1.4 if self.wordf > 1.1 else 1.0)
            if not (c.isascii() and c.isalnum()) or rng.random() >= p:
                self.ch(c)
                i += 1
                continue
            kind = rng.choice(["sub", "sub", "sub", "double", "omit", "swap"])
            if kind == "omit" and left >= 1:
                k = min(rng.choice([1, 1, 2, 3]), left)
                self.type_str(w[i + 1:i + 1 + k])
                self._notice()
                if arrows and rng.random() < .35:
                    self.move(-k); self.ch(c); self.move(k)
                else:
                    self.backspace(k); self.type_str(w[i:i + 1 + k])
                i += k + 1
            elif kind == "swap" and left >= 1 and w[i + 1] != c and w[i + 1].isalnum():
                k = min(rng.choice([0, 0, 1]), left - 1)
                self.ch(w[i + 1]); self.ch(c); self.type_str(w[i + 2:i + 2 + k])
                self._notice()
                self.backspace(2 + k); self.type_str(w[i:i + 2 + k])
                i += 2 + k
            elif kind == "double":
                k = min(rng.choice([0, 0, 1]), left)
                self.ch(c); self.ch(c); self.type_str(w[i + 1:i + 1 + k])
                self._notice()
                self.backspace(k + 1); self.type_str(w[i + 1:i + 1 + k])
                i += k + 1
            else:
                wrong = self._wrong(c)
                if wrong is None:
                    self.ch(c)
                    i += 1
                    continue
                k = min(rng.choice([0, 0, 0, 1, 1, 2, 3]), left)
                self.ch(wrong); self.type_str(w[i + 1:i + 1 + k])
                self._notice()
                r = rng.random()
                if arrows and k >= 1 and r < .2:
                    self.move(-k); self.backspace(); self.ch(c); self.move(k)
                elif arrows and k >= 1 and r < .35:
                    self.move(-k - 1); self.delete(); self.ch(c); self.move(k)
                else:
                    self.backspace(k + 1); self.type_str(w[i:i + 1 + k])
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
        for pos, wlen, correct in reversed(self.defects):   # last slip first
            if 0 < wlen <= 2 and self.rng.random() < .35:
                self.move(pos - self.cur)
                self.pause(u(.15, .5))
                self.delete(wlen)
            else:
                self.move(pos + wlen - self.cur)
                self.pause(u(.15, .5))
                self.backspace(wlen)
            self.type_str(correct)
            self.pause(u(.2, .7) * self.ps + .1)
        self.defects = []
        self.move(len(self.buf) - self.cur)
        self.pause(u(.25, .9) * self.ps + .1)

    # --- the session --------------------------------------------------------------
    def _burst_len(self):
        return int(self.rng.triangular(2, 16, 5))

    def run(self, text):
        rng, u = self.rng, self.rng.uniform
        tokens = re.findall(r"\S+|\s", text)
        sentence_start, after_clause, skip_space, first = True, False, False, True
        for idx, tok in enumerate(tokens):
            if tok.isspace():
                if tok == "\n":
                    self.review()
                    self.ch("\n")
                    self.pause(u(1.0, 4.5) * self.ps)
                    sentence_start = True
                elif skip_space and tok == " ":
                    pass
                else:
                    self.ch(tok)
                skip_space = False
                continue

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
            if self.revise and tok.isascii() and rng.random() < min(.12, self.error_rate * 1.2):
                nxt_space = idx + 2 < len(tokens) and tokens[idx + 1] == " "
                if bare in SKIPPABLE and tok.isalpha() and nxt_space and not sentence_start:
                    self.defects.append([self.cur, 0, tok + " "])      # word left out
                    skip_space = typed = True
                else:
                    alt = None
                    if tok.lower() in CONFUSABLE:                       # wrong word
                        alt = CONFUSABLE[tok.lower()]
                        alt = alt.capitalize() if tok[0].isupper() else alt
                    elif tok.isalpha() and len(tok) >= 4:               # unnoticed typo
                        alt = self._misspell(tok)
                    if alt:
                        self.defects.append([self.cur, len(alt), tok])
                        self.type_str(alt)
                        typed = True
            if not typed:
                self.type_word(tok)

            end = tok.rstrip("\"')]")[-1:]
            sentence_start = end in (".", "!", "?")
            after_clause = end in (",", ";", ":")
            if sentence_start and self.defects and (rng.random() < .7 or len(self.defects) >= 3):
                self.review()

        self.review()
        if self.shift_down:
            self._emit(self.shift_press + max(.012, self.shift_hold * u(.6, 1.5)),
                       "release", "<shift>")
            self.shift_down = False
        self.ev.sort(key=lambda e: (e[0], e[1]))
        return self.ev


def play(events, press, release, stop, clock=time.monotonic):
    """Replay planned events in real time. Returns False if stopped early."""
    down, start = [], clock()
    try:
        for t, _, kind, key in events:
            wait = start + t - clock()
            if (wait > 0 and stop.wait(wait)) or stop.is_set():
                return False
            if kind == "press":
                press(key)
                down.append(key)
            else:
                release(key)
                if key in down:
                    down.remove(key)
        return True
    finally:
        for key in reversed(down):   # never leave a key stuck down
            release(key)


class App:
    def __init__(self, root):
        import tkinter as tk
        from tkinter import ttk
        from pynput import keyboard
        self.kb = keyboard
        self.controller = keyboard.Controller()
        K = keyboard.Key
        self.keymap = {"<shift>": K.shift, "<backspace>": K.backspace, "<delete>": K.delete,
                       "<left>": K.left, "<right>": K.right, "<enter>": K.enter,
                       "<tab>": K.tab}
        self.root = root
        self.stop = threading.Event()
        self.worker = None
        self.events = queue.Queue()  # messages from other threads -> Tk thread
        self._last_hotkey = 0.0

        root.title("Auto Typer")
        root.minsize(480, 430)
        frm = ttk.Frame(root, padding=12)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="Text to type:").pack(anchor="w")
        self.text = tk.Text(frm, height=8, wrap="word", undo=True)
        self.text.pack(fill="both", expand=True, pady=(2, 10))

        opts = ttk.Frame(frm)
        opts.pack(fill="x")
        self.wpm = tk.StringVar(value="60")
        self.errors = tk.StringVar(value="2.5")
        self.pauses = tk.StringVar(value="100")
        self.start_s = tk.StringVar(value="3")
        self.repeat = tk.StringVar(value="1")
        self.technique = tk.StringVar(value="Touch typing")
        self.revise = tk.BooleanVar(value=True)
        self.us_shift = tk.BooleanVar(value=True)
        self.enter_after = tk.BooleanVar(value=False)
        fields = [
            ("Base speed (WPM, approx)", self.wpm),
            ("Mistakes (% of keys)", self.errors),
            ("Pauses and breaks (%)", self.pauses),
            ("Start countdown (s)", self.start_s),
            ("Repeat (0 = forever)", self.repeat),
        ]
        for i, (label, var) in enumerate(fields):
            ttk.Label(opts, text=label).grid(row=i // 2, column=(i % 2) * 2, sticky="w", pady=2)
            ttk.Entry(opts, textvariable=var, width=7).grid(
                row=i // 2, column=(i % 2) * 2 + 1, sticky="w", padx=(6, 18))
        ttk.Label(opts, text="Technique").grid(row=2, column=2, sticky="w")
        ttk.Combobox(opts, textvariable=self.technique, values=list(TECHNIQUES),
                     state="readonly", width=13).grid(row=2, column=3, sticky="w", padx=(6, 0))
        checks = [
            ("Revise with arrow keys and Delete (off = Backspace only)", self.revise),
            ("US keyboard layout (hold Shift for symbols)", self.us_shift),
            ("Press Enter after each repeat", self.enter_after),
        ]
        for i, (label, var) in enumerate(checks):
            ttk.Checkbutton(opts, text=label, variable=var).grid(
                row=3 + i, column=0, columnspan=4, sticky="w", pady=(6 if i == 0 else 0, 0))

        bar = ttk.Frame(frm)
        bar.pack(fill="x", pady=(12, 0))
        self.button = ttk.Button(bar, text=f"Start ({HOTKEY_NAME})", command=self.toggle)
        self.button.pack(side="left")
        self.status = tk.StringVar(value=f"Ready. {HOTKEY_NAME} starts/stops from any window.")
        ttk.Label(bar, textvariable=self.status).pack(side="left", padx=10)

        self.listener = keyboard.Listener(on_press=self._on_key)
        self.listener.daemon = True
        self.listener.start()
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._poll()

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
                    self.status.set(value)
                elif kind == "done":
                    self.button.config(text=f"Start ({HOTKEY_NAME})")
                    self.status.set(value)
        except queue.Empty:
            pass
        self.root.after(50, self._poll)

    # --- actions ------------------------------------------------------------
    def running(self):
        return self.worker is not None and self.worker.is_alive()

    def toggle(self):
        if self.running():
            self.stop.set()
            return
        text = self.text.get("1.0", "end-1c")
        if not text:
            self.status.set("Nothing to type.")
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
            self.status.set("Settings must be numbers.")
            return
        self.stop.clear()
        self.button.config(text=f"Stop ({HOTKEY_NAME})")
        self.worker = threading.Thread(
            target=self._run, daemon=True,
            args=(text, opts, countdown, repeat, self.enter_after.get()))
        self.worker.start()

    def _run(self, text, opts, countdown, repeat, enter_after):
        for s in range(countdown, 0, -1):
            self.events.put(("status", f"Starting in {s}... click the target window"))
            if self.stop.wait(1):
                self.events.put(("done", "Cancelled."))
                return
        press = lambda k: self.controller.press(self.keymap.get(k, k))
        release = lambda k: self.controller.release(self.keymap.get(k, k))
        n = 0
        try:
            while not self.stop.is_set() and (repeat == 0 or n < repeat):
                plan = Humanizer(**opts).run(text)   # a fresh rhythm every time
                secs = int(plan[-1][0]) if plan else 0
                self.events.put(("status", f"Typing, about {secs // 60}:{secs % 60:02d}"
                                           f" - {HOTKEY_NAME} to stop"))
                if not play(plan, press, release, self.stop):
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
