"""Offscreen smoke test for the status-bar stream-health wiring.

Run headlessly (CI sets ``QT_QPA_PLATFORM=offscreen``); skipped if PySide6 or
pyqtgraph cannot be imported. This exercises the thin GUI glue in
:meth:`MainWindow.refresh_health`, while the rate math itself is covered
purely in ``tests/test_health.py``.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets

from myopyc.config import AppConfig
from myopyc.devices.model import SampleBatch
from myopyc.streams.schema import EMG_DTYPE, IMU_DTYPE, StreamKind
from myopyc.ui.main_window import MainWindow


@pytest.fixture(scope="module")
def qapp() -> QtWidgets.QApplication:
    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication([])
    assert isinstance(app, QtWidgets.QApplication)
    return app


def _emg(device: str, count: int) -> SampleBatch:
    import numpy as np

    rows = np.zeros(count, dtype=EMG_DTYPE)
    rows["seq"] = np.arange(count)
    return SampleBatch(device, StreamKind.EMG, rows)


def _imu(device: str, count: int) -> SampleBatch:
    import numpy as np

    rows = np.zeros(count, dtype=IMU_DTYPE)
    rows["seq"] = np.arange(count)
    return SampleBatch(device, StreamKind.IMU, rows)


def _config() -> AppConfig:
    config = AppConfig()
    # No auto-start so the constructor does not spin up simulator threads, and
    # no device bindings are persisted during the test.
    config.ui.auto_start_streaming = False
    return config


def test_status_bar_has_permanent_health_widget(qapp: QtWidgets.QApplication) -> None:
    window = MainWindow(_config(), simulate=0)
    try:
        assert isinstance(window._status_label, QtWidgets.QLabel)
        # It is added as a permanent widget so transient messages do not hide it.
        assert window._status_label in window.statusBar().children()
    finally:
        window.manager.stop_all()
        window._health_timer.stop()
        window._timer.stop()


def test_idle_hint_when_no_devices(qapp: QtWidgets.QApplication) -> None:
    window = MainWindow(_config(), simulate=0)
    try:
        assert window._status_label is not None
        line = window.refresh_health()
        assert line == "add a simulated arm or connect a Myo"
        assert window._status_label.text() == line
    finally:
        window.manager.stop_all()
        window._health_timer.stop()
        window._timer.stop()


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
        assert "Left:" in first and "dropped 0" in first

        # Second window: another full second of samples -> observed == expected.
        window.manager._emit(_emg(key, 200))
        window.manager._emit(_imu(key, 50))
        second = window.refresh_health()
        assert "emg 200/200 Hz" in second
        assert "imu 50/50 Hz" in second
        assert "⚠" not in second
        assert window._status_label.text() == second
    finally:
        window.manager.stop_all()
        window._health_timer.stop()
        window._timer.stop()
