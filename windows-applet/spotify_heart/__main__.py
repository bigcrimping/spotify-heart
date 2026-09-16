"""CLI entry point: `python -m spotify_heart` / `spotify-heart.exe`."""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time

from . import __version__, config, log as logsetup
from .config import local_dir
from .hr_smoother import HrSmoother
from .serial_reader import LineSourceReader, SerialReader

log = logging.getLogger("spotify_heart")

MUTEX_NAME = "Global\\SpotifyHeart"
ERROR_ALREADY_EXISTS = 183


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="spotify-heart", description=__doc__)
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("--config", help="path to the config file (default config.local.json beside the app)")
    ap.add_argument("--login", action="store_true", help="run the Spotify login and exit")
    ap.add_argument("--probe", action="store_true", help="exercise every external API and exit")
    ap.add_argument("--pick", type=int, metavar="N", help="pick one track for heart rate N and exit")
    ap.add_argument("--dry-run", action="store_true", help="never call queue or play")
    ap.add_argument("--port", metavar="COMn", help="serial port (overrides autodetect)")
    ap.add_argument("--console", action="store_true", help="no tray; log to stdout")
    ap.add_argument("--hidden", action="store_true",
                    help="start with the window hidden in the tray")
    ap.add_argument("--no-window", action="store_true", help="tray icon only, no window")
    ap.add_argument("--fake-sensor", metavar="FILE", help="read heart_rate lines from FILE or - for stdin")
    return ap.parse_args(argv)


def acquire_single_instance():
    """Hold a named mutex for the life of the process. None if another copy runs."""
    if sys.platform != "win32":
        return object()
    import ctypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        return object()  # can't create it; don't block startup
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return None
    return handle


def make_reader(args, cfg, smoother: HrSmoother, stop: threading.Event):
    if args.fake_sensor:
        return LineSourceReader(smoother.push, args.fake_sensor, stop=stop)
    return SerialReader(smoother.push, port=args.port or cfg.serial_port, stop=stop)


def run_hr_monitor(reader, smoother: HrSmoother, stop: threading.Event, every_s: float = 1) -> None:
    """Log the heart-rate estimate on lock changes and every `every_s` seconds."""
    if not reader.is_alive():
        reader.start()
    was_locked = None
    last = 0.0
    try:
        while not stop.is_set():
            now = time.monotonic()
            est = smoother.estimate(now)
            if est.locked != was_locked or now - last >= every_s:
                last = now
                if est.locked:
                    log.info("[hr] locked bpm=%d n=%d spread=%d", est.bpm, est.n, est.spread)
                elif was_locked:
                    log.info("[hr] unlocked n=%d spread=%d", est.n, est.spread)
                else:
                    log.info("[hr] waiting n=%d bpm=%d spread=%d", est.n, est.bpm, est.spread)
            was_locked = est.locked
            if isinstance(reader, LineSourceReader) and not reader.is_alive():
                break
            stop.wait(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    cfg = config.load(args.config)
    if args.dry_run:
        cfg.dry_run = True
    oneshot = args.login or args.probe or args.pick is not None
    logsetup.setup(cfg.log_dir, console=args.console or oneshot)
    log.info("spotify-heart %s, config %s", __version__, cfg.path)

    if args.login:
        from .spotify_client import SpotifyClient, SpotifyError, TokenStore

        client = SpotifyClient(cfg.spotify_client_id, TokenStore(cfg.tokens_path))
        try:
            client.login()
            np = client.now_playing()
        except SpotifyError as e:
            log.error("[login] %s", e)
            return 1
        if np and np.track_id:
            log.info("[login] check OK, now playing: %s - %s", np.name, np.artist)
        else:
            log.info("[login] check OK, nothing playing right now")
        return 0

    if args.pick is not None:
        return run_pick(cfg, args.pick)

    if args.probe:
        return run_probe(cfg)

    mutex = acquire_single_instance()
    if mutex is None:
        log.error("another copy of spotify-heart is already running")
        if not args.console and sys.platform == "win32":
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                None, "Spotify Heart is already running (look for the heart in the tray).",
                "Spotify Heart", 0x40,
            )
        return 1

    smoother = HrSmoother(
        cfg.hr_window_s, cfg.hr_min_readings, cfg.hr_max_spread, cfg.hr_min, cfg.hr_max
    )
    stop = threading.Event()
    reader = make_reader(args, cfg, smoother, stop)
    if args.console:
        run_console(cfg, reader, smoother, stop)
    else:
        run_tray(cfg, reader, smoother, stop, args)
    return 0


def run_probe(cfg) -> int:
    """Exercise every external API and save fixtures (tools/probe_apis.py)."""
    tools = local_dir() / "tools"
    if not (tools / "probe_apis.py").exists():
        log.error("[probe] tools/probe_apis.py is not next to the app (%s)", tools)
        return 2
    sys.path.insert(0, str(tools))
    import probe_apis

    return probe_apis.run(cfg, do_queue=False)


