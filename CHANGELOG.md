# myopyc changelog

All notable changes to this project are documented here. The format follows
Keep a Changelog and the project uses semantic versioning.

## [0.1.0] - unreleased

### Added

- GUI with live 8-channel EMG oscilloscope and IMU traces per armband.
- Status bar stream-health readout: per-stream observed vs expected sample rate
  and a cumulative dropped-sample counter, refreshed every second whether or not
  a recording is running. A dead link is flagged with a warning marker.
- Up to two concurrent Myo sources via `dl-myo` (BLE, dongleless).
- Simulated armband source for hardware-free development and demos.
- MAC-to-label device bindings persisted in `~/.config/myopyc/config.toml`.
- RAM-buffered recorder with time-based `.npy` chunk flushes and JSON manifest.
- Explicit drop/gap accounting when the RAM budget is exceeded.
- Crash-safety session repair: `repair_session()` reconciles on-disk chunks with
  the manifest after an interrupted write — quarantining orphaned or truncated
  `.npy` files into `unrepaired/` (never deleting them), dropping manifest
  entries whose files are missing or unreadable, patching sample counts, and
  moving a stuck `recording` session to a `repaired` status. Idempotent; a
  `manifest.json.bak` is kept before the first rewrite. Exposed via
  `SessionReader.repair()` and `myopyc inspect --repair`.
- Reader API (`SessionReader`, `load_session`) for NumPy/SciPy processing.
- Headless `record`, `inspect` and `sessions` CLI commands.
- `myopyc record --max-seconds` (alias: `--seconds`) sets the maximum recording
  duration with a positive-value guard; CLI exit codes are documented in usage.
- `inspect --json` emits a single machine-readable JSON object (safe to pipe into
  `jq`); `--repair` and `--arrays` fold their data into the same object rather
  than printing extra lines.
- Linux setup and usage documentation.
- Version single-source check test asserting `__version__`, `pyproject.toml`, and the
  latest `CHANGELOG.md` heading stay identical.

### Changed

- Data-format append-only contract encoded explicitly: readers tolerate unknown
  optional manifest fields and unknown stream kinds (keeping `FORMAT_VERSION` at 1
  for additive evolution), with forward-compatibility tests and usage docs.
