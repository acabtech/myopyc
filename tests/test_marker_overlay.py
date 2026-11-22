"""Offscreen Qt tests for marker overlay rendering on EMG/IMU plots."""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6 import QtWidgets

from myopyc.devices.model import DeviceInfo
from myopyc.recorder.markers import Marker
from myopyc.streams.live import LiveBuffer
from myopyc.streams.schema import EMG_DTYPE, IMU_DTYPE, StreamKind
from myopyc.ui.plots import DeviceView


@pytest.fixture(scope="module")
def qapp() -> QtWidgets.QApplication:
    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication([])
    assert isinstance(app, QtWidgets.QApplication)
    return app


def _emg(count: int = 40, t0: int = 1_000_000_000) -> np.ndarray:
    rows = np.zeros(count, dtype=EMG_DTYPE)
    rows["t_ns"] = t0 + np.arange(count) * 5_000_000  # 200 Hz
    rows["seq"] = np.arange(count)
    rows["ch1"] = np.arange(count)
    return rows


def _imu(count: int = 10, t0: int = 1_000_000_000) -> np.ndarray:
    rows = np.zeros(count, dtype=IMU_DTYPE)
    rows["t_ns"] = t0 + np.arange(count) * 20_000_000  # 50 Hz
    rows["seq"] = np.arange(count)
    rows["ax"] = np.linspace(-1.0, 1.0, count)
    return rows


def test_marker_lines_show_for_markers_in_window(qapp: QtWidgets.QApplication) -> None:
    live = LiveBuffer(max_samples=4096)
    info = DeviceInfo(key="left", label="Left", mac=None)
    view = DeviceView(info, live, emg_window_s=2.0, imu_window_s=4.0)

    t0 = 1_000_000_000
    emg = _emg(40, t0=t0)
    imu = _imu(10, t0=t0)
    from myopyc.devices.model import SampleBatch

    live.append(SampleBatch("left", StreamKind.EMG, emg))
    live.append(SampleBatch("left", StreamKind.IMU, imu))

    in_window = Marker(label="hit", t_ns=t0 + 100_000_000)  # +0.1s from start, within window
    out_window = Marker(label="old", t_ns=t0 - 10_000_000_000)  # far before
    view.set_markers([in_window, out_window])
    view.refresh()

    assert len(view.emg._marker_lines) == 2
    assert len(view.imu._marker_lines) == 2
    assert view.emg._marker_lines[0].isVisible() is True
    assert view.emg._marker_lines[1].isVisible() is False
    assert view.imu._marker_lines[0].isVisible() is True
    assert view.imu._marker_lines[1].isVisible() is False


def test_marker_positions_track_time_axis(qapp: QtWidgets.QApplication) -> None:
    live = LiveBuffer(max_samples=4096)
    info = DeviceInfo(key="left", label="Left", mac=None)
    view = DeviceView(info, live, emg_window_s=2.0, imu_window_s=4.0)

    t0 = 2_000_000_000
    from myopyc.devices.model import SampleBatch

    emg = _emg(40, t0=t0)  # ends at t0 + 195ms
    live.append(SampleBatch("left", StreamKind.EMG, emg))
    marker = Marker(label="m1", t_ns=t0 + 95_000_000)
    view.set_markers([marker])
    view.refresh()

    line = view.emg._marker_lines[0]
    assert line.isVisible() is True
    # x is seconds relative to window end (last sample)
    t_end_ns = int(emg["t_ns"][-1])
    expected_x = (marker.t_ns - t_end_ns) / 1e9
    assert line.pos().x() == pytest.approx(expected_x, abs=1e-6)


def test_set_markers_replaces_previous(qapp: QtWidgets.QApplication) -> None:
    live = LiveBuffer(max_samples=4096)
    info = DeviceInfo(key="left", label="Left", mac=None)
    view = DeviceView(info, live)
    view.set_markers([Marker("a", 1), Marker("b", 2), Marker("c", 3)])
    assert len(view.emg._marker_lines) == 3
    view.set_markers([Marker("x", 1)])
    assert len(view.emg._marker_lines) == 1
    assert view.emg._marker_lines[0].label == "x"
