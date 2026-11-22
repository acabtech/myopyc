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
