"""The window's text, tested without Tk (view() is pure)."""

from spotify_heart.dj import LOGIN, PLAYING, WAIT_HR_LOCK, DjStatus
from spotify_heart.hr_smoother import HrEstimate
from spotify_heart.selector import Pick
from spotify_heart.window import view

LOCKED = HrEstimate(True, 78, 20, 2, 0.5)
UNLOCKED_FRESH = HrEstimate(False, 0, 2, 1, 5.0)
UNLOCKED_STALE = HrEstimate(False, 0, 0, 0, 200.0)

PICK = Pick("spotify:track:x", "x", "Losin' It", "Bury Your Dead", 45, 0.5)


def playing_status(**kw):
    base = dict(
        state=PLAYING, hr=LOCKED, hr_bpm=78, sensor_ok=True,
        now_playing="Go 2 Sleep - Ludacris", now_playing_id="t1",
        now_title="Go 2 Sleep", now_artist="Ludacris",
        now_progress_ms=60_000, now_duration_ms=240_000, queue_lead_ms=15_000,
    )
    base.update(kw)
    return DjStatus(**base)


def test_heart_rate_when_locked():
    v = view(playing_status())
    assert v.hr == "78"
    assert "20 readings" in v.hr_note
    assert v.colour == "green"


def test_heart_rate_when_stale():
    v = view(playing_status(hr=UNLOCKED_STALE))
    assert v.hr == "78"                      # the last steady reading
    assert "stale" in v.hr_note
    assert v.colour == "amber"


def test_heart_rate_with_no_readings_yet():
    v = view(playing_status(hr=UNLOCKED_FRESH, hr_bpm=0))
    assert v.hr == "--"
    assert "sit still" in v.hr_note


def test_sensor_unplugged():
    v = view(playing_status(sensor_ok=False, sensor_text="board not found"))
    assert v.hr == "--"
    assert v.hr_note == "sensor: board not found"
    assert v.colour == "grey"


def test_next_up_before_it_is_queued():
    v = view(playing_status(next_pick=PICK, next_queued=False))
    assert (v.next_title, v.next_artist) == ("Losin' It", "Bury Your Dead")
    assert v.tempo_chip == "45 BPM · half"
    assert v.next_note == "will queue shortly"


def test_next_up_once_queued():
    v = view(playing_status(next_pick=PICK, next_queued=True))
    assert v.tempo_chip == "45 BPM · half"
    assert v.next_note == "queued, plays next"


def test_double_tempo_is_labelled():
    v = view(playing_status(next_pick=Pick("u", "i", "T", "A", 150, 2.0)))
    assert v.tempo_chip == "150 BPM · double"


def test_same_tempo_has_no_label():
    v = view(playing_status(next_pick=Pick("u", "i", "T", "A", 75, 1.0)))
    assert v.tempo_chip == "75 BPM"


def test_still_choosing():
    v = view(playing_status(next_pick=None))
    assert v.next_up == ""
    assert v.next_note == "choosing a match..."


def test_nothing_playing():
    v = view(playing_status(now_playing="", now_playing_id=None, next_pick=None))
    assert v.now == "Nothing playing"
    assert v.next_note == "press play in Spotify"


def test_waiting_for_a_lock():
    v = view(playing_status(state=WAIT_HR_LOCK, next_pick=None))
    assert v.next_note == "waiting for a steady heart rate"


def test_logged_out():
    v = view(DjStatus(state=LOGIN, sensor_ok=True, hr=LOCKED, hr_bpm=78))
    assert v.next_note == "log in to Spotify first"
    assert v.colour == "red"


def test_message_and_error_are_surfaced():
    assert view(playing_status(message="Open Spotify...")).message == "Open Spotify..."
    # An error wins over a message.
    v = view(playing_status(message="Spotify idle", error="Spotify Premium required"))
    assert v.message == "Spotify Premium required"


def test_skip_is_only_offered_when_it_would_work():
    assert view(playing_status()).can_skip
    assert not view(playing_status(paused=True)).can_skip
    assert not view(playing_status(state=WAIT_HR_LOCK)).can_skip
    assert view(playing_status(paused=True)).paused


