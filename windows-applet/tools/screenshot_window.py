"""Save screenshots of the status window for the README.

Uses made-up tracks and a fixed heart rate, so the shots are reproducible and
carry nothing personal.

    python tools/screenshot_window.py [outdir]     (default: docs/)
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import ImageGrab  # noqa: E402

from spotify_heart.dj import PLAYING, WAIT_HR_LOCK, DjStatus  # noqa: E402
from spotify_heart.hr_smoother import HrEstimate  # noqa: E402
from spotify_heart.selector import Pick  # noqa: E402
from spotify_heart.window import Window  # noqa: E402

LOCKED = HrEstimate(True, 96, 28, 2, 0.4)


def status(**kw) -> DjStatus:
    base = dict(
        state=PLAYING,
        hr=LOCKED,
        hr_bpm=96,
        sensor_ok=True,
        now_playing="Midnight Runner - The Long Way Home",
        now_playing_id="demo1",
        now_title="Midnight Runner",
        now_artist="The Long Way Home",
        now_progress_ms=138_000,
        now_duration_ms=214_000,
        queue_lead_ms=15_000,
        next_pick=Pick("spotify:track:demo", "demo", "Faster Than Falling", "Neon Tides", 192, 2.0),
        next_queued=False,
        tempo_mode="double",
    )
    base.update(kw)
    return DjStatus(**base)


SHOTS = {
    "window": status(),
    "window-waiting": status(
        state=WAIT_HR_LOCK,
        hr=HrEstimate(False, 0, 3, 1, 2.0),
        hr_bpm=0,
        now_playing="",
        now_playing_id=None,
        now_title="",
        now_artist="",
        now_duration_ms=0,
        next_pick=None,
        tempo_mode="auto",
        message="Open Spotify on a device and press play once",
    ),
    "window-queued": status(next_queued=True, tempo_mode="auto", now_progress_ms=202_000),
}


def shoot(name: str, st: DjStatus, outdir: Path) -> Path:
    win = Window(
        lambda: st, threading.Event(), on_skip=lambda: None, on_pause=lambda p: None,
        on_login=lambda: None, on_quit=lambda: None, show_request=threading.Event(),
    )
    win.root.attributes("-topmost", True)
    win.root.geometry("+80+80")
    win.root.update()                    # lay out, so the progress bar knows its width
    win.refresh()
    win.root.update()
    time.sleep(0.6)                      # let the compositor catch up
    win.root.update()
    x, y = win.root.winfo_rootx(), win.root.winfo_rooty()
    w, h = win.root.winfo_width(), win.root.winfo_height()
    title_h = y - win.root.winfo_y()     # height of the window frame's title bar
    img = ImageGrab.grab(bbox=(x, y - title_h, x + w, y + h))
    outdir.mkdir(parents=True, exist_ok=True)
    path = outdir / f"{name}.png"
    img.save(path)
    win.root.destroy()
    return path


def main() -> int:
    outdir = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs"
    for name, st in SHOTS.items():
        print("saved", shoot(name, st, outdir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
