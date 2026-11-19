"""CLI tests for ``myopyc record`` duration flags and exit codes."""

from __future__ import annotations

import json
from pathlib import Path

from myopyc.cli import main


import pytest


@pytest.mark.parametrize(
    ("flag", "value"), [("--max-seconds", "0"), ("--seconds", "-1")]
)
def test_record_rejects_non_positive_seconds(flag: str, value: str, tmp_path: Path, capsys) -> None:
    code = main(["record", flag, value, "--simulate", "1", "--output-dir", str(tmp_path)])
    assert code == 2
    err = capsys.readouterr().err
    assert "--max-seconds must be a positive number of seconds" in err


def test_record_accepts_max_seconds_flag(tmp_path: Path, capsys, monkeypatch) -> None:
    # Avoid long sleeps: make the timer expire immediately.
    ticks = iter([0.0, 1.0])
    monkeypatch.setattr("myopyc.cli.time.monotonic", lambda: next(ticks, 1.0))
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
    # Final line is the session summary JSON.
    summary = json.loads(out[out.rfind("{") :])
    assert summary["status"] == "stopped"
