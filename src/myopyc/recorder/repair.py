"""Crash-safe session repair.

If the app dies mid-flush (SIGKILL, power loss, disk full) a session can be left
with files on disk that disagree with ``manifest.json``:

- **Orphaned chunks** — a ``.npy`` written by ``np.save`` but never registered
  in the manifest, because the process died before the atomic manifest rewrite.
- **Truncated chunks** — a partially written ``.npy`` from an interrupted save.
  These surface either as unlisted files or as listed files that fail to load.
- **Missing chunks** — listed in the manifest but absent on disk.

``repair_session`` reconciles the two views against each other *without ever
rewriting chunk data* (chunks are immutable once written). Unlisted or unreadable
files are moved aside into a quarantine folder; manifest entries that do not
correspond to a readable, correctly sized chunk file are dropped. Sample counts
are then recomputed from the surviving chunks and the manifest is rewritten
atomically. A session interrupted mid-recording has its status changed to
``"repaired"``.

Repaired data through the last complete flush is preserved exactly. The
operation is idempotent: repairing an already-clean session changes nothing and
reports no actions.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from myopyc.recorder.manifest import (
    MANIFEST_NAME,
    isoformat,
    read_manifest,
    utc_now,
    write_manifest,
)
from myopyc.streams.schema import STREAM_DTYPES, StreamKind

log = logging.getLogger(__name__)

# Folder (inside the session dir, outside the device scan roots) that receives
# chunk files which exist on disk but cannot be trusted as part of the session.
QUARANTINE_DIR = "unrepaired"

_REPAIRED = "repaired"


@dataclass
class RepairReport:
    """Structured summary of what :func:`repair_session` did (or verified)."""

    changed: bool = False
    orphaned: list[str] = field(default_factory=list)
    quarantined: list[str] = field(default_factory=list)
    unlisted: list[str] = field(default_factory=list)
    mismatched: list[str] = field(default_factory=list)
    status_before: str | None = None
    status_after: str | None = None
    dropped_samples: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "changed": self.changed,
            "status_before": self.status_before,
            "status_after": self.status_after,
            "orphaned": list(self.orphaned),
            "quarantined": list(self.quarantined),
            "unlisted": list(self.unlisted),
            "mismatched": list(self.mismatched),
            "dropped_samples": dict(self.dropped_samples),
        }


def _is_chunk_name(name: str) -> bool:
    return name.startswith("chunk-") and name.endswith(".npy")


def _stream_roots(session_dir: Path) -> list[tuple[str, str, Path]]:
    """Yield ``(device_key, stream_name, directory)`` for every stream folder."""

    roots: list[tuple[str, str, Path]] = []
    devices_dir = session_dir / "devices"
    if not devices_dir.is_dir():
        return roots
    for device_dir in sorted(p for p in devices_dir.iterdir() if p.is_dir()):
        for stream_dir in sorted(p for p in device_dir.iterdir() if p.is_dir()):
            roots.append((device_dir.name, stream_dir.name, stream_dir))
    return roots


def _loadable(path: Path, expected_dtype: np.dtype[Any] | None) -> bool:
    """Return True if ``path`` is a complete ``.npy`` matching ``expected_dtype``."""

    try:
        with path.open("rb") as handle:
            array = np.load(handle, allow_pickle=False)
    except Exception:  # any failure means "not trustworthy"
        return False
    return expected_dtype is None or array.dtype == expected_dtype


def repair_session(session_dir: Path | str, *, dry_run: bool = False) -> RepairReport:
    """Reconcile on-disk chunks with ``manifest.json`` for a crashed session.

    Parameters
    ----------
    session_dir:
        A session directory containing (or meant to contain) ``manifest.json``.
    dry_run:
        Analyse and report, but move no files and write no manifest.

    Returns
    -------
    RepairReport
        Details of every quarantine / unlist action and the status transition.
    """

    session_dir = Path(session_dir)
    report = RepairReport()

    try:
        manifest = read_manifest(session_dir)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{session_dir} is not a repairable myopyc session: {exc}") from exc

    report.status_before = manifest.get("status")
    devices: dict[str, Any] = manifest.get("devices", {})

    # Map every chunk the manifest lists to its expected location, keyed by the
    # relative POSIX path (the same string stored in ``chunk["file"]``).
    listed: dict[str, tuple[str, str, dict[str, Any]]] = {}
    for device_key, entry in devices.items():
        for stream_name, stream in entry.get("streams", {}).items():
            for chunk in stream.get("chunks", []):
                rel = str(chunk.get("file", ""))
                if rel:
                    listed[rel] = (device_key, stream_name, chunk)

    # 1. Unlist manifest chunks whose files are missing or unreadable (truncated).
    #    Their sample counts are tallied per stream so totals can be patched, and
    #    truncated files are quarantined alongside orphans.
    for device_key, entry in devices.items():
        for stream_name, stream in entry.get("streams", {}).items():
            stream_key = f"{device_key}/{stream_name}"
            survivors: list[dict[str, Any]] = []
            for chunk in stream.get("chunks", []):
                rel = str(chunk.get("file", ""))
                path = session_dir / rel
                if not rel or not path.is_file():
                    report.unlisted.append(rel or "<empty path>")
                    report.dropped_samples[stream_key] = report.dropped_samples.get(
                        stream_key, 0
                    ) + int(chunk.get("samples", 0))
                    continue
                dtype = STREAM_DTYPES.get(StreamKind(stream_name))
                if dtype is not None and not _loadable(path, dtype):
                    report.mismatched.append(rel)
                    report.dropped_samples[stream_key] = report.dropped_samples.get(
                        stream_key, 0
                    ) + int(chunk.get("samples", 0))
                    if not dry_run:
                        _quarantine(session_dir, path, report)
                    continue
                survivors.append(chunk)
            if len(survivors) != len(stream.get("chunks", [])):
                report.changed = True
            stream["chunks"] = survivors
            stream["samples"] = sum(int(chunk.get("samples", 0)) for chunk in survivors)

    # 2. Quarantine chunk files on disk that the manifest never listed (orphans),
    #    including truncated last chunks that were cut off before registration.
    for _device, _stream, stream_dir in _stream_roots(session_dir):
        for path in sorted(stream_dir.iterdir()):
            if not (path.is_file() and _is_chunk_name(path.name)):
                continue
            rel = path.relative_to(session_dir).as_posix()
            if rel in listed:
                continue
            report.orphaned.append(rel)
            report.changed = True
            if not dry_run:
                _quarantine(session_dir, path, report)

    # 3. A session interrupted mid-recording becomes "repaired" once we have
    #    reconciled its chunks, so it is never mistaken for an active capture.
    was_recording = report.status_before == "recording"
    if was_recording:
        report.status_after = _REPAIRED
        report.changed = True
        if not dry_run:
            manifest["status"] = _REPAIRED
            manifest["stopped_utc"] = manifest.get("stopped_utc") or isoformat(utc_now())
    else:
        report.status_after = str(manifest.get("status"))

    if report.changed and not dry_run:
        if not _manifest_backed_up(session_dir):
            _backup_manifest(session_dir)
        write_manifest(session_dir, manifest)

    if report.changed:
        log.info(
            "repaired %s: quarantined=%d unlisted=%d mismatched=%d orphaned=%d",
            session_dir.name,
            len(report.quarantined),
            len(report.unlisted),
            len(report.mismatched),
            len(report.orphaned),
        )
    else:
        log.debug("no repair needed for %s", session_dir.name)

    return report


def needs_repair(session_dir: Path | str) -> bool:
    """Cheap read-only probe: would :func:`repair_session` change anything?"""

    return repair_session(session_dir, dry_run=True).changed


def _quarantine(session_dir: Path, path: Path, report: RepairReport) -> None:
    rel = path.relative_to(session_dir).as_posix()
    target = session_dir / QUARANTINE_DIR / Path(rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(path, target)
        report.quarantined.append(rel)
        report.changed = True
    except OSError:
        # A file we cannot move is still untrustworthy; drop it from the manifest
        # path above rather than aborting the whole repair.
        log.exception("could not quarantine %s", path)


def _backup_manifest(session_dir: Path) -> None:
    manifest = session_dir / MANIFEST_NAME
    if manifest.is_file():
        os.replace(manifest, session_dir / "manifest.json.bak")


def _manifest_backed_up(session_dir: Path) -> bool:
    return (session_dir / "manifest.json.bak").is_file()
