"""Spotify Web API client: PKCE login, token refresh, search and playback."""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import logging
import secrets
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import requests

log = logging.getLogger(__name__)

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
REDIRECT_HOST = "127.0.0.1"
REDIRECT_PORT = 8888
REDIRECT_URI = f"http://{REDIRECT_HOST}:{REDIRECT_PORT}/callback"
SCOPES = "user-modify-playback-state user-read-playback-state user-read-currently-playing"
TIMEOUT_S = 10
LOGIN_TIMEOUT_S = 300
MAX_429_RETRIES = 3


class SpotifyError(Exception):
    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


class AuthError(SpotifyError):
    """Tokens missing or rejected; the user must log in again."""


class TransientError(SpotifyError):
    """Network trouble, timeout or 5xx; back off and retry later."""


class NoActiveDevice(SpotifyError):
    """404 from a player endpoint: no Spotify client is active."""


@dataclass
class NowPlaying:
    is_playing: bool
    progress_ms: int
    track_id: str | None
    duration_ms: int
    name: str
    artist: str

    @property
    def remaining_ms(self) -> int:
        return self.duration_ms - self.progress_ms


@dataclass
class Track:
    uri: str
    id: str
    name: str
    artist: str


# ---------------------------------------------------------------- tokens


class TokenStore:
    """The tokens on disk. Safe to share between the DJ and login threads."""

    def __init__(self, path: Path):
        self.lock = threading.RLock()
        self.path = path
        self.refresh_token = ""
        self.access_token = ""
        self.expires_at = 0.0
        self._mtime = 0.0
        self.reload_if_changed()

    def reload_if_changed(self) -> None:
        """Pick up tokens written by another process (e.g. a --login run)."""
        with self.lock:
            self._reload_if_changed()

    def _reload_if_changed(self) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            return
        if mtime == self._mtime:
            return
        d = json.loads(self.path.read_text(encoding="utf-8"))
        self.refresh_token = d.get("refresh_token", "")
        self.access_token = d.get("access_token", "")
        self.expires_at = float(d.get("expires_at", 0))
        self._mtime = mtime

    def update(self, resp: dict, now: float) -> None:
        with self.lock:
            self._update(resp, now)

    def _update(self, resp: dict, now: float) -> None:
        self.access_token = resp["access_token"]
        self.expires_at = now + int(resp.get("expires_in", 3600))
        # Spotify only sometimes rotates the refresh token; keep the old one otherwise.
        if resp.get("refresh_token"):
            self.refresh_token = resp["refresh_token"]
        self._save()

    def save(self) -> None:
        with self.lock:
            self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "refresh_token": self.refresh_token,
                    "access_token": self.access_token,
                    "expires_at": self.expires_at,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        tmp.replace(self.path)
        self._mtime = self.path.stat().st_mtime


# ---------------------------------------------------------------- PKCE login


def make_pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    return verifier, challenge


PAGE = """<!doctype html><meta charset="utf-8"><title>Spotify Heart</title>
<body style="font-family:system-ui;max-width:32em;margin:4em auto;text-align:center">
<h1>{title}</h1><p>{body}</p></body>"""


def _wait_for_callback(state: str, timeout_s: float) -> str:
    """Serve 127.0.0.1:8888/callback once and return the auth code."""
    result: dict[str, str] = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            u = urllib.parse.urlparse(self.path)
            if u.path != "/callback":
                self.send_error(404)
                return
            q = urllib.parse.parse_qs(u.query)
            if q.get("state", [""])[0] != state:
                result["error"] = "state mismatch"
            elif "error" in q:
                result["error"] = q["error"][0]
            else:
                result["code"] = q.get("code", [""])[0]
            ok = "code" in result
            html = PAGE.format(
                title="Logged in" if ok else "Login failed",
                body="You can close this tab and go back to Spotify Heart."
                if ok
                else f"Spotify said: {result.get('error')}. Try again from the app.",
            )
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode())
            done.set()

        def log_message(self, fmt, *args):
            log.debug("[login] " + fmt, *args)

    try:
        server = http.server.HTTPServer((REDIRECT_HOST, REDIRECT_PORT), Handler)
    except OSError as e:
        raise AuthError(f"cannot listen on {REDIRECT_URI} ({e}); is another login running?")
    t = threading.Thread(target=server.serve_forever, name="login_server", daemon=True)
    t.start()
    try:
        if not done.wait(timeout_s):
            raise AuthError("login timed out")
    finally:
        server.shutdown()
        server.server_close()
    if "code" not in result:
        raise AuthError(f"login failed: {result.get('error')}")
    return result["code"]


# ---------------------------------------------------------------- client


