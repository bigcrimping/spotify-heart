"""Hit every external endpoint once and save sanitised fixtures.

    python tools/probe_apis.py [--config PATH] [--queue]

Prints PASS/FAIL per call. Fixtures go to tests/fixtures/. `--queue` really
adds the searched track to the Spotify queue.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

from spotify_heart import config  # noqa: E402
from spotify_heart.spotify_client import SpotifyClient, SpotifyError, TokenStore  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
GETSONGBPM = "https://api.getsong.co"


def scrub(obj, path=""):
    """Replace bulky or account-specific values and trim lists before saving."""
    if isinstance(obj, dict):
        if "is_active" in obj and "type" in obj:  # a player device
            obj = {**obj, "id": "device-id", "name": "Test Device"}
        out = {}
        for k, v in obj.items():
            if k in ("available_markets",):
                out[k] = ["GB"]
            elif k in ("images",):
                out[k] = []
            elif k in ("external_urls",):
                out[k] = {"spotify": "https://open.spotify.com/x"}
            elif k == "href":
                out[k] = "https://api.spotify.com/v1/x"
            elif k == "context" and isinstance(v, dict):
                out[k] = {**scrub(v), "uri": "spotify:playlist:test-playlist"}
            elif k == "api_key":
                out[k] = "REDACTED"
            else:
                out[k] = scrub(v, f"{path}.{k}")
        return out
    if isinstance(obj, list):
        return [scrub(v, path) for v in obj[:5]]  # keep fixtures small
    return obj


def save(name: str, data, keep_if_empty: bool = True) -> None:
    """Write a fixture, but never replace a good one with an empty answer.

    The probe runs against whatever the account is doing at the time: with no
    active device, or nothing playing, the response is legitimately empty and
    would otherwise overwrite the fixture the tests rely on.
    """
    if not keep_if_empty and (FIXTURES / name).exists():
        print(f"      (kept existing {name}: this run returned nothing)")
        return
    FIXTURES.mkdir(parents=True, exist_ok=True)
    (FIXTURES / name).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def result(label: str, ok: bool, detail: str = "") -> bool:
    print(f"{'PASS' if ok else 'FAIL'}  {label}  {detail}")
    return ok


def run(cfg: config.Config, do_queue: bool) -> int:
    client = SpotifyClient(cfg.spotify_client_id, TokenStore(cfg.tokens_path))
    oks = []

    try:
        client.refresh()
        oks.append(result("spotify refresh", True))
    except SpotifyError as e:
        oks.append(result("spotify refresh", False, str(e)))
        print("Run `python -m spotify_heart --login` first.")
        return 1

    title, artist = "Mr. Brightside", "The Killers"
    if cfg.getsongbpm_key:
        try:
            r = requests.get(
                f"{GETSONGBPM}/tempo/", params={"api_key": cfg.getsongbpm_key, "bpm": 120}, timeout=10
            )
            ok = r.status_code == 200
            detail = f"HTTP {r.status_code}"
            if ok:
                data = r.json()
                save("getsongbpm_tempo_120.json", scrub(data))
                items = data.get("tempo") if isinstance(data, dict) else None
                if items:
                    first = items[0]
                    title = first.get("song_title", title)
                    artist = (first.get("artist") or {}).get("name", artist)
                    detail += f", {len(items)} songs, first: {title} - {artist}"
                else:
                    detail += f", unexpected shape: top-level keys {list(data)[:5]}"
            else:
                detail += f" {r.text[:200]}"
            oks.append(result("getsongbpm tempo 120", ok, detail))
        except requests.RequestException as e:
            oks.append(result("getsongbpm tempo 120", False, str(e)))
    else:
        print("SKIP  getsongbpm tempo 120  (getsongbpm_key is empty)")

    track = None
    try:
        r = client._request(
            "GET", "/search", params={"q": f"track:{title} artist:{artist}", "type": "track", "limit": 1}
        )
        save("spotify_search.json", scrub(r.json()))
        track = client.search_track(title, artist)
        oks.append(result("spotify search", track is not None, f"{track.uri if track else 'no match'}"))
        r = client._request(
            "GET", "/search",
            params={"q": "track:zzqxqzz nonexistent artist:nobodyxq", "type": "track", "limit": 1},
        )
        save("spotify_search_empty.json", scrub(r.json()))
    except SpotifyError as e:
        oks.append(result("spotify search", False, str(e)))

    try:
        r = client._request("GET", "/me/player/currently-playing")
        if r.status_code == 204 or not r.content:
            detail = "204 (nothing playing)"
        else:
            save("spotify_currently_playing.json", scrub(r.json()))
            np = client.now_playing()
            detail = f"{r.status_code} is_playing={np.is_playing} {np.name} - {np.artist}"
        oks.append(result("spotify now-playing", True, detail))
    except SpotifyError as e:
        oks.append(result("spotify now-playing", False, str(e)))

    try:
        r = client._request("GET", "/me/player/devices")
        data = r.json()
        save("spotify_devices.json", scrub(data), keep_if_empty=bool(data.get("devices")))
        names = [f"{d.get('name')} ({d.get('type')}{', active' if d.get('is_active') else ''})"
                 for d in data.get("devices", [])]
        oks.append(result("spotify devices", True, ", ".join(names) or "none"))
    except SpotifyError as e:
        oks.append(result("spotify devices", False, str(e)))

    if do_queue and track:
        try:
            client.queue(track.uri)
            oks.append(result("spotify queue", True, f"queued {track.name} - {track.artist}"))
        except SpotifyError as e:
            oks.append(result("spotify queue", False, str(e)))

    print(f"\n{sum(oks)}/{len(oks)} passed; fixtures in {FIXTURES}")
    return 0 if all(oks) else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config")
    ap.add_argument("--queue", action="store_true", help="really queue the searched track")
    args = ap.parse_args(argv)
    return run(config.load(args.config), args.queue)


if __name__ == "__main__":
    sys.exit(main())
