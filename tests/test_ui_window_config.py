"""Verify config.ui plot window settings are honored end-to-end in the GUI wiring.

Runs headlessly (CI sets ``QT_QPA_PLATFORM=offscreen``); skipped if PySide6 or
pyqtgraph cannot be imported.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets

from myopyc.config import AppConfig
from myopyc.ui.main_window import MainWindow


@pytest.fixture(scope="module")
def qapp() -> QtWidgets.QApplication:
    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication([])
    assert isinstance(app, QtWidgets.QApplication)
    return app


def _window(config: AppConfig) -> MainWindow:
    config.ui.auto_start_streaming = False
    return MainWindow(config, simulate=0)


def test_device_view_uses_configured_window_durations(
    qapp: QtWidgets.QApplication,
) -> None:
    config = AppConfig()
    config.ui.emg_window_s = 3.5
    config.ui.imu_window_s = 6.0
    window = _window(config)
    try:
        info = window.manager.add_simulated("Left")
        window._mount_view(info)
        view = window._views[info.key]
        assert view.emg.window_s == 3.5
        assert view.imu.window_s == 6.0
    finally:
        window.manager.stop_all()
        window._health_timer.stop()
        window._timer.stop()


def test_default_window_durations_when_unconfigured(
    qapp: QtWidgets.QApplication,
) -> None:
    window = _window(AppConfig())
    try:
        info = window.manager.add_simulated("Left")
        window._mount_view(info)
        view = window._views[info.key]
        assert view.emg.window_s == 2.0
        assert view.imu.window_s == 4.0
    finally:
        window.manager.stop_all()
        window._health_timer.stop()
        window._timer.stop()
