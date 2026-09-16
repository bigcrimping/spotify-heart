"""Emit plausible `heart_rate:` lines on stdout, like the MR60BHA2 firmware.

Random walk around 72 BPM, a wild 150 every 30th line, and a 20 s silence
(sensor not confident) every `--gap-every` seconds.

    python tools/fake_sensor.py | python -m spotify_heart --console --fake-sensor -
"""

from __future__ import annotations

import argparse
import random
import sys
import time


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--center", type=float, default=72)
    ap.add_argument("--interval", type=float, default=0.5)
    ap.add_argument("--gap-every", type=float, default=60, help="seconds between silences")
    ap.add_argument("--gap", type=float, default=20, help="silence length in seconds")
    ap.add_argument("--seed", type=int)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    hr = args.center
    n = 0
    next_gap = time.monotonic() + args.gap_every
    try:
        while True:
            if time.monotonic() >= next_gap:
                time.sleep(args.gap)
                next_gap = time.monotonic() + args.gap_every
            n += 1
            hr += rng.uniform(-1, 1) + (args.center - hr) * 0.1
            value = 150.0 if n % 30 == 0 else round(hr)
            sys.stdout.write(f"heart_rate: {value:.2f}\n")
            sys.stdout.flush()
            time.sleep(args.interval)
    except (KeyboardInterrupt, BrokenPipeError):
        pass


if __name__ == "__main__":
    main()
