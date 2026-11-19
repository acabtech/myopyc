from __future__ import annotations

import time

from myopyc.devices.manager import DeviceManager
from myopyc.devices.model import SampleBatch
from myopyc.streams.schema import StreamKind


def _wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_simulated_device_streams_emg_and_imu() -> None:
    manager = DeviceManager()
    batches: list[SampleBatch] = []
    manager.subscribe(batches.append)
    info = manager.add_simulated("Left")
    manager.start_all()
    try:
        assert _wait_until(
            lambda: (
                any(batch.kind is StreamKind.EMG for batch in batches)
                and any(batch.kind is StreamKind.IMU for batch in batches)
            )
        )
        assert manager.state(info.key) == "streaming"
        counts = manager.counts(info.key)
        assert counts[StreamKind.EMG] > 0
        assert counts[StreamKind.IMU] > 0
    finally:
        manager.stop_all()

    assert manager.state(info.key) == "stopped"
    assert manager.meta()[info.key]["label"] == "Left"


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
        assert _wait_until(lambda: manager.battery(info.key) is not None)
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