def run_pick(cfg, hr: int) -> int:
    """Select one track for heart rate hr and print it. Never queues."""
    import random

    from .bpm_source import BpmError, BpmSource
    from .selector import select
    from .spotify_client import SpotifyClient, SpotifyError, TokenStore
    from .state import load_recent

    client = SpotifyClient(cfg.spotify_client_id, TokenStore(cfg.tokens_path))
    bpm = BpmSource(cfg.getsongbpm_key, cfg.cache_dir)
    t0 = time.monotonic()
    try:
        pick = select(
            hr,
            bpm.songs_at,
            client.search_track,
            load_recent(cfg.state_path, cfg.no_repeat_n),
            random.Random(),
            tol=cfg.bpm_tol,
            allow_half_double=cfg.allow_half_double,
            is_cached=bpm.is_cached,
            genres=cfg.genres,
            mode=cfg.tempo_mode,
        )
    except (BpmError, SpotifyError) as e:
        log.error("[pick] %s", e)
        return 1
    dt = time.monotonic() - t0
    if pick is None:
        log.error("[pick] nothing found for hr=%d (%.1f s)", hr, dt)
        return 1
    print(pick.describe(hr))
    log.info("[pick] %.1f s, %d GetSongBPM request(s)", dt, bpm.requests_made)
    return 0


def build_dj(cfg, reader, smoother: HrSmoother, stop: threading.Event, request_login):
    import random

    from .bpm_source import BpmSource
    from .dj import Dj
    from .spotify_client import SpotifyClient, TokenStore
    from .state import load_recent, save_recent

    client = SpotifyClient(cfg.spotify_client_id, TokenStore(cfg.tokens_path))
    return Dj(
        cfg,
        smoother,
        client,
        BpmSource(cfg.getsongbpm_key, cfg.cache_dir),
        load_recent(cfg.state_path, cfg.no_repeat_n),
        save_recent=lambda ring: save_recent(cfg.state_path, ring),
        reader=reader,
        stop=stop,
        rng=random.Random(),
        request_login=request_login,
    )


def run_login(dj, notify=lambda msg: None) -> None:
    from .spotify_client import SpotifyError

    try:
        dj.spotify.login()
        notify("Logged in to Spotify")
    except SpotifyError as e:
        log.error("[login] %s", e)
        notify(f"Spotify login failed: {e}")
    dj.wake.set()


def run_console(cfg, reader, smoother: HrSmoother, stop: threading.Event) -> None:
    dj = build_dj(cfg, reader, smoother, stop, request_login=None)
    dj.request_login = lambda: threading.Thread(target=run_login, args=(dj,), daemon=True).start()
    reader.start()
    dj.start()
    try:
        run_hr_monitor(reader, smoother, stop, every_s=30)
    finally:
        stop.set()


def run_tray(cfg, reader, smoother: HrSmoother, stop: threading.Event, args) -> None:
    from .tray import Status, Tray, TrayActions, open_folder

    tray: Tray
    dj = build_dj(cfg, reader, smoother, stop, request_login=None)
    show_request = threading.Event()

    def login():
        run_login(dj, tray.notify)

    def skip():
        dj.skip.set()
        dj.wake.set()          # act on it now rather than at the next tick

    def set_paused(paused: bool):
        dj.paused.set() if paused else dj.paused.clear()
        log.info("[ui] %s", "paused" if paused else "resumed")

    def status() -> Status:
        st = dj.status
        return Status(st.colour, st.tooltip, dj.spotify.has_login)

    actions = TrayActions(
        login=login,
        skip=skip,
        open_folder=lambda: open_folder(cfg.dir),
        show_window=None if args.no_window else show_request.set,
        tempo_mode=lambda: dj.tempo_mode,
        set_tempo_mode=dj.set_tempo_mode,
        paused=dj.paused,
    )
    tray = Tray(status, actions, stop)
    dj.request_login = tray.login  # first run with no tokens: open the browser once
    reader.start()
    dj.start()

    window = None
    if not args.no_window:
        from .window import Window

        try:
            window = Window(
                lambda: dj.status, stop, on_skip=skip, on_pause=set_paused, on_login=tray.login,
                on_quit=tray.quit, show_request=show_request, start_hidden=args.hidden,
                on_tempo_mode=dj.set_tempo_mode, on_refresh=dj.repick,
            )
        except Exception as e:      # no Tk, no display: the tray still works
            log.warning("[window] not available (%s); tray only", e)

    if window is None:
        log.info("[tray] running")
        tray.run()                  # blocks on the main thread
    else:
        # pystray's Windows backend runs its own message loop, so it is happy
        # on a second thread; Tk is not, and keeps the main one.
        threading.Thread(target=tray.run, name="tray", daemon=True).start()
        log.info("[tray] running with window")
        window.run()
    stop.set()


if __name__ == "__main__":
    sys.exit(main())
