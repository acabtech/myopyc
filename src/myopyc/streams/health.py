"""Pure stream-health computation for the status bar.

The GUI polls :class:`~myopyc.devices.manager.DeviceManager.counts` (cumulative
per-stream sample totals) on a fixed cadence. This module turns those monotonic
counters into *observed* sample rates, compares them against the rates declared
in :func:`~myopyc.streams.schema.stream_meta`, and folds in the recorder's
dropped-sample counter so the status bar can report stream health whether or
not a recording is running.

Nothing here imports Qt, so the whole thing is unit-testable headlessly and the
status line can be built and asserted on without a display server.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from myopyc.streams.schema import StreamKind, stream_meta

#: Fraction of the expected rate below which a stream is "degraded" but alive.
DEGRADED_RATIO = 0.9


def expected_rate(kind: StreamKind) -> float:
    """Nominal sample rate (Hz) declared for ``kind`` in the stream schema."""

    return float(stream_meta(kind)["sample_rate_hz"])


@dataclass(frozen=True)
class StreamHealth:
    """Observed behaviour of one stream over the most recent sampling window.

    ``measured`` is ``True`` when :meth:`RateTracker.observe` had a prior
    baseline for this stream and a positive time gap, i.e. when ``observed_hz``
    is a real rate. It is ``False`` on the first sample after start (or
    :meth:`RateTracker.reset`), where the counter is only being primed and no
    rate can be inferred yet.

    ``delta`` is the raw number of samples that arrived in the window — for
    event-driven streams it *is* the reportable figure (events, not Hz).
    """

    kind: StreamKind
    observed_hz: float
    expected_hz: float
    total: int
    measured: bool = True
    delta: int = 0

    @property
    def event_driven(self) -> bool:
        """``True`` for streams whose nominal rate is 0 (discrete events)."""

        return self.expected_hz <= 0

    @property
    def ratio(self) -> float:
        if self.expected_hz <= 0:
            return 0.0
        return self.observed_hz / self.expected_hz

    @property
    def status(self) -> str:
        """One of ``idle`` / ``healthy`` / ``degraded`` / ``stalled``.

        ``idle`` means there is nothing to judge yet — either no samples have
        *ever* arrived or we are still priming this stream's baseline. It is
        distinct from ``stalled``, where samples were arriving in an earlier
        window but nothing arrived now. That distinction keeps a freshly mounted
        (or just-reset) device from being flagged as a dead link.

        Event-driven streams (e.g. gestures) are judged by flow, not rate:
        events arriving in the window read ``healthy``, silence reads ``idle``
        — never ``stalled``/``degraded``, since sparseness is normal.
        """

        if not self.measured or self.total <= 0:
            return "idle"
        if self.event_driven:
            return "healthy" if self.observed_hz > 0 else "idle"
        if self.observed_hz <= 0:
            return "stalled"
        if self.ratio < DEGRADED_RATIO:
            return "degraded"
        return "healthy"


@dataclass(frozen=True)
class DeviceHealth:
    """Aggregate stream health for one device plus its dropped-sample count."""

    key: str
    label: str
    streams: tuple[StreamHealth, ...]
    dropped: int = 0
    battery: int | None = None

    @property
    def status(self) -> str:
        """Worst meaningful status across the device's streams.

        ``healthy`` < ``idle`` < ``degraded`` < ``stalled`` — so a device shows
        as idle only while nothing has started, but a genuine stall anywhere on
        the device takes precedence over healthy streams elsewhere.
        """

        if not self.streams:
            return "idle"
        order = {"healthy": 0, "idle": 1, "degraded": 2, "stalled": 3}
        return max((s.status for s in self.streams), key=lambda name: order[name])


class RateTracker:
    """Derive per-stream observed rates from cumulative counters.

    Call :meth:`observe` once per UI tick with the latest cumulative sample
    counts for a device; the tracker remembers the previous reading and the
    wall-clock gap between calls to compute the rate over that window.
    """

    def __init__(self, *, kinds: tuple[StreamKind, ...] | None = None) -> None:
        self._kinds = kinds or tuple(StreamKind)
        # (device, kind) -> (last_total, last_mono)
        self._prev: dict[tuple[str, StreamKind], tuple[int, float]] = {}

    def reset(self, device: str | None = None) -> None:
        """Forget history for one device, or all devices when ``None``."""

        if device is None:
            self._prev.clear()
            return
        for key in [k for k in self._prev if k[0] == device]:
            del self._prev[key]

    def observe(
        self,
        device: str,
        counts: Mapping[StreamKind, int],
        *,
        now: float,
        dt: float,
    ) -> tuple[StreamHealth, ...]:
        """Update from cumulative ``counts`` and return this window's health."""

        results: list[StreamHealth] = []
        for kind in self._kinds:
            total = int(counts.get(kind, 0))
            key = (device, kind)
            prev = self._prev.get(key)
            measured = prev is not None and dt > 0
            if prev is None or dt <= 0:
                observed = 0.0
            else:
                last_total, _last_mono = prev
                delta = max(0, total - last_total)
                observed = delta / dt
            self._prev[key] = (total, now)
            delta = max(0, total - last_total) if prev is not None else 0
            results.append(
                StreamHealth(
                    kind=kind,
                    observed_hz=observed,
                    expected_hz=expected_rate(kind),
                    total=total,
                    measured=measured,
                    delta=delta,
                )
            )
        return tuple(results)


def _format_stream(health: StreamHealth) -> str:
    if health.event_driven:
        # Discrete events carry no rate expectation; show the window count.
        return f"{health.kind.value} {health.delta} in window"
    return f"{health.kind.value} {health.observed_hz:.0f}/{health.expected_hz:.0f} Hz"


def format_device_line(device: DeviceHealth) -> str:
    """One device's compact status fragment, e.g.::

    left: emg 200/200 Hz, imu 50/50 Hz
    """

    streams = ", ".join(_format_stream(s) for s in device.streams)
    battery = "" if device.battery is None else f", battery {device.battery}%"
    return f"{device.label}: {streams}{battery}"


def format_status_line(devices: Sequence[DeviceHealth], *, dropped: int) -> str:
    """Build the full status-bar line from per-device health snapshots.

    ``dropped`` is the cumulative dropped-sample count (from the recorder when
    active, else the running total the GUI tracks). Returns a friendly hint when
    no armband is streaming yet.
    """

    if not devices:
        return "add a simulated arm or connect a Myo"
    parts = [format_device_line(d) for d in devices]
    line = " | ".join(parts)
    # Surface any stalled stream prominently so a dead link is not missed.
    if any(d.status == "stalled" for d in devices):
        line = f"⚠ {line}"
    return f"{line} | dropped {dropped}"
