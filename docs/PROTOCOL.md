# HM80 configuration protocol

Derived from static analysis of the official hm-mag 2.5.9 app (for interoperability) and verified on an HM80
running firmware 2.5.9: against a USB capture of the official app, against its saved profiles, and with live
reads and writes from Linux. Fields marked *(inferred)* are not confirmed on hardware.

## Transport

- USB `c019:1234`. Interfaces: HID keyboard (0), CDC-ACM (1–2), audio (3–4). Configuration uses the **CDC-ACM
  serial port** (`/dev/ttyACM*`), not HID.
- Every packet is **32 bytes**: `[cmd][30 bytes payload][sum(bytes 0..30) & 0xFF]`. Unused bytes are zero.
- Several packets may arrive in one read; split on 32-byte boundaries. The official app drops its whole receive
  buffer on a checksum mismatch.
- Multi-byte fields are little-endian. "CRC-32" is the standard zlib CRC-32.
- Bytes after a keyboard packet's payload can contain stale buffer data; ignore them.

## Model: the keyboard pulls, the host serves

The host holds the per-key table; the keyboard never uploads it.

1. The keyboard sends `0x01` about 33 times per second.
2. The host answers with the CRC-32 of each of the 6 layers of its per-key table.
3. For each layer whose CRC differs from its own, the keyboard requests every key's record with `0x18` and stores
   the replies **in flash**. There is no separate save command.

Lighting (`0x16`) and global settings (`0x17`) work the other way: the keyboard pushes them when the port opens,
and the host writes them back when the user changes something.

## Commands

| cmd | direction | layout |
|---|---|---|
| `0x01` | kb → host | `01 'm14a' 00 <fw major minor patch> 64 ff×12 …` heartbeat (`02 05 09` = 2.5.9) |
| `0x01` | host → kb | `01 <fw major minor patch, as the keyboard reported> <6 × u32 CRC-32 of layers 0..5> 00 00 00` (the official app sends `02 05 09`) |
| `0x18` | kb → host | `18 <layer> <key>` record request |
| `0x18` | host → kb | `18 <layer> <key> <18-byte record>` |
| `0x16` | both | `16 <layer 0..5> <5-byte lighting struct>`; the host sends all six for any change; the keyboard answers each with a `0x17` |
| `0x17` | both | `17 <21-byte global block>` |
| `0x19` | host → kb | calibration: `19 01` start, `19 00` stop and save, `19 02` factory baseline (see below; never send) |
| `0x19` | kb → host | calibration table, 5 entries per packet: `[key][status][u16 rest][u16 bottom]` (`ff` = end); with byte 7 = `fe`: one key just calibrated |
| `0x1a` | kb → host | an "open program/file/link" key action fired |
| `0x1b` | host → kb | custom per-key lighting frames: host announces `1b 01 <u32 size>`, keyboard pulls `<u32 offset> <u8 len>` chunks |
| `0x1c` | kb → host | *(not seen)* the live key travel is not a separate packet, it rides in every heartbeat, see below |
| `0x1d` | both | screen image upload |
| `0x14` / `0x15` | kb ↔ host | **firmware update.** The keyboard asks; the host serves an image in chunks. Do not answer unless you mean to flash, see "Firmware update" below. |

## Firmware versions

The keyboard compares the three version bytes of the host's `0x01` reply with its own built-in version. If they differ in any way,
older or newer, it logs "begin DFU", asks once per connection for a firmware image (`0x14`), and does not go on to pull layers. This is
a version lock, not a statement that its firmware is old. Replying with the version from its own heartbeat (bytes 6..8) avoids it.

The images bundled with the official drivers 2.5.3 to 2.5.9 were compared. 2.5.4, 2.5.6, 2.5.7 and 2.5.9 differ by 5 to 14 bytes, all of
them version digits and the immediates that hold them (2.5.7 against 2.5.9 is 6 bytes). 2.5.3 and 2.5.5 add small changes (the lighting
color conversion, the start-up code). 2.5.8 is a different build, 124 KB instead of 90 KB with another RAM layout, and is not covered.

## Firmware update

