import random
from dataclasses import dataclass

from spotify_heart.selector import (
    RecentRing,
    candidate_tempos,
    clean_title,
    select,
    song_key,
)


@dataclass
class S:
    title: str
    artist: str
    tempo: int


@dataclass
class T:
    uri: str
    id: str


def library(tempos=range(40, 221), per=10):
    """Fake GetSongBPM: `per` songs at every tempo."""
    return {t: [S(f"Song {t}-{i}", f"Artist {t}-{i}", t) for i in range(per)] for t in tempos}


class Lookup:
    def __init__(self, lib):
        self.lib = lib
        self.calls = []

    def __call__(self, tempo):
        self.calls.append(tempo)
        return list(self.lib.get(tempo, []))


def search_all(title, artist):
    return T(f"spotify:track:{title}", title)


def test_candidates_normal():
    assert sorted(candidate_tempos(72)) == [(36, 0.5), (72, 1.0), (144, 2.0)][1:]  # 36 < 40 dropped


def test_candidates_half_double_extremes():
    assert sorted(t for t, _ in candidate_tempos(45)) == [45, 90]
    assert sorted(t for t, _ in candidate_tempos(180)) == [90, 180]
    assert candidate_tempos(100, allow_half_double=False) == [(100, 1.0)]


def test_pick_within_tolerance_of_a_candidate():
    lk = Lookup(library())
    for seed in range(50):
        p = select(72, lk, search_all, RecentRing(20), random.Random(seed), tol=3)
        assert p is not None
        assert abs(p.tempo - 72 * p.multiplier) <= 3
        assert p.multiplier in (1.0, 2.0)


def test_extremes_find_something():
    for hr in (45, 180):
        p = select(hr, Lookup(library()), search_all, RecentRing(20), random.Random(1))
        assert p is not None
        assert abs(p.tempo - hr * p.multiplier) <= 3


def test_offsets_cover_tolerance_range():
    seen = set()
    lk = Lookup(library())
    for seed in range(200):
        select(100, lk, search_all, RecentRing(20), random.Random(seed), allow_half_double=False)
    seen = {t - 100 for t in lk.calls}
    assert seen == set(range(-3, 4))


def test_empty_lists_try_next_offset():
    lib = {101: [S("Only", "One", 101)]}
    lk = Lookup(lib)
    p = select(100, lk, search_all, RecentRing(20), random.Random(3), tol=1,
               allow_half_double=False, offsets_per_tempo=3)
    assert p is not None and p.tempo == 101
    assert len(lk.calls) <= 3


def test_no_repeat_by_song_and_by_id():
    lib = {100: [S("A", "X", 100), S("B", "X", 100)]}
    recent = RecentRing(20)
    recent.add("id-a", "A", "X")
    for seed in range(20):
        p = select(100, Lookup(lib), search_all, recent, random.Random(seed), tol=0,
                   allow_half_double=False)
        assert p.title == "B"

    # Same Spotify ID reached through a different title is also skipped.
    recent = RecentRing(20)
    recent.add("same", "Z", "Z")
    p = select(100, Lookup(lib), lambda t, a: T("spotify:track:same", "same"), recent,
               random.Random(0), tol=0, allow_half_double=False)
    assert p is None


def test_ring_is_bounded():
    r = RecentRing(2)
    for i in range(5):
        r.add(f"id{i}", f"t{i}", "a")
    assert [it["id"] for it in r.to_list()] == ["id3", "id4"]
    assert not r.has_id("id0") and r.has_song("t4", "a")
    assert RecentRing(2, r.to_list()).has_id("id3")


def test_none_when_budget_exhausted():
    lk = Lookup(library())
    p = select(100, lk, lambda t, a: None, RecentRing(20), random.Random(0), max_lookups=6)
    assert p is None
    assert len(lk.calls) == 6


def test_searches_capped_per_list():
    searches = []

    def search(t, a):
        searches.append(t)
        return None

    select(100, Lookup(library()), search, RecentRing(20), random.Random(0),
           max_lookups=1, allow_half_double=False)
    assert len(searches) == 5


def test_search_uses_cleaned_title():
    lib = {100: [S("Hey Jude (Remastered 2015)", "The Beatles", 100)]}
    got = []
    select(100, Lookup(lib), lambda t, a: got.append((t, a)) or T("u", "i"), RecentRing(5),
           random.Random(0), tol=0, allow_half_double=False)
    assert got == [("Hey Jude", "The Beatles")]


