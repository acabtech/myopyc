"""User configuration (XDG config, TOML) and default paths."""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import tomli_w

from myopyc.devices.model import slugify

log = logging.getLogger(__name__)

APP_DIR = "myopyc"
CONFIG_VERSION = 1


def config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_DIR


def data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / APP_DIR


def config_path() -> Path:
    return config_home() / "config.toml"


def default_output_dir() -> Path:
    return data_home() / "sessions"


@dataclass(frozen=True)
class DeviceBinding:
    """A remembered armband: MAC address mapped to a stable user label."""

    mac: str
    label: str
    slot: int = 0

    @property
    def key(self) -> str:
        return slugify(self.label)


@dataclass
class RecorderSettings:
    flush_interval_s: float = 2.0
    max_ram_bytes: int = 64 * 1024 * 1024
    output_dir: str = field(default_factory=lambda: str(default_output_dir()))


@dataclass
class UIConfig:
    emg_window_s: float = 2.0
    imu_window_s: float = 4.0
    auto_start_streaming: bool = True
    gesture_annotations: bool = True
    derived_channels: bool = False
    notch_hz: float = 60.0


@dataclass
class AppConfig:
    version: int = CONFIG_VERSION
    devices: list[DeviceBinding] = field(default_factory=list)
    recorder: RecorderSettings = field(default_factory=RecorderSettings)
    ui: UIConfig = field(default_factory=UIConfig)

    def binding_for_mac(self, mac: str) -> DeviceBinding | None:
        wanted = mac.lower()
        return next((b for b in self.devices if b.mac.lower() == wanted), None)

    def add_or_update_binding(self, mac: str, label: str, slot: int = 0) -> DeviceBinding:
        """Store a MAC/label association, replacing an older label for the same MAC."""

        if not mac.strip():
            raise ValueError("MAC address must not be empty")
        for index, existing in enumerate(self.devices):
            if existing.mac.lower() == mac.lower():
                binding = DeviceBinding(mac=mac, label=label, slot=slot)
                self.devices[index] = binding
                self.devices.sort(key=lambda b: b.slot)
                return binding
        binding = DeviceBinding(mac=mac, label=label, slot=slot)
        self.devices.append(binding)
        self.devices.sort(key=lambda b: b.slot)
        return binding

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["version"] = CONFIG_VERSION
        return data

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AppConfig:
        devices = [
            DeviceBinding(
                mac=str(item.get("mac", "")),
                label=str(item.get("label", "")),
                slot=int(item.get("slot", 0)),
            )
            for item in raw.get("devices", [])
            if item.get("mac") and item.get("label")
        ]
        recorder_raw = raw.get("recorder", {})
        ui_raw = raw.get("ui", {})
        recorder = RecorderSettings(
            flush_interval_s=float(recorder_raw.get("flush_interval_s", 2.0)),
            max_ram_bytes=int(recorder_raw.get("max_ram_bytes", 64 * 1024 * 1024)),
            output_dir=str(recorder_raw.get("output_dir", default_output_dir())),
        )
        ui = UIConfig(
            emg_window_s=float(ui_raw.get("emg_window_s", 2.0)),
            imu_window_s=float(ui_raw.get("imu_window_s", 4.0)),
            auto_start_streaming=bool(ui_raw.get("auto_start_streaming", True)),
            gesture_annotations=bool(ui_raw.get("gesture_annotations", True)),
            derived_channels=bool(ui_raw.get("derived_channels", False)),
            notch_hz=float(ui_raw.get("notch_hz", 60.0)),
        )
        return cls(
            version=int(raw.get("version", CONFIG_VERSION)),
            devices=devices,
            recorder=recorder,
            ui=ui,
        )

    def validate(self) -> list[str]:
        """Return human-readable warnings; never raises so a bad file cannot block startup."""

        warnings: list[str] = []
        seen_macs: set[str] = set()
        seen_labels: set[str] = set()
        for binding in self.devices:
            mac = binding.mac.lower()
            label = binding.label.lower()
            if mac in seen_macs:
                warnings.append(f"duplicate MAC binding: {binding.mac}")
            if label in seen_labels:
                warnings.append(f"duplicate label binding: {binding.label}")
            seen_macs.add(mac)
            seen_labels.add(label)
        if len(self.devices) > 2:
            warnings.append("more than two device bindings configured; only two can stream at once")
        if self.recorder.flush_interval_s <= 0:
            warnings.append("recorder.flush_interval_s must be positive")
        if self.recorder.max_ram_bytes < 1024 * 1024:
            warnings.append("recorder.max_ram_bytes is very small; expect frequent dropped samples")
        return warnings


def load_config(path: Path | None = None) -> AppConfig:
    """Load configuration, falling back to defaults if the file is absent or invalid."""

    path = path or config_path()
    if not path.exists():
        return AppConfig()
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
        config = AppConfig.from_dict(raw)
    except (OSError, tomllib.TOMLDecodeError, TypeError, ValueError) as exc:
        log.warning("could not read %s (%s); using defaults", path, exc)
        return AppConfig()

    for warning in config.validate():
        log.warning("config: %s", warning)
    return config


def save_config(config: AppConfig, path: Path | None = None) -> Path:
    """Write configuration atomically, creating parent directories as needed."""

    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = tomli_w.dumps(config.to_dict())
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, path)
    return path
