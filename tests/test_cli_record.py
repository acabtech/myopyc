"""CLI tests for ``myopyc record`` duration flags and exit codes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from myopyc.cli import main


@pytest.mark.parametrize(("flag", "value"), [("--max-seconds", "0"), ("--seconds", "-1")])
def test_record_rejects_non_positive_seconds(flag: str, value: str, tmp_path: Path, capsys) -> None:
    code = main(["record", flag, value, "--simulate", "1", "--output-dir", str(tmp_path)])
    assert code == 2
    err = capsys.readouterr().err
    assert "--max-seconds must be a positive number of seconds" in err


def test_record_accepts_max_seconds_flag(tmp_path: Path, capsys, monkeypatch) -> None:
    # Avoid long sleeps: a strictly increasing fake clock means the recording
    # deadline can never survive a second loop check, regardless of how many
    # calls the simulator/manager consume before the deadline is computed.
    calls = {"n": 0}

    def fake_monotonic() -> float:
        value = float(calls["n"])
        calls["n"] += 1
        return value

    monkeypatch.setattr("myopyc.cli.time.monotonic", fake_monotonic)
    monkeypatch.setattr("myopyc.cli.time.sleep", lambda _s: None)

    code = main(
        [
            "record",
            "--max-seconds",
            "0.5",
            "--simulate",
            "1",
            "--output-dir",
            str(tmp_path),
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "recording 0.5s to" in out
    # The summary JSON is pretty-printed after the human lines: raw-decode the
    # first object that starts after the "recording ..." line.
    summary_start = out.index("{")
    summary, _ = json.JSONDecoder().raw_decode(out[summary_start:])
    assert summary["status"] == "stopped"
