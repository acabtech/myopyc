"""Marker round trips: writer → manifest → reader, plus JSONL marker files."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from myopyc.recorder.manifest import read_manifest
from myopyc.recorder.markers import Marker, load_marker_file, markers_from_manifest
from myopyc.recorder.reader import SessionReader
from myopyc.recorder.writer import SessionRecorder
from tests.conftest import make_emg
from tests.test_recorder import DEVICES, _recorder

# --------------------------------------------------------------------- model


def test_marker_roundtrip_writer_to_manifest_to_reader(tmp_path) -> None:
    """A marker added to a running session survives in the final manifest."""

    recorder = _recorder(tmp_path)
    recorder.add(make_emg("left", count=2))
    recorder.add_marker(Marker(label="task start", t_ns=1_500_000_000, note="participant 7"))
    recorder.add(make_emg("left", count=2, start_seq=2, t0=2_000_000_000))
    recorder.stop()

    manifest = read_manifest(recorder.session_dir)
    assert manifest["markers"] == [
        {"label": "task start", "t_ns": 1_500_000_000, "note": "participant 7"}
    ]

    reader = SessionReader(recorder.session_dir)
    assert reader.markers == [Marker(label="task start", t_ns=1_500_000_000, note="participant 7")]
    assert reader.load().markers == reader.markers
    # EMG data is untouched by marker bookkeeping.
    assert reader.load_stream("left", "emg").size == 4


def test_add_marker_after_stop_still_persists(tmp_path) -> None:
    """add_marker is valid post-stop: the manifest is rewritten immediately."""

    recorder = _recorder(tmp_path)
    recorder.stop()
    recorder.add_marker(Marker(label="late note", t_ns=5))
    manifest = read_manifest(recorder.session_dir)
    assert manifest["markers"] == [{"label": "late note", "t_ns": 5, "note": ""}]


def test_summary_includes_markers(tmp_path) -> None:
    recorder = _recorder(tmp_path)
    recorder.add_marker(Marker(label="m", t_ns=1))
    recorder.stop()
    summary = SessionReader(recorder.session_dir).summary()
    assert summary["markers"] == [{"label": "m", "t_ns": 1, "note": ""}]


def test_session_without_markers_reads_empty(tmp_path) -> None:
    """Pre-marker v0.1/v0.2 sessions carry no ``markers`` key at all."""

    recorder = _recorder(tmp_path)
    recorder.add(make_emg("left", count=1))
    recorder.stop()
    reader = SessionReader(recorder.session_dir)
    assert "markers" not in read_manifest(recorder.session_dir)
    assert reader.markers == []


# ------------------------------------------------- manifest-entry tolerance


def test_markers_from_manifest_tolerates_malformed_entries() -> None:
    """Other writer versions may emit junk entries; readers degrade, not crash."""

    manifest = {
        "markers": [
            {"label": "good", "t_ns": 7, "note": "n"},
            {"label": "", "t_ns": 8},  # empty label
            {"label": "no-time"},  # missing t_ns
            {"label": "bad-time", "t_ns": "1"},  # wrong type
            {"label": "negative", "t_ns": -1},
            "not-even-a-dict",
            {"label": "extra-ok", "t_ns": 9, "future_field": True},  # unknown fields kept
            {"label": "has-created", "t_ns": 10, "note": 42, "created_utc": "2026-01-01T00:00:00Z"},
        ]
    }
    markers = markers_from_manifest(manifest)
    assert [m.label for m in markers] == ["good", "extra-ok", "has-created"]
    # Unknown extra fields survive for the raw-manifest consumer, but are not
    # projected into the typed model.
    assert markers[1].note == ""
    assert markers[2].created_utc == "2026-01-01T00:00:00Z"
    assert markers[2].note == ""


def test_markers_from_manifest_rejects_non_list() -> None:
    assert markers_from_manifest({"markers": "junk"}) == []
    assert markers_from_manifest({}) == []


def test_marker_as_dict_roundtrip() -> None:
    marker = Marker(label="x", t_ns=3, note="n", created_utc="2026-01-01T00:00:00Z")
    assert Marker.from_entry(marker.as_dict()) == marker
    assert Marker.from_entry({"label": "x", "t_ns": 3}) == Marker(label="x", t_ns=3)


# ------------------------------------------------------------- marker files


def _write_marker_file(tmp_path: Path, lines: list[str], name: str = "markers.jsonl") -> Path:
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_load_marker_file_parses_and_skips_comments(tmp_path: Path) -> None:
    path = _write_marker_file(
        tmp_path,
        [
            "# offset-based cue",
            "",
            '{"label": "go", "offset_s": 2.5, "note": "flex now"}',
            '  {"label": "abs", "t_ns": 12345}  ',
        ],
    )
    specs = load_marker_file(path)
    assert len(specs) == 2
    assert specs[0].label == "go"
    assert specs[0].offset_s == 2.5
    assert specs[0].note == "flex now"
    assert specs[0].t_ns is None
    assert specs[1].label == "abs"
    assert specs[1].t_ns == 12345
    assert specs[1].offset_s is None


def test_load_marker_file_rejects_bad_lines(tmp_path: Path) -> None:
    cases = [
        ([], "empty file"),  # no error at parse; caller rejects zero markers
        (["{bad json"], "not json"),
        (["[1, 2]"], "not an object"),
        (['{"label": "x", "cue": 1}'], "unknown key"),
        (['{"label": "  ", "offset_s": 1}'], "empty label"),
        (['{"label": "x"}'], "neither offset nor t_ns"),
        (['{"label": "x", "offset_s": 1, "t_ns": 2}'], "both offset and t_ns"),
        (['{"label": "x", "offset_s": -1}'], "negative offset"),
        (['{"label": "x", "offset_s": "soon"}'], "string offset"),
        (['{"label": "x", "t_ns": -5}'], "negative t_ns"),
        (['{"label": "x", "t_ns": 1.5}'], "float t_ns"),
        (['{"label": "x", "offset_s": 1, "note": 7}'], "non-string note"),
    ]
    for lines, reason in cases:
        path = _write_marker_file(tmp_path, lines)
        if reason == "empty file":
            assert load_marker_file(path) == []
            continue
        with pytest.raises(ValueError) as excinfo:
            load_marker_file(path)
        assert "line 1" in str(excinfo.value), reason


# -------------------------------------------------------- CLI record wiring


def test_record_marker_file_injection(tmp_path: Path, capsys, monkeypatch) -> None:
    """`record --marker-file` lands markers in the session manifest."""

    calls = {"n": 0}

    def fake_monotonic() -> float:
        value = float(calls["n"])
        calls["n"] += 1
        return value

    monkeypatch.setattr("myopyc.cli.time.monotonic", fake_monotonic)
    monkeypatch.setattr("myopyc.cli.time.sleep", lambda _s: None)
    # Deterministic marker anchor: whatever "now" is, it is fixed.
    monkeypatch.setattr("myopyc.cli.time.monotonic_ns", lambda: 10**12)

    marker_file = _write_marker_file(
        tmp_path,
        [
            '{"label": "rel", "offset_s": 2.5, "note": "cue"}',
            '{"label": "abs", "t_ns": 9876543210}',
        ],
    )

    from myopyc.cli import main

    code = main(
        [
            "record",
            "--max-seconds",
            "0.5",
            "--simulate",
            "1",
            "--output-dir",
            str(tmp_path),
            "--marker-file",
            str(marker_file),
        ]
    )
    assert code == 0

    session_dir = next(path for path in tmp_path.iterdir() if (path / "manifest.json").is_file())
    manifest = read_manifest(session_dir)
    # offset_s resolved against the recording-start monotonic anchor (10^12).
    assert manifest["markers"] == [
        {"label": "rel", "t_ns": 10**12 + 2_500_000_000, "note": "cue"},
        {"label": "abs", "t_ns": 9_876_543_210, "note": ""},
    ]
    # The machine-readable summary exposes the markers too.
    out = capsys.readouterr().out
    summary_start = out.index("{")
    summary, _ = json.JSONDecoder().raw_decode(out[summary_start:])
    assert summary["markers"] == manifest["markers"]


def test_record_rejects_invalid_marker_file(tmp_path: Path, capsys) -> None:
    marker_file = _write_marker_file(tmp_path, ['{"label": "x"}'])
    from myopyc.cli import main

    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "record",
                "--max-seconds",
                "0.5",
                "--simulate",
                "1",
                "--output-dir",
                str(tmp_path),
                "--marker-file",
                str(marker_file),
            ]
        )
    assert excinfo.value.code == 2
    assert "exactly one of 'offset_s' or 't_ns'" in capsys.readouterr().err


def test_record_rejects_empty_marker_file(tmp_path: Path, capsys) -> None:
    marker_file = _write_marker_file(tmp_path, [])
    from myopyc.cli import main

    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "record",
                "--max-seconds",
                "0.5",
                "--simulate",
                "1",
                "--output-dir",
                str(tmp_path),
                "--marker-file",
                str(marker_file),
            ]
        )
    assert excinfo.value.code == 2
    assert "contains no markers" in capsys.readouterr().err


def test_record_rejects_missing_marker_file(tmp_path: Path, capsys) -> None:
    from myopyc.cli import main

    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "record",
                "--max-seconds",
                "0.5",
                "--simulate",
                "1",
                "--output-dir",
                str(tmp_path),
                "--marker-file",
                str(tmp_path / "nope.jsonl"),
            ]
        )
    assert excinfo.value.code == 2
    assert "cannot read marker file" in capsys.readouterr().err


# ------------------------------------------------- append-only compat guard


def test_markers_are_additive_format_version_stays_1(tmp_path) -> None:
    """A session carrying markers is still a valid v1 session with all keys."""

    recorder = SessionRecorder(tmp_path, DEVICES, flush_interval_s=0.05)
    recorder.start()
    recorder.add_marker(Marker(label="m", t_ns=1))
    recorder.stop()
    manifest = read_manifest(recorder.session_dir)
    assert manifest["format"] == "myopyc.session"
    assert manifest["format_version"] == 1
    assert manifest["status"] == "stopped"
    # Nothing pre-existing was displaced.
    for key in ("format", "session_id", "created_utc", "timebase", "chunking", "devices", "gaps"):
        assert key in manifest
