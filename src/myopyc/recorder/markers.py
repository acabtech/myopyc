"""Session markers: timestamped, session-scoped annotations.

A marker is a host-monotonic timestamp (``t_ns`` on the same timebase as the
EMG/IMU/EVENT streams), a short label and an optional free-text note, scoped
to the whole session rather than to one device. Markers are persisted as a
new optional top-level ``markers`` array in the manifest — purely additive
under the append-only evolution contract, so ``FORMAT_VERSION`` stays 1 and
readers that predate markers simply ignore the key.

Marker files for ``record --marker-file`` are JSONL: one JSON object per
line, blank lines and ``#`` comment lines skipped. Each object requires a
non-empty ``label`` and exactly one of ``offset_s`` (seconds after recording
start) or ``t_ns`` (absolute host-monotonic nanoseconds); ``note`` is
optional.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_ALLOWED_KEYS = frozenset({"label", "note", "offset_s", "t_ns"})


@dataclass(frozen=True)
class Marker:
    """One user annotation on the session timeline."""

    label: str
    t_ns: int
    note: str = ""
    created_utc: str | None = None

    def as_dict(self) -> dict[str, Any]:
        entry: dict[str, Any] = {"label": self.label, "t_ns": self.t_ns, "note": self.note}
        if self.created_utc is not None:
            entry["created_utc"] = self.created_utc
        return entry

    @classmethod
    def from_entry(cls, entry: Mapping[str, Any]) -> Marker | None:
        """Build a marker from a manifest entry; ``None`` if unusable.

        Readers must tolerate sessions from other writer versions, so a
        malformed entry degrades to a skip instead of an exception.
        """

        label = entry.get("label")
        t_ns = entry.get("t_ns")
        if not isinstance(label, str) or not label:
            return None
        if isinstance(t_ns, bool) or not isinstance(t_ns, int) or t_ns < 0:
            return None
        note = entry.get("note", "")
        created = entry.get("created_utc")
        return Marker(
            label=label,
            t_ns=t_ns,
            note=note if isinstance(note, str) else "",
            created_utc=created if isinstance(created, str) else None,
        )


def markers_from_manifest(manifest: Mapping[str, Any]) -> list[Marker]:
    """Extract the typed marker list from a manifest (tolerant, ordered)."""

    raw = manifest.get("markers", [])
    if not isinstance(raw, list):
        return []
    markers: list[Marker] = []
    for entry in raw:
        if isinstance(entry, Mapping) and (marker := Marker.from_entry(entry)) is not None:
            markers.append(marker)
    return markers


@dataclass(frozen=True)
class MarkerSpec:
    """One validated line of a marker file, before ``t_ns`` resolution."""

    label: str
    note: str
    offset_s: float | None = None  # seconds after recording start
    t_ns: int | None = None  # absolute host-monotonic timestamp


def load_marker_file(path: Path | str) -> list[MarkerSpec]:
    """Parse a JSONL marker file into validated specs, in file order.

    Raises :class:`ValueError` (with a line number) or :class:`OSError` on
    unusable input; the caller decides how to report it.
    """

    specs: list[MarkerSpec] = []
    with Path(path).open(encoding="utf-8") as handle:
        for number, raw in enumerate(handle, start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            try:
                spec = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"line {number}: not valid JSON ({exc.msg})") from exc
            if not isinstance(spec, dict):
                raise ValueError(f"line {number}: expected a JSON object")
            unknown = set(spec) - _ALLOWED_KEYS
            if unknown:
                raise ValueError(f"line {number}: unknown key(s): {', '.join(sorted(unknown))}")
            label = spec.get("label")
            if not isinstance(label, str) or not label.strip():
                raise ValueError(f"line {number}: 'label' must be a non-empty string")
            note = spec.get("note", "")
            if not isinstance(note, str):
                raise ValueError(f"line {number}: 'note' must be a string")
            offset_s = spec.get("offset_s")
            t_ns = spec.get("t_ns")
            if (offset_s is not None) == (t_ns is not None):
                raise ValueError(f"line {number}: exactly one of 'offset_s' or 't_ns' is required")
            if offset_s is not None:
                if (
                    isinstance(offset_s, bool)
                    or not isinstance(offset_s, int | float)
                    or not math.isfinite(offset_s)
                    or offset_s < 0
                ):
                    raise ValueError(f"line {number}: 'offset_s' must be a non-negative number")
                specs.append(MarkerSpec(label=label.strip(), note=note, offset_s=float(offset_s)))
            else:
                if isinstance(t_ns, bool) or not isinstance(t_ns, int) or t_ns < 0:
                    raise ValueError(f"line {number}: 't_ns' must be a non-negative integer")
                specs.append(MarkerSpec(label=label.strip(), note=note, t_ns=t_ns))
    return specs
