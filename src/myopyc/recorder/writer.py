"""RAM-buffered, time-based chunked recorder.

Samples arrive on BLE threads and are accepted without touching the disk. A
single writer thread drains the buffers every ``flush_interval_s`` seconds and
writes one ``.npy`` chunk per device/stream. If the RAM cap is reached between
flushes the oldest buffered samples are dropped and an explicit gap entry is
recorded in the manifest.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from myopyc.devices.model import SampleBatch
from myopyc.recorder.manifest import (
    MANIFEST_NAME,
    isoformat,
    new_manifest,
    read_manifest,
    utc_now,
    write_manifest,
)
from myopyc.recorder.markers import Marker, markers_from_manifest
from myopyc.streams.schema import STREAM_DTYPES, StreamKind

log = logging.getLogger(__name__)

CHUNK_FORMAT = "chunk-{index:05d}.npy"


class SessionRecorder:
    """Record one session into a timestamped directory.

    Parameters
    ----------
    output_dir:
        Root directory that receives session folders.
    devices:
        Mapping of device key to static metadata (``label``, ``mac``, ...).
    flush_interval_s:
        Wall-clock interval between chunk flushes.
    max_ram_bytes:
        Total RAM budget across all device/stream buffers before oldest samples
        are dropped and recorded as gaps.
    session_name:
        Optional folder suffix; the leading UTC timestamp is always added.
    """

    def __init__(
        self,
        output_dir: Path | str,
        devices: Mapping[str, Mapping[str, Any]],
        *,
        flush_interval_s: float = 2.0,
        max_ram_bytes: int = 64 * 1024 * 1024,
        session_name: str | None = None,
        markers: list[Marker] | None = None,
        _resume_session_dir: Path | None = None,
        _resume_manifest: dict[str, Any] | None = None,
    ) -> None:
        if not devices:
            raise ValueError("at least one device is required")
        self.output_dir = Path(output_dir).expanduser()
        self.devices = {key: dict(meta) for key, meta in devices.items()}
        self.flush_interval_s = max(0.05, float(flush_interval_s))
        self.max_ram_bytes = max(1, int(max_ram_bytes))
        self._resuming = _resume_session_dir is not None

        if self._resuming:
            if _resume_manifest is None:
                raise ValueError("resume manifest is required when resuming")
            self.session_dir = Path(_resume_session_dir)
            self.session_id = str(_resume_manifest.get("session_id") or self.session_dir.name)
        else:
            stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
            suffix = f"_{session_name}" if session_name else ""
            self.session_id = f"{stamp}_{uuid.uuid4().hex[:6]}"
            self.session_dir = self.output_dir / f"{self.session_id}{suffix}"

        stream_count = len(self.devices) * len(StreamKind)
        self._per_stream_cap = max(1, self.max_ram_bytes // stream_count)

        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = False
        self._stopping = False
        self._stopped = False

        self._buffers: dict[tuple[str, StreamKind], list[np.ndarray[Any, Any]]] = {
            (key, kind): [] for key in self.devices for kind in StreamKind
        }
        self._buffer_bytes: dict[tuple[str, StreamKind], int] = {key: 0 for key in self._buffers}
        self._chunk_index: dict[tuple[str, StreamKind], int] = {key: 0 for key in self._buffers}
        self._chunks: dict[tuple[str, StreamKind], list[dict[str, Any]]] = {
            key: [] for key in self._buffers
        }
        self._received: dict[tuple[str, StreamKind], int] = {key: 0 for key in self._buffers}
        self._written: dict[tuple[str, StreamKind], int] = {key: 0 for key in self._buffers}
        self._dropped: dict[tuple[str, StreamKind], int] = {key: 0 for key in self._buffers}
        self._gaps: list[dict[str, Any]] = []

        if self._resuming:
            self._manifest = dict(_resume_manifest)
            self._load_resume_state()
            if markers:
                self._markers.extend(markers)
                self._manifest["markers"] = [m.as_dict() for m in self._markers]
        else:
            self._manifest = new_manifest(
                self.session_id,
                self.devices,
                flush_interval_s=self.flush_interval_s,
                max_ram_bytes=self.max_ram_bytes,
            )
            self._markers = list(markers or [])

    # ------------------------------------------------------------------ lifecycle
    @property
    def recording(self) -> bool:
        return self._started and not self._stopping and not self._stopped

    @property
    def manifest_path(self) -> Path:
        return self.session_dir / MANIFEST_NAME

    def start(self) -> Path:
        """Create or reopen the session directory and start the flush thread."""

        with self._lock:
            if self._started:
                raise RuntimeError("recorder already started")
            self.session_dir.mkdir(parents=True, exist_ok=self._resuming)
            for key in self.devices:
                for kind in StreamKind:
                    (self.session_dir / "devices" / key / kind.value).mkdir(
                        parents=True, exist_ok=True
                    )
            if self._resuming:
                self._manifest["status"] = "recording"
                self._manifest["stopped_utc"] = None
                now = isoformat(utc_now())
                self._manifest["resumed_utc"] = now
                self._manifest["resume_count"] = int(self._manifest.get("resume_count", 0)) + 1
                self._manifest.setdefault("resume_history", [])
                if isinstance(self._manifest["resume_history"], list):
                    self._manifest["resume_history"].append({"resumed_utc": now})
            write_manifest(self.session_dir, self._manifest)
            self._started = True
            self._thread = threading.Thread(target=self._run, name="myopyc-recorder", daemon=True)
            self._thread.start()
        log.info("%s to %s", "resuming recording" if self._resuming else "recording", self.session_dir)
        return self.session_dir

    def stop(self) -> Path:
        """Flush remaining data, finalise the manifest and stop the writer."""

        with self._lock:
            if not self._started:
                raise RuntimeError("recorder was never started")
            if self._stopped:
                return self.session_dir
            self._stopping = True
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(10.0, self.flush_interval_s * 4))
            if thread.is_alive():
                log.warning("recorder writer thread did not stop cleanly")

        # Drain until quiescent. New add() calls are rejected while stopping so
        # samples accepted before this point are either flushed or, on persistent
        # write failure, remain buffered and reported.
        for _ in range(10):
            self._flush_all()
            with self._lock:
                has_pending = any(self._buffers[key] for key in self._buffers)
                if not has_pending:
                    break
            time.sleep(0.05)
        else:
            with self._lock:
                pending = sum(len(self._buffers[key]) for key in self._buffers)
            if pending:
                log.error(
                    "could not flush %d pending buffer(s) during stop; data retained in memory only",
                    pending,
                )

        with self._lock:
            self._stopped = True
            self._stopping = False
            self._manifest["status"] = "stopped"
            self._manifest["stopped_utc"] = isoformat(utc_now())
            write_manifest(self.session_dir, self._manifest)
        log.info("recording finalised: %s", self.session_dir)
        return self.session_dir

    # ------------------------------------------------------------------ ingestion
    def add(self, batch: SampleBatch) -> None:
        """Accept a sample batch from any thread. Never blocks on disk I/O."""

        key = (batch.device, batch.kind)
        if key not in self._buffers:
            raise KeyError(f"device {batch.device!r} was not declared for this session")
        if batch.kind not in STREAM_DTYPES:
            raise ValueError(f"unsupported stream {batch.kind!r}")
        if batch.data.size == 0:
            return

        array = np.asarray(batch.data, dtype=STREAM_DTYPES[batch.kind])
        with self._lock:
            if not self.recording:
                return
            self._buffers[key].append(array)
            size = int(array.nbytes)
            self._buffer_bytes[key] += size
            self._received[key] += int(array.size)

            while self._buffer_bytes[key] > self._per_stream_cap and len(self._buffers[key]) > 1:
                dropped = self._buffers[key].pop(0)
                self._buffer_bytes[key] -= int(dropped.nbytes)
                self._dropped[key] += int(dropped.size)
                self._gaps.append(
                    {
                        "device": batch.device,
                        "stream": batch.kind.value,
                        "reason": "ram-buffer-overflow",
                        "t_start_ns": int(dropped["t_ns"][0]),
                        "t_end_ns": int(dropped["t_ns"][-1]),
                        "samples": int(dropped.size),
                        "recorded_utc": isoformat(utc_now()),
                    }
                )

    # ------------------------------------------------------------------ markers
    def add_marker(self, marker: Marker) -> None:
        """Record one marker (already resolved to an absolute ``t_ns``).

        Thread-safe; safe before, during, or after recording. While recording
        the marker rides the next atomic manifest rewrite (the next flush or
        :meth:`stop`); outside a running session it is flushed to
        ``manifest.json`` immediately, so late annotations survive.
        """

        with self._lock:
            self._markers.append(marker)
            self._manifest["markers"] = [m.as_dict() for m in self._markers]
        if not self.recording and self._started:
            # Not running (pre-start or post-stop): flush immediately so late
            # annotations survive without a subsequent rewrite.
            with self._write_lock:
                write_manifest(self.session_dir, self._manifest)

    # ------------------------------------------------------------------ resume
    def _load_resume_state(self) -> None:
        """Seed counters/indices from an existing manifest for append-resume."""

        manifest_devices = self._manifest.get("devices", {})
        self._gaps = list(self._manifest.get("gaps", []))
        self._markers = markers_from_manifest(self._manifest)
        for key, meta in self.devices.items():
            device_entry = manifest_devices.get(key)
            if not isinstance(device_entry, dict):
                device_entry = {
                    "label": meta.get("label", key),
                    "mac": meta.get("mac"),
                    "name": meta.get("name", "Myo"),
                    "firmware": meta.get("firmware"),
                    "streams": {},
                }
                manifest_devices[key] = device_entry
            device_entry.setdefault("label", meta.get("label", key))
            device_entry.setdefault("mac", meta.get("mac"))
            device_entry.setdefault("name", meta.get("name", "Myo"))
            device_entry.setdefault("firmware", meta.get("firmware"))
            streams = device_entry.setdefault("streams", {})
            for kind in StreamKind:
                skey = (key, kind)
                stream_entry = streams.get(kind.value)
                if not isinstance(stream_entry, dict):
                    stream_entry = {"chunks": [], "samples": 0, "dropped_samples": 0}
                    streams[kind.value] = stream_entry
                chunks = list(stream_entry.get("chunks", []))
                self._chunks[skey] = chunks
                self._written[skey] = int(stream_entry.get("samples", 0))
                self._dropped[skey] = int(stream_entry.get("dropped_samples", 0))
                self._chunk_index[skey] = self._next_chunk_index(key, kind, chunks)
        self._manifest["devices"] = manifest_devices

    def _next_chunk_index(
        self, device: str, kind: StreamKind, chunks: list[dict[str, Any]]
    ) -> int:
        """Compute next chunk index, never colliding with existing chunk files."""

        indices: list[int] = []
        for chunk in chunks:
            file_name = Path(str(chunk.get("file", ""))).name
            if file_name.startswith("chunk-") and file_name.endswith(".npy"):
                try:
                    indices.append(int(file_name[len("chunk-") : -len(".npy")]))
                except ValueError:
                    continue
        stream_dir = self.session_dir / "devices" / device / kind.value
        if stream_dir.is_dir():
            for path in stream_dir.glob("chunk-*.npy"):
                try:
                    indices.append(int(path.stem.split("-", 1)[1]))
                except (IndexError, ValueError):
                    continue
        return max(indices, default=-1) + 1

    # ------------------------------------------------------------------ internals
    def _run(self) -> None:
        while not self._stop_event.wait(self.flush_interval_s):
            try:
                self._flush_all()
            except Exception:  # pragma: no cover - defensive: keep recording alive
                log.exception("chunk flush failed; buffers retained for next attempt")
        try:
            self._flush_all()
        except Exception:  # pragma: no cover
            log.exception("final chunk flush failed")

    def _flush_all(self) -> None:
        with self._write_lock:
            with self._lock:
                pending: dict[tuple[str, StreamKind], list[np.ndarray[Any, Any]]] = {}
                for key in self._buffers:
                    if self._buffers[key]:
                        pending[key] = self._buffers[key]
                        self._buffers[key] = []
                        self._buffer_bytes[key] = 0

                if not pending:
                    self._refresh_manifest_counters()
                    write_manifest(self.session_dir, self._manifest)
                    return

            new_chunks: list[tuple[tuple[str, StreamKind], dict[str, Any]]] = []
            written: list[tuple[tuple[str, StreamKind], list[np.ndarray[Any, Any]]]] = []
            for key, arrays in pending.items():
                try:
                    chunk_meta = self._write_chunk(key, arrays)
                except Exception:
                    log.exception("could not write chunk for %s/%s", *key)
                    with self._lock:
                        self._buffers[key][:0] = arrays
                        self._buffer_bytes[key] += sum(int(a.nbytes) for a in arrays)
                    continue
                new_chunks.append((key, chunk_meta))
                written.append((key, arrays))

            with self._lock:
                for key, chunk_meta in new_chunks:
                    self._chunks[key].append(chunk_meta)
                    self._chunk_index[key] += 1
                    self._written[key] += int(chunk_meta["samples"])
                self._refresh_manifest_counters()
                write_manifest(self.session_dir, self._manifest)

            if written:
                total = sum(int(a.size) for _, arrays in written for a in arrays)
                log.debug("flushed %d samples in %d chunks", total, len(written))

    def _write_chunk(
        self, key: tuple[str, StreamKind], arrays: list[np.ndarray[Any, Any]]
    ) -> dict[str, Any]:
        device, kind = key
        index = self._chunk_index[key]
        relative = Path("devices") / device / kind.value / CHUNK_FORMAT.format(index=index)
        path = self.session_dir / relative
        data = np.concatenate(arrays) if len(arrays) > 1 else arrays[0]
        try:
            np.save(path, data, allow_pickle=False)
            size = int(path.stat().st_size)
        except Exception:
            # Never leave a partial/orphan chunk behind when the write path fails.
            # Counters are committed by the caller only after a successful return.
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                log.warning("could not remove incomplete chunk %s", path, exc_info=True)
            raise
        return {
            "file": relative.as_posix(),
            "samples": int(data.size),
            "t_start_ns": int(data["t_ns"][0]),
            "t_end_ns": int(data["t_ns"][-1]),
            "bytes": size,
        }

    def _refresh_manifest_counters(self) -> None:
        for (device, kind), chunks in self._chunks.items():
            entry = self._manifest["devices"][device]["streams"][kind.value]
            entry["chunks"] = chunks
            entry["samples"] = self._written[(device, kind)]
            entry["dropped_samples"] = self._dropped[(device, kind)]
        self._manifest["gaps"] = self._gaps
        if self._markers:
            self._manifest["markers"] = [m.as_dict() for m in self._markers]

    # ------------------------------------------------------------------ status
    def stats(self) -> dict[str, Any]:
        with self._lock:
            buffered = sum(self._buffer_bytes.values())
            received = sum(self._received.values())
            dropped = sum(self._dropped.values())
            chunks = sum(len(items) for items in self._chunks.values())
            gaps = len(self._gaps)
        return {
            "session_dir": str(self.session_dir),
            "recording": self.recording,
            "buffered_bytes": buffered,
            "received_samples": received,
            "dropped_samples": dropped,
            "chunks_written": chunks,
            "gaps": gaps,
        }


def _manifest_devices_for_resume(manifest: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    devices: dict[str, dict[str, Any]] = {}
    for key, entry in manifest.get("devices", {}).items():
        if not isinstance(entry, dict):
            continue
        devices[str(key)] = {
            "label": entry.get("label", key),
            "mac": entry.get("mac"),
            "name": entry.get("name", "Myo"),
            "firmware": entry.get("firmware"),
        }
    return devices


def find_interrupted_sessions(root: Path | str) -> list[Path]:
    """Return sessions whose manifest status indicates an interrupted recording."""

    root = Path(root).expanduser()
    if not root.exists():
        return []
    interrupted: list[Path] = []
    for path in sorted(root.iterdir()):
        manifest_path = path / MANIFEST_NAME
        if not manifest_path.is_file():
            continue
        try:
            manifest = read_manifest(path)
        except Exception:
            continue
        if manifest.get("status") == "recording":
            interrupted.append(path)
    return sorted(interrupted, key=lambda p: p.name, reverse=True)


def resume_recorder(
    session_dir: Path | str,
    *,
    flush_interval_s: float | None = None,
    max_ram_bytes: int | None = None,
    markers: list[Marker] | None = None,
) -> SessionRecorder:
    """Resume an interrupted session into a new continuing chunk series.

    Existing chunks/manifests are reconciled via :func:`repair_session` first so
    resume never appends over an inconsistent mid-crash state. The recorder keeps
    the same session directory and session id, but writes new chunk files whose
    indices continue after the highest existing chunk on disk/manifest.
    """

    from myopyc.recorder.repair import repair_session

    session_dir = Path(session_dir).expanduser()
    report = repair_session(session_dir)
    manifest = read_manifest(session_dir)
    status = manifest.get("status")
    if status not in {"repaired", "stopped"}:
        raise ValueError(
            f"session {session_dir.name} has status {status!r}; only repaired/stopped sessions can be resumed"
        )

    devices = _manifest_devices_for_resume(manifest)
    if not devices:
        raise ValueError(f"session {session_dir.name} has no devices to resume")

    chunking = manifest.get("chunking", {}) if isinstance(manifest.get("chunking"), dict) else {}
    flush = (
        float(flush_interval_s)
        if flush_interval_s is not None
        else float(chunking.get("flush_interval_s", 2.0))
    )
    ram = (
        int(max_ram_bytes)
        if max_ram_bytes is not None
        else int(chunking.get("max_ram_bytes", 64 * 1024 * 1024))
    )

    recorder = SessionRecorder(
        session_dir.parent,
        devices,
        flush_interval_s=flush,
        max_ram_bytes=ram,
        markers=markers,
        _resume_session_dir=session_dir,
        _resume_manifest=manifest,
    )
    recorder.start()
    log.info(
        "resumed session %s after repair changed=%s quarantined=%d",
        session_dir.name,
        report.changed,
        len(report.quarantined),
    )
    return recorder


def open_recorder(
    output_dir: Path | str,
    devices: Mapping[str, Mapping[str, Any]],
    *,
    flush_interval_s: float = 2.0,
    max_ram_bytes: int = 64 * 1024 * 1024,
    session_name: str | None = None,
    markers: list[Marker] | None = None,
) -> SessionRecorder:
    """Create and start a :class:`SessionRecorder` in one call."""

    recorder = SessionRecorder(
        output_dir,
        devices,
        flush_interval_s=flush_interval_s,
        max_ram_bytes=max_ram_bytes,
        session_name=session_name,
        markers=markers,
    )
    recorder.start()
    return recorder