# ------------------------------------------------------ progress and beating


def test_progress_and_remaining():
    v = view(playing_status())                    # 60 s into a 4 min track
    assert v.progress == 0.25
    assert v.remaining == "3:00 left"
    assert round(v.queue_mark, 4) == round(225 / 240, 4)   # 15 s before the end


def test_progress_keeps_moving_between_polls():
    v = view(playing_status(), elapsed_s=30)
    assert v.progress == 0.375
    assert v.remaining == "2:30 left"


def test_progress_does_not_advance_while_paused():
    v = view(playing_status(paused=True), elapsed_s=30)
    assert v.progress == 0.25


def test_progress_never_runs_past_the_end():
    v = view(playing_status(), elapsed_s=9999)
    assert v.progress == 1.0
    assert v.remaining == "0:00 left"


def test_no_progress_when_nothing_plays():
    v = view(playing_status(now_playing_id=None, now_duration_ms=0))
    assert v.progress == -1 and v.remaining == ""


def test_beat_follows_the_heart_rate():
    assert view(playing_status()).beat_ms == 769          # 60000 / 78
    assert view(playing_status(hr=UNLOCKED_STALE)).beat_ms == 0


def test_mmss():
    from spotify_heart.window import mmss
    assert (mmss(0), mmss(5_000), mmss(65_000), mmss(600_000)) == ("0:00", "0:05", "1:05", "10:00")


# ------------------------------------------------------------- smoke tests
# These build the real Tk window. They caught a duplicate-keyword bug that the
# pure view() tests could not see, because the window falls back to tray-only
# when it fails to build.

import threading

import pytest


@pytest.fixture
def real_window():
    tk = pytest.importorskip("tkinter")
    try:
        w = _make_window()
    except tk.TclError as e:            # no display (CI, headless)
        pytest.skip(f"no display: {e}")
    yield w
    w.root.destroy()


def _make_window(status=None, **kw):
    from spotify_heart.window import Window

    st = status or (lambda: playing_status(next_pick=PICK, next_queued=True))
    return Window(
        st, threading.Event(), on_skip=lambda: None, on_pause=lambda p: None,
        on_login=lambda: None, on_quit=lambda: None,
        show_request=threading.Event(), start_hidden=True, **kw
    )


def test_window_builds_and_refreshes(real_window):
    real_window.refresh()
    assert real_window.hr_var.get() == "78"
    assert real_window.now_title_var.get() == "Go 2 Sleep"
    assert real_window.next_title_var.get() == "Losin' It"
    assert real_window.chip_var.get() == "45 BPM · half"
    assert "Spotify Heart" in real_window.root.title()


def test_window_is_tall_enough_for_its_buttons(real_window):
    """The first version clipped the buttons: the height was hard-coded."""
    root = real_window.root
    root.update_idletasks()
    wanted = root.winfo_reqheight()
    height = int(root.geometry().split("+")[0].split("x")[1])
    assert height >= wanted
    buttons_bottom = real_window.skip_btn.winfo_y() + real_window.skip_btn.winfo_reqheight()
    assert buttons_bottom <= height


def test_window_survives_every_state(real_window):
    from spotify_heart.dj import DjStatus

    for st in [
        DjStatus(),                                        # logged out
        playing_status(sensor_ok=False),
        playing_status(next_pick=None),
        playing_status(message="Open Spotify on a device and press play once"),
        playing_status(paused=True),
        playing_status(next_pick=PICK, next_queued=False),
    ]:
        real_window.get_status = lambda s=st: s
        real_window.refresh()                              # must not raise
    assert real_window.skip_btn["state"] in ("normal", "disabled")


def test_pause_button_reports_the_new_state():
    asked = []
    w = _make_window()
    try:
        w.on_pause = asked.append
        w.get_status = lambda: playing_status()
        w.refresh()
        w._pause()
        assert asked == [True]                             # not paused -> pause
        w.get_status = lambda: playing_status(paused=True)
        w.refresh()
        w._pause()
        assert asked == [True, False]                      # paused -> resume
    finally:
        w.root.destroy()


# ---------------------------------------------------------- tempo control


