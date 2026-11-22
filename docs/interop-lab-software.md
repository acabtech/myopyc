# NumPy → lab software interchange

This recipe shows how to take a recorded myopyc session (chunked `.npy` streams
plus `manifest.json`) into common lab-analysis tools. It is a **read-only,
post-hoc** workflow: never rewrite chunk files or mutate the session directory.

For the normative on-disk contract, see
[`docs/data-format.md`](data-format.md). For CLI export shortcuts, see
[`docs/usage.md`](usage.md#exporting-a-session).

## Load a session with NumPy

```python
from pathlib import Path

from myopyc.recorder import load_session

session = load_session(Path("~/.local/share/myopyc/sessions/<session>").expanduser())
emg = session["left"]["emg"].samples   # structured NumPy array, EMG_DTYPE
imu = session["left"]["imu"].samples   # structured NumPy array, IMU_DTYPE
```

`load_session()` returns [`SessionData`](../src/myopyc/recorder/reader.py)
containing per-device [`DeviceData`](../src/myopyc/recorder/reader.py) and
per-stream [`StreamData`](../src/myopyc/recorder/reader.py). Each `StreamData`
has:

- `samples`: contiguous structured array for the whole stream (all chunks
  concatenated in manifest order).
- `chunks`: the raw per-chunk arrays, if you need chunk boundaries.
- `meta`: manifest stream metadata (dtype string, `sample_rate_hz`, columns).

Stream dtypes are defined in [`src/myopyc/streams/schema.py`](../src/myopyc/streams/schema.py):

| Stream | Key fields | Notes |
|---|---|---|
| EMG | `t_ns`, `seq`, `ch1`..`ch8` | `ch*` are raw `int8`; nominal 200 Hz. |
| IMU | `t_ns`, `seq`, `qw/qx/qy/qz`, `ax/ay/az`, `gx/gy/gz` | quaternion, accel in g, gyro in deg/s; nominal 50 Hz. |
| EVENT | `t_ns`, `seq`, `type`, `ox/oy/oz/ow` | event-driven (`sample_rate_hz == 0.0`); not a regular signal. |

## Timestamps, units, and gaps

- **`t_ns` is host-monotonic nanoseconds**, not wall-clock time and not the
  Myo hardware clock. Convert to relative seconds with
  `t_s = (samples["t_ns"] - samples["t_ns"][0]) / 1e9`.
- **Do not assume a fixed sample interval.** Use `t_ns` for time axes. Nominal
  rates are useful metadata, but BLE receive jitter and drops make `t_ns` the
  authoritative timeline.
- **Gaps are real data loss.** RAM-buffer overflow drops oldest samples and
  records gap entries in the manifest. Gaps are *not* encoded inside chunks;
  chunks contain contiguous received samples. Check `stream.dropped_samples` and
  `session.manifest["gaps"]` before analysis.
- **EMG is raw `int8`.** Convert to `float32`/`float64` before filtering,
  rectification, RMS, or statistics to avoid integer overflow/wraparound.
- **IMU units:** accelerometer is g, gyroscope is deg/s, quaternion is
  `(qw, qx, qy, qz)` as recorded by the schema.

## Generic CSV for spreadsheets / LabChart-style import

For a quick interchange file, export a plain matrix with an explicit time
column. myopyc also provides `myopyc export <session> --format csv`, which
writes one CSV per device/stream and includes `t_iso` next to `t_ns`; use the
recipe below when you need a custom shape or unit conversion.

```python
import csv
import numpy as np
from pathlib import Path
from myopyc.recorder import load_session

session = load_session(Path("path/to/session"))
emg = session["left"]["emg"].samples
if emg.size == 0:
    raise SystemExit("no EMG samples")

t_s = (emg["t_ns"] - emg["t_ns"][0]).astype(np.float64) / 1e9
channels = np.stack([emg[f"ch{i}"].astype(np.float32) for i in range(1, 9)], axis=1)

out = Path("left_emg.csv")
with out.open("w", newline="", encoding="utf-8") as f:
    writer = csv.writer(f)
    writer.writerow(["time_s"] + [f"emg_ch{i}" for i in range(1, 9)])
    for row_t, row_ch in zip(t_s, channels):
        writer.writerow([f"{row_t:.9f}", *row_ch.tolist()])
```

LabChart and many spreadsheet tools can import this as delimited text. Pick
`time_s` as the X/time column and the `emg_ch*` columns as channels. If the
tool expects evenly sampled data, resample explicitly onto a uniform grid and
record the resampling method in your analysis notes.

## MAT-file interchange (MATLAB / SciPy / EEGLAB workflows)

MATLAB and SciPy can exchange `.mat` files. A compact MAT layout keeps raw
arrays, time, and units together:

```python
import numpy as np
from pathlib import Path
from scipy.io import savemat
from myopyc.recorder import load_session

session = load_session(Path("path/to/session"))
emg = session["left"]["emg"].samples

savemat(
    "left_emg.mat",
    {
        "t_ns": emg["t_ns"].astype(np.int64),
        "t_s": (emg["t_ns"] - emg["t_ns"][0]).astype(np.float64) / 1e9,
        "emg_i8": np.stack([emg[f"ch{i}"] for i in range(1, 9)], axis=0),  # 8 x N
        "emg_uV_note": np.array(["raw Myo int8; scale/interpret per your pipeline"]),
        "fs_nominal_hz": np.array([[200.0]]),
    },
    do_compression=True,
)
```

Notes:

- Write EMG as `8 x N` (channels x samples) or `N x 8`; be explicit in field
  names and downstream code. EEGLAB-style MATLAB workflows commonly expect
  channels x samples.
- Preserve `t_ns` for exact alignment; keep `t_s` as a convenience axis.
- Include nominal rate and unit metadata as separate fields — `.mat` has no
  universal channel-unit header.

## EEGLAB-oriented recipe

EEGLAB expects continuous channel data (typically channels x samples) plus
sampling/chanloc metadata. myopyc EMG is not EEG, but the matrix shape maps
cleanly for signal-processing workflows.

**Python-side prep:**

```python
import numpy as np
from scipy.io import savemat
from myopyc.recorder import load_session

session = load_session(Path("path/to/session"))
emg = session["left"]["emg"].samples
data = np.stack([emg[f"ch{i}"].astype(np.float32) for i in range(1, 9)], axis=0)  # 8 x N
t_s = (emg["t_ns"] - emg["t_ns"][0]).astype(np.float64) / 1e9

savemat("left_emg_eeglab.mat", {"data": data, "t_s": t_s, "srate": np.array([[200.0]])})
```

**MATLAB/EEGLAB-side sketch:**

```matlab
load('left_emg_eeglab.mat'); % data: 8 x N, srate: 200
EEG = pop_importdata('dataformat','array','data','left_emg_eeglab.mat', ...
    'srate',200,'pnts',size(data,2),'xmin',t_s(1));
% Set chan labels/units manually: EMG1..EMG8, raw Myo int8 units.
```

Caveats for EEGLAB:

- Treat channels as EMG, not scalp EEG; channel locations are usually not
  applicable.
- If there are recorded gaps/drops, do not silently stitch them as continuous
  EEG unless your analysis explicitly allows it. Either analyze contiguous
  segments between gaps, or resample/interpolate and document it.
- EVENT markers can be exported separately from
  [`SessionReader.markers`](../src/myopyc/recorder/reader.py) or the manifest
  `markers` array and imported as EEGLAB events if needed.

## LabChart-oriented recipe

LabChart typically imports delimited text or MATLAB/MAT files through its
import dialogs. The safest path is a wide CSV with a time column:

1. Export with `myopyc export <session> --format csv`, or generate a custom CSV
   as above.
2. Import as delimited text; select the time column as X.
3. Assign channel names (`EMG ch1`..`ch8`, `ax/ay/az`, etc.) and units from the
   table above.
4. Confirm the time base: myopyc `t_ns` is monotonic host time; `t_iso` in
   CLI CSV export is derived from that monotonic clock for readability, not a
   wall-clock ground truth.

For IMU, export accelerometer/gyroscope/quaternion as separate channel groups;
keep units explicit (`g`, `deg/s`, quaternion).

## Event/marker interoperability

Gesture/pose events are a regular sampled-structured stream only in storage
shape; semantically they are **event-driven** (`sample_rate_hz == 0.0`). For
event timelines, prefer:

- `session[device]["event"].samples` for recorded pose events (`type`, payload
  quaternion, `t_ns`).
- `session.markers` / `reader.markers` for user/session annotations.

When importing into tools with event channels, convert events to an event table
(time + label) rather than forcing them onto a fixed-rate signal grid.

## Verification checklist

Before trusting an imported dataset:

- Confirm sample counts match `myopyc inspect <session> --json`.
- Check `dropped_samples` and manifest `gaps`; decide how to handle missing
  time.
- Plot a short raw segment after import and compare against myopyc playback.
- Keep the original session directory immutable; treat exported MAT/CSV files as
  derived artifacts.
