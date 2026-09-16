"""Config file (config.local.json beside the app, else %APPDATA%\\SpotifyHeart) and tunables."""

from __future__ import annotations

import json
import os
import sys
import logging
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

log = logging.getLogger(__name__)


def app_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "SpotifyHeart"


@dataclass
class Config:
    spotify_client_id: str = ""
    getsongbpm_key: str = ""
    serial_port: str = "auto"
    hr_window_s: float = 30.0
    hr_min_readings: int = 5
    hr_max_spread: float = 8.0
    hr_min: float = 40.0
    hr_max: float = 180.0
    hr_stale_s: float = 300.0  # how long a lock is still usable after the last reading
    bpm_tol: int = 3
    allow_half_double: bool = True
    tempo_mode: str = "auto"  # auto, same, double or half
    genres: list[str] = field(default_factory=list)  # empty = any genre
    poll_s: float = 5.0
    queue_lead_ms: int = 15000
    no_repeat_n: int = 20
    dry_run: bool = False

    # Not stored in config.json; set by load().
    path: Path | None = None

    @property
    def dir(self) -> Path:
        return self.path.parent if self.path else app_dir()

    @property
    def tokens_path(self) -> Path:
        return self.dir / "tokens.json"

    @property
    def state_path(self) -> Path:
        return self.dir / "state.json"

    @property
    def cache_dir(self) -> Path:
        return self.dir / "cache"

    @property
    def log_dir(self) -> Path:
        return self.dir / "logs"

    def to_json(self) -> dict:
        d = asdict(self)
        d.pop("path")
        return d

    def save(self) -> None:
        assert self.path is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.to_json(), indent=2) + "\n", encoding="utf-8")


LOCAL_NAME = "config.local.json"


def local_dir() -> Path:
    """Project folder in a source checkout, or the exe's folder when frozen."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def default_path() -> Path:
    """config.local.json beside the project/exe if present, else %APPDATA%.

    In a source checkout the local file is always used (and created), so
    development never touches %APPDATA%.
    """
    local = local_dir() / LOCAL_NAME
    if local.exists() or not getattr(sys, "frozen", False):
        return local
    return app_dir() / "config.json"


def coerce(key: str, value, default):
    """Convert a JSON value to the type of the field's default.

    A hand-edited config easily ends up with "5" where 5 was meant; without
    this the mistake surfaces much later as a confusing TypeError.
    """
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            return value.strip().lower() == "true"
        if isinstance(value, (int, float)):
            return bool(value)
        raise ValueError(f"expected true or false, got {value!r}")
    if isinstance(default, list):
        if isinstance(value, str):
            value = [v for v in (part.strip() for part in value.split(",")) if v]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError(f"expected a list of strings, got {value!r}")
        return value
    if isinstance(default, (int, float)) and not isinstance(value, bool):
        try:
            return type(default)(value)
        except (TypeError, ValueError):
            raise ValueError(f"expected a number, got {value!r}")
    if isinstance(default, str):
        if not isinstance(value, str):
            raise ValueError(f"expected text, got {value!r}")
        return value
    return value


def load(path: str | Path | None = None) -> Config:
    """Load the config file, creating it with defaults if missing.

    tokens.json, state.json, cache/ and logs/ live in the same folder.
    Unknown keys are ignored; a value of the wrong type falls back to the
    default with a warning; missing keys take defaults and are written back
    so the file always lists every tunable.
    """
    p = Path(path) if path else default_path()
    defaults = {f.name: getattr(Config(), f.name) for f in fields(Config) if f.name != "path"}
    known = set(defaults)
    data = {}
    if p.exists():
        data = json.loads(p.read_text(encoding="utf-8"))
    values = {}
    for k, v in data.items():
        if k not in known:
            continue
        try:
            values[k] = coerce(k, v, defaults[k])
        except ValueError as e:
            log.warning("[config] %s: %s; using the default %r", k, e, defaults[k])
    from .selector import MODES

    if values.get("tempo_mode") not in (None, *MODES):
        log.warning("[config] tempo_mode: %r is not one of %s; using auto",
                    values["tempo_mode"], ", ".join(sorted(MODES)))
        values.pop("tempo_mode")
    cfg = Config(**values)
    cfg.path = p
    if not p.exists() or set(data) < known:
        cfg.save()
    return cfg
