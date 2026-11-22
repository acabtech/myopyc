"""Device manager tests, driven deterministically without real-time waits.

The simulator emits its first block and battery reading *synchronously* inside
:meth:`SimulatedSource.start`, so no sleeping is needed. State/counts/battery
paths that depend on stream callbacks are exercised by emitting synthetic
batches directly through :meth:`DeviceManager._emit`, keeping the suite fast.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys

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


def test_manager_accepts_reconnecting_state() -> None:
    """The BLE adapter's reconnecting state is a first-class DeviceState.

    The adapter reports ``reconnecting`` while its backoff loop retries a
    dropped link; the manager must store it (typed) so the UI can surface it
    instead of showing a stale ``streaming`` state forever.
    """

    manager = DeviceManager()
    info = manager.add_simulated("Left")
    manager._on_status(info.key, "streaming", "raw EMG + IMU")
    manager._on_status(info.key, "reconnecting", "link dropped")
    assert manager.state(info.key) == "reconnecting"
    assert manager.detail(info.key) == "link dropped"


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


def test_merge_saved_bindings_adds_remembered_and_dedupes() -> None:
    from myopyc.devices.discovery import DiscoveredMyo, merge_saved_bindings

    # Nothing discovered: saved bindings still show up, flagged as remembered.
    merged = merge_saved_bindings([], {"EB:A0:71:06:1D:FD": "Right"})
    assert len(merged) == 1
    assert merged[0].mac == "EB:A0:71:06:1D:FD"
    assert merged[0].name == "Right"
    assert merged[0].remembered is True

    # Discovered devices win; saved bindings for them must not duplicate.
    seen = [DiscoveredMyo("EB:A0:71:06:1D:FD", "Sam's Myo", False)]
    merged = merge_saved_bindings(seen, {"EB:A0:71:06:1D:FD": "Right"})
    assert len(merged) == 1
    assert merged[0].name == "Sam's Myo"
    assert merged[0].remembered is False


def test_reconnect_delay_policy_is_capped_exponential() -> None:
    assert adapter_mod.reconnect_delay_s(1) == 2.0
    assert adapter_mod.reconnect_delay_s(2) == 4.0
    assert adapter_mod.reconnect_delay_s(3) == 8.0
    assert adapter_mod.reconnect_delay_s(4) == 16.0
    assert adapter_mod.reconnect_delay_s(5) == 30.0
    assert adapter_mod.reconnect_delay_s(99) == 30.0


def test_sleep_before_reconnect_wakes_on_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    info = DeviceInfo(key="real", label="Real", mac="D2:3B:85:94:32:8E", name="Myo")
    source = adapter_mod.DlMyoSource.__new__(adapter_mod.DlMyoSource)
    source.info = info
    source._stop_requested = False

    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        source._stop_requested = True

    monkeypatch.setattr(adapter_mod.time, "sleep", fake_sleep)
    monkeypatch.setattr(adapter_mod.time, "monotonic", lambda: 0.0)

    source._sleep_before_reconnect(30.0)
    assert sleeps == [0.25]


def test_thread_main_retries_and_reports_reconnecting(monkeypatch: pytest.MonkeyPatch) -> None:
    info = DeviceInfo(key="real", label="Real", mac="D2:3B:85:94:32:8E", name="Myo")
    statuses: list[tuple[str, str, str | None]] = []
    source = adapter_mod.DlMyoSource.__new__(adapter_mod.DlMyoSource)
    source.info = info
    source._status = lambda key, state, detail: statuses.append((key, state, detail))
    source._stop_requested = False
    source._loop = None
    source._stop_event = None

    calls = {"n": 0}
    delays: list[float] = []

    class FakeLoop:
        def run_until_complete(self, coro):
            # Ignore shutdown_asyncgens()'s noop coroutine in the finally block.
            name = getattr(coro, "cr_code", None)
            if getattr(name, "co_name", "") == "_noop":
                coro.close()
                return None
            calls["n"] += 1
            coro.close()
            if calls["n"] == 1:
                raise RuntimeError("link dropped")
            return True

        def is_running(self) -> bool:
            return False

        def shutdown_asyncgens(self):
            async def _noop():
                return None

            return _noop()

        def close(self) -> None:
            return None

    monkeypatch.setattr(adapter_mod.asyncio, "new_event_loop", lambda: FakeLoop())
    monkeypatch.setattr(adapter_mod.asyncio, "set_event_loop", lambda _loop: None)
    monkeypatch.setattr(adapter_mod, "reconnect_delay_s", lambda attempt: 2.0 if attempt == 1 else 4.0)
    monkeypatch.setattr(source, "_sleep_before_reconnect", lambda delay: delays.append(delay))

    source._thread_main()

    assert calls["n"] == 2
    assert delays == [2.0]
    assert ("real", "reconnecting", "link dropped") in statuses


# --------------------------------------------------------------------- events
# Pose -> EVENT type decoding in DlMyoSource. The classifier notification only
# carries a pose; the orientation payload is the last IMU quaternion.


class _FakeEventType:
    """Duck-typed stand-in for ``myo.types.ClassifierEventType``."""

    POSE = "pose"


class _FakePose:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeClassifierEvent:
    """Duck-typed stand-in for ``myo.types.ClassifierEvent``."""

    def __init__(self, event_type: object, pose_name: str | None = None) -> None:
        self.t = event_type
        if pose_name is not None:
            self.pose = _FakePose(pose_name)


@pytest.fixture()
def _real_classifier_enum(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the decoder's ``myo.types`` import at the fake enum.

    The real import happens inside ``_on_classifier_event`` (dl-myo is an
    optional dependency), so the monkeypatch swaps ``myo.types`` wholesale.
    """

    import types as _types

    fake_types = _types.ModuleType("myo.types")
    fake_types.ClassifierEventType = _FakeEventType  # type: ignore[attr-defined]
    fake_myos = _types.ModuleType("myo")
    fake_myos.types = fake_types  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "myo", fake_myos)
    monkeypatch.setitem(sys.modules, "myo.types", fake_types)


