"""Export a recorded session to CSV, NPZ or ZIP for downstream analysis.

CSV: one file per device/stream, with a human-readable ISO UTC timestamp
column alongside the raw ``t_ns`` nanoseconds. NPZ: one compressed archive
with one array per device/stream under ``<device>/<stream>`` keys.

ZIP: an archival export of the *whole session directory* (``manifest.json``
plus every chunk ``.npy``) with a generated ``INTEGRITY.json`` sidecar that
holds a SHA-256 digest per archived file. :func:`verify_session_zip`
recomputes the digests so a transfer can be checked for corruption or
tampering before the archive is trusted/unpacked.
"""

from __future__ import annotations

import hashlib
import json
import os
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from myopyc.recorder.manifest import read_manifest
from myopyc.recorder.reader import SessionData, SessionReader

_CSV_SUFFIX = ".csv"
_ZIP_SUFFIX = ".zip"
_INTEGRITY_NAME = "INTEGRITY.json"


def _iso_utc(t_ns: int) -> str:
    """Render nanoseconds on the host monotonic clock as a UTC ISO string.

    The monotonic clock has no fixed epoch, so this is *relative* time:
    nanoseconds counted from an arbitrary boot point, rendered for
    readability (sorting/grepping), not wall-clock truth.
    """

    return np.datetime64(t_ns, "ns").astype("datetime64[us]").__str__()


def _stream_rows(samples: np.ndarray) -> dict[str, np.ndarray]:
    """Split a structured sample array into columns, with ISO time prepended."""

    cols: dict[str, np.ndarray] = {
        "t_iso": np.array([_iso_utc(int(v)) for v in samples["t_ns"]], dtype=object)
    }
    for name in samples.dtype.names or ():
        cols[name] = samples[name]
    return cols


def export_session_csv(session: SessionData, out_dir: Path) -> list[Path]:
    """Write one CSV per device/stream into ``out_dir``; return the paths."""

    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for key, device in session.devices.items():
        for kind, stream in device.streams.items():
            if stream.samples.size == 0:
                continue
            path = out_dir / f"{key}_{kind.value}{_CSV_SUFFIX}"
            cols = _stream_rows(stream.samples)
            header = ",".join(cols)
            rows = np.stack(
                [np.asarray(v, dtype=object) if v.dtype == object else v for v in cols.values()],
                axis=1,
            )
            with path.open("w", newline="", encoding="utf-8") as fh:
                fh.write(f"{header}\n")
                for row in rows:
                    fh.write(",".join(str(v) for v in row) + "\n")
            written.append(path)
    return written


def export_session_npz(session: SessionData, out_path: Path) -> Path:
    """Write one compressed NPZ with ``<device>/<stream>`` array keys."""

    out_path.parent.mkdir(parents=True, exist_ok=True)
    arrays: dict[str, np.ndarray] = {}
    for key, device in session.devices.items():
        for kind, stream in device.streams.items():
            arrays[f"{key}/{kind.value}"] = stream.samples
    # numpy's savez_compressed stub types **arrays as bool; silence it since the
    # runtime signature is savez_compressed(file, *args, **kwds).
    with out_path.open("wb") as fh:
        np.savez_compressed(fh, **arrays)  # type: ignore[arg-type]
    return out_path


def _iter_session_files(session_dir: Path) -> Iterator[Path]:
    """Yield every regular file under ``session_dir`` (relative, sorted)."""

    for path in sorted(session_dir.rglob("*")):
        if path.is_file():
            yield path.relative_to(session_dir)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _file_digests(session_dir: Path) -> dict[str, str]:
    """Map archive-relative path → SHA-256 hex digest for every session file."""

    return {rel.as_posix(): _sha256(session_dir / rel) for rel in _iter_session_files(session_dir)}


@dataclass
class ZipIntegrityReport:
    """Result of a :func:`verify_session_zip` check."""

    archive: Path
    ok: bool
    checked: int = 0
    missing: list[str] = field(default_factory=list)
    corrupted: list[str] = field(default_factory=list)
    unexpected: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "archive": str(self.archive),
            "ok": self.ok,
            "checked": self.checked,
            "missing": self.missing,
            "corrupted": self.corrupted,
            "unexpected": self.unexpected,
        }


