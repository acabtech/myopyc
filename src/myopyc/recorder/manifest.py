"""Session manifest creation, atomic writes and validation.

Append-only compatibility contract (``FORMAT_VERSION == 1``): manifests may gain
new optional top-level fields, per-device/per-stream fields, chunk fields, or
entirely new stream kinds over time. Readers must ignore keys they do not
understand and must not require new optional fields to be present; writers must
only add new optional fields and must never repurpose existing ones. Existing
sessions remain readable forever by v0.1+ readers as long as this rule holds.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from myopyc import __version__
from myopyc.streams.schema import StreamKind, stream_meta

FORMAT = "myopyc.session"
FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"


def utc_now() -> datetime:
    return datetime.now(UTC)


def isoformat(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_manifest(
    session_id: str,
    devices: Mapping[str, Mapping[str, Any]],
    *,
    flush_interval_s: float,
    max_ram_bytes: int,
) -> dict[str, Any]:
    """Build the initial manifest for a recording session."""

    device_entries: dict[str, Any] = {}
    for key, meta in devices.items():
        device_entries[key] = {
            "label": meta.get("label", key),
            "mac": meta.get("mac"),
            "name": meta.get("name", "Myo"),
            "firmware": meta.get("firmware"),
            "streams": {
                kind.value: {
                    **stream_meta(kind),
                    "chunks": [],
                    "samples": 0,
                    "dropped_samples": 0,
                }
                for kind in StreamKind
            },
        }

    return {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "session_id": session_id,
        "app_version": __version__,
        "created_utc": isoformat(utc_now()),
        "stopped_utc": None,
        "status": "recording",
        "timebase": "host-monotonic-ns",
        "chunking": {
            "mode": "time",
            "flush_interval_s": flush_interval_s,
            "max_ram_bytes": max_ram_bytes,
        },
        "devices": device_entries,
        "gaps": [],
    }


def write_manifest(session_dir: Path, manifest: Mapping[str, Any]) -> Path:
    """Atomically write ``manifest.json`` inside ``session_dir``."""

    target = Path(session_dir) / MANIFEST_NAME
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    os.replace(tmp, target)
    return target


def read_manifest(session_dir: Path | str) -> dict[str, Any]:
    path = Path(session_dir) / MANIFEST_NAME
    with path.open(encoding="utf-8") as handle:
        manifest = json.load(handle)
    if manifest.get("format") != FORMAT:
        raise ValueError(f"{path} is not a myopyc session manifest")
    return manifest
