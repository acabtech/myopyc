"""Pure (headless) tests for stream-health rate computation and formatting."""

from __future__ import annotations

import pytest

from myopyc.streams.health import (
    DEGRADED_RATIO,
    DeviceHealth,
    RateTracker,
    StreamHealth,
    expected_rate,
    format_device_line,
    format_status_line,
)
from myopyc.streams.schema import StreamKind


def test_expected_rate_matches_schema() -> None:
    assert expected_rate(StreamKind.EMG) == pytest.approx(200.0)
    assert expected_rate(StreamKind.IMU) == pytest.approx(50.0)


def test_first_observe_is_idle() -> None:
    tracker = RateTracker()
    streams = tracker.observe("left", {}, now=0.0, dt=0.0)
    by_kind = {s.kind: s for s in streams}
    assert by_kind[StreamKind.EMG].status == "idle"
    assert by_kind[StreamKind.EMG].observed_hz == 0.0


def test_steady_rate_is_healthy() -> None:
    tracker = RateTracker()
    # Prime: first window establishes the baseline total.
    tracker.observe("left", {StreamKind.EMG: 200, StreamKind.IMU: 50}, now=0.0, dt=1.0)
    streams = tracker.observe("left", {StreamKind.EMG: 400, StreamKind.IMU: 100}, now=1.0, dt=1.0)
    by_kind = {s.kind: s for s in streams}
    assert by_kind[StreamKind.EMG].observed_hz == pytest.approx(200.0)
    assert by_kind[StreamKind.EMG].status == "healthy"
    assert by_kind[StreamKind.IMU].status == "healthy"


def _emg_only_tracker() -> RateTracker:
    return RateTracker(kinds=(StreamKind.EMG,))


def test_degraded_when_below_ratio_but_above_zero() -> None:
    tracker = _emg_only_tracker()
    expected = int(expected_rate(StreamKind.EMG))
    half = int(expected * (DEGRADED_RATIO / 2))
    tracker.observe("left", {StreamKind.EMG: expected}, now=0.0, dt=1.0)
    (stream,) = tracker.observe("left", {StreamKind.EMG: expected + half}, now=1.0, dt=1.0)
    assert stream.status == "degraded"


def test_stalled_after_samples_then_silence() -> None:
    tracker = _emg_only_tracker()
    expected = int(expected_rate(StreamKind.EMG))
    tracker.observe("left", {StreamKind.EMG: expected}, now=0.0, dt=1.0)
    (stream,) = tracker.observe("left", {StreamKind.EMG: expected}, now=1.0, dt=1.0)
    assert stream.status == "stalled"
    assert stream.total == expected  # cumulative count is retained


def test_reset_clears_history() -> None:
    tracker = _emg_only_tracker()
    tracker.observe("left", {StreamKind.EMG: 100}, now=0.0, dt=1.0)
    tracker.reset("left")
    (stream,) = tracker.observe("left", {StreamKind.EMG: 100}, now=1.0, dt=1.0)
    # With history cleared the device looks idle again (baseline re-established).
    assert stream.status == "idle"


def test_stream_health_ratio_guard() -> None:
    zero = StreamHealth(StreamKind.EMG, observed_hz=0.0, expected_hz=0.0, total=5)
    assert zero.ratio == 0.0


def test_device_status_worst_of_streams() -> None:
    streams = (
        StreamHealth(StreamKind.EMG, observed_hz=200.0, expected_hz=200.0, total=200),
        StreamHealth(StreamKind.IMU, observed_hz=0.0, expected_hz=50.0, total=50),
    )
    device = DeviceHealth(key="left", label="Left", streams=streams)
    # EMG healthy, IMU stalled -> device is stalled (worst).
    assert device.status == "stalled"


def _healthy_emg_stream() -> StreamHealth:
    return StreamHealth(StreamKind.EMG, observed_hz=200.0, expected_hz=200.0, total=200)


@pytest.mark.parametrize(
    ("device", "expected"),
    [
        pytest.param(
            DeviceHealth(
                key="left",
                label="Left",
                streams=(
                    _healthy_emg_stream(),
                    StreamHealth(StreamKind.IMU, observed_hz=50.0, expected_hz=50.0, total=50),
                ),
            ),
            "Left: emg 200/200 Hz, imu 50/50 Hz",
            id="two-streams",
        ),
        pytest.param(
            DeviceHealth(
                key="left", label="Left", streams=(_healthy_emg_stream(),), battery=76
            ),
            "Left: emg 200/200 Hz, battery 76%",
            id="battery-known",
        ),
        pytest.param(
            DeviceHealth(key="left", label="Left", streams=(_healthy_emg_stream(),), battery=None),
            "Left: emg 200/200 Hz",
            id="battery-unknown",
        ),
    ],
)
def test_format_device_line(device: DeviceHealth, expected: str) -> None:
    assert format_device_line(device) == expected


def test_format_status_line_empty_hint() -> None:
    assert format_status_line([], dropped=0) == "add a simulated arm or connect a Myo"


@pytest.mark.parametrize(
    ("streams", "dropped", "expected"),
    [
        pytest.param(
            (_healthy_emg_stream(),),
            3,
            "Left: emg 200/200 Hz | dropped 3",
            id="normal",
        ),
        pytest.param(
            (StreamHealth(StreamKind.EMG, observed_hz=0.0, expected_hz=200.0, total=999),),
            0,
            "⚠ Left: emg 0/200 Hz | dropped 0",
            id="stall-warns",
        ),
    ],
)
def test_format_status_line(
    streams: tuple[StreamHealth, ...], dropped: int, expected: str
) -> None:
    line = format_status_line([DeviceHealth(key="left", label="Left", streams=streams)], dropped)
    assert line == expected
    if expected.startswith("⚠"):
        assert "dropped 0" in line
    else:
        assert "⚠" not in line
