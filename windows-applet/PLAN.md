# Spotify Heart, Windows applet — implementation plan

## Status

| Phase | State | Date | Notes |
|---|---|---|---|
| 0 Human prerequisites | **done** | 2026-09-16 | Spotify Client ID + GetSongBPM key go in `windows-applet\config.local.json` (git-ignored; the app looks there first, then `%APPDATA%\SpotifyHeart\config.json`; tokens, cache and logs sit beside whichever is used) |
| 1 Scaffolding, smoother, serial | **done** | 2026-09-16 | Dev venv is `.venv` on Python 3.12 (`pip install -r requirements-dev.txt; pip install -e .`). Real board sends ~1 line/s (not 2). Opening with DTR/RTS low is fine; "no output" just means no confident reading. |
| 2 Spotify client and login | **done** | 2026-09-16 | Browser login works; `probe_apis.py --queue` PASS 5/5 with phone playing (queue needs an *active* device, else 404). GetSongBPM probe PASS once the key activated (took a few minutes after sign-up). `--probe` CLI flag not wired (use tools/probe_apis.py). |
| 3 BPM lookup and selection | **done** | 2026-09-16 | `--pick 72` x10 = 10 distinct tracks in ~0.8-1.7 s; 45 and 180 both pick; repeat runs make 0 GetSongBPM requests (selector tries cached offsets first). GetSongBPM facts: see section 5. Picks skew obscure (the DB is broad); `artist.genres` exists if filtering is wanted. |
| 4 DJ loop | **in progress** | 2026-09-16 | `dj.py` written and wired into tray + `--console`; `tests/test_dj.py` (19 tests) pass, 66 total. Live so far: login -> lock (81) -> Spotify paused at lock -> after 30 s started a matched track (80 BPM) -> polling every 5 s. **Still to check live:** the near-end queue (204 ~15 s before end, next track is the pick), pause-for-2-min (no play), quit-Spotify message cadence + recovery. Design choice: the plan's "paused >30 s -> play" applies only at the first lock; later pauses never trigger play (matches the pause acceptance test). A lock stays usable for picking for 5 min after the sensor loses you. |
| 5 Tray, packaging | tray done early; packaging not started | 2026-09-16 | `tray.py` runs by default (`.venv\Scripts\pythonw.exe -m spotify_heart`); colour/tooltip now come from `dj.status`; Pause, Skip, Log in, Open app folder, About, Quit all wired. Single-instance mutex verified. Tray was stopped at end of session. Next: `packaging/build_exe.ps1`, `install_startup.ps1`. |
| 6 Hardening, docs | not started | | Log rotation (5 MB x 3) already in `log.py`. README still to write (needs the GetSongBPM backlink line). |

**Resume here (next session):** stop any running tray copy, sit in front of the sensor with Spotify playing on the phone, run `.venv\Scripts\python -m spotify_heart --console` for a few songs and tick off the Phase 4 live checks above. Then Phase 5 packaging, then Phase 6.

Open ideas raised by the owner / seen in testing (not yet decided):
- Picks skew obscure (GetSongBPM is broad). `artist.genres` is in the response; a `genres` config filter would be easy.
- `--probe` flag is not wired; use `python tools/probe_apis.py [--queue]`.
- Nothing is committed yet.

Variant B of two: the Seeed MR60BHA2 board keeps the tiny `HeartRateOnly`
firmware it already has and just prints heart rate over USB serial. A Python
tray application on the Windows PC reads it, picks a tempo-matched song, and
queues it on Spotify. The firmware lives in `../esp32-heartrate/`.

This plan doubles as the development log. Every phase has an entry
condition, concrete tasks, and an acceptance test. Do the phases in order. Do
not skip acceptance tests. Where the plan says "verify against the live API and
save a fixture", do that before writing a parser. Do not guess JSON shapes.

---

## 0. Decisions already made (do not re-open)

