"""Automatic Myo discovery over BlueZ.

Two discovery channels cover all real-world armband states:

1. **BLE advertisement scan** (bleak) — finds Myos that are advertising,
   matched by the Myo service UUID ``d5060001-...``.
2. **BlueZ D-Bus** — the armband may already be connected by the OS (after
   pairing with bluetoothctl) or be known-but-idle in BlueZ's device cache.
   Both cases are invisible to a passive scan, so the BlueZ object database
   is queried directly for ``Device1`` objects carrying the Myo service.

For OS-connected devices the helper can ask BlueZ to release the connection
so that a normal BLE connect can take over.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

log = logging.getLogger(__name__)

#: Primary Myo GATT service UUID (dl-myo ``GATTProfile.MYO_SERVICE``).
MYO_SERVICE_UUID = "d5060001-a904-deb9-4748-2c7f4a124842"


def _variant(value, default=None):
    """Unwrap a dbus_fast Variant; pass plain values through."""
    inner = getattr(value, "value", value)
    return inner if inner is not None else default


class DiscoveredMyo:
    """One armband found by discovery, regardless of the channel."""

    __slots__ = ("connected", "mac", "name", "remembered")

    def __init__(
        self,
        mac: str,
        name: str | None,
        connected: bool,
        remembered: bool = False,
    ) -> None:
        self.mac = mac
        self.name = name or "Myo"
        self.connected = connected
        # True when the armband was not seen by any scan channel but is in
        # the saved bindings (it may be asleep or out of range).
        self.remembered = remembered

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        state = "connected" if self.connected else "advertising"
        return f"DiscoveredMyo({self.mac}, {self.name!r}, {state})"


async def _scan_advertising() -> list[DiscoveredMyo]:
    """Scan BLE advertisements for Myo service UUIDs (~5 s window)."""
    try:
        from bleak import BleakScanner
    except Exception:
        log.debug("bleak unavailable; skipping advertisement scan")
        return []

    found: dict[str, DiscoveredMyo] = {}
    try:
        devices = await BleakScanner.discover(timeout=5.0, return_adv=True)
    except Exception:
        log.debug("advertisement scan failed", exc_info=True)
        return []
    for device, adv in devices.values():
        uuids = [str(u).lower() for u in (adv.service_uuids or [])]
        if not any(u == MYO_SERVICE_UUID for u in uuids):
            continue
        mac = str(device.address).upper()
        found.setdefault(mac, DiscoveredMyo(mac, device.name, False))
    return list(found.values())


async def _query_bluez() -> list[DiscoveredMyo]:
    """List Myo devices from BlueZ's object database (D-Bus).

    Covers armbands that are OS-connected or cached from a previous
    pairing — neither of which appears in an advertisement scan.
    """
    try:
        from dbus_fast import BusType, Message, MessageType
        from dbus_fast.aio import MessageBus
    except Exception:
        log.debug("bluez discovery unavailable: dbus_fast not installed")
        return []

    try:
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    except Exception:
        log.debug("bluez discovery unavailable: cannot reach D-Bus system bus")
        return []

    found: list[DiscoveredMyo] = []
    try:
        reply = await bus.call(
            Message(
                destination="org.bluez",
                path="/",
                interface="org.freedesktop.DBus.ObjectManager",
                member="GetManagedObjects",
            )
        )
        if reply.message_type is MessageType.ERROR:
            raise RuntimeError(reply.error_name or "GetManagedObjects failed")
        for _path, interfaces in reply.body[0].items():
            dev = interfaces.get("org.bluez.Device1")
            if not dev:
                continue
            uuids = [str(u).lower() for u in _variant(dev.get("UUIDs"), [])]
            if not any(u == MYO_SERVICE_UUID for u in uuids):
                continue
            mac = str(_variant(dev.get("Address"), "")).upper()
            if not mac:
                continue
            found.append(
                DiscoveredMyo(
                    mac,
                    _variant(dev.get("Name"), "Myo"),
                    bool(_variant(dev.get("Connected"), False)),
                )
            )
    except Exception:
        log.debug("bluez discovery failed", exc_info=True)
    finally:
        with contextlib.suppress(Exception):
            bus.disconnect()
    return found


async def discover_myos() -> list[DiscoveredMyo]:
    """Find every reachable Myo: advertising ones plus OS-connected ones.

    Results are de-duplicated by MAC; a device seen in both channels keeps
    its ``connected`` flag so callers know a BlueZ release is needed.
    """
    scan, bluez = await asyncio.gather(_scan_advertising(), _query_bluez())
    merged: dict[str, DiscoveredMyo] = {}
    for entry in bluez + scan:
        existing = merged.get(entry.mac)
        if existing:
            existing.connected = existing.connected or entry.connected
        else:
            merged[entry.mac] = entry
    return sorted(merged.values(), key=lambda m: m.mac)


def merge_saved_bindings(
    found: list[DiscoveredMyo], labels_by_mac: dict[str, str]
) -> list[DiscoveredMyo]:
    """Add saved bindings that discovery did not see as "remembered" entries.

    A sleeping or out-of-range armband cannot be discovered, but if the user
    already bound it once it should still show up in pick lists — waking it
    up (moving it) is often all that is needed.
    """
    merged = {entry.mac: entry for entry in found}
    for mac, label in labels_by_mac.items():
        if mac not in merged:
            merged[mac] = DiscoveredMyo(mac, label, False, remembered=True)
    return sorted(merged.values(), key=lambda m: m.mac)


async def release_bluez_connection(mac: str) -> bool:
    """Disconnect an OS-level (BlueZ) connection to the armband at ``mac``.

    Returns ``True`` when a held connection was found and released.
    """
    mac = mac.upper()
    try:
        from dbus_fast import BusType, Message, MessageType
        from dbus_fast.aio import MessageBus
    except Exception:
        log.debug("bluez release unavailable: dbus_fast not installed")
        return False

    try:
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    except Exception:
        log.debug("bluez release unavailable: cannot reach D-Bus system bus")
        return False

    released = False
    try:
        reply = await bus.call(
            Message(
                destination="org.bluez",
                path="/",
                interface="org.freedesktop.DBus.ObjectManager",
                member="GetManagedObjects",
            )
        )
        if reply.message_type is MessageType.ERROR:
            raise RuntimeError(reply.error_name or "GetManagedObjects failed")
        for path, interfaces in reply.body[0].items():
            dev = interfaces.get("org.bluez.Device1")
            if not dev:
                continue
            address = str(_variant(dev.get("Address"), "")).upper()
            if address != mac:
                continue
            if not _variant(dev.get("Connected"), False):
                break
            disconnect = Message(
                destination="org.bluez",
                path=path,
                interface="org.bluez.Device1",
                member="Disconnect",
            )
            ack = await bus.call(disconnect)
            if ack.message_type is MessageType.ERROR:
                log.warning("BlueZ disconnect failed for %s: %s", mac, ack.error_name)
            else:
                released = True
                log.info("released BlueZ connection to %s (%s)", mac, path)
            break
    except Exception:
        log.debug("bluez release failed for %s", mac, exc_info=True)
    finally:
        with contextlib.suppress(Exception):
            bus.disconnect()
    return released
