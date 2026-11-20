"""Export a recorded session to CSV or NPZ for downstream analysis.

CSV: one file per device/stream, with a human-readable ISO UTC timestamp
column alongside the raw ``t_ns`` nanoseconds. NPZ: one compressed archive
with one array per device/stream under ``<device>/<stream>`` keys.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from myopyc.recorder.reader import SessionData, SessionReader

_CSV_SUFFIX = ".csv"


def _iso_utc(t_ns: int) -> str:
    """Render nanoseconds on the host monotonic clock as a UTC ISO string.

    The monotonic clock has no fixed epoch, so this is *relative* time:
    nanoseconds counted from an arbitrary boot point, rendered for
    readability (sorting/grepping), not wall-clock truth.
    """

    return np.datetime64(t_ns, "ns").astype("datetime64[us]").__str__()


def _stream_rows(samples: np.ndarray) -> dict[str, np.ndarray]:
    """Split a structured sample array into columns, with ISO time prepended."""

    cols: dict[str, np.ndarray] = {
        "t_iso": np.array([_iso_utc(int(v)) for v in samples["t_ns"]], dtype=object)
    }
    for name in samples.dtype.names or ():
        cols[name] = samples[name]
    return cols


def export_session_csv(session: SessionData, out_dir: Path) -> list[Path]:
    """Write one CSV per device/stream into ``out_dir``; return the paths."""

    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for key, device in session.devices.items():
        for kind, stream in device.streams.items():
            if stream.samples.size == 0:
                continue
            path = out_dir / f"{key}_{kind.value}{_CSV_SUFFIX}"
            cols = _stream_rows(stream.samples)
            header = ",".join(cols)
            rows = np.stack(
                [np.asarray(v, dtype=object) if v.dtype == object else v for v in cols.values()],
                axis=1,
            )
            with path.open("w", newline="", encoding="utf-8") as fh:
                fh.write(f"{header}\n")
                for row in rows:
                    fh.write(",".join(str(v) for v in row) + "\n")
            written.append(path)
    return written


def export_session_npz(session: SessionData, out_path: Path) -> Path:
    """Write one compressed NPZ with ``<device>/<stream>`` array keys."""

    out_path.parent.mkdir(parents=True, exist_ok=True)
    arrays: dict[str, np.ndarray] = {}
    for key, device in session.devices.items():
        for kind, stream in device.streams.items():
            arrays[f"{key}/{kind.value}"] = stream.samples
    # numpy's savez_compressed stub types **arrays as bool; silence it since the
    # runtime signature is savez_compressed(file, *args, **kwds).
    with out_path.open("wb") as fh:
        np.savez_compressed(fh, **arrays)  # type: ignore[arg-type]
    return out_path


def export_session(session_dir: Path | str, out: Path, fmt: str) -> list[Path]:
    """Export ``session_dir`` in ``fmt`` ('csv' or 'npz') into/under ``out``.

    For CSV, ``out`` is a directory. For NPZ, ``out`` is the archive file
    (a ``.npz`` suffix is added if missing).
    """

    fmt = fmt.lower()
    if fmt not in ("csv", "npz"):
        raise ValueError(f"unsupported export format: {fmt!r} (use 'csv' or 'npz')")
    reader = SessionReader(session_dir)
    data: SessionData = reader.load()
    if fmt == "csv":
        return export_session_csv(data, out)
    if out.suffix != ".npz":
        out = out.with_suffix(".npz")
    return [export_session_npz(data, out)]
