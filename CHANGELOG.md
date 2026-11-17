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
- Reader API (`SessionReader`, `load_session`) for NumPy/SciPy processing.
- Headless `record`, `inspect` and `sessions` CLI commands.
- Linux setup and usage documentation.
