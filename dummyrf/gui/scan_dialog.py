"""Network scan dialog: pick an adapter, sweep its subnet, add what answers."""
from __future__ import annotations

import weakref

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDialog, QFormLayout, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QProgressBar, QPushButton,
                               QTableWidget, QTableWidgetItem, QVBoxLayout)

from .. import discovery
from ..show import DeviceSpec
from . import dialogs
from .fit import compact_combo, fit_to_screen, tame_combos
from .context import AppContext

FAMILY_LABEL = {"shure": "Shure", "sennheiser_g4": "Sennheiser G4"}
COLS = ["", "Family", "Model", "IP address", "Port", "Name / ID", "Band", "Status"]


class ScanDialog(QDialog):
    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.setWindowTitle("Scan network for devices")
        self._future = None
        self.found: list[discovery.Found] = []

        self.iface = compact_combo(30)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.load_interfaces)
        row_if = QHBoxLayout()
        row_if.addWidget(self.iface, 1)
        row_if.addWidget(refresh)

        self.start = QLineEdit()
        self.end = QLineEdit()
        row_rng = QHBoxLayout()
        row_rng.addWidget(self.start)
        row_rng.addWidget(QLabel("to"))
        row_rng.addWidget(self.end)

        self.use_shure = QCheckBox("Shure (TCP)")
        self.shure_ports = QLineEdit("2202")
        self.use_senn = QCheckBox("Sennheiser G4 (UDP)")
        self.senn_ports = QLineEdit("53212")
        for cb in (self.use_shure, self.use_senn):
            cb.setChecked(True)
        row_sh = QHBoxLayout()
        row_sh.addWidget(self.use_shure)
        row_sh.addWidget(QLabel("ports"))
        row_sh.addWidget(self.shure_ports, 1)
        row_se = QHBoxLayout()
        row_se.addWidget(self.use_senn)
        row_se.addWidget(QLabel("ports"))
        row_se.addWidget(self.senn_ports, 1)

        form = QFormLayout()
        form.addRow("Network adapter", row_if)
        form.addRow("Address range", row_rng)
        form.addRow(row_sh)
        form.addRow(row_se)

        self.scan_btn = QPushButton("Scan")
        self.scan_btn.setDefault(True)
        self.scan_btn.clicked.connect(self.toggle_scan)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.status = QLabel("")
        row_run = QHBoxLayout()
        row_run.addWidget(self.scan_btn)
        row_run.addWidget(self.progress, 1)
        row_run.addWidget(self.status)

        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)

        self.add_btn = QPushButton("Add selected to show")
        self.add_btn.setEnabled(False)
        self.add_btn.clicked.connect(self.add_selected)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        row_btn = QHBoxLayout()
        hint = QLabel("macOS may ask to allow “Local Network” access the first time — allow it, "
                      "or nothing will be found.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray; font-size: 11px;")
        row_btn.addWidget(hint, 1)
        row_btn.addWidget(self.add_btn)
        row_btn.addWidget(close)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addLayout(row_run)
        lay.addWidget(self.table, 1)
        lay.addLayout(row_btn)

        self.iface.currentIndexChanged.connect(self._iface_changed)
        self.load_interfaces()
        tame_combos(self)
        fit_to_screen(self, 0.6, 0.65, max_w=900, max_h=640)

    # ------------------------------------------------------------ setup
    def load_interfaces(self):
        try:
            ifaces = discovery.list_interfaces()
        except Exception as e:  # noqa: BLE001
            dialogs.critical(self, "Network adapters", f"Could not list adapters:\n{e}")
            ifaces = []
        self.iface.blockSignals(True)
        self.iface.clear()
        for i in ifaces:
            self.iface.addItem(str(i), i)
        self.iface.blockSignals(False)
        self._iface_changed()

    def _iface_changed(self):
        iface: discovery.Interface | None = self.iface.currentData()
        if not iface:
            return
        a, b = discovery.default_range(iface)
        self.start.setText(a)
        self.end.setText(b)
        if iface.is_loopback:
            # the demo rack listens on these ports on 127.0.0.1
            self.shure_ports.setText("2202, 22020-22025")
            self.senn_ports.setText("53212-53213")
        else:
            self.shure_ports.setText("2202")
            self.senn_ports.setText("53212")

    # ------------------------------------------------------------ scanning
    def toggle_scan(self):
        if self._future and not self._future.done():
            self._future.cancel()
            return
        try:
            hosts = discovery.host_range(self.start.text(), self.end.text())
            sp = discovery.parse_ports(self.shure_ports.text()) if self.use_shure.isChecked() else []
            np_ = discovery.parse_ports(self.senn_ports.text()) if self.use_senn.isChecked() else []
        except ValueError as e:
            dialogs.warning(self, "Scan", str(e))
            return
        if not (sp or np_):
            dialogs.warning(self, "Scan", "Tick at least one device family.")
            return
        self.table.setRowCount(0)
        self.found = []
        self.add_btn.setEnabled(False)
        self.scan_btn.setText("Stop")
        self.progress.setRange(0, 0)
        self.status.setText(f"Scanning {len(hosts)} address(es)…")
        m = self.ctx.manager

        # The scan runs on the I/O thread: refer to this dialog only weakly from
        # there, so no reference cycle can keep it alive and get garbage-collected
        # on the wrong thread (that crashed PySide).
        me = weakref.ref(self)

        def progress(done, total, phase):
            def update():
                if (d := me()) is not None:
                    d._progress(done, total, phase)
            m.call_in_gui(update)

        def finished(found, err):
            if (d := me()) is not None:
                d._finished(found, err)

        self._future = m.submit(discovery.scan(hosts, sp, np_, progress), finished)

    def _progress(self, done: int, total: int, phase: str):
        self.progress.setRange(0, total)
        self.progress.setValue(done)
        self.status.setText(f"{phase}: {done}/{total}")

    def _finished(self, found, err):
        self._future = None
        self.scan_btn.setText("Scan")
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        if err is not None:
            import concurrent.futures
            if isinstance(err, (concurrent.futures.CancelledError,)):
                self.status.setText("Stopped.")
            else:
                self.status.setText("Failed.")
                dialogs.critical(self, "Scan", f"Scan failed:\n{err}")
            return
        self.found = found
        self.status.setText(f"Found {len(found)} device(s).")
        self._fill()

    def _fill(self):
        have = {(d.host, d.port) for d in self.ctx.show_data.devices}
        self.table.setRowCount(len(self.found))
        for r, f in enumerate(self.found):
            in_show = (f.host, f.port) in have
            chk = QTableWidgetItem()
            if f.supported and not in_show:
                chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
                chk.setCheckState(Qt.Checked)
            else:
                chk.setFlags(Qt.ItemIsEnabled)
            self.table.setItem(r, 0, chk)
            vals = [FAMILY_LABEL.get(f.family, f.family), f.model, f.host, str(f.port), f.name, f.band]
            for c, v in enumerate(vals, start=1):
                self.table.setItem(r, c, QTableWidgetItem(v))
            if in_show:
                st, color = "already in show (connects automatically)", "#888"
            elif not f.supported:
                st, color = "model not supported yet", "#c0392b"
            else:
                st, color = "new", "#2e9d4f"
            it = QTableWidgetItem(st)
            it.setForeground(QColor(color))
            self.table.setItem(r, 7, it)
        self.add_btn.setEnabled(any(self.table.item(r, 0).flags() & Qt.ItemIsUserCheckable
                                    for r in range(self.table.rowCount())))

    def add_selected(self):
        added = 0
        for r, f in enumerate(self.found):
            it = self.table.item(r, 0)
            if not (it.flags() & Qt.ItemIsUserCheckable) or it.checkState() != Qt.Checked:
                continue
            # Shure reports a device ID; a G4 "Name" is the channel name, not the unit's
            label = f.name if f.family == "shure" else ""
            spec = DeviceSpec(f.family, f.host, f.port, f.model, label)
            self.ctx.show_data.devices.append(spec)
            self.ctx.manager.add_device(spec)
            added += 1
        self._fill()
        self.status.setText(f"Added {added} device(s).")

    def reject(self):
        if self._future and not self._future.done():
            self._future.cancel()
        super().reject()
