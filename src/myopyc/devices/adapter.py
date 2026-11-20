"""Real Myo source built on the ``dl-myo`` package (dongleless BLE).

``dl-myo`` is an optional dependency (``pip install 'myopyc[hardware]'``). The
adapter runs ``dl-myo``'s asyncio client on a private thread so the Qt event
loop and the recorder never wait on Bluetooth.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections.abc import Callable

import numpy as np

from myopyc.devices.discovery import release_bluez_connection
from myopyc.devices.model import DeviceInfo, SampleBatch
from myopyc.streams.schema import EMG_DTYPE, IMU_DTYPE, StreamKind

log = logging.getLogger(__name__)

EmitFn = Callable[[SampleBatch], None]
StatusFn = Callable[[str, str, str | None], None]
BatteryFn = Callable[[str, int], None]

_INSTALL_HINT = (
    "dl-myo is not installed. Install the hardware extra: pip install 'myopyc[hardware]'"
)


def dl_myo_available() -> bool:
    try:
        import myo  # noqa: F401
    except Exception:
        return False
    return True


class DlMyoSource:
    """Stream raw EMG and IMU from one physical Myo via ``dl-myo``."""

    def __init__(
        self,
        info: DeviceInfo,
        emit: EmitFn,
        status: StatusFn | None = None,
        battery: BatteryFn | None = None,
    ) -> None:
        if not info.mac:
            raise ValueError("a MAC address is required for a real Myo")
        if not dl_myo_available():
            raise RuntimeError(_INSTALL_HINT)
        self.info = info
        self._emit = emit
        self._status = status or (lambda *_: None)
        self._battery = battery or (lambda *_: None)
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._stop_requested = False
        self._emg_seq = 0
        self._imu_seq = 0

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_requested = False
        self._thread = threading.Thread(
            target=self._thread_main, name=f"myo-{self.info.key}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_requested = True
        loop = self._loop
        stop_event = self._stop_event
        if loop and stop_event and loop.is_running():
            loop.call_soon_threadsafe(stop_event.set)
        if self._thread:
            self._thread.join(timeout=8.0)
        self._status(self.info.key, "stopped", None)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ------------------------------------------------------------------ thread
    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            attempt = 0
            while True:
                try:
                    loop.run_until_complete(self._session())
                    break  # clean stop from within _session
                except Exception as exc:
                    if self._stop_requested:
                        break
                    attempt += 1
                    delay = min(30.0, 2.0 * attempt)
                    log.warning(
                        "Myo %s dropped (%s); reconnecting in %.0fs",
                        self.info.mac,
                        exc,
                        delay,
                    )
                    self._status(self.info.key, "reconnecting", str(exc))
                    time.sleep(delay)
        finally:
            self._loop = None
            self._stop_event = None
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()

    async def _session(self) -> None:
        from myo import Myo, MyoClient
        from myo.types import (
            AggregatedData,
            ClassifierEvent,
            ClassifierMode,
            EMGData,
            EMGMode,
            FVData,
            IMUData,
            IMUMode,
            MotionEvent,
            SleepMode,
        )

        source = self

        class Client(MyoClient):
            async def on_classifier_event(self, ce: ClassifierEvent) -> None:
                return None

            async def on_aggregated_data(self, ad: AggregatedData) -> None:
                return None

            async def on_emg_data(self, emg: EMGData) -> None:
                source._on_emg(emg)

            async def on_emg_data_aggregated(self, eds) -> None:
                return None

            async def on_fv_data(self, fvd: FVData) -> None:
                return None

            async def on_imu_data(self, imu: IMUData) -> None:
                source._on_imu(imu)

            async def on_motion_event(self, me: MotionEvent) -> None:
                return None

        self._status(self.info.key, "connecting", self.info.mac)
        myo = await Myo.with_mac(self.info.mac)
        if myo is None:
            # A paired Myo that BlueZ already holds a connection to stops
            # advertising, so the discovery scan never sees it. Ask BlueZ to
            # release the connection, then retry discovery patiently: the
            # armband may be asleep and only resumes advertising after it
            # wakes (movement) or a few seconds after being disconnected.
            await self._release_bluez_connection()
            self._status(self.info.key, "connecting", "retrying discovery")
            for attempt in range(5):
                await asyncio.sleep(3.0)
                myo = await Myo.with_mac(self.info.mac)
                if myo is not None:
                    break
                self._status(self.info.key, "connecting", f"discovery retry {attempt + 2}/6")
        if myo is None:
            raise RuntimeError(f"Myo {self.info.mac} not found")

        client = Client()
        client.m = myo
        # An armband that was just disconnected by BlueZ (or woken from sleep)
        # often refuses the first connection attempt; bleak surfaces this as a
        # connect TimeoutError. Retry patiently before giving up.
        last_connect_error: Exception | None = None
        for attempt in range(3):
            try:
                await client.connect()
                last_connect_error = None
                break
            except TimeoutError as exc:
                last_connect_error = exc
                self._status(self.info.key, "connecting", f"connect retry {attempt + 1}/3")
                await asyncio.sleep(3.0)
        if last_connect_error is not None:
            raise RuntimeError(
                f"Myo {self.info.mac} connect failed: {last_connect_error}"
            ) from last_connect_error
        try:
            await client.setup(
                classifier_mode=ClassifierMode.DISABLED,
                emg_mode=EMGMode.SEND_RAW,
                imu_mode=IMUMode.SEND_DATA,
            )
            # Keep-alive: never let the armband sleep while we stream, so a
            # stationary arm doesn't silently kill the session mid-record.
            try:
                await client.set_sleep_mode(SleepMode.NEVER_SLEEP)
            except Exception:
                log.debug("could not set NEVER_SLEEP for %s", self.info.mac, exc_info=True)
            self._stop_event = asyncio.Event()
            await client.start()
            battery_task = asyncio.create_task(self._battery_loop(client))
            self._status(self.info.key, "streaming", "raw EMG + IMU")
            await self._stop_event.wait()
            battery_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await battery_task
            self._status(self.info.key, "stopping", None)
        finally:
            # Restore normal sleep behaviour before disconnecting.
            with contextlib.suppress(Exception):
                await client.set_sleep_mode(SleepMode.NORMAL)
            try:
                await client.stop()
            except Exception:
                log.debug("stop() failed for %s", self.info.mac, exc_info=True)
            await client.disconnect()

    # ------------------------------------------------------------------ bluez

    async def _release_bluez_connection(self) -> bool:
        """Disconnect an OS-level (BlueZ) connection to this Myo.

        When a paired armband is connected by the OS (e.g. after pairing with
        bluetoothctl), it stops advertising and ``dl-myo``'s discovery scan
        cannot find it. Asking BlueZ to disconnect via D-Bus lets the device
        resume advertising so a normal connect can proceed.
        """
        return await release_bluez_connection(self.info.mac or "")

    # ------------------------------------------------------------------ battery
    async def _battery_loop(self, client) -> None:
        """Report the battery level, then re-read it every ~60 s.

        Runs as a background task next to the stream loop so a slow/failing
        battery read can never block EMG/IMU delivery. A failed read keeps the
        previously reported level rather than blanking the UI.
        """

        while True:
            try:
                level = await client.battery_level()
                self._battery(self.info.key, int(level))
            except Exception:
                log.debug("battery level unavailable for %s", self.info.mac, exc_info=True)
            await asyncio.sleep(60.0)

    # ------------------------------------------------------------------ decoding
    def _on_emg(self, emg) -> None:
        t_ns = time.monotonic_ns()
        rows = np.empty(2, dtype=EMG_DTYPE)
        rows["t_ns"] = (t_ns, t_ns)
        rows["seq"] = (self._emg_seq, self._emg_seq + 1)
        for channel in range(8):
            rows[f"ch{channel + 1}"] = (emg.sample1[channel], emg.sample2[channel])
        self._emg_seq += 2
        self._emit(SampleBatch(self.info.key, StreamKind.EMG, rows))

    def _on_imu(self, imu) -> None:
        row = np.empty(1, dtype=IMU_DTYPE)
        row["t_ns"] = time.monotonic_ns()
        row["seq"] = self._imu_seq
        row["qw"] = imu.orientation.w
        row["qx"] = imu.orientation.x
        row["qy"] = imu.orientation.y
        row["qz"] = imu.orientation.z
        row["ax"], row["ay"], row["az"] = imu.accelerometer
        row["gx"], row["gy"], row["gz"] = imu.gyroscope
        self._imu_seq += 1
        self._emit(SampleBatch(self.info.key, StreamKind.IMU, row))
