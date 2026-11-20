from __future__ import annotations

import numpy as np
import pytest

from myopyc.streams.schema import (
    EMG_CHANNELS,
    EMG_DTYPE,
    EVENT_DTYPE,
    EVENT_PAYLOAD_FIELDS,
    EVENT_TYPE_IDS,
    EVENT_TYPE_NAMES,
    IMU_DTYPE,
    STREAM_DTYPES,
    StreamKind,
    event_type_name,
    stream_meta,
)


def test_emg_dtype_layout() -> None:
    assert EMG_DTYPE.names is not None
    assert "t_ns" in EMG_DTYPE.names
    assert "seq" in EMG_DTYPE.names
    for channel in range(1, EMG_CHANNELS + 1):
        assert f"ch{channel}" in EMG_DTYPE.names
    assert EMG_DTYPE.itemsize == 8 * 2 + EMG_CHANNELS


def test_imu_dtype_layout() -> None:
    assert IMU_DTYPE.names is not None
    for field in ("qw", "qx", "qy", "qz", "ax", "ay", "az", "gx", "gy", "gz"):
        assert field in IMU_DTYPE.names


def test_stream_metadata_describes_units_and_rate() -> None:
    emg = stream_meta(StreamKind.EMG)
    assert emg["sample_rate_hz"] == 200.0
    assert len(emg["columns"]) == 2 + EMG_CHANNELS
    assert any(column["name"] == "ch8" for column in emg["columns"])

    imu = stream_meta(StreamKind.IMU)
    assert imu["sample_rate_hz"] == 50.0
    assert {column["name"] for column in imu["columns"]} >= {"qw", "az", "gz"}


def test_event_dtype_layout() -> None:
    """EVENT stream: t_ns + seq + pose code + packed orientation payload."""
    assert EVENT_DTYPE.names is not None
    for field in ("t_ns", "seq", "type", *EVENT_PAYLOAD_FIELDS):
        assert field in EVENT_DTYPE.names
    # t_ns + seq (8 bytes each), type (1 byte), payload (4 x float32).
    assert EVENT_DTYPE.itemsize == 8 + 8 + 1 + 4 * 4
    assert EVENT_DTYPE["type"].base == np.uint8
    assert EVENT_DTYPE["ox"].base == np.float32
    assert StreamKind.EVENT in STREAM_DTYPES


def test_event_type_vocabulary() -> None:
    """Pose codes 0-4 are the documented vocabulary; unknowns degrade gracefully."""
    assert EVENT_TYPE_NAMES == {
        0: "none",
        1: "fist",
        2: "wave_left",
        3: "wave_right",
        4: "double_click",
    }
    assert EVENT_TYPE_IDS["fist"] == 1
    assert EVENT_TYPE_IDS["double_click"] == 4
    assert event_type_name(2) == "wave_left"
    assert event_type_name(0) == "none"
    assert event_type_name(99) == "unknown-99"
    with pytest.raises(KeyError):
        EVENT_TYPE_IDS["unknown-99"]


def test_event_metadata_documents_event_driven_rate() -> None:
    meta = stream_meta(StreamKind.EVENT)
    assert meta["sample_rate_hz"] == 0.0  # event-driven, not sampled
    assert {column["name"] for column in meta["columns"]} >= {"t_ns", "seq", "type", "ow"}
    assert "event" in meta["notes"].lower()
