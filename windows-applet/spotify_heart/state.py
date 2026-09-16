"""state.json: the recently played ring, persisted across runs."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .selector import RecentRing

log = logging.getLogger(__name__)


def load_recent(path: Path, n: int) -> RecentRing:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return RecentRing(n, d.get("recent", []))
    except FileNotFoundError:
        return RecentRing(n)
    except (ValueError, KeyError, TypeError) as e:
        log.warning("[state] ignoring unreadable %s: %s", path, e)
        return RecentRing(n)


def save_recent(path: Path, ring: RecentRing) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"recent": ring.to_list()}, indent=2), encoding="utf-8")
    tmp.replace(path)
