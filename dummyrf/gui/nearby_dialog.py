"""Pick broadcast transmitters around the venue and add them as exclusions."""
from __future__ import annotations

import asyncio
import weakref

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDialog, QDoubleSpinBox,
                               QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QMenu, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout)

from .. import geocode, library, propagation
from ..coordination import Exclusion
from .fit import compact_combo, fit_to_screen, tame_combos
from .site_search import attach_site_search
from .context import AppContext

COLS = ["", "Level", "Verdict", "km", "Site", "Location", "Service", "Ch.", "MHz",
        "Mux / programme", "ERP kW", "Ant. m", "Pol."]
VERDICT_COLOR = {"very": "#e74c3c", "receivable": "#e67e22", "weak": "#c9a400", "negligible": "#888888"}
SERVICE_GROUPS = {"TV": ("DVB-T", "DVB-T2", "DVB-T/T2", "5G Broadcast"), "DAB": ("DAB+",),
                  "FM": ("FM",)}


class NearbyTransmittersDialog(QDialog):
    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.lib = library.get()
        self.setWindowTitle("Nearby transmitters")
        self.rows: list[tuple[float, library.Station, library.Transmitter, float]] = []

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

        # ---- propagation model (saved with the show)
        ms = self.model_settings()
        self.env = compact_combo(16)
        for key, label in propagation.ENVIRONMENTS.items():
            self.env.addItem(label, key)
        self.env.setCurrentIndex(max(0, self.env.findData(ms.environment)))
        self.env.setToolTip("Okumura-Hata path loss for the surroundings between transmitter and "
                            "venue.\nFree space = no obstacles at all (worst case, highest level).")
        self.rx_h = self._spin(1, 30, ms.rx_height_m, " m", 1,
                               "Height of the wireless receivers' antennas above ground")
        self.building = self._spin(0, 40, ms.extra_loss_db, " dB", 0,
                                   "Extra loss for an indoor venue (walls/roof), typically 10–15 dB")
        self.use_pat = QCheckBox("antenna patterns")
        self.use_pat.setChecked(ms.use_patterns)
        self.use_pat.setToolTip("Use the directional antenna patterns from the UKE register "
                                "(off = every transmitter omnidirectional)")
        self.tick_above = self._spin(-130, 0, ms.exclude_above_dbm, " dBm", 0,
                                     "Transmitters at or above this level are ticked for exclusion")
        self.adj_above = self._spin(-130, 0, ms.adjacent_above_dbm, " dBm", 0,
                                    "At or above this level the neighbouring channels are excluded too")
        for w in (self.rx_h, self.building, self.tick_above, self.adj_above):
            w.editingFinished.connect(self._model_changed)
        self.env.currentIndexChanged.connect(self._model_changed)
        self.use_pat.toggled.connect(self._model_changed)
        row_m = QHBoxLayout()
        for w in (self.env, QLabel("rx antenna"), self.rx_h, QLabel("building loss"), self.building,
                  self.use_pat):
            row_m.addWidget(w)
        row_m.addStretch()
        row_t = QHBoxLayout()
        for w in (QLabel("tick at or above"), self.tick_above, QLabel("also neighbours at or above"),
                  self.adj_above):
            row_t.addWidget(w)
        row_t.addStretch()

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
        form.addRow("Model", row_m)
        form.addRow("Exclude", row_t)

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
        credits = QLabel("Levels: Okumura-Hata estimate, no terrain — a guide, not a measurement.  "
                         "Postal codes: GeoNames (geonames.org, CC BY 4.0) · "
                         "online search: © OpenStreetMap contributors")
        credits.setWordWrap(True)
        credits.setStyleSheet("color: gray; font-size: 10px;")
        lay.addWidget(credits)
        if self.pos:
            self._find_transmitters()

    # ------------------------------------------------------------ model
    @staticmethod
    def _spin(lo, hi, value, suffix, decimals, tip):
        w = QDoubleSpinBox()
        w.setRange(lo, hi)
        w.setDecimals(decimals)
        w.setValue(value)
        w.setSuffix(suffix)
        w.setToolTip(tip)
        return w

    def model_settings(self) -> propagation.ModelSettings:
        saved = self.ctx.show_data.settings.propagation or {}
        try:
            return propagation.ModelSettings(**saved)
        except TypeError:
            return propagation.ModelSettings()

    def _model_changed(self, *_):
        ms = propagation.ModelSettings(
            environment=self.env.currentData(), rx_height_m=self.rx_h.value(),
            extra_loss_db=self.building.value(), use_patterns=self.use_pat.isChecked(),
            exclude_above_dbm=self.tick_above.value(), adjacent_above_dbm=self.adj_above.value())
        self.ctx.show_data.settings.propagation = vars(ms).copy()
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
        ms = self.model_settings()
        rows = []
        for d, s in self.lib.nearby(lat, lon, self.radius.value()):
            az = propagation.bearing_deg(s.lat, s.lon, lat, lon)     # transmitter -> venue
            for t in s.transmitters:
                lvl = propagation.received_dbm(t.erp_kw, (t.freq_khz or 0) / 1000 or 100.0, d,
                                               t.h_antenna_m, ms, t.pattern_db, az)
                rows.append((d, s, t, lvl))
        self.rows = sorted(rows, key=lambda r: -r[3])       # strongest first
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
        ms = self.model_settings()
        shown = [r for r in self.rows if self._wanted(r[2])]
        have = {(e.lo_khz, e.hi_khz) for e in self.ctx.show_data.exclusions}
        self.table.setRowCount(len(shown))
        self._shown = shown
        ticked = 0
        for i, (d, s, t, lvl) in enumerate(shown):
            ex = t.exclusion(s.name)
            already = ex is not None and (ex.lo_khz, ex.hi_khz) in have
            chk = QTableWidgetItem("✓" if already else "")
            chk.setFlags(Qt.ItemIsEnabled | (Qt.NoItemFlags if already else Qt.ItemIsUserCheckable))
            if not already:
                on = lvl >= ms.exclude_above_dbm
                ticked += on
                chk.setCheckState(Qt.Checked if on else Qt.Unchecked)
            chk.setToolTip("already an exclusion" if already else "")
            self.table.setItem(i, 0, chk)
            verdict = propagation.verdict(lvl, ms)
            vals = [f"{lvl:.0f} dBm" if lvl > -999 else "—", verdict, f"{d:.1f}", s.name, s.location,
                    t.service, t.channel, f"{t.freq_khz / 1000:.3f}" if t.freq_khz else "", t.label,
                    f"{t.erp_kw:g}" if t.erp_kw else "",
                    f"{t.h_antenna_m:g}" if t.h_antenna_m else "", t.polarization]
            color = QColor(VERDICT_COLOR[verdict.split()[0]])
            for c, v in enumerate(vals, start=1):
                it = QTableWidgetItem(v)
                if c in (1, 3, 8, 10, 11):
                    it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if c in (1, 2):
                    it.setForeground(color)
                self.table.setItem(i, c, it)
            if t.pattern_db and ms.use_patterns:
                self.table.item(i, 1).setToolTip("directional antenna pattern applied")
        sites = len({id(s) for _, s, _, _ in shown})
        self.summary.setText(f"{len(shown)} transmitter(s) at {sites} site(s), {ticked} ticked "
                             f"at ≥ {ms.exclude_above_dbm:g} dBm ({len(self.rows)} within "
                             f"{self.radius.value():g} km).  " + " · ".join(self.lib.site_sources))
        self.add_btn.setEnabled(bool(shown))

    def _check_all(self, on: bool):
        for i in range(self.table.rowCount()):
            it = self.table.item(i, 0)
            if it.flags() & Qt.ItemIsUserCheckable:
                it.setCheckState(Qt.Checked if on else Qt.Unchecked)

    def add_checked(self):
        ms = self.model_settings()
        new: list[Exclusion] = []
        for i, (d, s, t, lvl) in enumerate(self._shown):
            it = self.table.item(i, 0)
            if it.flags() & Qt.ItemIsUserCheckable and it.checkState() == Qt.Checked:
                ex = t.exclusion(s.name)
                if ex:
                    ex.label += f", {d:.0f} km, ~{lvl:.0f} dBm"
                    new.append(ex)
                    if lvl >= ms.adjacent_above_dbm and t.width_khz:
                        w = t.width_khz
                        for lo, side in ((ex.lo_khz - w, "below"), (ex.hi_khz, "above")):
                            new.append(Exclusion(lo, lo + w, f"neighbour {side} {s.name}: "
                                                 f"{t.service} {t.channel} (~{lvl:.0f} dBm)"))
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
