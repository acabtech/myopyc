# myopyc

`myopyc` is a Linux desktop app for dongleless Thalmic Myo capture: live
EMG/IMU visualization, up to two armbands, and RAM-buffered chunked recordings.

## What it does

- Live 8-channel EMG oscilloscope and IMU traces per armband.
- Concurrent streaming from up to two Myos (or simulated sources).
- RAM-first recording with time-based `.npy` chunk flushes and explicit gap/drop
  accounting.
- Session management: markers, gesture events, notes, read-only playback, and
  export (CSV/NPZ/ZIP with integrity check).
- A small reader API for NumPy/SciPy post-processing.

## Start here

- [Setup (Linux)](setup-linux.md) — BlueZ/permissions and hardware prerequisites.
- [Usage](usage.md) — GUI, headless recording, inspect/export, troubleshooting.
- [User guide](user-guide.md) — collection workflows: markers, gestures,
  derived channels, playback, notes, export.
- [Data format](data-format.md) — normative session/chunk/manifest contract.
- [Lab interop](interop-lab-software.md) — NumPy/MAT/EEGLAB/LabChart recipes.

## Quick run

```bash
git clone https://github.com/acabtech/myopyc.git
cd myopyc
uv sync --extra hardware
uv run myopyc gui --simulate 1
```

For hardware-free development, simulation requires no BLE adapter.
