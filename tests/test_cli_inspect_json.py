"""CLI tests for ``myopyc inspect --json`` machine-readable output.

The contract: ``--json`` emits exactly one JSON object and suppresses all human
text, so output can be piped into ``jq`` or ``json.loads`` directly. ``--repair``
and ``--arrays`` fold their data into the same object rather than printing extra
lines.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from myopyc.cli import main
from myopyc.recorder.manifest import read_manifest, write_manifest
from myopyc.recorder.writer import SessionRecorder
from myopyc.streams.schema import EMG_DTYPE
from tests.conftest import make_emg, make_imu

DEVICES = {
    "left": {"label": "Left", "mac": "SIM-LEFT", "name": "Simulated Myo"},
    "right": {"label": "Right", "mac": "SIM-RIGHT", "name": "Simulated Myo"},
}
EMG_DIR = Path("devices") / "left" / "emg"


def _record_session(tmp_path: Path) -> Path:
    """Clean stopped session: left has 5 EMG samples over two chunks, right has IMU."""

    recorder = SessionRecorder(tmp_path, DEVICES, flush_interval_s=0.05)
    recorder.start()
    recorder.add(make_emg("left", count=2, start_seq=0, t0=1_000_000_000))
    recorder._flush_all()  # chunk-00000
    recorder.add(make_emg("left", count=3, start_seq=2, t0=1_100_000_000))
    recorder.add(make_imu("right", count=2))
    return recorder.stop()


def test_inspect_json_emits_single_object(tmp_path: Path, capsys) -> None:
    session_dir = _record_session(tmp_path)

    code = main(["inspect", str(session_dir), "--json"])
    assert code == 0

    out = capsys.readouterr().out
    # A single parse means no human text polluted stdout.
    payload = json.loads(out)
    assert payload["session_id"] == read_manifest(session_dir)["session_id"]
    assert payload["status"] == "stopped"
    assert payload["notes"] == ""
    assert "streams" in payload
    # No keys leak in when the flags were not requested.
    assert "repair" not in payload
    assert "arrays" not in payload


def test_inspect_json_arrays_folded_into_object(tmp_path: Path, capsys) -> None:
    session_dir = _record_session(tmp_path)

    code = main(["inspect", str(session_dir), "--json", "--arrays"])
    assert code == 0

    payload = json.loads(capsys.readouterr().out)
    arrays = payload["arrays"]
    assert arrays["left/emg"]["shape"] == [5]
    assert arrays["left/emg"]["dtype"] == str(EMG_DTYPE)
    # The right device recorded IMU but no EMG, so its emg stream is empty.
    assert arrays["right/imu"]["shape"] == [2]
    assert arrays["right/emg"]["shape"] == [0]


def test_inspect_json_repair_folded_into_object(tmp_path: Path, capsys) -> None:
    session_dir = _record_session(tmp_path)
    # Force a repairable state: stuck status plus an orphaned chunk file.
    manifest = read_manifest(session_dir)
    manifest["status"] = "recording"
    write_manifest(session_dir, manifest)
    np.save(session_dir / EMG_DIR / "chunk-00099.npy", np.zeros(1, dtype=EMG_DTYPE))

    code = main(["inspect", str(session_dir), "--json", "--repair"])
    assert code == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["repair"]["changed"] is True
    assert payload["repair"]["status_after"] == "repaired"
    # The session summary reflects the reconciled (stopped/repaired) manifest.
    assert payload["status"] == "repaired"
    assert not (session_dir / EMG_DIR / "chunk-00099.npy").exists()
