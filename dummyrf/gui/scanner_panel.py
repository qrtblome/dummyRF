"""Live RF scanner controls + live / peak-hold traces for the Spectrum tab."""
from __future__ import annotations

import asyncio
import concurrent.futures
import datetime
import time
import weakref

import numpy as np
import pyqtgraph as pg
from PySide6.QtWidgets import (QComboBox, QDoubleSpinBox, QFileDialog, QGroupBox, QHBoxLayout,
                               QLabel, QPushButton, QSpinBox, QVBoxLayout)

from .. import rfscan
from ..scan import Scan, write_csv, write_sdb3
from . import dialogs
from .context import AppContext

LIVE_PEN = pg.mkPen("#7fb3ff", width=1)
PEAK_PEN = pg.mkPen("#ff6b6b", width=1.5)


class ScannerPanel(QGroupBox):
    MAX_FPS = 8

    def __init__(self, ctx: AppContext, parent=None):
        super().__init__("RF scanner", parent)
        self.ctx = ctx
        self.scanner: rfscan.Scanner | None = None
        self._future = None
        self.peak = rfscan.PeakHold()
        self.live: tuple[np.ndarray, np.ndarray] | None = None
        self._last_draw = 0.0
        self._started_at: datetime.datetime | None = None
        self._coord_scan: Scan | None = None   # the scan we handed to coordination

        self.live_curve = pg.PlotDataItem(pen=LIVE_PEN, name="live")
        self.peak_curve = pg.PlotDataItem(pen=PEAK_PEN, name="peak hold")

        self.backend = QComboBox()
        self.backend.addItem("RTL-SDR (rtl_power)", "rtl")
        self.backend.addItem("Simulated scanner", "sim")
        self.start_mhz = self._mhz_box(470.0)
        self.stop_mhz = self._mhz_box(700.0)
        self.step = QComboBox()
        for k in (25, 50, 100, 200):
            self.step.addItem(f"{k} kHz", k)
        self.gain = QDoubleSpinBox()
        self.gain.setRange(-1, 50)
        self.gain.setDecimals(1)
        self.gain.setSpecialValueText("auto")
        self.gain.setValue(-1)
        self.gain.setSuffix(" dB")
        self.gain.setToolTip("RTL-SDR tuner gain; 'auto' lets the dongle choose")
        self.offset = QDoubleSpinBox()
        self.offset.setRange(-100, 100)
        self.offset.setDecimals(1)
        self.offset.setSuffix(" dB")
        self.offset.setToolTip("Added to every level. rtl_power reports uncalibrated dB — "
                               "set this once against a known signal to read roughly dBm.")
        self.ppm = QSpinBox()
        self.ppm.setRange(-200, 200)
        self.ppm.setSuffix(" ppm")
        self.ppm.setToolTip("Frequency correction of the dongle's crystal")
        from_inv = QPushButton("From inventory")
        from_inv.setToolTip("Set the range to the bands used in the Coordination tab")
        from_inv.clicked.connect(self.range_from_inventory)

        self.run_btn = QPushButton("Start")
        self.run_btn.clicked.connect(self.toggle)
        reset = QPushButton("Reset peak")
        reset.clicked.connect(self.reset_peak)
        self.use_btn = QPushButton("Use peak hold for coordination")
        self.use_btn.clicked.connect(self.use_for_coordination)
        self.save_btn = QPushButton("Save scan…")
        self.save_btn.clicked.connect(self.save)
        self.status = QLabel("Idle.")

        r1 = QHBoxLayout()
        for w in (self.backend, QLabel("from"), self.start_mhz, QLabel("to"), self.stop_mhz,
                  from_inv, QLabel("resolution"), self.step, QLabel("gain"), self.gain,
                  QLabel("offset"), self.offset, self.ppm):
            r1.addWidget(w)
        r1.addStretch()
        r2 = QHBoxLayout()
        for w in (self.run_btn, reset, self.use_btn, self.save_btn):
            r2.addWidget(w)
        r2.addWidget(self.status, 1)
        lay = QVBoxLayout(self)
        lay.addLayout(r1)
        lay.addLayout(r2)
        self._update_buttons()

    @staticmethod
    def _mhz_box(v: float) -> QDoubleSpinBox:
        b = QDoubleSpinBox()
        b.setRange(rfscan.RTL_MIN_KHZ / 1000, rfscan.RTL_MAX_KHZ / 1000)
        b.setDecimals(3)
        b.setSuffix(" MHz")
        b.setValue(v)
        return b

    # ------------------------------------------------------------ plot hookup
    def attach(self, plot_item) -> None:
        """Re-add our traces after the Spectrum tab cleared the plot."""
        for c in (self.peak_curve, self.live_curve):
            if c.scene() is None:
                plot_item.addItem(c)   # named items join the legend by themselves

    # ------------------------------------------------------------ actions
    def range_from_inventory(self):
        inv = self.ctx.show_data.inventory
        if not inv:
            dialogs.information(self, "RF scanner", "The Coordination tab has no slots yet.")
            return
        self.start_mhz.setValue(min(s.lo_khz for s in inv) / 1000)
        self.stop_mhz.setValue(max(s.hi_khz for s in inv) / 1000)

    def settings(self) -> rfscan.ScanSettings:
        return rfscan.ScanSettings(
            start_khz=int(round(self.start_mhz.value() * 1000)),
            stop_khz=int(round(self.stop_mhz.value() * 1000)),
            step_khz=self.step.currentData(),
            gain_db=None if self.gain.value() < 0 else self.gain.value(),
            level_offset_db=self.offset.value(),
            ppm=self.ppm.value())

    def toggle(self):
        if self.scanner and self.scanner.running:
            self.stop()
        else:
            self.start()

    def start(self):
        st = self.settings()
        try:
            st.validate()
        except ValueError as e:
            dialogs.warning(self, "RF scanner", str(e))
            return
        kind = self.backend.currentData()
        if kind == "rtl" and not rfscan.find_rtl_power():
            dialogs.warning(self, "RF scanner",
                            "rtl_power was not found.\n\nInstall the RTL-SDR tools with:\n"
                            "    brew install librtlsdr\n\nthen plug in the dongle and try again.")
            return
        m = self.ctx.manager
        me = weakref.ref(self)   # the scanner runs on the I/O thread; never hold the panel strongly

        def on_sweep(f, v):
            m.call_in_gui(lambda: (p := me()) and p._sweep(f, v))

        def on_status(text):
            m.call_in_gui(lambda: (p := me()) and p.status.setText(text))

        def done(_res, err):
            if (p := me()) is not None:
                p._finished(err)

        self.reset_peak()
        self.scanner = rfscan.BACKENDS[kind](st, on_sweep, on_status)
        self.scanner.running = True
        self._started_at = datetime.datetime.now()
        self._future = m.submit(self.scanner.run(), done)
        self.status.setText("Starting…")
        self._update_buttons()

    def stop(self):
        if self.scanner:
            self.ctx.manager.submit(self.scanner.stop())
        self.status.setText("Stopping…")

    def _finished(self, err):
        self._future = None
        if err is None or isinstance(err, (asyncio.CancelledError, concurrent.futures.CancelledError)):
            self.status.setText(f"Stopped after {self.peak.sweeps} sweep(s).")
        else:
            msg = str(err) or type(err).__name__
            self.status.setText(f"Stopped: {msg.splitlines()[0]}")
            dialogs.warning(self, "RF scanner", f"The scan stopped:\n\n{msg}")
        self._update_buttons()

    def _sweep(self, f: np.ndarray, v: np.ndarray):
        self.live = (f, v)
        self.peak.add(f, v)
        now = time.monotonic()
        if now - self._last_draw >= 1 / self.MAX_FPS:
            self._last_draw = now
            self.live_curve.setData(f / 1000, v)
            self.peak_curve.setData(self.peak.freqs / 1000, self.peak.levels)
        if self.scanner and self.scanner.running:
            self.status.setText(f"Scanning — {self.peak.sweeps} sweep(s), "
                                f"peak {np.nanmax(self.peak.levels):.1f} dB")
        self._update_buttons()

    def reset_peak(self):
        self.peak.reset()
        self.live = None
        self.live_curve.setData([], [])
        self.peak_curve.setData([], [])
        self._update_buttons()

    def _update_buttons(self):
        running = bool(self.scanner and self.scanner.running)
        self.run_btn.setText("Stop" if running else "Start")
        have = self.peak.freqs is not None and len(self.peak.freqs) > 0
        self.use_btn.setEnabled(have)
        self.save_btn.setEnabled(have)
        for w in (self.backend, self.start_mhz, self.stop_mhz, self.step, self.gain, self.ppm):
            w.setEnabled(not running)

    def _peak_scan(self) -> Scan:
        src = "RTL-SDR" if self.backend.currentData() == "rtl" else "Simulated"
        when = (self._started_at or datetime.datetime.now()).strftime("%Y-%m-%d %H:%M")
        return Scan(f"{src} peak hold {when}", self.peak.freqs.copy(), self.peak.levels.copy())

    def use_for_coordination(self):
        scan = self._peak_scan()
        if self._coord_scan is not None and self._coord_scan in self.ctx.scans:
            self.ctx.scans.remove(self._coord_scan)   # replace our previous one
        self._coord_scan = scan
        self.ctx.scans.append(scan)
        self.ctx.show_data.settings.use_scan = True
        self.ctx.scans_changed.emit()
        self.ctx.settings_changed.emit()
        self.status.setText(f"Peak hold ({self.peak.sweeps} sweeps) is now a scan for coordination.")

    def save(self):
        scan = self._peak_scan()
        default = scan.name.replace(":", "-").replace(" ", "_") + ".sdb3"
        path, flt = QFileDialog.getSaveFileName(self, "Save scan", default,
                                                "WWB scan (*.sdb3);;CSV (*.csv)")
        if not path:
            return
        try:
            if path.lower().endswith(".csv") or "CSV" in flt and not path.lower().endswith(".sdb3"):
                write_csv(scan, path if path.lower().endswith(".csv") else path + ".csv")
            else:
                src = "RTL-SDR" if self.backend.currentData() == "rtl" else "dummyRF"
                write_sdb3(scan, path, step_khz=self.step.currentData(), scanner=src,
                           when=self._started_at)
            self.status.setText(f"Saved {path}")
        except (OSError, ValueError) as e:
            dialogs.critical(self, "Save scan", str(e))

    def shutdown(self):
        if self.scanner and self.scanner.running:
            try:
                self.ctx.manager.submit(self.scanner.stop()).result(3)
            except Exception:  # noqa: BLE001
                pass
