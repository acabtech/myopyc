"""Shared device/stream data model."""

from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from myopyc.streams.schema import StreamKind


@dataclass(frozen=True)
class DeviceInfo:
    """Static description of one armband."""

    key: str
    label: str
    mac: str | None = None
    name: str = "Myo"
    firmware: str | None = None


@dataclass(frozen=True)
class SampleBatch:
    """A block of samples for one device and one stream.

    ``data`` is a NumPy structured array matching the dtype in
    :mod:`myopyc.streams.schema`.
    """

    device: str
    kind: StreamKind
    data: NDArray[np.void]


def slugify(value: str) -> str:
    """Turn a user label into a safe directory component."""

    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip("-._").lower()
    return slug or "myo"


def unique_key(label: str, existing: set[str]) -> str:
    """Return a unique device key derived from ``label``."""

    base = slugify(label)
    if base not in existing:
        return base
    index = 2
    while f"{base}-{index}" in existing:
        index += 1
    return f"{base}-{index}"
