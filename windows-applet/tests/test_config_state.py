import json
import logging

from spotify_heart import config, state
from spotify_heart.selector import RecentRing


def write(p, data):
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


# ------------------------------------------------------------------ config


def test_creates_file_with_defaults(tmp_path):
    p = tmp_path / "config.local.json"
    cfg = config.load(p)
    assert p.exists()
    saved = json.loads(p.read_text(encoding="utf-8"))
    assert saved["poll_s"] == 5 and saved["genres"] == []
    assert "path" not in saved                      # runtime-only field
    assert cfg.path == p


def test_paths_sit_beside_the_config(tmp_path):
    cfg = config.load(tmp_path / "config.local.json")
    assert cfg.tokens_path == tmp_path / "tokens.json"
    assert cfg.state_path == tmp_path / "state.json"
    assert cfg.cache_dir == tmp_path / "cache"
    assert cfg.log_dir == tmp_path / "logs"


def test_missing_keys_take_defaults_and_are_written_back(tmp_path):
    p = write(tmp_path / "c.json", {"poll_s": 9})
    cfg = config.load(p)
    assert cfg.poll_s == 9
    assert cfg.queue_lead_ms == 15000
    assert json.loads(p.read_text(encoding="utf-8"))["queue_lead_ms"] == 15000


def test_unknown_keys_are_ignored(tmp_path):
    p = write(tmp_path / "c.json", {"poll_s": 9, "nonsense": 1})
    cfg = config.load(p)
    assert cfg.poll_s == 9
    assert not hasattr(cfg, "nonsense")


def test_numbers_written_as_text_are_accepted(tmp_path):
    p = write(tmp_path / "c.json", {"poll_s": "7", "queue_lead_ms": "20000"})
    cfg = config.load(p)
    assert cfg.poll_s == 7.0 and isinstance(cfg.poll_s, float)
    assert cfg.queue_lead_ms == 20000 and isinstance(cfg.queue_lead_ms, int)


def test_bad_value_falls_back_to_default_with_a_warning(tmp_path, caplog):
    p = write(tmp_path / "c.json", {"poll_s": "soon", "bpm_tol": [1], "serial_port": 3})
    with caplog.at_level(logging.WARNING):
        cfg = config.load(p)
    assert (cfg.poll_s, cfg.bpm_tol, cfg.serial_port) == (5, 3, "auto")
    warned = " ".join(r.getMessage() for r in caplog.records)
    for key in ("poll_s", "bpm_tol", "serial_port"):
        assert key in warned


def test_genres_accepts_a_list_or_a_comma_separated_string(tmp_path):
    assert config.load(write(tmp_path / "a.json", {"genres": ["rock", "pop"]})).genres == [
        "rock",
        "pop",
    ]
    assert config.load(write(tmp_path / "b.json", {"genres": "rock, pop"})).genres == [
        "rock",
        "pop",
    ]
    assert config.load(write(tmp_path / "c.json", {"genres": [1]})).genres == []


def test_booleans(tmp_path):
    assert config.load(write(tmp_path / "a.json", {"dry_run": True})).dry_run is True
    assert config.load(write(tmp_path / "b.json", {"dry_run": "true"})).dry_run is True
    assert config.load(write(tmp_path / "c.json", {"dry_run": "no"})).dry_run is False


def test_save_roundtrip(tmp_path):
    cfg = config.load(tmp_path / "c.json")
    cfg.poll_s = 11
    cfg.genres = ["jazz"]
    cfg.save()
    again = config.load(tmp_path / "c.json")
    assert again.poll_s == 11 and again.genres == ["jazz"]


# ------------------------------------------------------------------- state


def test_recent_ring_survives_a_restart(tmp_path):
    p = tmp_path / "state.json"
    ring = RecentRing(20)
    ring.add("id1", "Song One", "Artist")
    ring.add("id2", "Song Two", "Artist")
    state.save_recent(p, ring)

    loaded = state.load_recent(p, 20)
    assert loaded.has_id("id1") and loaded.has_song("Song Two", "Artist")
    assert not loaded.has_id("id3")


def test_missing_state_file_is_an_empty_ring(tmp_path):
    ring = state.load_recent(tmp_path / "nothing.json", 20)
    assert ring.to_list() == []


def test_unreadable_state_file_is_ignored(tmp_path, caplog):
    p = tmp_path / "state.json"
    p.write_text("{ not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        ring = state.load_recent(p, 20)
    assert ring.to_list() == []
    assert "state" in " ".join(r.getMessage() for r in caplog.records)


def test_saved_ring_is_capped_at_n(tmp_path):
    p = tmp_path / "state.json"
    ring = RecentRing(3)
    for i in range(6):
        ring.add(f"id{i}", f"s{i}", "a")
    state.save_recent(p, ring)
    assert len(json.loads(p.read_text(encoding="utf-8"))["recent"]) == 3
    assert state.load_recent(p, 3).has_id("id5")


def test_save_replaces_the_file_atomically(tmp_path):
    p = tmp_path / "state.json"
    state.save_recent(p, RecentRing(5))
    state.save_recent(p, RecentRing(5))
    assert not (tmp_path / "state.tmp").exists()
