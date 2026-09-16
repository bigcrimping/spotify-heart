# esp32-heartrate — native ESP-IDF port of the HeartRateOnly firmware

## Status

| Phase | State | Date | Notes |
|---|---|---|---|
| 0 Decisions, prerequisites | plan written | 2026-09-17 | Nothing built yet. Board still runs the Arduino `HeartRateOnly` sketch. |
| 1 Host-tested frame parser | code written, tests not yet run | 2026-09-17 | `main/mmwave_frame.{h,c}`, `main/mr60bha2.{h,c}`, `test/test_frame.c`, `test/run_tests.ps1` exist. Not yet executed: no host C compiler was available on the development machine. Run `test\run_tests.ps1 -Fixtures` on a machine with TinyCC. |
| 2 ESP-IDF project skeleton (build only) | **done** | 2026-09-17 | `idf.py set-target esp32c6 && idf.py build` passes with zero warnings from `main/`. App binary 0x27FB0 bytes (160 KB, 84% of the 1 MB app partition free). Generated `sdkconfig` confirmed: `CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y`, `CONFIG_ESP_CONSOLE_SECONDARY_NONE=y`, `CONFIG_NEWLIB_NANO_FORMAT` unset, 4 MB flash. Owner chose to skip the Phase 1 test run (no host compiler reachable, savannah download timed out); run `test\run_tests.ps1` once TinyCC is back. Nothing flashed yet. |
| 3 Flash and bench verification | **done** | 2026-09-17 | Flashed on COM3 with `idf.py -p COM3 flash`, no manual BOOT needed; flash_id confirmed 4 MB. First two flashes boot-looped on an interrupt WDT (IDF 5.2.7 UART1 clock bug, see section 6); fixed with the manual clock enable in `radar_task.c`. With a person 50 cm away: `heart_rate:` 77 to 104 BPM about once per second, `0.00` lines in between when the radar is not confident. Applet `--console --dry-run --port COM3` locked at 79 BPM 4 s after start and picked a track. Fixtures captured with the dump build and decoded independently in Python: `capture_nobody_present.txt` (1991 frames) and `capture_still_subject.txt` (2160 frames), 0 checksum errors in both. Normal build reflashed and re-checked afterwards. |
| 4 Hardening | not started | | |
| 5 Documentation and hand-over | not started | | |

---

## What this is

