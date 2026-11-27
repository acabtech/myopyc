"""Regressions for integrity verification of untrusted ZIP archives."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from myopyc.recorder.export import verify_session_zip

INTEGRITY = "INTEGRITY.json"


def _archive(path: Path, **members: bytes) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


def test_missing_integrity_sidecar_reports_failure(tmp_path: Path) -> None:
    archive = _archive(tmp_path / "no-integrity.zip", **{"manifest.json": b"{}\n"})

    report = verify_session_zip(archive)

    assert report.ok is False
    assert INTEGRITY in report.missing
    assert report.corrupted == []


def test_invalid_integrity_json_reports_failure(tmp_path: Path) -> None:
    archive = _archive(
        tmp_path / "bad-json.zip",
        **{"manifest.json": b"{}\n", INTEGRITY: b"{ not json"},
    )

    report = verify_session_zip(archive)

    assert report.ok is False
    assert INTEGRITY in report.corrupted


def test_malformed_integrity_payload_reports_failure(tmp_path: Path) -> None:
    archive = _archive(
        tmp_path / "bad-payload.zip",
        **{INTEGRITY: json.dumps({"files": []}).encode()},
    )

    report = verify_session_zip(archive)

    assert report.ok is False
    assert INTEGRITY in report.corrupted


def test_non_zip_input_reports_failure(tmp_path: Path) -> None:
    archive = tmp_path / "not-a-zip.zip"
    archive.write_bytes(b"definitely not a zip archive")

    report = verify_session_zip(archive)

    assert report.ok is False
    assert INTEGRITY in report.corrupted
