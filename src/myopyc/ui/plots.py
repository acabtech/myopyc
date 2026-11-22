"""Live device panels: EMG oscilloscope and IMU traces."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtWidgets

from myopyc.devices.model import DeviceInfo
from myopyc.recorder.markers import Marker
from myopyc.streams.live import LiveBuffer
from myopyc.streams.schema import EMG_CHANNELS, StreamKind

CHANNEL_PENS = [
    pg.mkPen(pg.intColor(index, hues=EMG_CHANNELS), width=1) for index in range(EMG_CHANNELS)
]

# Derived-channel overlays drawn on top of the raw EMG traces. Distinct
# greys/weights so the raw per-channel colors stay unambiguous underneath.
RMS_PEN = pg.mkPen("#e8e8e8", width=2)
RECT_PEN = pg.mkPen("#8a8a8a", width=1, style=QtCore.Qt.PenStyle.DashLine)


MARKER_PEN = pg.mkPen("#ffd166", width=1, style=QtCore.Qt.PenStyle.DashLine)


def _notch_biquad_coeffs(
    freq_hz: float, sample_rate_hz: float, q: float = 30.0
) -> tuple[float, ...]:
    """Second-order IIR notch (biquad) coefficients, normalized so ``a0 == 1``.

    The filter removes a narrow band around ``freq_hz`` (e.g. mains 50/60 Hz).
    It is intentionally small/fixed-order for cheap per-refresh evaluation in
    the live plot path; the result is GUI-only and never written to recordings.
    """

    w0 = 2.0 * np.pi * float(freq_hz) / float(sample_rate_hz)
    cos_w0 = float(np.cos(w0))
    sin_w0 = float(np.sin(w0))
    alpha = sin_w0 / (2.0 * float(q))
    b0 = 1.0
    b1 = -2.0 * cos_w0
    b2 = 1.0
    a0 = 1.0 + alpha
    a1 = -2.0 * cos_w0
    a2 = 1.0 - alpha
    return b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0


def apply_notch(samples: np.ndarray, freq_hz: float, sample_rate_hz: float = 200.0) -> np.ndarray:
    """Apply a narrow notch filter to a 1-D signal; returns a float32 copy.

    Invalid/out-of-Nyquist frequencies are a no-op so the derived overlay stays
    robust to bad config values. Filtering is causal (lfilter form) which is
    appropriate for scrolling live data.
    """

    data = np.ascontiguousarray(samples, dtype=np.float32)
    freq = float(freq_hz)
    nyquist = float(sample_rate_hz) / 2.0
    if data.size == 0 or not np.isfinite(freq) or freq <= 0.0 or freq >= nyquist:
        return data
    b0, b1, b2, a1, a2 = _notch_biquad_coeffs(freq, sample_rate_hz)
    # Direct Form I transposed is simple and stable enough for a narrow notch.
    y = np.empty_like(data)
    x1 = x2 = y1 = y2 = 0.0
    for i, x0 in enumerate(data.astype(np.float64, copy=False)):
        y0 = b0 * x0 + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        y[i] = y0
        x2, x1 = x1, x0
        y2, y1 = y1, y0
    return np.asarray(y, dtype=np.float32)


class EmgPlot(pg.PlotWidget):
    #: RMS envelope window in samples (EMG runs at 200 Hz; 50 samples = 250 ms).
    RMS_WINDOW = 50

    def __init__(self, window_s: float, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent=parent)
        self.window_s = window_s
        self._notch_hz: float | None = None
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
        self._markers: list[Marker] = []
        self._marker_lines: list[pg.InfiniteLine] = []

    def set_derived_visible(self, visible: bool) -> None:
        """Show or hide the rectified/RMS overlays without touching raw curves."""

        for curve in self._derived_curves:
            curve.setVisible(visible)
        if not visible:
            # Clear stale geometry so a re-enabled view never flashes old data.
            for curve in self._derived_curves:
                curve.setData([])

    def set_notch_hz(self, freq_hz: float | None) -> None:
        """Configure per-channel notch for derived overlays (GUI-only).

        ``None`` or a non-positive value disables notching; the rectified/RMS
        overlays then use the raw signal as before. The next ``update_data``
        call picks up the new setting.
        """

        if freq_hz is None or not np.isfinite(freq_hz) or float(freq_hz) <= 0.0:
            self._notch_hz = None
        else:
            self._notch_hz = float(freq_hz)

    def set_markers(self, markers: Sequence[Marker]) -> None:
        """Replace marker annotations rendered as vertical lines."""

        self._markers = list(markers)
        self._sync_marker_lines()

    def _sync_marker_lines(self) -> None:
        # Grow/shrink the pool of InfiniteLines to match current markers.
        while len(self._marker_lines) < len(self._markers):
            line = pg.InfiniteLine(angle=90, movable=False, pen=MARKER_PEN)
            line.setZValue(10)
            line.hide()
            self.addItem(line)
            self._marker_lines.append(line)
        while len(self._marker_lines) > len(self._markers):
            line = self._marker_lines.pop()
            self.removeItem(line)
        for line, marker in zip(self._marker_lines, self._markers, strict=False):
            label = str(marker.label)
            # pyqtgraph renders the label next to the line; keep it short.
            line.label = label
            if hasattr(line, "setLabel"):
                line.setLabel(text=label, color="#ffd166", position=0.95)

    def _update_marker_positions(self, t_end_ns: int) -> None:
        if not self._markers:
            return
        window_start_ns = t_end_ns - int(self.window_s * 1e9)
        for line, marker in zip(self._marker_lines, self._markers, strict=False):
            t_ns = int(marker.t_ns)
            if window_start_ns <= t_ns <= t_end_ns:
                x = (t_ns - t_end_ns) / 1e9
                line.setPos(float(x))
                line.show()
            else:
                line.hide()

    def update_data(self, samples: np.ndarray) -> None:
        if samples.size == 0:
            for line in self._marker_lines:
                line.hide()
            return
        limit = max(1, int(200 * self.window_s))
        samples = np.ascontiguousarray(samples[-limit:])
        t_end_ns = int(samples["t_ns"][-1])
        times = np.ascontiguousarray((samples["t_ns"] - t_end_ns) / 1e9)
        derived_visible = any(curve.isVisible() for curve in self._derived_curves)
        rms_stack = []
        for index, curve in enumerate(self._curves):
            values = np.ascontiguousarray(samples[f"ch{index + 1}"], dtype=np.float32)
            curve.setData(times, values)
            if derived_visible:
                derived_values = (
                    apply_notch(values, self._notch_hz) if self._notch_hz is not None else values
                )
                rect = np.abs(derived_values)
                self._derived_curves[index].setData(times, rect)
                rms_stack.append(rect)
        if derived_visible:
            if rms_stack:
                mean_sq = np.mean(np.square(rms_stack, dtype=np.float32), axis=0)
                window = min(self.RMS_WINDOW, len(mean_sq))
                kernel = np.ones(window, dtype=np.float32) / window
                convolved = np.convolve(mean_sq, kernel, mode="full")
                start = (len(kernel) - 1) // 2
                envelope = np.sqrt(
                    np.clip(convolved[start : start + len(mean_sq)], 0.0, None)
                ).astype(np.float32)
                self._rms_curve.setData(times, envelope)
            else:
                self._rms_curve.setData([])
        self._update_marker_positions(t_end_ns)


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
        self._markers: list[Marker] = []
        self._marker_lines: list[pg.InfiniteLine] = []

    def set_markers(self, markers: Sequence[Marker]) -> None:
        """Replace marker annotations rendered as vertical lines."""

        self._markers = list(markers)
        while len(self._marker_lines) < len(self._markers):
            line = pg.InfiniteLine(angle=90, movable=False, pen=MARKER_PEN)
            line.setZValue(10)
            line.hide()
            self.addItem(line)
            self._marker_lines.append(line)
        while len(self._marker_lines) > len(self._markers):
            line = self._marker_lines.pop()
            self.removeItem(line)
        for line, marker in zip(self._marker_lines, self._markers, strict=False):
            label = str(marker.label)
            line.label = label
            if hasattr(line, "setLabel"):
                line.setLabel(text=label, color="#ffd166", position=0.95)

    def _update_marker_positions(self, t_end_ns: int) -> None:
        if not self._markers:
            return
        window_start_ns = t_end_ns - int(self.window_s * 1e9)
        for line, marker in zip(self._marker_lines, self._markers, strict=False):
            t_ns = int(marker.t_ns)
            if window_start_ns <= t_ns <= t_end_ns:
                x = (t_ns - t_end_ns) / 1e9
                line.setPos(float(x))
                line.show()
            else:
                line.hide()

    def update_data(self, samples: np.ndarray) -> None:
        if samples.size == 0:
            for line in self._marker_lines:
                line.hide()
            return
        limit = max(1, int(50 * self.window_s))
        samples = np.ascontiguousarray(samples[-limit:])
        t_end_ns = int(samples["t_ns"][-1])
        times = np.ascontiguousarray((samples["t_ns"] - t_end_ns) / 1e9)
        for index, axis in enumerate(("ax", "ay", "az")):
            self._curves[index].setData(
                times, np.ascontiguousarray(samples[axis], dtype=np.float32)
            )
        self._update_marker_positions(t_end_ns)


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

    def set_markers(self, markers: Sequence[Marker]) -> None:
        """Render the same session markers on both EMG and IMU plots."""

        self.emg.set_markers(markers)
        self.imu.set_markers(markers)

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
