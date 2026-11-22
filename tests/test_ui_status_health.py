"""Headless (offscreen) smoke tests for the Qt wiring.

Covers the status-bar stream-health glue (:meth:`MainWindow.refresh_health`)
and the configured plot-window durations. The rate math itself is covered
purely in ``tests/test_health.py``. Skipped if PySide6/pyqtgraph are missing;
CI sets ``QT_QPA_PLATFORM=offscreen``.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6 import QtWidgets

from myopyc.config import AppConfig
from myopyc.devices.model import SampleBatch
from myopyc.streams.schema import EMG_DTYPE, EVENT_DTYPE, IMU_DTYPE, StreamKind
from myopyc.ui.main_window import MainWindow


@pytest.fixture(scope="module")
def qapp() -> QtWidgets.QApplication:
    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication([])
    assert isinstance(app, QtWidgets.QApplication)
    return app


@pytest.fixture(autouse=True)
def no_ble_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep MainWindow from probing real Bluetooth hardware in tests.

    The constructor spawns an auto-discovery thread when dl-myo is
    importable; its status callbacks race the monkeypatched clock used by
    the health-refresh test (StopIteration) and touch live hardware.
    """
    monkeypatch.setattr("myopyc.ui.main_window.dl_myo_available", lambda: False)


def _emg(device: str, count: int) -> SampleBatch:
    rows = np.zeros(count, dtype=EMG_DTYPE)
    rows["seq"] = np.arange(count)
    return SampleBatch(device, StreamKind.EMG, rows)


def _imu(device: str, count: int) -> SampleBatch:
    rows = np.zeros(count, dtype=IMU_DTYPE)
    rows["seq"] = np.arange(count)
    return SampleBatch(device, StreamKind.IMU, rows)


def _config() -> AppConfig:
    config = AppConfig()
    # No auto-start so the constructor does not spin up simulator threads, and
    # no device bindings are persisted during the test.
    config.ui.auto_start_streaming = False
    return config


def _close(window: MainWindow) -> None:
    window.manager.stop_all()
    window._health_timer.stop()
    window._timer.stop()


def test_status_bar_has_permanent_health_widget(qapp: QtWidgets.QApplication) -> None:
    window = MainWindow(_config(), simulate=0)
    try:
        assert isinstance(window._status_label, QtWidgets.QLabel)
        # It is added as a permanent widget so transient messages do not hide it.
        assert window._status_label in window.statusBar().children()
    finally:
        _close(window)


def test_idle_hint_when_no_devices(qapp: QtWidgets.QApplication) -> None:
    window = MainWindow(_config(), simulate=0)
    try:
        assert window._status_label is not None
        line = window.refresh_health()
        assert line == "add a simulated arm or connect a Myo"
        assert window._status_label.text() == line
    finally:
        _close(window)


