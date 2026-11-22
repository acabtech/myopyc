"""Main application window."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from myopyc.config import AppConfig, save_config
from myopyc.devices.adapter import dl_myo_available
from myopyc.devices.discovery import discover_myos, release_bluez_connection
from myopyc.devices.manager import DeviceManager
from myopyc.devices.model import DeviceInfo, SampleBatch
from myopyc.recorder.markers import Marker, markers_from_manifest
from myopyc.recorder.reader import SessionReader, find_sessions
from myopyc.recorder.writer import SessionRecorder
from myopyc.streams.health import DeviceHealth, RateTracker, format_status_line
from myopyc.streams.live import LiveBuffer
from myopyc.streams.schema import EVENT_TYPE_NAMES, StreamKind
from myopyc.ui.playback import SLIDER_STEPS, PlaybackBar, PlaybackController
from myopyc.ui.plots import DeviceView
from myopyc.ui.sessions_dock import SessionsDock

log = logging.getLogger(__name__)


def _run_async(coro):
    """Run ``coro`` to completion on a fresh event loop (worker thread)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _call_on_ui_thread(context: QtCore.QObject, callback) -> None:
    """Schedule ``callback`` on the thread that owns ``context``.

    ``QTimer.singleShot(0, callback)`` (without a context object) silently
    does nothing when it is called from a non-Qt thread. Passing a live
    ``QObject`` binds the timer to that object's thread, so the callback
    actually runs on the UI thread (and is cancelled if the object dies).
    """
    QtCore.QTimer.singleShot(0, context, callback)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(
        self,
        config: AppConfig,
        *,
        simulate: int = 0,
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
        self._health = RateTracker()
        self._last_health_mono: float | None = None
        self._status_label: QtWidgets.QLabel | None = None
        self._playback_bar: PlaybackBar | None = None
        self._playback: PlaybackController | None = None
        # Sustained RAM-overflow detection: warn only after repeated drop growth.
        self._drop_prev: int | None = None
        self._drop_increase_streak: int = 0
        self._drop_stable_streak: int = 0
        self._overflow_sustained: bool = False

        self.setWindowTitle("myopyc - Myo EMG/IMU capture")
        self.resize(1440, 900)
        self._build_toolbar()
        self._build_central()
        self._build_marker_shortcuts()
        self._build_sessions_dock()
        self._build_playback()

        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

        # Stream health is reported on a slower cadence than the plots: rates
        # averaged over ~1 s are stable enough to read and cheap to update.
        self._health_timer = QtCore.QTimer(self)
        self._health_timer.setInterval(1000)
        self._health_timer.timeout.connect(self.refresh_health)
        self._health_timer.start()

        for _ in range(max(0, min(simulate, 2))):
            self._add_simulated()
        for mac, label in ble_devices or []:
            self._connect_ble_device(mac, label)

        if config.ui.auto_start_streaming and self.manager.keys:
            self.manager.start_all()

        if not self.manager.keys and dl_myo_available():
            # No devices requested: probe for nearby/OS-connected Myos in the
            # background and connect whatever is found, so a plain `myopyc`
            # just works when the armband is on (no manual MAC entry).
            self.statusBar().showMessage("searching for Myo armbands…", 0)
            self._discovery_thread = threading.Thread(
                target=self._auto_discover, name="myo-discovery", daemon=True
            )
            self._discovery_thread.start()
        else:
            self._discovery_thread = None

    # ------------------------------------------------------------------ markers
    def _build_marker_shortcuts(self) -> None:
        """Chorded ``M`` + number-row shortcuts that drop session markers.

        ``M 1`` … ``M 9`` inject a marker labelled ``marker-1`` … ``marker-9``
        (labels are adjustable later; the timestamp is what matters for
        analysis). Only active while a recording is open; timestamps come from
        the same host-monotonic clock the streams use, so markers line up
        with the recorded ``t_ns`` axis.
        """

        self._marker_shortcuts: list[QtGui.QShortcut] = []
        for digit in range(1, 10):
            shortcut = QtGui.QShortcut(QtGui.QKeySequence(f"M, {digit}"), self)
            shortcut.setContext(QtCore.Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(
                lambda checked=False, index=digit: self._inject_marker(f"marker-{index}")
            )
            self._marker_shortcuts.append(shortcut)

    def _inject_marker(self, label: str, note: str = "") -> bool:
        """Write one marker into the open recording. ``True`` if recorded.

        Without an active recorder the shortcut is a no-op and the status bar
        explains why; with one, the marker rides the next manifest rewrite.
        """

        recorder = self._recorder
        if recorder is None:
            self.statusBar().showMessage("start a recording to drop markers", 4000)
            return False
        try:
            marker = Marker(label=label, t_ns=time.monotonic_ns(), note=note)
            recorder.add_marker(marker)
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("could not inject marker")
            self.statusBar().showMessage(f"marker failed: {exc}", 4000)
            return False
        self._refresh_live_markers()
        self.statusBar().showMessage(f"marker '{label}' recorded", 4000)
        return True

    def _current_live_markers(self) -> list[Marker]:
        """Markers for the active recording, or an empty list when idle."""

        if self._recorder is None:
            return []
        manifest = getattr(self._recorder, "_manifest", None)
        if not isinstance(manifest, dict):
            return []
        return markers_from_manifest(manifest)

    def _refresh_live_markers(self) -> None:
        """Push current session markers into live (non-playback) views."""

        if self._playback is not None and self._playback.active:
            return
        markers = self._current_live_markers()
        for view in self._views.values():
            view.set_markers(markers)

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

        # Gesture flash/annotation: checked by default (announcements visible),
        # unchecked silences them entirely ("toggleable suppression").
        self.gesture_action = QtGui.QAction("Gesture annotations", self)
        self.gesture_action.setCheckable(True)
        self.gesture_action.setChecked(self.config.ui.gesture_annotations)
        self.gesture_action.toggled.connect(self._toggle_gesture_annotations)
        toolbar.addAction(self.gesture_action)

        # Derived EMG channels (rectified / RMS envelope + optional notch):
        # GUI-only overlays, never written into recorded data. Off by default.
        self.derived_action = QtGui.QAction("Derived channels", self)
        self.derived_action.setCheckable(True)
        self.derived_action.setChecked(self.config.ui.derived_channels)
        self.derived_action.toggled.connect(self._toggle_derived_channels)
        toolbar.addAction(self.derived_action)

        self.notch_action = QtGui.QAction("Notch 60 Hz", self)
        self.notch_action.setCheckable(True)
        self.notch_action.setChecked(abs(float(self.config.ui.notch_hz) - 60.0) < 1e-6)
        self.notch_action.toggled.connect(self._toggle_notch)
        toolbar.addAction(self.notch_action)

        self.add_arm_action = QtGui.QAction("Add simulated arm", self)
        self.add_arm_action.triggered.connect(self._add_simulated)
        toolbar.addAction(self.add_arm_action)

        connect_action = QtGui.QAction("Connect Myo...", self)
        connect_action.triggered.connect(self._pick_myos)
        toolbar.addAction(connect_action)

        toolbar.addSeparator()
        restart_action = QtGui.QAction("Restart streams", self)
        restart_action.triggered.connect(self._restart_streams)
        toolbar.addAction(restart_action)

        stop_action = QtGui.QAction("Stop streams", self)
        stop_action.triggered.connect(self.manager.stop_all)
        toolbar.addAction(stop_action)

        toolbar.addSeparator()
        self.sessions_action = QtGui.QAction("Sessions", self)
        self.sessions_action.setCheckable(True)
        self.sessions_action.setChecked(True)
        self.sessions_action.toggled.connect(self._toggle_sessions_dock)
        toolbar.addAction(self.sessions_action)

        open_action = QtGui.QAction("Open folder", self)
        open_action.triggered.connect(self._open_output_dir)
        toolbar.addAction(open_action)

        self.folder_label = QtWidgets.QLabel(f"  {self.output_dir}")
        self.folder_label.setStyleSheet("color: #6f7a89;")
        toolbar.addWidget(self.folder_label)

    def _build_central(self) -> None:
        central = QtWidgets.QWidget()
        central_layout = QtWidgets.QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)

        # Playback control strip: hidden until a session is opened for
        # playback; sits above the device grid.
        self._playback_bar = PlaybackBar(central)
        self._playback_bar.setVisible(False)
        central_layout.addWidget(self._playback_bar)

        grid_host = QtWidgets.QWidget()
        self._grid = QtWidgets.QGridLayout(grid_host)
        self._grid.setContentsMargins(8, 8, 8, 8)
        self._grid.setSpacing(8)
        central_layout.addWidget(grid_host, 1)

        self.setCentralWidget(central)
        self.statusBar().showMessage("ready")
        # Permanent health widget on the right; transient messages (errors,
        # "saved …") still use showMessage on the left and never clobber it.
        self._status_label = QtWidgets.QLabel("stream health idle")
        self._status_label.setObjectName("streamHealth")
        self._status_label.setStyleSheet("color: #9aa4b2;")
        self.statusBar().addPermanentWidget(self._status_label)

    def _build_sessions_dock(self) -> None:
        """Right-side sessions dock: list/open/rename/delete recorded sessions."""

        self._sessions_dock = SessionsDock(
            lambda: self.output_dir,
            lambda: self._recorder.session_dir if self._recorder is not None else None,
            self,
        )
        self._sessions_dock.message.connect(self.statusBar().showMessage)
        self._sessions_dock.session_opened.connect(self._on_session_opened)
        self.addDockWidget(QtCore.Qt.DockWidgetArea.RightDockWidgetArea, self._sessions_dock)
        self._sessions_dock.refresh()

    def _toggle_sessions_dock(self, visible: bool) -> None:
        self._sessions_dock.setVisible(visible)
        # Keep the toolbar check in sync when the dock itself is closed via
        # its own close button, so the toggle never drifts out of step.
        if visible != self.sessions_action.isChecked():
            self.sessions_action.setChecked(visible)

    def _on_session_opened(self, session_dir: Path) -> None:
        """Open the double-clicked/selected session in read-only playback."""

        self._start_playback(session_dir)

    # ------------------------------------------------------------------ playback
    def _build_playback(self) -> None:
        """Wire the playback controller to the central playback bar."""

        assert self._playback_bar is not None
        self._playback = PlaybackController(
            self,
            emg_window_s=self.config.ui.emg_window_s,
            imu_window_s=self.config.ui.imu_window_s,
            on_mount_views=self._mount_playback_views,
            on_close_views=self._close_playback_views,
            on_progress=self._on_playback_progress,
        )
        self._playback.message.connect(self.statusBar().showMessage)
        self._playback_bar.scrubbed.connect(self._on_playback_scrub)
        self._playback_bar.play_toggled.connect(self._on_playback_toggle)
        self._playback_bar.stopped.connect(self._on_playback_stopped)

    def _start_playback(self, session_dir: Path) -> bool:
        """Load ``session_dir`` into playback; ``True`` when it mounted.

        Refuses while recording is active (the recorder keeps its own state and
        the views are shared) — the status bar explains why.
        """

        assert self._playback is not None
        if self._recorder is not None:
            self.statusBar().showMessage(
                "stop the current recording before playing back a session", 4000
            )
            return False
        return self._playback.open_session(session_dir)

    def _mount_playback_views(self, session_name: str) -> None:
        """Swap the central grid to the read-only playback views."""

        assert self._playback is not None
        self._views = self._playback.views
        self._rebuild_grid()
        assert self._playback_bar is not None
        self._playback_bar.load_session(session_name, self._playback.duration_s)
        self._playback_bar.setVisible(True)
        self.setWindowTitle(f"myopyc - playing {session_name}")

    def _close_playback_views(self) -> None:
        """Restore the live device views after playback ends."""

        self._views = self._live_views()
        self._rebuild_grid()
        self._refresh_live_markers()
        assert self._playback_bar is not None
        self._playback_bar.clear()
        self._playback_bar.setVisible(False)
        if self._recorder is None:
            self.setWindowTitle("myopyc - Myo EMG/IMU capture")

    def _live_views(self) -> dict[str, DeviceView]:
        """Device views backed by the live buffers (re-created on demand)."""

        views: dict[str, DeviceView] = {}
        for key in self.manager.keys:
            info = self.manager.info(key)
            views[key] = self._make_view(info)
        return views

    def _on_playback_progress(self, current_s: float, total_s: float, fraction: float) -> None:
        """Track the playhead on the playback bar."""

        assert self._playback_bar is not None
        self._playback_bar.set_position(current_s, total_s)
        self._playback_bar.set_slider(round(fraction * SLIDER_STEPS))

    def _make_view(self, info: DeviceInfo) -> DeviceView:
        """Build a DeviceView honoring the current config (windows, overlays)."""

        view = DeviceView(
            info,
            self.live,
            emg_window_s=self.config.ui.emg_window_s,
            imu_window_s=self.config.ui.imu_window_s,
        )
        view.emg.set_derived_visible(self.config.ui.derived_channels)
        return view

    def _on_playback_scrub(self, value: int) -> None:
        assert self._playback is not None
        self._playback.seek_fraction(value / SLIDER_STEPS)

    def _on_playback_toggle(self, playing: bool) -> None:
        """Toggle the controller's transport to match the bar's Play state.

        The controller flips its own internal ``_playing`` flag on each call,
        so the bar's checked state and the controller's playing state are
        kept consistent by driving from a single source (the bar).
        """

        assert self._playback is not None
        if playing != self._playback.playing:
            self._playback.play_pause()

    def _on_playback_stopped(self) -> None:
        assert self._playback is not None
        self._playback.stop()

    def _close_playback(self) -> None:
        if self._playback is not None and self._playback.active:
            self._playback.close_session()

    def refresh_sessions(self) -> None:
        """Public hook: rebuild the sessions list (called after recordings end)."""

        self._sessions_dock.refresh()

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

    # ------------------------------------------------------------------ device picker
    def _pick_myos(self) -> None:
        """Open a dialog that scans and lists nearby Myo armbands."""
        dialog = _MyoPickerDialog(self)
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        entries = dialog.selected_entries()
        if not entries:
            return
        for entry, default_label in entries:
            if len(self.manager.keys) >= 2:
                break
            binding = self.config.binding_for_mac(entry.mac)
            label = binding.label if binding else default_label
            if entry.connected:
                # Release the OS-held connection in the background so our BLE
                # client can take over; connecting itself is retried patiently
                # by the adapter, so no need to block the UI here.
                threading.Thread(
                    target=self._release_then_connect,
                    args=(entry.mac, label),
                    daemon=True,
                    name=f"release-{entry.mac}",
                ).start()
            else:
                self._connect_ble_device(entry.mac, label)

    def _release_then_connect(self, mac: str, label: str) -> None:
        try:
            _run_async(release_bluez_connection(mac))
            time.sleep(2.0)
        except Exception:
            log.exception("could not release BlueZ connection to %s", mac)
        _call_on_ui_thread(self, lambda: self._connect_ble_device(mac, label))

    # ------------------------------------------------------------------ auto-discovery
    def _auto_discover(self) -> None:
        """Probe for Myo armbands off the UI thread, then connect them.

        Executed on a daemon thread; all UI interaction is marshalled back
        onto the Qt thread via zero-timeout single-shot timers.
        """

        def on_results(macs: list[str]) -> None:
            for mac in macs:
                if len(self.manager.keys) >= 2:
                    break
                binding = self.config.binding_for_mac(mac)
                label = binding.label if binding else ("Left" if not self.manager.keys else "Right")
                self._connect_ble_device(mac, label)
            if not self.manager.keys:
                self.statusBar().showMessage(
                    "no Myo found; wake the armband or use Connect Myo…", 8000
                )
            elif self.config.ui.auto_start_streaming:
                self.manager.start_all()

        found: list = []
        try:
            found = _run_async(discover_myos())
        except Exception:
            log.exception("myo discovery failed")
        macs: list[str] = []
        for entry in found:
            if entry.connected:
                # OS holds the connection; release it so our BLE client can
                # take over, then give the armband a moment to re-advertise.
                try:
                    _run_async(release_bluez_connection(entry.mac))
                except Exception:
                    log.exception("could not release BlueZ connection to %s", entry.mac)
                time.sleep(2.0)
            macs.append(entry.mac)
        _call_on_ui_thread(self, lambda: on_results(macs))

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
        if self._playback is not None and self._playback.active:
            # While playback is open the live view would be discarded anyway;
            # the status bar explains that new devices wait for it to close.
            self.statusBar().showMessage("close playback before adding armbands", 4000)
            return
        view = self._make_view(info)
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
        if batch.kind is StreamKind.EVENT and self.gesture_action.isChecked():
            self._announce_event(batch)

    def _announce_event(self, batch: SampleBatch) -> None:
        """Flash the gesture annotation on the device panel for a fired pose.

        Runs on whatever thread the source emits from; the UI work is
        marshalled onto the Qt thread so the label/timer are touched safely.
        """

        for row in batch.data:
            name = EVENT_TYPE_NAMES.get(int(row["type"]), f"unknown-{int(row['type'])}")
            view = self._views.get(batch.device)
            if view is None:
                continue
            if QtCore.QThread.currentThread() is self.thread():
                # Already on the UI thread (tests, or a UI-side emit): flash now.
                view.announce_event(name)
            else:
                # Sources emit from their own threads; marshal onto the Qt thread.
                _call_on_ui_thread(self, lambda v=view, n=name: v.announce_event(n))

    def _toggle_derived_channels(self, checked: bool) -> None:
        """Apply the derived-channel overlay toggle to every open EMG plot."""

        self.config.ui.derived_channels = checked
        for view in self._views.values():
            view.emg.set_derived_visible(checked)
        try:
            save_config(self.config)
        except OSError:
            log.exception("could not persist derived-channel toggle")
        self.statusBar().showMessage(
            f"derived channels {'on' if checked else 'off'}", 4000
        )

    def _toggle_notch(self, checked: bool) -> None:
        """Set per-channel notch frequency used by derived EMG overlays."""

        notch_hz = 60.0 if checked else 0.0
        self.config.ui.notch_hz = notch_hz
        for view in self._views.values():
            view.emg.set_notch_hz(notch_hz)
        try:
            save_config(self.config)
        except OSError:
            log.exception("could not persist notch toggle")
        self.statusBar().showMessage(
            f"notch {'60 Hz' if checked else 'off'}", 4000
        )

    def _toggle_gesture_annotations(self, checked: bool) -> None:
        self.config.ui.gesture_annotations = checked
        try:
            save_config(self.config)
        except OSError:
            log.exception("could not persist gesture-annotation toggle")
        if not checked:
            self.statusBar().showMessage("gesture annotations off", 4000)
        else:
            self.statusBar().showMessage("gesture annotations on", 4000)

    # ------------------------------------------------------------------ recording
    def _toggle_recording(self, checked: bool) -> None:
        if checked:
            self._start_recording()
        else:
            self._stop_recording()

    def _reset_overflow_warning(self) -> None:
        self._drop_prev = None
        self._drop_increase_streak = 0
        self._drop_stable_streak = 0
        self._overflow_sustained = False

    def _update_overflow_warning(self, dropped: int) -> bool:
        """Track sustained RAM-buffer overflow across health ticks.

        A warning is raised only after repeated drop growth (not a single blip)
        and cleared after drops stay stable for a short window.
        """

        increase_needed = 2
        clear_after_stable = 3
        if self._drop_prev is None:
            self._drop_prev = dropped
            return self._overflow_sustained

        if dropped > self._drop_prev:
            self._drop_increase_streak += 1
            self._drop_stable_streak = 0
            if self._drop_increase_streak >= increase_needed:
                self._overflow_sustained = True
        elif dropped == self._drop_prev:
            self._drop_stable_streak += 1
            self._drop_increase_streak = 0
            if self._overflow_sustained and self._drop_stable_streak >= clear_after_stable:
                self._overflow_sustained = False
        else:
            # Counter reset (new recording/session); treat as fresh baseline.
            self._drop_increase_streak = 0
            self._drop_stable_streak = 0
            self._overflow_sustained = False

        self._drop_prev = dropped
        return self._overflow_sustained

    def _start_recording(self) -> None:
        if self._recorder is not None:
            return
        # Playback shares the central device views; recording needs them live.
        self._close_playback()
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
        self._reset_overflow_warning()
        self.record_action.setText("Stop recording")
        self._refresh_live_markers()
        self.setWindowTitle(f"myopyc - recording {recorder.session_dir.name}")

    def _stop_recording(self) -> None:
        recorder = self._recorder
        self._recorder = None
        self._reset_overflow_warning()
        self.record_action.setText("Record")
        self.setWindowTitle("myopyc - Myo EMG/IMU capture")
        self._refresh_live_markers()
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
        finally:
            self.refresh_sessions()

    # ------------------------------------------------------------------ tick
    def _tick(self) -> None:
        playing = self._playback is not None and self._playback.active
        for key, view in self._views.items():
            if playing:
                # Playback views render their own status; the manager has no
                # state for session device keys and would overwrite it with
                # a misleading "idle".
                view.refresh()
                continue
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
                f"chunks {stats['chunks_written']}"
            )

    # ------------------------------------------------------------------ health
    def refresh_health(self) -> str:
        """Recompute per-stream rates/drops and update the status-bar label.

        Runs on a ~1 s cadence, independent of recording state, so the status
        bar always reflects stream health (observed vs expected Hz) and the
        cumulative dropped-sample counter. Returns the formatted line (also used
        by tests to assert the wiring without a display server).
        """

        now = time.monotonic()
        dt = 0.0 if self._last_health_mono is None else now - self._last_health_mono
        self._last_health_mono = now

        devices: list[DeviceHealth] = []
        for key in self.manager.keys:
            info = self.manager.info(key)
            streams = self._health.observe(key, self.manager.counts(key), now=now, dt=dt)
            devices.append(
                DeviceHealth(
                    key=key,
                    label=info.label,
                    streams=streams,
                    dropped=0,
                    battery=self.manager.battery(key),
                    connection=self.manager.state(key),
                )
            )

        dropped = self._recorder.stats()["dropped_samples"] if self._recorder is not None else 0
        overflow_warning = self._update_overflow_warning(dropped) if self._recorder is not None else False
        if self._recorder is None:
            self._reset_overflow_warning()
        line = format_status_line(devices, dropped=dropped, overflow_warning=overflow_warning)
        if self._status_label is not None:
            self._status_label.setText(line)
        return line

    # ------------------------------------------------------------------ misc
    def _open_output_dir(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(self.output_dir)))

    def saved_sessions(self) -> list[Path]:
        return find_sessions(self.output_dir)

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        if self._recorder is not None:
            self.record_action.setChecked(False)
        self._close_playback()
        self.manager.stop_all()
        super().closeEvent(event)


