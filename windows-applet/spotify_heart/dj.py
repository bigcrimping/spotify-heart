"""The DJ thread: waits for a heart-rate lock, then queues a tempo-matched
track shortly before each song ends.

All decisions happen in `step(now)`, which does one tick and returns how long
to wait before the next. Tests drive it with a fake clock and fake clients.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass, replace
from typing import Callable

from .bpm_source import BpmAuthError, BpmError, BpmTransientError
from .hr_smoother import HrEstimate, HrSmoother
from .selector import MODES, Pick, RecentRing, select
from .spotify_client import (
    AuthError,
    NoActiveDevice,
    NowPlaying,
    SpotifyError,
    TransientError,
)

log = logging.getLogger(__name__)

LOGIN = "LOGIN"
WAIT_HR_LOCK = "WAIT_HR_LOCK"
PLAYING = "PLAYING"

BACKOFF_START_S = 5
BACKOFF_MAX_S = 300
NO_DEVICE_RETRY_S = 30
NO_PICK_BACKOFF_S = 60
START_IDLE_S = 30  # paused this long at first lock -> start playback
HR_STALE_S = 300  # a lock older than this is too old to pick with (cfg.hr_stale_s)
HR_STALE_WARN_S = 60  # tooltip says "stale" once the lock is older than this
API_RESYNC_S = 30  # longest gap between now-playing calls while a track runs
REPICK_MIN_GAP_S = 30  # least time between re-picks when the heart rate drifts
REJECT_N = 10  # picks turned down with "pick another", not offered again
WAIT_TICK_S = 1
LOGIN_TICK_S = 2
NO_DEVICE_MSG = "Open Spotify on a device and press play once"


@dataclass(frozen=True)
class DjStatus:
    state: str = LOGIN
    hr: HrEstimate | None = None
    hr_bpm: int = 0  # last locked bpm, 0 if none
    sensor_ok: bool = False
    sensor_text: str = ""
    now_playing: str = ""
    now_playing_id: str | None = None
    now_title: str = ""
    now_artist: str = ""
    now_progress_ms: int = 0
    now_duration_ms: int = 0
    now_sampled_at: float = 0.0   # clock reading when progress was measured
    queue_lead_ms: int = 15000
    last_pick: Pick | None = None
    last_pick_hr: int = 0
    next_pick: Pick | None = None  # what plays after the current track
    next_queued: bool = False      # True once Spotify has accepted it
    tempo_mode: str = "auto"       # auto, same, double or half
    message: str = ""
    error: str = ""
    backoff: bool = False
    paused: bool = False

    @property
    def hr_stale(self) -> bool:
        """True once the lock is old enough that the listener may have left."""
        if self.hr is None or self.hr.locked:
            return False
        return self.hr.age_s > HR_STALE_WARN_S

    @property
    def colour(self) -> str:
        if self.state == LOGIN:
            return "red"
        if not self.sensor_ok:
            return "grey"
        if self.backoff or self.error or self.state != PLAYING or self.hr_stale:
            return "amber"
        return "green"

    @property
    def tooltip(self) -> str:
        if not self.sensor_ok:
            hr = f"Sensor: {self.sensor_text or 'not connected'}"
        elif self.hr and self.hr.locked:
            hr = f"HR {self.hr.bpm}"
        elif self.hr_bpm:
            hr = f"HR ~{self.hr_bpm} ({'stale' if self.hr_stale else 'last lock'})"
        else:
            hr = "HR -- (sit still)"
        parts = [hr]
        if self.paused:
            parts.append("paused")
        if self.state == LOGIN:
            parts.append("log in to Spotify")
        elif self.message or self.error:
            parts.append(self.error or self.message)
        elif self.last_pick:
            p = self.last_pick
            parts.append(f"queued: {p.title} - {p.artist} ({p.tempo} BPM)")
        elif self.state == WAIT_HR_LOCK:
            parts.append("waiting for a steady heart rate")
        return " | ".join(parts)


class _Either:
    """Two rings read as one, for `select()`. Not a RecentRing: nothing is
    ever added through it."""

    def __init__(self, a: RecentRing, b: RecentRing):
        self.a, self.b = a, b

    def has_id(self, track_id: str) -> bool:
        return self.a.has_id(track_id) or self.b.has_id(track_id)

    def has_song(self, title: str, artist: str) -> bool:
        return self.a.has_song(title, artist) or self.b.has_song(title, artist)


class Dj(threading.Thread):
    def __init__(
        self,
        cfg,
        smoother: HrSmoother,
        spotify,
        bpm,
        recent: RecentRing,
        save_recent: Callable[[RecentRing], None] = lambda r: None,
        reader=None,
        stop: threading.Event | None = None,
        paused: threading.Event | None = None,
        skip: threading.Event | None = None,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
        request_login: Callable[[], None] | None = None,
    ):
        super().__init__(name="dj", daemon=True)
        self.cfg = cfg
        self.smoother = smoother
        self.spotify = spotify
        self.bpm = bpm
        self.recent = recent
        self.save_recent = save_recent
        self.reader = reader
        self.stop_event = stop or threading.Event()
        self.paused = paused or threading.Event()
        self.skip = skip or threading.Event()
        self.clock = clock
        self.rng = rng or random.Random()
        self.request_login = request_login
        self.wake = threading.Event()  # set to cut a wait short (skip, login)

        self.state = LOGIN
        self._status = DjStatus()
        self._login_requested = False
        self._backoff_s = 0.0
        self._last_lock_bpm = 0
        self._last_lock_at = -1e18
        self._queued_for: str | None = None
        self._last_track: str | None = None
        self._need_start = False
        self._idle_since: float | None = None
        self._idle_logged = False
        self._retry_at = 0.0  # no queue/play/select attempts before this
        self._message_at = -1e18
        self._next_pick: Pick | None = None  # chosen ahead of the queue window
        self._next_pick_hr = 0
        self._next_pick_at = -1e18
        self._catch_up = False  # queue the waiting pick at the next chance
        self._rejects = RecentRing(REJECT_N)  # turned down for this slot
        self.tempo_mode = getattr(cfg, "tempo_mode", "auto")
        self._status = replace(
            self._status, queue_lead_ms=int(cfg.queue_lead_ms), tempo_mode=self.tempo_mode
        )
        self._np: NowPlaying | None = None  # last answer from the API
        self._np_at = 0.0
        self._next_api_at = 0.0

    # ------------------------------------------------------------ status

    @property
    def status(self) -> DjStatus:
        return self._status

    def _set(self, **kw) -> None:
        self._status = replace(self._status, **kw)

    def _goto(self, new: str, reason: str) -> None:
        if new != self.state:
            log.info("[dj] %s -> %s (%s)", self.state, new, reason)
            self.state = new
        self._set(state=new)

    # ------------------------------------------------------------ thread

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                delay = self.step(self.clock())
            except Exception:  # never let the thread die
                log.exception("[dj] unexpected error")
                delay = self._backoff("unexpected error")
            self.wake.clear()
            self._wait(delay)

    def _wait(self, delay: float) -> None:
        end = self.clock() + delay
        while not self.stop_event.is_set() and not self.wake.is_set():
            left = end - self.clock()
            if left <= 0 or self.skip.is_set():
                return
            self.stop_event.wait(min(left, 0.5))

    # ------------------------------------------------------------ tick

    def step(self, now: float) -> float:
        est = self.smoother.estimate(now)
        if est.locked:
            self._last_lock_bpm, self._last_lock_at = est.bpm, now
        sensor_ok = self.reader.connected if self.reader is not None else True
        sensor_text = getattr(self.reader, "last_error", "") if self.reader is not None else ""
        self._set(
            hr=est,
            hr_bpm=self._last_lock_bpm,
            sensor_ok=sensor_ok,
            sensor_text=sensor_text,
            paused=self.paused.is_set(),
        )
        try:
            if self.state == LOGIN:
                return self._step_login()
            if self.state == WAIT_HR_LOCK:
                if est.locked:
                    self._need_start = True
                    self._idle_since = None
                    self._goto(PLAYING, f"hr locked at {est.bpm}")
                    return self._step_playing(now)
                return WAIT_TICK_S
            return self._step_playing(now)
        except AuthError as e:
            log.warning("[dj] auth: %s", e)
            self._set(error="")
            self._goto(LOGIN, "refresh token rejected")
            return LOGIN_TICK_S
        except (TransientError, BpmTransientError) as e:
            return self._backoff(str(e))
        except BpmAuthError as e:
            self._set(error="GetSongBPM key rejected")
            log.error("[dj] %s", e)
            return BACKOFF_MAX_S
        except (SpotifyError, BpmError) as e:
            status = getattr(e, "status", None)
            self._set(error="Spotify Premium required" if status == 403 else str(e)[:80])
            return self._backoff(str(e))

    def _backoff(self, reason: str) -> float:
        self._backoff_s = (
            BACKOFF_START_S if not self._backoff_s else min(self._backoff_s * 2, BACKOFF_MAX_S)
        )
        log.warning("[dj] %s; retrying in %d s", reason, self._backoff_s)
        self._set(backoff=True)
        return self._backoff_s

    def _ok(self) -> None:
        if self._backoff_s:
            log.info("[dj] recovered")
        self._backoff_s = 0
        self._set(backoff=False, error="")

    def _step_login(self) -> float:
        if not self.spotify.has_login:
            if self.request_login and not self._login_requested:
                self._login_requested = True
                self.request_login()
            return LOGIN_TICK_S
        self.spotify.refresh()
        self._ok()
        self._login_requested = False
        self._goto(WAIT_HR_LOCK, "logged in")
        return 0

    # ------------------------------------------------------------ playing

    def _hr_for_pick(self, now: float) -> int | None:
        if self._last_lock_bpm and now - self._last_lock_at <= self._hr_stale_s:
            return self._last_lock_bpm
        return None

    @property
    def _hr_stale_s(self) -> float:
        return float(getattr(self.cfg, "hr_stale_s", HR_STALE_S))

    def _now_playing(self, now: float) -> NowPlaying | None:
        """Spotify's view of the player, from the API only when it is due.

        Between calls the last answer is advanced by the elapsed time. A
        four-minute track then costs a handful of requests instead of ~48,
        which matters on a development-mode app with low rate limits.
        """
        if self._np is not None and now < self._next_api_at:
            elapsed_ms = int((now - self._np_at) * 1000)
            if not self._np.is_playing:
                return self._np
            if self._np.remaining_ms - elapsed_ms > 0:
                return replace(self._np, progress_ms=self._np.progress_ms + elapsed_ms)
        np = self.spotify.now_playing()
        self._np, self._np_at = np, now
        self._next_api_at = now + float(self.cfg.poll_s)
        return np

    def _schedule_next_api(self, now: float, np: NowPlaying) -> None:
        """Skip ahead to just before the queue window on a long track."""
        poll = float(self.cfg.poll_s)
        slack = (np.remaining_ms - self.cfg.queue_lead_ms) / 1000.0 - poll
        self._next_api_at = now + (min(max(slack, poll), API_RESYNC_S) if slack > poll else poll)

    def _step_playing(self, now: float) -> float:
        poll = float(self.cfg.poll_s)
        np: NowPlaying | None = self._now_playing(now)
        self._ok()
        paused = self.paused.is_set()
        playing = bool(np and np.track_id and np.is_playing)

        if np and np.track_id:
            self._set(
                now_playing=f"{np.name} - {np.artist}",
                now_playing_id=np.track_id,
                now_title=np.name,
                now_artist=np.artist,
                now_progress_ms=np.progress_ms,
                now_duration_ms=np.duration_ms,
                now_sampled_at=now,
            )
            if np.track_id != self._last_track:
                # A track can end sooner than Spotify's own progress says, for
                # instance with crossfade on, or when a phone reports a stale
                # position. The queue window is then stepped over entirely, so
                # queue the waiting pick now: it still plays next.
                if self._last_track is not None and self._next_pick is not None                         and self._queued_for != self._last_track:
                    log.info("[dj] track changed before the queue window; queueing now")
                    self._catch_up = True
                self._last_track = np.track_id
                self.recent.add(np.track_id, np.name, np.artist)
                self.save_recent(self.recent)
        else:
            self._set(
                now_playing="", now_playing_id=None, now_title="", now_artist="",
                now_progress_ms=0, now_duration_ms=0,
            )

        if self.skip.is_set():
            self.skip.clear()
            if not paused:
                return self._do_skip(now)

        if not playing:
            if self._idle_since is None:
                self._idle_since = now
            idle_for = now - self._idle_since
            if not self._idle_logged:
                log.info("[dj] idle (%s)", "nothing playing" if not (np and np.track_id) else "paused")
                self._idle_logged = True
            nothing = not (np and np.track_id)
            if self._need_start and not paused and (nothing or idle_for >= START_IDLE_S):
                return self._start_playback(now) or poll
            if not self._message_at_retry(now):
                self._set(message="Spotify idle")
            return poll

        # Music is playing.
        if self._idle_logged:
            log.info("[dj] playing again: %s - %s", np.name, np.artist)
        self._idle_since = None
        self._idle_logged = False
        self._need_start = False
        if self._status.message:
            self._set(message="")

        log.info(
            "[dj] poll: %s - %s, %d s left, hr %s",
            np.name, np.artist, np.remaining_ms // 1000,
            self._last_lock_bpm or "--",
        )
        if paused or now < self._retry_at:
            return poll

        self._publish_next(np)
        if np.remaining_ms > self.cfg.queue_lead_ms and not self._catch_up:
            # Plenty of time: choose the next track now, so the queue window
            # holds nothing but the queue call itself.
            self._ensure_next_pick(now)
            self._schedule_next_api(now, np)
            return poll

        if self._queued_for != np.track_id:
            if self._hr_for_pick(now) is None:
                # The lock expired while the pick sat waiting: the listener has
                # probably walked away, so drop it rather than queue a track
                # matched to a heart rate we no longer believe.
                self._next_pick = None
            pick = self._next_pick or self._pick(now)
            if pick is None:
                return poll
            try:
                self._queue(pick)
            except NoActiveDevice:
                return self._no_device(now)
            self._queued_for = np.track_id
            self._next_pick = None
        self._catch_up = False
        self._publish_next(np)
        return poll

    def repick(self) -> None:
        """Choose a different song for the next slot, on request. The one on
        offer is set aside so the replacement is not the same song again."""
        if self._next_pick is not None:
            p = self._next_pick
            log.info("[dj] pick another (not %s - %s)", p.title, p.artist)
            self._rejects.add(p.id, p.title, p.artist)
        self._next_pick = None
        self._retry_at = 0.0
        self._set(next_pick=None, next_queued=False)
        self.wake.set()

    def _picking_ring(self) -> RecentRing:
        """Recently played tracks plus anything turned down for this slot."""
        return _Either(self.recent, self._rejects)

    def set_tempo_mode(self, mode: str) -> None:
        """Match the tempo to the heart rate, its half, its double, or any of
        them ("auto"). Takes effect on the next pick, which is chosen now."""
        if mode not in MODES:
            log.warning("[dj] unknown tempo mode %r", mode)
            return
        if mode == self.tempo_mode:
            return
        log.info("[dj] tempo mode %s -> %s", self.tempo_mode, mode)
        self.tempo_mode = mode
        self._next_pick = None          # the waiting pick used the old mode
        self._retry_at = 0.0
        self._set(tempo_mode=mode, next_pick=None, next_queued=False)
        self.wake.set()

    def _publish_next(self, np: NowPlaying | None) -> None:
        """Say what is lined up: the pick already queued for this track, else
        the one waiting to be queued, else nothing yet."""
        queued = np is not None and self._queued_for == np.track_id
        if queued and self._next_pick is None:
            self._set(next_pick=self._status.last_pick, next_queued=True)
        else:
            self._set(next_pick=self._next_pick, next_queued=False)

    def _ensure_next_pick(self, now: float) -> None:
        """Keep a pick ready for the next queue window, matching the heart rate."""
        hr = self._hr_for_pick(now)
        if hr is None:
            return
        if self._next_pick is not None:
            drifted = abs(hr - self._next_pick_hr) > self.cfg.bpm_tol
            if not drifted or now - self._next_pick_at < REPICK_MIN_GAP_S:
                return
            log.info(
                "[dj] heart rate moved %d -> %d; choosing a new track",
                self._next_pick_hr, hr,
            )
        if now < self._retry_at:
            return
        pick = self._pick(now)
        if pick is not None:
            self._next_pick, self._next_pick_hr, self._next_pick_at = pick, hr, now

    def _message_at_retry(self, now: float) -> bool:
        return self._status.message == NO_DEVICE_MSG and now < self._retry_at

    def _pick(self, now: float) -> Pick | None:
        hr = self._hr_for_pick(now)
        if hr is None:
            log.info("[dj] no recent heart-rate lock; not picking")
            self._retry_at = now + self.cfg.poll_s
            return None
        pick = select(
            hr,
            self.bpm.songs_at,
            self.spotify.search_track,
            self._picking_ring(),
            self.rng,
            tol=self.cfg.bpm_tol,
            allow_half_double=self.cfg.allow_half_double,
            is_cached=getattr(self.bpm, "is_cached", lambda t: False),
            genres=self.cfg.genres,
            mode=self.tempo_mode,
        )
        if pick is None:
            log.warning("[dj] no pick for hr=%d; backing off %d s", hr, NO_PICK_BACKOFF_S)
            self._retry_at = now + NO_PICK_BACKOFF_S
            return None
        log.info("[dj] pick: %s", pick.describe(hr))
        self._set(last_pick_hr=hr)
        return pick

    def _record(self, pick: Pick) -> None:
        self._rejects = RecentRing(REJECT_N)
        self.recent.add(pick.id, pick.title, pick.artist)
        self.save_recent(self.recent)
        self._set(last_pick=pick, message="")

    def _queue(self, pick: Pick) -> None:
        if self.cfg.dry_run:
            log.info("[dj] dry-run: would queue %s", pick.uri)
        else:
            self.spotify.queue(pick.uri)
            log.info("[dj] queued %s (204)", pick.uri)
        self._record(pick)

    def _no_device(self, now: float) -> float:
        if now - self._message_at >= NO_DEVICE_RETRY_S:
            log.warning("[dj] no active Spotify device: %s", NO_DEVICE_MSG)
            self._message_at = now
        self._set(message=NO_DEVICE_MSG)
        self._retry_at = now + NO_DEVICE_RETRY_S
        return NO_DEVICE_RETRY_S

    def _start_playback(self, now: float) -> float | None:
        if now < self._retry_at:
            return None
        pick = self._next_pick or self._pick(now)
        if pick is None:
            return None
        self._next_pick = None
        try:
            if self.cfg.dry_run:
                log.info("[dj] dry-run: would play %s", pick.uri)
            else:
                self.spotify.play(pick.uri)
                log.info("[dj] started playback %s", pick.uri)
        except NoActiveDevice:
            # Spotify only counts a device as active once it has played
            # something, so an open-but-idle phone lands here. If it lists a
            # device we can name it and play there, instead of asking the
            # listener to press play by hand.
            device = self._idle_device()
            if device is not None:
                try:
                    self.spotify.play(pick.uri, device_id=device["id"])
                    log.info("[dj] started playback on %s: %s", device.get("name"), pick.uri)
                except (NoActiveDevice, SpotifyError) as e:
                    log.info("[dj] could not wake %s: %s", device.get("name"), e)
                    self._next_pick = pick
                    return self._no_device(now)
            else:
                # Keep the pick for the next attempt: re-picking every 30 s
                # while Spotify is closed spends a Spotify search for nothing.
                self._next_pick = pick
                return self._no_device(now)
        self._record(pick)
        self._need_start = False
        self._queued_for = None
        self._np = None          # what is playing just changed
        return None

    def _idle_device(self) -> dict | None:
        """A device Spotify knows about that we can start playback on."""
        try:
            devices = self.spotify.devices()
        except SpotifyError as e:
            log.info("[dj] could not list devices: %s", e)
            return None
        usable = [d for d in devices if d.get("id") and not d.get("is_restricted")]
        return usable[0] if usable else None

    def _do_skip(self, now: float) -> float:
        log.info("[dj] skip requested")
        pick = self._next_pick or self._pick(now)
        if pick is None:
            return self.cfg.poll_s
        self._next_pick = None
        try:
            self._queue(pick)
            if not self.cfg.dry_run:
                self.spotify.next()
        except NoActiveDevice:
            return self._no_device(now)
        self._queued_for = None
        self._np = None          # what is playing just changed
        return 1.0
