"""Device manager tests, driven deterministically without real-time waits.

The simulator emits its first block and battery reading *synchronously* inside
:meth:`SimulatedSource.start`, so no sleeping is needed. State/counts/battery
paths that depend on stream callbacks are exercised by emitting synthetic
batches directly through :meth:`DeviceManager._emit`, keeping the suite fast.
"""

from __future__ import annotations

import asyncio
import contextlib

import numpy as np
import pytest

from myopyc.devices import adapter as adapter_mod
from myopyc.devices.manager import DeviceManager
from myopyc.devices.model import DeviceInfo, SampleBatch
from myopyc.devices.simulator import SimulatedSource
from myopyc.streams.schema import (
    EMG_DTYPE,
    EVENT_DTYPE,
    EVENT_TYPE_IDS,
    IMU_DTYPE,
    StreamKind,
    event_type_name,
)


def _synth(device: str, kind: StreamKind, count: int) -> SampleBatch:
    rows = np.zeros(count, dtype=EMG_DTYPE if kind is StreamKind.EMG else IMU_DTYPE)
    rows["seq"] = np.arange(count)
    return SampleBatch(device, kind, rows)


def test_simulated_source_emits_emg_and_imu_synchronously() -> None:
    """The simulator's first tick produces both streams without waiting."""
    info = DeviceInfo(key="left", label="Left", mac="SIM-LEFT", name="Simulated Myo")
    batches: list[SampleBatch] = []
    source = SimulatedSource(info, batches.append)
    source.start()
    try:
        kinds = {batch.kind for batch in batches}
        assert kinds == {StreamKind.EMG, StreamKind.IMU}
        assert source.running
    finally:
        source.stop()
    assert not source.running


def test_simulator_event_tick_fires_on_cadence() -> None:
    """Gestures fire only once the cadence expires, then re-arm with a gap."""

    info = DeviceInfo(key="left", label="Left", mac="SIM-LEFT", name="Simulated Myo")
    batches: list[SampleBatch] = []
    source = SimulatedSource(info, batches.append)
    source._next_event_at = 5.0

    source._tick_events(4.9)
    assert batches == []  # before the cadence: silent

    source._tick_events(5.1)
    assert len(batches) == 1
    batch = batches[0]
    assert batch.kind is StreamKind.EVENT
    assert batch.data.dtype == EVENT_DTYPE
    assert batch.data["seq"].tolist() == [0]
    # The pose comes from the real vocabulary (codes 1..4), payload is a
    # plausible quaternion with a near-unit norm.
    code = int(batch.data["type"][0])
    assert code in set(EVENT_TYPE_IDS.values()) - {0}
    assert event_type_name(code) != "none"
    norm = sum(float(batch.data[f"o{axis}"][0]) ** 2 for axis in "xyzw")
    assert 0.9 < norm < 1.1

    # After firing, the next gesture is scheduled 4..10 s later: not immediate.
    source._tick_events(5.2)
    assert len(batches) == 1
    next_gap = source._next_event_at - 5.1
    assert 3.9 < next_gap < 10.1
    assert source._event_seq == 1


