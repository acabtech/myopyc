# myopyc

Dongleless Thalmic Myo capture for Linux: live EMG/IMU visualization, up to two
armbands at once, and RAM-buffered recordings written to disk in chunks.

`myopyc` is a desktop application that wraps the excellent
[`dl-myo`](https://github.com/iomz/dl-myo) BLE client. It adds the things you
need for actual data collection:

- a live, smoothly scrolling EMG oscilloscope and IMU traces per armband;
- concurrent streaming from up to **two Myos**;
- stable **MAC-to-label bindings** (Left/Right) that survive restarts;
- **RAM-first recording** with time-based flushes to chunked `.npy` files;
- explicit gap/drop accounting when buffers overflow;
- a small reader API for NumPy/SciPy post-processing.

> **Status:** v0.1 alpha. Linux is the supported platform. `dl-myo` is GPLv3, so
> this project is GPL-3.0-or-later as well.

## Requirements

- Linux with BlueZ and a working BLE adapter (see [docs/setup-linux.md](docs/setup-linux.md)).
- Python 3.11+.
- One or two Thalmic Myo armbands (no dongle needed).
- `dl-myo` for real hardware (`pip install 'myopyc[hardware]'`); simulation needs no hardware.

## Install

```bash
git clone https://github.com/acabtech/myopyc.git
cd myopyc
uv sync --extra hardware
uv run myopyc
```

Or with pip:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[hardware]'
myopyc
```

## Run

Start the GUI with a simulated armband (no hardware required):

```bash
myopyc gui --simulate 1
```

Connect a real Myo by MAC address:

```bash
myopyc gui --simulate 0 --ble D2:3B:85:94:32:8E=Left
```

Two real armbands:

```bash
myopyc gui --simulate 0 \
  --ble D2:3B:85:94:32:8E=Left \
  --ble E1:4C:76:A5:43:9F=Right
```

Press **Record** in the toolbar to start a session, and again to stop and flush.
Sessions land in `~/.local/share/myopyc/sessions/` by default.

Headless recording is available for scripts and soak tests:

```bash
myopyc record --seconds 10 --simulate 2
myopyc inspect ~/.local/share/myopyc/sessions/<session>
myopyc sessions
```

## Recorded data

Each session is a self-contained directory:

```
20261117_142530_ab12cd/
├── manifest.json
└── devices/
    ├── left/
    │   ├── emg/chunk-00000.npy
    │   └── imu/chunk-00000.npy
    └── right/
        ├── emg/chunk-00000.npy
        └── imu/chunk-00000.npy
```

`manifest.json` records schema version, device identities, timebase, chunk
index, sample counts, dropped samples and every gap. EMG chunks are structured
arrays with host monotonic timestamps (`t_ns`), a per-stream sequence counter
and eight raw channels; IMU chunks add a quaternion plus accelerometer and
gyroscope. The full on-disk format — chunk dtypes, `t_ns` semantics, gap
encoding and the append-only evolution contract — is specified in
[docs/data-format.md](docs/data-format.md). Read the data with:

```python
from myopyc.recorder import load_session

session = load_session("~/.local/share/myopyc/sessions/20261117_142530_ab12cd")
emg = session["left"]["emg"].samples
imu = session["right"]["imu"].samples
```

## Configuration

Settings are stored in `~/.config/myopyc/config.toml` and created on first run:

```toml
version = 1

[[devices]]
mac = "D2:3B:85:94:32:8E"
label = "Left"
slot = 0

[recorder]
flush_interval_s = 2.0
max_ram_bytes = 67108864
output_dir = "/home/you/.local/share/myopyc/sessions"

[ui]
emg_window_s = 2.0
imu_window_s = 4.0
auto_start_streaming = true
```

Samples are buffered in RAM and flushed at most every `flush_interval_s`. If
the total buffer exceeds `max_ram_bytes`, the oldest samples are dropped and a
gap is recorded in the manifest rather than stalling the BLE stream.

## Development

```bash
make install   # uv sync --extra dev --extra hardware
make test      # pytest
make lint      # ruff
make run       # myopyc gui --simulate 1
```

The upstream `dl-myo` checkout lives in `upstream/dl-myo/` for local reference
only and is gitignored; the runtime uses the pinned/shared package dependency.
Design notes are kept locally in `plans/` (also gitignored).

## License

GPL-3.0-or-later. See [LICENSE](LICENSE). `dl-myo` is copyright its authors and
distributed under GPLv3.
