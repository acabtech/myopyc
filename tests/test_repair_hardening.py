"""Regressions for repair validation and manifest path containment."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from myopyc.recorder.manifest import read_manifest, write_manifest
from myopyc.recorder.reader import SessionReader
from myopyc.recorder.repair import QUARANTINE_DIR, repair_session
from myopyc.streams.schema import EMG_DTYPE
from tests.test_repair import _record_two_flushes

FUTURE = "future_stream"


def _outside_array(path: Path) -> Path:
    rows = np.zeros(2, dtype=EMG_DTYPE)
    rows["seq"] = [100, 101]
    np.save(path, rows)
    return path


def _inject_chunk(session_dir: Path, stream: str, rel: str, samples: int = 2) -> None:
    manifest = read_manifest(session_dir)
    streams = manifest["devices"]["left"]["streams"]
    entry = streams.setdefault(stream, {"chunks": [], "samples": 0, "dropped_samples": 0})
    entry["chunks"].append({"file": rel, "samples": samples, "bytes": 48})
    entry["samples"] = int(entry.get("samples", 0)) + samples
    write_manifest(session_dir, manifest)


# ---------------------------------------------------------------- issue #15


def test_unknown_stream_kind_truncated_chunk_is_quarantined(tmp_path: Path) -> None:
    session_dir = _record_two_flushes(tmp_path)
    stream_dir = session_dir / "devices" / "left" / FUTURE
    stream_dir.mkdir(parents=True, exist_ok=True)
    chunk = _outside_array(stream_dir / "chunk-00000.npy")
    chunk.write_bytes(chunk.read_bytes()[:20])  # valid header, no body
    rel = chunk.relative_to(session_dir).as_posix()
    _inject_chunk(session_dir, FUTURE, rel)

    report = repair_session(session_dir)

    assert report.changed is True
    assert rel in report.mismatched
    assert not chunk.exists()
    assert (session_dir / QUARANTINE_DIR / Path(rel)).is_file()
    manifest = read_manifest(session_dir)
    entry = manifest["devices"]["left"]["streams"][FUTURE]
    assert entry["chunks"] == []
    assert entry["samples"] == 0
    assert report.dropped_samples[f"left/{FUTURE}"] == 2


def test_unknown_stream_kind_readable_chunk_survives(tmp_path: Path) -> None:
    """Unknown kinds keep dtype flexibility: any readable .npy is still valid."""

    session_dir = _record_two_flushes(tmp_path)
    stream_dir = session_dir / "devices" / "left" / FUTURE
    stream_dir.mkdir(parents=True, exist_ok=True)
    chunk = stream_dir / "chunk-00000.npy"
    np.save(chunk, np.arange(6, dtype=np.int32))  # not any known stream dtype
    rel = chunk.relative_to(session_dir).as_posix()
    _inject_chunk(session_dir, FUTURE, rel)

    report = repair_session(session_dir)

    assert report.mismatched == []
    assert chunk.is_file()
    entry = read_manifest(session_dir)["devices"]["left"]["streams"][FUTURE]
    assert len(entry["chunks"]) == 1
    assert entry["samples"] == 2


# ---------------------------------------------------------------- issue #17


@pytest.mark.parametrize("escape", ["absolute", "relative", "symlink"])
def test_escaping_manifest_chunk_paths_are_unlisted_not_read(tmp_path: Path, escape: str) -> None:
    session_dir = _record_two_flushes(tmp_path)
    outside = _outside_array(tmp_path / "outside.npy")

    if escape == "absolute":
        rel = str(outside)
    elif escape == "relative":
        rel = "../outside.npy"
    else:
        link = session_dir / "devices" / "left" / "emg" / "escape.npy"
        link.symlink_to(outside)
        rel = link.relative_to(session_dir).as_posix()

    _inject_chunk(session_dir, "emg", rel)
    report = repair_session(session_dir)

    assert report.changed is True
    assert rel in report.mismatched
    assert report.quarantined == []
    assert outside.is_file()  # never touched, moved or deleted
    entry = read_manifest(session_dir)["devices"]["left"]["streams"]["emg"]
    assert [Path(c["file"]).name for c in entry["chunks"]] == [
        "chunk-00000.npy",
        "chunk-00001.npy",
    ]
    assert entry["samples"] == 5  # only the two real chunks survive
    assert SessionReader(session_dir).load_stream("left", "emg").size == 5


def test_reader_refuses_escaping_chunk_path(tmp_path: Path) -> None:
    session_dir = _record_two_flushes(tmp_path)
    _outside_array(tmp_path / "outside.npy")
    _inject_chunk(session_dir, "emg", "../outside.npy")

    reader = SessionReader(session_dir)
    with pytest.raises(ValueError, match="outside session"):
        list(reader.iter_chunks("left", "emg"))
    with pytest.raises(ValueError, match="outside session"):
        reader.load_stream("left", "emg")
    assert (tmp_path / "outside.npy").is_file()
