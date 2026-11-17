from __future__ import annotations

from myopyc.streams.live import LiveBuffer
from myopyc.streams.schema import StreamKind
from tests.conftest import make_emg


def test_live_buffer_keeps_recent_samples() -> None:
    live = LiveBuffer(max_samples=5)
    live.append(make_emg("left", count=4))
    live.append(make_emg("left", count=4, start_seq=4, t0=2_000_000_000))

    snapshot = live.snapshot("left", StreamKind.EMG)
    assert snapshot.size == 5
    assert snapshot["seq"].tolist() == [3, 4, 5, 6, 7]


def test_live_buffer_is_per_device_and_typed() -> None:
    live = LiveBuffer()
    live.append(make_emg("left"))
    empty = live.snapshot("right", StreamKind.EMG)
    assert empty.size == 0
    assert empty.dtype.names == (
        "t_ns",
        "seq",
        "ch1",
        "ch2",
        "ch3",
        "ch4",
        "ch5",
        "ch6",
        "ch7",
        "ch8",
    )
    assert live.devices() == {"left"}

    live.clear("left")
    assert live.snapshot("left", StreamKind.EMG).size == 0
