# myopyc changelog

All notable changes to this project are documented here. The format follows
Keep a Changelog and the project uses semantic versioning.

## [0.2.0.dev0]

### Added

- Derived EMG channel overlays (signal-quality milestone, GUI-only slice):
  a checkable "Derived channels" toolbar action draws per-channel rectified
  traces (dashed grey) and a combined RMS envelope (bold light curve,
  250 ms moving window over the channel mean) on top of the raw EMG
  oscilloscope in every `DeviceView` — live and playback alike. The
  computation happens only while the toggle is on, hides with stale
  geometry cleared so re-enabling never flashes old data, and never alters
  recorded raw data (plots consume the live/playback buffers only). The
  toggle is persisted as `ui.derived_channels` in `config.toml`
  (default off) and honored when new armbands mount. Covered by offscreen
  Qt tests (visibility flip on open views, config round trip, envelope math
  against a constant signal, stale-geometry clearing).
- Read-only session playback (session-management milestone): opening a
  session from the Sessions dock (Open button or double-click) loads the
  recorded EMG/IMU chunks through the same per-device plot widgets used for
  live capture, driven from the recorded host-monotonic `t_ns` timeline. A
  playback bar above the device grid offers a scrub slider (the plots show the
  6 s window ending at the playhead), Play/Pause at 2x real time (Play after
  the end replays from the top) and Stop, which re-mounts the live armband
  views — simulated/BLE devices keep streaming throughout. Playback is
  strictly read-only: chunk files are only read, the session directory is
  never written, and the live buffers are untouched (playback uses its own
  private ring buffer). Mutual exclusions are handled with status-bar hints:
  recording cannot start while playback is open (starting one closes playback
  first), playback refuses while a recording runs, and new armbands wait until
  playback is closed. Covered by offscreen Qt tests (controller load/seek/
  transport, bar states, dock→playback routing, recording/playback mutual
  exclusion, byte-identical session directory after playback, post-playback
  manifest round trip); documented in `docs/usage.md`.
- ZIP export with manifest integrity check (session-management milestone):
  `myopyc export <session> --format zip` archives the whole session directory
  (`manifest.json` plus every chunk file) together with a generated
  `INTEGRITY.json` sidecar mapping each archived member to its SHA-256 digest.
  The freshly written archive is verified before the command returns, and
  `verify_session_zip()` (exported from `myopyc.recorder.export`) recomputes
  digests after a transfer, reporting missing, corrupted (digest mismatch) and
  unexpected members separately. Covered by round-trip, tamper-detection,
  missing-member and CLI tests; documented in `docs/usage.md`.
- GUI sessions dock (first slice of the v0.2 session-management milestone):
  a right-side dock lists recorded sessions in the output directory (newest
  first) with sample/chunk counts and status, refreshed automatically when a
  recording is finalised. Sessions can be opened (summary dialog with
  devices, samples, drops, chunks, gaps and marker count), renamed (safe —
  manifest chunk paths are relative) and deleted (with confirmation); the
  session currently being recorded is protected from both rename and delete.
  The toolbar's "Open sessions" action became a checkable "Sessions" dock
  toggle plus an "Open folder" action. Unreadable manifests still list (so
  they can be inspected/deleted) with a placeholder description. Covered by
  offscreen Qt tests (listing, open/summary round trip, broken-manifest
  handling, rename validation/collisions, delete confirmation, active-session
  protection, post-recording refresh, dock toggle).
- Session markers (first slice of the v0.2 event markers & annotations
  milestone): a timestamped, session-scoped annotation model (`label`,
  host-monotonic `t_ns`, optional free-text `note`) persisted as a new
  optional top-level `markers` array in the manifest — purely additive under
  the append-only rule, so `FORMAT_VERSION` stays 1 and readers that predate
  markers ignore the key. Headless injection via
  `myopyc record --marker-file <jsonl>` (`label` plus exactly one of
  `offset_s` (seconds after recording start) or absolute `t_ns`, optional
  `note`; blank lines/`#` comments skipped, per-line validation with line
  numbers). Typed access via `SessionReader.markers` / `SessionData.markers`,
  and the `markers` array is included in `myopyc inspect` summaries (human and
  `--json`). Covered by round-trip writer → manifest → reader tests,
  manifest tolerance tests for malformed entries, JSONL parser tests and CLI
  tests. GUI marker overlay (plot lanes) is deferred to a follow-up.
