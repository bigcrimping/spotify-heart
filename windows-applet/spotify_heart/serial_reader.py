"""Reads `heart_rate: NN.NN` lines from the board (or a fake source)."""

from __future__ import annotations

import logging
import re
import sys
import threading
import time
from typing import Callable, Iterable

import serial
import serial.tools.list_ports

log = logging.getLogger(__name__)

VID = 0x303A
PID = 0x1001
LINE_RE = re.compile(r"^heart_rate:\s*([0-9.]+)")
RECONNECT_S = 2.0


def parse_line(line: str) -> float | None:
    m = LINE_RE.match(line.strip())
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def find_port(fallback: str = "auto") -> str | None:
    for p in serial.tools.list_ports.comports():
        if p.vid == VID and p.pid == PID:
            return p.device
    return None if fallback in ("", "auto") else fallback


def open_port(device: str) -> serial.Serial:
    # Native USB-Serial/JTAG on the C6 can reset when DTR/RTS toggle, so
    # set both low before the port is opened.
    s = serial.Serial()
    s.baudrate = 115200
    s.timeout = 1
    s.dtr = False
    s.rts = False
    s.port = device
    s.open()
    return s


class SerialReader(threading.Thread):
    """Feeds parsed readings to `on_hr(bpm, t)`; reconnects forever."""

    def __init__(
        self,
        on_hr: Callable[[float, float], None],
        port: str = "auto",
        stop: threading.Event | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        super().__init__(name="serial_reader", daemon=True)
        self.on_hr = on_hr
        self.port_setting = port
        self.stop_event = stop or threading.Event()
        self.clock = clock
        self.device: str | None = None  # currently open port, for status
        self.connected = False
        self.last_error = ""

    def run(self) -> None:
        announced_missing = False
        while not self.stop_event.is_set():
            device = find_port(self.port_setting)
            if not device:
                if not announced_missing:
                    log.warning("[serial] board not found (VID %04X PID %04X); waiting", VID, PID)
                    announced_missing = True
                self.last_error = "board not found"
                self.stop_event.wait(RECONNECT_S)
                continue
            announced_missing = False
            try:
                s = open_port(device)
            except (serial.SerialException, OSError) as e:
                msg = str(e)
                if "PermissionError" in msg or "Access is denied" in msg:
                    msg = f"{device} is busy (is the Arduino Serial Monitor open?)"
                if msg != self.last_error:
                    log.warning("[serial] cannot open %s: %s", device, msg)
                self.last_error = msg
                self.stop_event.wait(RECONNECT_S)
                continue
            try:
                with s:
                    self.device, self.connected, self.last_error = device, True, ""
                    log.info("[serial] connected to %s", device)
                    self._read_loop(s)
            except (serial.SerialException, OSError) as e:
                # Unplugging shows up here, often as "Access is denied".
                self.last_error = f"lost {device}"
                log.debug("[serial] read error: %s", e)
            if self.connected:
                log.info("[serial] disconnected from %s", device)
            self.connected = False
            self.stop_event.wait(RECONNECT_S)

    def _read_loop(self, s: serial.Serial) -> None:
        while not self.stop_event.is_set():
            raw = s.readline()
            if not raw:
                continue
            bpm = parse_line(raw.decode(errors="ignore"))
            if bpm is not None:
                self.on_hr(bpm, self.clock())


class LineSourceReader(threading.Thread):
    """Same contract as SerialReader but reads lines from a file or stdin."""

    def __init__(
        self,
        on_hr: Callable[[float, float], None],
        source: str,
        stop: threading.Event | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        super().__init__(name="fake_sensor", daemon=True)
        self.on_hr = on_hr
        self.source = source
        self.stop_event = stop or threading.Event()
        self.clock = clock
        self.device = "stdin" if source == "-" else source
        self.connected = True
        self.last_error = ""

    def _lines(self) -> Iterable[str]:
        if self.source == "-":
            yield from sys.stdin
        else:
            with open(self.source, encoding="utf-8", errors="ignore") as f:
                yield from f

    def run(self) -> None:
        for line in self._lines():
            if self.stop_event.is_set():
                break
            bpm = parse_line(line)
            if bpm is not None:
                self.on_hr(bpm, self.clock())
        self.connected = False
        log.info("[serial] fake sensor source ended")
