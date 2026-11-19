from __future__ import annotations

from myopyc.streams.schema import EMG_CHANNELS, EMG_DTYPE, IMU_DTYPE, StreamKind, stream_meta


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


