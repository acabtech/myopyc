"""Read-only playback of a recorded session through the live plot widgets.

:class:`PlaybackBar` is a small control strip (scrub slider plus
play/pause/stop) and :class:`PlaybackController` owns the loaded
:class:`~myopyc.recorder.reader.SessionData`, rendering the time window at the
current playhead into the per-device :class:`~myopyc.ui.plots.DeviceView`
panels.

Playback is strictly read-only: chunk files are only read, nothing touches the
session directory and no recorder is created. Views are rebuilt with a private
:class:`~myopyc.streams.live.LiveBuffer`, so the main window's live capture
buffers are never touched; closing playback re-mounts the live views.

The playhead advances on the *host-monotonic* ``t_ns`` axis shared by EMG/IMU,
EVENT samples and markers, so all three stay aligned in playback.
"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
from numpy.typing import NDArray
from PySide6 import QtCore, QtWidgets

from myopyc.devices.model import DeviceInfo
from myopyc.recorder.reader import SessionData, load_session
from myopyc.streams.live import LiveBuffer
from myopyc.streams.schema import StreamKind
from myopyc.ui.plots import DeviceView

#: Playback cadence: plot refresh + slider tick, matching the live 40 ms loop.
TICK_MS = 40

#: Playback speed: recorded seconds consumed per real second (2x so a long
#: session can be reviewed quickly; the scrub slider covers the rest).
PLAYBACK_RATE = 2.0

#: Slider resolution (positions across the whole session).
SLIDER_STEPS = 1000


class PlaybackBar(QtWidgets.QWidget):
    """Scrub slider plus play/pause/stop controls for session playback."""

    #: Emitted when the user drags the slider (0..``SLIDER_STEPS``).
    scrubbed = QtCore.Signal(int)
    #: Emitted when Play/Pause is toggled (``True`` = playing).
    play_toggled = QtCore.Signal(bool)
    #: Emitted when Stop is pressed.
    stopped = QtCore.Signal()

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._title = QtWidgets.QLabel("no session", self)
        self._title.setStyleSheet("color: #6f7a89;")

        self._slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal, self)
        self._slider.setRange(0, SLIDER_STEPS)
        self._slider.setEnabled(False)
        self._slider.sliderMoved.connect(self.scrubbed)

        self._play = QtWidgets.QPushButton("Play", self)
        self._play.setEnabled(False)
        self._play.setCheckable(True)
        self._play.toggled.connect(self.play_toggled)

        self._stop = QtWidgets.QPushButton("Stop", self)
        self._stop.setEnabled(False)
        self._stop.clicked.connect(self.stopped)

        self._position_label = QtWidgets.QLabel("0:00 / 0:00", self)
        self._position_label.setStyleSheet("color: #9aa4b2;")

        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.addWidget(self._title)
        layout.addWidget(self._slider, 1)
        layout.addWidget(self._play)
        layout.addWidget(self._stop)
        layout.addWidget(self._position_label)

    # ------------------------------------------------------------------ state
    def load_session(self, name: str, duration_s: float) -> None:
        """Point the bar at a loaded session (name + total duration)."""

        self._title.setText(f"playing: {name}")
        self.set_slider(0)
        self._slider.setEnabled(duration_s > 0)
        self._play.setChecked(False)
        self._play.setEnabled(duration_s > 0)
        self._stop.setEnabled(True)
        self.set_position(0.0, duration_s)

    def clear(self) -> None:
        """Reset the bar to its idle state."""

        self._title.setText("no session")
        self.set_slider(0)
        self._slider.setEnabled(False)
        self._play.setChecked(False)
        self._play.setEnabled(False)
        self._stop.setEnabled(False)
        self._position_label.setText("0:00 / 0:00")

    @property
    def playing(self) -> bool:
        return self._play.isChecked()

    def set_playing(self, playing: bool) -> None:
        self._play.setChecked(playing)

    # ------------------------------------------------------------------ display
    def set_position(self, current_s: float, total_s: float) -> None:
        self._position_label.setText(f"{_fmt(current_s)} / {_fmt(total_s)}")

    def set_slider(self, value: int) -> None:
        """Move the slider without re-emitting :attr:`scrubbed`."""

        self._slider.blockSignals(True)
        self._slider.setValue(value)
        self._slider.blockSignals(False)


def _fmt(seconds: float) -> str:
    seconds = max(0, int(seconds))
    minutes, secs = divmod(seconds, 60)
    return f"{minutes}:{secs:02d}"


class PlaybackController(QtCore.QObject):
    """Loads a session, renders windows of it into DeviceViews and plays."""

    #: Emitted with a transient status text (wired to the main status bar).
    message = QtCore.Signal(str)

    def __init__(
        self,
        parent: QtCore.QObject | None,
        *,
        emg_window_s: float,
        imu_window_s: float,
        on_mount_views,
        on_close_views,
        on_progress=None,
    ) -> None:
        """Create a controller.

        ``on_mount_views(session_name)`` is called after the read-only
        ``DeviceView``s have been built; the main window re-grids its central
        widget and shows the playback bar. ``on_close_views()`` restores the
        live views when playback ends. ``on_progress(current_s, total_s,
        fraction)`` runs after every render so the bar can track the playhead.
        All three are injected so the controller stays widget-agnostic and
        offscreen-testable.
        """

        super().__init__(parent)
        self._emg_window_s = emg_window_s
        self._imu_window_s = imu_window_s
        self._on_mount_views = on_mount_views
        self._on_close_views = on_close_views
        self._on_progress = on_progress or (lambda *_: None)

        self._session: SessionData | None = None
        self._t_start_ns: int = 0
        self._duration_ns: int = 0
        self._views: dict[str, DeviceView] = {}
        self._live = LiveBuffer(max_samples=4096)
        self._progress_ns: int = 0
        self._playing = False
        self._last_tick: float | None = None

        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._tick)

    # ------------------------------------------------------------------ state
    @property
    def active(self) -> bool:
        """``True`` while a session is loaded (playing or paused)."""

        return self._session is not None

    @property
    def playing(self) -> bool:
        return self._playing

    @property
    def views(self) -> dict[str, DeviceView]:
        return self._views

    def session_dir(self) -> Path | None:
        return self._session.directory if self._session is not None else None

    # ------------------------------------------------------------------ load / close
    def open_session(self, session_dir: Path | str) -> bool:
        """Load a recorded session and mount read-only views for its devices.

        Loading is strictly read-only: chunk files are only read. Returns
        ``False`` (with a message) when the session cannot be loaded or has no
        plotted data.
        """

        try:
            session = load_session(session_dir)
        except Exception as exc:
            self.message.emit(f"could not load {Path(session_dir).name}: {exc}")
            return False

        plotted = {
            key: device
            for key, device in session.devices.items()
            if any(len(device[stream]) for stream in (StreamKind.EMG, StreamKind.IMU))
        }
        if not plotted:
            self.message.emit(f"no recorded data to play in {Path(session_dir).name}")
            return False

        if self._session is not None:
            self.close_session(silent=True)
        self._session = session
        self._views = {}
        self._live.clear()

        start: int | None = None
        end: int | None = None
        for key, device in session.devices.items():
            info = DeviceInfo(key=key, label=device.label, mac=device.mac)
            view = DeviceView(
                info,
                self._live,
                emg_window_s=self._emg_window_s,
                imu_window_s=self._imu_window_s,
            )
            view.status_label.setText("playback")
            self._views[key] = view
            for stream in device.streams.values():
                if stream.samples.size == 0:
                    continue
                first = int(stream.samples["t_ns"][0])
                last = int(stream.samples["t_ns"][-1])
                start = first if start is None else min(start, first)
                end = last if end is None else max(end, last)

        self._t_start_ns = int(start or 0)
        self._duration_ns = max(0, int(end or 0) - self._t_start_ns)
        self._progress_ns = 0
        self._playing = False
        self._last_tick = None
        self._render()
        self._on_mount_views(self._session.directory.name)
        self.message.emit(
            f"playing {self._session.directory.name} ({_fmt(self.duration_s)} loaded)"
        )
        return True

    def close_session(self, *, silent: bool = False) -> None:
        """Leave playback and restore the live device views."""

        self._timer.stop()
        self._playing = False
        self._session = None
        self._views = {}
        self._progress_ns = 0
        self._last_tick = None
        self._on_close_views()
        if not silent:
            self.message.emit("playback closed")

    # ------------------------------------------------------------------ transport
    @property
    def duration_s(self) -> float:
        return self._duration_ns / 1e9

    @property
    def position_s(self) -> float:
        return self._progress_ns / 1e9

    def play_pause(self) -> None:
        """Toggle play/pause; replay from the top when the end was reached."""

        if self._session is None or self._duration_ns == 0:
            return
        self._playing = not self._playing
        if self._playing:
            if self._progress_ns >= self._duration_ns:
                self._progress_ns = 0
            self._last_tick = None
            self._timer.start()
        else:
            self._timer.stop()

    def stop(self) -> None:
        """End playback entirely (controller returns to inactive state)."""

        if self._session is None:
            return
        self.close_session()

    def seek_fraction(self, fraction: float) -> None:
        """Scrub to ``fraction`` (0..1) of the session timeline."""

        if self._session is None:
            return
        fraction = min(1.0, max(0.0, fraction))
        self._progress_ns = int(fraction * self._duration_ns)
        self._last_tick = None
        self._render()

    # ------------------------------------------------------------------ ticking
    def _tick(self) -> None:
        now = time.monotonic()
        if self._last_tick is not None:
            self._progress_ns += int((now - self._last_tick) * 1e9 * PLAYBACK_RATE)
        self._last_tick = now
        if self._progress_ns >= self._duration_ns:
            self._progress_ns = self._duration_ns
            self._playing = False
            self._timer.stop()
        self._render()

    # ------------------------------------------------------------------ rendering
    def _render(self) -> None:
        """Push the current window of each stream into the read-only views."""

        session = self._session
        if session is None:
            return
        playhead = self._t_start_ns + self._progress_ns
        for key in session.devices:
            # Each render replaces the plotted window wholesale; otherwise the
            # overlapping re-appends would fill the ring with duplicates.
            self._live.clear(device=key)
        for key, device in session.devices.items():
            for stream in device.streams.values():
                if stream.samples.size == 0 or stream.kind is StreamKind.EVENT:
                    continue  # EVENT samples have no continuous plot; manifest summary covers them
                window = _window_slice(stream.samples, playhead)
                if window.size:
                    self._live.append(_batch_from_window(key, stream.kind, window))
            view = self._views.get(key)
            if view is not None:
                view.refresh()

        fraction = self._progress_ns / self._duration_ns if self._duration_ns else 0.0
        self._on_progress(self.position_s, self.duration_s, fraction)


def _window_slice(
    samples: NDArray[np.void], playhead_ns: int, window_s: float = 6.0
) -> NDArray[np.void]:
    """Rows covering the ``window_s`` ending at ``playhead_ns``."""

    if samples.size == 0:
        return samples
    t = samples["t_ns"]
    end = int(np.searchsorted(t, playhead_ns, side="right"))
    start_ns = playhead_ns - int(window_s * 1e9)
    start = int(np.searchsorted(t, start_ns, side="left"))
    return samples[start:end]


def _batch_from_window(key: str, kind: StreamKind, window: NDArray[np.void]):
    """Wrap a window slice into a :class:`SampleBatch` for the LiveBuffer."""

    from myopyc.devices.model import SampleBatch

    return SampleBatch(device=key, kind=kind, data=np.ascontiguousarray(window))
