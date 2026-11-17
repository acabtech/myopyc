"""Bounded in-memory view of the most recent samples, for live plotting."""

from __future__ import annotations

import threading
from collections import deque
from typing import Any

import numpy as np
from numpy.typing import NDArray

from myopyc.devices.model import SampleBatch
from myopyc.streams.schema import STREAM_DTYPES, StreamKind


class LiveBuffer:
    """Thread-safe ring of the last ``max_samples`` rows per device/stream."""

    def __init__(self, max_samples: int = 4096) -> None:
        self.max_samples = max_samples
        self._rows: dict[tuple[str, StreamKind], deque[Any]] = {}
        self._lock = threading.Lock()

    def append(self, batch: SampleBatch) -> None:
        key = (batch.device, batch.kind)
        with self._lock:
            rows = self._rows.get(key)
            if rows is None:
                rows = deque(maxlen=self.max_samples)
                self._rows[key] = rows
            rows.extend(batch.data)

    def snapshot(self, device: str, kind: StreamKind) -> NDArray[Any]:
        """Return the buffered samples in time order (empty typed array if none)."""

        key = (device, kind)
        with self._lock:
            rows = list(self._rows.get(key, ()))
        if not rows:
            return np.empty(0, dtype=STREAM_DTYPES[kind])
        return np.array(rows, dtype=STREAM_DTYPES[kind])

    def devices(self) -> set[str]:
        with self._lock:
            return {device for device, _ in self._rows}

    def clear(self, device: str | None = None) -> None:
        with self._lock:
            if device is None:
                self._rows.clear()
            else:
                for key in [k for k in self._rows if k[0] == device]:
                    del self._rows[key]
