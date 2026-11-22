# Linux setup

## Bluetooth stack

`myopyc` talks to the Myo through BlueZ using Bleak (via `dl-myo`). You need a
working BLE adapter; the Myo's original USB dongle is not used.

```bash
# Arch
sudo pacman -S bluez bluez-utils

# Debian/Ubuntu
sudo apt install bluez bluetooth
```

Make sure the adapter is up:

```bash
bluetoothctl show
bluetoothctl power on
```

## Permissions

Reading BLE advertisements and connecting usually requires access to the
`bluetooth` group or a privileged helper. Add your user to the group and log
back in:

```bash
sudo usermod -aG bluetooth "$USER"
```

If scanning still fails, check that the adapter is not blocked:

```bash
rfkill list bluetooth
```

## Finding your Myo's MAC address

The armband advertises with the Myo service UUID
`d5060001-a904-deb9-4748-2c7f4a124842`. Scan for nearby BLE devices:

```bash
bluetoothctl
[bluetooth]# scan on
```

Or use the upstream example to discover and connect without a dongle:

```bash
uv run python upstream/dl-myo/examples/sample_client.py --seconds 5
```

Copy the MAC address into `myopyc gui --ble <MAC>=Left`.

## Dual armband notes

Two concurrent BLE streams are more demanding than one. If you see frequent
disconnects or dropped samples:

- prefer a recent Intel or CSR-based adapter with a good antenna;
- keep the armbands within line of sight of the adapter;
- disable other BLE-heavy applications during recording;
- run one armband at a time to confirm the adapter is healthy.

`myopyc` reports drops and gaps explicitly rather than hiding them. If one
armband is unstable, record the other one alone; the GUI and file format support
a single device without changes.

## Myo wake/sleep behaviour

The Myo sleeps when left still. Wear it or move it to wake it before connecting.
`myopyc` asks the armband not to sleep while streaming, so a stationary arm does
not silently end a session, but a hardware disconnect (out of range, radio
interference, adapter reset) still needs a reconnect.

## Disconnect / reconnect behaviour

The BLE adapter (`dl-myo`) runs on its own thread with an automatic reconnect
loop. What happens when the link drops:

1. The adapter logs the failure and reports the device as **reconnecting**.
2. It retries with exponential backoff — 2 s, 4 s, 8 s, 16 s, capped at 30 s —
   until the link is re-established or the stream is stopped by the user
   (the retry loop only exits early on a stop request).
3. During a reconnect no samples arrive; samples from before the drop are
   preserved and everything after the link returns continues the same
   streams. The gap shows up as a time hole in the data, not corrupted
   samples — the recorder's drop/gap accounting stays accurate.

How the UI surfaces the state:

- each device panel shows the state in its header — `reconnecting` in amber,
  `error` in red, `connecting`/`stopping` in yellow, `streaming` in teal;
- the status-bar health line tags any device that is not streaming, e.g.
  `Left [reconnecting]: emg 0/200 Hz, imu 0/50 Hz | dropped 0`, and prefixes
  the whole line with `⚠` while a device is reconnecting or in `error`.

Things to know:

- **Battery level persists** across a reconnect: the last successfully read
  level is kept, and a failed read never blanks the panel.
- **Sample sequence counters survive** reconnects (EMG/IMU sequence numbers do
  not reset), so ordering across a drop is recoverable downstream.
- A device that cannot be found at connect time (asleep, out of range, or
  held by the OS) is retried patiently too: discovery is attempted up to 6
  times over ~18 s, and the BLE connect itself up to 3 times, before the
  adapter raises and the same backoff loop takes over.
- If the armband stays missing, wake it (move it around) — the running retry
  loop picks it up without restarting myopyc.

## Privacy

sEMG is biometric-adjacent data. Treat recorded sessions as sensitive and do not
publish them without consent and anonymisation.
