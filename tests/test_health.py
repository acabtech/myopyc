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


def test_rate_tracker_states() -> None:
    """Schema rates, idle baseline, healthy steady state, degraded, stalled, reset."""
    assert expected_rate(StreamKind.EMG) == pytest.approx(200.0)
    assert expected_rate(StreamKind.IMU) == pytest.approx(50.0)
    assert expected_rate(StreamKind.EVENT) == 0.0  # event-driven: no expectation

    tracker = RateTracker()
    by_kind = {s.kind: s for s in tracker.observe("left", {}, now=0.0, dt=0.0)}
    assert by_kind[StreamKind.EMG].status == "idle"
    assert by_kind[StreamKind.EMG].observed_hz == 0.0

    # Prime: first window establishes the baseline total.
    tracker.observe("left", {StreamKind.EMG: 200, StreamKind.IMU: 50}, now=0.0, dt=1.0)
    streams = tracker.observe("left", {StreamKind.EMG: 400, StreamKind.IMU: 100}, now=1.0, dt=1.0)
    by_kind = {s.kind: s for s in streams}
    assert by_kind[StreamKind.EMG].observed_hz == pytest.approx(200.0)
    assert by_kind[StreamKind.EMG].status == "healthy"
    assert by_kind[StreamKind.IMU].status == "healthy"

    emg_tracker = RateTracker(kinds=(StreamKind.EMG,))
    expected = int(expected_rate(StreamKind.EMG))
    half = int(expected * (DEGRADED_RATIO / 2))
    emg_tracker.observe("left", {StreamKind.EMG: expected}, now=0.0, dt=1.0)
    (stream,) = emg_tracker.observe("left", {StreamKind.EMG: expected + half}, now=1.0, dt=1.0)
    assert stream.status == "degraded"

    stall = RateTracker(kinds=(StreamKind.EMG,))
    stall.observe("left", {StreamKind.EMG: expected}, now=0.0, dt=1.0)
    (stream,) = stall.observe("left", {StreamKind.EMG: expected}, now=1.0, dt=1.0)
    assert stream.status == "stalled"
    assert stream.total == expected  # cumulative count is retained

    reset = RateTracker(kinds=(StreamKind.EMG,))
    reset.observe("left", {StreamKind.EMG: 100}, now=0.0, dt=1.0)
    reset.reset("left")
    (stream,) = reset.observe("left", {StreamKind.EMG: 100}, now=1.0, dt=1.0)
    # With history cleared the device looks idle again (baseline re-established).
    assert stream.status == "idle"


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


def test_health_object_guards() -> None:
    """Ratio guard for zero expectations and worst-of-streams device status."""
    zero = StreamHealth(StreamKind.EMG, observed_hz=0.0, expected_hz=0.0, total=5)
    assert zero.ratio == 0.0

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
            DeviceHealth(key="left", label="Left", streams=(_healthy_emg_stream(),), battery=76),
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
def test_format_status_line(streams: tuple[StreamHealth, ...], dropped: int, expected: str) -> None:
    line = format_status_line(
        [DeviceHealth(key="left", label="Left", streams=streams)], dropped=dropped
    )
    assert line == expected
    if expected.startswith("⚠"):
        assert "dropped 0" in line
    else:
        assert "⚠" not in line


# ------------------------------------------------------------------ event streams
def test_event_stream_never_stalls_or_degrades() -> None:
    """Event-driven streams are judged by flow, not rate: sparse is normal."""

    tracker = RateTracker()
    # Prime, then a window with new events: healthy with the right delta.
    tracker.observe("left", {StreamKind.EVENT: 1}, now=0.0, dt=1.0)
    streams = tracker.observe("left", {StreamKind.EVENT: 3}, now=1.0, dt=1.0)
    (event,) = [s for s in streams if s.kind is StreamKind.EVENT]
    assert event.delta == 2
    assert event.status == "healthy"
    assert event.event_driven is not False  # sanity: flag is exposed

    # A quiet window (no new events) reads idle, never stalled.
    (quiet,) = [
        s
        for s in tracker.observe("left", {StreamKind.EVENT: 3}, now=2.0, dt=1.0)
        if s.kind is StreamKind.EVENT
    ]
    assert quiet.status == "idle"
    assert quiet.delta == 0


def test_format_event_stream_shows_window_count() -> None:
    """The status fragment for events is a count, not a fake 0/0 Hz rate."""

    stream = StreamHealth(StreamKind.EVENT, observed_hz=2.0, expected_hz=0.0, total=3, delta=2)
    assert format_device_line(DeviceHealth(key="left", label="Left", streams=(stream,))) == (
        "Left: event 2 in window"
    )
    # Idle events (nothing in the window) still render cleanly.
    idle = StreamHealth(StreamKind.EVENT, observed_hz=0.0, expected_hz=0.0, total=3, delta=0)
    assert format_device_line(DeviceHealth(key="left", label="Left", streams=(idle,))) == (
        "Left: event 0 in window"
    )
