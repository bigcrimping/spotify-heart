import json
from pathlib import Path

import pytest
import responses

from spotify_heart.bpm_source import (
    BASE,
    BpmAuthError,
    BpmSource,
    BpmTransientError,
    TTL_S,
    parse_tempo_response,
)

FIX = Path(__file__).parent / "fixtures"
URL = f"{BASE}/tempo/"


def fixture(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_parse_real_fixture():
    songs = parse_tempo_response(fixture("getsongbpm_tempo_120.json"))
    assert songs
    assert all(s.tempo == 120 for s in songs)  # tempo arrives as a string
    assert all(s.title and s.artist for s in songs)


def test_parse_out_of_range_error_object():
    assert parse_tempo_response(fixture("getsongbpm_tempo_out_of_range.json")) == []


def test_parse_skips_bad_items():
    data = {"tempo": [
        {"song_title": "Ok", "tempo": "99.6", "artist": {"name": "A"}},
        {"song_title": "", "tempo": "99", "artist": {"name": "A"}},
        {"song_title": "No artist", "tempo": "99", "artist": None},
        {"song_title": "Bad tempo", "tempo": "fast", "artist": {"name": "A"}},
        "junk",
    ]}
    songs = parse_tempo_response(data)
    assert [(s.title, s.tempo) for s in songs] == [("Ok", 100)]


@responses.activate
def test_fetch_then_cache_hit(tmp_path):
    responses.get(URL, json=fixture("getsongbpm_tempo_120.json"))
    src = BpmSource("k", tmp_path, clock=Clock())
    assert not src.is_cached(120)
    a = src.songs_at(120)
    b = src.songs_at(120)
    assert a == b and a
    assert len(responses.calls) == 1
    assert src.requests_made == 1
    assert src.is_cached(120)
    assert responses.calls[0].request.params == {"api_key": "k", "bpm": "120"}
    # A fresh instance (next run) also hits the cache.
    assert BpmSource("k", tmp_path, clock=Clock()).songs_at(120) == a
    assert len(responses.calls) == 1


@responses.activate
def test_cache_expires(tmp_path):
    responses.get(URL, json=fixture("getsongbpm_tempo_120.json"))
    clock = Clock()
    src = BpmSource("k", tmp_path, clock=clock)
    src.songs_at(120)
    clock.t += TTL_S + 1
    assert not src.is_cached(120)
    src.songs_at(120)
    assert len(responses.calls) == 2


@responses.activate
def test_bad_key(tmp_path):
    responses.get(URL, status=401, json={"error": "Invalid API Key, or inactive."})
    with pytest.raises(BpmAuthError):
        BpmSource("k", tmp_path).songs_at(120)


def test_empty_key(tmp_path):
    with pytest.raises(BpmAuthError):
        BpmSource("", tmp_path).songs_at(120)


@responses.activate
def test_server_errors_are_transient(tmp_path):
    responses.get(URL, status=503)
    responses.get(URL, status=429)
    src = BpmSource("k", tmp_path)
    for _ in range(2):
        with pytest.raises(BpmTransientError):
            src.songs_at(120)


def test_corrupt_cache_is_refetched(tmp_path):
    (tmp_path / "bpm_120.json").write_text("{not json")
    assert not BpmSource("k", tmp_path).is_cached(120)
