"""Dark Qt/pyqtgraph styling for the desktop app."""

from __future__ import annotations

import pyqtgraph as pg
from PySide6 import QtGui, QtWidgets

ACCENT = "#48d1cc"
BACKGROUND = "#15181d"
PANEL = "#1d2229"
TEXT = "#e6e9ef"


def apply_theme(app: QtWidgets.QApplication) -> None:
    app.setStyle("Fusion")
    palette = QtGui.QPalette()
    palette.setColor(QtGui.QPalette.ColorRole.Window, QtGui.QColor(BACKGROUND))
    palette.setColor(QtGui.QPalette.ColorRole.WindowText, QtGui.QColor(TEXT))
    palette.setColor(QtGui.QPalette.ColorRole.Base, QtGui.QColor(PANEL))
    palette.setColor(QtGui.QPalette.ColorRole.AlternateBase, QtGui.QColor(BACKGROUND))
    palette.setColor(QtGui.QPalette.ColorRole.Text, QtGui.QColor(TEXT))
    palette.setColor(QtGui.QPalette.ColorRole.Button, QtGui.QColor(PANEL))
    palette.setColor(QtGui.QPalette.ColorRole.ButtonText, QtGui.QColor(TEXT))
    palette.setColor(QtGui.QPalette.ColorRole.Highlight, QtGui.QColor(ACCENT))
    palette.setColor(QtGui.QPalette.ColorRole.HighlightedText, QtGui.QColor("#101317"))
    palette.setColor(QtGui.QPalette.ColorRole.ToolTipBase, QtGui.QColor(PANEL))
    palette.setColor(QtGui.QPalette.ColorRole.ToolTipText, QtGui.QColor(TEXT))
    app.setPalette(palette)
    app.setStyleSheet(
        f"""
        QToolBar {{ background: {PANEL}; border: 0; spacing: 6px; padding: 4px; }}
        QStatusBar {{ background: {PANEL}; color: {TEXT}; }}
        QGroupBox {{ border: 1px solid #2b323c; border-radius: 6px; margin-top: 12px; }}
        QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {ACCENT}; }}
        QPushButton {{ padding: 5px 12px; border-radius: 4px; background: #2a313b; }}
        QPushButton:hover {{ background: #343d49; }}
        QPushButton:checked {{ background: #b23b3b; }}
        """
    )
    pg.setConfigOptions(antialias=False, background=BACKGROUND, foreground=TEXT)
