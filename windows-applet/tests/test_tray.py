"""Tray tests. pystray is imported but never run: no icon appears."""

import threading

import pytest

from spotify_heart.tray import COLOURS, Status, Tray, TrayActions, heart_image


class FakeIcon:
    """Stands in for pystray.Icon, which needs a real desktop."""

    def __init__(self, *args, **kwargs):
        self.name, self.icon, self.title = args[0], args[1], args[2]
        self.menu = kwargs.get("menu")
        self.visible = False
        self.notifications = []
        self.menu_updates = 0
        self.stopped = False

    def update_menu(self):
        self.menu_updates += 1

    def notify(self, msg, title=None):
        self.notifications.append(msg)

    def stop(self):
        self.stopped = True


@pytest.fixture
def tray(monkeypatch):
    monkeypatch.setattr("spotify_heart.tray.pystray.Icon", FakeIcon)
    state = {"status": Status("grey", "starting", False), "logins": 0, "skips": 0, "folders": 0}

    def login():
        state["logins"] += 1

    actions = TrayActions(
        login=login,
        skip=lambda: state.__setitem__("skips", state["skips"] + 1),
        open_folder=lambda: state.__setitem__("folders", state["folders"] + 1),
    )
    t = Tray(lambda: state["status"], actions, threading.Event())
    t.state = state
    return t


def test_heart_image_per_colour():
    for name in COLOURS:
        img = heart_image(name, 64)
        assert img.size == (64, 64)
        assert img.mode == "RGBA"
        # The heart covers the middle and leaves the corners transparent.
        assert img.getpixel((32, 40))[3] == 255
        assert img.getpixel((1, 62))[3] == 0
    assert heart_image("green").getpixel((32, 40))[:3] == COLOURS["green"]
    assert heart_image("no such colour").getpixel((32, 40))[:3] == COLOURS["grey"]


def test_refresh_follows_status(tray):
    tray.refresh()
    assert tray.icon.title == "starting"
    assert tray.icon.icon is tray._images["grey"]

    tray.state["status"] = Status("green", "HR 72 | queued: Song - Artist", True)
    tray.refresh()
    assert tray.icon.icon is tray._images["green"]
    assert tray.icon.title == "HR 72 | queued: Song - Artist"


def test_tooltip_is_trimmed_to_the_windows_limit(tray):
    tray.state["status"] = Status("green", "x" * 300, True)
    tray.refresh()
    assert len(tray.icon.title) == 127


def test_icon_image_only_changes_with_the_colour(tray):
    tray.refresh()
    first = tray.icon.icon
    tray.state["status"] = Status("grey", "different text", False)
    tray.refresh()
    assert tray.icon.icon is first


def test_pause_toggles_the_event(tray):
    assert not tray.actions.paused.is_set()
    tray._toggle_pause()
    assert tray.actions.paused.is_set()
    tray._toggle_pause()
    assert not tray.actions.paused.is_set()


def test_login_runs_off_the_tray_thread(tray):
    tray.login()
    for t in threading.enumerate():
        if t.name == "login":
            t.join(timeout=5)
    assert tray.state["logins"] == 1


def test_menu_actions(tray):
    tray.actions.skip()
    tray.actions.open_folder()
    assert tray.state["skips"] == 1 and tray.state["folders"] == 1


def test_about_mentions_getsongbpm(tray, monkeypatch):
    """The free API key requires visible attribution."""
    opened = []
    monkeypatch.setattr("spotify_heart.tray.webbrowser.open", opened.append)
    tray._about()
    assert "getsongbpm.com" in tray.icon.notifications[0].lower()
    assert opened == ["https://getsongbpm.com"]


def test_quit_stops_everything(tray):
    tray.quit()
    assert tray.stop.is_set() and tray.icon.stopped


def test_notify_survives_a_backend_without_notifications(tray):
    def boom(*a, **kw):
        raise NotImplementedError("no notification support")

    tray.icon.notify = boom
    tray.notify("hello")  # must not raise


def test_show_window_entry_appears_only_with_a_window(monkeypatch):
    monkeypatch.setattr("spotify_heart.tray.pystray.Icon", FakeIcon)
    status = lambda: Status("grey", "x", False)
    stop = threading.Event()
    shown = []

    with_window = Tray(status, TrayActions(login=lambda: None, show_window=lambda: shown.append(1)), stop)
    labels = [getattr(i, "text", None) for i in with_window.icon.menu]
    assert "Show window" in [str(t) for t in labels]

    without = Tray(status, TrayActions(login=lambda: None), stop)
    assert "Show window" not in [str(getattr(i, "text", "")) for i in without.icon.menu]
