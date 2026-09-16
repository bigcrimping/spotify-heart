import random
from dataclasses import dataclass

import pytest

from spotify_heart.bpm_source import BpmTransientError, Song
from spotify_heart.config import Config
from spotify_heart.dj import (
    BACKOFF_MAX_S,
    LOGIN,
    NO_DEVICE_MSG,
    PLAYING,
    WAIT_HR_LOCK,
    Dj,
)
from spotify_heart.hr_smoother import HrSmoother
from spotify_heart.selector import RecentRing
from spotify_heart.spotify_client import (
    AuthError,
    NoActiveDevice,
    NowPlaying,
    Track,
    TransientError,
)


@dataclass
class Reader:
    connected: bool = True
    last_error: str = ""


class FakeSpotify:
    def __init__(self):
        self.has_login = True
        self.np: NowPlaying | None = None
        self.calls: list[tuple] = []
        self.fail: dict[str, list[Exception]] = {}
        self.np_calls = 0
        self.device_list: list[dict] = []

    def _maybe_fail(self, name):
        errs = self.fail.get(name)
        if errs:
            raise errs.pop(0)

    def refresh(self):
        self.calls.append(("refresh",))
        self._maybe_fail("refresh")

    def now_playing(self):
        self.np_calls += 1
        self._maybe_fail("now_playing")
        return self.np

    def search_track(self, title, artist):
        return Track(f"spotify:track:{title}", title, title, artist)

    def queue(self, uri):
        self.calls.append(("queue", uri))
        self._maybe_fail("queue")

    def play(self, uri, device_id=None):
        self.calls.append(("play", uri, device_id))
        self._maybe_fail("play")

    def next(self):
        self.calls.append(("next",))

    def devices(self):
        self._maybe_fail("devices")
        return list(self.device_list)

    def of(self, kind):
        return [c for c in self.calls if c[0] == kind]


class FakeBpm:
    def __init__(self):
        self.fail: list[Exception] = []
        self.lookups: list[int] = []

    def songs_at(self, tempo):
        self.lookups.append(tempo)
        if self.fail:
            raise self.fail.pop(0)
        return [Song(f"t{tempo}-{i}", f"a{tempo}-{i}", tempo) for i in range(50)]

    def is_cached(self, tempo):
        return True


def playing(track="cur", remaining_ms=100_000, is_playing=True, duration_ms=200_000):
    return NowPlaying(is_playing, duration_ms - remaining_ms, track, duration_ms, track, "Art")


class Rig:
    def __init__(self, dry_run=False):
        self.cfg = Config(dry_run=dry_run)
        self.sm = HrSmoother()
        self.sp = FakeSpotify()
        self.bpm = FakeBpm()
        self.reader = Reader()
        self.saved = 0
        self.logins = 0
        self.t = 0.0
        self.dj = Dj(
            self.cfg, self.sm, self.sp, self.bpm, RecentRing(20),
            save_recent=self._save, reader=self.reader, rng=random.Random(0),
            request_login=self._login,
        )

    def _save(self, ring):
        self.saved += 1

    def _login(self):
        self.logins += 1

    def lock(self, bpm=75):
        for i in range(10):
            self.sm.push(bpm, self.t - i * 0.5)

    def step(self, force_api=True):
        """One tick. By default the now-playing call is due, as it was before
        the DJ started spacing those calls out; tests that care about the
        spacing pass force_api=False."""
        if force_api:
            self.dj._next_api_at = 0
        return self.dj.step(self.t)

    def run(self, seconds, feed_hr=True, force_api=True):
        """Advance the fake clock, stepping whenever the dj asks to."""
        end = self.t + seconds
        while self.t < end:
            if feed_hr:
                self.lock()
            delay = self.step(force_api=force_api)
            self.t += max(delay, 0.001)

    def to_playing(self, track="cur"):
        self.sp.np = playing(track)
        self.step()  # LOGIN -> WAIT
        self.lock()
        self.step()  # WAIT -> PLAYING
        assert self.dj.state == PLAYING


