"""On-disk sample schemas.

A recording is stored as one ``.npy`` chunk per device and stream, plus a JSON
manifest. Keeping the dtype in one place means the recorder, the reader and the
live UI all agree on column order and units.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

import numpy as np

EMG_CHANNELS = 8
IMU_FIELDS = 11


class StreamKind(StrEnum):
    """Supported sensor streams."""

    EMG = "emg"
    IMU = "imu"


EMG_DTYPE = np.dtype(
    [("t_ns", "<i8"), ("seq", "<i8")] + [(f"ch{i}", "i1") for i in range(1, EMG_CHANNELS + 1)]
)

IMU_DTYPE = np.dtype(
    [
        ("t_ns", "<i8"),
        ("seq", "<i8"),
        ("qw", "<f4"),
        ("qx", "<f4"),
        ("qy", "<f4"),
        ("qz", "<f4"),
        ("ax", "<f4"),
        ("ay", "<f4"),
        ("az", "<f4"),
        ("gx", "<f4"),
        ("gy", "<f4"),
        ("gz", "<f4"),
    ]
)

STREAM_DTYPES: dict[StreamKind, np.dtype[Any]] = {
    StreamKind.EMG: EMG_DTYPE,
    StreamKind.IMU: IMU_DTYPE,
}

_EMG_COLUMNS = [
    {"name": "t_ns", "unit": "ns", "description": "Host monotonic receive time"},
    {"name": "seq", "unit": "1", "description": "Per-stream sequence counter"},
] + [
    {"name": f"ch{i}", "unit": "int8", "description": f"Raw EMG channel {i}"}
    for i in range(1, EMG_CHANNELS + 1)
]

_IMU_COLUMNS = [
    {"name": "t_ns", "unit": "ns", "description": "Host monotonic receive time"},
    {"name": "seq", "unit": "1", "description": "Per-stream sequence counter"},
    {"name": "qw", "unit": "quaternion", "description": "Orientation quaternion w"},
    {"name": "qx", "unit": "quaternion", "description": "Orientation quaternion x"},
    {"name": "qy", "unit": "quaternion", "description": "Orientation quaternion y"},
    {"name": "qz", "unit": "quaternion", "description": "Orientation quaternion z"},
    {"name": "ax", "unit": "g", "description": "Accelerometer x"},
    {"name": "ay", "unit": "g", "description": "Accelerometer y"},
    {"name": "az", "unit": "g", "description": "Accelerometer z"},
    {"name": "gx", "unit": "deg/s", "description": "Gyroscope x"},
    {"name": "gy", "unit": "deg/s", "description": "Gyroscope y"},
    {"name": "gz", "unit": "deg/s", "description": "Gyroscope z"},
]


def stream_meta(kind: StreamKind) -> dict[str, Any]:
    """Return the manifest description for a stream (dtype columns, rate, units)."""

    if kind is StreamKind.EMG:
        columns = _EMG_COLUMNS
        rate = 200.0
        notes = "Raw, unfiltered 8-channel EMG sampled by the Myo at ~200 Hz."
    else:
        columns = _IMU_COLUMNS
        rate = 50.0
        notes = "Orientation quaternion plus accelerometer and gyroscope, ~50 Hz."

    return {
        "sample_rate_hz": rate,
        "dtype": str(STREAM_DTYPES[kind]),
        "columns": [dict(column) for column in columns],
        "notes": notes,
    }
