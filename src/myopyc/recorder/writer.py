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
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from myopyc.devices.model import SampleBatch
from myopyc.recorder.manifest import MANIFEST_NAME, isoformat, new_manifest, utc_now, write_manifest
from myopyc.recorder.markers import Marker
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
    ) -> None:
        if not devices:
            raise ValueError("at least one device is required")
        self.output_dir = Path(output_dir).expanduser()
        self.devices = {key: dict(meta) for key, meta in devices.items()}
        self.flush_interval_s = max(0.05, float(flush_interval_s))
        self.max_ram_bytes = max(1, int(max_ram_bytes))

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

        self._manifest: dict[str, Any] = new_manifest(
            self.session_id,
            self.devices,
            flush_interval_s=self.flush_interval_s,
            max_ram_bytes=self.max_ram_bytes,
        )
        self._markers: list[Marker] = list(markers or [])

    # ------------------------------------------------------------------ lifecycle
    @property
    def recording(self) -> bool:
        return self._started and not self._stopped

    @property
    def manifest_path(self) -> Path:
        return self.session_dir / MANIFEST_NAME

    def start(self) -> Path:
        """Create the session directory and start the flush thread."""

        with self._lock:
            if self._started:
                raise RuntimeError("recorder already started")
            self.session_dir.mkdir(parents=True, exist_ok=False)
            for key in self.devices:
                for kind in StreamKind:
                    (self.session_dir / "devices" / key / kind.value).mkdir(
                        parents=True, exist_ok=True
                    )
            write_manifest(self.session_dir, self._manifest)
            self._started = True
            self._thread = threading.Thread(target=self._run, name="myopyc-recorder", daemon=True)
            self._thread.start()
        log.info("recording to %s", self.session_dir)
        return self.session_dir

    def stop(self) -> Path:
        """Flush remaining data, finalise the manifest and stop the writer."""

        with self._lock:
            if not self._started:
                raise RuntimeError("recorder was never started")
            if self._stopped:
                return self.session_dir
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(10.0, self.flush_interval_s * 4))
            if thread.is_alive():
                log.warning("recorder writer thread did not stop cleanly")

        self._flush_all()
        with self._lock:
            self._stopped = True
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
                except OSError:
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
        np.save(path, data, allow_pickle=False)
        self._chunk_index[key] = index + 1
        self._written[key] += int(data.size)
        return {
            "file": relative.as_posix(),
            "samples": int(data.size),
            "t_start_ns": int(data["t_ns"][0]),
            "t_end_ns": int(data["t_ns"][-1]),
            "bytes": int(path.stat().st_size),
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
