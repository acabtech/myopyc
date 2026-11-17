"""Synthetic Myo source used for demos, UI development and tests."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable

import numpy as np

from myopyc.devices.model import DeviceInfo, SampleBatch
from myopyc.streams.schema import EMG_CHANNELS, EMG_DTYPE, IMU_DTYPE, StreamKind

EmitFn = Callable[[SampleBatch], None]
StatusFn = Callable[[str, str, str | None], None]


class SimulatedSource:
    """Generate plausible raw EMG and IMU data at the real sample rates."""

    emg_rate_hz = 200
    imu_rate_hz = 50
    block_seconds = 0.02

    def __init__(
        self,
        info: DeviceInfo,
        emit: EmitFn,
        status: StatusFn | None = None,
        *,
        seed: int = 0,
    ) -> None:
        self.info = info
        self._emit = emit
        self._status = status or (lambda *_: None)
        self._rng = np.random.default_rng(seed)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._emg_seq = 0
        self._imu_seq = 0
        self._started_at = 0.0
        self._phase = float(seed % 7) * 0.7

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._started_at = time.monotonic()
        self._thread = threading.Thread(target=self._run, name=f"sim-{self.info.key}", daemon=True)
        self._thread.start()
        self._status(self.info.key, "streaming", "simulated")

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        self._status(self.info.key, "stopped", None)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ------------------------------------------------------------------ generation
    def _run(self) -> None:
        emg_per_block = int(self.emg_rate_hz * self.block_seconds)
        imu_per_block = max(1, int(self.imu_rate_hz * self.block_seconds))
        next_tick = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            elapsed = now - self._started_at
            self._emit(self._make_emg(elapsed, emg_per_block))
            for _ in range(imu_per_block):
                self._emit(self._make_imu(elapsed))
            next_tick += self.block_seconds
            delay = next_tick - time.monotonic()
            if delay > 0:
                self._stop.wait(delay)
            else:
                next_tick = time.monotonic()

    def _make_emg(self, elapsed: float, count: int) -> SampleBatch:
        t0 = time.monotonic_ns()
        # A slow contraction envelope plus per-channel oscillation and noise.
        envelope = 0.35 + 0.65 * (0.5 + 0.5 * math.sin(2 * math.pi * 0.5 * elapsed + self._phase))
        burst = 1.0 if (elapsed % 8.0) < 1.5 else 0.35
        samples = np.empty((count, EMG_CHANNELS), dtype=np.float32)
        for channel in range(EMG_CHANNELS):
            freq = 55 + channel * 7
            wave = np.sin(2 * math.pi * freq * (elapsed + np.arange(count) / self.emg_rate_hz))
            noise = self._rng.normal(0, 12, count)
            samples[:, channel] = (wave * 40 * envelope * burst) + noise
        samples = np.clip(samples, -128, 127).astype(np.int8)

        rows = np.empty(count, dtype=EMG_DTYPE)
        rows["t_ns"] = t0 + (np.arange(count) * 1_000_000_000 // self.emg_rate_hz)
        rows["seq"] = np.arange(self._emg_seq, self._emg_seq + count)
        for channel in range(EMG_CHANNELS):
            rows[f"ch{channel + 1}"] = samples[:, channel]
        self._emg_seq += count
        return SampleBatch(self.info.key, StreamKind.EMG, rows)

    def _make_imu(self, elapsed: float) -> SampleBatch:
        row = np.empty(1, dtype=IMU_DTYPE)
        roll = 0.35 * math.sin(elapsed * 1.1 + self._phase)
        pitch = 0.25 * math.sin(elapsed * 0.7)
        yaw = 0.15 * math.sin(elapsed * 0.3)
        cy, sy = math.cos(yaw / 2), math.sin(yaw / 2)
        cp, sp = math.cos(pitch / 2), math.sin(pitch / 2)
        cr, sr = math.cos(roll / 2), math.sin(roll / 2)
        qw = cr * cp * cy + sr * sp * sy
        qx = sr * cp * cy - cr * sp * sy
        qy = cr * sp * cy + sr * cp * sy
        qz = cr * cp * sy - sr * sp * cy

        row["t_ns"] = time.monotonic_ns()
        row["seq"] = self._imu_seq
        row["qw"], row["qx"], row["qy"], row["qz"] = qw, qx, qy, qz
        row["ax"] = math.sin(elapsed * 2.1) * 0.15
        row["ay"] = math.cos(elapsed * 1.7) * 0.15
        row["az"] = 1.0 + math.sin(elapsed * 3.0) * 0.05
        row["gx"] = math.cos(elapsed * 1.1) * 12.0
        row["gy"] = math.sin(elapsed * 0.9) * 10.0
        row["gz"] = math.sin(elapsed * 0.4) * 4.0
        self._imu_seq += 1
        return SampleBatch(self.info.key, StreamKind.IMU, row)
