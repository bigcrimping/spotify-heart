"""A small status window: heart rate, what is playing, what plays next.

`view()` turns a DjStatus into exactly what appears on screen and contains no
Tk, so it is unit-tested directly. `Window` is the Tk shell around it: a dark
card layout drawn with plain tk widgets, because ttk will not theme reliably
across Windows versions.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable

from .dj import LOGIN, PLAYING, WAIT_HR_LOCK, DjStatus
from .tray import COLOURS

log = logging.getLogger(__name__)

POLL_MS = 250
TITLE = "Spotify Heart"

BG = "#14161c"
CARD = "#1e2129"
EDGE = "#2a2e39"
TEXT = "#e8eaf0"
MUTED = "#8b90a0"
DIM = "#5a5f6d"
BTN = "#262a34"
BTN_HOVER = "#333845"
ON = "#3a4150"          # a selected segment

# The tempo choices, in the order they appear.
TEMPO_CHOICES = [("auto", "Auto"), ("half", "Half"), ("same", "Heartbeat"), ("double", "Double")]

FONT = "Segoe UI"


def hexc(colour: str) -> str:
    return "#%02x%02x%02x" % COLOURS.get(colour, COLOURS["grey"])


@dataclass(frozen=True)
class View:
    hr: str
    hr_note: str
    colour: str
    now: str
    now_title: str
    now_artist: str
    progress: float  # 0..1, -1 when nothing is playing
    queue_mark: float  # 0..1 position of the queue moment, -1 when unknown
    remaining: str
    next_up: str
    next_title: str
    next_artist: str
    tempo_chip: str
    next_note: str
    message: str
    paused: bool
    can_skip: bool
    can_refresh: bool
    tempo_mode: str
    tempo_note: str
    beat_ms: int  # milliseconds between beats, 0 when not locked


def _hr_lines(st: DjStatus) -> tuple[str, str]:
    if not st.sensor_ok:
        return "--", f"sensor: {st.sensor_text or 'not connected'}"
    if st.hr and st.hr.locked:
        return f"{st.hr.bpm}", f"steady over {st.hr.n} readings"
    if st.hr_bpm:
        return f"{st.hr_bpm}", "stale, sit still again" if st.hr_stale else "last steady reading"
    return "--", "sit still in front of the sensor"


def tempo_note(st: DjStatus) -> str:
    """What the current tempo choice means for this heart rate."""
    hr = st.hr.bpm if st.hr and st.hr.locked else st.hr_bpm
    if not hr:
        return {"auto": "any of half, heartbeat or double",
                "half": "half your heart rate",
                "same": "your heart rate",
                "double": "double your heart rate"}[st.tempo_mode]
    if st.tempo_mode == "auto":
        return f"around {round(hr / 2)}, {hr} or {hr * 2} BPM"
    target = {"half": round(hr / 2), "same": hr, "double": hr * 2}[st.tempo_mode]
    from .selector import TEMPO_MAX, TEMPO_MIN

    if not TEMPO_MIN <= target <= TEMPO_MAX:
        return f"{target} BPM is out of range, matching freely"
    return f"around {target} BPM"


def mmss(ms: int) -> str:
    total = max(int(ms // 1000), 0)
    return f"{total // 60}:{total % 60:02d}"


def view(st: DjStatus, elapsed_s: float = 0.0) -> View:
    """`elapsed_s` is the time since the status was taken, so the progress bar
    keeps moving between the DJ's polls."""
    hr, hr_note = _hr_lines(st)

    progress = queue_mark = -1.0
    remaining = ""
    if st.now_duration_ms > 0 and st.now_playing_id:
        played = st.now_progress_ms + (elapsed_s * 1000 if not st.paused else 0)
        played = min(max(played, 0), st.now_duration_ms)
        progress = played / st.now_duration_ms
        queue_mark = max(0.0, (st.now_duration_ms - st.queue_lead_ms) / st.now_duration_ms)
        remaining = f"{mmss(st.now_duration_ms - played)} left"

    next_title = next_artist = tempo_chip = ""
    if st.state == LOGIN:
        now, next_up, next_note = "", "", "log in to Spotify first"
    else:
        now = st.now_playing or "Nothing playing"
        if st.next_pick is None:
            next_up = ""
            if st.state == WAIT_HR_LOCK:
                next_note = "waiting for a steady heart rate"
            elif not st.now_playing:
                next_note = "press play in Spotify"
            else:
                next_note = "choosing a match..."
        else:
            p = st.next_pick
            next_up = f"{p.title} — {p.artist}"
            next_title, next_artist = p.title, p.artist
            match = {2.0: " · double", 0.5: " · half"}.get(p.multiplier, "")
            tempo_chip = f"{p.tempo} BPM{match}"
            next_note = "queued, plays next" if st.next_queued else "will queue shortly"

    return View(
        hr=hr,
        hr_note=hr_note,
        colour=st.colour,
        now=now,
        now_title=st.now_title or ("Nothing playing" if st.state != LOGIN else ""),
        now_artist=st.now_artist,
        progress=progress,
        queue_mark=queue_mark,
        remaining=remaining,
        next_up=next_up,
        next_title=next_title,
        next_artist=next_artist,
        tempo_chip=tempo_chip,
        next_note=next_note,
        message=st.error or st.message,
        tempo_mode=st.tempo_mode,
        tempo_note=tempo_note(st),
        paused=st.paused,
        can_skip=st.state == PLAYING and not st.paused,
        can_refresh=st.state == PLAYING and st.next_pick is not None and not st.next_queued,
        beat_ms=int(60_000 / st.hr.bpm) if st.hr and st.hr.locked and st.hr.bpm else 0,
    )


