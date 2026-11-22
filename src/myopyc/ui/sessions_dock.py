"""Sessions dock: list, open, rename and delete recorded sessions.

Wraps :func:`myopyc.recorder.reader.find_sessions` for the listing and derives
lightweight per-session summaries directly from ``manifest.json`` (no ``.npy``
loading), so refreshing a directory of large sessions stays cheap.

"Open" (button or double-click) emits :attr:`session_opened`; the main window
starts read-only playback of that session through the plot widgets. A session
with an unreadable manifest cannot be opened (a message is emitted instead).

Tests operate the dock programmatically (``refresh`` / ``open_session`` /
``rename_session`` / ``delete_session`` with dialogs suppressed) so no Qt
widget needs a display server.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6 import QtCore, QtWidgets

from myopyc.recorder.manifest import read_manifest
from myopyc.recorder.reader import find_sessions


def session_summary(session_dir: Path | str) -> dict[str, Any]:
    """Lightweight manifest-derived summary of a session (no chunk loading)."""

    directory = Path(session_dir)
    manifest = read_manifest(directory)
    samples = 0
    dropped = 0
    chunks = 0
    devices: list[str] = []
    for key, entry in manifest.get("devices", {}).items():
        devices.append(str(entry.get("label", key)))
        for stream in entry.get("streams", {}).values():
            if not isinstance(stream, dict):
                continue
            samples += int(stream.get("samples", 0))
            dropped += int(stream.get("dropped_samples", 0))
            chunks += len(stream.get("chunks", []))
    return {
        "session_dir": directory,
        "session_id": manifest.get("session_id"),
        "created_utc": manifest.get("created_utc"),
        "status": manifest.get("status"),
        "devices": devices,
        "samples": samples,
        "dropped": dropped,
        "chunks": chunks,
        "gaps": len(manifest.get("gaps", [])),
        "markers": len(manifest.get("markers", [])),
    }


def _describe(summary: dict[str, Any]) -> str:
    """One-line human description used as the list-item text."""

    name = summary["session_dir"].name
    status = summary.get("status") or "?"
    return f"{name}  ·  {summary['samples']} samples  ·  {summary['chunks']} chunks  ·  {status}"


class SessionsDock(QtWidgets.QDockWidget):
    """Right-side dock listing recorded sessions with list/open/rename/delete."""

    #: Emitted with the session directory when a session is opened.
    session_opened = QtCore.Signal(object)  # Path
    #: Emitted with a transient status text (wired to the main status bar).
    message = QtCore.Signal(str)

    def __init__(
        self,
        get_root: Callable[[], Path],
        get_active_session: Callable[[], Path | None] | None = None,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__("Sessions", parent)
        self._get_root = get_root
        self._get_active_session = get_active_session or (lambda: None)

        self.setObjectName("sessionsDock")
        self.setAllowedAreas(
            QtCore.Qt.DockWidgetArea.LeftDockWidgetArea
            | QtCore.Qt.DockWidgetArea.RightDockWidgetArea
        )

        content = QtWidgets.QWidget(self)
        layout = QtWidgets.QVBoxLayout(content)
        layout.setContentsMargins(4, 4, 4, 4)

        self._list = QtWidgets.QListWidget(content)
        self._list.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self._list.itemDoubleClicked.connect(lambda _item: self.open_selected())
        layout.addWidget(self._list, 1)

        buttons = QtWidgets.QHBoxLayout()
        refresh_btn = QtWidgets.QPushButton("Refresh", content)
        refresh_btn.clicked.connect(self.refresh)
        buttons.addWidget(refresh_btn)
        open_btn = QtWidgets.QPushButton("Open", content)
        open_btn.clicked.connect(self.open_selected)
        buttons.addWidget(open_btn)
        rename_btn = QtWidgets.QPushButton("Rename…", content)
        rename_btn.clicked.connect(self.rename_selected)
        buttons.addWidget(rename_btn)
        delete_btn = QtWidgets.QPushButton("Delete…", content)
        delete_btn.clicked.connect(self.delete_selected)
        buttons.addWidget(delete_btn)
        layout.addLayout(buttons)

        self._empty = QtWidgets.QLabel("no sessions recorded yet", content)
        self._empty.setStyleSheet("color: #6f7a89;")
        layout.addWidget(self._empty)

        self.setWidget(content)

    # ------------------------------------------------------------------ listing
    def refresh(self) -> None:
        """Rebuild the list from ``find_sessions`` on the current root."""

        self._list.clear()
        sessions = find_sessions(self._get_root())
        self._empty.setVisible(not sessions)
        for path in sessions:
            item = QtWidgets.QListWidgetItem(path.name)
            try:
                item.setText(_describe(session_summary(path)))
            except Exception:
                # Unreadable manifest (mid-write, corrupt): still listable so
                # the user can inspect/delete it, but without a summary.
                item.setText(f"{path.name}  ·  (unreadable manifest)")
            item.setData(QtCore.Qt.ItemDataRole.UserRole, path)
            self._list.addItem(item)

    def selected_session(self) -> Path | None:
        """Path of the currently selected session, if any."""

        item = self._list.currentItem()
        if item is None:
            return None
        path = item.data(QtCore.Qt.ItemDataRole.UserRole)
        return Path(path) if path is not None else None

    # ------------------------------------------------------------------ open
    def open_selected(self) -> dict[str, Any] | None:
        session = self.selected_session()
        if session is None:
            self.message.emit("no session selected")
            return None
        return self.open_session(session)

    def open_session(self, session_dir: Path | str) -> dict[str, Any] | None:
        """Open one session for playback by emitting :attr:`session_opened`.

        Returns the manifest-derived summary dict, or ``None`` when the
        manifest is unreadable (an error message is emitted in that case, so
        the main window never receives a broken path).
        """

        directory = Path(session_dir)
        try:
            summary = session_summary(directory)
        except Exception as exc:
            self.message.emit(f"could not open {directory.name}: {exc}")
            return None
        self.session_opened.emit(directory)
        return summary

    # ------------------------------------------------------------------ rename
    def rename_selected(self) -> Path | None:
        session = self.selected_session()
        if session is None:
            self.message.emit("no session selected")
            return None
        return self.rename_session(session)

    def rename_session(self, session_dir: Path | str, new_name: str | None = None) -> Path | None:
        """Rename a session directory (manifest paths are relative, so safe).

        With ``new_name`` the action is non-interactive (used by tests);
        otherwise an input dialog asks for the new name. Returns the new path
        on success, ``None`` on refusal/failure (with a message emitted).
        """

        directory = Path(session_dir)
        if directory == self._get_active_session():
            self.message.emit("cannot rename the session currently being recorded")
            return None
        answer: str | None = new_name
        if answer is None:
            proposed = directory.name
            accepted, text = QtWidgets.QInputDialog.getText(
                self, "Rename session", "New name:", text=proposed
            )
            if not accepted:
                return None
            # PySide6's stub resolves ``getText`` to a ``(str, bool)``-shaped
            # overload in some versions, so normalize the second item to str.
            answer = str(text)
        name = answer.strip()
        if not name or "/" in name or "\\" in name or name.startswith("."):
            self.message.emit(f"invalid session name: {new_name!r}")
            return None
        target = directory.with_name(name)
        if target.exists():
            self.message.emit(f"already exists: {target.name}")
            return None
        try:
            renamed = directory.rename(target)
        except OSError as exc:
            self.message.emit(f"rename failed: {exc}")
            return None
        self.refresh()
        self.message.emit(f"renamed to {renamed.name}")
        return renamed

    # ------------------------------------------------------------------ delete
    def delete_selected(self) -> bool:
        session = self.selected_session()
        if session is None:
            self.message.emit("no session selected")
            return False
        return self.delete_session(session)

    def delete_session(self, session_dir: Path | str, *, confirm: bool = True) -> bool:
        """Delete a session directory (with a confirmation dialog by default).

        Returns ``True`` when the directory was removed.
        """

        directory = Path(session_dir)
        if directory == self._get_active_session():
            self.message.emit("cannot delete the session currently being recorded")
            return False
        if confirm:
            answer = QtWidgets.QMessageBox.question(
                self,
                "Delete session",
                f"Delete session {directory.name} and all its data?\nThis cannot be undone.",
                defaultButton=QtWidgets.QMessageBox.StandardButton.No,
            )
            if answer != QtWidgets.QMessageBox.StandardButton.Yes:
                self.message.emit("delete cancelled")
                return False
        try:
            shutil.rmtree(directory)
        except OSError as exc:
            self.message.emit(f"delete failed: {exc}")
            return False
        self.refresh()
        self.message.emit(f"deleted {directory.name}")
        return True
