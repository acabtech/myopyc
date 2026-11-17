"""Command line entry point.

Subcommands::

    myopyc                       # GUI with one simulated armband
    myopyc gui --simulate 2
    myopyc gui --ble D2:3B:85:94:32:8E=Left
    myopyc record --seconds 10 --simulate 2
    myopyc inspect <session-dir>
    myopyc sessions
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from myopyc import __version__
from myopyc.config import load_config


def _parse_ble(values: list[str] | None) -> list[tuple[str, str]]:
    devices: list[tuple[str, str]] = []
    for value in values or []:
        if "=" not in value:
            raise argparse.ArgumentTypeError(
                f"--ble expects MAC=label (got {value!r}); MAC addresses contain ':' so use '='"
            )
        mac, label = value.split("=", 1)
        if not mac.strip() or not label.strip():
            raise argparse.ArgumentTypeError(f"invalid --ble value: {value!r}")
        devices.append((mac.strip(), label.strip()))
    return devices


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--simulate",
        type=int,
        default=1,
        metavar="N",
        help="number of simulated armbands (0-2, default: 1)",
    )
    parser.add_argument(
        "--ble",
        action="append",
        metavar="MAC=LABEL",
        help="connect a real Myo (repeatable, max two)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="session output directory (default: XDG data dir)",
    )
    parser.add_argument(
        "--flush-interval",
        type=float,
        default=None,
        help="seconds between RAM-to-disk chunk flushes",
    )
    parser.add_argument("--debug", action="store_true", help="verbose logging")


def _setup_logging(debug: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


def _cmd_gui(args: argparse.Namespace) -> int:
    from myopyc.app import run_gui

    config = load_config()
    if args.flush_interval is not None:
        config.recorder.flush_interval_s = args.flush_interval
    try:
        return run_gui(
            config,
            simulate=args.simulate,
            ble_devices=_parse_ble(args.ble),
            output_dir=args.output_dir,
        )
    except KeyboardInterrupt:
        return 130


def _cmd_record(args: argparse.Namespace) -> int:
    from myopyc.devices.manager import DeviceManager
    from myopyc.recorder.reader import SessionReader
    from myopyc.recorder.writer import SessionRecorder

    config = load_config()
    output_dir = args.output_dir or config.recorder.output_dir
    flush_interval = args.flush_interval or config.recorder.flush_interval_s

    manager = DeviceManager()
    for _ in range(min(args.simulate, 2)):
        manager.add_simulated()
    for mac, label in _parse_ble(args.ble):
        manager.add_ble(mac, label)
    if not manager.keys:
        print("nothing to record: pass --simulate or --ble", file=sys.stderr)
        return 2

    recorder = SessionRecorder(
        output_dir,
        manager.meta(),
        flush_interval_s=flush_interval,
        max_ram_bytes=config.recorder.max_ram_bytes,
        session_name=args.session_name,
    )
    manager.subscribe(recorder.add)
    recorder.start()
    print(f"recording {args.seconds:g}s to {recorder.session_dir}", flush=True)
    manager.start_all()

    deadline = time.monotonic() + max(0.5, args.seconds)
    try:
        while time.monotonic() < deadline:
            time.sleep(min(1.0, max(0.05, deadline - time.monotonic())))
            stats = recorder.stats()
            print(
                f"  samples={stats['received_samples']} "
                f"buffered={stats['buffered_bytes']}B "
                f"dropped={stats['dropped_samples']} "
                f"chunks={stats['chunks_written']}",
                flush=True,
            )
    finally:
        manager.stop_all()
        session_dir = recorder.stop()

    print(json.dumps(SessionReader(session_dir).summary(), indent=2))
    return 0


def _cmd_inspect(args: argparse.Namespace) -> int:
    from myopyc.recorder.reader import SessionReader

    reader = SessionReader(args.session)
    print(json.dumps(reader.summary(), indent=2))
    if args.arrays:
        data = reader.load()
        for key, device in data.devices.items():
            for kind, stream in device.streams.items():
                print(
                    f"{key}/{kind.value}: shape={stream.samples.shape} dtype={stream.samples.dtype}"
                )
    return 0


def _cmd_sessions(args: argparse.Namespace) -> int:
    from myopyc.recorder.reader import find_sessions

    config = load_config()
    root = args.output_dir or config.recorder.output_dir
    sessions = find_sessions(root)
    if not sessions:
        print(f"no sessions under {root}")
        return 0
    for session in sessions:
        print(session)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="myopyc", description=__doc__)
    parser.add_argument("--version", action="version", version=f"myopyc {__version__}")
    sub = parser.add_subparsers(dest="command")

    gui = sub.add_parser("gui", help="launch the desktop application")
    _add_common(gui)
    gui.set_defaults(func=_cmd_gui)

    record = sub.add_parser("record", help="headless recording run")
    _add_common(record)
    record.add_argument("--seconds", type=float, default=10.0, help="recording duration")
    record.add_argument("--session-name", default=None, help="suffix for the session folder")
    record.set_defaults(func=_cmd_record)

    inspect = sub.add_parser("inspect", help="summarise a recorded session")
    inspect.add_argument("session", type=Path)
    inspect.add_argument("--arrays", action="store_true", help="also show array shapes/dtypes")
    inspect.set_defaults(func=_cmd_inspect)

    sessions = sub.add_parser("sessions", help="list recorded sessions")
    sessions.add_argument("--output-dir", type=Path, default=None)
    sessions.set_defaults(func=_cmd_sessions)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "func", None) is None:
        # Default to the GUI with one simulated armband so first run shows something.
        args = parser.parse_args(["gui", *(argv or [])])
    _setup_logging(getattr(args, "debug", False))
    return int(args.func(args))
