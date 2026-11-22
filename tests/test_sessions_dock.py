"""Offscreen Qt tests for the Sessions dock (list / open / rename / delete).

Creates real recorded-session fixtures by writing manifests with the recorder's
own manifest helpers, then drives ``SessionsDock`` programmatically. Dialogs
are bypassed by calling the non-interactive variants (``new_name=`` argument
and ``confirm=False``) or by stubbing :class:`QMessageBox` statics, so no
display server is needed. Skipped if PySide6/pyqtgraph are missing.
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
from myopyc.recorder.manifest import new_manifest, write_manifest
from myopyc.streams.schema import EMG_DTYPE, StreamKind
from myopyc.ui.main_window import MainWindow


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


def _fake_session(root, name: str, samples: int = 10, markers: int = 0):
    """Write a minimal but valid session directory (manifest only).

    ``new_manifest`` builds stream entries from ``stream_meta`` itself, so the
    sample counts are patched onto the returned manifest afterwards.
    """

    manifest = new_manifest(
        f"20261122_000000_{name}",
        {"dev-1": {"label": "Left", "mac": None}},
        flush_interval_s=1.0,
        max_ram_bytes=1,
    )
    manifest["status"] = "complete"
    manifest["markers"] = [{"label": f"m{i}", "t_ns": i} for i in range(markers)]
    emg = manifest["devices"]["dev-1"]["streams"]["emg"]
    emg["samples"] = samples
    emg["chunks"] = [{"file": "emg/chunk-000.npy"}]
    directory = root / name
    directory.mkdir(parents=True)
    write_manifest(directory, manifest)
    return directory


def _dock(window: MainWindow):
    return window._sessions_dock


def test_dock_lists_sessions_newest_first(qapp, tmp_path) -> None:
    _fake_session(tmp_path, "20261122_090000_alpha", samples=100)
    _fake_session(tmp_path, "20261122_100000_beta", samples=5)

    window = MainWindow(_config(), simulate=0, output_dir=tmp_path)
    try:
        dock = _dock(window)
        # Added to the main window and visible per the toolbar toggle (the
        # window itself is never shown in offscreen tests, so check visibility
        # via the toggle state and dock features rather than isVisible()).
        assert dock.parent() is window
        assert window.sessions_action.isChecked()
        assert not dock.isHidden()
        assert [dock._list.item(i).data(0x0100) for i in range(dock._list.count())] == [
            tmp_path / "20261122_100000_beta",
            tmp_path / "20261122_090000_alpha",
        ]
        # Description line includes sample counts and status.
        assert "5 samples" in dock._list.item(0).text()
        assert "complete" in dock._list.item(0).text()
    finally:
        _close(window)


def test_open_session_signals_for_playback(qapp, tmp_path, monkeypatch) -> None:
    """Open emits ``session_opened`` (the main window starts playback).

    The dock itself never loads ``.npy`` chunks; it validates the manifest and
    hands the path over. Note the manifest-only fixture has no chunk files, so
    the window's playback controller refuses to mount it — asserting that the
    *dock* contract (signal + summary) holds regardless.
    """

    directory = _fake_session(tmp_path, "20261122_090000_alpha", samples=42, markers=2)
    opened: list = []
    window = MainWindow(_config(), simulate=0, output_dir=tmp_path)
    try:
        dock = _dock(window)
        dock.session_opened.connect(opened.append)

        messages: list[str] = []
        dock.message.connect(messages.append)
        summary = dock.open_session(directory)
        assert summary is not None
        assert summary["samples"] == 42
        assert summary["markers"] == 2
        assert opened == [directory]
        # No summary dialog is shown any more; open routes into playback.
        assert not any("samples: 42" in m for m in messages)

        # Double-click open path on a selected item.
        dock._list.setCurrentRow(0)
        summary2 = dock.open_selected()
        assert summary2 is not None and summary2["session_dir"] == directory
    finally:
        _close(window)


def test_open_session_with_broken_manifest(qapp, tmp_path) -> None:
    bad = tmp_path / "20261122_090000_broken"
    bad.mkdir()
    (bad / "manifest.json").write_text("not json", encoding="utf-8")

    window = MainWindow(_config(), simulate=0, output_dir=tmp_path)
    try:
        dock = _dock(window)
        messages: list[str] = []
        dock.message.connect(messages.append)
        assert dock.open_session(bad) is None
        assert any("could not open" in m for m in messages)
    finally:
        _close(window)


def test_rename_session(qapp, tmp_path) -> None:
    directory = _fake_session(tmp_path, "20261122_090000_alpha")

    window = MainWindow(_config(), simulate=0, output_dir=tmp_path)
    try:
        dock = _dock(window)
        renamed = dock.rename_session(directory, new_name="pilot-run")
        assert renamed == tmp_path / "pilot-run"
        assert (tmp_path / "pilot-run" / "manifest.json").is_file()
        assert not directory.exists()
        assert [dock._list.item(i).data(0x0100).name for i in range(dock._list.count())] == [
            "pilot-run"
        ]
    finally:
        _close(window)


def test_rename_rejects_bad_names_and_collisions(qapp, tmp_path) -> None:
    alpha = _fake_session(tmp_path, "20261122_090000_alpha")
    _fake_session(tmp_path, "20261122_100000_beta")

    window = MainWindow(_config(), simulate=0, output_dir=tmp_path)
    try:
        dock = _dock(window)
        messages: list[str] = []
        dock.message.connect(messages.append)

        assert dock.rename_session(alpha, new_name="") is None  # empty
        assert dock.rename_session(alpha, new_name="a/b") is None  # path separator
        assert dock.rename_session(alpha, new_name="20261122_100000_beta") is None  # collision
        assert alpha.exists()
        assert any("invalid session name" in m or "already exists" in m for m in messages)
    finally:
        _close(window)


def test_delete_session_with_confirmation(qapp, tmp_path, monkeypatch) -> None:
    directory = _fake_session(tmp_path, "20261122_090000_alpha")

    window = MainWindow(_config(), simulate=0, output_dir=tmp_path)
    try:
        dock = _dock(window)

        # Answer "No" -> kept.
        monkeypatch.setattr(
            QtWidgets.QMessageBox,
            "question",
            staticmethod(lambda *a, **k: QtWidgets.QMessageBox.StandardButton.No),
        )
        assert not dock.delete_session(directory)
        assert directory.exists()

        # Answer "Yes" -> removed.
        monkeypatch.setattr(
            QtWidgets.QMessageBox,
            "question",
            staticmethod(lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Yes),
        )
        assert dock.delete_session(directory)
        assert not directory.exists()
        assert dock._list.count() == 0
        assert not dock._empty.isHidden()
    finally:
        _close(window)


def test_active_recording_session_is_protected(qapp, tmp_path) -> None:
    """The open recording cannot be renamed or deleted from the dock."""

    window = MainWindow(_config(), simulate=0, output_dir=tmp_path)
    try:
        window.manager.add_simulated("Left")
        window.record_action.setChecked(True)
        recorder = window._recorder
        assert recorder is not None
        messages: list[str] = []
        _dock(window).message.connect(messages.append)

        assert _dock(window).rename_session(recorder.session_dir, new_name="x") is None
        assert not _dock(window).delete_session(recorder.session_dir, confirm=False)
        assert recorder.session_dir.exists()
        assert any("currently being recorded" in m for m in messages)

        window.record_action.setChecked(False)
    finally:
        _close(window)


def test_recording_stop_refreshes_the_dock(qapp, tmp_path) -> None:
    """Stopping a recording adds the finished session to the dock listing."""

    window = MainWindow(_config(), simulate=0, output_dir=tmp_path)
    try:
        window.manager.add_simulated("Left")
        rows = np.zeros(3, dtype=EMG_DTYPE)
        rows["seq"] = np.arange(3)
        window.record_action.setChecked(True)
        window._on_samples(SampleBatch(window.manager.keys[0], StreamKind.EMG, rows))
        window.record_action.setChecked(False)

        dock = _dock(window)
        assert dock._list.count() == 1
        assert dock._list.item(0).data(0x0100) == window.saved_sessions()[0]
    finally:
        _close(window)


def test_toolbar_toggle_and_no_selection_hints(qapp, tmp_path) -> None:
    window = MainWindow(_config(), simulate=0, output_dir=tmp_path)
    try:
        dock = _dock(window)
        messages: list[str] = []
        dock.message.connect(messages.append)

        # Toggle hides/shows the dock and keeps the action in sync.
        window.sessions_action.setChecked(False)
        assert dock.isHidden()
        window.sessions_action.setChecked(True)
        assert not dock.isHidden()

        # No selection -> open/rename/delete emit a hint, change nothing.
        assert dock.open_selected() is None
        assert dock.rename_selected() is None
        assert not dock.delete_selected()
        assert messages.count("no session selected") == 3
    finally:
        _close(window)
