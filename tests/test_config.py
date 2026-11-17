from __future__ import annotations

from myopyc.config import AppConfig, DeviceBinding, RecorderSettings, load_config, save_config
from myopyc.devices.model import slugify, unique_key


def test_slugify() -> None:
    assert slugify("Left arm") == "left-arm"
    assert slugify("  ") == "myo"
    assert slugify("Arm/B") == "arm-b"


def test_unique_key() -> None:
    assert unique_key("Left", set()) == "left"
    assert unique_key("Left", {"left"}) == "left-2"
    assert unique_key("Left", {"left", "left-2"}) == "left-3"


def test_config_roundtrip(tmp_path) -> None:
    path = tmp_path / "config.toml"
    config = AppConfig()
    config.devices.append(DeviceBinding(mac="D2:3B:85:94:32:8E", label="Left", slot=0))
    config.recorder = RecorderSettings(
        flush_interval_s=1.5, max_ram_bytes=1024, output_dir=str(tmp_path)
    )
    save_config(config, path)

    loaded = load_config(path)
    assert loaded.devices[0].mac == "D2:3B:85:94:32:8E"
    assert loaded.devices[0].label == "Left"
    assert loaded.recorder.flush_interval_s == 1.5
    assert loaded.recorder.max_ram_bytes == 1024


def test_missing_config_uses_defaults(tmp_path) -> None:
    loaded = load_config(tmp_path / "nope.toml")
    assert loaded.devices == []
    assert loaded.recorder.flush_interval_s == 2.0


def test_bad_config_falls_back(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("this is not = [valid toml", encoding="utf-8")
    loaded = load_config(path)
    assert loaded.devices == []


def test_binding_update_is_stable(tmp_path) -> None:
    config = AppConfig()
    config.add_or_update_binding("AA:BB", "Left", slot=0)
    config.add_or_update_binding("AA:BB", "Right", slot=1)
    assert len(config.devices) == 1
    assert config.devices[0].label == "Right"
    assert config.binding_for_mac("aa:bb") is not None


def test_validation_warns_for_duplicates_and_limits() -> None:
    config = AppConfig()
    config.devices = [
        DeviceBinding("AA:BB", "Left", 0),
        DeviceBinding("AA:BB", "Right", 1),
        DeviceBinding("CC:DD", "Third", 2),
    ]
    warnings = config.validate()
    assert any("duplicate MAC" in warning for warning in warnings)
    assert any("more than two" in warning for warning in warnings)
