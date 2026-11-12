"""Spectrum tab: scans, carriers, exclusions and intermod products."""
from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6.QtWidgets import (QCheckBox, QFileDialog, QHBoxLayout, QLabel, QMessageBox,
                               QPushButton, QVBoxLayout, QWidget)

from ..coordination import imd_products, mhz
from ..scan import load_scan, synthetic_scan
from .context import AppContext

SCAN_COLORS = ["#4aa3df", "#9b59b6", "#16a085", "#d35400", "#7f8c8d"]
CARRIER_COLORS = {"digital_mic": "#2ecc71", "analog_iem": "#f1c40f", "analog_mic": "#e67e22",
                  "strict": "#ecf0f1"}


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
        self.plot.scene().sigMouseMoved.connect(self._mouse)

        lay = QVBoxLayout(self)
        lay.addLayout(bar)
        lay.addWidget(self.plot)

        for sig in (ctx.inventory_changed, ctx.exclusions_changed, ctx.scans_changed,
                    ctx.settings_changed):
            sig.connect(self.redraw)
        self.redraw()

    # ------------------------------------------------------------ actions
    def load(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Load scan", "",
                                                "Scan files (*.csv *.txt *.sdb *.dat);;All files (*)")
        errors = []
        for p in paths:
            try:
                self.ctx.scans.append(load_scan(p))
                if p not in self.ctx.show_data.scan_paths:
                    self.ctx.show_data.scan_paths.append(p)
            except (OSError, ValueError) as e:
                errors.append(str(e))
        if errors:
            QMessageBox.warning(self, "Load scan", "\n".join(errors))
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
            pi.plot(s.freqs_khz / 1000, s.levels_dbm, name=s.name,
                    pen=pg.mkPen(SCAN_COLORS[i % len(SCAN_COLORS)], width=1))

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

        for n, s in enumerate(sorted(slots, key=lambda s: s.freq_khz)):
            color = CARRIER_COLORS.get(s.profile, "#ffffff")
            line = pg.InfiniteLine(s.freq_khz / 1000, angle=90, movable=False,
                                   pen=pg.mkPen(color, width=1.5),
                                   label=s.label if self.show_labels.isChecked() else None,
                                   labelOpts={"position": 0.92 - 0.12 * (n % 4), "color": color, "rotateAxis": (1, 0),
                                              "anchors": [(0, 0.5), (0, 0.5)]})
            line.setToolTip(f"{s.label}\n{mhz(s.freq_khz)} MHz")
            pi.addItem(line)
