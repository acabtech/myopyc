# myopyc v0.2.0

Local release draft prepared from `main` (no push, no GitHub publish).

## Highlights

- **Collection workflow / annotations**: session markers model, GUI/headless marker injection, manifest-persisted markers, and marker overlays in live plots/playback/inspect.
- **Event stream**: first-class `StreamKind.EVENT` with dtype/schema/manifest/reader support, simulated gesture events, GUI gesture flash/toggle, and recorded event chunks readable via `SessionReader`.
- **Session management**: sessions dock (list/open/rename/delete/notes), read-only playback through plot widgets, per-session notes, and ZIP export with integrity verification.
- **Export & interop**: `myopyc export --format csv|npz|zip`, CSV with ISO timestamps, and NumPy/lab-software interchange documentation.
- **Signal quality / UX**: derived EMG overlays (rectified/RMS), optional per-channel notch, RAM overflow warning in status bar.
- **Robustness**: BLE auto-reconnect policy hardened with explicit exponential backoff (2/4/8/16s capped at 30s), reconnecting status/detail reporting, and stop-interruptible reconnect waits.
- **Docs**: user guide, GitHub Pages docs site workflow, data-format contract updates, lab-software interop recipe.

## Validation

- `uv run ruff check src/myopyc/devices/adapter.py tests/test_devices.py` — passed
- `QT_QPA_PLATFORM=offscreen uv run python -m pytest tests/test_cli_inspect_json.py tests/test_cli_record.py tests/test_config.py tests/test_devices.py tests/test_export.py tests/test_health.py tests/test_live.py tests/test_markers.py tests/test_recorder.py tests/test_repair.py tests/test_schema.py tests/test_version.py -q --no-cov` — passed (105 passed)
- `uv build` — built `dist/myopyc-0.2.0.tar.gz` and `dist/myopyc-0.2.0-py3-none-any.whl`

## Notes / caveats

- Local draft only: **not pushed**, **not published to GitHub Releases/PyPI**.
- Full Qt/offscreen UI test collection in this environment hit a PySide6/pyqtgraph import incompatibility (`PySide6 has no attribute __version__` during collection). Targeted non-Qt and reconnect/health suites passed; UI release confidence should be confirmed in CI/clean env before publishing.
- Hardware soak precondition from v0.1 remains a separate hardware validation concern; no new hardware soak was run for this draft.
