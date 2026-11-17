#!/usr/bin/env python3
"""Print a summary (and optionally array shapes) for a recorded session."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from myopyc.recorder import SessionReader


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path)
    parser.add_argument("--arrays", action="store_true", help="load streams and print shapes")
    args = parser.parse_args()

    reader = SessionReader(args.session)
    print(json.dumps(reader.summary(), indent=2))

    if args.arrays:
        session = reader.load()
        for key, device in session.devices.items():
            for kind, stream in device.streams.items():
                print(f"{key}/{kind.value}: {stream.samples.shape} {stream.samples.dtype}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
