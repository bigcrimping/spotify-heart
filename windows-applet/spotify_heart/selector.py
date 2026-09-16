"""Song selection for a heart rate. Pure: lookups are injected."""

from __future__ import annotations

import logging
import random
import re
from collections import deque
from dataclasses import dataclass
from typing import Callable, Iterable, Protocol, Sequence

log = logging.getLogger(__name__)

TEMPO_MIN = 40
TEMPO_MAX = 220

# How the tempo is matched to the heart rate. "auto" tries all of them.
MODES = {"auto": None, "same": 1.0, "double": 2.0, "half": 0.5}


class SongLike(Protocol):
    title: str
    artist: str
    tempo: int
    genres: tuple[str, ...]


class TrackLike(Protocol):
    uri: str
    id: str


@dataclass(frozen=True)
class Pick:
    uri: str
    id: str
    title: str
    artist: str
    tempo: int
    multiplier: float  # 1, 2 or 0.5: tempo ~= hr * multiplier

    def describe(self, hr: int) -> str:
        m = {1: "x1", 2: "x2", 0.5: "x0.5"}.get(self.multiplier, f"x{self.multiplier}")
        return (
            f'hr={hr} -> tempo {self.tempo} ({m}) -> "{self.title}" by "{self.artist}" '
            f"({self.tempo} BPM) -> {self.uri}"
        )


class RecentRing:
    """Last N Spotify track IDs plus their title/artist keys."""

    def __init__(self, n: int, items: Iterable[dict] = ()):
        self.items: deque[dict] = deque(maxlen=max(n, 1))
        for it in items:
            self.items.append({"id": it["id"], "key": it.get("key", "")})

    def add(self, track_id: str, title: str, artist: str) -> None:
        self.items.append({"id": track_id, "key": song_key(title, artist)})

    def has_id(self, track_id: str) -> bool:
        return any(it["id"] == track_id for it in self.items)

    def has_song(self, title: str, artist: str) -> bool:
        k = song_key(title, artist)
        return any(it["key"] == k for it in self.items)

    def to_list(self) -> list[dict]:
        return list(self.items)


_SUFFIX_RE = re.compile(
    r"""(\s*[\(\[][^\)\]]*[\)\]])+\s*$"""                # trailing (…) / […] groups
    r"""|\s+-\s+.*\b(remaster|live|version|edit|mix|mono|stereo)\b.*$""",
    re.IGNORECASE,
)


def clean_title(title: str) -> str:
    """Drop "(Remastered 2011)", "[Live]", " - 2009 Remaster" style suffixes."""
    cleaned = _SUFFIX_RE.sub("", title).strip()
    return cleaned or title.strip()


def song_key(title: str, artist: str) -> str:
    return f"{clean_title(title).casefold()}|{artist.strip().casefold()}"


def candidate_tempos(
    hr: int,
    allow_half_double: bool = True,
    lo: int = TEMPO_MIN,
    hi: int = TEMPO_MAX,
    mode: str = "auto",
) -> list[tuple[int, float]]:
    """(tempo, multiplier) pairs for a heart rate, within [lo, hi], unshuffled.

    A mode other than "auto" pins the multiplier, unless that lands outside
    the tempo range (a heart rate of 115 has no double at 230), in which case
    the usual candidates are used instead.
    """
    forced = MODES.get(mode)
    if forced is not None:
        tempo = round(hr * forced)
        if lo <= tempo <= hi:
            return [(tempo, forced)]
        log.info("[select] %s of %d is outside %d-%d; matching freely", mode, hr, lo, hi)
    cands = [(hr, 1.0)]
    if allow_half_double:
        cands += [(hr * 2, 2.0), (round(hr / 2), 0.5)]
    return [(t, m) for t, m in cands if lo <= t <= hi]


def select(
    hr: int,
    lookup: Callable[[int], list[SongLike]],
    search: Callable[[str, str], TrackLike | None],
    recent: RecentRing,
    rng: random.Random,
    tol: int = 3,
    allow_half_double: bool = True,
    max_lookups: int = 6,
    offsets_per_tempo: int = 3,
    songs_per_list: int = 5,
    is_cached: Callable[[int], bool] = lambda tempo: False,
    genres: Sequence[str] = (),
    mode: str = "auto",
) -> Pick | None:
    """Pick a Spotify track whose tempo matches hr (or half/double) within tol.

    Returns None once the lookup budget is spent without a match.
    """
    wanted = {g.strip().casefold() for g in genres if g.strip()}
    cands = candidate_tempos(hr, allow_half_double, mode=mode)
    rng.shuffle(cands)
    lookups = 0
    for target, mult in cands:
        offsets = list(range(-tol, tol + 1))
        rng.shuffle(offsets)
        # Cached tempos first: same variety (songs are shuffled), fewer API calls.
        offsets.sort(key=lambda d: not is_cached(target + d))
        tried = 0
        for d in offsets:
            if tried >= offsets_per_tempo or lookups >= max_lookups:
                break
            tempo = target + d
            if not TEMPO_MIN <= tempo <= TEMPO_MAX:
                continue
            tried += 1
            lookups += 1
            songs = list(lookup(tempo))
            if wanted:
                # GetSongBPM gives each song its artist's genres. A song with
                # none cannot be shown to match, so it is left out.
                songs = [s for s in songs if wanted & set(getattr(s, "genres", ()))]
            if not songs:
                continue
            rng.shuffle(songs)
            fresh = [s for s in songs if not recent.has_song(s.title, s.artist)][:songs_per_list]
            for s in fresh:
                track = search(clean_title(s.title), s.artist)
                if track is None or recent.has_id(track.id):
                    continue
                return Pick(track.uri, track.id, s.title, s.artist, s.tempo, mult)
            log.info("[select] tempo %d: no Spotify match in %d tries", tempo, len(fresh))
        if lookups >= max_lookups:
            break
    log.warning("[select] hr=%d: no pick after %d lookups", hr, lookups)
    return None
