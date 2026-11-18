# Usage

## GUI

```bash
myopyc gui --simulate 1
```

The toolbar provides:

- **Record / Stop recording** — start or finalise a session (Ctrl+R).
- **Add simulated arm** — mount a second fake armband for UI testing.
- **Connect Myo...** — enter a MAC address and a label; the binding is saved.
- **Restart streams / Stop streams** — recover from a BLE hiccup.
- **Open sessions** — open the session root in your file manager.

Each armband panel shows connection state, battery (when available), the latest
orientation quaternion, an 8-channel EMG oscilloscope and accelerometer traces.
The EMG view is decimated for display only; recorded data keeps every sample.

The status bar permanently shows **stream health** on the right: each armband's
observed sample rate versus its expected rate (EMG ~200 Hz, IMU ~50 Hz) and a
cumulative **dropped sample** counter, refreshed once a second whether or not you
are recording. A stream that was flowing but then goes silent is prefixed with a
`⚠` marker so a dropped BLE link is easy to spot. While recording, the left side
of the status bar additionally reports accepted samples, buffered kilobytes and
chunks written.

## Recording

Press **Record** once at least one armband is streaming. Data is accepted into
RAM and flushed every `flush_interval_s` seconds (default 2 s) into `.npy`
chunks. Stopping the recording performs a final flush and writes the final
manifest.

If the RAM budget is exhausted, the oldest samples are dropped and a gap entry
is written to `manifest.json`. This keeps the BLE stream alive and makes loss
visible instead of silently corrupting the timeline.

## Headless recording

```bash
# 10 seconds from two simulated armbands
myopyc record --max-seconds 10 --simulate 2

# real hardware, custom output directory and faster flushes
myopyc record --max-seconds 60 --simulate 0 \
  --ble D2:3B:85:94:32:8E=Left \
  --output-dir ~/recordings --flush-interval 1.0
```

`--max-seconds` (alias: `--seconds`) sets the maximum recording duration in
seconds (default: `10`). The command prints periodic counters and a JSON
summary, then exits.

### Exit codes

- `0` — recording completed and the session was finalised successfully.
- `2` — invalid arguments or a guard rejected the request (for example
  `--max-seconds` must be a positive number, or no `--simulate`/`--ble` sources
  were provided).
- `130` — interrupted by the user (SIGINT / Ctrl+C), when raised to the CLI.

## Inspecting sessions

```bash
myopyc sessions
myopyc inspect ~/.local/share/myopyc/sessions/20261117_142530_ab12cd
myopyc inspect <session> --arrays
```

For scripting, `--json` prints exactly one machine-readable JSON object with no
human text, so it is safe to pipe into `jq` or `json.loads`. The `--repair` and
`--arrays` flags fold their data into the same object (under `repair` and
`arrays` keys) instead of printing extra lines:

```bash
myopyc inspect <session> --json
myopyc inspect <session> --json --arrays | jq '.arrays["left/emg"]'
myopyc inspect <session> --json --repair | jq '.repair.status_after'
```

## Recovering a crashed session

If the machine loses power or the app is killed mid-recording, a session can be
left in an inconsistent state: a chunk file written to disk but never registered
in the manifest, a truncated last chunk, or a status still stuck on `recording`.
No recorded data through the *last completed flush* is lost, but the directory
needs reconciling before it reads cleanly:

```bash
myopyc inspect <session> --repair
```

`--repair` reconciles the chunk files against `manifest.json`:

- **orphaned** files (on disk, not in the manifest) and **truncated** files
  (unreadable `.npy`) are moved into an `unrepaired/` folder — never deleted;
- manifest entries whose files are missing or unreadable are dropped and sample
  counts patched;
- a session interrupted mid-recording has its status set to `repaired`;
- the previous manifest is kept as `manifest.json.bak`.

The operation is idempotent, so it is safe to run again, and a programmatic
equivalent is available:

```python
from myopyc.recorder import SessionReader

reader = SessionReader("<session>")
report = reader.repair()  # report.changed tells you if anything moved
```

## Reading data in Python

```python
import numpy as np
from myopyc.recorder import SessionReader, load_session

# Everything at once
session = load_session("path/to/session")
for key, device in session.devices.items():
    emg = device["emg"].samples
    imu = device["imu"].samples
    print(key, device.label, emg.shape, imu.shape)

# Or one stream at a time
reader = SessionReader("path/to/session")
emg = reader.load_stream("left", "emg")
t_seconds = (emg["t_ns"] - emg["t_ns"][0]) / 1e9
channels = np.stack([emg[f"ch{i}"] for i in range(1, 9)], axis=1)
```

Timestamps are host monotonic nanoseconds taken at BLE receipt. They are
suitable for ordering and per-device timing, and for best-effort alignment
between two armbands; they are not a hardware-synchronised clock. The manifest
lists every chunk and gap so a reader can reconstruct the timeline exactly.

## Data format compatibility

Sessions are identified by `format: "myopyc.session"` and `format_version: 1`.
`FORMAT_VERSION` stays at 1 and evolves only by **adding optional fields**
(append-only). Writers may add new optional top-level keys, per-device or
per-stream metadata, per-chunk fields, new gap reasons, or entirely new stream
kinds; they must never repurpose or remove existing keys. Readers must ignore
keys they do not understand and must not require newly added optional fields to
be present, so a session written by a newer tool remains readable by older v0.1+
readers (which will simply skip unknown stream kinds/fields). Breaking layout or
dtype changes require a future `format_version` bump.

## Configuration

Edit `~/.config/myopyc/config.toml` to change bindings, the output directory,
the flush interval, the RAM budget or plot windows. Invalid values do not stop
startup; warnings are logged and defaults are used where possible.

## Troubleshooting

- **"dl-myo is not installed"** — install the hardware extra: `pip install 'myopyc[hardware]'`.
- **Device not found** — confirm the MAC, wake the armband, and check `bluetoothctl scan on`.
- **Frequent drops** — increase `flush_interval_s`, use a faster disk, or move closer to the adapter.
- **App starts but no plots** — simulated devices only start streaming after the window opens; press **Restart streams** if a source errored.
