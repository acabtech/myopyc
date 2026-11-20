# myopyc changelog

All notable changes to this project are documented here. The format follows
Keep a Changelog and the project uses semantic versioning.

## [0.1.0] - unreleased

### Added

- `StreamKind.EVENT` stream foundation (first slice of the v0.2 gesture/pose
  work): a dedicated third stream kind with its own dtype (`t_ns`, `seq`,
  `type`, packed `ox/oy/oz/ow` orientation payload) and a documented pose
  vocabulary (`0` none, `1` fist, `2` wave_left, `3` wave_right,
  `4` double_click; unknown codes render as `unknown-<n>`). The recorder
  creates per-device `event/` chunk directories, the manifest lists the
  event-driven stream (`sample_rate_hz` 0.0), and `SessionReader` loads it like
  EMG/IMU. Purely additive — `FORMAT_VERSION` stays 1 and old readers still
  load sessions that carry an event stream. Round-trip and dtype-layout tests
  cover the writer → manifest → reader path.
- Battery level shown per device: a color-coded `battery N%` label in each
  device panel (red ≤ 15%, amber ≤ 40%) and in the status-bar health line, fed
  from both the BLE adapter's periodic read (~60 s) and the simulated source's
  draining cell. Covered by fake-battery callback tests for the simulator
  drain/floor and the adapter's battery loop (including failed-read recovery).
- GUI with live 8-channel EMG oscilloscope and IMU traces per armband.
- Mypy typing coverage extended to `tests/` (alongside `src/myopyc`), with current
  typing issues fixed and checks wired into `make lint` and CI.
- Coverage floor for `src/myopyc` (initial 70%) agreed and enforced via
  `[tool.coverage.report].fail_under` and CI `--cov-fail-under`; raise as the
  suite grows.
- Status bar stream-health readout: per-stream observed vs expected sample rate
  and a cumulative dropped-sample counter, refreshed every second whether or not
  a recording is running. A dead link is flagged with a warning marker.
- Configurable EMG/IMU plot scroll windows (`ui.emg_window_s` / `ui.imu_window_s`)
  verified end-to-end from config to live device views.
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
- Data-format specification ([docs/data-format.md](docs/data-format.md)): session
  layout, manifest field reference, EMG/IMU chunk dtypes, `t_ns` semantics
  (host-monotonic BLE receive time), gap/overflow encoding, crash-repair
  behaviour, and the append-only v1 evolution contract.
- Linux setup and usage documentation.
- Version single-source check test asserting `__version__`, `pyproject.toml`, and the
  latest `CHANGELOG.md` heading stay identical.

### Changed

- Data-format append-only contract encoded explicitly: readers tolerate unknown
  optional manifest fields and unknown stream kinds (keeping `FORMAT_VERSION` at 1
  for additive evolution), with forward-compatibility tests and usage docs.