- GUI marker injection: while a recording runs, chorded keyboard shortcuts
  `M 1` … `M 9` drop a marker (labelled `marker-1` … `marker-9`) into the open
  session at the current timestamp on the same host-monotonic clock the
  EMG/IMU/EVENT streams use, so markers align exactly with recorded samples.
  The shortcuts are no-ops with a status-bar hint when no recording is open;
  each successful injection is confirmed in the status bar. Covered by
  offscreen Qt tests (injection round trip into the session manifest via
  `SessionReader.markers` and the no-recording guard).
- GUI gesture annotations: a fired pose (fist, waves, double click, from either
  the simulated or the real armband) flashes a bold amber `gesture: <name>`
  label on the device panel header for 2 s, then dims to grey as the
  annotation of the most recent gesture. A checkable toolbar action
  ("Gesture annotations", on by default and persisted to `config.toml` as
  `ui.gesture_annotations`) provides toggleable suppression — when off, EVENT
  batches are still recorded and counted but never announced in the UI.
  Covered by offscreen Qt tests (flash, suppression toggle, config round trip).
- Explicit legacy-reader tolerance test for the event stream: a v0.1-style
  reader that only knows EMG/IMU skips a session's `event` stream entry and
  chunk directory without error, pinning the append-only `FORMAT_VERSION == 1`
  contract for the gesture/pose milestone.

### Changed

- Post-release version bump to `0.2.0.dev0` (development toward the v0.2
  collection-workflow milestone).
- CI now sets `QT_QPA_PLATFORM=offscreen` explicitly in the Test step so the
  headless Qt UI smoke tests (`tests/test_ui_status_health.py`) run
  deterministically in the workflow rather than relying on the test module's
  own environment default, and the Linux-only stance is documented as a
  comment in [.github/workflows/ci.yml](.github/workflows/ci.yml).

## [0.1.0] - 2026-11-20

### Added

- Session export for downstream analysis: `myopyc export <session> --format
  csv|npz [--output PATH]`. CSV writes one file per device/stream (skipping
  streams with no samples) with a human-readable ISO timestamp column
  (`t_iso`) alongside the raw `t_ns` nanoseconds; NPZ writes one compressed
  archive with `<device>/<stream>` array keys preserving the exact dtypes.
  Default output is `<session>-export` (a directory for CSV, a `.npz` file for
  NPZ).
- Simulated gesture events: `SimulatedSource` emits a plausible EVENT batch on a
  random ~4–10 s cadence (first gesture 2–6 s after start) with a pose drawn
  from the real Myo vocabulary (fist, wave_left, wave_right, double_click) and
  an orientation payload mirroring the IMU quaternion, so the event stream is
  demoable and testable without hardware. Stream health treats event-driven
  streams by flow rather than rate — the status bar reports `event <n> in
  window`, and a quiet window reads `idle`, never a misleading `stalled`/`degraded`.
- Real gesture capture from hardware: the BLE adapter now enables the Myo's
  classifier (`ClassifierMode.ENABLED`) and decodes pose classifier events into
  the EVENT stream (fist, wave_left/right, fingers_spread, double_click;
  wave_in/out mapped to wave_left/right). The orientation payload is the most
  recent IMU quaternion (identity until the first IMU sample), event rows carry
  their own sequence counter, and non-pose classifier events (arm sync, unlock)
  plus unmapped pose names are dropped rather than recorded. Covered by
  duck-typed classifier-event tests (no hardware needed).
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

### Changed

- BLE disconnect/reconnect behaviour is now surfaced in the UI and documented:
  a dropped link shows the device state as `reconnecting` (amber) in the device
  panel header, and the status-bar health line tags any device that is not
  streaming (e.g. `Left [reconnecting]: emg 0/200 Hz …`) and prefixes it with a
  warning marker while a device is reconnecting or in `error` — previously a
  dropped link could pass for a quiet stream. The reconnect policy (indefinite
  retries with exponential backoff capped at 30 s, patient discovery/connect
  retries, sequence counters and last-known battery persisting across drops)
  is documented in [docs/setup-linux.md](docs/setup-linux.md). Gates 0.1.1.

### Known limitations

- The 30-minute dual-Myo hardware soak test is pending; it will validate BLE
  link stability over long captures before/shortly after this tag. Crash-safe
  session repair (`myopyc inspect --repair`) mitigates interrupted recordings
  in the meantime.
