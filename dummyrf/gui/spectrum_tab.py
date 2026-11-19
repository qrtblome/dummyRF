"""Spectrum tab: scans, carriers, exclusions and intermod products."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QSettings
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QCheckBox, QFileDialog, QHBoxLayout, QLabel, QMenu,
                               QPushButton, QVBoxLayout, QWidget)

from ..coordination import SPACING_PROFILES, imd_products, mhz
from ..scan import load_scan, synthetic_scan
from . import dialogs
from .context import AppContext
from .markers import ChannelMarkers
from .wwb_watch import DEFAULT_FOLDER, WwbScanWatcher

SCAN_COLORS = ["#4aa3df", "#9b59b6", "#16a085", "#d35400", "#7f8c8d"]
FILL_FLOOR_DBM = -130   # scan fills reach down to here


class SpectrumTab(QWidget):
    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        pg.setConfigOptions(antialias=True)

        bar = QHBoxLayout()
        for text, fn in [("Load scan…", self.load), ("Demo scan", self.demo),
                         ("Clear scans", self.clear)]:
            b = QPushButton(text)
            b.clicked.connect(fn)
            bar.addWidget(b)
        self.show_imd = QCheckBox("Show IMD products")
        self.show_imd.setChecked(True)
        self.show_imd.toggled.connect(self.redraw)
        self.show_labels = QCheckBox("Labels")
        self.show_labels.setChecked(True)
        self.show_labels.toggled.connect(self.redraw)
        bar.addWidget(self.show_imd)
        bar.addWidget(self.show_labels)
        # ---- receiver scans made in Wireless Workbench
        self.wwb = WwbScanWatcher(parent=self)
        self.wwb.new_scan.connect(self._wwb_scan)
        self.wwb.failed.connect(lambda msg: self.info.setText(f"WWB scan not loaded: {msg}"))
        cfg = QSettings("dummyRF", "dummyRF")
        self.wwb.folder = Path(cfg.value("wwb/folder", str(DEFAULT_FOLDER)))
        self.wwb_auto = QCheckBox("Auto-load WWB scans")
        self.wwb_auto.setToolTip(f"Load every scan Wireless Workbench saves in\n{self.wwb.folder}\n"
                                 "(scans made with Shure receivers)")
        self.wwb_auto.toggled.connect(self._wwb_toggled)
        wwb_more = QPushButton("WWB…")
        wwb_more.setToolTip("Choose WWB's scan folder, or load the latest WWB scan now")
        wwb_more.clicked.connect(self._wwb_menu)
        bar.addWidget(self.wwb_auto)
        bar.addWidget(wwb_more)
        bar.addStretch()
        self.info = QLabel()
        bar.addWidget(self.info)

        self.plot = pg.PlotWidget()
        self.plot.setLabel("bottom", "Frequency", units="MHz")
        self.plot.setLabel("left", "Level", units="dBm")
        self.plot.showGrid(x=True, y=True, alpha=0.2)
        self.plot.setYRange(-120, -20)
        self.plot.addLegend(offset=(-10, 10))
        self.threshold = pg.InfiniteLine(angle=0, movable=True, pen=pg.mkPen("#e74c3c", dash=[6, 4]),
                                         label="threshold {value:.0f} dBm",
                                         labelOpts={"position": 0.95, "color": "#e74c3c"})
        self.threshold.sigPositionChangeFinished.connect(self._threshold_moved)
        self.markers = ChannelMarkers(self.plot.plotItem)
        self.plot.scene().sigMouseMoved.connect(self._mouse)

        from .scanner_panel import ScannerPanel
        self.scanner = ScannerPanel(ctx, self)

        lay = QVBoxLayout(self)
        lay.addLayout(bar)
        lay.addWidget(self.scanner)
        lay.addWidget(self.plot)

        for sig in (ctx.inventory_changed, ctx.exclusions_changed, ctx.scans_changed,
                    ctx.settings_changed):
            sig.connect(self.redraw)
        self.redraw()
        if QSettings("dummyRF", "dummyRF").value("wwb/auto", False, type=bool):
            self.wwb_auto.setChecked(True)

    # ------------------------------------------------------------ actions
    def load(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Load scan", "",
                                                "Scan files (*.sdb3 *.csv *.txt *.sdb *.dat);;All files (*)")
        errors = []
        for p in paths:
            try:
                self.ctx.scans.append(load_scan(p))
                if p not in self.ctx.show_data.scan_paths:
                    self.ctx.show_data.scan_paths.append(p)
            except (OSError, ValueError) as e:
                errors.append(str(e))
        if errors:
            dialogs.warning(self, "Load scan", "\n".join(errors))
        self.ctx.scans_changed.emit()
        self._fit()

    def demo(self):
        self.ctx.scans.append(synthetic_scan(seed=len(self.ctx.scans) + 1))
        self.ctx.scans_changed.emit()
        self._fit()

    def clear(self):
        self.ctx.scans.clear()
        self.ctx.show_data.scan_paths.clear()
        self.ctx.scans_changed.emit()

    # ------------------------------------------------------------ WWB scans
    def _wwb_toggled(self, on: bool):
        QSettings("dummyRF", "dummyRF").setValue("wwb/auto", on)
        if on:
            if not self.wwb.folder.is_dir():
                self.info.setText(f"WWB folder not found: {self.wwb.folder}")
            self.wwb.start()
            self.info.setText(f"Waiting for new WWB scans in {self.wwb.folder.name}…")
        else:
            self.wwb.stop()

    def _wwb_scan(self, scan):
        self.ctx.scans.append(scan)
        if scan.path and scan.path not in self.ctx.show_data.scan_paths:
            self.ctx.show_data.scan_paths.append(scan.path)
        self.ctx.scans_changed.emit()
        self._fit()
        self.info.setText(f"Loaded WWB scan: {scan.name}")

    def _wwb_menu(self):
        menu = QMenu(self)
        menu.addAction("Load latest WWB scan now", self._wwb_latest)
        menu.addAction(f"Scan folder: {self.wwb.folder} …", self._wwb_folder)
        menu.exec(self.cursor().pos())

    def _wwb_latest(self):
        scan = self.wwb.load_latest()
        if scan is None:
            dialogs.information(self, "WWB scans", f"No .sdb3 scans found in\n{self.wwb.folder}")
            return
        self._wwb_scan(scan)

    def _wwb_folder(self):
        d = QFileDialog.getExistingDirectory(self, "WWB scan folder", str(self.wwb.folder))
        if d:
            self.wwb.folder = Path(d)
            QSettings("dummyRF", "dummyRF").setValue("wwb/folder", d)
            self.wwb_auto.setToolTip(f"Load every scan Wireless Workbench saves in\n{d}")
            if self.wwb.active:
                self.wwb.start()

    def _fit(self):
        xs = [s.freqs_khz for s in self.ctx.scans] + \
             [np.array([s.freq_khz for s in self.ctx.show_data.inventory if s.freq_khz])]
        xs = [x for x in xs if len(x)]
        if xs:
            lo = min(x.min() for x in xs) / 1000
            hi = max(x.max() for x in xs) / 1000
            self.plot.setXRange(lo - 1, hi + 1)

    def _threshold_moved(self):
        st = self.ctx.show_data.settings
        st.threshold_dbm = round(self.threshold.value(), 1)
        self.ctx.settings_changed.emit()

    def _mouse(self, pos):
        if self.plot.sceneBoundingRect().contains(pos):
            p = self.plot.plotItem.vb.mapSceneToView(pos)
            self.info.setText(f"{p.x():.3f} MHz   {p.y():.1f} dBm")

    # ------------------------------------------------------------ drawing
    def redraw(self, *_):
        pi = self.plot.plotItem
        pi.clear()
        if pi.legend:
            pi.legend.clear()
        sd = self.ctx.show_data

        for e in sd.exclusions:
            r = pg.LinearRegionItem((e.lo_khz / 1000, e.hi_khz / 1000), movable=False,
                                    brush=pg.mkBrush(192, 57, 43, 45), pen=pg.mkPen(None))
            r.setZValue(-20)
            pi.addItem(r)

        for i, s in enumerate(self.ctx.scans):
            c = QColor(SCAN_COLORS[i % len(SCAN_COLORS)])
            fill = QColor(c)
            fill.setAlpha(55)
            # filled down to the bottom of the plot so overlapping scans stay readable
            pi.plot(s.freqs_khz / 1000, s.levels_dbm, name=s.name, pen=pg.mkPen(c, width=1.2),
                    fillLevel=FILL_FLOOR_DBM, brush=pg.mkBrush(fill))

        self.scanner.attach(pi)

        if sd.settings.use_scan and self.ctx.scans:
            self.threshold.setValue(sd.settings.threshold_dbm)
            pi.addItem(self.threshold)

        slots = [s for s in sd.inventory if s.freq_khz]
        freqs = [s.freq_khz for s in slots]
        if self.show_imd.isChecked() and 1 < len(freqs) <= 120:
            prods = imd_products(freqs, fifth=sd.settings.fifth_order)
            for order, ntx, color, y in ((3, 2, "#e74c3c", -118), (3, 3, "#e67e22", -116),
                                         (5, 2, "#8e44ad", -114)):
                xs = [p / 1000 for p, o, src in prods if o == order and len(set(src)) == ntx]
                if xs:
                    pi.plot(xs, [y] * len(xs), pen=None, symbol="t1", symbolSize=6,
                            symbolBrush=color, symbolPen=None,
                            name=f"{ntx}-tx {'3rd' if order == 3 else '5th'}")

        self.markers.set([(s.freq_khz / 1000, s.label or "?", s.profile,
                           f"{s.label}\n{mhz(s.freq_khz)} MHz\n{SPACING_PROFILES.get(s.profile).label if s.profile in SPACING_PROFILES else s.profile}")
                          for s in slots], labels=self.show_labels.isChecked())