def test_prefers_cached_offsets():
    lk = Lookup(library())
    for seed in range(20):
        lk.calls.clear()
        select(100, lk, search_all, RecentRing(5), random.Random(seed),
               allow_half_double=False, is_cached=lambda t: t == 98)
        assert lk.calls == [98]


def test_clean_title():
    assert clean_title("Hey Jude (Remastered 2015)") == "Hey Jude"
    assert clean_title("Song [Live] (Deluxe)") == "Song"
    assert clean_title("Song - 2009 Remaster") == "Song"
    assert clean_title("Song - Live at Wembley") == "Song"
    assert clean_title("Mr. Brightside") == "Mr. Brightside"
    assert clean_title("(I Can't Get No) Satisfaction") == "(I Can't Get No) Satisfaction"
    assert clean_title("Rock - Paper - Scissors") == "Rock - Paper - Scissors"
    assert clean_title("(Untitled)") == "(Untitled)"
    assert song_key("Hey Jude (Remastered)", " The Beatles ") == "hey jude|the beatles"


# ------------------------------------------------------------ genre filtering


@dataclass
class G:
    title: str
    artist: str
    tempo: int
    genres: tuple


def genre_library(tempos=range(40, 221)):
    """Three songs per tempo: rock, jazz, and one with no genres at all."""
    return {
        t: [
            G(f"rock {t}", f"r{t}", t, ("rock", "indie")),
            G(f"jazz {t}", f"j{t}", t, ("jazz",)),
            G(f"plain {t}", f"p{t}", t, ()),
        ]
        for t in tempos
    }


def test_genre_filter_keeps_only_matching_songs():
    for seed in range(20):
        p = select(100, Lookup(genre_library()), search_all, RecentRing(20),
                   random.Random(seed), genres=["rock"])
        assert p is not None and p.title.startswith("rock")


def test_genre_filter_is_case_insensitive_and_matches_any_listed():
    got = set()
    for seed in range(30):
        p = select(100, Lookup(genre_library()), search_all, RecentRing(20),
                   random.Random(seed), genres=["ROCK", " jazz "])
        got.add(p.title.split()[0])
    assert got == {"rock", "jazz"}


def test_no_genres_configured_allows_everything():
    got = set()
    for seed in range(30):
        p = select(100, Lookup(genre_library()), search_all, RecentRing(20),
                   random.Random(seed), genres=[])
        got.add(p.title.split()[0])
    assert got == {"rock", "jazz", "plain"}


def test_unmatchable_genre_gives_up_within_budget():
    lk = Lookup(genre_library())
    p = select(100, lk, search_all, RecentRing(20), random.Random(0), genres=["polka"])
    assert p is None
    assert len(lk.calls) == 6          # the usual lookup budget, then it stops


# ------------------------------------------------------------- tempo modes


def test_mode_pins_the_multiplier():
    assert candidate_tempos(80, mode="double") == [(160, 2.0)]
    assert candidate_tempos(80, mode="half") == [(40, 0.5)]
    assert candidate_tempos(80, mode="same") == [(80, 1.0)]
    assert sorted(candidate_tempos(80, mode="auto")) == [(40, 0.5), (80, 1.0), (160, 2.0)]


def test_mode_out_of_range_falls_back_to_matching_freely():
    # 115 x 2 = 230, above the 220 limit, so the other options are offered.
    assert sorted(candidate_tempos(115, mode="double")) == [(58, 0.5), (115, 1.0)]
    # 70 / 2 = 35, below the 40 limit.
    assert sorted(candidate_tempos(70, mode="half")) == [(70, 1.0), (140, 2.0)]


def test_select_honours_the_mode():
    for seed in range(20):
        p = select(80, Lookup(library()), search_all, RecentRing(20),
                   random.Random(seed), mode="double")
        assert p.multiplier == 2.0
        assert abs(p.tempo - 160) <= 3
        p = select(80, Lookup(library()), search_all, RecentRing(20),
                   random.Random(seed), mode="half")
        assert p.multiplier == 0.5
        assert abs(p.tempo - 40) <= 3


def test_unknown_mode_behaves_as_auto():
    assert sorted(candidate_tempos(80, mode="sideways")) == sorted(candidate_tempos(80))
