"""GetSongBPM tempo lookup with a 7-day on-disk cache.

Response shape (tests/fixtures/getsongbpm_tempo_120.json, captured 2026-09-16):
  {"tempo": [{"song_id", "song_title", "tempo": "120", "artist": {"name", ...}, "album": {...}}, ...]}
Up to 250 songs, all at exactly the requested tempo, same list every call.
Out of range (valid is 40..~249): {"tempo": {"error": "Tempo not in allowed range"}}.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import requests

log = logging.getLogger(__name__)

BASE = "https://api.getsong.co"
TTL_S = 7 * 24 * 3600
TIMEOUT_S = 15
CACHE_V = 2   # bump when the cached shape changes; older files are refetched


class BpmError(Exception):
    pass


class BpmAuthError(BpmError):
    """Key missing, wrong or not activated."""


class BpmTransientError(BpmError):
    pass


@dataclass(frozen=True)
class Song:
    title: str
    artist: str
    tempo: int
    genres: tuple[str, ...] = ()


def parse_tempo_response(data) -> list[Song]:
    items = data.get("tempo") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []  # {"tempo": {"error": ...}} or anything unexpected
    songs = []
    for it in items:
        try:
            title = (it.get("song_title") or "").strip()
            artist = ((it.get("artist") or {}).get("name") or "").strip()
            tempo = round(float(it.get("tempo")))
        except (AttributeError, TypeError, ValueError):
            continue
        raw_genres = (it.get("artist") or {}).get("genres") or []
        genres = tuple(g.strip().casefold() for g in raw_genres if isinstance(g, str) and g.strip())
        if title and artist:
            songs.append(Song(title, artist, tempo, genres))
    return songs


class BpmSource:
    def __init__(
        self,
        api_key: str,
        cache_dir: Path,
        session: requests.Session | None = None,
        clock: Callable[[], float] = time.time,
        ttl_s: float = TTL_S,
    ):
        self.api_key = api_key
        self.cache_dir = cache_dir
        self.http = session or requests.Session()
        self.clock = clock
        self.ttl_s = ttl_s
        self.requests_made = 0
        self._mem: dict[int, tuple[float, list[Song]]] = {}  # tempo -> (fetched_at, songs)

    def _cache_path(self, bpm: int) -> Path:
        return self.cache_dir / f"bpm_{bpm}.json"

    def _read_cache(self, bpm: int) -> list[Song] | None:
        hit = self._mem.get(bpm)
        if hit is not None:
            fetched_at, songs = hit
            if self.clock() - fetched_at <= self.ttl_s:
                return songs
            del self._mem[bpm]
        p = self._cache_path(bpm)
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return None
        if d.get("v") != CACHE_V or self.clock() - d.get("fetched_at", 0) > self.ttl_s:
            return None
        try:
            songs = [
                Song(s["title"], s["artist"], s["tempo"], tuple(s.get("genres", ())))
                for s in d.get("songs", [])
            ]
        except (KeyError, TypeError):
            return None
        self._mem[bpm] = (d.get("fetched_at", 0), songs)
        return songs

    def _write_cache(self, bpm: int, songs: list[Song]) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        p = self._cache_path(bpm)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "v": CACHE_V,
                    "fetched_at": self.clock(),
                    "songs": [{**s.__dict__, "genres": list(s.genres)} for s in songs],
                }
            ),
            encoding="utf-8",
        )
        tmp.replace(p)
        self._mem[bpm] = (self.clock(), songs)

    def is_cached(self, bpm: int) -> bool:
        """select() asks this once per candidate tempo, so keep it cheap: the
        first read parses the file, the rest come from memory."""
        return self._read_cache(bpm) is not None

    def songs_at(self, bpm: int) -> list[Song]:
        cached = self._read_cache(bpm)
        if cached is not None:
            log.info("[bpm] cache hit tempo %d (%d songs)", bpm, len(cached))
            return cached
        if not self.api_key:
            raise BpmAuthError("getsongbpm_key is empty in the config file")
        try:
            r = self.http.get(
                f"{BASE}/tempo/", params={"api_key": self.api_key, "bpm": bpm}, timeout=TIMEOUT_S
            )
        except requests.RequestException as e:
            raise BpmTransientError(f"GetSongBPM request failed: {e}")
        self.requests_made += 1
        if r.status_code in (401, 403):
            raise BpmAuthError(f"GetSongBPM rejected the key: {r.status_code} {r.text[:120]}")
        if r.status_code == 429 or r.status_code >= 500:
            raise BpmTransientError(f"GetSongBPM {r.status_code}")
        if r.status_code != 200:
            raise BpmError(f"GetSongBPM {r.status_code} {r.text[:120]}")
        try:
            songs = parse_tempo_response(r.json())
        except ValueError:
            raise BpmTransientError("GetSongBPM returned non-JSON")
        log.info("[bpm] GET tempo %d -> %d songs", bpm, len(songs))
        self._write_cache(bpm, songs)
        return songs
