"""Tests for session resume (Option 2, milestone 154).

Covers backend resume (new continuing chunk series) and the GUI startup prompt
flow (offscreen Qt), without requiring real BLE hardware.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

import pytest

from myopyc.recorder.manifest import read_manifest, write_manifest
from myopyc.recorder.reader import SessionReader
from myopyc.recorder.writer import SessionRecorder, find_interrupted_sessions, resume_recorder
from myopyc.streams.schema import StreamKind
from tests.conftest import make_emg

DEVICES = {
    "left": {"label": "Left", "mac": "SIM-LEFT", "name": "Simulated Myo"},
}


def _interrupted_session(tmp_path: Path) -> Path:
    rec = SessionRecorder(tmp_path, DEVICES, flush_interval_s=0.05)
    rec.start()
    rec.add(make_emg("left", count=2, start_seq=0, t0=1_000_000_000))
    rec._flush_all()
    rec.add(make_emg("left", count=3, start_seq=2, t0=1_100_000_000))
    session_dir = rec.stop()
    manifest = read_manifest(session_dir)
    manifest["status"] = "recording"
    manifest["stopped_utc"] = None
    write_manifest(session_dir, manifest)
    return session_dir


def test_find_interrupted_sessions_only_recording(tmp_path: Path) -> None:
    interrupted = _interrupted_session(tmp_path)
    clean = SessionRecorder(tmp_path / "clean", DEVICES, flush_interval_s=0.05)
    clean.start()
    clean.add(make_emg("left", count=1))
    clean.stop()

    found = find_interrupted_sessions(tmp_path)
    assert interrupted in found
    assert all(read_manifest(p)["status"] == "recording" for p in found)


def test_resume_recorder_appends_new_chunk_series(tmp_path: Path) -> None:
    session_dir = _interrupted_session(tmp_path)

    resumed = resume_recorder(session_dir, flush_interval_s=0.05)
    try:
        assert resumed.recording
        resumed.add(make_emg("left", count=2, start_seq=5, t0=1_200_000_000))
    finally:
        resumed.stop()

    manifest = read_manifest(session_dir)
    assert manifest["status"] == "stopped"
    assert manifest["resume_count"] == 1
    assert isinstance(manifest.get("resumed_utc"), str)
    chunks = manifest["devices"]["left"]["streams"]["emg"]["chunks"]
    names = [Path(c["file"]).name for c in chunks]
    assert names == ["chunk-00000.npy", "chunk-00001.npy", "chunk-00002.npy"]

    data = SessionReader(session_dir).load_stream("left", StreamKind.EMG)
    assert data["seq"].tolist() == [0, 1, 2, 3, 4, 5, 6]


def test_resume_is_idempotent_after_stop(tmp_path: Path) -> None:
    session_dir = _interrupted_session(tmp_path)
    r1 = resume_recorder(session_dir, flush_interval_s=0.05)
    r1.add(make_emg("left", count=1, start_seq=5, t0=1_200_000_000))
    r1.stop()

    # Stop makes it clean; resume again should work and continue chunk numbering.
    r2 = resume_recorder(session_dir, flush_interval_s=0.05)
    r2.add(make_emg("left", count=1, start_seq=6, t0=1_300_000_000))
    r2.stop()

    manifest = read_manifest(session_dir)
    assert manifest["resume_count"] == 2
    names = [Path(c["file"]).name for c in manifest["devices"]["left"]["streams"]["emg"]["chunks"]]
    assert names == ["chunk-00000.npy", "chunk-00001.npy", "chunk-00002.npy", "chunk-00003.npy"]
    seq = SessionReader(session_dir).load_stream("left", "emg")["seq"].tolist()
    assert seq == [0, 1, 2, 3, 4, 5, 6]


# ---------------------------------------------------------------- GUI (offscreen)

try:
    pytest.importorskip("PySide6")
    pytest.importorskip("pyqtgraph")
except (AttributeError, ImportError):
    pytest.skip("PySide6/pyqtgraph not usable in this environment", allow_module_level=True)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets  # noqa: E402

from myopyc.config import AppConfig  # noqa: E402
from myopyc.ui.main_window import MainWindow  # noqa: E402


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
    window.manager.stop_all()
    window._health_timer.stop()
    window._timer.stop()
    if window._recorder is not None:
        with contextlib.suppress(Exception):
            window._recorder.stop()


def test_gui_startup_prompt_resume(qapp, tmp_path: Path) -> None:
    session_dir = _interrupted_session(tmp_path)
    window = MainWindow(_config(), simulate=0, output_dir=tmp_path)
    try:
        window._interrupted_session_action_chooser = lambda _p: "resume"
        window._prompt_interrupted_sessions()
        assert window._recorder is not None
        assert window._recorder.session_dir == session_dir
        assert window.record_action.isChecked()
        window._recorder.add(make_emg("left", count=1, start_seq=5, t0=1_200_000_000))
        window._stop_recording()
        manifest = read_manifest(session_dir)
        assert manifest["status"] == "stopped"
        assert manifest.get("resume_count") == 1
        names = [
            Path(c["file"]).name for c in manifest["devices"]["left"]["streams"]["emg"]["chunks"]
        ]
        assert names[-1] == "chunk-00002.npy"
    finally:
        _close(window)


def test_gui_startup_prompt_repair_only(qapp, tmp_path: Path) -> None:
    session_dir = _interrupted_session(tmp_path)
    window = MainWindow(_config(), simulate=0, output_dir=tmp_path)
    try:
        window._interrupted_session_action_chooser = lambda _p: "repair"
        window._prompt_interrupted_sessions()
        assert window._recorder is None
        manifest = read_manifest(session_dir)
        assert manifest["status"] == "repaired"
        assert "resume_count" not in manifest
    finally:
        _close(window)