def test_tempo_note_explains_the_choice():
    from spotify_heart.window import tempo_note

    st = playing_status()                       # locked at 78
    assert tempo_note(st) == "around 39, 78 or 156 BPM"
    assert tempo_note(playing_status(tempo_mode="double")) == "around 156 BPM"
    assert tempo_note(playing_status(tempo_mode="same")) == "around 78 BPM"
    # Half of 78 is 39, under the 40 BPM floor, so it says so.
    assert "out of range" in tempo_note(playing_status(tempo_mode="half"))

    higher = playing_status(hr=HrEstimate(True, 96, 20, 2, 0.5), hr_bpm=96, tempo_mode="half")
    assert tempo_note(higher) == "around 48 BPM"


def test_tempo_note_warns_when_out_of_range():
    st = playing_status(hr=HrEstimate(True, 115, 20, 2, 0.5), hr_bpm=115, tempo_mode="double")
    assert "out of range" in tempo_note_of(st)


def tempo_note_of(st):
    from spotify_heart.window import tempo_note

    return tempo_note(st)


def test_tempo_note_without_a_reading():
    st = DjStatus(state=PLAYING, sensor_ok=True, tempo_mode="double")
    assert tempo_note_of(st) == "double your heart rate"


def test_view_carries_the_mode():
    assert view(playing_status(tempo_mode="half")).tempo_mode == "half"


def test_tempo_buttons_show_the_active_choice(real_window):
    from spotify_heart.window import BTN, ON

    real_window.get_status = lambda: playing_status(tempo_mode="double")
    real_window.refresh()
    assert real_window.mode_btns["double"]["bg"] == ON
    assert real_window.mode_btns["auto"]["bg"] == BTN
    assert "156 BPM" in real_window.tempo_note_var.get()


def test_tempo_buttons_report_the_choice():
    asked = []
    w = _make_window()
    try:
        w.on_tempo_mode = asked.append
        w.mode_btns["half"].invoke()
        w.mode_btns["auto"].invoke()
        assert asked == ["half", "auto"]
    finally:
        w.root.destroy()


def test_refresh_button_asks_for_another_pick():
    asked = []
    w = _make_window()
    try:
        w.on_refresh = lambda: asked.append(1)
        w.get_status = lambda: playing_status(next_pick=PICK, next_queued=False)
        w.refresh()
        assert str(w.refresh_btn["state"]) == "normal"
        w.refresh_btn.invoke()
        assert asked == [1]
        assert str(w.refresh_btn["state"]) == "disabled"   # briefly, against double-clicks
    finally:
        w.root.destroy()


def test_refresh_is_offered_only_when_there_is_something_to_replace():
    assert view(playing_status(next_pick=PICK, next_queued=False)).can_refresh
    # Already queued at Spotify: too late to swap it here.
    assert not view(playing_status(next_pick=PICK, next_queued=True)).can_refresh
    assert not view(playing_status(next_pick=None)).can_refresh
    assert not view(playing_status(state=WAIT_HR_LOCK, next_pick=PICK)).can_refresh


def test_tempo_and_heart_rate_share_the_header(real_window):
    """The tempo control sits beside the heart rate, not below the cards."""
    real_window.root.deiconify()          # positions are only real once mapped
    real_window.root.update()
    heart_y = real_window.heart.winfo_rooty()
    auto_y = real_window.mode_btns["auto"].winfo_rooty()
    assert abs(auto_y - heart_y) < 60          # same band of the window
    assert real_window.mode_btns["auto"].winfo_rootx() > real_window.heart.winfo_rootx()
    # and above the Now playing card
    assert auto_y < real_window.skip_btn.winfo_rooty()


def test_buttons_still_fit_once_a_message_appears(real_window):
    """A message used to appear below the buttons and push them off screen."""
    real_window.root.deiconify()
    real_window.get_status = lambda: playing_status(
        message="Open Spotify on a device and press play once"
    )
    real_window.refresh()
    real_window.root.update()
    height = real_window.root.winfo_height()
    for widget in (real_window.skip_btn, real_window.msg):
        bottom = widget.winfo_rooty() - real_window.root.winfo_rooty() + widget.winfo_height()
        assert bottom <= height, f"{widget} runs past the bottom"