class Window:
    """Tk window driven by `get_status`. Runs on the main thread."""

    def __init__(
        self,
        get_status: Callable[[], DjStatus],
        stop: threading.Event,
        on_skip: Callable[[], None],
        on_pause: Callable[[bool], None],
        on_login: Callable[[], None],
        on_quit: Callable[[], None],
        show_request: threading.Event,
        on_tempo_mode: Callable[[str], None] = lambda mode: None,
        on_refresh: Callable[[], None] = lambda: None,
        start_hidden: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ):
        import tkinter as tk  # here, so a machine without Tk is not fatal

        self.tk = tk
        self.get_status = get_status
        self.stop = stop
        self.on_skip = on_skip
        self.on_pause = on_pause
        self.on_login = on_login
        self.on_quit = on_quit
        self.on_tempo_mode = on_tempo_mode
        self.on_refresh = on_refresh
        self.show_request = show_request
        self.clock = clock
        self._beat_at = 0.0
        self._paused_state = False

        self.root = tk.Tk()
        self.root.title(TITLE)
        self.root.configure(bg=BG)
        self._build()
        # Size to what the layout actually needs, so nothing is ever clipped.
        self.root.update_idletasks()
        w = max(self.root.winfo_reqwidth(), 440)
        h = self.root.winfo_reqheight()
        self.root.geometry(f"{w}x{h}")
        self.root.minsize(w, h)
        if start_hidden:
            self.root.withdraw()

    # ------------------------------------------------------------- pieces

    def _label(self, parent, text="", size=10, bold=False, fg=TEXT, bg=CARD,
               anchor="w", **kw):
        return self.tk.Label(
            parent, text=text, bg=bg, fg=fg,
            font=(FONT, size, "bold" if bold else "normal"),
            anchor=anchor, justify="left", **kw
        )

    def _card(self, parent):
        tk = self.tk
        edge = tk.Frame(parent, bg=EDGE)
        edge.pack(fill="x", padx=16, pady=(0, 10))
        inner = tk.Frame(edge, bg=CARD)
        inner.pack(fill="x", padx=1, pady=1)
        return inner

    def _button(self, parent, text, command, primary=False):
        tk = self.tk
        b = tk.Button(
            parent, text=text, command=command, bd=0, relief="flat",
            bg=BTN, fg=TEXT, activebackground=BTN_HOVER, activeforeground=TEXT,
            disabledforeground=DIM, font=(FONT, 9, "bold" if primary else "normal"),
            padx=16, pady=9, cursor="hand2", highlightthickness=0,
        )
        b.bind("<Enter>", lambda e: b["state"] == "normal" and b.configure(bg=BTN_HOVER))
        b.bind("<Leave>", lambda e: b.configure(bg=BTN))

        return b

    def _build(self) -> None:
        tk = self.tk

        head = tk.Frame(self.root, bg=BG)
        head.pack(fill="x", padx=16, pady=(16, 14))
        self.heart = tk.Canvas(head, width=42, height=42, bg=BG, highlightthickness=0)
        self.heart.pack(side="left")
        self._draw_heart(1.0)

        nums = tk.Frame(head, bg=BG)
        nums.pack(side="left", padx=(12, 0))
        row = tk.Frame(nums, bg=BG)
        row.pack(anchor="w")
        self.hr_var = tk.StringVar(value="--")
        self._label(row, size=30, bold=True, bg=BG, textvariable=self.hr_var).pack(side="left")
        self._label(row, " BPM", size=10, fg=MUTED, bg=BG).pack(side="left", pady=(14, 0))
        self.hr_note_var = tk.StringVar()
        self._label(nums, size=9, fg=MUTED, bg=BG, textvariable=self.hr_note_var).pack(anchor="w")

        tempo = tk.Frame(head, bg=BG)
        tempo.pack(side="right", anchor="n")
        self._label(tempo, "TEMPO", size=8, fg=DIM, bg=BG, anchor="e").pack(fill="x")
        seg = tk.Frame(tempo, bg=BG)
        seg.pack(anchor="e", pady=(4, 0))
        self.mode_btns = {}
        for mode, label in TEMPO_CHOICES:
            b = tk.Button(
                seg, text=label, bd=0, relief="flat", bg=BTN, fg=TEXT,
                activebackground=BTN_HOVER, activeforeground=TEXT,
                font=(FONT, 9), padx=10, pady=5, cursor="hand2", highlightthickness=0,
                command=lambda m=mode: self.on_tempo_mode(m),
            )
            b.pack(side="left", padx=(6, 0))
            self.mode_btns[mode] = b
        self.tempo_note_var = tk.StringVar()
        self._label(tempo, size=9, fg=MUTED, bg=BG, anchor="e",
                    textvariable=self.tempo_note_var).pack(fill="x", pady=(6, 0))

        # Now playing
        card = self._card(self.root)
        self._label(card, "NOW PLAYING", size=8, fg=DIM).pack(anchor="w", padx=14, pady=(12, 2))
        self.now_title_var = tk.StringVar()
        self.now_artist_var = tk.StringVar()
        self._label(card, size=11, bold=True, textvariable=self.now_title_var,
                    wraplength=380).pack(anchor="w", padx=14)
        self._label(card, size=9, fg=MUTED, textvariable=self.now_artist_var,
                    wraplength=380).pack(anchor="w", padx=14)
        bar_row = tk.Frame(card, bg=CARD)
        bar_row.pack(fill="x", padx=14, pady=(10, 12))
        self.bar = tk.Canvas(bar_row, height=6, bg=CARD, highlightthickness=0)
        self.bar.pack(side="left", fill="x", expand=True)
        self.remaining_var = tk.StringVar()
        self._label(bar_row, size=8, fg=MUTED, textvariable=self.remaining_var,
                    width=8, anchor="e").pack(side="right", padx=(8, 0))

        # Next up
        card = self._card(self.root)
        top = tk.Frame(card, bg=CARD)
        top.pack(fill="x", padx=14, pady=(12, 2))
        self._label(top, "NEXT UP", size=8, fg=DIM).pack(side="left")
        self.refresh_btn = tk.Button(
            top, text="↻  Pick another", command=self._refresh_pick, bd=0, relief="flat",
            bg=CARD, fg=MUTED, activebackground=CARD, activeforeground=TEXT,
            disabledforeground=DIM, font=(FONT, 8), padx=6, pady=0,
            cursor="hand2", highlightthickness=0,
        )
        self.refresh_btn.bind("<Enter>", lambda e: self.refresh_btn["state"] == "normal"
                              and self.refresh_btn.configure(fg=TEXT))
        self.refresh_btn.bind("<Leave>", lambda e: self.refresh_btn.configure(fg=MUTED))
        self.refresh_btn.pack(side="left", padx=(10, 0))
        self.chip_var = tk.StringVar()
        self.chip = self._label(top, size=8, fg=TEXT, bg=EDGE, textvariable=self.chip_var,
                                padx=8, pady=2)
        self.chip.pack(side="right")
        self.next_title_var = tk.StringVar()
        self.next_artist_var = tk.StringVar()
        self.next_note_var = tk.StringVar()
        self._label(card, size=11, bold=True, textvariable=self.next_title_var,
                    wraplength=380).pack(anchor="w", padx=14)
        self._label(card, size=9, fg=MUTED, textvariable=self.next_artist_var,
                    wraplength=380).pack(anchor="w", padx=14)
        self._label(card, size=9, fg=MUTED, textvariable=self.next_note_var,
                    wraplength=380).pack(anchor="w", padx=14, pady=(6, 12))

        # Message strip: only takes space when there is something to say.
        self.msg_var = tk.StringVar()
        self.msg = self._label(self.root, size=9, fg="#f0aa1e", bg=BG,
                               textvariable=self.msg_var, wraplength=440)
        self.msg.pack(fill="x", padx=16, pady=(0, 2))

        buttons = tk.Frame(self.root, bg=BG)
        buttons.pack(fill="x", padx=16, pady=(6, 16))
        self.pause_text = tk.StringVar(value="Pause")
        self.pause_btn = self._button(buttons, "Pause", self._pause)
        self.pause_btn.configure(textvariable=self.pause_text)
        self.pause_btn.pack(side="left")
        self.skip_btn = self._button(buttons, "Skip", self._skip, primary=True)
        self.skip_btn.pack(side="left", padx=8)
        self.login_btn = self._button(buttons, "Log in", self.on_login)
        self.login_btn.pack(side="left")
        self._button(buttons, "Quit", self.on_quit).pack(side="right")
        self._button(buttons, "Hide", self.hide).pack(side="right", padx=8)

        self.root.protocol("WM_DELETE_WINDOW", self.hide)

    # ------------------------------------------------------------ drawing

    def _draw_heart(self, scale: float, colour: str = "grey") -> None:
        """The same shape as the tray icon, beating with the heart rate."""
        c = self.heart
        c.delete("all")
        s = 42.0
        cx, cy = s / 2, s / 2
        w = s * 0.82 * scale
        x0, y0 = cx - w / 2, cy - w / 2
        r = w * 0.26
        fill = hexc(colour)
        c.create_oval(x0, y0 + w * 0.06, x0 + 2 * r, y0 + w * 0.06 + 2 * r, fill=fill, outline="")
        c.create_oval(x0 + w - 2 * r, y0 + w * 0.06, x0 + w, y0 + w * 0.06 + 2 * r,
                      fill=fill, outline="")
        c.create_polygon(
            x0 + w * 0.02, y0 + w * 0.43,
            x0 + w * 0.98, y0 + w * 0.43,
            x0 + w * 0.50, y0 + w * 0.97,
            fill=fill, outline="",
        )

    def _draw_bar(self, v: View) -> None:
        bar = self.bar
        bar.delete("all")
        w = max(bar.winfo_width(), 1)
        h = 6
        bar.create_rectangle(0, 0, w, h, fill=EDGE, outline="")
        if v.progress >= 0:
            bar.create_rectangle(0, 0, w * v.progress, h, fill=hexc(v.colour), outline="")
            if 0 < v.queue_mark < 1:
                x = w * v.queue_mark
                bar.create_rectangle(x - 1, 0, x + 1, h, fill=MUTED, outline="")

    # ------------------------------------------------------------ actions

    def _skip(self) -> None:
        self.skip_btn.configure(state="disabled")
        self.root.after(1500, lambda: self.skip_btn.configure(state="normal"))
        self.on_skip()

    def _refresh_pick(self) -> None:
        self.refresh_btn.configure(state="disabled")
        self.root.after(1200, lambda: self.refresh_btn.configure(state="normal"))
        self.on_refresh()

    def _pause(self) -> None:
        self.on_pause(not self._paused_state)

    def show(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide(self) -> None:
        self.root.withdraw()

    # ------------------------------------------------------------ refresh

    def refresh(self) -> None:
        st = self.get_status()
        elapsed = max(self.clock() - st.now_sampled_at, 0.0) if st.now_sampled_at else 0.0
        v = view(st, elapsed)

        self.hr_var.set(v.hr)
        self.hr_note_var.set(v.hr_note)
        self.now_title_var.set(v.now_title)
        self.now_artist_var.set(v.now_artist)
        self.remaining_var.set(v.remaining)
        self.next_title_var.set(v.next_title or "—")
        self.next_artist_var.set(v.next_artist)
        self.next_note_var.set(v.next_note)
        self.chip_var.set(v.tempo_chip)
        self.chip.configure(bg=EDGE if v.tempo_chip else CARD)
        self._paused_state = v.paused
        for mode, b in self.mode_btns.items():
            on = mode == v.tempo_mode
            b.configure(bg=ON if on else BTN, font=(FONT, 9, "bold" if on else "normal"))
        self.tempo_note_var.set(v.tempo_note)
        self.pause_text.set("Resume" if v.paused else "Pause")
        self.skip_btn.configure(state="normal" if v.can_skip else "disabled")
        self.refresh_btn.configure(state="normal" if v.can_refresh else "disabled")
        self._draw_bar(v)

        self.msg_var.set(v.message)
        self._ensure_height()

        # A gentle beat in time with the heart rate.
        now = self.clock()
        beating = v.beat_ms and not v.paused
        if beating and now - self._beat_at >= v.beat_ms / 1000:
            self._beat_at = now
            self._draw_heart(1.12, v.colour)
            self.root.after(110, lambda c=v.colour: self._draw_heart(1.0, c))
        elif not beating:
            self._draw_heart(1.0, v.colour)

        self.root.title(f"{TITLE} — {v.hr} BPM" if v.hr != "--" else TITLE)

    def _ensure_height(self) -> None:
        """Grow if the content needs more room, e.g. a message that wraps.
        Never shrink: that would make the window jitter as text changes."""
        self.root.update_idletasks()
        needed = self.root.winfo_reqheight()
        if needed > self.root.winfo_height():
            self.root.geometry(f"{self.root.winfo_width()}x{needed}")
            self.root.minsize(self.root.winfo_width(), needed)

    def _tick(self) -> None:
        if self.stop.is_set():
            self.root.destroy()
            return
        if self.show_request.is_set():
            self.show_request.clear()
            self.show()
        try:
            self.refresh()
        except Exception:
            log.exception("[window] refresh failed")
        self.root.after(POLL_MS, self._tick)

    def run(self) -> None:
        self.refresh()
        self.root.after(POLL_MS, self._tick)
        self.root.mainloop()