The Windows tray app in `../windows-applet/` reads lines of the form
`heart_rate: 72.00` from the Seeed MR60BHA2 board (XIAO ESP32-C6 plus a
60 GHz radar) over USB serial. Today those lines come from the Arduino sketch
the `examples/HeartRateOnly` sketch of the upstream
[Seeed Arduino mmWave library](https://github.com/Seeed-Studio/Seeed_Arduino_mmWave) built with the Arduino IDE.

This plan replaces that sketch with a **plain C ESP-IDF application** that
produces byte-identical output, so the Windows app needs no changes. The
Arduino library is not compiled; the parts of it that matter (about 120 lines
of framing and checksum logic) are re-implemented in C.

This plan doubles as the development log. Every phase has an entry condition, tasks,
and an acceptance test. Do the phases in order. Do not skip acceptance tests.

---

## 0. Decisions already made

| Decision | Choice | Why |
|---|---|---|
| Framework | ESP-IDF **v5.2.7**, pure C, no Arduino | It is what is installed (`C:\Espressif\frameworks\esp-idf-v5.2.7`) and it supports the ESP32-C6. Arduino-as-an-IDF-component was rejected: the core that built the current sketch (arduino-esp32 3.3.11) targets IDF 5.5, not 5.2.7. |
| Output contract | Exactly `heart_rate: %.2f\n`, 115200 baud, over native USB-Serial/JTAG | This is what `windows-applet/spotify_heart/serial_reader.py` matches (`^heart_rate:\s*([0-9.]+)`). Any other line is ignored by the app, so diagnostics are allowed but must never start with `heart_rate:`. |
| USB identity | Native USB-Serial/JTAG, VID `0x303A` PID `0x1001` | Same enumeration as the Arduino build, so the app's port autodetect keeps working unchanged. Do not add TinyUSB. |
| Radar UART | `UART_NUM_1` on **GPIO16 (TX) / GPIO17 (RX)**, 115200 8N1 | The Arduino sketch used `HardwareSerial(0)`, which on the C6 defaults to GPIO16/17 (XIAO D6/D7). Using UART1 with explicit pins keeps the radar link independent of the IDF console and of ROM download mode. The ESP only listens; nothing is ever sent to the radar in this variant. |
| IDF console | `CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y`, log level WARN | Console and heart-rate lines share the USB port. Logs are off by default so the stream stays clean. |
| Parser scope | Generic frame parser plus decoders for the types the MR60BHA2 emits; only `0x0A15` (heart rate) is printed by default | Cheap to keep the other types decoded (breath, distance, presence) behind a Kconfig switch for later, but the default output must match the Arduino build exactly. |
| Reset pin | None | `HeartRateOnly` calls `begin()` with `rst = -1`. The radar streams unsolicited from power-up. |
| Project location | `spotify-heart/esp32-heartrate/` (this folder) | Sibling of `windows-applet/`, the app that consumes its output. |
| Rollback | Reflash the upstream `HeartRateOnly` Arduino example (esp32 core 3.x, board `XIAO_ESP32C6`) | Known-good; it produces the same output. No need to archive binaries. |

Assumptions to confirm at Phase 2 (change the sdkconfig if wrong):

- XIAO ESP32-C6 flash is 4 MB (`python -m esptool -p COMx flash_id` prints it).
- `%f` in `printf` works, i.e. `CONFIG_NEWLIB_NANO_FORMAT` is **not** set.
  With nano format, `%.2f` prints nothing and the app would see `heart_rate: `.

The upstream library is not vendored; it is referenced by URL (https://github.com/Seeed-Studio/Seeed_Arduino_mmWave)
and the protocol facts taken from it are written down in section 1.

---

## 1. Protocol reference (extracted from the Arduino library)

Source of truth: the upstream library's `src/SeeedmmWave.cpp` (framing) and
`src/SEEED_MR60BHA2.cpp` (payload decoding), v1.0.0 at https://github.com/Seeed-Studio/Seeed_Arduino_mmWave. Everything below was read from
that code, not guessed.

### Frame layout (radar to ESP, 115200 8N1)

| Offset | Size | Field | Notes |
|---|---|---|---|
| 0 | 1 | SOF | Always `0x01` |
| 1 | 2 | ID | Big-endian sequence number. Ignore on receive. |
| 3 | 2 | LEN | Big-endian payload length. Library drops the frame if LEN > 512. |
| 5 | 2 | TYPE | Big-endian message type (table below) |
| 7 | 1 | HEAD_CKSUM | `~(b[0] ^ b[1] ^ ... ^ b[6])` |
| 8 | LEN | DATA | Multi-byte values inside are **little-endian** (native C6 layout; the library uses `reinterpret_cast`) |
| 8+LEN | 1 | DATA_CKSUM | `~(XOR of all DATA bytes)`; for LEN = 0 this is `0xFF` |

Total frame length = 9 + LEN. Checksum: XOR all bytes, then bitwise NOT.

Receive algorithm the library uses (replicate it, it is proven on this board):

1. Hunt for `0x01`. Start a frame buffer with it.
2. Accumulate bytes. Once 8 header bytes are in, read LEN. If LEN > 512, abandon
   the frame and go back to hunting.
3. When the buffer reaches 9 + LEN bytes the frame is complete. Verify both
   checksums; on failure discard the frame. On success dispatch by TYPE.
4. Go back to hunting.

Improvement allowed in the port: after a checksum failure, re-scan the
discarded bytes from offset 1 for another `0x01` instead of throwing them all
away. Count resyncs and checksum failures in diagnostics counters.

### Message types (`enum TypeHeartBreath` in `SEEED_MR60BHA2.h`)

| TYPE | Name | Payload | Printed by default? |
|---|---|---|---|
| `0x0A15` | Heart rate | `float32` BPM | **yes**: `heart_rate: %.2f` |
| `0x0A14` | Breath rate | `float32` breaths/min | no (Kconfig `HR_PRINT_EXTRAS`) |
| `0x0A16` | Distance | `uint32 flag` then `float32 range`; range only valid when flag != 0 | no |
| `0x0A13` | Phases | 3 × `float32` (total, breath, heart) | no |
| `0x0F09` | Human presence | `uint8` (0 none, 1 someone) | no |
| `0x0A08` | 3D point cloud | `uint32 n` then n × {`float x`, `float y`, `int32 dop`, `int32 cluster`} | no, ignore |
| `0x0A04` | Target info | same layout as `0x0A08` | no, ignore |
| `0xFFFF` | Firmware info | `uint32` (bytes: project, major, sub, modified) | no, keep silent |
| `0x0A17` | unknown, not in the Arduino library | 8 bytes, all zero with nobody present | no, ignore |
| `0x0B06` | unknown, not in the Arduino library | 2 bytes, alternates `55 00` / `AA 00` at ~30 Hz (looks like a heartbeat/keepalive) | no, ignore |
| `0x0100` | ASCII log line from the radar MCU | e.g. `breath rate = 16.000000 `; only seen with a person present | no, ignore |

Observed on hardware 2026-09-17 (fixtures in `test/fixtures/`, decoded
independently in Python):

- **Nobody present** (`capture_nobody_present.txt`): `0x0A13`, `0x0A14`,
  `0x0A15`, `0x0A16`, `0x0A17` and `0x0F09` every 100 ms, all zero (heart
  rate `0.00`, presence `0`), plus `0x0B06` three times per cycle. So the
  firmware prints `heart_rate: 0.00` at 10 Hz when idle. The Windows app's
  smoother rejects anything outside 40 to 180 BPM, so those lines are harmless.
- **Person 50 cm away** (`capture_still_subject.txt`): heart-rate frames drop
  to one every ~0.48 s with real values (85 to 104 BPM in that run, some
  `0.00` in between), presence `1`, distance flag `1` with range ~80 cm,
  target info with one target, and `0x0100` text lines appear. This matches
  the "about one line every 0.5 s" note in `../windows-applet/PLAN.md`.

The library's `getHeartRate()` is edge-triggered (one true per frame), so the
port prints exactly one line per received `0x0A15`.

---

## 2. Target layout

```
esp32-heartrate/
  PLAN.md                     (this file)
  README.md                   (Phase 5)
  CMakeLists.txt              include($ENV{IDF_PATH}/tools/cmake/project.cmake); project(spotify_heart_hr)
  sdkconfig.defaults
  version.txt                 e.g. 0.1.0  (IDF picks it up as the app version)
  main/
    CMakeLists.txt            idf_component_register(SRCS "main.c" "radar_task.c" "mmwave_frame.c" "mr60bha2.c" ...)
    main.c                    app_main: print one info line, start radar task
    app_config.h              pins, UART number, baud, buffer sizes
    mmwave_frame.h/.c         PURE C99, no IDF includes: byte-at-a-time frame parser + checksum
    mr60bha2.h/.c             PURE C99: decode payloads for the types in section 1
    radar_task.h/.c           IDF: UART1 setup, read loop, feeds parser, prints lines
    Kconfig.projbuild         HR_PRINT_EXTRAS, HR_DUMP_FRAMES (both default n)
  test/
    test_frame.c              host unit tests, compiled with TinyCC
    fixtures/                 raw frame captures from the real board (Phase 3)
    run_tests.ps1             one-liner that builds and runs the tests
```

`mmwave_frame.c` and `mr60bha2.c` must compile with a plain C compiler with no
IDF headers so the host tests can exercise them. Nothing in them may call
`printf`, `ESP_LOG`, or allocate; they work on a caller-provided struct with a
fixed 9 + 512 byte buffer.

---

## 3. Runtime design

One FreeRTOS task, `radar_task`, priority 5, 4 KB stack:

```
loop:
  n = uart_read_bytes(UART_NUM_1, buf, sizeof buf, pdMS_TO_TICKS(100))
  if n == 0: mmwave_parser_reset(&parser)      // idle gap: drop any partial frame
  for each byte: if (mmwave_parser_feed(&parser, byte, &frame)) handle(frame)
handle(frame):
  switch frame.type:
    0x0A15: printf("heart_rate: %.2f\n", bpm); fflush(stdout)
    others: decode if HR_PRINT_EXTRAS, else ignore
  if HR_DUMP_FRAMES: printf("frame: <hex bytes>\n")
```

UART1: `uart_driver_install(UART_NUM_1, 4096 RX, 0 TX, 0, NULL, 0)`,
`uart_param_config` 115200 8N1 no flow control, `uart_set_pin(UART_NUM_1, 16,
17, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE)`. A 4 KB ring buffer is plenty:
frames are 13 bytes and arrive a few times per second (the Arduino sketch's
32 KB was overkill).

`app_main` prints one line at boot, `info: spotify-heart-hr <version> esp32c6
reset=<esp_reset_reason>`, then starts the task and returns. The app ignores
that line.

No Wi-Fi, no NVS, no OTA. The task watchdog stays on (the read call blocks, so
idle is never starved).

---

## 4. Build environment

ESP-IDF v5.2.x, installed with the Espressif Windows installer (adjust the
paths to your install). In a fresh PowerShell:

```powershell
$env:IDF_PATH = "C:\Espressif\frameworks\esp-idf-v5.2.7"
. "$env:IDF_PATH\export.ps1"
cd <repo>\esp32-heartrate
idf.py set-target esp32c6
idf.py build
idf.py -p COM3 flash monitor
```

Host tests use TinyCC (`tcc.exe` on PATH, or point `$env:TCC` at it). Any
C99 compiler works if you adapt `test/run_tests.ps1`.

`sdkconfig.defaults` starting point:

```
CONFIG_IDF_TARGET="esp32c6"
CONFIG_ESPTOOLPY_FLASHSIZE_4MB=y
CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y
CONFIG_LOG_DEFAULT_LEVEL_WARN=y
CONFIG_BOOTLOADER_LOG_LEVEL_WARN=y
# CONFIG_NEWLIB_NANO_FORMAT is not set   (float printf must work)
```

---

## 5. Phases

### Phase 1 — Host-tested frame parser (no hardware, no IDF)

Entry: none.

Tasks:

1. Write `main/mmwave_frame.h/.c`: `mmwave_frame_init(parser*)`,
   `bool mmwave_frame_feed(parser*, uint8_t byte, frame_out*)`,
   `uint8_t mmwave_checksum(const uint8_t*, size_t)`. Implement the receive
   algorithm from section 1 including the LEN > 512 guard and the resync rule.
   Keep counters: frames_ok, bad_head_cksum, bad_data_cksum, oversize, resyncs.
   Also provide `mmwave_parser_reset()` for the idle-gap rule in section 3: a
   stray SOF followed by a plausible LEN would otherwise make the parser wait
   for hundreds of bytes and swallow real frames (the Arduino library has this
   weakness; it only recovers once the bogus frame fills up and fails its
   checksum).
2. Write `main/mr60bha2.h/.c`: `bool mr60bha2_decode(const frame*, reading*)`
   filling a tagged union for the types in the table. Read little-endian
   values with `memcpy`, never by pointer cast (alignment).
3. Write `test/test_frame.c` with a helper that *builds* frames using the same
   checksum rule, then feeds them:
   - a clean heart-rate frame, byte by byte: exactly one frame out, BPM equal
     to the encoded float;
   - the same frame with 20 bytes of random junk in front (including stray
     `0x01` bytes): one frame out;
   - two frames back to back: two out, in order;
   - one corrupted data byte: zero out, bad_data_cksum == 1, and a following
     good frame still decodes (resync works);
   - a header claiming LEN = 600: oversize == 1, next good frame decodes;
   - a frame with LEN = 0 and trailing `0xFF`: parses, no crash;
   - a point-cloud frame (`0x0A08`, n = 3): decodes to n = 3 and is not
     reported as heart rate.
4. `test/run_tests.ps1` compiles with `tcc -run` and exits non-zero on any
   failure.

Acceptance: `test/run_tests.ps1` prints all tests passing. No IDF include
anywhere in `mmwave_frame.c` or `mr60bha2.c` (grep for `esp_` and `freertos`
returns nothing).

### Phase 2 — ESP-IDF project skeleton (build only)

Entry: Phase 1 passing.

Tasks:

1. Create `CMakeLists.txt`, `main/CMakeLists.txt`, `sdkconfig.defaults`,
   `version.txt`, `main/Kconfig.projbuild` as in section 2.
2. Write `main/app_config.h`, `main/radar_task.c`, `main/main.c` per section 3.
3. `idf.py set-target esp32c6` then `idf.py build`.
4. Check the generated `sdkconfig` for `CONFIG_NEWLIB_NANO_FORMAT` (must be
   unset) and `CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y`.

Acceptance: build succeeds with zero warnings from `main/`. Binary size is
reported (expect well under 300 KB). Nothing flashed yet.

### Phase 3 — Flash and bench verification

Entry: Phase 2 built. Board plugged in over its USB-C. Nothing else holding the
COM port (close the Arduino IDE serial monitor and stop the tray app; see
`../windows-applet/PLAN.md` section 7 for the one-program-per-port rule).

Tasks:

1. Note the current COM port of the `303A:1001` device (was COM3).
2. `python -m esptool -p COMx flash_id` to confirm flash size; fix
   `sdkconfig.defaults` and rebuild if it is not 4 MB.
3. `idf.py -p COMx flash monitor`. The C6's USB-Serial/JTAG peripheral handles
   the esptool reset sequence itself, so no manual BOOT press should be needed.
   If it fails with "No serial data received": hold BOOT, tap RESET, release
   BOOT, then flash again.
4. In the monitor, with a still person about 50 cm from the radar, confirm
   `heart_rate: NN.NN` lines at roughly one every 0.5 to 1 s, values in a
   plausible 50 to 110 range. Confirm the lines stop when the subject leaves.
5. Quit the monitor. Run the Windows app in console mode
   (`..\windows-applet\.venv\Scripts\python -m spotify_heart --console`) and
   confirm it autodetects the port and reports the same readings. This is the
   real acceptance test: the app must not be changed.
6. Build the dump variant (`sdkconfig.dump`, command in section 6), capture
   20 to 30 s of `frame:` lines into `test/fixtures/capture_<situation>.txt`,
   then reflash the normal build. The host test replays every fixture passed
   on its command line (`test\run_tests.ps1 -Fixtures`) and asserts frames > 0
   and zero checksum failures. Done for `capture_nobody_present.txt`; still
   wanted: `capture_still_subject.txt` with a person 50 cm away.

Acceptance: steps 4, 5 and 6 all pass. Record the COM port, flash size, and the
observed line cadence in the Status table.

### Phase 4 — Hardening

Entry: Phase 3 passing.

Tasks and their tests:

1. **Host absent.** Power the board from a USB charger (no PC) for two
   minutes, then plug into the PC and open the port. Lines must appear within
   two seconds; the firmware must not have stalled on a full USB TX FIFO. If it
   did, switch output to `usb_serial_jtag_driver_install` with a TX ring buffer
   and a zero-timeout write, and repeat.
2. **Port reconnect.** With lines flowing, close and reopen the app five times
   and toggle DTR/RTS via pyserial. The chip must not reset or hang (the app
   already opens with DTR/RTS low; this checks the other paths).
3. **Radar silent.** Boot with GPIO17 disconnected or the radar not yet
   streaming. No crash, no watchdog, and the `info:` line still prints.
4. **Garbage on the wire.** Feed junk into RX for 10 s (e.g. a second USB-UART
   adapter typing random text at 115200), then reconnect the radar. Heart-rate
   lines resume; counters show resyncs, not a reboot.
5. **Counters on demand.** If any byte arrives on the USB console (the app
   never sends any), print one `stats: ok=N head=N data=N oversize=N resync=N`
   line. Handy for bench debugging, invisible to the app.
6. Run for one hour with the tray app doing its normal job. No reboot
   (the `info:` line carries `esp_reset_reason()`, so a reboot shows up as an
   unexpected second `info:` line in the app's log).

Acceptance: all six pass and are recorded in the Status table.

### Phase 5 — Documentation and hand-over

Entry: Phase 4 passing.

Tasks:

1. `README.md` in this folder: what it does, the exact output contract, the
   build and flash commands from section 4, the BOOT+RESET fallback, and the
   rollback command.
2. Update `../windows-applet/PLAN.md` section 0 ("Firmware" row) and section 1
   to point at this folder instead of the Arduino sketch, keeping the Arduino
   command as the documented rollback.
3. Commit this folder.

Acceptance: a fresh clone plus the section 4 commands produce a working board
without reading anything outside this folder.

---

## 6. Known pitfalls

- **IDF 5.2.7 hangs on UART1 init on the C6 (found 2026-09-17).** The UART
  driver's first register sync (`uart_ll_update` inside `uart_hal_init`)
  runs before the driver selects the UART's source clock, and nothing else
  enables UART1's clock on the C6 (`esp_perip_clk_init` is a stub, the ROM only
  configures UART0). The sync spins forever inside a critical section and the
  interrupt watchdog reboots the chip about 5 s after boot, with `MEPC` in
  `uart_ll_update` and `A4 = 0x60001000` (UART1). Calling `uart_param_config`
  before `uart_driver_install` does not help. `radar_task_start()` works around
  it by enabling the bus clock, selecting the source clock and enabling it via
  the `uart_ll_*` functions before touching the driver. Newer IDF releases
  reorder this; if the project ever moves to IDF 5.3+, the workaround can go.
- **Diagnostic builds need their own sdkconfig.** Extra defaults in
  `sdkconfig.dump` are only applied when an sdkconfig is *created*, so build
  the dump variant with `-B build_dump -DSDKCONFIG=build_dump/sdkconfig
  -DSDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.dump"`. Without the
  `-DSDKCONFIG` part the project-root sdkconfig is reused and the option is
  silently ignored (the binary size stays identical, which is the tell).
- **UART0 is GPIO16/17 on the C6 and that is the radar link.** If the IDF
  console is ever switched back to UART, the IDF log will be sent into the
  radar and radar bytes will be read as console input. Keep the console on
  USB-Serial/JTAG and the radar on UART1 with explicit pins.
- **ROM boot chatter reaches the radar.** The ROM bootloader always prints its
  banner on GPIO16 at reset. The Arduino build had the same behaviour and the
  radar ignores it, so this is known harmless.
- **`%.2f` needs full newlib formatting.** Nano formatting silently prints
  nothing for floats. Checked in Phase 2.
- **Only one program may own the COM port.** `idf.py monitor`, the Arduino
  serial monitor, and the tray app all conflict. Stop the tray app before
  flashing.
- **DTR/RTS can reset the chip.** The app already opens the port with both low.
  `idf.py monitor` deliberately resets the board on start, which is fine.
- **Do not print anything starting with `heart_rate:` except real readings.**
  The app's regex is prefix-based and would treat a debug line as a reading.
- **`getHeartRate()` semantics.** One line per received `0x0A15` frame, never
  repeated, never interpolated. The smoothing lives in the Windows app.
- **Frames with LEN = 0.** The library's *transmit* path omits the data
  checksum when there is no payload, but its *receive* path always expects
  one. This port only receives, so always expect the trailing checksum byte.

---

## Sources checked on 2026-09-17

- upstream `src/SeeedmmWave.{h,cpp}` v1.0.0: frame layout,
  checksum, LEN > 512 guard, receive state machine.
- upstream `src/SEEED_MR60BHA2.{h,cpp}`: type IDs and payload
  layouts.
- upstream `examples/HeartRateOnly/HeartRateOnly.ino`: the
  behaviour to replicate.
- `../windows-applet/spotify_heart/serial_reader.py`: VID/PID, regex, DTR/RTS
  handling the firmware must stay compatible with.
- Arduino esp32 core 3.3.11 `variants/XIAO_ESP32C6/pins_arduino.h` and
  `cores/esp32/HardwareSerial.h`: UART0 default pins GPIO16/17, CDC on boot
  enabled by default for this board.
- `C:\Espressif\frameworks\esp-idf-v5.2.7\components\soc\esp32c6` exists;
  `riscv32-esp-elf` toolchain installed under `C:\Espressif\tools`.
