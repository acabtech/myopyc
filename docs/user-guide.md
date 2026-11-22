# User guide: collection workflows

This guide covers the v0.2 collection workflows that are implemented in
`myopyc` today: markers, gesture annotations, derived EMG overlays, session
management/playback, and export. It complements the command reference in
[`usage.md`](usage.md) and the normative on-disk contract in
[`data-format.md`](data-format.md).

## Markers

Markers are session-scoped annotations on the shared host-monotonic `t_ns`
timeline. They are stored in the manifest as an append-only optional top-level
`markers` array (`FORMAT_VERSION` remains `1`), so they align with recorded
EMG/IMU/EVENT samples by timestamp.

### GUI injection (live recording)

While a recording is running, press `M` followed by a number-row digit to
inject a marker at the current host-monotonic time:

| Shortcut | Label |
|---|---|
| `M 1` … `M 9` | `marker-1` … `marker-9` |

Behavior:

- The status bar confirms each successful injection.
- If no recording is active, the shortcut is a no-op and the status bar shows a
  hint.
- Injected markers are written through the active recorder and appear in the
  session manifest/`inspect` summary.

### Headless injection

Use `myopyc record --marker-file <jsonl>` to pre-plan markers for a headless
recording. The file is JSON Lines: one JSON object per line, blank lines and
`#` comments are skipped.

```jsonl
{"label": "task start", "offset_s": 0, "note": "participant 7"}
{"label": "flex cue", "offset_s": 30.5}
{"label": "hardware sync", "t_ns": 9876543210}
```

Rules:

- `label` is required and must be non-empty.
- Provide exactly one of `offset_s` (seconds after recording start, resolved on
  the host-monotonic clock used by streams) or absolute `t_ns`.
- `note` is optional free text.
- Invalid lines fail with line-numbered validation errors before/during command
  handling as documented by the CLI.

See [`usage.md#markers`](usage.md#markers) and
[`data-format.md#markers`](data-format.md#markers) for command/schema details.

### Marker overlay and review

Markers render as vertical dashed amber lines labeled on the shared `t_ns` axis
in EMG and IMU plots. The same overlay is used in:

- **Live recording views** — refreshed when a marker is injected into the open
  recording.
- **Read-only playback** — rendered from the session's persisted manifest
  markers at the current playhead window.

For programmatic access, use `SessionReader.markers` or `session.markers` from
`myopyc.recorder.load_session()`. `myopyc inspect` also includes marker
summaries.

## Gestures and pose events

Gesture/pose events are recorded as a first-class `event` stream
(`StreamKind.EVENT`), not as manifest-only markers. The stream is event-driven
(`sample_rate_hz` 0.0), uses the shared host-monotonic `t_ns` timebase, and
stores pose type plus an orientation quaternion payload.

### GUI gesture annotations

When a pose event fires, the device panel header flashes a bold amber
`gesture: <name>` label for ~2 seconds, then dims to the most recent gesture.

- **Gesture annotations** toolbar action toggles UI suppression.
- The toggle is persisted as `ui.gesture_annotations` in
  `~/.config/myopyc/config.toml` (default on).
- Suppression affects UI only: EVENT batches are still recorded and counted.

### Event data access

Recorded events are readable like EMG/IMU through `SessionReader` and
`load_session()`:

```python
from myopyc.recorder import load_session

session = load_session("path/to/session")
events = session["left"]["event"].samples
# fields: t_ns, seq, type, ox, oy, oz, ow
```

The event vocabulary and dtype are specified in the
[EVENT chunk dtype section](data-format.md#event-deviceskeyeventchunk-nnnnnpy).

## Derived channels, RMS, and notch

Derived EMG overlays are display-only signal-quality aids. They never modify
recorded raw data.

### Enable in GUI

Use the checkable **Derived channels** toolbar action. When enabled, each EMG
plot shows:

- **Rectified traces** (dashed grey), per channel.
- **Combined RMS envelope** (bold light curve), computed over a 250 ms moving
  window of the channel mean.

The toggle is persisted as `ui.derived_channels` in `config.toml` (default
off). Disabling clears stale overlay geometry so re-enabling does not flash old
data.

### Notch filter

An optional per-channel notch filter can be applied in the derived-overlay path
only (default 60 Hz; toolbar toggle, persisted as `ui.notch_hz`). Set notch to
0 Hz/off to disable. Raw recorded samples are unaffected.

### Current scope / not implemented

The v0.2 roadmap still has open signal-quality items that are **not implemented
yet**:

- Raw-signal quality view with per-channel histogram/saturation detection
  during a calibration routine.
- Guided calibration workflow (relax/flex prompts) writing a `calibration`
  block into the manifest.

Do not expect calibration blocks or guided calibration prompts in the current
GUI/CLI.

## Sessions, playback, and notes

### Sessions dock

The **Sessions** dock lists recorded sessions in the configured output
directory (newest first). Actions:

- **Open** / double-click: open read-only playback.
- **Rename…**: renames the session directory (manifest chunk paths are relative;
  active recording is protected).
- **Delete…**: confirms, then removes the session directory; active recording is
  protected.
- **Notes…**: edits the session's manifest-canonical `notes` field via an
  atomic manifest rewrite. Missing notes read as empty. Active recording is
  protected from dock edits.
- **Refresh**: rescan after external changes.

### Read-only playback

Playback reuses the same per-device EMG/IMU plot widgets but reads only from
recorded `.npy` chunks. It never writes to the session directory.

Controls:

- **Scrub slider**: moves the playhead; plots show the 6 s window ending at the
  playhead.
- **Play/Pause**: runs at 2x real time; Play after end replays from the top.
- **Stop**: closes playback and re-mounts live device views.

Safety/mutual exclusion:

- Cannot start playback while recording is active.
- Starting recording closes playback first.
- Adding armbands while playback is open waits until playback closes.
- Simulated/BLE devices may keep streaming during playback, but live buffers are
  separate from playback buffers.

## Export

### CSV / NPZ

Use `myopyc export <session> --format csv|npz` for one-off analysis:

- **CSV**: one file per device/stream. Includes human-readable `t_iso`
  alongside raw `t_ns`. `t_iso` is derived from the host monotonic clock for
  readability/sorting, not wall-clock ground truth.
- **NPZ**: one compressed archive with `<device>/<stream>` keys, preserving
  exact dtypes.

See [`usage.md#exporting-a-session`](usage.md#exporting-a-session).

### ZIP with integrity check

Use `myopyc export <session> --format zip` for archival/transfer. The archive
contains the full session directory plus `INTEGRITY.json` (SHA-256 digests for
archived members). The export verifies itself after writing.

Programmatic verification:

```python
from myopyc.recorder.export import verify_session_zip

report = verify_session_zip("path/to/session.zip")
assert report.ok, report.as_dict()
```

`verify_session_zip()` reports missing members, digest mismatches, and
unexpected members.

## Related docs

- [`usage.md`](usage.md) — command/reference details and troubleshooting.
- [`data-format.md`](data-format.md) — normative manifest/chunk/marker/event
  contract.
- [`interop-lab-software.md`](interop-lab-software.md) — NumPy/MAT/EEGLAB/
  LabChart-oriented import recipes.
- [`setup-linux.md`](setup-linux.md) — Linux BLE/permissions setup.
