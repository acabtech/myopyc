"""Command line entry point.

Subcommands::

    myopyc                       # GUI with no devices (add via --simulate/--ble)
    myopyc gui --simulate 2
    myopyc gui --ble D2:3B:85:94:32:8E=Left
    myopyc devices               # scan, pick armband(s) from a list, stream
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
        default=0,
        metavar="N",
        help="number of simulated armbands (0-2, default: 0)",
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


def _cmd_devices(args: argparse.Namespace) -> int:
    """Scan, show a numbered list, connect the pick(s) and persist bindings."""
    from myopyc.devices.manager import DeviceManager

    config = load_config()
    picked = _interactive_device_picker(config)
    if not picked:
        return 2
    manager = DeviceManager()
    for mac, label in picked:
        manager.add_ble(mac, label)
        config.add_or_update_binding(mac, label, slot=len(config.devices))
        print(f"connected {label} ({mac})", flush=True)
    try:
        from myopyc.config import save_config

        save_config(config)
        print("bindings saved", flush=True)
    except OSError:
        logging.exception("could not persist device bindings")
    # Keep streaming until Ctrl-C so the user can verify the armbands work.
    print("streaming until Ctrl-C…", flush=True)
    try:
        manager.start_all()
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print()
    finally:
        manager.stop_all()
    return 0


def _interactive_device_picker(config) -> list[tuple[str, str]] | None:
    """Scan for armbands and prompt for a numbered-list pick on the terminal."""
    import asyncio

    from myopyc.devices.discovery import discover_myos, release_bluez_connection

    print("scanning for Myo armbands…", flush=True)
    from myopyc.devices.discovery import merge_saved_bindings

    found = merge_saved_bindings(
        asyncio.run(discover_myos()),
        {b.mac: b.label for b in config.devices},
    )
    if not found:
        print(
            "no Myo armbands found. Wake the armband (move it around) "
            "and try again, or pass --ble MAC:LABEL.",
            file=sys.stderr,
        )
        return None
    print("available Myo armbands:", flush=True)
    for index, entry in enumerate(found, start=1):
        binding = config.binding_for_mac(entry.mac)
        bound = f", saved as {binding.label}" if binding else ""
        if entry.connected:
            state = "OS-connected"
        elif entry.remembered:
            state = "remembered (wake it to connect)"
        else:
            state = "advertising"
        print(f"  [{index}] {entry.name}  {entry.mac}  ({state}{bound})", flush=True)
    while True:
        try:
            raw = input("pick armband(s), e.g. '1' or '1 2' (empty = cancel): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if not raw:
            return None
        try:
            picks = [found[int(token) - 1] for token in raw.split()]
        except (ValueError, IndexError):
            print("invalid selection; enter numbers between 1 and", len(found), flush=True)
            continue
        devices: list[tuple[str, str]] = []
        for entry in picks:
            if entry.connected:
                asyncio.run(release_bluez_connection(entry.mac))
                time.sleep(2.0)
            binding = config.binding_for_mac(entry.mac)
            label = binding.label if binding else ("Left", "Right")[len(devices)]
            devices.append((entry.mac, label))
        return devices


def _discover_ble_devices(config) -> list[tuple[str, str]]:
    """Probe for Myo armbands; release OS-held connections so we can connect.

    Used when neither ``--simulate`` nor ``--ble`` is given so a plain
    ``myopyc record`` streams from whatever armband is reachable. Saved
    bindings supply labels; unbound armbands get Left/Right.
    """
    import asyncio

    from myopyc.devices.discovery import discover_myos, release_bluez_connection

    found = asyncio.run(discover_myos())
    devices: list[tuple[str, str]] = []
    for index, entry in enumerate(found):
        if entry.connected:
            asyncio.run(release_bluez_connection(entry.mac))
            time.sleep(2.0)
        binding = config.binding_for_mac(entry.mac)
        label = binding.label if binding else ("Left", "Right")[index % 2]
        devices.append((entry.mac, label))
    return devices


def _cmd_record(args: argparse.Namespace) -> int:
    from myopyc.devices.manager import DeviceManager
    from myopyc.recorder.reader import SessionReader
    from myopyc.recorder.writer import SessionRecorder

    config = load_config()
    output_dir = args.output_dir or config.recorder.output_dir
    flush_interval = args.flush_interval or config.recorder.flush_interval_s

    manager = DeviceManager()
    ble = _parse_ble(args.ble)
    if not args.simulate and not ble and sys.stdin.isatty():
        # Plain interactive run: scan and let the user pick from a list.
        ble = _interactive_device_picker(config)
        if ble is None:
            print("no device selected; use --simulate or --ble to skip the picker", file=sys.stderr)
            return 2
    elif not args.simulate and not ble:
        # Non-interactive: auto-connect whatever is reachable.
        ble = _discover_ble_devices(config)
    for _ in range(min(args.simulate, 2)):
        manager.add_simulated()
    for mac, label in ble:
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
    if args.max_seconds <= 0:
        print("--max-seconds must be a positive number of seconds", file=sys.stderr)
        return 2

    manager.subscribe(recorder.add)
    recorder.start()
    print(f"recording {args.max_seconds:g}s to {recorder.session_dir}", flush=True)
    manager.start_all()

    deadline = time.monotonic() + args.max_seconds
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

    if args.json:
        # Machine-readable mode: emit exactly one JSON object and suppress all
        # human/text prints so the output is safe to pipe into jq or a parser.
        repair_dict = reader.repair().as_dict() if args.repair else None
        payload = reader.summary()
        if repair_dict is not None:
            payload["repair"] = repair_dict
        if args.arrays:
            arrays: dict[str, dict[str, object]] = {}
            data = reader.load()
            for key, device in data.devices.items():
                for kind, stream in device.streams.items():
                    arrays[f"{key}/{kind.value}"] = {
                        "shape": list(stream.samples.shape),
                        "dtype": str(stream.samples.dtype),
                    }
            payload["arrays"] = arrays
        print(json.dumps(payload, indent=2))
        return 0

    if args.repair:
        report = reader.repair()
        print(json.dumps({"repair": report.as_dict()}, indent=2))
    print(json.dumps(reader.summary(), indent=2))
    if args.arrays:
        data = reader.load()
        for key, device in data.devices.items():
            for kind, stream in device.streams.items():
                print(
                    f"{key}/{kind.value}: shape={stream.samples.shape} dtype={stream.samples.dtype}"
                )
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    from myopyc.recorder.export import export_session

    out = args.output or args.session.with_name(args.session.name + "-export")
    try:
        written = export_session(args.session, out, args.format)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    for path in written:
        print(path)
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
    record.add_argument(
        "--max-seconds",
        "--seconds",
        dest="max_seconds",
        type=float,
        default=10.0,
        help="maximum recording duration in seconds (default: 10)",
    )
    record.add_argument("--session-name", default=None, help="suffix for the session folder")
    record.set_defaults(func=_cmd_record)

    inspect = sub.add_parser("inspect", help="summarise a recorded session")
    inspect.add_argument("session", type=Path)
    inspect.add_argument("--arrays", action="store_true", help="also show array shapes/dtypes")
    inspect.add_argument(
        "--json",
        action="store_true",
        help="emit a single machine-readable JSON object (no human text); "
        "combines with --repair and --arrays into the same object",
    )
    inspect.add_argument(
        "--repair",
        action="store_true",
        help="reconcile chunks against the manifest before inspecting "
        "(recover a crash-interrupted session)",
    )
    inspect.set_defaults(func=_cmd_inspect)

    export = sub.add_parser("export", help="export a recorded session for analysis")
    export.add_argument("session", type=Path, help="path to the session directory")
    export.add_argument(
        "--format",
        default="csv",
        choices=("csv", "npz"),
        help="export format (default: csv)",
    )
    export.add_argument(
        "--output",
        type=Path,
        default=None,
        help="output directory (csv) or file (npz); default: <session>-export",
    )
    export.set_defaults(func=_cmd_export)

    sessions = sub.add_parser("sessions", help="list recorded sessions")
    sessions.add_argument("--output-dir", type=Path, default=None)
    sessions.set_defaults(func=_cmd_sessions)

    devices = sub.add_parser("devices", help="scan for Myo armbands and save bindings for a pick")
    devices.set_defaults(func=_cmd_devices)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "func", None) is None:
        # Default to the GUI; devices come from --simulate/--ble or saved bindings.
        args = parser.parse_args(["gui", *(argv or [])])
    _setup_logging(getattr(args, "debug", False))
    return int(args.func(args))