class SpotifyClient:
    def __init__(
        self,
        client_id: str,
        tokens: TokenStore,
        session: requests.Session | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.client_id = client_id
        self.tokens = tokens
        self.http = session or requests.Session()
        self.clock = clock
        self.sleep = sleep
        self.auth_lock = threading.RLock()

    @property
    def has_login(self) -> bool:
        self.tokens.reload_if_changed()
        return bool(self.tokens.refresh_token)

    # -- auth

    def login(self, open_browser: Callable[[str], Any] = webbrowser.open) -> None:
        if not self.client_id:
            raise AuthError("spotify_client_id is empty in the config file")
        verifier, challenge = make_pkce()
        state = secrets.token_urlsafe(16)
        url = AUTH_URL + "?" + urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": REDIRECT_URI,
                "scope": SCOPES,
                "state": state,
                "code_challenge_method": "S256",
                "code_challenge": challenge,
            }
        )
        log.info("[login] opening browser for Spotify login")
        # Start listening before the browser can redirect back.
        box: dict[str, Any] = {}

        def wait():
            try:
                box["code"] = _wait_for_callback(state, LOGIN_TIMEOUT_S)
            except AuthError as e:
                box["err"] = e

        waiter = threading.Thread(target=wait, daemon=True)
        waiter.start()
        time.sleep(0.2)
        if "err" in box:
            raise box["err"]
        open_browser(url)
        waiter.join()
        if "err" in box:
            raise box["err"]
        self._token_request(
            {
                "grant_type": "authorization_code",
                "code": box["code"],
                "redirect_uri": REDIRECT_URI,
                "client_id": self.client_id,
                "code_verifier": verifier,
            }
        )
        log.info("[login] logged in; tokens saved to %s", self.tokens.path)

    def refresh(self) -> None:
        self.tokens.reload_if_changed()
        if not self.tokens.refresh_token:
            raise AuthError("not logged in")
        self._token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": self.tokens.refresh_token,
                "client_id": self.client_id,
            }
        )
        log.info("[spotify] access token refreshed")

    def _token_request(self, data: dict) -> None:
        with self.auth_lock:
            self._token_request_locked(data)

    def _token_request_locked(self, data: dict) -> None:
        try:
            r = self.http.post(TOKEN_URL, data=data, timeout=TIMEOUT_S)
        except requests.RequestException as e:
            raise TransientError(f"token request failed: {e}")
        if r.status_code >= 500:
            raise TransientError(f"token endpoint {r.status_code}", r.status_code)
        if r.status_code != 200:
            raise AuthError(f"token request rejected: {r.status_code} {r.text[:200]}", r.status_code)
        self.tokens.update(r.json(), self.clock())

    def _ensure_token(self) -> None:
        with self.auth_lock:
            self.tokens.reload_if_changed()
            if not self.tokens.access_token or self.clock() > self.tokens.expires_at - 60:
                self.refresh()

    # -- requests

    def _request(self, method: str, path: str, **kw) -> requests.Response:
        self._ensure_token()
        refreshed = False
        retries_429 = 0
        while True:
            headers = {"Authorization": f"Bearer {self.tokens.access_token}"}
            try:
                r = self.http.request(method, API + path, headers=headers, timeout=TIMEOUT_S, **kw)
            except requests.RequestException as e:
                raise TransientError(f"{method} {path}: {e}")
            if r.status_code == 401:
                if refreshed:
                    raise AuthError(f"{method} {path}: 401 after refresh", 401)
                self.refresh()
                refreshed = True
                continue
            if r.status_code == 429:
                if retries_429 >= MAX_429_RETRIES:
                    raise TransientError(f"{method} {path}: rate limited", 429)
                wait = int(r.headers.get("Retry-After", "1") or 1)
                log.warning("[spotify] 429 on %s, sleeping %d s", path, wait)
                self.sleep(wait)
                retries_429 += 1
                continue
            if r.status_code >= 500:
                raise TransientError(f"{method} {path}: {r.status_code}", r.status_code)
            if r.status_code == 404 and path.startswith("/me/player"):
                raise NoActiveDevice(f"{method} {path}: no active device", 404)
            if r.status_code == 403:
                raise SpotifyError(
                    f"{method} {path}: 403 (Premium required, or user not allow-listed) {r.text[:200]}",
                    403,
                )
            if r.status_code >= 400:
                raise SpotifyError(f"{method} {path}: {r.status_code} {r.text[:200]}", r.status_code)
            return r

    # -- API calls

    def search_track(self, title: str, artist: str) -> Track | None:
        q = f"track:{title} artist:{artist}"
        r = self._request("GET", "/search", params={"q": q, "type": "track", "limit": 1})
        items = r.json().get("tracks", {}).get("items") or []
        if not items:
            return None
        it = items[0]
        return Track(it["uri"], it["id"], it.get("name", ""), _artists(it))

    def now_playing(self) -> NowPlaying | None:
        r = self._request("GET", "/me/player/currently-playing")
        if r.status_code == 204 or not r.content:
            return None
        d = r.json()
        item = d.get("item")
        if not item:
            return NowPlaying(bool(d.get("is_playing")), 0, None, 0, "", "")
        return NowPlaying(
            bool(d.get("is_playing")),
            int(d.get("progress_ms") or 0),
            item.get("id"),
            int(item.get("duration_ms") or 0),
            item.get("name", ""),
            _artists(item),
        )

    def queue(self, uri: str) -> None:
        self._request("POST", "/me/player/queue", params={"uri": uri})

    def play(self, uri: str, device_id: str | None = None) -> None:
        """Start a track. With device_id, wake that device and play there."""
        params = {"device_id": device_id} if device_id else None
        self._request("PUT", "/me/player/play", params=params, json={"uris": [uri]})

    def next(self) -> None:
        self._request("POST", "/me/player/next")

    def devices(self) -> list[dict]:
        r = self._request("GET", "/me/player/devices")
        return r.json().get("devices", [])


def _artists(item: dict) -> str:
    return ", ".join(a.get("name", "") for a in item.get("artists", []))
