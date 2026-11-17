"""Test fixtures."""

from __future__ import annotations

import numpy as np
import pytest

from myopyc.devices.model import SampleBatch
from myopyc.streams.schema import EMG_DTYPE, IMU_DTYPE, StreamKind


def make_emg(
    device: str, count: int = 4, start_seq: int = 0, t0: int = 1_000_000_000
) -> SampleBatch:
    rows = np.zeros(count, dtype=EMG_DTYPE)
    rows["t_ns"] = t0 + np.arange(count) * 5_000_000
    rows["seq"] = start_seq + np.arange(count)
    for channel in range(1, 9):
        rows[f"ch{channel}"] = np.arange(count) + channel
    return SampleBatch(device, StreamKind.EMG, rows)


def make_imu(device: str, count: int = 2, t0: int = 1_000_000_000) -> SampleBatch:
    rows = np.zeros(count, dtype=IMU_DTYPE)
    rows["t_ns"] = t0 + np.arange(count) * 20_000_000
    rows["seq"] = np.arange(count)
    rows["qw"] = 1.0
    rows["az"] = 1.0
    return SampleBatch(device, StreamKind.IMU, rows)


@pytest.fixture
def emg_batch() -> SampleBatch:
    return make_emg("left")


@pytest.fixture
def imu_batch() -> SampleBatch:
    return make_imu("left")