Worked on real hardware in both directions (2.5.3, 2.5.5, 2.5.7, 2.5.8 and 2.5.9, about 1.5 s for 90 KB). `hm80/firmware.py`.

1. **Trigger.** Answer a heartbeat with a `0x01` whose version differs from the keyboard's. The port closes within 0.2 s
   (it restarts into its updater, same USB id and port) and about two seconds after it is back it sends `0x14`. Its model
   is in bytes 1..4 (`m14a`). A keyboard that is already waiting sends `0x14` on its own after each restart.
2. **Image info.** The host replies `14 <ver:3> <size:u32> <crc32:u32>`, little endian, `crc32` as `zlib.crc32`. `<ver>` is the
   version the image reports. It is stored in the image as ASCII after the marker `This is a prefix for a version number`.
3. **Chunks.** The keyboard sends `15 <offset:u32> <len:u8>` (24 bytes at a time in practice, 25 at most). The host replies
   `15 <offset:u32> <len:u8> <data>`. 3776 requests for the 90,624-byte 2.5.9 image, in ascending offset order.
4. **Finish.** After the last chunk the keyboard restarts once more and sends heartbeats with the new version. Nothing else
   is needed from the host, and no packet marking the end was seen.
5. **No answer.** A keyboard that got a mismatched hello and is never served restarts about every 10.5 s, asks again, does
   not type, and a power cycle does not clear it. Serving the image clears it.

The 2.5.9 image sits verbatim inside `hm-mag-v2.5.9.exe` (offset 430461, 90,624 bytes, SHA-256 `45f7cbaa…`), so a range request
fetches just it. The 2.5.8 image installs and heartbeats, but the keyboard does not register key presses and loses its lighting
effects. Not tested: an interrupted transfer.


## Live key travel (in every `0x01` heartbeat)

About 33 heartbeats a second carry the key that moved last. All numbers are little-endian, in micrometers.

| bytes | meaning |
|---|---|
| 10 | key index, `0xFF` when none is moving |
| 11 | triggered (past its actuation point and not yet released) |
| 12–13 | depth (int16) |
| 14–15 | the depth where the key last let go, which is its actual reset point |
| 16–17 | the deepest depth reached while triggered |
| 18–21 | the key's press and release distances, in 0.01 mm |

The official switch tester clamps depths to -0.1…4.2 mm. Its "Fluctuation" is the highest minus the lowest of the last 10
depth readings, and its "Real Time Value" panel shows the depth, the released-at depth only while the key is released,
and the peak only while it is triggered. The bytes after 21 are stale buffer data.

## Per-key record (18 bytes)

6 layers × 110 slots; the HM80 uses keys 0–83. CRCs cover `84 × 18` bytes per layer.

| offset | type | field |
|---|---|---|
| 0 | u16 | total travel, 0.01 mm (layer 0; stock 330 = 3.30 mm; recalibrate after changing) |
| 2 | u8 | initial actuation, 0.01 mm |
| 3 | u16 | actuation, 0.01 mm |
| 5 | u16 | reset, 0.01 mm |
| 7 | u8 | key mode, see below |
| 8 | 3 B | action A: `[type][u16 value]`, `ff ff` = none |
| 11 | 3 B | action B (release / hold / canceled / heavy key) |
| 14 | u16 | count (1–1000) |
| 16 | u16 | time in ms (5–5000); bit 15 = jitter: the firmware adds 1–5 ms at random to each wait |