| Decision | Choice | Why |
|---|---|---|
| Where logic runs | Windows host, Python 3.11+, packaged as a single `.exe` tray app | Keeps the microcontroller side trivial. Trivial OAuth, ordinary debugging, no TLS on a microcontroller. |
| Firmware | `../esp32-heartrate/`, a native ESP-IDF port of the upstream `HeartRateOnly` example (originally the Arduino sketch itself) | Board prints `heart_rate: NN.NN` lines at 115200 baud. Nothing else is needed from it. |
| BPM data source | GetSongBPM API (`api.getsong.co`) | Spotify blocked its Audio Features / Recommendations endpoints for every app not already in extended mode on 27 Nov 2024. New apps get HTTP 403. There is no supported Spotify source of tempo. |
| Spotify role | Search (title + artist to track URI) and playback control only | Those endpoints still work for development-mode apps after the Feb 2026 changes. |
| When to change song | When the current track is about to end, queue the next one | Smooth listening, no interruptions. |
| Tempo match | Heart rate, or half, or double, each within +/-3 BPM | Most music is 90 to 170 BPM; a 65 BPM heart still gets a big pool at 130. |
| Auth flow | Authorization Code with PKCE, done inside the app with a loopback redirect on `127.0.0.1:8888`. Tokens stored in `%APPDATA%\SpotifyHeart\tokens.json`. | PKCE needs no client secret. Loopback HTTP redirects are the only plain-HTTP redirects Spotify permits. |
| UI | System tray icon with a small menu. No window. | It is a background service with a status glance. |
| Language / libs | Python, `pyserial`, `requests`, `pystray`, `Pillow`; `pytest` + `responses` for tests; PyInstaller for packaging | All mainstream, all pip-installable, all easy to work with. |

Hard constraints (all verified 2026-09-15):

- Spotify **Premium** is required on the account that owns the dev app. Playback
  and queue endpoints return 403 otherwise.
- The dev app is in **development mode**: max 5 allow-listed users, low rate
  limits (expect 429s if you poll aggressively). Fine for one owner.
- GetSongBPM: free key, **3,000 requests/hour**, and the terms require a
  visible **backlink to getsongbpm.com** in the project (README and the tray
  "About" entry). Do that or the key gets suspended.
- Do NOT call `/v1/audio-features`, `/v1/audio-analysis`, or `/v1/recommendations`.
- Do NOT commit tokens or keys. `.gitignore` already covers `tokens.json`,
  `config.local.json`, `.env`.

---

## 1. Proven starting point