def export_session_zip(session_dir: Path | str, out_path: Path) -> Path:
    """Archive the whole session with a SHA-256 integrity sidecar.

    The archive contains ``manifest.json``, every recorded chunk file and the
    generated ``INTEGRITY.json`` mapping each archived member to its SHA-256
    digest. The freshly written archive is verified before being returned, so
    an export that silently corrupts data fails loudly at export time.
    """

    directory = Path(session_dir)
    read_manifest(directory)  # refuse to export sessions without a valid manifest
    out_path = out_path if out_path.suffix == _ZIP_SUFFIX else out_path.with_suffix(_ZIP_SUFFIX)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    digests = _file_digests(directory)
    payload = {
        "algorithm": "sha256",
        "integrity_version": 1,
        "files": digests,
    }
    tmp = out_path.with_name(out_path.name + ".tmp")
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for rel in _iter_session_files(directory):
                zf.write(directory / rel, arcname=rel.as_posix())
            zf.writestr(_INTEGRITY_NAME, json.dumps(payload, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, out_path)
    finally:
        tmp.unlink(missing_ok=True)

    report = verify_session_zip(out_path)
    if not report.ok:
        raise RuntimeError(
            f"integrity check failed on freshly written {out_path}: {report.as_dict()}"
        )
    return out_path


def verify_session_zip(archive: Path | str) -> ZipIntegrityReport:
    """Recompute SHA-256 digests of every member against ``INTEGRITY.json``.

    Missing, corrupted (digest mismatch) and unexpected (not listed) members
    are reported separately; ``ok`` is ``True`` only when none of them apply
    and at least one expected member was checked.
    """

    archive = Path(archive)
    report = ZipIntegrityReport(archive=archive, ok=False)
    try:
        zf = zipfile.ZipFile(archive)
    except (zipfile.BadZipFile, OSError):
        report.corrupted.append(_INTEGRITY_NAME)
        return report

    with zf:
        try:
            integrity = zf.read(_INTEGRITY_NAME)
        except KeyError:
            report.missing.append(_INTEGRITY_NAME)
            return report
        try:
            payload = json.loads(integrity)
        except (json.JSONDecodeError, UnicodeDecodeError, zipfile.BadZipFile, OSError):
            report.corrupted.append(_INTEGRITY_NAME)
            return report
        listed_raw = payload.get("files", {}) if isinstance(payload, dict) else {}
        if not isinstance(listed_raw, dict):
            report.corrupted.append(_INTEGRITY_NAME)
            return report
        listed: dict[str, str] = listed_raw
        seen: set[str] = set()
        for info in zf.infolist():
            if info.is_dir() or info.filename == _INTEGRITY_NAME:
                continue
            seen.add(info.filename)
            if info.filename not in listed:
                report.unexpected.append(info.filename)
                continue
            report.checked += 1
            try:
                digest = hashlib.sha256(zf.read(info)).hexdigest()
            except (zipfile.BadZipFile, OSError, RuntimeError):
                report.corrupted.append(info.filename)
                continue
            if digest != listed[info.filename]:
                report.corrupted.append(info.filename)
        report.missing = sorted(set(listed) - seen)
    problems = bool(report.missing or report.corrupted or report.unexpected or report.checked == 0)
    report.ok = not problems
    return report


def export_session(session_dir: Path | str, out: Path, fmt: str) -> list[Path]:
    """Export ``session_dir`` in ``fmt`` ('csv', 'npz' or 'zip') into/under ``out``.

    For CSV, ``out`` is a directory. For NPZ/ZIP, ``out`` is the archive file
    (the matching suffix is added if missing).
    """

    fmt = fmt.lower()
    if fmt not in ("csv", "npz", "zip"):
        raise ValueError(f"unsupported export format: {fmt!r} (use 'csv', 'npz' or 'zip')")
    if fmt == "zip":
        return [export_session_zip(session_dir, out)]
    reader = SessionReader(session_dir)
    data: SessionData = reader.load()
    if fmt == "csv":
        return export_session_csv(data, out)
    if out.suffix != ".npz":
        out = out.with_suffix(".npz")
    return [export_session_npz(data, out)]