def test_refresh_reports_rates_and_drops(
    qapp: QtWidgets.QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = MainWindow(_config(), simulate=0)
    try:
        assert window._status_label is not None
        info = window.manager.add_simulated("Left")
        key = info.key

        # Drive the tracker with controlled wall-clock and growing counters.
        clock = iter([0.0, 1.0])
        monkeypatch.setattr("myopyc.ui.main_window.time.monotonic", lambda: next(clock))

        # First window: prime with a full second of samples (idle -> baseline set).
        window.manager._emit(_emg(key, 200))
        window.manager._emit(_imu(key, 50))
        first = window.refresh_health()
        # The simulated device is never started in this test, so its idle
        # connection state is tagged in the line (non-default states are).
        assert "Left [idle]" in first and "dropped 0" in first
        assert not first.startswith("⚠")  # idle state alone is not a warning

        # Second window: another full second of samples -> observed == expected.
        window.manager._emit(_emg(key, 200))
        window.manager._emit(_imu(key, 50))
        second = window.refresh_health()
        assert "emg 200/200 Hz" in second
        assert "imu 50/50 Hz" in second
        assert "⚠" not in second
        assert window._status_label.text() == second
    finally:
        _close(window)


@pytest.mark.parametrize(
    ("emg_window_s", "imu_window_s", "expected_emg", "expected_imu"),
    [
        pytest.param(3.5, 6.0, 3.5, 6.0, id="configured"),
        pytest.param(None, None, 2.0, 4.0, id="defaults"),
    ],
)
def test_device_view_window_durations(
    qapp: QtWidgets.QApplication,
    emg_window_s: float | None,
    imu_window_s: float | None,
    expected_emg: float,
    expected_imu: float,
) -> None:
    """Config.ui plot-window settings are honored; defaults apply otherwise."""
    config = _config()
    if emg_window_s is not None:
        config.ui.emg_window_s = emg_window_s
    if imu_window_s is not None:
        config.ui.imu_window_s = imu_window_s
    window = MainWindow(config, simulate=0)
    try:
        info = window.manager.add_simulated("Left")
        window._mount_view(info)
        view = window._views[info.key]
        assert view.emg.window_s == expected_emg
        assert view.imu.window_s == expected_imu
    finally:
        _close(window)


def test_device_view_shows_reconnecting_state(qapp: QtWidgets.QApplication) -> None:
    """A dropped BLE link is surfaced in the device panel header in amber."""

    window = MainWindow(_config(), simulate=0)
    try:
        info = window.manager.add_simulated("Left")
        window._mount_view(info)
        view = window._views[info.key]

        view.update_display("reconnecting", "link dropped", None)
        assert view.status_label.text() == "reconnecting - link dropped"
        assert "ff9f43" in view.status_label.styleSheet()  # the amber reconnecting color

        # Once the link returns, the header flips back to the teal streaming.
        view.update_display("streaming", "raw EMG + IMU", 80)
        assert view.status_label.text() == "streaming - raw EMG + IMU"
        assert "48d1cc" in view.status_label.styleSheet()
    finally:
        _close(window)


def _event(device: str, type_code: int, count: int = 1) -> SampleBatch:
    rows = np.zeros(count, dtype=EVENT_DTYPE)
    rows["t_ns"] = 1_000_000_000 + np.arange(count) * 1_000_000
    rows["seq"] = np.arange(count)
    rows["type"] = type_code
    rows["ow"] = 1.0
    return SampleBatch(device, StreamKind.EVENT, rows)


def test_gesture_annotation_flashes_on_event(qapp: QtWidgets.QApplication) -> None:
    """A fired pose flashes the amber gesture label on the device panel."""

    from myopyc.streams.schema import EVENT_TYPE_IDS

    window = MainWindow(_config(), simulate=0)
    try:
        info = window.manager.add_simulated("Left")
        window._mount_view(info)
        view = window._views[info.key]
        assert view.gesture_label.text() == ""

        window._on_samples(_event(info.key, EVENT_TYPE_IDS["fist"]))
        assert view.gesture_label.text() == "gesture: fist"
        assert "ffd166" in view.gesture_label.styleSheet()  # the flash amber

        # A second event updates the annotation to the newest pose.
        window._on_samples(_event(info.key, EVENT_TYPE_IDS["wave_left"]))
        assert view.gesture_label.text() == "gesture: wave_left"
    finally:
        _close(window)


def test_gesture_annotation_suppression_toggle(qapp: QtWidgets.QApplication) -> None:
    """Unchecking 'Gesture annotations' silences EVENT announcements."""

    from myopyc.streams.schema import EVENT_TYPE_IDS

    window = MainWindow(_config(), simulate=0)
    try:
        info = window.manager.add_simulated("Left")
        window._mount_view(info)
        view = window._views[info.key]

        window.gesture_action.setChecked(False)
        assert window.config.ui.gesture_annotations is False

        window._on_samples(_event(info.key, EVENT_TYPE_IDS["double_click"]))
        assert view.gesture_label.text() == ""  # suppressed entirely

        # Re-enabling resumes the flash.
        window.gesture_action.setChecked(True)
        assert window.config.ui.gesture_annotations is True
        window._on_samples(_event(info.key, EVENT_TYPE_IDS["fist"]))
        assert view.gesture_label.text() == "gesture: fist"
    finally:
        _close(window)


def test_gesture_annotation_config_persisted(qapp: QtWidgets.QApplication, tmp_path) -> None:
    """The toggle survives a config save/reload round trip."""

    from myopyc.config import load_config, save_config

    window = MainWindow(_config(), simulate=0)
    try:
        window.gesture_action.setChecked(False)
        path = tmp_path / "config.toml"
        save_config(window.config, path)
        reloaded = load_config(path)
        assert reloaded.ui.gesture_annotations is False
    finally:
        _close(window)


# ------------------------------------------------------------------ markers
def _recording_window(qapp: QtWidgets.QApplication, tmp_path) -> MainWindow:
    """A MainWindow with a real (simulated-device) recording running."""

    window = MainWindow(_config(), simulate=0)
    window.output_dir = tmp_path
    info = window.manager.add_simulated("Left")
    window.record_action.setChecked(True)
    assert window._recorder is not None
    _ = info
    return window


def test_marker_shortcut_number_row(qapp: QtWidgets.QApplication, tmp_path) -> None:
    """``M <digit>`` drops a marker into the open session's manifest."""

    window = _recording_window(qapp, tmp_path)
    try:
        assert len(window._marker_shortcuts) == 9
        assert window._inject_marker("marker-1")
        window._stop_recording()
        assert window._recorder is None

        sessions = window.saved_sessions()
        assert len(sessions) == 1
        from myopyc.recorder.reader import SessionReader

        markers = SessionReader(sessions[0]).markers
        assert [m.label for m in markers] == ["marker-1"]
        assert markers[0].t_ns > 0
    finally:
        _close(window)


def test_marker_without_recording_is_noop(qapp: QtWidgets.QApplication, tmp_path) -> None:
    """Without a recording the shortcut explains itself and records nothing."""

    window = MainWindow(_config(), simulate=0)
    window.output_dir = tmp_path
    try:
        assert not window._inject_marker("marker-3")
        assert window.saved_sessions() == []
    finally:
        _close(window)


# ------------------------------------------------------------------ derived channels
def test_derived_toggle_applies_to_open_views(qapp: QtWidgets.QApplication) -> None:
    """Toggling 'Derived channels' flips the EMG overlay visibility live."""

    window = MainWindow(_config(), simulate=0)
    try:
        info = window.manager.add_simulated("Left")
        window._mount_view(info)
        view = window._views[info.key]

        # Off is the default; no derived curve is visible.
        assert window.derived_action.isChecked() is False
        assert not any(c.isVisible() for c in view.emg._derived_curves)

        window.derived_action.setChecked(True)
        assert window.config.ui.derived_channels is True
        assert all(c.isVisible() for c in view.emg._derived_curves)

        window.derived_action.setChecked(False)
        assert window.config.ui.derived_channels is False
        assert not any(c.isVisible() for c in view.emg._derived_curves)
    finally:
        _close(window)


def test_derived_toggle_config_persisted(qapp: QtWidgets.QApplication, tmp_path) -> None:
    """The toggle survives a config save/reload round trip."""

    from myopyc.config import load_config, save_config

    window = MainWindow(_config(), simulate=0)
    try:
        window.derived_action.setChecked(True)
        path = tmp_path / "config.toml"
        save_config(window.config, path)
        reloaded = load_config(path)
        assert reloaded.ui.derived_channels is True
    finally:
        _close(window)


def test_derived_overlays_follow_live_data(qapp: QtWidgets.QApplication) -> None:
    """With the toggle on, rectified/RMS curves get data on refresh; raw curves untouched."""

    window = MainWindow(_config(), simulate=0)
    try:
        info = window.manager.add_simulated("Left")
        window._mount_view(info)
        view = window._views[info.key]
        window.derived_action.setChecked(True)
        # Isolate this legacy rectified/RMS assertion from notch behavior.
        window.notch_action.setChecked(False)

        batch = _emg(info.key, 100)
        # A constant full-scale signal on every channel: the RMS envelope of a
        # constant is that constant (in the window-steady middle region).
        for ch in (f"ch{i}" for i in range(1, 9)):
            batch.data[ch] = np.int8(64)
        window.manager._emit(batch)
        view.refresh()

        raw_x, raw_y = view.emg._curves[0].getData()
        rect_x, rect_y = view.emg._derived_curves[0].getData()
        rms_x, rms_y = view.emg._rms_curve.getData()
        assert rect_x is not None and len(rect_x) == len(raw_x)
        assert np.array_equal(rect_y, np.abs(np.asarray(raw_y, dtype=np.float32)))
        assert rms_x is not None and len(rms_x) == len(raw_x)
        # Middle of the window: the moving average is fully populated.
        assert np.allclose(rms_y[len(rms_y) // 2], 64.0, atol=1.0)
    finally:
        _close(window)


def test_notch_toggle_applies_to_derived_overlays(qapp: QtWidgets.QApplication) -> None:
    """Notching attenuates a 60 Hz component in derived overlays only."""

    window = MainWindow(_config(), simulate=0)
    try:
        info = window.manager.add_simulated("Left")
        window._mount_view(info)
        view = window._views[info.key]
        window.derived_action.setChecked(True)
        window.notch_action.setChecked(False)

        batch = _emg(info.key, 400)
        t = np.arange(batch.data.shape[0], dtype=np.float32) / 200.0
        sine = (80.0 * np.sin(2.0 * np.pi * 60.0 * t)).astype(np.int8)
        for ch in (f"ch{i}" for i in range(1, 9)):
            batch.data[ch] = sine
        window.manager._emit(batch)
        view.refresh()

        _x, rect_no_notch = view.emg._derived_curves[0].getData()
        assert rect_no_notch is not None
        tail_no_notch = np.asarray(rect_no_notch[-100:], dtype=np.float32)

        window.notch_action.setChecked(True)
        view.refresh()
        _x, rect_notch = view.emg._derived_curves[0].getData()
        assert rect_notch is not None
        tail_notch = np.asarray(rect_notch[-100:], dtype=np.float32)

        assert float(np.mean(tail_notch)) < float(np.mean(tail_no_notch)) * 0.25
        # Raw EMG curve remains the original signal; notching is overlay-only.
        _raw_x, raw_y = view.emg._curves[0].getData()
        assert np.array_equal(np.asarray(raw_y, dtype=np.int8), sine[-len(raw_y) :])
    finally:
        _close(window)


def test_notch_toggle_config_persisted(qapp: QtWidgets.QApplication, tmp_path) -> None:
    """The notch toggle survives a config save/reload round trip."""

    from myopyc.config import load_config, save_config

    window = MainWindow(_config(), simulate=0)
    try:
        window.notch_action.setChecked(False)
        path = tmp_path / "config.toml"
        save_config(window.config, path)
        reloaded = load_config(path)
        assert reloaded.ui.notch_hz == 0.0
    finally:
        _close(window)


def test_derived_toggle_clears_curves_when_hidden(qapp: QtWidgets.QApplication) -> None:
    """Hiding the overlays clears stale geometry so re-enabling shows no old data."""

    window = MainWindow(_config(), simulate=0)
    try:
        info = window.manager.add_simulated("Left")
        window._mount_view(info)
        view = window._views[info.key]
        window.derived_action.setChecked(True)
        window.manager._emit(_emg(info.key, 100))
        view.refresh()
        _x, rect_y = view.emg._derived_curves[0].getData()
        assert rect_y is not None and len(rect_y) > 0

        window.derived_action.setChecked(False)
        _x, rect_y = view.emg._derived_curves[0].getData()
        # pyqtgraph may keep either an empty array or None after setData([]).
        assert rect_y is None or len(rect_y) == 0
    finally:
        _close(window)


# ------------------------------------------------------------------ overflow warning
class _FakeRecorder:
    def __init__(self, dropped: int = 0) -> None:
        self._dropped = dropped

    def stats(self) -> dict[str, int]:
        return {
            "dropped_samples": self._dropped,
            "received_samples": 0,
            "buffered_bytes": 0,
            "chunks_written": 0,
            "gaps": 0,
        }


def test_sustained_overflow_warning_appears_and_clears(
    qapp: QtWidgets.QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Status bar warns only on sustained drop growth, then clears when stable."""

    window = MainWindow(_config(), simulate=0)
    try:
        info = window.manager.add_simulated("Left")
        window._mount_view(info)
        key = info.key
        fake = _FakeRecorder(dropped=0)
        window._recorder = fake  # type: ignore[assignment]

        clock = iter([0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0])
        monkeypatch.setattr("myopyc.ui.main_window.time.monotonic", lambda: next(clock))

        # Prime baseline.
        window.manager._emit(_emg(key, 200))
        window.manager._emit(_imu(key, 50))
        line = window.refresh_health()
        assert "RAM overflow dropping samples" not in line

        # First increase: not yet sustained.
        fake._dropped = 10
        window.manager._emit(_emg(key, 200))
        window.manager._emit(_imu(key, 50))
        line = window.refresh_health()
        assert "RAM overflow dropping samples" not in line
        assert "dropped 10" in line

        # Second consecutive increase: sustained warning.
        fake._dropped = 20
        window.manager._emit(_emg(key, 200))
        window.manager._emit(_imu(key, 50))
        line = window.refresh_health()
        assert line.startswith("⚠ RAM overflow dropping samples")
        assert "dropped 20" in line

        # Stable drops: warning remains until stable window elapses.
        for dropped in (20, 20, 20):
            fake._dropped = dropped
            window.manager._emit(_emg(key, 200))
            window.manager._emit(_imu(key, 50))
            line = window.refresh_health()
        assert "RAM overflow dropping samples" not in line
        assert "dropped 20" in line
    finally:
        window._recorder = None
        _close(window)


def test_no_overflow_warning_when_not_recording(qapp: QtWidgets.QApplication) -> None:
    """Without an active recorder, drop counter is reported but no overflow warning."""

    window = MainWindow(_config(), simulate=0)
    try:
        info = window.manager.add_simulated("Left")
        window._mount_view(info)
        line = window.refresh_health()
        assert "RAM overflow dropping samples" not in line
        assert "dropped 0" in line
    finally:
        _close(window)
