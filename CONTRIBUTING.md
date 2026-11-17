# Contributing

Thanks for helping with `myopyc`.

## Development setup

```bash
uv sync --extra dev --extra hardware
uv run pre-commit install
```

Hardware is optional: all tests and the GUI can run against simulated armbands.

## Checks

```bash
make lint
make test
```

CI runs the same commands on Python 3.12.

## Project layout

```
src/myopyc/
├── devices/    # dl-myo adapter, simulator, device manager
├── streams/    # sample schemas and live ring buffers
├── recorder/   # manifest, chunk writer, session reader
├── ui/         # PySide6 window, plots, theme
├── config.py   # TOML configuration
├── app.py      # Qt bootstrap
└── cli.py      # command line entry point
```

Design notes are intentionally kept out of the repository (local `plans/`
directory). Keep user-facing documentation in `docs/` and `README.md`.

## Data compatibility

The on-disk schema is versioned in `manifest.json`. Any change to dtype columns,
units or the chunk layout needs a `FORMAT_VERSION` bump, a reader migration, and
a note in `CHANGELOG.md`.

## Upstream

`upstream/dl-myo` is a local, gitignored checkout used for reference. Runtime
code goes through the pinned package dependency; do not import from
`upstream/` in `src/`.

## License

Contributions are accepted under GPL-3.0-or-later.
