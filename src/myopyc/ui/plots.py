"""Live device panels: EMG oscilloscope and IMU traces."""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6 import QtWidgets

from myopyc.devices.model import DeviceInfo
from myopyc.streams.live import LiveBuffer
from myopyc.streams.schema import EMG_CHANNELS, StreamKind

CHANNEL_PENS = [
    pg.mkPen(pg.intColor(index, hues=EMG_CHANNELS), width=1) for index in range(EMG_CHANNELS)
]


class EmgPlot(pg.PlotWidget):
    def __init__(self, window_s: float, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent=parent)
        self.window_s = window_s
        self.setLabel("left", "EMG", units="int8")
        self.setLabel("bottom", "time", units="s")
        self.setYRange(-128, 127, padding=0.05)
        self.showGrid(x=False, y=True, alpha=0.15)
        self.setMouseEnabled(x=False, y=False)
        self.setMenuEnabled(False)
        self._curves = [self.plot(pen=CHANNEL_PENS[index]) for index in range(EMG_CHANNELS)]

    def update_data(self, samples: np.ndarray) -> None:
        if samples.size == 0:
            return
        limit = max(1, int(200 * self.window_s))
        samples = np.ascontiguousarray(samples[-limit:])
        times = np.ascontiguousarray((samples["t_ns"] - samples["t_ns"][-1]) / 1e9)
        for index, curve in enumerate(self._curves):
            values = np.ascontiguousarray(samples[f"ch{index + 1}"], dtype=np.float32)
            curve.setData(times, values)


class ImuPlot(pg.PlotWidget):
    def __init__(self, window_s: float, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent=parent)
        self.window_s = window_s
        self.setLabel("left", "accel", units="g")
        self.setLabel("bottom", "time", units="s")
        self.setYRange(-2.0, 2.0, padding=0.05)
        self.showGrid(x=False, y=True, alpha=0.15)
        self.setMouseEnabled(x=False, y=False)
        self.setMenuEnabled(False)
        self._curves = [
            self.plot(pen=pg.mkPen("#ff6b6b", width=1)),
            self.plot(pen=pg.mkPen("#6bff95", width=1)),
            self.plot(pen=pg.mkPen("#6bb5ff", width=1)),
        ]

    def update_data(self, samples: np.ndarray) -> None:
        if samples.size == 0:
            return
        limit = max(1, int(50 * self.window_s))
        samples = np.ascontiguousarray(samples[-limit:])
        times = np.ascontiguousarray((samples["t_ns"] - samples["t_ns"][-1]) / 1e9)
        for index, axis in enumerate(("ax", "ay", "az")):
            self._curves[index].setData(
                times, np.ascontiguousarray(samples[axis], dtype=np.float32)
            )


class DeviceView(QtWidgets.QGroupBox):
    """One armband: status header plus EMG and IMU plots."""

    def __init__(
        self,
        info: DeviceInfo,
        live: LiveBuffer,
        *,
        emg_window_s: float = 2.0,
        imu_window_s: float = 4.0,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent=parent)
        self.info = info
        self.live = live
        self.setTitle(info.label)

        self.status_label = QtWidgets.QLabel("idle")
        self.status_label.setStyleSheet("color: #9aa4b2;")
        self.battery_label = QtWidgets.QLabel("")
        self.battery_label.setStyleSheet("color: #9aa4b2;")
        self.orientation_label = QtWidgets.QLabel("")
        self.orientation_label.setStyleSheet("color: #6f7a89; font-size: 11px;")

        header = QtWidgets.QHBoxLayout()
        header.addWidget(self.status_label)
        header.addStretch(1)
        header.addWidget(self.orientation_label)
        header.addSpacing(12)
        header.addWidget(self.battery_label)

        self.emg = EmgPlot(emg_window_s)
        self.imu = ImuPlot(imu_window_s)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(header)
        layout.addWidget(self.emg, stretch=3)
        layout.addWidget(self.imu, stretch=2)

    def update_display(self, state: str, detail: str | None, battery: int | None) -> None:
        text = state
        if detail:
            text = f"{state} - {detail}"
        self.status_label.setText(text)
        color = {
            "streaming": "#48d1cc",
            "error": "#ff6b6b",
            "connecting": "#ffd166",
            "stopping": "#ffd166",
        }.get(state, "#9aa4b2")
        self.status_label.setStyleSheet(f"color: {color};")
        if battery is None:
            self.battery_label.setText("")
        else:
            self.battery_label.setText(f"battery {battery}%")
            if battery <= 15:
                color = "#ff6b6b"
            elif battery <= 40:
                color = "#ffd166"
            else:
                color = "#48d1cc"
            self.battery_label.setStyleSheet(f"color: {color};")

    def refresh(self) -> None:
        emg = self.live.snapshot(self.info.key, StreamKind.EMG)
        imu = self.live.snapshot(self.info.key, StreamKind.IMU)
        self.emg.update_data(emg)
        self.imu.update_data(imu)
        if imu.size:
            last = imu[-1]
            self.orientation_label.setText(
                f"q=({last['qw']:.2f}, {last['qx']:.2f}, {last['qy']:.2f}, {last['qz']:.2f})"
            )
