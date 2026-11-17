from __future__ import annotations

import pytest

from myopyc.recorder.manifest import read_manifest
from myopyc.recorder.reader import SessionReader, find_sessions
from myopyc.recorder.writer import SessionRecorder
from myopyc.streams.schema import StreamKind
from tests.conftest import make_emg, make_imu

DEVICES = {
    "left": {"label": "Left", "mac": "SIM-LEFT", "name": "Simulated Myo"},
    "right": {"label": "Right", "mac": "SIM-RIGHT", "name": "Simulated Myo"},
}


def _recorder(tmp_path, **kwargs) -> SessionRecorder:
    recorder = SessionRecorder(tmp_path, DEVICES, flush_interval_s=0.05, **kwargs)
    recorder.start()
    return recorder


def test_roundtrip_writes_chunks_and_manifest(tmp_path) -> None:
    recorder = _recorder(tmp_path)
    for index in range(3):
        recorder.add(
            make_emg("left", count=4, start_seq=index * 4, t0=1_000_000_000 + index * 10_000_000)
        )
    recorder.add(make_imu("right", count=2))
    session_dir = recorder.stop()

    manifest = read_manifest(session_dir)
    assert manifest["status"] == "stopped"
    assert manifest["format_version"] == 1
    assert manifest["devices"]["left"]["streams"]["emg"]["samples"] == 12
    assert manifest["devices"]["right"]["streams"]["imu"]["samples"] == 2
    assert len(manifest["devices"]["left"]["streams"]["emg"]["chunks"]) == 1

    reader = SessionReader(session_dir)
    data = reader.load()
    emg = data["left"][StreamKind.EMG].samples
    assert emg.size == 12
    assert emg["seq"].tolist() == list(range(12))
    assert data["right"][StreamKind.IMU].samples.size == 2
    assert reader.summary()["gaps"] == 0


def test_multiple_flushes_create_ordered_chunks(tmp_path) -> None:
    recorder = _recorder(tmp_path)
    recorder.add(make_emg("left", count=2, start_seq=0, t0=1))
    recorder._flush_all()
    recorder.add(make_emg("left", count=2, start_seq=2, t0=100))
    recorder._flush_all()
    recorder.stop()

    chunks = SessionReader(recorder.session_dir).chunks("left", StreamKind.EMG)
    assert [chunk["samples"] for chunk in chunks] == [2, 2]
    loaded = SessionReader(recorder.session_dir).load_stream("left", StreamKind.EMG)
    assert loaded["seq"].tolist() == [0, 1, 2, 3]


def test_overflow_drops_oldest_and_records_gap(tmp_path) -> None:
    # A 1-byte budget makes every non-empty buffer overflow immediately.
    recorder = _recorder(tmp_path, max_ram_bytes=1)
    for index in range(10):
        recorder.add(make_emg("left", count=4, start_seq=index * 4, t0=1_000_000_000 + index * 100))
    recorder.stop()

    manifest = read_manifest(recorder.session_dir)
    emg = manifest["devices"]["left"]["streams"]["emg"]
    assert emg["samples"] == 4  # only the newest batch survived
    assert emg["dropped_samples"] == 36
    assert len(manifest["gaps"]) >= 1
    assert manifest["gaps"][0]["reason"] == "ram-buffer-overflow"


def test_add_before_start_is_ignored(tmp_path) -> None:
    recorder = SessionRecorder(tmp_path, DEVICES, flush_interval_s=0.05)
    recorder.add(make_emg("left"))
    with pytest.raises(RuntimeError):
        recorder.stop()


def test_unknown_device_raises(tmp_path) -> None:
    recorder = _recorder(tmp_path)
    try:
        with pytest.raises(KeyError):
            recorder.add(make_emg("ghost"))
    finally:
        recorder.stop()


def test_find_sessions(tmp_path) -> None:
    recorder = _recorder(tmp_path)
    recorder.stop()
    found = find_sessions(tmp_path)
    assert found == [recorder.session_dir]
    assert (recorder.session_dir / "manifest.json").is_file()
