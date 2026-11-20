"""Qt application bootstrap."""

from __future__ import annotations

import sys
from pathlib import Path

from myopyc.config import AppConfig


def run_gui(
    config: AppConfig,
    *,
    simulate: int = 0,
    ble_devices: list[tuple[str, str]] | None = None,
    output_dir: Path | str | None = None,
) -> int:
    from PySide6 import QtWidgets

    from myopyc.ui.main_window import MainWindow
    from myopyc.ui.theme import apply_theme

    existing = QtWidgets.QApplication.instance()
    if isinstance(existing, QtWidgets.QApplication):
        app = existing
    else:
        app = QtWidgets.QApplication(sys.argv)
    apply_theme(app)
    window = MainWindow(
        config,
        simulate=simulate,
        ble_devices=ble_devices or [],
        output_dir=output_dir,
    )
    window.show()
    return int(app.exec())
