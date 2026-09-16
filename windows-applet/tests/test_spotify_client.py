import json
from pathlib import Path

import pytest
import responses

from spotify_heart.spotify_client import (
    API,
    TOKEN_URL,
    AuthError,
    NoActiveDevice,
    SpotifyClient,
    TokenStore,
    TransientError,
)

FIX = Path(__file__).parent / "fixtures"
NOW = 1_000_000.0


def fixture(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


@pytest.fixture
def client(tmp_path):
    store = TokenStore(tmp_path / "tokens.json")
    store.refresh_token = "r1"
    store.access_token = "a1"
    store.expires_at = NOW + 3600
    store.save()
    sleeps = []
    c = SpotifyClient("cid", store, clock=lambda: NOW, sleep=sleeps.append)
    c.sleeps = sleeps
    return c


def token_resp(access="a2", refresh=None):
    body = {"access_token": access, "token_type": "Bearer", "expires_in": 3600, "scope": "x"}
    if refresh:
        body["refresh_token"] = refresh
    return body


@responses.activate
def test_search_parses_fixture(client):
    data = fixture("spotify_search.json")
    item = data["tracks"]["items"][0]
    responses.get(f"{API}/search", json=data)
    t = client.search_track("Mr. Brightside", "The Killers")
    assert t.uri.startswith("spotify:track:")
    assert t.uri == f"spotify:track:{t.id}" == item["uri"]
    assert t.artist == item["artists"][0]["name"]
    q = responses.calls[0].request.params
    assert q["q"] == "track:Mr. Brightside artist:The Killers"
    assert q["type"] == "track" and q["limit"] == "1"
    assert responses.calls[0].request.headers["Authorization"] == "Bearer a1"


@responses.activate
def test_search_empty(client):
    responses.get(f"{API}/search", json=fixture("spotify_search_empty.json"))
    assert client.search_track("zz", "zz") is None


@responses.activate
def test_401_refreshes_then_retries(client):
    responses.get(f"{API}/me/player/devices", status=401)
    responses.post(TOKEN_URL, json=token_resp("a2"))
    responses.get(f"{API}/me/player/devices", json=fixture("spotify_devices.json"))
    devs = client.devices()
    assert devs and devs[0]["name"] == "Test Device"
    assert responses.calls[2].request.headers["Authorization"] == "Bearer a2"
    body = responses.calls[1].request.body
    assert "grant_type=refresh_token" in body and "refresh_token=r1" in body and "client_id=cid" in body


@responses.activate
def test_second_401_is_auth_error(client):
    responses.get(f"{API}/me/player/devices", status=401)
    responses.post(TOKEN_URL, json=token_resp("a2"))
    responses.get(f"{API}/me/player/devices", status=401)
    with pytest.raises(AuthError):
        client.devices()


@responses.activate
def test_rejected_refresh_is_auth_error(client):
    client.tokens.expires_at = 0
    responses.post(TOKEN_URL, status=400, json={"error": "invalid_grant"})
    with pytest.raises(AuthError):
        client.devices()


@responses.activate
def test_429_honours_retry_after(client):
    responses.get(f"{API}/me/player/devices", status=429, headers={"Retry-After": "7"})
    responses.get(f"{API}/me/player/devices", json={"devices": []})
    assert client.devices() == []
    assert client.sleeps == [7]


@responses.activate
def test_429_gives_up_after_three(client):
    for _ in range(4):
        responses.get(f"{API}/me/player/devices", status=429, headers={"Retry-After": "1"})
    with pytest.raises(TransientError):
        client.devices()
    assert client.sleeps == [1, 1, 1]


@responses.activate
def test_5xx_is_transient(client):
    responses.get(f"{API}/me/player/devices", status=503)
    with pytest.raises(TransientError):
        client.devices()


@responses.activate
def test_now_playing_204(client):
    responses.get(f"{API}/me/player/currently-playing", status=204)
    assert client.now_playing() is None


@responses.activate
def test_now_playing_idle_item_null(client):
    responses.get(
        f"{API}/me/player/currently-playing",
        json={"is_playing": False, "progress_ms": None, "item": None},
    )
    np = client.now_playing()
    assert np.track_id is None and not np.is_playing


@responses.activate
def test_now_playing_track(client):
    responses.get(
        f"{API}/me/player/currently-playing",
        json={
            "is_playing": True,
            "progress_ms": 200_000,
            "item": {"id": "t1", "duration_ms": 222_000, "name": "Song", "artists": [{"name": "A"}]},
        },
    )
    np = client.now_playing()
    assert (np.track_id, np.remaining_ms, np.name, np.artist) == ("t1", 22_000, "Song", "A")


@responses.activate
def test_queue_404_is_no_active_device(client):
    responses.post(f"{API}/me/player/queue", status=404, json={"error": {"status": 404}})
    with pytest.raises(NoActiveDevice):
        client.queue("spotify:track:x")


@responses.activate
def test_queue_and_play_requests(client):
    responses.post(f"{API}/me/player/queue", status=204)
    responses.put(f"{API}/me/player/play", status=204)
    client.queue("spotify:track:x")
    client.play("spotify:track:x")
    assert responses.calls[0].request.params == {"uri": "spotify:track:x"}
    assert json.loads(responses.calls[1].request.body) == {"uris": ["spotify:track:x"]}


@responses.activate
def test_rotated_refresh_token_is_saved(client, tmp_path):
    client.tokens.expires_at = 0
    responses.post(TOKEN_URL, json=token_resp("a2", refresh="r2"))
    responses.get(f"{API}/me/player/devices", json={"devices": []})
    client.devices()
    saved = json.loads((tmp_path / "tokens.json").read_text())
    assert saved["refresh_token"] == "r2"
    assert saved["access_token"] == "a2"
    assert saved["expires_at"] == NOW + 3600


@responses.activate
def test_refresh_without_new_token_keeps_old(client, tmp_path):
    client.tokens.expires_at = 0
    responses.post(TOKEN_URL, json=token_resp("a2"))
    responses.get(f"{API}/me/player/devices", json={"devices": []})
    client.devices()
    assert json.loads((tmp_path / "tokens.json").read_text())["refresh_token"] == "r1"


def test_token_store_reloads_external_write(tmp_path):
    import os

    p = tmp_path / "tokens.json"
    a = TokenStore(p)
    p.write_text(json.dumps({"refresh_token": "ext", "access_token": "x", "expires_at": 1}))
    st = p.stat()
    os.utime(p, (st.st_atime, st.st_mtime + 5))
    a.reload_if_changed()
    assert a.refresh_token == "ext"


def test_pkce_pair():
    import base64
    import hashlib

    from spotify_heart.spotify_client import make_pkce

    v, c = make_pkce()
    assert 43 <= len(v) <= 128
    expect = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
    assert c == expect


@responses.activate
def test_now_playing_real_fixture(client):
    data = fixture("spotify_currently_playing.json")
    responses.get(f"{API}/me/player/currently-playing", json=data)
    np = client.now_playing()
    assert np.track_id == data["item"]["id"]
    assert np.duration_ms == data["item"]["duration_ms"]
    assert np.remaining_ms == data["item"]["duration_ms"] - data["progress_ms"]
    assert np.name and np.artist
