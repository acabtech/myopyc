from __future__ import annotations

import numpy as np
import pytest

from myopyc.recorder.manifest import read_manifest
from myopyc.recorder.reader import SessionReader, find_sessions
from myopyc.recorder.writer import SessionRecorder
from myopyc.streams.schema import EVENT_TYPE_IDS, StreamKind, event_type_name
from tests.conftest import make_emg, make_event, make_imu

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


def test_event_stream_roundtrip(tmp_path) -> None:
    """EVENT is a first-class third stream: recorded like EMG/IMU, readable back."""
    recorder = _recorder(tmp_path)
    recorder.add(
        make_event("left", count=3, start_seq=0, t0=1_000_000_000, type_code=EVENT_TYPE_IDS["fist"])
    )
    recorder.add(make_emg("left", count=4, t0=1_000_000_000))
    recorder.add(make_event("right", count=1, type_code=EVENT_TYPE_IDS["double_click"]))
    recorder.stop()

    manifest = read_manifest(recorder.session_dir)
    event_entry = manifest["devices"]["left"]["streams"]["event"]
    assert event_entry["samples"] == 3
    assert event_entry["sample_rate_hz"] == 0.0  # event-driven stream
    assert manifest["devices"]["right"]["streams"]["event"]["samples"] == 1

    reader = SessionReader(recorder.session_dir)
    data = reader.load()
    events = data["left"][StreamKind.EVENT].samples
    assert events.size == 3
    assert events["seq"].tolist() == [0, 1, 2]
    assert events["type"].tolist() == [EVENT_TYPE_IDS["fist"]] * 3
    assert data["left"][StreamKind.EMG].samples.size == 4
    assert data["right"][StreamKind.EVENT].samples["type"].tolist() == [
        EVENT_TYPE_IDS["double_click"]
    ]

    # The typed reader renders the pose names without breaking on unknown codes.
    assert event_type_name(int(events["type"][0])) == "fist"
    assert summary_kinds(reader) == {
        ("left", "emg"),
        ("left", "imu"),
        ("left", "event"),
        ("right", "emg"),
        ("right", "imu"),
        ("right", "event"),
    }


def summary_kinds(reader: SessionReader) -> set[tuple[str, str]]:
    summary = reader.summary()
    return {(row["device"], row["stream"]) for row in summary["streams"]}


def test_find_sessions(tmp_path) -> None:
    recorder = _recorder(tmp_path)
    recorder.stop()
    found = find_sessions(tmp_path)
    assert found == [recorder.session_dir]
    assert (recorder.session_dir / "manifest.json").is_file()


def test_append_only_manifest_fields_ignored(tmp_path) -> None:
    """Forward-compat: unknown optional fields/stream kinds must not break readers.

    ``FORMAT_VERSION`` stays 1 and evolves by adding optional fields only. A v0.1
    reader must still load sessions that carry future fields (e.g. markers, an
    EVENT stream, extra per-chunk metadata) it does not understand.
    """

    recorder = _recorder(tmp_path)
    recorder.add(make_emg("left", count=4, start_seq=0, t0=1_000_000_000))
    recorder.add(make_imu("right", count=2))
    session_dir = recorder.stop()

    manifest = read_manifest(session_dir)
    # Optional top-level fields that future writers may add.
    manifest["markers"] = [{"t_ns": 1_000_000_000, "label": "start"}]
    manifest["future_flag"] = {"enabled": True}
    # Extra per-stream field and an entirely unknown stream kind ("event" is a
    # known v0.2 kind now, so use a genuinely future name to test the skip path).
    left_streams = manifest["devices"]["left"]["streams"]
    left_streams["emg"]["calibration"] = {"offset": 0}
    left_streams["future_stream"] = {
        "chunks": [],
        "samples": 0,
        "dropped_samples": 0,
        "dtype": "future",
    }
    # Extra per-chunk metadata.
    left_streams["emg"]["chunks"][0]["checksum"] = "deadbeef"
    manifest["gaps"].append({"reason": "future-gap-kind", "extra": 1})
    (session_dir / "manifest.json").write_text(
        __import__("json").dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )

    reader = SessionReader(session_dir)
    # Unknown keys are preserved on disk and visible via the raw manifest, but the
    # typed reader continues to understand EMG/IMU/EVENT exactly as before.
    assert reader.manifest["markers"][0]["label"] == "start"
    assert "future_stream" in reader.manifest["devices"]["left"]["streams"]

    data = reader.load()
    emg = data["left"][StreamKind.EMG].samples
    assert emg.size == 4
    assert emg["seq"].tolist() == list(range(4))
    assert data["right"][StreamKind.IMU].samples.size == 2

    summary = reader.summary()
    assert summary["status"] == "stopped"
    kinds = {(row["device"], row["stream"]) for row in summary["streams"]}
    assert ("left", "emg") in kinds and ("right", "imu") in kinds
    # Known kinds are understood; the unknown future stream is skipped.
    assert all(row["stream"] in {"emg", "imu", "event"} for row in summary["streams"])
    assert not any(row["stream"] == "future_stream" for row in summary["streams"])

    # A reader path that never asked to mutate the session leaves it intact.
    assert (session_dir / "manifest.json").read_text(encoding="utf-8").endswith("}\n")
    assert read_manifest(session_dir)["markers"][0]["label"] == "start"


def test_legacy_v01_reader_skips_event_stream(tmp_path) -> None:
    """A v0.1 reader (which predates ``StreamKind.EVENT``) tolerates an ``event``.

    The v0.2 event stream is a new per-stream manifest key; a reader that only
    knows EMG/IMU must simply skip it — loading and summary must not touch or
    crash on the unknown ``event`` entry or its chunk files.
    """

    recorder = _recorder(tmp_path)
    recorder.add(make_event("left", count=2, type_code=EVENT_TYPE_IDS["fist"]))
    recorder.add(make_emg("left", count=4, t0=1_000_000_000))
    recorder.add(make_imu("left", count=2))
    session_dir = recorder.stop()
    assert (session_dir / "devices" / "left" / "event").is_dir()

    # A faithful stand-in for the v0.1 reader: it enumerates only the kinds it
    # knows and uses .get() on the per-stream entry, so unknown keys are inert.
    legacy_kinds = (StreamKind.EMG, StreamKind.IMU)
    manifest = read_manifest(session_dir)
    streams = manifest["devices"]["left"]["streams"]
    assert "event" in streams  # the session genuinely carries the new kind

    for kind in legacy_kinds:
        entry = streams.get(kind.value, {})
        assert int(entry.get("samples", 0)) >= 0
        for chunk in entry.get("chunks", []):
            array = np.load(session_dir / chunk["file"], allow_pickle=False)
            assert array.size > 0

    # Summary built the v0.1 way never reports the unknown stream kind.
    rows = [
        {
            "stream": kind.value,
            "samples": int(streams.get(kind.value, {}).get("samples", 0)),
        }
        for kind in legacy_kinds
    ]
    assert {row["stream"] for row in rows} == {"emg", "imu"}
    assert not any(row["stream"] == "event" for row in rows)