@pytest.fixture
def rig():
    return Rig()


def test_login_then_wait_then_playing(rig):
    rig.step()
    assert rig.dj.state == WAIT_HR_LOCK
    assert rig.step() == 1
    assert rig.dj.state == WAIT_HR_LOCK
    rig.lock()
    rig.sp.np = playing()
    rig.step()
    assert rig.dj.state == PLAYING


def test_no_tokens_requests_login_once(rig):
    rig.sp.has_login = False
    for _ in range(5):
        rig.step()
    assert rig.dj.state == LOGIN
    assert rig.logins == 1
    assert rig.dj.status.colour == "red"
    rig.sp.has_login = True
    rig.step()
    assert rig.dj.state == WAIT_HR_LOCK


def test_queues_exactly_once_per_track_near_end(rig):
    rig.to_playing("A")
    rig.sp.np = playing("A", remaining_ms=60_000)
    rig.run(30)
    assert rig.sp.of("queue") == []
    rig.sp.np = playing("A", remaining_ms=14_000)
    rig.run(30)
    assert len(rig.sp.of("queue")) == 1
    pick = rig.dj.status.last_pick
    assert abs(pick.tempo - 75 * pick.multiplier) <= 3
    assert rig.dj.status.colour == "green"
    assert "queued:" in rig.dj.status.tooltip
    # Next track: queues again near its end.
    rig.sp.np = playing("B", remaining_ms=10_000)
    rig.run(10)
    assert len(rig.sp.of("queue")) == 2
    assert rig.sp.of("queue")[0] != rig.sp.of("queue")[1]
    assert rig.saved >= 2


def test_poll_interval(rig):
    rig.to_playing()
    assert rig.step() == rig.cfg.poll_s


def test_dry_run_never_queues():
    rig = Rig(dry_run=True)
    rig.to_playing()
    rig.sp.np = playing("A", remaining_ms=5_000)
    rig.run(10)
    assert rig.sp.of("queue") == [] and rig.sp.of("play") == []
    assert rig.dj.status.last_pick is not None


def test_paused_never_queues_or_plays(rig):
    rig.dj.paused.set()
    rig.sp.np = None
    rig.step()
    rig.lock()
    rig.run(120)
    rig.sp.np = playing("A", remaining_ms=5_000)
    rig.run(60)
    assert rig.sp.of("queue") == [] and rig.sp.of("play") == []
    assert rig.dj.status.paused


def test_starts_playback_when_idle_at_first_lock(rig):
    rig.sp.np = None  # 204
    rig.step()
    rig.lock()
    rig.step()
    assert len(rig.sp.of("play")) == 1
    rig.run(60)
    assert len(rig.sp.of("play")) == 1  # one-off


def test_paused_track_at_lock_starts_after_30s(rig):
    rig.sp.np = playing("A", is_playing=False)
    rig.step()
    rig.run(25)
    assert rig.sp.of("play") == []
    rig.run(10)
    assert len(rig.sp.of("play")) == 1


def test_pausing_spotify_later_never_plays(rig):
    rig.to_playing()
    rig.run(20)
    rig.sp.np = playing("cur", is_playing=False)
    rig.run(120)
    assert rig.sp.of("play") == []
    assert "idle" in rig.dj.status.tooltip.lower()


def test_no_device_message_every_30s(rig, caplog):
    rig.sp.np = None
    rig.step()
    rig.lock()
    rig.sp.fail["play"] = [NoActiveDevice("x", 404)] * 100
    rig.run(95)
    plays = rig.sp.of("play")
    assert 3 <= len(plays) <= 4  # t=0, 30, 60, 90
    assert rig.dj.status.message == NO_DEVICE_MSG
    assert NO_DEVICE_MSG in rig.dj.status.tooltip
    warnings = [r for r in caplog.records if NO_DEVICE_MSG in r.getMessage()]
    assert len(warnings) == len(plays)
    # Recovers once the device is back.
    rig.sp.fail["play"] = []
    rig.run(35)
    assert len(rig.sp.of("play")) == len(plays) + 1
    rig.sp.np = playing("started")
    rig.run(10)
    assert rig.dj.status.message == ""
    assert len(rig.sp.of("play")) == len(plays) + 1


