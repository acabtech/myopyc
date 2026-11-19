"""Coordinate up to two armband sources and fan out samples to subscribers."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any, Literal

from myopyc.devices.adapter import DlMyoSource
from myopyc.devices.model import DeviceInfo, SampleBatch, unique_key
from myopyc.devices.simulator import SimulatedSource
from myopyc.streams.schema import StreamKind

log = logging.getLogger(__name__)

DeviceState = Literal["idle", "connecting", "streaming", "stopping", "stopped", "error"]
SampleFn = Callable[[SampleBatch], None]

MAX_DEVICES = 2


class DeviceManager:
    """Own the live sources, their status and the subscriber fan-out."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sources: dict[str, Any] = {}
        self._info: dict[str, DeviceInfo] = {}
        self._state: dict[str, DeviceState] = {}
        self._detail: dict[str, str | None] = {}
        self._battery: dict[str, int | None] = {}
        self._counts: dict[str, dict[StreamKind, int]] = {}
        self._subscribers: list[SampleFn] = []

    # ------------------------------------------------------------------ accessors
    @property
    def keys(self) -> list[str]:
        with self._lock:
            return list(self._info)

    def info(self, key: str) -> DeviceInfo:
        return self._info[key]

    def state(self, key: str) -> DeviceState:
        return self._state.get(key, "idle")

    def detail(self, key: str) -> str | None:
        return self._detail.get(key)

    def battery(self, key: str) -> int | None:
        return self._battery.get(key)

    def counts(self, key: str) -> dict[StreamKind, int]:
        return dict(self._counts.get(key, {kind: 0 for kind in StreamKind}))

    def meta(self) -> dict[str, dict[str, Any]]:
        """Static device metadata for the session manifest."""

        with self._lock:
            return {
                key: {
                    "label": info.label,
                    "mac": info.mac,
                    "name": info.name,
                    "firmware": info.firmware,
                }
                for key, info in self._info.items()
            }

    def has(self, key: str) -> bool:
        return key in self._info

    # ------------------------------------------------------------------ wiring
    def subscribe(self, callback: SampleFn) -> None:
        self._subscribers.append(callback)

    def _emit(self, batch: SampleBatch) -> None:
        with self._lock:
            counts = self._counts.setdefault(batch.device, {kind: 0 for kind in StreamKind})
            counts[batch.kind] = counts.get(batch.kind, 0) + int(batch.data.size)
        for callback in list(self._subscribers):
            try:
                callback(batch)
            except Exception:
                log.exception("sample subscriber failed")

    def _on_status(self, key: str, state: str, detail: str | None) -> None:
        with self._lock:
            self._state[key] = state  # type: ignore[assignment]
            self._detail[key] = detail
        log.info("%s: %s%s", key, state, f" ({detail})" if detail else "")

    def _on_battery(self, key: str, level: int) -> None:
        with self._lock:
            self._battery[key] = level

    def _register(self, info: DeviceInfo) -> None:
        with self._lock:
            self._info[info.key] = info
            self._state[info.key] = "idle"
            self._detail[info.key] = None
            self._battery[info.key] = None
            self._counts[info.key] = {kind: 0 for kind in StreamKind}

    def _next_key(self, label: str) -> str:
        with self._lock:
            return unique_key(label, set(self._info))

    # ------------------------------------------------------------------ device creation
    def add_simulated(self, label: str | None = None, *, mac: str | None = None) -> DeviceInfo:
        if len(self.keys) >= MAX_DEVICES:
            raise RuntimeError(f"at most {MAX_DEVICES} armbands can stream at once")
        label = label or f"Arm {len(self.keys) + 1}"
        key = self._next_key(label)
        info = DeviceInfo(
            key=key, label=label, mac=mac or f"SIM-{key.upper()}", name="Simulated Myo"
        )
        source = SimulatedSource(
            info,
            self._emit,
            self._on_status,
            seed=sum(ord(char) for char in key),
            battery=self._on_battery,
        )
        self._register(info)
        self._sources[key] = source
        return info

    def add_ble(self, mac: str, label: str) -> DeviceInfo:
        if len(self.keys) >= MAX_DEVICES:
            raise RuntimeError(f"at most {MAX_DEVICES} armbands can stream at once")
        key = self._next_key(label)
        info = DeviceInfo(key=key, label=label, mac=mac, name="Myo")
        source = DlMyoSource(info, self._emit, self._on_status, self._on_battery)
        self._register(info)
        self._sources[key] = source
        return info

    # ------------------------------------------------------------------ lifecycle
    def start(self, key: str | None = None) -> None:
        keys = [key] if key else self.keys
        for device_key in keys:
            source = self._sources.get(device_key)
            if source is None:
                continue
            try:
                source.start()
            except Exception as exc:
                log.exception("could not start %s", device_key)
                self._on_status(device_key, "error", str(exc))

    def start_all(self) -> None:
        self.start()

    def stop(self, key: str | None = None) -> None:
        keys = [key] if key else self.keys
        for device_key in keys:
            source = self._sources.get(device_key)
            if source is None:
                continue
            try:
                source.stop()
            except Exception:
                log.exception("could not stop %s", device_key)

    def stop_all(self) -> None:
        self.stop()

    def remove(self, key: str) -> None:
        self.stop(key)
        with self._lock:
            self._sources.pop(key, None)
            self._info.pop(key, None)
            self._state.pop(key, None)
            self._detail.pop(key, None)
            self._battery.pop(key, None)
            self._counts.pop(key, None)
