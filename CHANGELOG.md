# myopyc changelog

All notable changes to this project are documented here. The format follows
Keep a Changelog and the project uses semantic versioning.

## [0.1.0] - unreleased

### Added

- GUI with live 8-channel EMG oscilloscope and IMU traces per armband.
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
- `inspect --json` emits a single machine-readable JSON object (safe to pipe into
  `jq`); `--repair` and `--arrays` fold their data into the same object rather
  than printing extra lines.
- Linux setup and usage documentation.
