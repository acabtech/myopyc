"""Main application window."""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from myopyc.config import AppConfig, save_config
from myopyc.devices.manager import DeviceManager
from myopyc.devices.model import DeviceInfo, SampleBatch
from myopyc.recorder.reader import SessionReader, find_sessions
from myopyc.recorder.writer import SessionRecorder
from myopyc.streams.live import LiveBuffer
from myopyc.ui.plots import DeviceView

log = logging.getLogger(__name__)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(
        self,
        config: AppConfig,
        *,
        simulate: int = 1,
        ble_devices: list[tuple[str, str]] | None = None,
        output_dir: Path | str | None = None,
    ) -> None:
        super().__init__()
        self.config = config
        self.output_dir = Path(output_dir or config.recorder.output_dir).expanduser()
        self.manager = DeviceManager()
        self.live = LiveBuffer(max_samples=4096)
        self.manager.subscribe(self._on_samples)
        self._views: dict[str, DeviceView] = {}
        self._recorder: SessionRecorder | None = None

        self.setWindowTitle("myopyc - Myo EMG/IMU capture")
        self.resize(1440, 900)
        self._build_toolbar()
        self._build_central()

        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

        for _ in range(max(0, min(simulate, 2))):
            self._add_simulated()
        for mac, label in ble_devices or []:
            self._connect_ble_device(mac, label)

        if config.ui.auto_start_streaming and self.manager.keys:
            self.manager.start_all()

    # ------------------------------------------------------------------ building
    def _build_toolbar(self) -> None:
        toolbar = QtWidgets.QToolBar("Main")
        toolbar.setMovable(False)
        toolbar.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.addToolBar(toolbar)

        self.record_action = QtGui.QAction("Record", self)
        self.record_action.setCheckable(True)
        self.record_action.setShortcut("Ctrl+R")
        self.record_action.toggled.connect(self._toggle_recording)
        toolbar.addAction(self.record_action)

        self.add_arm_action = QtGui.QAction("Add simulated arm", self)
        self.add_arm_action.triggered.connect(self._add_simulated)
        toolbar.addAction(self.add_arm_action)

        connect_action = QtGui.QAction("Connect Myo...", self)
        connect_action.triggered.connect(self._connect_dialog)
        toolbar.addAction(connect_action)

        toolbar.addSeparator()
        restart_action = QtGui.QAction("Restart streams", self)
        restart_action.triggered.connect(self._restart_streams)
        toolbar.addAction(restart_action)

        stop_action = QtGui.QAction("Stop streams", self)
        stop_action.triggered.connect(self.manager.stop_all)
        toolbar.addAction(stop_action)

        toolbar.addSeparator()
        open_action = QtGui.QAction("Open sessions", self)
        open_action.triggered.connect(self._open_output_dir)
        toolbar.addAction(open_action)

        self.folder_label = QtWidgets.QLabel(f"  {self.output_dir}")
        self.folder_label.setStyleSheet("color: #6f7a89;")
        toolbar.addWidget(self.folder_label)

    def _build_central(self) -> None:
        central = QtWidgets.QWidget()
        self._grid = QtWidgets.QGridLayout(central)
        self._grid.setContentsMargins(8, 8, 8, 8)
        self._grid.setSpacing(8)
        self.setCentralWidget(central)
        self.statusBar().showMessage("ready")

    # ------------------------------------------------------------------ devices
    def _add_simulated(self) -> None:
        try:
            label = (
                ("Left", "Right")[len(self.manager.keys)] if len(self.manager.keys) < 2 else None
            )
            info = self.manager.add_simulated(label)
        except (RuntimeError, IndexError) as exc:
            self.statusBar().showMessage(str(exc), 4000)
            return
        self._mount_view(info)
        self.manager.start(info.key)
        if not self._recorder and self.config.ui.auto_start_streaming:
            self.manager.start_all()

    def _connect_dialog(self) -> None:
        mac, ok = QtWidgets.QInputDialog.getText(
            self, "Connect Myo", "MAC address (e.g. D2:3B:85:94:32:8E):"
        )
        if not ok or not mac.strip():
            return
        mac = mac.strip()
        binding = self.config.binding_for_mac(mac)
        default_label = binding.label if binding else ("Left" if not self.manager.keys else "Right")
        label, ok = QtWidgets.QInputDialog.getText(
            self, "Device label", "Label for this armband:", text=default_label
        )
        if not ok or not label.strip():
            return
        self._connect_ble_device(mac, label.strip())

    def _connect_ble_device(self, mac: str, label: str) -> None:
        try:
            info = self.manager.add_ble(mac, label)
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Bluetooth connection", str(exc))
            return
        self.config.add_or_update_binding(mac, label, slot=len(self.config.devices))
        try:
            save_config(self.config)
        except OSError:
            log.exception("could not persist device binding")
        self._mount_view(info)
        self.manager.start(info.key)

    def _mount_view(self, info: DeviceInfo) -> None:
        view = DeviceView(
            info,
            self.live,
            emg_window_s=self.config.ui.emg_window_s,
            imu_window_s=self.config.ui.imu_window_s,
        )
        self._views[info.key] = view
        self._rebuild_grid()

    def _rebuild_grid(self) -> None:
        while self._grid.count():
            item = self._grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
        for index, view in enumerate(self._views.values()):
            self._grid.addWidget(view, 0, index)
        for column in range(2):
            self._grid.setColumnStretch(column, 1)

    def _restart_streams(self) -> None:
        self.manager.stop_all()
        self.manager.start_all()

    # ------------------------------------------------------------------ samples
    def _on_samples(self, batch: SampleBatch) -> None:
        self.live.append(batch)
        recorder = self._recorder
        if recorder is not None:
            with contextlib.suppress(KeyError):
                recorder.add(batch)

    # ------------------------------------------------------------------ recording
    def _toggle_recording(self, checked: bool) -> None:
        if checked:
            self._start_recording()
        else:
            self._stop_recording()

    def _start_recording(self) -> None:
        if self._recorder is not None:
            return
        if not self.manager.keys:
            self.record_action.setChecked(False)
            self.statusBar().showMessage("no armbands to record", 4000)
            return
        try:
            recorder = SessionRecorder(
                self.output_dir,
                self.manager.meta(),
                flush_interval_s=self.config.recorder.flush_interval_s,
                max_ram_bytes=self.config.recorder.max_ram_bytes,
            )
            recorder.start()
        except Exception as exc:
            log.exception("could not start recording")
            self.record_action.setChecked(False)
            self.statusBar().showMessage(f"recording failed: {exc}", 6000)
            return

        self._recorder = recorder
        self.record_action.setText("Stop recording")
        self.setWindowTitle(f"myopyc - recording {recorder.session_dir.name}")

    def _stop_recording(self) -> None:
        recorder = self._recorder
        self._recorder = None
        self.record_action.setText("Record")
        self.setWindowTitle("myopyc - Myo EMG/IMU capture")
        if recorder is None:
            return
        try:
            session_dir = recorder.stop()
            summary = SessionReader(session_dir).summary()
            samples = sum(row["samples"] for row in summary["streams"])
            dropped = sum(row["dropped"] for row in summary["streams"])
            self.statusBar().showMessage(
                f"saved {samples} samples ({dropped} dropped) to {session_dir}", 15000
            )
        except Exception as exc:
            log.exception("could not finalise recording")
            self.statusBar().showMessage(f"recording finalise failed: {exc}", 8000)

    # ------------------------------------------------------------------ tick
    def _tick(self) -> None:
        for key, view in self._views.items():
            view.update_display(
                self.manager.state(key), self.manager.detail(key), self.manager.battery(key)
            )
            view.refresh()

        recorder = self._recorder
        if recorder is not None:
            stats = recorder.stats()
            self.statusBar().showMessage(
                "recording | "
                f"samples {stats['received_samples']} | "
                f"buffered {stats['buffered_bytes'] / 1024:.1f} kB | "
                f"dropped {stats['dropped_samples']} | "
                f"chunks {stats['chunks_written']}"
            )
        elif not self.manager.keys:
            self.statusBar().showMessage("add a simulated arm or connect a Myo")

    # ------------------------------------------------------------------ misc
    def _open_output_dir(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        QtGui.QDesktopServices.openUrl(QtGui.QUrl.fromLocalFile(str(self.output_dir)))

    def saved_sessions(self) -> list[Path]:
        return find_sessions(self.output_dir)

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        if self._recorder is not None:
            self.record_action.setChecked(False)
        self.manager.stop_all()
        super().closeEvent(event)
