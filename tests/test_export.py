"""Tests for session export (CSV and NPZ)."""

from __future__ import annotations

import csv

import numpy as np
import pytest

from myopyc.cli import main
from myopyc.recorder.export import export_session
from myopyc.recorder.reader import load_session
from myopyc.streams.schema import EMG_DTYPE
from tests.conftest import make_emg, make_event, make_imu
from tests.test_recorder import _recorder


@pytest.fixture()
def recorded_session(tmp_path):
    recorder = _recorder(tmp_path)
    recorder.add(make_emg("left", count=4, t0=1_000_000_000))
    recorder.add(make_imu("left", count=2, t0=1_000_000_000))
    recorder.add(make_event("left", count=1, t0=1_100_000_000))
    recorder.stop()
    return recorder.session_dir


def test_csv_export_has_iso_timestamp_and_raw_t_ns(recorded_session, tmp_path) -> None:
    out = tmp_path / "csv-out"
    written = export_session(recorded_session, out, "csv")

    names = {path.name for path in written}
    assert names == {"left_emg.csv", "left_imu.csv", "left_event.csv"}
    for path in written:
        assert path.is_file()

    with (out / "left_emg.csv").open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 4
    # Both human-readable ISO timestamps AND the raw t_ns column survive.
    assert {"t_iso", "t_ns", "seq", "ch1", "ch8"} <= set(rows[0])
    # ISO column parses as an ISO-8601 timestamp string.
    assert isinstance(rows[0]["t_iso"], str) and "T" in rows[0]["t_iso"]
    assert rows[0]["t_ns"] == str(1_000_000_000)


def test_csv_export_skips_empty_streams(recorded_session, tmp_path) -> None:
    """Only streams that actually hold samples produce files."""

    session = load_session(recorded_session)
    session.devices["right"]  # exists but has no samples in this fixture
    out = tmp_path / "csv-out2"
    written = export_session(recorded_session, out, "csv")
    assert all("right" not in path.name for path in written)


def test_npz_export_roundtrips_arrays(recorded_session, tmp_path) -> None:
    out = tmp_path / "export.npz"
    written = export_session(recorded_session, out, "npz")

    assert written == [out]
    with np.load(out) as npz:
        # Both devices appear; the right one is empty but present as a typed array.
        assert set(npz.files) == {
            "left/emg",
            "left/imu",
            "left/event",
            "right/emg",
            "right/imu",
            "right/event",
        }
        assert npz["left/emg"].size == 4
        # t_ns monotonic and dtype preserved through export.
        t = npz["left/emg"]["t_ns"]
        assert (np.diff(t) > 0).all()
        assert npz["left/emg"].dtype == EMG_DTYPE
        assert npz["left/event"]["type"].tolist() == [1]
        assert npz["right/emg"].size == 0


def test_npz_export_adds_suffix(recorded_session, tmp_path) -> None:
    written = export_session(recorded_session, tmp_path / "no-suffix", "npz")
    assert written[0].suffix == ".npz"


def test_export_rejects_unknown_format(recorded_session, tmp_path) -> None:
    with pytest.raises(ValueError, match="unsupported export format"):
        export_session(recorded_session, tmp_path / "out", "parquet")


def test_cli_export_csv(tmp_path, recorded_session, capsys) -> None:
    rc = main(["export", str(recorded_session), "--format", "csv", "--output", str(tmp_path / "o")])
    assert rc == 0
    assert (tmp_path / "o" / "left_emg.csv").is_file()


def test_cli_export_npz(tmp_path, recorded_session, capsys) -> None:
    rc = main(["export", str(recorded_session), "--format", "npz"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out.endswith(".npz")
    assert np.load(out)["left/imu"].size == 2


def test_cli_export_bad_format_exits_2(recorded_session) -> None:
    with pytest.raises(SystemExit):
        main(["export", str(recorded_session), "--format", "xlsx"])
