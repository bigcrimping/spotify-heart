"""System tray icon: coloured heart, status tooltip and menu."""

from __future__ import annotations

import logging
import os
import threading
import webbrowser
from dataclasses import dataclass, field
from typing import Callable

import pystray
from PIL import Image, ImageDraw

from . import __version__

log = logging.getLogger(__name__)

COLOURS = {
    "grey": (140, 140, 140),
    "amber": (240, 170, 30),
    "green": (40, 190, 80),
    "red": (220, 50, 50),
}
REFRESH_S = 2.0

TEMPO_LABELS = [("auto", "Auto"), ("half", "Half"), ("same", "Heartbeat"), ("double", "Double")]


@dataclass
class Status:
    colour: str = "grey"
    tooltip: str = "Spotify Heart"
    logged_in: bool = False


@dataclass
class TrayActions:
    login: Callable[[], None]
    skip: Callable[[], None] | None = None
    open_folder: Callable[[], None] = lambda: None
    show_window: Callable[[], None] | None = None
    tempo_mode: Callable[[], str] | None = None       # reads the current mode
    set_tempo_mode: Callable[[str], None] | None = None
    paused: threading.Event = field(default_factory=threading.Event)


def heart_image(colour: str, size: int = 64) -> Image.Image:
    rgb = COLOURS.get(colour, COLOURS["grey"])
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size
    r = s * 0.26
    # Two lobes and a point.
    d.ellipse((s * 0.04, s * 0.10, s * 0.04 + 2 * r, s * 0.10 + 2 * r), fill=rgb)
    d.ellipse((s * 0.96 - 2 * r, s * 0.10, s * 0.96, s * 0.10 + 2 * r), fill=rgb)
    d.polygon([(s * 0.06, s * 0.47), (s * 0.94, s * 0.47), (s * 0.50, s * 0.94)], fill=rgb)
    return img


class Tray:
    def __init__(self, get_status: Callable[[], Status], actions: TrayActions, stop: threading.Event):
        self.get_status = get_status
        self.actions = actions
        self.stop = stop
        self._colour = ""
        self._images = {c: heart_image(c) for c in COLOURS}
        self.icon = pystray.Icon(
            "spotify-heart", self._images["grey"], "Spotify Heart", menu=self._menu()
        )

    def _menu(self) -> pystray.Menu:
        a = self.actions
        Item = pystray.MenuItem
        items = [
            Item(lambda _: self.get_status().tooltip, None, enabled=False),
            pystray.Menu.SEPARATOR,
        ]
        if a.tempo_mode is not None and a.set_tempo_mode is not None:
            items.append(Item("Tempo", pystray.Menu(*[
                Item(
                    label,
                    (lambda m: lambda: a.set_tempo_mode(m))(mode),
                    checked=(lambda m: lambda _: a.tempo_mode() == m)(mode),
                    radio=True,
                )
                for mode, label in TEMPO_LABELS
            ])))
        if a.show_window is not None:
            # default=True: a left-click on the icon opens the window.
            items.append(Item("Show window", lambda: a.show_window(), default=True))
        return pystray.Menu(
            *items,
            Item("Pause", self._toggle_pause, checked=lambda _: a.paused.is_set()),
            Item("Skip", lambda: a.skip and a.skip(), enabled=lambda _: a.skip is not None),
            Item(
                lambda _: "Log in to Spotify again" if self.get_status().logged_in else "Log in to Spotify",
                self.login,
            ),
            Item("Open app folder", lambda: a.open_folder()),
            Item("About", self._about),
            pystray.Menu.SEPARATOR,
            Item("Quit", self.quit),
        )

    def _toggle_pause(self):
        if self.actions.paused.is_set():
            self.actions.paused.clear()
            log.info("[tray] resumed")
        else:
            self.actions.paused.set()
            log.info("[tray] paused")

    def login(self):
        """Run the login on its own thread so the tray stays responsive."""
        threading.Thread(target=self.actions.login, name="login", daemon=True).start()

    def _about(self):
        self.notify(
            f"Spotify Heart {__version__}\nTempo data provided by GetSongBPM (getsongbpm.com)"
        )
        webbrowser.open("https://getsongbpm.com")

    def quit(self):
        """Stop the whole app: the DJ thread, the window and the icon."""
        log.info("[tray] quit")
        self.stop.set()
        self.icon.stop()

    def notify(self, msg: str) -> None:
        try:
            self.icon.notify(msg, "Spotify Heart")
        except Exception as e:  # notifications are best-effort
            log.debug("[tray] notify failed: %s", e)

    def _refresh_loop(self):
        while not self.stop.wait(REFRESH_S):
            self.refresh()

    def refresh(self):
        st = self.get_status()
        if st.colour != self._colour:
            self.icon.icon = self._images.get(st.colour, self._images["grey"])
            self._colour = st.colour
        self.icon.title = st.tooltip[:127]  # Windows tooltip limit
        self.icon.update_menu()

    def run(self):
        """Blocks on the main thread until Quit."""

        def setup(icon):
            icon.visible = True
            self.refresh()
            threading.Thread(target=self._refresh_loop, name="tray_refresh", daemon=True).start()

        self.icon.run(setup=setup)


def open_folder(path) -> None:
    os.startfile(str(path))  # noqa: S606 - Windows only
