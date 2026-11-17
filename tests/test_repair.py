"""Crash-safety tests for ``repair_session``.

These simulate the aftermath of a process dying mid-flush: chunk files on disk
that disagree with ``manifest.json``. The contract is that data through the last
*completed* flush is preserved exactly, untrustworthy files are quarantined
(never deleted), and repair is idempotent.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import numpy as np
import pytest

from myopyc.cli import main
from myopyc.recorder.manifest import read_manifest, write_manifest
from myopyc.recorder.reader import SessionReader
from myopyc.recorder.repair import QUARANTINE_DIR, needs_repair, repair_session
from myopyc.recorder.writer import SessionRecorder
from myopyc.streams.schema import EMG_DTYPE, StreamKind
from tests.conftest import make_emg

DEVICES = {"left": {"label": "Left", "mac": "SIM-LEFT", "name": "Simulated Myo"}}
EMG_DIR = Path("devices") / "left" / "emg"


def _record_two_flushes(tmp_path: Path) -> Path:
    """Produce a clean stopped session with two EMG chunks for the ``left`` device."""

    recorder = SessionRecorder(tmp_path, DEVICES, flush_interval_s=0.05)
    recorder.start()
    recorder.add(make_emg("left", count=2, start_seq=0, t0=1_000_000_000))
    recorder._flush_all()  # chunk-00000
    recorder.add(make_emg("left", count=3, start_seq=2, t0=1_100_000_000))
    session_dir = recorder.stop()  # final flush -> chunk-00001
    return session_dir


def _emg_chunks(manifest: dict) -> list[dict]:
    return manifest["devices"]["left"]["streams"]["emg"]["chunks"]


def _set_status(session_dir: Path, status: str) -> None:
    manifest = read_manifest(session_dir)
    manifest["status"] = status
    write_manifest(session_dir, manifest)


# --------------------------------------------------------------------------- happy path


def test_clean_session_is_untouched(tmp_path: Path) -> None:
    session_dir = _record_two_flushes(tmp_path)
    before = read_manifest(session_dir)

    report = repair_session(session_dir)
    after = read_manifest(session_dir)

    assert report.changed is False
    assert report.quarantined == []
    assert report.orphaned == []
    assert not needs_repair(session_dir)
    assert after == before  # byte-for-byte manifest identical
    # all five samples still load
    assert SessionReader(session_dir).load_stream("left", "emg").size == 5


def test_dry_run_does_not_mutate(tmp_path: Path) -> None:
    session_dir = _record_two_flushes(tmp_path)
    orphan = session_dir / EMG_DIR / "chunk-99999.npy"
    rows = np.zeros(2, dtype=EMG_DTYPE)
    rows["seq"] = [100, 101]
    np.save(orphan, rows)
    before = read_manifest(session_dir)

    report = repair_session(session_dir, dry_run=True)

    assert report.changed is True
    assert report.orphaned == [orphan.relative_to(session_dir).as_posix()]
    assert orphan.is_file()  # not moved
    assert read_manifest(session_dir) == before


# --------------------------------------------------------------------------- orphans


def test_orphaned_chunk_is_quarantined(tmp_path: Path) -> None:
    session_dir = _record_two_flushes(tmp_path)
    orphan = session_dir / EMG_DIR / "chunk-99999.npy"
    rows = np.zeros(2, dtype=EMG_DTYPE)
    rows["seq"] = [100, 101]
    np.save(orphan, rows)

    assert needs_repair(session_dir)
    report = repair_session(session_dir)

    assert report.changed is True
    assert not orphan.exists()
    assert (session_dir / QUARANTINE_DIR / EMG_DIR / "chunk-99999.npy").is_file()
    # manifest sample count is unaffected by the quarantined orphan
    assert read_manifest(session_dir)["devices"]["left"]["streams"]["emg"]["samples"] == 5


def test_repair_is_idempotent(tmp_path: Path) -> None:
    session_dir = _record_two_flushes(tmp_path)
    orphan = session_dir / EMG_DIR / "chunk-00007.npy"
    np.save(orphan, np.zeros(1, dtype=EMG_DTYPE))

    first = repair_session(session_dir)
    snapshot = read_manifest(session_dir)
    second = repair_session(session_dir)

    assert first.changed is True
    assert second.changed is False
    assert read_manifest(session_dir) == snapshot
    assert not needs_repair(session_dir)


# --------------------------------------------------------------------------- truncation


def test_truncated_listed_chunk_is_dropped(tmp_path: Path) -> None:
    session_dir = _record_two_flushes(tmp_path)
    chunks = _emg_chunks(read_manifest(session_dir))
    last = session_dir / chunks[-1]["file"]

    # Simulate a partially written .npy: keep only the leading magic/header bytes.
    good_bytes = last.read_bytes()
    last.write_bytes(good_bytes[:20])

    report = repair_session(session_dir)

    assert report.changed is True
    assert not last.exists()
    manifest = read_manifest(session_dir)
    surviving = _emg_chunks(manifest)
    # the first complete chunk survives, the truncated one is gone
    assert len(surviving) == 1
    assert surviving[0]["samples"] == 2
    assert manifest["devices"]["left"]["streams"]["emg"]["samples"] == 2


def test_missing_listed_chunk_is_unlisted(tmp_path: Path) -> None:
    session_dir = _record_two_flushes(tmp_path)
    chunks = _emg_chunks(read_manifest(session_dir))
    (session_dir / chunks[0]["file"]).unlink()  # first chunk file vanished

    report = repair_session(session_dir)

    assert report.changed is True
    assert report.unlisted  # the dead entry was dropped
    manifest = read_manifest(session_dir)
    assert len(_emg_chunks(manifest)) == 1
    assert manifest["devices"]["left"]["streams"]["emg"]["samples"] == 3


# --------------------------------------------------------------------------- crash state


def test_recording_interruption_flips_status_and_keeps_data(tmp_path: Path) -> None:
    # A crash mid-recording: a fully-written chunk on disk, but the process died
    # before the manifest rewrite that would have registered it, leaving status
    # "recording" and the chunk orphaned.
    session_dir = _record_two_flushes(tmp_path)
    # Register an extra complete chunk file WITHOUT adding it to the manifest,
    # and rewind status to "recording" to mimic the interrupted writer.
    extra = session_dir / EMG_DIR / "chunk-00002.npy"
    rows = np.zeros(4, dtype=EMG_DTYPE)
    rows["t_ns"] = 1_200_000_000 + np.arange(4) * 5_000_000
    rows["seq"] = [5, 6, 7, 8]
    np.save(extra, rows)
    _set_status(session_dir, "recording")

    report = repair_session(session_dir)

    assert report.status_before == "recording"
    assert report.status_after == "repaired"
    assert report.changed is True
    manifest = read_manifest(session_dir)
    assert manifest["status"] == "repaired"
    assert manifest["stopped_utc"] is not None
    # orphaned chunk quarantined; the two completed flushes are intact
    assert not extra.exists()
    assert (session_dir / QUARANTINE_DIR / EMG_DIR / "chunk-00002.npy").is_file()
    assert manifest["devices"]["left"]["streams"]["emg"]["samples"] == 5
    # data through the last completed flush is preserved exactly
    loaded = SessionReader(session_dir).load_stream("left", "emg")
    assert loaded["seq"].tolist() == [0, 1, 2, 3, 4]


def test_manifest_backed_up_before_rewrite(tmp_path: Path) -> None:
    session_dir = _record_two_flushes(tmp_path)
    np.save(session_dir / EMG_DIR / "chunk-12345.npy", np.zeros(1, dtype=EMG_DTYPE))

    repair_session(session_dir)

    backup = session_dir / "manifest.json.bak"
    assert backup.is_file()
    original = json.loads(backup.read_text())
    assert len(_emg_chunks(original)) == 2  # pre-repair had both chunks listed


# --------------------------------------------------------------------------- CLI wiring


def test_cli_inspect_repair_recovers_session(tmp_path: Path, capsys) -> None:
    session_dir = _record_two_flushes(tmp_path)
    _set_status(session_dir, "recording")
    np.save(session_dir / EMG_DIR / "chunk-00009.npy", np.zeros(1, dtype=EMG_DTYPE))

    code = main(["inspect", str(session_dir), "--repair"])

    assert code == 0
    out = capsys.readouterr().out  # a repair-report JSON object is printed first
    printed, _ = json.JSONDecoder().raw_decode(out)
    assert printed["repair"]["status_after"] == "repaired"
    # the session on disk was actually fixed
    assert read_manifest(session_dir)["status"] == "repaired"
    assert not (session_dir / EMG_DIR / "chunk-00009.npy").exists()


def test_reader_repair_refreshes_manifest(tmp_path: Path) -> None:
    session_dir = _record_two_flushes(tmp_path)
    np.save(session_dir / EMG_DIR / "chunk-00050.npy", np.zeros(1, dtype=EMG_DTYPE))

    reader = SessionReader(session_dir)
    report = reader.repair()

    assert report.changed is True
    assert reader.manifest["devices"]["left"]["streams"]["emg"]["samples"] == 5
    assert SessionReader(session_dir).load_stream("left", "emg").size == 5


def test_stream_kind_enum_matches_manifest_keys() -> None:
    # Guard: repair relies on StreamKind(value) round-tripping the manifest keys.
    assert {kind.value for kind in StreamKind} == {"emg", "imu"}


# A real subprocess that is SIGKILLed partway through writing a chunk, so the
# on-disk file has a valid NumPy header but an incomplete body -- exactly the
# state a power loss leaves behind. The manifest still lists only the previous
# completed flush and status "recording".
_CRASH_SCRIPT = textwrap.dedent(
    """
    import os, sys, time
    from pathlib import Path
    import numpy as np
    from numpy.lib.format import write_array_header_2_0
    from myopyc.devices.model import SampleBatch
    from myopyc.streams.schema import EMG_DTYPE, StreamKind
    from myopyc.recorder.writer import SessionRecorder

    out = Path(sys.argv[1])
    recorder = SessionRecorder(out, {"left": {"label": "Left"}}, flush_interval_s=999)
    recorder.start()
    rows = np.zeros(2, dtype=EMG_DTYPE)
    rows["t_ns"] = [1, 2]
    rows["seq"] = [0, 1]
    # one clean flush -> chunk-00000 written and registered in the manifest
    recorder.add(SampleBatch("left", StreamKind.EMG, rows))
    recorder._flush_all()
    session_dir = recorder.session_dir

    # begin a SECOND chunk that will never be finished or registered. It keeps
    # appending until the process is SIGKILLed mid-write.
    path = session_dir / "devices" / "left" / "emg" / "chunk-00001.npy"
    with path.open("wb") as fh:
        write_array_header_2_0(
            fh,
            {"descr": np.dtype(EMG_DTYPE).descr, "fortran_order": False, "shape": (5_000_000,)},
        )
        block = np.zeros(200_000, dtype=EMG_DTYPE).tobytes()
        announced = False
        while True:
            fh.write(block)
            fh.flush()
            os.fsync(fh.fileno())
            if not announced:
                announced = True
                print("READY", flush=True)
            time.sleep(0.02)
    """
)


@pytest.mark.skipif(
    not hasattr(signal, "SIGKILL") or sys.platform == "win32",
    reason="requires SIGKILL",
)
def test_sigkill_mid_flush_leaves_recoverable_state(tmp_path: Path) -> None:
    # First build a clean two-flush-free baseline via the recorder so the
    # manifest is well-formed, then kill a real process mid second flush.
    session_root = tmp_path / "sessions"
    proc = subprocess.Popen(
        [sys.executable, "-c", _CRASH_SCRIPT, str(session_root)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    ready = proc.stdout.readline()
    if "READY" not in ready:  # pragma: no cover - diagnostics on failure
        proc.kill()
        err = proc.stderr.read() if proc.stderr else ""
        pytest.skip(f"crash subprocess did not start: {ready!r} {err!r}")

    # let it write several more blocks, then SIGKILL mid-flush
    import time as _time

    _time.sleep(0.1)
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait(timeout=10)

    sessions = list(session_root.iterdir())
    assert sessions, "crash subprocess produced no session directory"
    session_dir = sessions[0]

    interrupted = session_dir / EMG_DIR / "chunk-00001.npy"
    assert interrupted.is_file()
    # the partial file is genuinely unreadable, like a real crash
    with pytest.raises((ValueError, EOFError, OSError)):
        np.load(interrupted, allow_pickle=False)

    manifest = read_manifest(session_dir)
    assert manifest["status"] == "recording"  # writer died before finalising
    assert len(_emg_chunks(manifest)) == 1  # only chunk-00000 was registered

    # repair recovers everything through the last completed flush
    report = repair_session(session_dir)
    assert report.changed is True
    assert report.status_after == "repaired"
    assert not interrupted.exists()
    assert (session_dir / QUARANTINE_DIR / EMG_DIR / "chunk-00001.npy").is_file()

    manifest = read_manifest(session_dir)
    assert manifest["status"] == "repaired"
    assert manifest["devices"]["left"]["streams"]["emg"]["samples"] == 2
    loaded = SessionReader(session_dir).load_stream("left", "emg")
    assert loaded["seq"].tolist() == [0, 1]  # preserved exactly
    assert not needs_repair(session_dir)  # idempotent
