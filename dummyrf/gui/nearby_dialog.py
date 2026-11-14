"""Pick broadcast transmitters around the venue and add them as exclusions."""
from __future__ import annotations

import asyncio
import weakref

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDialog, QDoubleSpinBox,
                               QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QMenu, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout)

from .. import geocode, library
from ..coordination import Exclusion
from .fit import compact_combo, fit_to_screen, tame_combos
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
        self.rows: list[tuple[float, library.Station, library.Transmitter]] = []

        self.venue = QLineEdit()
        self.venue.setPlaceholderText("postal code (60-685), town (Poznań) or latitude, longitude")
        self.venue.setToolTip("Offline: postal code, town or coordinates.\n"
                              "Street addresses and venue names: use 'Search online'.")
        self.venue.returnPressed.connect(self.search)
        self.online_btn = QPushButton("Search online")
        self.online_btn.setToolTip("Look up an address or venue name on OpenStreetMap "
                                   "(sends what you typed to nominatim.openstreetmap.org)")
        self.online_btn.clicked.connect(self.search_online)
        self.where = QLabel("")
        self.where.setWordWrap(True)
        self.where.setStyleSheet("color: gray;")
        self.pos: tuple[float, float] | None = None
        st = ctx.show_data.settings
        if st.venue_lat is not None and st.venue_lon is not None:
            self.pos = (st.venue_lat, st.venue_lon)
            self.venue.setText(st.venue_label or f"{st.venue_lat:.5f}, {st.venue_lon:.5f}")
            self._show_where(st.venue_label or "saved venue", *self.pos)
        self.site_pick = compact_combo(30)
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
        self.radius.editingFinished.connect(self._find_transmitters)

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
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)   # macOS default keeps fields narrow
        row_v = QHBoxLayout()
        row_v.addWidget(self.venue, 1)
        row_v.addWidget(self.online_btn)
        form.addRow("Venue", row_v)
        form.addRow("", self.where)
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
        self.summary.setWordWrap(True)
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
        tame_combos(self)
        fit_to_screen(self, 0.75, 0.75, max_w=1200, max_h=760)
        credits = QLabel("Postal codes: GeoNames (geonames.org, CC BY 4.0) · "
                         "online search: © OpenStreetMap contributors")
        credits.setStyleSheet("color: gray; font-size: 10px;")
        lay.addWidget(credits)
        if self.pos:
            self._find_transmitters()

    # ------------------------------------------------------------ search
    def _site_chosen(self, idx: int):
        pos = self.site_pick.itemData(idx)
        if pos:
            name = self.site_pick.itemText(idx).split(" — ")[0]
            self.venue.setText(name)
            self._use(geocode.Place(f"transmitter site {name}", pos[0], pos[1], "site"))

    def _show_where(self, label: str, lat: float, lon: float):
        self.where.setText(f"→ {label}   ({lat:.4f}, {lon:.4f})")

    def _use(self, place: geocode.Place):
        self.pos = (place.lat, place.lon)
        st = self.ctx.show_data.settings
        st.venue_lat, st.venue_lon = place.lat, place.lon
        st.venue_label = self.venue.text().strip()
        self._show_where(place.label, place.lat, place.lon)
        self._find_transmitters()

    def _choose(self, places: list[geocode.Place], what: str):
        """One result: use it.  Several: let the user pick from a menu."""
        if not places:
            hint = "" if what == "online" else "  Try 'Search online' for street addresses."
            self.where.setText(f"✗ Nothing found for “{self.venue.text().strip()}”.{hint}")
            return
        if len(places) == 1:
            self._use(places[0])
            return
        menu = QMenu(self)
        for pl in places[:20]:
            menu.addAction(str(pl), lambda pl=pl: self._use(pl))
        self.where.setText(f"{len(places)} matches — pick one")
        menu.exec(self.venue.mapToGlobal(self.venue.rect().bottomLeft()))

    def search(self):
        text = self.venue.text().strip()
        if not text:
            return
        if self.pos and text == (self.ctx.show_data.settings.venue_label or ""):
            self._find_transmitters()        # unchanged venue: just re-run (radius etc.)
            return
        self._choose(geocode.lookup(text), "offline")

    def search_online(self):
        text = self.venue.text().strip()
        if not text:
            return
        self.online_btn.setEnabled(False)
        self.where.setText("Searching OpenStreetMap…")
        me = weakref.ref(self)

        async def run():
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, geocode.search_online, text)

        def done(places, err):
            d = me()
            if d is None:
                return
            d.online_btn.setEnabled(True)
            if err is not None:
                d.where.setText(f"✗ Online search failed: {err}")
            else:
                d._choose(places, "online")
        self.ctx.manager.submit(run(), done)

    def _find_transmitters(self):
        if not self.pos:
            return
        lat, lon = self.pos
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
