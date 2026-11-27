"""Read recorded sessions back into NumPy arrays."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from myopyc.recorder.manifest import read_manifest
from myopyc.recorder.markers import Marker, markers_from_manifest
from myopyc.recorder.repair import RepairReport, contained_chunk_path, repair_session
from myopyc.streams.schema import STREAM_DTYPES, StreamKind


@dataclass
class StreamData:
    kind: StreamKind
    samples: NDArray[Any]
    chunks: list[dict[str, Any]] = field(default_factory=list)
    dropped_samples: int = 0

    def __len__(self) -> int:
        return int(self.samples.size)


@dataclass
class DeviceData:
    key: str
    label: str
    mac: str | None
    streams: dict[StreamKind, StreamData]

    def __getitem__(self, kind: StreamKind | str) -> StreamData:
        return self.streams[StreamKind(kind)]


@dataclass
class SessionData:
    directory: Path
    manifest: dict[str, Any]
    devices: dict[str, DeviceData]

    def __getitem__(self, key: str) -> DeviceData:
        return self.devices[key]

    @property
    def gaps(self) -> list[dict[str, Any]]:
        return list(self.manifest.get("gaps", []))

    @property
    def markers(self) -> list[Marker]:
        """Typed markers from the manifest (empty for pre-marker sessions)."""

        return markers_from_manifest(self.manifest)

    @property
    def notes(self) -> str:
        """Free-text session note (empty for sessions without one)."""

        value = self.manifest.get("notes", "")
        return value if isinstance(value, str) else ""


class SessionReader:
    """Lazy access to a recorded session directory."""

    def __init__(self, session_dir: Path | str) -> None:
        self.directory = Path(session_dir)
        self.manifest = read_manifest(self.directory)

    @property
    def markers(self) -> list[Marker]:
        """Typed markers from the manifest (empty for pre-marker sessions)."""

        return markers_from_manifest(self.manifest)

    @property
    def notes(self) -> str:
        """Free-text session note (empty for sessions without one)."""

        value = self.manifest.get("notes", "")
        return value if isinstance(value, str) else ""

    @property
    def device_keys(self) -> list[str]:
        return list(self.manifest.get("devices", {}))

    def chunks(self, device: str, kind: StreamKind | str) -> list[dict[str, Any]]:
        kind = StreamKind(kind)
        entry = self.manifest["devices"][device]["streams"].get(kind.value, {})
        return list(entry.get("chunks", []))

    def iter_chunks(self, device: str, kind: StreamKind | str) -> Iterator[NDArray[Any]]:
        kind = StreamKind(kind)
        for chunk in self.chunks(device, kind):
            rel = str(chunk.get("file", ""))
            path = contained_chunk_path(self.directory, rel)
            if path is None:
                raise ValueError(f"chunk path {rel!r} is outside session {self.directory}")
            yield np.load(path, allow_pickle=False)

    def load_stream(self, device: str, kind: StreamKind | str) -> NDArray[Any]:
        kind = StreamKind(kind)
        arrays = list(self.iter_chunks(device, kind))
        if not arrays:
            return np.empty(0, dtype=STREAM_DTYPES[kind])
        if len(arrays) == 1:
            return arrays[0]
        return np.concatenate(arrays)

    def load(self) -> SessionData:
        devices: dict[str, DeviceData] = {}
        for key, entry in self.manifest["devices"].items():
            streams: dict[StreamKind, StreamData] = {}
            for kind in StreamKind:
                meta = entry.get("streams", {}).get(kind.value, {})
                streams[kind] = StreamData(
                    kind=kind,
                    samples=self.load_stream(key, kind),
                    chunks=list(meta.get("chunks", [])),
                    dropped_samples=int(meta.get("dropped_samples", 0)),
                )
            devices[key] = DeviceData(
                key=key,
                label=entry.get("label", key),
                mac=entry.get("mac"),
                streams=streams,
            )
        return SessionData(directory=self.directory, manifest=self.manifest, devices=devices)

    def summary(self) -> dict[str, Any]:
        rows = []
        for key, entry in self.manifest["devices"].items():
            for kind in StreamKind:
                meta = entry.get("streams", {}).get(kind.value, {})
                rows.append(
                    {
                        "device": key,
                        "label": entry.get("label", key),
                        "mac": entry.get("mac"),
                        "stream": kind.value,
                        "samples": int(meta.get("samples", 0)),
                        "dropped": int(meta.get("dropped_samples", 0)),
                        "chunks": len(meta.get("chunks", [])),
                    }
                )
        return {
            "session_dir": str(self.directory),
            "session_id": self.manifest.get("session_id"),
            "created_utc": self.manifest.get("created_utc"),
            "status": self.manifest.get("status"),
            "notes": self.notes,
            "gaps": len(self.manifest.get("gaps", [])),
            "markers": [m.as_dict() for m in self.markers],
            "streams": rows,
        }

    def repair(self) -> RepairReport:
        """Reconcile on-disk chunks with the manifest, then reload it.

        Safe on a clean session (a no-op). Intended for the GUI open path and
        ``myopyc inspect --repair``; the constructor itself never mutates the
        session so read-only tooling stays side-effect free unless asked.
        """

        report = repair_session(self.directory)
        self.manifest = read_manifest(self.directory)
        return report


def load_session(session_dir: Path | str) -> SessionData:
    """Convenience wrapper: read every stream of a session into memory."""

    return SessionReader(session_dir).load()


def find_sessions(root: Path | str) -> list[Path]:
    """Return session directories below ``root`` (newest first)."""

    root = Path(root)
    if not root.exists():
        return []
    return sorted(
        (path for path in root.iterdir() if (path / "manifest.json").is_file()),
        key=lambda path: path.name,
        reverse=True,
    )