def test_queue_404_retries_after_30s(rig):
    rig.to_playing()
    rig.sp.np = playing("A", remaining_ms=14_000)
    rig.sp.fail["queue"] = [NoActiveDevice("x", 404)]
    rig.run(10)
    assert len(rig.sp.of("queue")) == 1
    rig.run(25)
    assert len(rig.sp.of("queue")) == 2


def test_backoff_on_transient_error(rig):
    rig.to_playing()
    rig.sp.fail["now_playing"] = [TransientError("down")] * 10
    delays = [rig.step() for _ in range(8)]
    assert delays == [5, 10, 20, 40, 80, 160, 300, 300]
    assert rig.dj.state == PLAYING
    assert rig.dj.status.colour == "amber"
    rig.sp.fail["now_playing"] = []
    assert rig.step() == rig.cfg.poll_s
    assert rig.dj.status.colour == "green"
    assert rig.step() == rig.cfg.poll_s  # backoff reset


def test_bpm_transient_error_backs_off(rig):
    rig.to_playing()
    rig.sp.np = playing("A", remaining_ms=5_000)
    rig.bpm.fail = [BpmTransientError("503")]
    assert rig.step() == 5
    assert rig.step() == rig.cfg.poll_s
    assert len(rig.sp.of("queue")) == 1


def test_auth_error_returns_to_login(rig):
    rig.to_playing()
    rig.sp.fail["now_playing"] = [AuthError("revoked")]
    rig.step()
    assert rig.dj.state == LOGIN
    assert rig.dj.status.colour == "red"
    rig.step()  # has_login still true -> refresh -> WAIT
    assert rig.dj.state == WAIT_HR_LOCK


def test_rejected_refresh_stays_in_login(rig):
    rig.sp.fail["refresh"] = [AuthError("invalid_grant")] * 3
    for _ in range(3):
        rig.step()
        assert rig.dj.state == LOGIN


def test_no_pick_without_recent_lock(rig):
    rig.to_playing()
    rig.t += 400  # lock is now stale, and no new readings
    rig.sp.np = playing("A", remaining_ms=5_000)
    rig.run(20, feed_hr=False)
    assert rig.sp.of("queue") == []


def test_skip_queues_then_next(rig):
    rig.to_playing()
    rig.dj.skip.set()
    rig.step()
    assert [c[0] for c in rig.sp.calls[-2:]] == ["queue", "next"]
    assert not rig.dj.skip.is_set()


def test_sensor_disconnected_is_grey(rig):
    rig.to_playing()
    rig.reader.connected = False
    rig.reader.last_error = "board not found"
    rig.step()
    assert rig.dj.status.colour == "grey"
    assert "board not found" in rig.dj.status.tooltip


def test_backoff_caps(rig):
    rig.to_playing()
    rig.sp.fail["now_playing"] = [TransientError("down")] * 20
    assert max(rig.step() for _ in range(20)) == BACKOFF_MAX_S


# --------------------------------------------------- pre-selection and polling


def test_pick_happens_before_the_queue_window(rig):
    """The queue window must hold nothing but the queue call itself."""
    rig.to_playing("A")
    rig.sp.np = playing("A", remaining_ms=120_000)
    rig.run(20)
    assert rig.dj._next_pick is not None       # chosen well ahead of time
    assert rig.sp.of("queue") == []
    lookups_before = len(rig.bpm.lookups)

    rig.sp.np = playing("A", remaining_ms=12_000)
    rig.step()
    assert len(rig.sp.of("queue")) == 1
    assert len(rig.bpm.lookups) == lookups_before   # no lookup inside the window
    assert rig.dj._next_pick is None


