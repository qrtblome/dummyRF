"""Pick broadcast transmitters around the venue and add them as exclusions."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog,
                               QDoubleSpinBox, QFormLayout, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QPushButton, QTableWidget, QTableWidgetItem,
                               QVBoxLayout)

from .. import library
from ..coordination import Exclusion
from . import dialogs
from .site_search import attach_site_search
from .context import AppContext

COLS = ["", "km", "Site", "Location", "Region", "Service", "Ch.", "MHz", "Mux / programme",
        "ERP kW", "Pol."]
SERVICE_GROUPS = {"TV": ("DVB-T", "DVB-T2", "DVB-T/T2", "5G Broadcast"), "DAB": ("DAB+",),
                  "FM": ("FM",)}


class NearbyTransmittersDialog(QDialog):
    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.lib = library.get()
        self.setWindowTitle("Nearby transmitters")
        self.resize(1100, 640)
        self.rows: list[tuple[float, library.Station, library.Transmitter]] = []

        self.venue = QLineEdit()
        self.venue.setPlaceholderText("latitude, longitude — e.g. 52.4064, 16.9252 (copy from a map)")
        st = ctx.show_data.settings
        if st.venue_lat is not None and st.venue_lon is not None:
            self.venue.setText(f"{st.venue_lat:.5f}, {st.venue_lon:.5f}")
        self.site_pick = QComboBox()
        for s in sorted(self.lib.sites, key=lambda s: s.name):
            self.site_pick.addItem(f"{s.name} — {s.location} ({s.region.title()})", (s.lat, s.lon))
        attach_site_search(self.site_pick)
        self.site_pick.setCurrentIndex(-1)
        self.site_pick.lineEdit().setPlaceholderText("…or start typing a transmitter site / town")
        self.site_pick.activated.connect(self._site_chosen)

        self.radius = QDoubleSpinBox()
        self.radius.setRange(1, 500)
        self.radius.setValue(40)
        self.radius.setSuffix(" km")
        self.min_erp = QDoubleSpinBox()
        self.min_erp.setRange(0, 1000)
        self.min_erp.setDecimals(2)
        self.min_erp.setSuffix(" kW")
        self.svc = {k: QCheckBox(k) for k in SERVICE_GROUPS}
        self.svc["TV"].setChecked(True)
        self.svc["DAB"].setChecked(True)
        self.in_band = QCheckBox("only frequencies inside the inventory's bands")
        self.in_band.setChecked(bool(ctx.show_data.inventory))
        self.in_band.setEnabled(bool(ctx.show_data.inventory))

        find = QPushButton("Find")
        find.setDefault(True)
        find.clicked.connect(self.search)

        row_svc = QHBoxLayout()
        for cb in self.svc.values():
            row_svc.addWidget(cb)
            cb.toggled.connect(self._refilter)
        row_svc.addWidget(self.in_band)
        row_svc.addStretch()
        self.in_band.toggled.connect(self._refilter)
        row_r = QHBoxLayout()
        row_r.addWidget(self.radius)
        row_r.addWidget(QLabel("min. ERP"))
        row_r.addWidget(self.min_erp)
        row_r.addStretch()
        row_r.addWidget(find)
        form = QFormLayout()
        form.addRow("Venue", self.venue)
        form.addRow("", self.site_pick)
        form.addRow("Radius", row_r)
        form.addRow("Services", row_svc)

        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.setSortingEnabled(False)

        self.summary = QLabel(" · ".join(self.lib.site_sources) or "No transmitter list loaded.")
        self.summary.setStyleSheet("color: gray;")
        all_btn = QPushButton("Check all")
        none_btn = QPushButton("Check none")
        all_btn.clicked.connect(lambda: self._check_all(True))
        none_btn.clicked.connect(lambda: self._check_all(False))
        self.add_btn = QPushButton("Add checked as exclusions")
        self.add_btn.clicked.connect(self.add_checked)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        row_b = QHBoxLayout()
        row_b.addWidget(self.summary, 1)
        for b in (all_btn, none_btn, self.add_btn, close):
            row_b.addWidget(b)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(self.table, 1)
        lay.addLayout(row_b)
        if self.venue.text():
            self.search()

    # ------------------------------------------------------------ search
    def _site_chosen(self, idx: int):
        pos = self.site_pick.itemData(idx)
        if pos:
            self.venue.setText(f"{pos[0]:.5f}, {pos[1]:.5f}")
            self.search()

    def search(self):
        try:
            lat, lon = library.parse_coords(self.venue.text())
        except ValueError as e:
            dialogs.warning(self, "Nearby transmitters", str(e))
            return
        st = self.ctx.show_data.settings
        st.venue_lat, st.venue_lon = lat, lon
        self.rows = [(d, s, t) for d, s in self.lib.nearby(lat, lon, self.radius.value())
                     for t in s.transmitters]
        self._refilter()

    def _wanted(self, t: library.Transmitter) -> bool:
        groups = [g for g, cb in self.svc.items() if cb.isChecked()]
        if not any(t.service in SERVICE_GROUPS[g] for g in groups):
            return False
        if t.erp_kw < self.min_erp.value():
            return False
        if self.in_band.isChecked() and t.freq_khz and t.width_khz:
            lo, hi = t.freq_khz - t.width_khz // 2, t.freq_khz + t.width_khz // 2
            if not any(lo < s.hi_khz and hi > s.lo_khz for s in self.ctx.show_data.inventory):
                return False
        return True

    def _refilter(self, *_):
        shown = [r for r in self.rows if self._wanted(r[2])]
        have = {(e.lo_khz, e.hi_khz) for e in self.ctx.show_data.exclusions}
        self.table.setRowCount(len(shown))
        self._shown = shown
        for i, (d, s, t) in enumerate(shown):
            ex = t.exclusion(s.name)
            already = ex is not None and (ex.lo_khz, ex.hi_khz) in have
            chk = QTableWidgetItem("✓" if already else "")
            chk.setFlags(Qt.ItemIsEnabled | (Qt.NoItemFlags if already else Qt.ItemIsUserCheckable))
            if not already:
                chk.setCheckState(Qt.Checked)
            chk.setToolTip("already an exclusion" if already else "")
            self.table.setItem(i, 0, chk)
            vals = [f"{d:.1f}", s.name, s.location, s.region.title(), t.service, t.channel,
                    f"{t.freq_khz / 1000:.3f}" if t.freq_khz else "", t.label,
                    f"{t.erp_kw:g}" if t.erp_kw else "", t.polarization]
            for c, v in enumerate(vals, start=1):
                it = QTableWidgetItem(v)
                if c in (1, 7, 9):
                    it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(i, c, it)
        sites = len({id(s) for _, s, _ in shown})
        self.summary.setText(f"{len(shown)} transmitter(s) at {sites} site(s) shown "
                             f"({len(self.rows)} within {self.radius.value():g} km).  "
                             + " · ".join(self.lib.site_sources))
        self.add_btn.setEnabled(bool(shown))

    def _check_all(self, on: bool):
        for i in range(self.table.rowCount()):
            it = self.table.item(i, 0)
            if it.flags() & Qt.ItemIsUserCheckable:
                it.setCheckState(Qt.Checked if on else Qt.Unchecked)

    def add_checked(self):
        new: list[Exclusion] = []
        for i, (d, s, t) in enumerate(self._shown):
            it = self.table.item(i, 0)
            if it.flags() & Qt.ItemIsUserCheckable and it.checkState() == Qt.Checked:
                ex = t.exclusion(s.name)
                if ex:
                    ex.label += f", {d:.0f} km"
                    new.append(ex)
        have = {(e.lo_khz, e.hi_khz) for e in self.ctx.show_data.exclusions}
        added = []
        for e in new:
            if (e.lo_khz, e.hi_khz) not in have:
                have.add((e.lo_khz, e.hi_khz))
                added.append(e)
        self.ctx.show_data.exclusions += added
        self.ctx.exclusions_changed.emit()
        self._refilter()
        self.summary.setText(f"Added {len(added)} exclusion(s); {len(new) - len(added)} "
                             f"were already covered by the same channel.")