class _MyoPickerDialog(QtWidgets.QDialog):
    """Scan-and-pick dialog for Myo armbands.

    Runs ``discover_myos()`` on a background thread, streams results into a
    list widget, and returns the user's selection (with labels) on accept.
    """

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Connect Myo")
        self.setMinimumSize(460, 340)
        self._entries: dict[str, object] = {}  # mac -> DiscoveredMyo
        self._scan_done = False
        self._scanning = False

        layout = QtWidgets.QVBoxLayout(self)

        top = QtWidgets.QHBoxLayout()
        hint = QtWidgets.QLabel("Scanning for Myo armbands… (about 5 s)")
        hint.setWordWrap(True)
        top.addWidget(hint, 1)
        self._hint = hint
        self._rescan = QtWidgets.QPushButton("Rescan", self)
        self._rescan.setEnabled(False)
        self._rescan.clicked.connect(self._start_scan)
        top.addWidget(self._rescan)
        layout.addLayout(top)

        self._list = QtWidgets.QListWidget(self)
        self._list.itemDoubleClicked.connect(lambda _item: self.accept())
        layout.addWidget(self._list, 1)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setText("Connect")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._ok = buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok)
        self._ok.setEnabled(False)

        self._start_scan()

    # -- background scan ---------------------------------------------------
    def _start_scan(self) -> None:
        if self._scanning:
            return
        self._scanning = True
        self._scan_done = False
        self._hint.setText("Scanning for Myo armbands… (about 5 s)")
        self._rescan.setEnabled(False)
        self._thread = threading.Thread(target=self._scan, daemon=True, name="myo-scan")
        self._thread.start()

    def _scan(self) -> None:
        try:
            found = _run_async(discover_myos())
        except Exception:
            log.exception("myo scan failed")
            found = []
        try:
            from myopyc.devices.discovery import merge_saved_bindings

            found = merge_saved_bindings(found, {b.mac: b.label for b in self._bindings()})
        except Exception:
            log.exception("could not merge saved bindings into scan results")
        # Bind the timer to ``self`` so it is delivered on the Qt thread; the
        # context-free singleShot overload is a no-op from a worker thread.
        _call_on_ui_thread(self, lambda: self._populate(found))

    def _bindings(self):
        return self.parent().config.devices if self.parent() is not None else []

    def _streaming_macs(self) -> set[str]:
        parent = self.parent()
        if parent is None or not hasattr(parent, "manager"):
            return set()
        try:
            return {parent.manager.info(key).mac.upper() for key in parent.manager.keys}
        except Exception:
            return set()

    def _populate(self, found: list) -> None:
        self._scanning = False
        self._scan_done = True
        self._rescan.setEnabled(True)

        previously_checked: set[str] = set()
        for row in range(self._list.count()):
            item = self._list.item(row)
            if item.checkState() == QtCore.Qt.CheckState.Checked:
                previously_checked.add(item.data(QtCore.Qt.ItemDataRole.UserRole))
        self._list.clear()
        self._entries.clear()

        streaming = self._streaming_macs()
        visible = []
        for entry in found:
            if entry.mac.upper() in streaming:
                continue  # already streaming through myopyc
            visible.append(entry)

        if not visible:
            self._hint.setText(
                "No Myo armbands found.\nWake the armband (move it) and press Rescan."
            )
            self._ok.setEnabled(False)
            return

        self._hint.setText(
            f"Found {len(visible)} armband(s). Select one or more, then press Connect."
        )
        for entry in visible:
            self._entries[entry.mac] = entry
            if entry.connected:
                state = " (OS connected)"
            elif entry.remembered:
                state = " (remembered - wake it to connect)"
            else:
                state = ""
            item = QtWidgets.QListWidgetItem(f"{entry.name} - {entry.mac}{state}")
            checked = not previously_checked or entry.mac in previously_checked
            item.setCheckState(
                QtCore.Qt.CheckState.Checked if checked else QtCore.Qt.CheckState.Unchecked
            )
            item.setData(QtCore.Qt.ItemDataRole.UserRole, entry.mac)
            self._list.addItem(item)
        self._ok.setEnabled(True)

    # -- results ------------------------------------------------------------
    def selected_entries(self) -> list[tuple[object, str]]:
        """Return checked entries with a Left/Right label assigned in order."""
        picks: list[tuple[object, str]] = []
        for row in range(self._list.count()):
            item = self._list.item(row)
            if item.checkState() != QtCore.Qt.CheckState.Checked:
                continue
            mac = item.data(QtCore.Qt.ItemDataRole.UserRole)
            label = ("Left", "Right")[len(picks) % 2]
            picks.append((self._entries[mac], label))
        return picks
