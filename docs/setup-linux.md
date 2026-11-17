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
`myopyc` asks the armband not to sleep while streaming, but a hardware
disconnect still needs a reconnect.

## Privacy

sEMG is biometric-adjacent data. Treat recorded sessions as sensitive and do not
publish them without consent and anonymisation.
