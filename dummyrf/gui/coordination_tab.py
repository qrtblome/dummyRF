"""Coordination tab: frequency inventory, exclusions, plan generation."""
from __future__ import annotations

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox,
                               QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
                               QListWidget, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox,
                               QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ..coordination import (SPACING_PROFILES, CoordinationResult, Exclusion, Slot, check,
                            coordinate, mhz, tv_channel)
from .context import AppContext

COLS = ["Label", "Equipment", "Band low (MHz)", "Band high (MHz)", "Frequency (MHz)", "Fixed",
        "Linked device", "Check"]


class _Worker(QThread):
    finished_result = Signal(object)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def run(self):
        try:
            self.finished_result.emit(self.fn())
        except Exception as e:  # noqa: BLE001
            self.finished_result.emit(e)


class CoordinationTab(QWidget):
    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        self._updating = False
        self._worker: _Worker | None = None
        self.conflicts: dict[int, list[str]] = {}

        # -------------------------------------------------- inventory table
        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.itemChanged.connect(self._item_changed)

        btns = QHBoxLayout()
        for text, fn in [("Add slot", self.add_slot), ("Remove", self.remove_slots),
                         ("Import from devices", self.import_devices),
                         ("Clear unfixed freqs", self.clear_unfixed)]:
            b = QPushButton(text)
            b.clicked.connect(fn)
            btns.addWidget(b)
        btns.addStretch()
        self.check_btn = QPushButton("Check")
        self.coord_btn = QPushButton("Coordinate")
        self.push_btn = QPushButton("Push to devices…")
        self.coord_btn.setDefault(True)
        self.check_btn.clicked.connect(self.run_check)
        self.coord_btn.clicked.connect(self.run_coordinate)
        self.push_btn.clicked.connect(self.push_to_devices)
        for b in (self.check_btn, self.coord_btn, self.push_btn):
            btns.addWidget(b)

        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addLayout(btns)
        ll.addWidget(self.table)

        # -------------------------------------------------- settings
        opts = QGroupBox("Options")
        of = QFormLayout(opts)
        self.fifth = QCheckBox("Avoid 2-tx 5th-order products")
        self.use_scan = QCheckBox("Avoid scan peaks above")
        self.threshold = QDoubleSpinBox()
        self.threshold.setRange(-130, 0)
        self.threshold.setSuffix(" dBm")
        self.time_limit = QDoubleSpinBox()
        self.time_limit.setRange(0.5, 120)
        self.time_limit.setSuffix(" s")
        row = QHBoxLayout()
        row.addWidget(self.use_scan)
        row.addWidget(self.threshold)
        of.addRow(self.fifth)
        of.addRow(row)
        of.addRow("Search time", self.time_limit)
        spacing = QLabel("\n".join(
            f"{p.label}: CC {p.cc_khz}, 2tx3 {p.imd3_khz}, 2tx5 {p.imd5_khz}, "
            f"3tx3 {p.imd3tx_khz} kHz" for p in SPACING_PROFILES.values()))
        spacing.setWordWrap(True)
        spacing.setStyleSheet("color: gray; font-size: 11px;")
        of.addRow(spacing)
        for w in (self.fifth, self.use_scan):
            w.toggled.connect(self._save_settings)
        for w in (self.threshold, self.time_limit):
            w.valueChanged.connect(self._save_settings)

        # -------------------------------------------------- exclusions
        exg = QGroupBox("Exclusions")
        el = QVBoxLayout(exg)
        self.excl_list = QListWidget()
        self.excl_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        el.addWidget(self.excl_list)
        r1 = QHBoxLayout()
        self.ex_lo = QDoubleSpinBox()
        self.ex_hi = QDoubleSpinBox()
        for w in (self.ex_lo, self.ex_hi):
            w.setRange(30, 3000)
            w.setDecimals(3)
            w.setValue(470)
        add_rng = QPushButton("Add range")
        add_rng.clicked.connect(self.add_range)
        r1.addWidget(self.ex_lo)
        r1.addWidget(QLabel("–"))
        r1.addWidget(self.ex_hi)
        r1.addWidget(add_rng)
        r2 = QHBoxLayout()
        self.tv_ch = QSpinBox()
        self.tv_ch.setRange(14, 69)
        self.tv_ch.setValue(21)
        self.tv_region = QComboBox()
        self.tv_region.addItems(["EU", "US"])
        add_tv = QPushButton("Add TV channel")
        add_tv.clicked.connect(self.add_tv)
        rem_ex = QPushButton("Remove")
        rem_ex.clicked.connect(self.remove_exclusions)
        r2.addWidget(self.tv_ch)
        r2.addWidget(self.tv_region)
        r2.addWidget(add_tv)
        r2.addWidget(rem_ex)
        el.addLayout(r1)
        el.addLayout(r2)

        self.results = QPlainTextEdit()
        self.results.setReadOnly(True)

        right = QSplitter(Qt.Vertical)
        right.addWidget(opts)
        right.addWidget(exg)
        right.addWidget(self.results)

        split = QSplitter(Qt.Horizontal)
        split.addWidget(left)
        split.addWidget(right)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        lay = QVBoxLayout(self)
        lay.addWidget(split)

        ctx.inventory_changed.connect(self.refresh_table)
        ctx.exclusions_changed.connect(self.refresh_exclusions)
        ctx.settings_changed.connect(self.load_settings)
        self.load_settings()
        self.refresh_table()
        self.refresh_exclusions()

    # ------------------------------------------------------------ helpers
    @property
    def inv(self) -> list[Slot]:
        return self.ctx.show_data.inventory

    def load_settings(self):
        st = self.ctx.show_data.settings
        self._updating = True
        self.fifth.setChecked(st.fifth_order)
        self.use_scan.setChecked(st.use_scan)
        self.threshold.setValue(st.threshold_dbm)
        self.time_limit.setValue(st.time_limit_s)
        self._updating = False

    def _save_settings(self, *_):
        if self._updating:
            return
        st = self.ctx.show_data.settings
        st.fifth_order = self.fifth.isChecked()
        st.use_scan = self.use_scan.isChecked()
        st.threshold_dbm = self.threshold.value()
        st.time_limit_s = self.time_limit.value()
        self.ctx.settings_changed.emit()

    def refresh_table(self):
        self._updating = True
        self.table.setRowCount(len(self.inv))
        for r, s in enumerate(self.inv):
            def item(text, editable=True):
                it = QTableWidgetItem(text)
                if not editable:
                    it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                return it
            self.table.setItem(r, 0, item(s.label))
            combo = QComboBox()
            for key, p in SPACING_PROFILES.items():
                combo.addItem(p.label, key)
            combo.setCurrentIndex(max(0, combo.findData(s.profile)))
            combo.currentIndexChanged.connect(lambda _i, row=r, c=combo: self._profile_changed(row, c))
            self.table.setCellWidget(r, 1, combo)
            self.table.setItem(r, 2, item(f"{s.lo_khz / 1000:.3f}"))
            self.table.setItem(r, 3, item(f"{s.hi_khz / 1000:.3f}"))
            self.table.setItem(r, 4, item(mhz(s.freq_khz) if s.freq_khz else ""))
            fx = QTableWidgetItem()
            fx.setFlags((fx.flags() | Qt.ItemIsUserCheckable) & ~Qt.ItemIsEditable)
            fx.setCheckState(Qt.Checked if s.fixed else Qt.Unchecked)
            self.table.setItem(r, 5, fx)
            link = ""
            if s.device_uid:
                link = f"{self.ctx.manager.name(s.device_uid)} ch{s.channel}"
            self.table.setItem(r, 6, item(link, False))
            msgs = self.conflicts.get(r)
            chk = item("" if msgs is None else (f"{len(msgs)} problem(s)" if msgs else "OK"), False)
            if msgs is not None:
                chk.setForeground(QColor("#c0392b" if msgs else "#2e9d4f"))
                chk.setToolTip("\n".join(msgs))
            self.table.setItem(r, 7, chk)
        self._updating = False

    def _profile_changed(self, row: int, combo: QComboBox):
        if row < len(self.inv):
            self.inv[row].profile = combo.currentData()
            self.conflicts.clear()
            self.ctx.inventory_changed.emit()

    def _item_changed(self, it: QTableWidgetItem):
        if self._updating:
            return
        r, c = it.row(), it.column()
        s = self.inv[r]
        txt = it.text().strip()
        try:
            if c == 0:
                s.label = txt
            elif c in (2, 3):
                v = int(round(float(txt.replace(",", ".")) * 1000))
                if c == 2:
                    s.lo_khz = v
                else:
                    s.hi_khz = v
            elif c == 4:
                if txt:
                    v = int(round(float(txt.replace(",", ".")) * 1000))
                    if v % 25:
                        raise ValueError("frequency must be on a 25 kHz step")
                    s.freq_khz = v
                else:
                    s.freq_khz = None
                    s.fixed = False
            elif c == 5:
                s.fixed = it.checkState() == Qt.Checked
                if s.fixed and not s.freq_khz:
                    s.fixed = False
                    QMessageBox.information(self, "Fixed", "Enter a frequency before fixing it.")
        except ValueError as e:
            QMessageBox.warning(self, "Invalid value", str(e))
        self.conflicts.clear()
        self.ctx.inventory_changed.emit()

    # ------------------------------------------------------------ slots
    def add_slot(self):
        last = self.inv[-1] if self.inv else None
        self.inv.append(Slot(f"Slot {len(self.inv) + 1}",
                             last.profile if last else "digital_mic",
                             last.lo_khz if last else 470000,
                             last.hi_khz if last else 608000))
        self.ctx.inventory_changed.emit()

    def remove_slots(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            del self.inv[r]
        self.conflicts.clear()
        self.ctx.inventory_changed.emit()

    def clear_unfixed(self):
        for s in self.inv:
            if not s.fixed:
                s.freq_khz = None
        self.conflicts.clear()
        self.ctx.inventory_changed.emit()

    def import_devices(self):
        m = self.ctx.manager
        linked = {(s.device_uid, s.channel): s for s in self.inv if s.device_uid}
        added = updated = 0
        skipped = []
        for uid, e in m.entries.items():
            prof = e.driver.profile
            if not prof or e.status != "online":
                skipped.append(m.name(uid))
                continue
            band = e.state.device.get("band")
            lo, hi = prof.bands.get(band or "", (min(b[0] for b in prof.bands.values()),
                                                 max(b[1] for b in prof.bands.values())))
            for ch in range(1, prof.channels + 1):
                vals = e.state.channels.get(ch, {})
                label = f"{m.name(uid)} {vals.get('name') or f'ch{ch}'}".strip()
                s = linked.get((uid, ch))
                if s:
                    s.lo_khz, s.hi_khz = lo, hi
                    s.freq_khz = vals.get("frequency") or s.freq_khz
                    updated += 1
                else:
                    self.inv.append(Slot(label, prof.coord_profile, lo, hi, vals.get("frequency"),
                                         False, uid, ch))
                    added += 1
        self.conflicts.clear()
        self.ctx.inventory_changed.emit()
        msg = f"Imported from devices: {added} added, {updated} updated."
        if skipped:
            msg += f"\nSkipped (offline / not identified): {', '.join(skipped)}"
        self.results.setPlainText(msg)

    # ------------------------------------------------------------ exclusions
    def refresh_exclusions(self):
        self.excl_list.clear()
        for e in sorted(self.ctx.show_data.exclusions, key=lambda e: e.lo_khz):
            self.excl_list.addItem(f"{mhz(e.lo_khz)} – {mhz(e.hi_khz)} MHz  {e.label}")

    def add_range(self):
        lo, hi = sorted((self.ex_lo.value(), self.ex_hi.value()))
        if hi <= lo:
            return
        self.ctx.show_data.exclusions.append(Exclusion(int(lo * 1000), int(hi * 1000), "manual"))
        self.ctx.exclusions_changed.emit()

    def add_tv(self):
        try:
            e = tv_channel(self.tv_ch.value(), self.tv_region.currentText())
        except ValueError as ex:
            QMessageBox.warning(self, "TV channel", str(ex))
            return
        self.ctx.show_data.exclusions.append(e)
        self.ctx.exclusions_changed.emit()

    def remove_exclusions(self):
        ordered = sorted(self.ctx.show_data.exclusions, key=lambda e: e.lo_khz)
        rows = {self.excl_list.row(i) for i in self.excl_list.selectedItems()}
        self.ctx.show_data.exclusions = [e for i, e in enumerate(ordered) if i not in rows]
        self.ctx.exclusions_changed.emit()

    # ------------------------------------------------------------ run
    def _scan_args(self):
        st = self.ctx.show_data.settings
        scan = self.ctx.combined_scan() if st.use_scan else None
        return scan, (st.threshold_dbm if scan is not None else None)

    def run_check(self):
        scan, thr = self._scan_args()
        conflicts = check(self.inv, self.ctx.show_data.exclusions,
                          self.ctx.show_data.settings.fifth_order, scan, thr)
        self.conflicts = {i: [] for i, s in enumerate(self.inv) if s.freq_khz}
        for c in conflicts:
            self.conflicts.setdefault(c.slot, []).append(f"{c.kind}: {c.detail}")
        self.refresh_table()
        lines = [f"{self.inv[c.slot].label}: {c.kind} — {c.detail}" for c in conflicts]
        n = sum(1 for s in self.inv if s.freq_khz)
        self.results.setPlainText(
            (f"{len(conflicts)} problem(s) among {n} assigned frequencies.\n\n" + "\n".join(lines))
            if conflicts else f"All {n} assigned frequencies are compatible.")

    def run_coordinate(self):
        if not self.inv:
            return
        if self._worker and self._worker.isRunning():
            return
        for s in self.inv:
            if s.hi_khz <= s.lo_khz:
                QMessageBox.warning(self, "Coordinate", f"{s.label}: band low must be below band high.")
                return
        st = self.ctx.show_data.settings
        scan, thr = self._scan_args()
        slots = [Slot(**vars(s)) for s in self.inv]
        excl = list(self.ctx.show_data.exclusions)
        self.coord_btn.setEnabled(False)
        self.results.setPlainText(f"Coordinating {len(slots)} slots (up to {st.time_limit_s:.0f} s)…")
        self._worker = _Worker(lambda: coordinate(slots, excl, scan=scan, threshold_dbm=thr,
                                                  fifth=st.fifth_order, time_limit=st.time_limit_s))
        self._worker.finished_result.connect(self._coordinated)
        self._worker.start()

    def _coordinated(self, res):
        self.coord_btn.setEnabled(True)
        if isinstance(res, Exception):
            QMessageBox.critical(self, "Coordinate", str(res))
            return
        res: CoordinationResult
        for i, f in res.freqs.items():
            if i < len(self.inv):
                self.inv[i].freq_khz = f
        for i in res.unplaced:
            if i < len(self.inv):
                self.inv[i].freq_khz = None
        self.ctx.inventory_changed.emit()
        self.run_check()
        head = (f"Placed {len(res.freqs)} of {len(self.inv)} "
                f"({res.attempts} attempt(s), {res.seconds:.1f} s).")
        if res.unplaced:
            head += ("\nCould not place: " + ", ".join(self.inv[i].label for i in res.unplaced)
                     + "\nTry: wider bands, fewer exclusions, a lower scan threshold or longer search.")
        self.results.setPlainText(head + "\n\n" + self.results.toPlainText())

    def push_to_devices(self):
        m = self.ctx.manager
        changes = []
        for s in self.inv:
            if not (s.device_uid and s.freq_khz):
                continue
            e = m.entries.get(s.device_uid)
            if not e:
                continue
            cur = e.state.channels.get(s.channel, {}).get("frequency")
            if cur != s.freq_khz:
                changes.append((s, cur, e.status))
        if not changes:
            QMessageBox.information(self, "Push", "Linked devices already match the plan.")
            return
        offline = [s.label for s, _, st in changes if st != "online"]
        text = "\n".join(f"{s.label}: {mhz(c) if c else '?'} → {mhz(s.freq_khz)} MHz"
                         for s, c, _ in changes)
        if offline:
            text += "\n\nOffline (will fail): " + ", ".join(offline)
        if QMessageBox.question(self, "Push frequencies",
                                f"Change {len(changes)} frequencies on the devices?\n"
                                f"Live channels will drop out while retuning.\n\n{text}") != QMessageBox.Yes:
            return
        for s, _, _ in changes:
            m.set_param(s.device_uid, s.channel, "frequency", s.freq_khz)
        self.results.setPlainText(f"Sent {len(changes)} frequency changes — results in the Devices log.")
