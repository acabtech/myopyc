"""Device manager tests, driven deterministically without real-time waits.

The simulator emits its first block and battery reading *synchronously* inside
:meth:`SimulatedSource.start`, so no sleeping is needed. State/counts/battery
paths that depend on stream callbacks are exercised by emitting synthetic
batches directly through :meth:`DeviceManager._emit`, keeping the suite fast.
"""

from __future__ import annotations

import numpy as np

from myopyc.devices.manager import DeviceManager
from myopyc.devices.model import DeviceInfo, SampleBatch
from myopyc.devices.simulator import SimulatedSource
from myopyc.streams.schema import EMG_DTYPE, IMU_DTYPE, StreamKind


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
