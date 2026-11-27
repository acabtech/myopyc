"""Regressions for recorder lock discipline, RAM caps, resume and durability."""

from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path

import pytest

from myopyc.recorder import writer as writer_module
from myopyc.recorder.manifest import read_manifest, write_manifest
from myopyc.recorder.writer import SessionRecorder, resume_recorder
from myopyc.streams.schema import StreamKind
from tests.conftest import make_emg

DEVICES = {
    "left": {"label": "Left", "mac": "SIM-LEFT", "name": "Simulated Myo"},
    "right": {"label": "Right", "mac": "SIM-RIGHT", "name": "Simulated Myo"},
}
EMG_KEY = ("left", StreamKind.EMG)


def _recorder(tmp_path: Path, **kwargs) -> SessionRecorder:
    # A huge flush interval keeps the writer thread out of deterministic tests.
    return SessionRecorder(tmp_path, DEVICES, flush_interval_s=999, **kwargs)


def _interrupted(tmp_path: Path, **kwargs) -> Path:
    recorder = _recorder(tmp_path, **kwargs)
    recorder.start()
    recorder.add(make_emg("left", count=2, t0=1_000_000_000))
    session_dir = recorder.stop()
    manifest = read_manifest(session_dir)
    manifest["status"] = "recording"
    manifest["stopped_utc"] = None
    write_manifest(session_dir, manifest)
    return session_dir


# ---------------------------------------------------------------- issue #14


def test_manifest_writes_never_hold_ingestion_lock(tmp_path: Path, monkeypatch) -> None:
    """start(), flush (with and without pending data) and stop() write off-lock."""

    recorder = _recorder(tmp_path)
    acquired: list[bool] = []
    real_write = writer_module.write_manifest

    def spy_write(session_dir: Path, manifest: dict) -> Path:
        # If add()'s lock were held here, this non-blocking acquire would fail.
        got = recorder._lock.acquire(blocking=False)
        acquired.append(got)
        if got:
            recorder._lock.release()
        return real_write(session_dir, manifest)

    monkeypatch.setattr(writer_module, "write_manifest", spy_write)
    recorder.start()
    recorder.add(make_emg("left", count=2, t0=1_000_000_000))
    recorder._flush_all()  # pending branch
    recorder._flush_all()  # empty-pending branch
    recorder.stop()

    assert acquired  # all manifest writes were intercepted
    assert all(acquired)


# ---------------------------------------------------------------- issue #12


def test_requeue_enforces_cap_while_add_stays_responsive(tmp_path: Path, monkeypatch) -> None:
    """A failed write that rebuffers data must still drop over-cap oldest arrays."""

    recorder = _recorder(tmp_path, max_ram_bytes=600)  # 6 streams -> 100 B per stream
    assert recorder._per_stream_cap == 100
    recorder.start()
    recorder.add(make_emg("left", count=4, start_seq=0, t0=1_000_000_000))  # 96 B

    real_write = recorder._write_chunk

    def failing_write(key, arrays):
        # add() can append samples while the (failing) disk write is in progress.
        recorder.add(make_emg("left", count=4, start_seq=4, t0=2_000_000_000))
        raise OSError("simulated persistent write failure")

    monkeypatch.setattr(recorder, "_write_chunk", failing_write)
    recorder._flush_all()

    with recorder._lock:
        assert recorder._buffer_bytes[EMG_KEY] <= recorder._per_stream_cap
        assert len(recorder._buffers[EMG_KEY]) == 1  # oldest requeued array dropped
        assert recorder._dropped[EMG_KEY] == 4
    gaps = [g for g in recorder._gaps if g["device"] == "left" and g["stream"] == "emg"]
    assert len(gaps) == 1
    assert gaps[0]["reason"] == "ram-buffer-overflow"
    assert gaps[0]["samples"] == 4
    assert gaps[0]["t_start_ns"] == 1_000_000_000

    monkeypatch.setattr(recorder, "_write_chunk", real_write)
    recorder.stop()

    emg = read_manifest(recorder.session_dir)["devices"]["left"]["streams"]["emg"]
    assert emg["samples"] == 4
    assert emg["dropped_samples"] == 4
    assert len(read_manifest(recorder.session_dir)["gaps"]) == 1


def test_requeue_drops_lone_buffer_exceeding_cap(tmp_path: Path, monkeypatch) -> None:
    """The single-over-cap policy also applies on the requeue path."""

    recorder = _recorder(tmp_path, max_ram_bytes=300)  # 50 B per stream < 96 B batch
    recorder.start()
    recorder.add(make_emg("left", count=4, t0=1_000_000_000))

    def failing_write(key, arrays):
        raise OSError("simulated write failure")

    monkeypatch.setattr(recorder, "_write_chunk", failing_write)
    recorder._flush_all()

    with recorder._lock:
        assert recorder._buffers[EMG_KEY] == []
        assert recorder._buffer_bytes[EMG_KEY] == 0
        assert recorder._dropped[EMG_KEY] == 4
    assert len(recorder._gaps) == 1
    recorder.stop()


# ---------------------------------------------------------------- issue #13


def test_resume_updates_and_preserves_chunking_metadata(tmp_path: Path) -> None:
    session_dir = _interrupted(tmp_path)

    resumed = resume_recorder(session_dir, flush_interval_s=0.5, max_ram_bytes=4096)
    manifest = read_manifest(session_dir)
    assert manifest["chunking"]["flush_interval_s"] == 0.5
    assert manifest["chunking"]["max_ram_bytes"] == 4096
    resumed.stop()

    # A later resume without overrides inherits the updated values, not defaults.
    again = resume_recorder(session_dir)
    try:
        assert again.flush_interval_s == 0.5
        assert again.max_ram_bytes == 4096
        manifest = read_manifest(session_dir)
        assert manifest["chunking"]["flush_interval_s"] == 0.5
        assert manifest["chunking"]["max_ram_bytes"] == 4096
    finally:
        again.stop()


# ---------------------------------------------------------------- issue #18


@pytest.mark.skipif(not Path("/proc/self/fd").exists(), reason="requires /proc for fd paths")
def test_flush_fsyncs_chunk_and_manifest_before_commit(tmp_path: Path, monkeypatch) -> None:
    recorder = _recorder(tmp_path)
    recorder.start()
    recorder.add(make_emg("left", count=2, t0=1_000_000_000))

    real_fsync = os.fsync
    synced: list[str] = []

    def spy_fsync(fd: int) -> None:
        with suppress(OSError):  # diagnostic only
            synced.append(os.readlink(f"/proc/self/fd/{fd}"))
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", spy_fsync)
    recorder._flush_all()
    recorder.stop()

    assert any(path.endswith("chunk-00000.npy") for path in synced)
    assert any(path.endswith("manifest.json.tmp") for path in synced)
    assert any(Path(path).name == "emg" for path in synced)  # chunk's directory
    assert any(Path(path).name == recorder.session_dir.name for path in synced)
