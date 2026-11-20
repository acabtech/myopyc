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
    EVENT = "event"


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

EVENT_DTYPE = np.dtype(
    [
        ("t_ns", "<i8"),
        ("seq", "<i8"),
        ("type", "u1"),
        ("ox", "<f4"),
        ("oy", "<f4"),
        ("oz", "<f4"),
        ("ow", "<f4"),
    ]
)

EVENT_TYPE_NAMES: dict[int, str] = {
    0: "none",
    1: "fist",
    2: "wave_left",
    3: "wave_right",
    4: "double_click",
}
"""Myo pose vocabulary encoded in :attr:`EVENT_DTYPE`'s ``type`` field.

``0`` is the sentinel for "no pose" (e.g. an orientation-style event with
no gesture). Unknown integers are rendered as ``"unknown-<n>"`` by
:func:`event_type_name` so new firmware poses never break old readers.
"""

EVENT_TYPE_IDS: dict[str, int] = {name: code for code, name in EVENT_TYPE_NAMES.items()}

#: Fields packed into :attr:`EVENT_DTYPE`'s ``payload`` column, in order.
EVENT_PAYLOAD_FIELDS = ("ox", "oy", "oz", "ow")

STREAM_DTYPES: dict[StreamKind, np.dtype[Any]] = {
    StreamKind.EMG: EMG_DTYPE,
    StreamKind.IMU: IMU_DTYPE,
    StreamKind.EVENT: EVENT_DTYPE,
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


_EVENT_COLUMNS = [
    {"name": "t_ns", "unit": "ns", "description": "Host monotonic receive time"},
    {"name": "seq", "unit": "1", "description": "Per-stream sequence counter"},
    {
        "name": "type",
        "unit": "code",
        "description": "Gesture/pose code (0=none, 1=fist, 2=wave_left, 3=wave_right, 4=double_click)",
    },
    {"name": "ox", "unit": "quaternion", "description": "Orientation payload quaternion x"},
    {"name": "oy", "unit": "quaternion", "description": "Orientation payload quaternion y"},
    {"name": "oz", "unit": "quaternion", "description": "Orientation payload quaternion z"},
    {"name": "ow", "unit": "quaternion", "description": "Orientation payload quaternion w"},
]


def event_type_name(code: int) -> str:
    """Human-readable pose name for an ``EVENT`` ``type`` code."""

    return EVENT_TYPE_NAMES.get(int(code), f"unknown-{int(code)}")


def stream_meta(kind: StreamKind) -> dict[str, Any]:
    """Return the manifest description for a stream (dtype columns, rate, units)."""

    if kind is StreamKind.EMG:
        columns = _EMG_COLUMNS
        rate = 200.0
        notes = "Raw, unfiltered 8-channel EMG sampled by the Myo at ~200 Hz."
    elif kind is StreamKind.IMU:
        columns = _IMU_COLUMNS
        rate = 50.0
        notes = "Orientation quaternion plus accelerometer and gyroscope, ~50 Hz."
    else:
        columns = _EVENT_COLUMNS
        rate = 0.0
        notes = (
            "Discrete gesture/pose events (fist, waves, double click) plus packed "
            "orientation payload; event-driven, so sample_rate_hz is 0.0. The "
            "``type`` field maps 1-4 to fist/wave_left/wave_right/double_click "
            "and 0 to no pose; unknown codes render as ``unknown-<n>``."
        )

    return {
        "sample_rate_hz": rate,
        "dtype": str(STREAM_DTYPES[kind]),
        "columns": [dict(column) for column in columns],
        "notes": notes,
    }