def _make_event_source(events: list[SampleBatch]) -> adapter_mod.DlMyoSource:
    info = DeviceInfo(key="real", label="Real", mac="D2:3B:85:94:32:8E", name="Myo")
    source = adapter_mod.DlMyoSource.__new__(adapter_mod.DlMyoSource)
    source.info = info
    source._emit = events.append
    source._event_seq = 0
    source._last_orientation = None
    return source


def test_adapter_pose_event_decode_and_orientation_payload(
    _real_classifier_enum: None,
) -> None:
    """A POSE classifier event becomes an EVENT row with the last IMU quaternion."""

    events: list[SampleBatch] = []
    source = _make_event_source(events)
    # Feed an IMU quaternion first, as the BLE stream would.
    source._last_orientation = (0.9, 0.1, 0.2, 0.3)

    source._on_classifier_event(_FakeClassifierEvent(_FakeEventType.POSE, "FIST"))

    assert len(events) == 1
    batch = events[0]
    assert batch.device == "real"
    assert batch.kind is StreamKind.EVENT
    row = batch.data[0]
    assert int(row["type"]) == EVENT_TYPE_IDS["fist"]
    assert int(row["seq"]) == 0
    assert (
        float(row["ow"]),
        float(row["ox"]),
        float(row["oy"]),
        float(row["oz"]),
    ) == pytest.approx((0.9, 0.1, 0.2, 0.3))

    # A second pose increments the event sequence counter, mirroring EMG/IMU.
    source._on_classifier_event(_FakeClassifierEvent(_FakeEventType.POSE, "WAVE_IN"))
    assert len(events) == 2
    assert int(events[1].data[0]["type"]) == EVENT_TYPE_IDS["wave_left"]
    assert int(events[1].data[0]["seq"]) == 1


def test_adapter_pose_without_imu_gets_identity_orientation(
    _real_classifier_enum: None,
) -> None:
    """A pose before the first IMU sample records an identity quaternion."""

    events: list[SampleBatch] = []
    source = _make_event_source(events)
    source._on_classifier_event(_FakeClassifierEvent(_FakeEventType.POSE, "DOUBLE_TAP"))
    assert len(events) == 1
    row = events[0].data[0]
    assert float(row["ow"]) == 1.0
    assert float(row["ox"]) == 0.0
    assert float(row["oy"]) == 0.0
    assert float(row["oz"]) == 0.0


def test_adapter_non_pose_classifier_events_are_dropped(
    _real_classifier_enum: None,
) -> None:
    """Arm-sync/unlock style events are noise; nothing is emitted."""

    events: list[SampleBatch] = []
    source = _make_event_source(events)
    source._on_classifier_event(_FakeClassifierEvent("arm_synced"))
    source._on_classifier_event(_FakeClassifierEvent("unlocked"))
    assert events == []


def test_adapter_unknown_pose_name_is_dropped(_real_classifier_enum: None) -> None:
    """A pose name outside the map (new firmware) must not emit a bad code."""

    events: list[SampleBatch] = []
    source = _make_event_source(events)
    source._on_classifier_event(_FakeClassifierEvent(_FakeEventType.POSE, "SPREAD_FINGERS_PRO"))
    assert events == []
    # The sequence counter stays untouched so ordering never skips.
    assert source._event_seq == 0
