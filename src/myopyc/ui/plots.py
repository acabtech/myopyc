"""Live device panels: EMG oscilloscope and IMU traces."""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtWidgets

from myopyc.devices.model import DeviceInfo
from myopyc.streams.live import LiveBuffer
from myopyc.streams.schema import EMG_CHANNELS, StreamKind

CHANNEL_PENS = [
    pg.mkPen(pg.intColor(index, hues=EMG_CHANNELS), width=1) for index in range(EMG_CHANNELS)
]

# Derived-channel overlays drawn on top of the raw EMG traces. Distinct
# greys/weights so the raw per-channel colors stay unambiguous underneath.
RMS_PEN = pg.mkPen("#e8e8e8", width=2)
RECT_PEN = pg.mkPen("#8a8a8a", width=1, style=QtCore.Qt.PenStyle.DashLine)


class EmgPlot(pg.PlotWidget):
    #: RMS envelope window in samples (EMG runs at 200 Hz; 50 samples = 250 ms).
    RMS_WINDOW = 50

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
        # Derived overlays: one RMS envelope (drawn last, on top) and one
        # rectified trace per channel. Hidden until the toolbar toggle is on;
        # computing them is skipped entirely while hidden.
        self._derived_curves: list[pg.PlotDataItem] = [
            self.plot(pen=RECT_PEN, visible=False) for _ in range(EMG_CHANNELS)
        ]
        self._rms_curve = self.plot(pen=RMS_PEN, visible=False)
        self._derived_curves.append(self._rms_curve)

    def set_derived_visible(self, visible: bool) -> None:
        """Show or hide the rectified/RMS overlays without touching raw curves."""

        for curve in self._derived_curves:
            curve.setVisible(visible)
        if not visible:
            # Clear stale geometry so a re-enabled view never flashes old data.
            for curve in self._derived_curves:
                curve.setData([])

    def update_data(self, samples: np.ndarray) -> None:
        if samples.size == 0:
            return
        limit = max(1, int(200 * self.window_s))
        samples = np.ascontiguousarray(samples[-limit:])
        times = np.ascontiguousarray((samples["t_ns"] - samples["t_ns"][-1]) / 1e9)
        derived_visible = any(curve.isVisible() for curve in self._derived_curves)
        rms_stack = []
        for index, curve in enumerate(self._curves):
            values = np.ascontiguousarray(samples[f"ch{index + 1}"], dtype=np.float32)
            curve.setData(times, values)
            if derived_visible:
                rect = np.abs(values)
                self._derived_curves[index].setData(times, rect)
                rms_stack.append(rect)
        if derived_visible:
            if rms_stack:
                envelope = np.sqrt(
                    np.convolve(
                        np.mean(rms_stack, axis=0) ** 2,
                        np.ones(self.RMS_WINDOW) / self.RMS_WINDOW,
                        mode="same",
                    )
                ).astype(np.float32)
                self._rms_curve.setData(times, envelope)
            else:
                self._rms_curve.setData([])


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

    #: How long a fired gesture stays highlighted before dimming to grey.
    GESTURE_FLASH_MS = 2000

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
        self.gesture_label = QtWidgets.QLabel("")
        self.gesture_label.setStyleSheet("color: #6f7a89;")
        self.battery_label = QtWidgets.QLabel("")
        self.battery_label.setStyleSheet("color: #9aa4b2;")
        self.orientation_label = QtWidgets.QLabel("")
        self.orientation_label.setStyleSheet("color: #6f7a89; font-size: 11px;")

        header = QtWidgets.QHBoxLayout()
        header.addWidget(self.status_label)
        header.addSpacing(12)
        header.addWidget(self.gesture_label)
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

        # Single-shot timer that dims the gesture annotation after the flash.
        self._gesture_timer = QtCore.QTimer(self)
        self._gesture_timer.setSingleShot(True)
        self._gesture_timer.setInterval(self.GESTURE_FLASH_MS)
        self._gesture_timer.timeout.connect(self._dim_gesture)

    def announce_event(self, name: str) -> None:
        """Flash an annotation for a fired gesture/pose named ``name``.

        Called from the UI thread (marshalled by :class:`MainWindow`). The
        label stays visible after the flash — it becomes the annotation of
        the most recent gesture for this armband — but dims to grey so a
        fresh gesture still stands out.
        """

        self.gesture_label.setText(f"gesture: {name}")
        self.gesture_label.setStyleSheet("color: #ffd166; font-weight: bold;")
        self._gesture_timer.start()

    def _dim_gesture(self) -> None:
        self.gesture_label.setStyleSheet("color: #6f7a89;")

    def update_display(self, state: str, detail: str | None, battery: int | None) -> None:
        text = state
        if detail:
            text = f"{state} - {detail}"
        self.status_label.setText(text)
        color = {
            "streaming": "#48d1cc",
            "error": "#ff6b6b",
            # Reconnecting gets its own alarming amber: the link dropped and the
            # adapter is retrying — the most important transient state to notice.
            "reconnecting": "#ff9f43",
            "connecting": "#ffd166",
            "stopping": "#ffd166",
        }.get(state, "#9aa4b2")
        self.status_label.setStyleSheet(f"color: {color};")
        self.status_label.setToolTip(
            "Connection states: streaming (teal), connecting/reconnecting (amber), "
            "reconnecting means the BLE link dropped and an automatic retry with "
            "backoff is running, error (red) means retries stopped failing."
        )
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