Key modes (names as in the official app; the firmware's handling is in the per-key mode switch):

| mode | name | A | B | count | time |
|---|---|---|---|---|---|
| 0 | Press | the key | – | – | – |
| 1 | Press/Release | the key | also pressed on release | – | how long B is held · jitter |
| 2 | Short/Long | sent on a tap | sent on a hold | – | hold threshold ("judge time") |
| 3 | N Rapid fire | tapped *count* times per press | – | taps | gap between taps · jitter |
| 4 | Rapid fire | tapped while held | – | – | gap between taps · jitter |
| 5 | Cancel *(no longer offered by the official app)* | the key | released while A is down | – | – |
| 6 | Light/Heavy | slow press | fast press | judge speed: heavy when the press speed ≥ count × 0.001 | – |

The unit of the firmware's press speed for Light/Heavy is not known.

Travel semantics (confirmed by feel): a key first triggers at *initial + actuation*. After that it re-triggers
after moving down by *actuation* and releases after moving up by *reset* (rapid trigger). The official presets are
(10, 2, 4), (0, 20, 20) and (0, 200, 200).

Action types: 0 keyboard (value = HID usage; modifiers `0x80 + bit`: LCtrl, LShift, LAlt, LGui, RCtrl, RShift,
RAlt, RGui) · 1 mouse · 2 consumer/media · 3 lighting · 4 layer · 5 open program/file/link · 6 MIDI · 7 screen.
Except for MIDI (`note | row << 8`) only the first value byte is a code; the app writes `ff` after it. Code `ff`
means the key does nothing on that layer (most of the Fn layer). Codes, from the official app:

| type | codes |
|---|---|
| 1 mouse | 1 left · 2 middle · 3 right · 4 wheel up · 5 wheel down |
| 2 media | 1 volume down · 2 volume up · 3 previous · 4 next · 5 play/pause · 6 mute · 7 stop |
| 3 lighting | 1/2 effect −/+ · 3/4 speed −/+ · 5/6 hue −/+ · 7/8 saturation −/+ · 9/10 brightness −/+ · 11 lights on/off · 12 LED test · 13 flashbang · 14/15 light-bar effect −/+ |
| 4 layer | 0 previous · 1 next · 2–6 layer 1–5 · 7 Fn layer · 12–16 hold for layer 1–5 · 17 Fn (hold) · 22 Win lock |
| 5 open | 0–3 program/file/link 1–4 |
| 7 screen | 0 previous page · 1 next page |

The official app's English translation swaps the two volume labels; the codes above follow its original Chinese
labels (音量减 = volume down), and the firmware the app ships agrees: it turns media codes 1–7 into the HID
consumer usages `ea e9 b6 b5 cd e2 b7` (a 7-entry byte table), so there are no other media codes. Layer 5 is the Fn layer. While Fn is held the keyboard pushes a `0x17` block with
active layer 5, and another with the previous layer on release, so a host can show the Fn layer while it is held.

## Calibration (`0x19`)

Verified on hardware, with the logic read from the firmware the app ships. The keyboard pushes its calibration
table when the port opens and again on `19 01`: per key a status (2 = calibrated, 1 = last calibration failed) and
the resting and bottomed-out sensor readings divided by 7 (around 1800 and 700–1100 on stock switches; the reading
falls as the key goes down). The app also sets heartbeat-reply byte 28 to 1 while its Calibration page is open.

During calibration (`19 01` until `19 00`) the keyboard stops its heartbeats and lighting effects and turns every
key's LED off. A key is calibrated once it has been pressed at least ~2100 raw units below rest, released, and held
still for 500 scans; the keyboard then stores the new rest and bottom values, lights the key blue (OK) or red
(failed: bottom/rest outside the expected ratio), and sends `19 <key> <status> <u16 rest> <u16 bottom> fe`. Keys that
are not pressed keep their old values. `19 00` writes the new values to flash; unplugging first discards them.

`19 02` is the factory step: it averages each sensor for 1000 scans as the "no magnet" baseline. It is meant for a
board **without switches**; with switches fitted it would record the magnets as the baseline. Keys whose resting
reading is within ±300 of that baseline are skipped by normal calibration.

## Lighting struct (`0x16`, 5 bytes per layer)

| byte | meaning |
|---|---|
| 0 | bits 0–3 backlight effect · bits 4–6 light-bar effect · bit 7 overlay custom per-key lighting |
| 1 | hue (0–255 → 0–360°) |
| 2 | saturation |
| 3 | brightness (0–255) |
| 4 | speed (0–255) |

While Fn is held the keyboard is on layer 5 (the Fn layer) and shows layer 5's struct, which is Cycle Left/Right from
the factory *(inferred from what a held Fn looks like, not captured)*. The app can keep layer 5's struct equal to that
of the layer in use (with the brightness byte at 40%, so Fn looks dimmer) by sending it with the usual six `0x16`
packets. That only happens while the app runs: the keyboard does not copy it by itself. Custom per-key lighting frames belong to their layer and cannot be copied this way.

Backlight effects: Off, Custom, Static, Breathe, Cycle, Cycle Left/Right, Cycle Up/Down, Cycle Out/In,
Rainbow Moving, Pinwheel, Spiral, Rainbow Beacon, Reactive Simple, Reactive Wide, Reactive Nexus, Splash.
Light-bar effects: Off, Static, Breathe, Cycle, Cycle Left, Cycle Right, Gravity Pendulum, Key Depth.

## Global block (`0x17`, 21 bytes)

| byte | meaning |
|---|---|
| 0 | active layer |
| 2 | bit 7 lights on · bits 0–6 auto-off minutes (0 = never, max 60) |
| 3 | indicator light on |
| 4–6 | indicator hue, saturation, value |
| 7 | Win-key lock, one bit per layer |
| 9–20 | SOCD ("Rappy Snappy") pairs: `[9 + layer*4 + pair*2] = key A, key B` (0xFF = none; layers 0–2) |
| 1 | unknown |
| 8 | screen page 1-4, which Fn + Pg Up / Pg Dn change (the block is pushed again after each press). 1 is the reactive key page; Pg Up goes 1 → 4 → 3 → 2 → 1, so 4 is the dino game, 3 the reaction test, 2 the user image |

## Screen images (`0x1d`)

The HM80 has a 240×135 color screen. The user image is stored on the keyboard as one file named `user.<ext>`;
uploading replaces it, and there is no command to read it back.

Upload (verified on hardware, ~15 KB/s; a full-screen 104-frame GIF took 7 minutes):

1. host `1d 02 <u32 size>`. The official app announces GIFs as if every pixel had an alpha byte; copy that.
2. The keyboard first erases flash for the announced size (~1 MB/s, so 10–15 s for a full-screen GIF), then
   kb `1d 01` (ready) → host `1d 03 <20-byte file name> <u32 file size>` (file bytes 4..23 + its length)
3. kb `1d 03 <u32 offset> <u8 len>` (len ≤ 24, in 128-byte pages) → host `1d 04 <u32 offset> <u8 len> <data>`
4. kb `1d 05` (file stored) → host `1d 07` (done). `1d 06` from the host cancels.

The official app also restores the built-in "system" images (status icons, a dino game, a reaction test) with
`1d 01` followed by one `1d 03` header per file; that is not implemented here.

File format (little-endian):

| offset | content |
|---|---|
| 0 | `01 00 19 c0` |
| 4 | file name, 20 bytes, NUL-padded (`user.gif`, `user.png`, `user.jpg`, `user.bmp`) |
| 24 | u16 width, u16 height (scaled to fit 240×135, aspect kept) |
| 28 | u16 frame count (1 for stills, ≤ 120), u16 frame delay ms (GIF: the first frame's) |
| 32 | u8 alpha (1 for PNG) |
| 33 | u32 pixel data size |
| 37 | u32 sum of bytes 0..36 |
| 41 | pixels: frames back to back, row-major RGB565 little-endian, plus one alpha byte per pixel if alpha |

The whole file is zero-padded to a multiple of 256 bytes.

## Profile files (`.hmc`)

The official app stores its whole host state (0xBDF4 bytes) with Qt's `qCompress` (4-byte big-endian length +
zlib stream) in `Documents\config.<model>.hmc` (`m14` for the HM80). Offsets in the decompressed struct:

| offset | content |
|---|---|
| 0x00 | u32 version (3), u32 model tag |
| 0x16 | 6 layer names, 0x3D bytes each |
| 0x137D | global block (`0x17`) |
| 0x1392 | 6 lighting structs (`0x16`) |
| 0x13B0 | per-key table, 6 × 110 × 18 bytes |
| 0x4218 | custom lighting frames (`0x1b`), 0x7BD8 bytes + u32 CRC: per layer 0x14A4 bytes = `[mode][?][u16 frame mask]` + 16 frames × 110 keys × RGB. Mode 0x10 plays the ticked frames in order, 0x11 at random, n shows frame n *(mask and color order inferred)* |
