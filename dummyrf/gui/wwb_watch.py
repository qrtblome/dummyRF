"""Load scans that Shure Wireless Workbench saves, as soon as it saves them.

Receiver scans (ULX-D, SLX-D, AD …) are only available through WWB — the
receivers' command-string interface has no scan.  WWB stores every scan as
.sdb3 in ``~/Documents/Shure/Frequency Plots`` (and its ``Autosaved`` /
``Imported`` subfolders); this watcher polls those folders and hands every new,
completely written file to the Spectrum tab.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

from ..scan import Scan, load_scan

DEFAULT_FOLDER = Path.home() / "Documents" / "Shure" / "Frequency Plots"


class WwbScanWatcher(QObject):
    new_scan = Signal(object)      # Scan
    failed = Signal(str)

    POLL_MS = 2000

    def __init__(self, folder: Path = DEFAULT_FOLDER, parent=None):
        super().__init__(parent)
        self.folder = Path(folder)
        self._timer = QTimer(self)
        self._timer.setInterval(self.POLL_MS)
        self._timer.timeout.connect(self.poll)
        self._since = 0.0
        self._seen: set[str] = set()
        self._sizes: dict[str, int] = {}

    @property
    def active(self) -> bool:
        return self._timer.isActive()

    def start(self, folder: Path | None = None) -> None:
        if folder is not None:
            self.folder = Path(folder)
        # only scans saved from now on — not the whole history
        self._since = time.time()
        self._seen = {str(p) for p in self._files()}
        self._sizes.clear()
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def _files(self) -> list[Path]:
        out = []
        for d in (self.folder, *[p for p in self._subdirs()]):
            try:
                with os.scandir(d) as it:
                    out += [Path(e.path) for e in it
                            if e.is_file() and e.name.lower().endswith(".sdb3")]
            except OSError:
                continue
        return out

    def _subdirs(self) -> list[Path]:
        try:
            with os.scandir(self.folder) as it:
                return [Path(e.path) for e in it if e.is_dir()]
        except OSError:
            return []

    def poll(self) -> None:
        for p in self._files():
            key = str(p)
            if key in self._seen:
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_mtime < self._since - 1:
                self._seen.add(key)
                continue
            # wait until WWB has finished writing: size unchanged since last poll
            if self._sizes.get(key) != st.st_size:
                self._sizes[key] = st.st_size
                continue
            self._seen.add(key)
            self._sizes.pop(key, None)
            try:
                self.new_scan.emit(load_scan(p))
            except (OSError, ValueError) as e:
                self.failed.emit(f"{p.name}: {e}")

    def load_latest(self) -> Scan | None:
        """The most recently saved scan in the folder, whatever its age."""
        files = sorted(self._files(), key=lambda p: p.stat().st_mtime, reverse=True)
        for p in files:
            try:
                return load_scan(p)
            except (OSError, ValueError):
                continue
        return None
