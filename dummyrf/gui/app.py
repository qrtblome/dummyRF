"""Main window."""
from __future__ import annotations

import gc
import sys
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QApplication, QFileDialog, QMainWindow, QMessageBox, QTabWidget

from .. import library
from ..manager import DeviceManager
from ..scan import load_scan
from ..show import Show
from . import dialogs
from .context import AppContext
from .settings import app_settings
from .coordination_tab import CoordinationTab
from .devices_tab import DevicesTab
from .fit import fit_to_screen, keep_on_screen, tame_combos
from .spectrum_tab import SpectrumTab

APP_NAME = "dummyRF"


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        lib = library.get()  # loads JSON data and applies band sets to device profiles
        self.manager = DeviceManager()
        self.ctx = AppContext(self.manager)
        self.path: Path | None = None

        self.tabs = QTabWidget()
        self.devices = DevicesTab(self.ctx)
        self.coord = CoordinationTab(self.ctx)
        self.spectrum = SpectrumTab(self.ctx)
        self.tabs.addTab(self.devices, "Devices")
        self.tabs.addTab(self.coord, "Coordination")
        self.tabs.addTab(self.spectrum, "Spectrum")
        self.setCentralWidget(self.tabs)
        self._menus()
        self.manager.log.connect(lambda t: self.statusBar().showMessage(t, 5000))
        tame_combos(self)
        self._restore_geometry()
        self._title()
        if lib.errors:
            self.devices.append_log("Library problems:\n  " + "\n  ".join(lib.errors))

    def _menus(self):
        fm = self.menuBar().addMenu("&File")
        for text, key, fn in [("&New show", QKeySequence.New, self.new_show),
                              ("&Open show…", QKeySequence.Open, self.open_show),
                              ("&Save", QKeySequence.Save, self.save_show),
                              ("Save &as…", QKeySequence.SaveAs, self.save_as)]:
            a = QAction(text, self)
            a.setShortcut(key)
            a.triggered.connect(fn)
            fm.addAction(a)
        fm.addSeparator()
        q = QAction("&Quit", self)
        q.setShortcut(QKeySequence.Quit)
        q.triggered.connect(self.close)
        fm.addAction(q)
        tm = self.menuBar().addMenu("&Tools")
        scan = QAction("Scan network for devices…", self)
        scan.setShortcut("Ctrl+F")
        scan.triggered.connect(self.devices.scan_network)
        tm.addAction(scan)
        demo = QAction("Start demo rack (simulators)", self)
        demo.triggered.connect(self.devices.start_demo)
        tm.addAction(demo)

    # ------------------------------------------------------------ geometry
    def _restore_geometry(self):
        geo = app_settings().value("window/geometry")
        if geo is not None and self.restoreGeometry(geo):
            keep_on_screen(self)
        else:
            fit_to_screen(self, 0.9, 0.9)

    def _title(self):
        self.setWindowTitle(f"{APP_NAME} — {self.path.name if self.path else 'untitled show'}")

    # ------------------------------------------------------------ show files
    def _load(self, show: Show):
        for uid in list(self.manager.entries):
            self.manager.remove_device(uid)
        self.ctx.show_data = show
        self.ctx.scans.clear()
        missing = []
        for p in show.scan_paths:
            try:
                self.ctx.scans.append(load_scan(p))
            except (OSError, ValueError):
                missing.append(p)
        show.scan_paths = [p for p in show.scan_paths if p not in missing]
        for spec in show.devices:
            self.manager.add_device(spec)
        self.devices.editor.clear()
        self.coord.conflicts.clear()
        self.ctx.notify_all()
        if missing:
            dialogs.warning(self, "Open show", "Scan files not found:\n" + "\n".join(missing))

    def new_show(self):
        if dialogs.question(self, "New show", "Discard the current show?") == QMessageBox.Yes:
            self.path = None
            self._load(Show())
            self._title()

    def open_show(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open show", "", "dummyRF show (*.rfshow *.json)")
        if not path:
            return
        try:
            show = Show.load(path)
        except (OSError, ValueError, TypeError) as e:
            dialogs.critical(self, "Open show", f"Could not open {path}:\n{e}")
            return
        self.path = Path(path)
        self._load(show)
        self._title()

    def save_show(self):
        if not self.path:
            return self.save_as()
        try:
            self.ctx.show_data.save(self.path)
            self.statusBar().showMessage(f"Saved {self.path}", 4000)
        except OSError as e:
            dialogs.critical(self, "Save", str(e))

    def save_as(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save show", "show.rfshow",
                                              "dummyRF show (*.rfshow)")
        if path:
            self.path = Path(path)
            self.save_show()
            self._title()

    def closeEvent(self, ev):
        app_settings().setValue("window/geometry", self.saveGeometry())
        self.spectrum.scanner.shutdown()
        self.manager.shutdown()
        super().closeEvent(ev)


def _gc_on_gui_thread(app: QApplication) -> None:
    """Run Python's cycle collector only on the GUI thread.

    Automatic collection can start on any thread that allocates — here the
    asyncio I/O thread — and freeing Qt widgets there crashed PySide
    (Shiboken::callCppDestructor<QComboBox> via mainThreadDeletionHandler).
    Plain reference counting is unaffected; only cycles wait for the timer.
    """
    gc.disable()
    ticks = 0

    def collect():
        nonlocal ticks
        ticks += 1
        gc.collect(2 if ticks % 10 == 0 else 0)

    timer = QTimer(app)
    timer.timeout.connect(collect)
    timer.start(1000)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    _gc_on_gui_thread(app)
    w = MainWindow()
    w.show()
    if "--demo" in sys.argv:
        w.devices.start_demo()
        w.spectrum.demo()
    return app.exec()