def test_simulator_events_flow_over_time() -> None:
    """A few simulated seconds produce at least one gesture, ordered by seq."""

    info = DeviceInfo(key="left", label="Left", mac="SIM-LEFT", name="Simulated Myo")
    events: list[SampleBatch] = []
    source = SimulatedSource(
        info, lambda batch: events.append(batch) if batch.kind is StreamKind.EVENT else None
    )
    source._next_event_at = 2.0  # first gesture fires early for the test
    for tick in range(0, 40):
        source._tick_events(tick * 0.25)  # 10 simulated seconds
    assert events, "10 simulated seconds must produce gestures"
    seqs = [int(batch.data["seq"][0]) for batch in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert source._event_seq == len(events)


def test_manager_streams_counts_and_state() -> None:
    """State transitions and per-stream counts work without live threads."""
    manager = DeviceManager()
    batches: list[SampleBatch] = []
    manager.subscribe(batches.append)
    info = manager.add_simulated("Left")

    manager._emit(_synth(info.key, StreamKind.EMG, 4))
    manager._emit(_synth(info.key, StreamKind.IMU, 2))
    counts = manager.counts(info.key)
    assert counts[StreamKind.EMG] == 4
    assert counts[StreamKind.IMU] == 2

    manager._on_status(info.key, "streaming", None)
    assert manager.state(info.key) == "streaming"
    assert manager.meta()[info.key]["label"] == "Left"

    manager.stop_all()
    manager._on_status(info.key, "stopped", None)
    assert manager.state(info.key) == "stopped"


def test_two_devices_and_limit() -> None:
    manager = DeviceManager()
    left = manager.add_simulated("Left")
    right = manager.add_simulated("Right")
    assert {left.key, right.key} == {"left", "right"}

    try:
        manager.add_simulated("Third")
    except RuntimeError as exc:
        assert "at most 2" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected device limit to be enforced")


def test_battery_reported_and_cleared_on_remove() -> None:
    """Simulated device publishes a plausible battery level; removal clears it."""
    manager = DeviceManager()
    info = manager.add_simulated("Left")
    manager.start_all()
    try:
        # start() reports the initial battery synchronously (no drain-wait).
        level = manager.battery(info.key)
        assert level is not None and 15 <= level <= 95
    finally:
        manager.stop_all()
    manager.remove(info.key)
    assert manager.battery(info.key) is None


def test_remove_device() -> None:
    manager = DeviceManager()
    info = manager.add_simulated("Left")
    manager.remove(info.key)
    assert manager.keys == []
    assert not manager.has(info.key)


# ------------------------------------------------------------------ battery paths
def test_simulator_reports_battery_via_fake_callback() -> None:
    """The simulator publishes battery through its callback, then drains it."""
    info = DeviceInfo(key="left", label="Left", mac="SIM-LEFT", name="Simulated Myo")
    readings: list[int] = []
    source = SimulatedSource(info, lambda _batch: None, battery=lambda _k, v: readings.append(v))
    source.start()
    try:
        # start() reports the initial level synchronously before any drain.
        assert readings, "start() must report battery synchronously"
        initial = readings[0]
        assert 15 <= initial <= 95

        # One simulated second of ticks must push exactly one further reading,
        # one percent lower (never below the floor).
        source._tick_battery(source._battery_last_emit + 1.0)
        assert readings[-1] == max(source._battery_floor, initial - 1)

        # Rapid back-to-back ticks are rate-limited to one update per second.
        before = len(readings)
        source._tick_battery(source._battery_last_emit + 0.5)
        assert len(readings) == before
    finally:
        source.stop()


def test_simulator_battery_floor() -> None:
    """The simulated cell never drains below its floor."""
    info = DeviceInfo(key="left", label="Left", mac="SIM-LEFT", name="Simulated Myo")
    readings: list[int] = []
    source = SimulatedSource(info, lambda _batch: None, battery=lambda _k, v: readings.append(v))
    source._battery_level = source._battery_floor
    source._tick_battery(source._battery_last_emit + 1.0)
    assert readings[-1] == source._battery_floor
    # Idempotent at the floor: more elapsed time does not keep pushing.
    before = len(readings)
    source._tick_battery(source._battery_last_emit + 2.0)
    assert len(readings) == before + 1  # emits again, but the level holds
    assert readings[-1] == source._battery_floor


class _StopLoop(Exception):
    """Sentinel to break out of the (infinite) battery loop under test."""


def _make_adapter(info: DeviceInfo, readings: dict[str, int]) -> adapter_mod.DlMyoSource:
    """Build a DlMyoSource via ``__new__`` so no dl-myo install is needed."""
    source = adapter_mod.DlMyoSource.__new__(adapter_mod.DlMyoSource)
    source.info = info
    source._battery = lambda key, level: readings.__setitem__(key, level)
    return source


def test_adapter_battery_loop_reports_level(monkeypatch: pytest.MonkeyPatch) -> None:
    """The adapter's battery loop forwards levels; a failed read keeps the last one."""
    info = DeviceInfo(key="real", label="Real", mac="D2:3B:85:94:32:8E", name="Myo")
    readings: dict[str, int] = {}
    source = _make_adapter(info, readings)

    class FakeClient:
        def __init__(self) -> None:
            self.calls = 0

        async def battery_level(self) -> int:
            self.calls += 1
            if self.calls == 1:
                return 84
            raise RuntimeError("link dropped")

    # Break the loop after its first sleep so the test stays instant: the first
    # read has already reported 84, and the 60 s cadence is irrelevant here.
    async def fake_sleep(_seconds: float) -> None:
        raise _StopLoop

    monkeypatch.setattr(adapter_mod.asyncio, "sleep", fake_sleep)
    with contextlib.suppress(_StopLoop):
        asyncio.run(source._battery_loop(FakeClient()))
    assert readings == {"real": 84}

    # A failing first read must not crash the loop nor publish anything.
    class DeadClient:
        async def battery_level(self) -> int:
            raise RuntimeError("link dropped")

    silent: dict[str, int] = {}
    source2 = _make_adapter(info, silent)
    with contextlib.suppress(_StopLoop):
        asyncio.run(source2._battery_loop(DeadClient()))
    assert silent == {}