def test_repick_when_heart_rate_drifts(rig):
    rig.to_playing("A")
    rig.sp.np = playing("A", remaining_ms=120_000)
    rig.run(10)
    first = rig.dj._next_pick
    assert rig.dj._next_pick_hr == 75

    rig.t += 40                                  # past REPICK_MIN_GAP_S
    rig.lock(120)
    rig.run(10, feed_hr=False)
    assert rig.dj._next_pick_hr == 120
    assert rig.dj._next_pick is not first
    pick = rig.dj._next_pick
    assert abs(pick.tempo - 120 * pick.multiplier) <= 3


def test_small_heart_rate_change_keeps_the_pick(rig):
    rig.to_playing("A")
    rig.sp.np = playing("A", remaining_ms=120_000)
    rig.run(10)
    first = rig.dj._next_pick
    rig.t += 60
    rig.lock(77)                                 # within bpm_tol of 75
    rig.run(10, feed_hr=False)
    assert rig.dj._next_pick is first


def test_long_track_does_not_poll_every_five_seconds(rig):
    """A four-minute track used to cost ~48 now-playing calls."""
    rig.to_playing()
    rig.sp.np_calls = 0
    remaining = 240_000
    while remaining > 0:
        rig.sp.np = playing("A", remaining_ms=remaining, duration_ms=240_000)
        before = rig.t
        rig.run(5, force_api=False)
        remaining -= int((rig.t - before) * 1000)
    assert rig.sp.np_calls <= 15                 # was 48 at a fixed 5 s poll
    assert len(rig.sp.of("queue")) == 1          # and the queue still happened


def test_stale_lock_shows_in_the_tooltip(rig):
    rig.to_playing()
    rig.t += 120                                 # no new readings for 2 min
    rig.run(1, feed_hr=False)
    assert "stale" in rig.dj.status.tooltip
    assert rig.dj.status.colour == "amber"


def test_skipped_track_queues_the_waiting_pick(rig):
    """The listener skips, so the queue window never arrives. The pick that was
    already chosen should still be queued: it plays after the new track."""
    rig.to_playing("A")
    rig.sp.np = playing("A", remaining_ms=120_000)
    rig.run(10)
    waiting = rig.dj._next_pick
    assert waiting is not None and rig.sp.of("queue") == []

    rig.sp.np = playing("B", remaining_ms=200_000)   # user hit skip
    rig.step()
    assert rig.sp.of("queue") == [("queue", waiting.uri)]
    assert rig.dj._queued_for == "B"
    assert rig.dj._next_pick is None

    # And it is not queued a second time for the same track.
    rig.run(20)
    assert len(rig.sp.of("queue")) == 1


def test_track_change_without_a_waiting_pick_queues_nothing(rig):
    rig.to_playing("A")
    rig.sp.np = playing("A", remaining_ms=120_000)
    rig.dj._next_pick = None
    rig.sp.np = playing("B", remaining_ms=200_000)
    rig.step()
    assert rig.sp.of("queue") == []


def test_no_device_retry_reuses_the_same_pick(rig):
    """Re-picking on every 30 s retry would spend a Spotify search each time."""
    rig.sp.np = None
    rig.step()
    rig.lock()
    rig.sp.fail["play"] = [NoActiveDevice("x", 404)] * 10
    rig.step()
    first = rig.dj._next_pick
    assert first is not None                  # kept for the next attempt
    lookups = len(rig.bpm.lookups)
    rig.run(95)
    assert rig.dj._next_pick is first
    assert len(rig.bpm.lookups) == lookups    # no fresh selection per retry
    assert len(rig.sp.of("play")) >= 3        # but it did keep retrying


def test_idle_device_is_woken_instead_of_asking_the_listener(rig):
    """An open but idle phone is not 'active', yet we can name it and play there."""
    rig.sp.np = None
    rig.sp.device_list = [{"id": "dev1", "name": "Pixel 9 Pro", "is_restricted": False}]
    rig.sp.fail["play"] = [NoActiveDevice("x", 404)]   # the untargeted attempt
    rig.step()
    rig.lock()
    rig.step()
    plays = rig.sp.of("play")
    assert [p[2] for p in plays] == [None, "dev1"]     # retried at the named device
    assert rig.dj.status.message != NO_DEVICE_MSG
    assert rig.dj.status.last_pick is not None


