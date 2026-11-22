"""Offscreen Qt tests for read-only session playback.

Builds a real recorded session with :class:`~myopyc.recorder.writer.SessionRecorder`
and simulated batches, then drives :class:`~myopyc.ui.playback.PlaybackController`
and the :class:`~myopyc.ui.main_window.MainWindow` playback wiring
programmatically (no display server; skipped when PySide6/pyqtgraph are
missing). Read-only-ness is asserted by checking the session directory is
byte-identical after playback (nothing written, no ``unrepaired/`` side
effects).
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
from myopyc.recorder.reader import SessionReader
from myopyc.recorder.writer import SessionRecorder
from myopyc.streams.schema import EMG_DTYPE, IMU_DTYPE, StreamKind
from myopyc.ui.main_window import MainWindow
from myopyc.ui.playback import SLIDER_STEPS, PlaybackBar, PlaybackController


@pytest.fixture(scope="module")
def qapp() -> QtWidgets.QApplication:
    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication([])
    assert isinstance(app, QtWidgets.QApplication)
    return app


@pytest.fixture(autouse=True)
def no_ble_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("myopyc.ui.main_window.dl_myo_available", lambda: False)


def _config() -> AppConfig:
    config = AppConfig()
    config.ui.auto_start_streaming = False
    return config


def _close(window: MainWindow) -> None:
    window._health_timer.stop()
    window._timer.stop()


def _recorded_session(tmp_path, t_start_ns: int = 1_000_000_000) -> SessionRecorder:
    """Record a short session with real EMG/IMU chunks into ``tmp_path``."""

    devices = {"left": {"label": "Left", "mac": None}}
    recorder = SessionRecorder(tmp_path, devices, flush_interval_s=1.0)
    recorder.start()

    n_emg, n_imu = 40, 10
    emg = np.zeros(n_emg, dtype=EMG_DTYPE)
    emg["t_ns"] = t_start_ns + np.arange(n_emg) * 5_000_000  # 200 Hz
    emg["seq"] = np.arange(n_emg)
    emg["ch1"] = np.arange(n_emg) - n_emg // 2  # visible waveform

    imu = np.zeros(n_imu, dtype=IMU_DTYPE)
    imu["t_ns"] = t_start_ns + np.arange(n_imu) * 20_000_000  # 50 Hz
    imu["seq"] = np.arange(n_imu)
    imu["ax"] = np.linspace(-1.0, 1.0, n_imu)

    recorder.add(SampleBatch("left", StreamKind.EMG, emg))
    recorder.add(SampleBatch("left", StreamKind.IMU, imu))
    recorder.stop()
    return recorder


def _snapshot(directory) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted(directory.rglob("*")) if path.is_file()}


# ---------------------------------------------------------------- controller
def test_open_session_mounts_views_and_does_not_write(qapp, tmp_path) -> None:
    recorder = _recorded_session(tmp_path)
    directory = recorder.session_dir
    before = _snapshot(directory)

    mounted: list[str] = []
    closed: list[bool] = []
    controller = PlaybackController(
        None,
        emg_window_s=2.0,
        imu_window_s=4.0,
        on_mount_views=mounted.append,
        on_close_views=lambda: closed.append(True),
    )
    messages: list[str] = []
    controller.message.connect(messages.append)

    try:
        assert controller.open_session(directory) is True
        assert controller.active and not controller.playing
        assert mounted == [directory.name]
        assert controller.duration_s == pytest.approx(0.195, abs=0.001)
        assert set(controller.views) == {"left"}
        view = controller.views["left"]
        assert view.status_label.text() == "playback"

        # Rendered data landed in the controller's private buffer, not in
        # any external one (read-only wrt. the live UI).
        assert view.live is controller._live
        # At the initial playhead (t_start) the window ends there, so exactly
        # the first instant is visible...
        emg_window = controller._live.snapshot("left", StreamKind.EMG)
        assert emg_window.size == 1
        assert int(emg_window["ch1"][0]) == -20
        # ...and seeking to the end reveals the full session window.
        controller.seek_fraction(1.0)
        emg_window = controller._live.snapshot("left", StreamKind.EMG)
        assert emg_window.size == 40
        assert list(emg_window["ch1"][:3]) == [-20, -19, -18]
    finally:
        controller.close_session(silent=True)

    assert closed == [True]
    assert _snapshot(directory) == before  # strictly read-only


def test_transport_play_seek_and_end(qapp, tmp_path) -> None:
    recorder = _recorded_session(tmp_path)
    controller = PlaybackController(
        None,
        emg_window_s=2.0,
        imu_window_s=4.0,
        on_mount_views=lambda name: None,
        on_close_views=lambda: None,
    )
    try:
        assert controller.open_session(recorder.session_dir) is True

        # Seek to the middle: window shifts forward, playhead is set.
        controller.seek_fraction(0.5)
        assert controller.position_s == pytest.approx(controller.duration_s / 2, abs=1e-3)
        emg_window = controller._live.snapshot("left", StreamKind.EMG)
        assert emg_window.size > 0
        assert int(emg_window["t_ns"][-1]) <= 1_000_000_000 + int(controller.duration_s * 1e9)

        # Play until the end (duration is tiny; force ticks manually).
        controller._progress_ns = controller._duration_ns
        controller._playing = True
        controller._tick()
        assert not controller.playing  # stopped itself at the end
        assert controller.position_s == pytest.approx(controller.duration_s, abs=1e-3)

        # Play again replays from the top.
        controller.play_pause()
        assert controller.playing
        assert controller.position_s < 0.01
        controller.play_pause()
        assert not controller.playing
    finally:
        controller.close_session(silent=True)


def test_open_rejects_unreadable_and_empty_sessions(qapp, tmp_path) -> None:
    messages: list[str] = []
    controller = PlaybackController(
        None,
        emg_window_s=2.0,
        imu_window_s=4.0,
        on_mount_views=lambda name: None,
        on_close_views=lambda: None,
    )
    controller.message.connect(messages.append)

    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "manifest.json").write_text("not json", encoding="utf-8")
    assert controller.open_session(broken) is False
    assert any("could not load" in m for m in messages)

    # A manifest-only session (no chunks on disk) has nothing to plot.
    recorder = SessionRecorder(tmp_path, {"left": {"label": "Left", "mac": None}})
    recorder.start()
    recorder.stop()
    assert controller.open_session(recorder.session_dir) is False
    assert any("no recorded data" in m for m in messages)
    assert not controller.active


# ---------------------------------------------------------------- bar
def test_playback_bar_states(qapp) -> None:
    bar = PlaybackBar()
    scrubbed: list[int] = []
    toggles: list[bool] = []
    stops: list[bool] = []
    bar.scrubbed.connect(scrubbed.append)
    bar.play_toggled.connect(toggles.append)
    # The ``stopped`` signal carries no arguments, so wrap the list append
    # (a zero-arg call would raise TypeError in the slot).
    bar.stopped.connect(lambda: stops.append(True))

    # Idle: everything disabled.
    assert not bar.playing and not bar._slider.isEnabled()
    assert not bar._play.isEnabled() and not bar._stop.isEnabled()

    bar.load_session("20261122_test", duration_s=12.0)
    assert "20261122_test" in bar._title.text()
    assert bar._slider.isEnabled() and bar._stop.isEnabled()
    assert not bar._play.isEnabled() or bar._play.isEnabled()  # play enabled for duration > 0
    assert bar._play.isEnabled()
    assert bar._position_label.text() == "0:00 / 0:12"

    # Programmatic slider moves (set_slider / Qt internals) do not emit
    # ``scrubbed`` — it is wired to sliderMoved, i.e. real user drags.
    bar.set_slider(500)
    assert bar._slider.value() == 500 and scrubbed == []

    bar.set_playing(True)
    assert bar.playing and toggles == [True]

    bar._stop.click()
    assert stops == [True]

    bar.clear()
    assert bar._position_label.text() == "0:00 / 0:00"
    assert not bar._slider.isEnabled()
    assert not bar._stop.isEnabled()
    assert stops == [True]  # click on a disabled button does nothing


def test_bar_duration_zero_disables_play(qapp) -> None:
    bar = PlaybackBar()
    bar.load_session("empty", duration_s=0.0)
    assert not bar._slider.isEnabled()
    assert not bar._play.isEnabled()
    assert bar._stop.isEnabled()  # still closable


# ---------------------------------------------------------------- main window
def test_window_open_session_starts_playback(qapp, tmp_path) -> None:
    recorder = _recorded_session(tmp_path)
    directory = recorder.session_dir
    before = _snapshot(directory)

    window = MainWindow(_config(), simulate=0, output_dir=tmp_path)
    try:
        assert window._start_playback(directory) is True
        playback = window._playback
        assert playback is not None and playback.active
        # Central grid now hosts the playback views; bar visible.
        bar = window._playback_bar
        assert bar is not None and bar.isVisibleTo(window)
        assert "playing" in window.windowTitle()
        assert set(window._views) == {"left"}
        assert window._views["left"].live is playback._live

        # Scrubbing through the bar moves the playhead.
        window._on_playback_scrub(SLIDER_STEPS // 2)
        assert playback.position_s == pytest.approx(playback.duration_s / 2, abs=1e-3)

        # Stop restores the live (empty) views and hides the bar.
        window._on_playback_stopped()
        assert not playback.active
        assert window._views == {}
        assert bar is not None and not bar.isVisibleTo(window)
        assert window.windowTitle() == "myopyc - Myo EMG/IMU capture"
    finally:
        _close(window)

    assert _snapshot(directory) == before


def test_window_playback_refused_while_recording(qapp, tmp_path) -> None:
    recorder = _recorded_session(tmp_path)
    window = MainWindow(_config(), simulate=0, output_dir=tmp_path)
    try:
        window.manager.add_simulated("Left")
        window.record_action.setChecked(True)
        messages: list[str] = []
        window.statusBar().messageChanged.connect(messages.append)
        assert window._start_playback(recorder.session_dir) is False
        playback = window._playback
        assert playback is not None
        assert not playback.active
        window.record_action.setChecked(False)
    finally:
        _close(window)


def test_window_recording_closes_playback_first(qapp, tmp_path) -> None:
    recorder = _recorded_session(tmp_path)
    window = MainWindow(_config(), simulate=0, output_dir=tmp_path)
    try:
        assert window._start_playback(recorder.session_dir) is True
        window.manager.add_simulated("Left")
        # Starting a recording must leave playback mode...
        window.record_action.setChecked(True)
        playback = window._playback
        bar = window._playback_bar
        assert playback is not None and not playback.active
        assert bar is not None and not bar.isVisibleTo(window)
        # ...and the recorder sees the live views again.
        assert window._views["left"].live is window.live
        window.record_action.setChecked(False)
    finally:
        _close(window)


def test_dock_open_routes_into_playback(qapp, tmp_path) -> None:
    recorder = _recorded_session(tmp_path)
    directory = recorder.session_dir
    window = MainWindow(_config(), simulate=0, output_dir=tmp_path)
    try:
        dock = window._sessions_dock
        summary = dock.open_session(directory)
        assert summary is not None
        playback = window._playback
        assert playback is not None
        assert playback.active
        assert playback.session_dir() == directory
    finally:
        _close(window)


def test_manifest_round_trip_after_playback(qapp, tmp_path) -> None:
    """Reader still sees the exact recorded samples after a playback pass."""

    recorder = _recorded_session(tmp_path)
    window = MainWindow(_config(), simulate=0, output_dir=tmp_path)
    try:
        assert window._start_playback(recorder.session_dir) is True
        window._on_playback_stopped()
    finally:
        _close(window)

    reader = SessionReader(recorder.session_dir)
    emg = reader.load_stream("left", "emg")
    assert emg.size == 40
    assert list(emg["seq"]) == list(range(40))
    assert reader.manifest["status"] == "stopped"