The board is a XIAO ESP32-C6 on the MR60BHA2 module. It was flashed on
2026-09-15 with the upstream `examples/HeartRateOnly/HeartRateOnly.ino` (https://github.com/Seeed-Studio/Seeed_Arduino_mmWave)
(esp32 core 3.3.11, FQBN `esp32:esp32:XIAO_ESP32C6`). It enumerates as
`USB Serial Device`, vendor ID `0x303A`, product ID `0x1001`, and was on COM3.

Its entire serial output is lines of the form:

```
heart_rate: 90.00
heart_rate: 89.00
```

at 115200 baud, about one line every 0.5 s when a still person is roughly 40 cm
to 1.5 m away. When the sensor is not confident (subject moving, too close,
nobody there) it stops printing. There is no other output. If the firmware ever
needs reflashing, see the top-level README.

Reading it from Python is one call:

```python
import serial
with serial.Serial("COM3", 115200, timeout=1) as s:
    while True:
        line = s.readline().decode(errors="ignore").strip()
        if line.startswith("heart_rate:"):
            print(float(line.split(":")[1]))
```

Pitfall: the C6 uses native USB-Serial/JTAG. Toggling DTR and RTS together can
reset the chip. If the board reboots each time the app connects, create the
`Serial` object without a port, set `dtr = False` and `rts = False`, then set
`port` and call `open()`.

---

## 2. Target repository layout

```
spotify-heart/windows-applet/
  PLAN.md                    (this file)
  README.md                  (setup guide; MUST contain the GetSongBPM backlink)
  .gitignore
  pyproject.toml             (name spotify-heart, deps, entry point spotify-heart = spotify_heart.__main__:main)
  requirements.txt           (pinned; pyserial, requests, pystray, Pillow)
  requirements-dev.txt       (pytest, responses, pyinstaller)
  spotify_heart/
    __init__.py              (__version__)
    __main__.py              (CLI parsing, single-instance lock, starts threads, tray)
    config.py                (paths under %APPDATA%\SpotifyHeart, load/save config.json, tunables with defaults)
    log.py                   (rotating file log + console)
    serial_reader.py         (port autodetect, reader thread, reconnect)
    hr_smoother.py           (median window, lock logic; pure, no I/O)
    spotify_client.py        (PKCE login, refresh, search, now_playing, queue, play, devices)
    bpm_source.py            (GetSongBPM tempo lookup + on-disk cache)
    selector.py              (candidate tempos, random pick, no-repeat; pure)
    dj.py                    (state machine thread)
    tray.py                  (pystray icon, menu, status text)
  tests/
    test_hr_smoother.py
    test_selector.py
    test_spotify_client.py   (responses library, uses fixtures)
    test_bpm_source.py
    test_dj.py               (fake clock, fake clients)
    fixtures/                (real sanitised JSON from tools/probe_apis.py)
  tools/
    probe_apis.py            (hits every endpoint from the PC, saves fixtures)
    fake_sensor.py           (prints heart_rate lines to stdout or a com0com port for testing without hardware)
  packaging/
    build_exe.ps1            (pyinstaller --onefile --noconsole ...)
    install_startup.ps1      (creates a shortcut in shell:startup)
```

Everything in `spotify_heart/` that is marked "pure" must have no I/O and no
threads, so it can be unit-tested directly.

---

## 3. Runtime architecture

Three threads plus the tray's own loop, communicating through thread-safe
objects. No async.

```
 serial_reader thread          dj thread                       tray (main thread)
 +----------------------+      +---------------------------+   +--------------------+
 | find port by VID/PID |      | state machine:            |   | pystray icon       |
 | readline loop        | HR   |  LOGIN -> WAIT_HR_LOCK -> |   | reads a Status     |
 | parse heart_rate     |----->|  PLAYING                  |-->| snapshot every 2 s |
 | -> HrSmoother.push() |      |  poll now-playing 5 s     |   | menu: pause, skip, |
 | reconnect on error   |      |  near end: select + queue |   | re-login, quit     |
 +----------------------+      +---------------------------+   +--------------------+
```

- `HrSmoother` is guarded by a lock; `push(bpm, t)` and `estimate(t) -> HrEstimate`.
- `Status` is a dataclass snapshot (state, hr estimate, port, last track,
  last error, paused flag) replaced atomically by the dj thread.
- `paused` and `skip_requested` are `threading.Event`s set by the tray.
- Single instance: a named mutex via `ctypes.windll.kernel32.CreateMutexW`
  (`Global\\SpotifyHeart`). Second launch shows a tray balloon and exits.

### Heart-rate smoothing (hr_smoother.py, pure)

- Accept a raw reading only if `40 <= hr <= 180`.
- Keep the last `hr_window_s` seconds of accepted readings (deque of
  `(timestamp, bpm)`).
- Estimate = **median** of readings in the window. Median, not mean, because
  the sensor occasionally emits a wild value.
- Locked only when the window holds at least `hr_min_readings` readings **and**
  the interquartile range is `<= hr_max_spread` BPM.
- `HrEstimate(locked: bool, bpm: int, n: int, spread: int, age_s: float)`.
- Takes time as an argument everywhere so tests can use a fake clock.

### Song selection (selector.py pure, bpm_source.py + spotify_client.py I/O)

Given a locked heart rate `H`:

1. Candidate tempos `{H, 2H, H/2}` filtered to `40..220`, shuffled.
2. For each candidate `T` (stop at first success, max 6 GetSongBPM calls per
   selection):
   - Random offset `d` in `-bpm_tol..+bpm_tol`, query GetSongBPM for
     `bpm = T + d`. Empty list: try the next `d` (up to 3 per `T`).
   - Shuffle; take up to 5 entries whose (title, artist) is not in the
     recently-played ring (last `no_repeat_n` Spotify track IDs, persisted in
     `state.json`).
   - Spotify search `q=track:<title> artist:<artist>`, `type=track`,
     `limit=1`. First hit wins: track URI + ID.
3. Return `Pick(uri, id, title, artist, tempo, multiplier)`, or `None` after
   the budget is spent (log it, back off 60 s, try again with a fresh estimate).

`bpm_source.py` caches each `bpm -> list` response on disk for 7 days in
`%APPDATA%\SpotifyHeart\cache\bpm_<N>.json`, so a long session hits GetSongBPM
a handful of times, not hundreds.

### Track-end trigger (dj.py)

- Poll `GET /v1/me/player/currently-playing` every `poll_s` seconds while
  PLAYING. Read `is_playing`, `progress_ms`, `item.id`, `item.duration_ms`.
- `remaining = duration_ms - progress_ms`. When `remaining <= queue_lead_ms`
  and no pick has been queued for this `item.id`, select and
  `POST /v1/me/player/queue?uri=<uri>`. Remember `item.id` as "queued for".
- If nothing is playing when the heart rate first locks (200 with
  `item: null`, 204, or `is_playing: false` for more than 30 s), do a one-off
  `PUT /v1/me/player/play` with `{"uris":["<uri>"]}`. On 404 "No active
  device", set status text `Open Spotify on a device and press play once` and
  retry every 30 s. Do not loop faster.
- Never call `play` to interrupt a playing track, except when the tray "Skip"
  is pressed: then select and `POST /v1/me/player/next` after queueing.
- `paused` set: keep polling for status display but never queue or play.

### State machine (dj.py)

```
START -> LOGIN (no tokens: open browser PKCE flow; tokens: refresh)
      -> WAIT_HR_LOCK -> PLAYING
  network error: exponential backoff 5 s, 10, 20, ... up to 5 min, stay in
  the same state, keep the tray icon amber
  refresh token rejected: back to LOGIN, tray icon red, menu offers "Log in"
```

Log every transition as `[dj] STATE_A -> STATE_B (reason)`.

---

## 4. Config and tunables

`%APPDATA%\SpotifyHeart\config.json` (created with defaults on first run;
`--config <path>` overrides):

```json
{
  "spotify_client_id": "",
  "getsongbpm_key": "",
  "serial_port": "auto",
  "hr_window_s": 30,
  "hr_min_readings": 5,
  "hr_max_spread": 8,
  "hr_min": 40,
  "hr_max": 180,
  "bpm_tol": 3,
  "allow_half_double": true,
  "poll_s": 5,
  "queue_lead_ms": 15000,
  "no_repeat_n": 20,
  "dry_run": false
}
```

`tokens.json` (same folder, never committed): `refresh_token`, `access_token`,
`expires_at` (epoch seconds). `state.json`: recently played IDs.

CLI (`python -m spotify_heart ...` or `spotify-heart.exe ...`):

| Flag | Effect |
|---|---|
| `--login` | Run the PKCE login and exit. |
| `--probe` | Same as `tools/probe_apis.py` but using the app's own config. |
| `--pick N` | Select one track for heart rate N, print it, exit. Never queues. |
| `--dry-run` | Full run but never calls queue or play. |
| `--port COMn` | Override autodetect. |
| `--console` | No tray; log to stdout; Ctrl-C quits. Use this during development. |
| `--fake-sensor FILE` | Read heart_rate lines from a file or `-` for stdin instead of serial. |

---

## 5. External API reference (verified 2026-09-15)

### GetSongBPM

- Base `https://api.getsong.co` (changed from `api.getsongbpm.com`; per the getsongbpm.com/api page, 2026-09-16). Auth is the query param `api_key` or an `X-API-KEY` header. Sign-up form asks for Website URL / App ID, **Backlink URL** (mandatory) and email; the key is activated by email.
- `GET /tempo/?api_key=KEY&bpm=120` returns songs at that tempo. Response has a
  top-level array `tempo`; each item has `song_title`, `tempo`, and nested
  `artist` (with `name`) and `album` objects. **Verify the exact shape with
  `tools/probe_apis.py` and save `tests/fixtures/getsongbpm_tempo_120.json`
  before writing the parser.** The docs page returns 403 to scripted fetches,
  so the fixture is the source of truth.
- Limit 3,000 requests per hour. Attribution backlink required.
- Observed 2026-09-16 (fixtures `getsongbpm_tempo_120.json`, `getsongbpm_tempo_out_of_range.json`):
  `tempo` is a **string**; each call returns up to 250 songs at exactly that
  tempo, the **same list in the same order** every time (so shuffle locally,
  cache freely); valid range is 40 to about 249; out of range returns 200 with
  `{"tempo": {"error": "Tempo not in allowed range"}}`; a bad or not-yet-active
  key returns 401 `{"error":"Invalid API Key, or inactive."}`; `artist` also
  carries `genres` and `from`. The old host `api.getsongbpm.com` now serves a
  bot-check page.

### Spotify accounts (auth)

- Authorize: `https://accounts.spotify.com/authorize` with `response_type=code`,
  `client_id`, `redirect_uri`, `scope`, `state`, `code_challenge_method=S256`,
  `code_challenge`.
- Token: `POST https://accounts.spotify.com/api/token`,
  `Content-Type: application/x-www-form-urlencoded`.
  - Initial: `grant_type=authorization_code&code=..&redirect_uri=..&client_id=..&code_verifier=..`
  - Refresh: `grant_type=refresh_token&refresh_token=..&client_id=..`
  - Response: `access_token`, `token_type`, `expires_in` (3600), `scope`, and
    **sometimes** a new `refresh_token`. If present, persist and use it; if
    absent, keep the old one.
- Scopes: `user-modify-playback-state user-read-playback-state user-read-currently-playing`.
- Redirect URI to register on the dashboard: `http://127.0.0.1:8888/callback`.
  `localhost` is rejected.
- Login flow inside the app: generate a 64-byte verifier, SHA-256 + base64url
  challenge, start `http.server` on `127.0.0.1:8888` in a thread, open the
  browser with `webbrowser.open`, wait up to 5 minutes for `/callback`, check
  `state`, exchange the code, save tokens, stop the server.

### Spotify Web API (all need `Authorization: Bearer <access_token>`)

| Purpose | Request | Notes |
|---|---|---|
| Search | `GET https://api.spotify.com/v1/search?q=track:<t>%20artist:<a>&type=track&limit=1` | Use `params=` so `requests` encodes it. Result at `tracks.items[0].uri` / `.id`. Empty `items` = no match. |
| Now playing | `GET https://api.spotify.com/v1/me/player/currently-playing` | 200 with `is_playing`, `progress_ms`, `item.id`, `item.duration_ms`, `item.name`; `item` is null when idle. May also be 204 with an empty body. Handle both. |
| Queue | `POST https://api.spotify.com/v1/me/player/queue?uri=spotify:track:<id>` | Empty body. 204 = ok. 404 = no active device. Premium only. |
| Start playback | `PUT https://api.spotify.com/v1/me/player/play` body `{"uris":["spotify:track:<id>"]}` | 204 = ok. 404 = no active device. Premium only. |
| Next | `POST https://api.spotify.com/v1/me/player/next` | Only for the tray "Skip" action. |
| Devices | `GET https://api.spotify.com/v1/me/player/devices` | Shown in the tray tooltip when queue returns 404. |

Common error handling, implemented once in `spotify_client._request()`:

- 401: refresh once, retry once. Second 401: raise `AuthError`, dj goes to LOGIN.
- 429: read `Retry-After` (seconds), sleep, retry (max 3).
- 5xx, connection error, timeout (10 s): raise `TransientError`, dj backs off.

---

## 6. Phases

### Phase 0 — Human prerequisites (done by hand)

1. Spotify Premium on the account you will log in with.
2. Create an app at developer.spotify.com Dashboard. Note the **Client ID**.
   Add redirect URI `http://127.0.0.1:8888/callback`. Tick "Web API".
3. Get a GetSongBPM API key at getsongbpm.com/api.
4. Python 3.11 or newer installed on the PC (`py -3 --version`).
5. Make sure a Spotify player (desktop or phone) is open and has played
   something recently on the account, so there is an "active device".

### Phase 1 — Scaffolding, smoother, serial reader (no network)

Tasks:

- Create the layout in section 2 with `pyproject.toml`, `requirements*.txt`,
  `.gitignore`, `log.py`, `config.py`.
- Implement `hr_smoother.py` (pure) and `tests/test_hr_smoother.py` covering:
  rejects out-of-range, median beats an outlier, not locked under
  `hr_min_readings`, not locked when spread too wide, unlocks after the window
  empties.
- Implement `serial_reader.py`: autodetect by VID `0x303A` PID `0x1001` via
  `serial.tools.list_ports.comports()`, fall back to `serial_port` from config,
  reconnect loop with 2 s sleep on any `SerialException`, parse with regex
  `^heart_rate:\s*([0-9.]+)`.
- `tools/fake_sensor.py`: emits plausible lines (random walk around 72, with a
  wild 150 every 30th line, and 20 s gaps) to stdout.
- `--console --fake-sensor -` mode in `__main__.py` printing the estimate once
  per second.

Acceptance:

- `pytest` passes.
- `python tools/fake_sensor.py | python -m spotify_heart --console --fake-sensor -`
  shows `[hr] locked bpm=7x n=.. spread=..` within 30 s and `[hr] unlocked`
  during the gaps.
- With the real board plugged in, `python -m spotify_heart --console` finds
  the port without `--port`, prints locked readings while you sit still, and
  survives unplugging and replugging the USB cable.

### Phase 2 — Spotify client and login

Tasks:

- `spotify_client.py` with PKCE login, refresh, and the six calls in section 5,
  plus `_request()` error handling.
- `tools/probe_apis.py`: refresh, GetSongBPM tempo 120, search first result,
  now-playing, devices. Saves sanitised fixtures under `tests/fixtures/`.
  Flag `--queue` actually queues.
- `tests/test_spotify_client.py` using the `responses` library and the
  fixtures: 401-then-refresh path, 429 with Retry-After, 204 now-playing,
  rotated refresh token is saved.

Acceptance:

- `python -m spotify_heart --login` opens the browser, and afterwards
  `tokens.json` exists with a refresh token.
- `python tools/probe_apis.py` prints PASS for all five calls and the fixture
  files exist. `--queue` makes a track appear in the Spotify client's queue.
- `pytest` passes.

### Phase 3 — BPM lookup and selection (dry run)

Tasks:

- `bpm_source.py` with parser written against the real fixture, plus the
  7-day disk cache.
- `selector.py` (pure) implementing section 3 with an injectable random source
  and injectable search function so tests are deterministic.
- `--pick N` CLI flag.
- Tests: half/double at 45 and 180, tolerance offsets, no-repeat ring,
  returns None when budget exhausted, strips "(Remastered ...)" from titles.

Acceptance:

- `python -m spotify_heart --pick 72` prints something like
  `hr=72 -> tempo 144 (x2) -> "Song" by "Artist" (144 BPM) -> spotify:track:...`
  within 5 s, and ten runs give at least 8 distinct tracks.
- `--pick 45` and `--pick 180` both find something.
- Second run of `--pick 72` makes no GetSongBPM request (cache hit visible in
  the log).
- `pytest` passes.

### Phase 4 — DJ loop and queueing

Tasks:

- `dj.py` state machine per section 3, with a fake clock and fake clients in
  `tests/test_dj.py` covering: queues exactly once per track near its end,
  starts playback when idle, respects `paused`, backs off on
  `TransientError`, returns to LOGIN on `AuthError`, 404 no-device message
  cadence of 30 s.
- `--dry-run` and `--console` end-to-end.

Acceptance:

- With Spotify playing and you sitting still, `python -m spotify_heart --console`
  logs a poll every 5 s and about 15 s before the track ends logs a 204 from
  queue. The next track played is the logged one and its tempo (per
  GetSongBPM) is within +/-3 of the heart rate or its half or double.
- Pause Spotify for 2 minutes: no `play` calls, idle state logged.
- Quit Spotify entirely: the "open Spotify" message appears no more often than
  every 30 s and things recover within a minute of pressing play again.
- `pytest` passes.

### Phase 5 — Tray, startup, packaging

Tasks:

- `tray.py`: pystray icon drawn with Pillow (grey = no sensor, amber =
  connecting or backoff, green = locked and playing, red = needs login).
  Tooltip shows `HR 72 | queued: Song - Artist (144 BPM)`. Menu: Pause /
  Resume, Skip, Log in to Spotify, Open log folder, About (with the
  GetSongBPM backlink), Quit.
- Single-instance mutex.
- `packaging/build_exe.ps1`: `pyinstaller --onefile --noconsole --name spotify-heart`.
- `packaging/install_startup.ps1`: creates `spotify-heart.lnk` in
  `shell:startup` pointing at the built exe.

Acceptance:

- Double-clicking `dist\spotify-heart.exe` shows the tray icon, no console
  window, and the icon colour follows the state described above.
- A second launch does not start a second copy.
- After `install_startup.ps1` and a sign-out/sign-in, the icon appears on its
  own and reconnects to the board.

### Phase 6 — Hardening and docs

- Sleep/wake: after the PC resumes, the COM port reappears with a new handle;
  confirm reconnect works and tokens refresh. Test by sleeping the PC for 5 min.
- Log rotation at 5 MB x 3 files.
- Run for 8 hours; memory (Task Manager private bytes) must not trend up.
- `README.md`: what it does, phase-0 steps, install, first login, tray menu,
  config keys, troubleshooting table (404 no device, 403 Premium, 429, board
  not found, board resets on connect), and the mandatory line
  `Tempo data provided by [GetSongBPM](https://getsongbpm.com)`.
- Add a "Status" section at the top of this PLAN.md listing which phases are
  done and the date.

---

## 7. Known pitfalls and pre-answered questions

- **"Spotify says BPM isn't available."** Correct for new apps. That is why
  GetSongBPM is used. Don't try `audio-features`; it will 403.
- **Board resets when the app connects.** Native USB-Serial/JTAG on the C6
  reacts to DTR/RTS. Set both false before opening the port (section 1).
- **Port number changes.** Autodetect by VID/PID, never hard-code COM3.
- **Only one program can hold the COM port.** If the Arduino IDE Serial
  Monitor is open, the app will fail to open the port. Log a clear message.
- **Titles with punctuation.** Pass the search query through `requests`'
  `params=`. Strip parenthesised suffixes like "(Remastered 2011)" before
  searching.
- **Half/double at the extremes.** 45 BPM gives {45, 90}; 180 gives {180, 90}.
- **Refresh token rotation.** Spotify may return a new refresh token on
  refresh. Always check and persist.
- **Dev-mode allowlist.** Only the app owner (and up to 4 added users) can
  authorise. Log in with the owner account.
- **Firewall prompt.** The loopback login server on port 8888 may trigger a
  Windows Firewall prompt the first time. Allowing it on private networks is
  fine; it only listens on 127.0.0.1.
- **PyInstaller and pystray.** Use `--collect-all pystray` if the icon fails to
  appear in the packaged build.

---

## 8. Dependencies

`requirements.txt` (pin the current versions when you create it):

```
pyserial
requests
pystray
Pillow
```

`requirements-dev.txt`:

```
pytest
responses
pyinstaller
```

No Arduino tooling is needed for this variant unless the board has to be
reflashed; see the top-level README.

---

## Sources checked on 2026-09-15

- Spotify Web API changes, Feb 2026: https://github.com/ramsayleung/rspotify/issues/550
- What still works for new dev-mode apps: https://developers.brizm.dev/blog/spotify-api-changes-2026/
- Spotify PKCE tutorial: https://developer.spotify.com/documentation/web-api/tutorials/code-pkce-flow
- Spotify redirect URI rules: https://developer.spotify.com/documentation/web-api/concepts/redirect_uri
- Spotify quota modes: https://developer.spotify.com/documentation/web-api/concepts/quota-modes
- Spotify start playback: https://developer.spotify.com/documentation/web-api/reference/start-a-users-playback
- Spotify add to queue: https://developer.spotify.com/documentation/web-api/reference/add-to-queue
- Spotify currently playing: https://developer.spotify.com/documentation/web-api/reference/get-the-users-currently-playing-track
- Spotify search: https://developer.spotify.com/documentation/web-api/reference/search
- GetSongBPM API: https://getsongbpm.com/api (and usage in https://github.com/iandioch/songchallenge/blob/master/get_track_bpms.py)
