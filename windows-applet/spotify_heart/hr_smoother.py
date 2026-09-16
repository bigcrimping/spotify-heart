"""Heart-rate smoothing: median over a time window with a lock test. Pure."""

from __future__ import annotations

import statistics
import threading
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class HrEstimate:
    locked: bool
    bpm: int
    n: int
    spread: int
    age_s: float  # seconds since the newest accepted reading (inf if none)


class HrSmoother:
    def __init__(
        self,
        window_s: float = 30,
        min_readings: int = 5,
        max_spread: float = 8,
        hr_min: float = 40,
        hr_max: float = 180,
    ):
        self.window_s = window_s
        self.min_readings = min_readings
        self.max_spread = max_spread
        self.hr_min = hr_min
        self.hr_max = hr_max
        self._buf: deque[tuple[float, float]] = deque()
        self._lock = threading.Lock()

    def push(self, bpm: float, t: float) -> bool:
        """Add a raw reading taken at time t. Returns False if rejected."""
        if not (self.hr_min <= bpm <= self.hr_max):
            return False
        with self._lock:
            self._buf.append((t, bpm))
            self._expire(t)
        return True

    def _expire(self, t: float) -> None:
        while self._buf and t - self._buf[0][0] > self.window_s:
            self._buf.popleft()

    def estimate(self, t: float) -> HrEstimate:
        with self._lock:
            self._expire(t)
            vals = [b for _, b in self._buf]
            age = t - self._buf[-1][0] if self._buf else float("inf")
        n = len(vals)
        if n == 0:
            return HrEstimate(False, 0, 0, 0, age)
        med = statistics.median(vals)
        spread = _iqr(vals)
        locked = n >= self.min_readings and spread <= self.max_spread
        return HrEstimate(locked, round(med), n, round(spread), age)


def _iqr(vals: list[float]) -> float:
    if len(vals) < 2:
        return 0.0
    q1, _, q3 = statistics.quantiles(vals, n=4, method="inclusive")
    return q3 - q1