def test_restricted_devices_are_not_used(rig):
    rig.sp.np = None
    rig.sp.device_list = [{"id": "dev1", "name": "TV", "is_restricted": True}]
    rig.sp.fail["play"] = [NoActiveDevice("x", 404)] * 5
    rig.step()
    rig.lock()
    rig.step()
    assert rig.dj.status.message == NO_DEVICE_MSG


# --------------------------------------------------------------- tempo mode


def test_tempo_mode_applies_to_the_next_pick(rig):
    rig.to_playing("A")
    rig.sp.np = playing("A", remaining_ms=120_000)
    rig.run(10)
    assert rig.dj.status.tempo_mode == "auto"

    rig.dj.set_tempo_mode("double")
    assert rig.dj._next_pick is None            # the old pick used the old mode
    assert rig.dj.wake.is_set()                 # and it re-picks straight away
    rig.run(10)
    pick = rig.dj._next_pick
    assert pick is not None and pick.multiplier == 2.0
    assert abs(pick.tempo - 75 * 2) <= 3
    assert rig.dj.status.tempo_mode == "double"


def test_tempo_mode_survives_until_changed(rig):
    """Half of the rig's default 75 BPM is under the 40 BPM floor, so this
    test locks at 96 only (48 is comfortably in range)."""
    rig.dj.set_tempo_mode("half")
    rig.sp.np = playing("A", remaining_ms=120_000)
    rig.step()                      # LOGIN -> WAIT_HR_LOCK
    rig.lock(96)
    rig.step()                      # WAIT_HR_LOCK -> PLAYING, picks
    for _ in range(3):
        rig.lock(96)
        rig.run(30, feed_hr=False)
        assert rig.dj._next_pick.multiplier == 0.5
        assert abs(rig.dj._next_pick.tempo - 48) <= 3


def test_unknown_tempo_mode_is_refused(rig):
    rig.dj.set_tempo_mode("sideways")
    assert rig.dj.tempo_mode == "auto"


def test_setting_the_same_mode_does_not_discard_the_pick(rig):
    rig.to_playing("A")
    rig.sp.np = playing("A", remaining_ms=120_000)
    rig.run(10)
    pick = rig.dj._next_pick
    rig.dj.set_tempo_mode("auto")
    assert rig.dj._next_pick is pick


# ------------------------------------------------------------ pick another


def test_repick_offers_a_different_song(rig):
    rig.to_playing("A")
    rig.sp.np = playing("A", remaining_ms=120_000)
    rig.run(10)
    first = rig.dj._next_pick
    assert first is not None

    rig.dj.repick()
    assert rig.dj._next_pick is None and rig.dj.wake.is_set()
    rig.run(10)
    second = rig.dj._next_pick
    assert second is not None and second.id != first.id
    assert rig.sp.of("queue") == []              # nothing was queued meanwhile


def test_repick_does_not_come_back_to_a_rejected_song(rig):
    rig.to_playing("A")
    rig.sp.np = playing("A", remaining_ms=120_000)
    rig.run(10)
    seen = set()
    for _ in range(5):
        seen.add(rig.dj._next_pick.id)
        rig.dj.repick()
        rig.run(10)
    assert len(seen) == 5                        # five different songs


def test_rejects_are_forgotten_once_something_is_queued(rig):
    rig.to_playing("A")
    rig.sp.np = playing("A", remaining_ms=120_000)
    rig.run(10)
    rig.dj.repick()
    rig.run(10)
    assert len(rig.dj._rejects.to_list()) == 1

    rig.sp.np = playing("A", remaining_ms=10_000)
    rig.run(10)
    assert len(rig.sp.of("queue")) == 1
    assert rig.dj._rejects.to_list() == []


def test_repick_with_nothing_on_offer_is_harmless(rig):
    rig.dj.repick()
    assert rig.dj._next_pick is None
    assert rig.dj._rejects.to_list() == []
