"""Devices tab: device list, channel editor and raw command console."""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox,
                               QSplitter, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from ..profiles import (BOOL, FAMILIES, FREQ, INT, STR, ModelProfile, Param,
                        models_for_family)
from ..show import DeviceSpec
from . import dialogs
from .context import AppContext

COLS = ["Device / Ch", "Model / Name", "Frequency (MHz)", "Gain", "Mute", "RF power",
        "Battery", "RF (dBm)", "Audio", "Status"]
KEY_COL = {"frequency": 2, "gain": 3, "mute": 4, "rf_power": 5, "battery": 6,
           "rf_level": 7, "audio_level": 8}
STATUS_COLOR = {"online": "#2e9d4f", "offline": "#c0392b", "connecting": "#c98a00"}


def fmt(p: Param | None, v: Any) -> str:
    if p is None or v is None:
        return ""
    if p.kind == FREQ:
        return f"{v / 1000:.3f}"
    if p.kind == BOOL:
        return "ON" if v else "off"
    if p.kind == INT and p.unit and p.unit != "bars":
        return f"{v:+d}" if p.unit == "dB" else str(v)
    return str(v)


class AddDeviceDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add device")
        form = QFormLayout(self)
        self.family = QComboBox()
        for key, (label, _) in FAMILIES.items():
            self.family.addItem(label, key)
        self.model = QComboBox()
        self.host = QLineEdit()
        self.host.setPlaceholderText("192.168.1.100")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.label = QLineEdit()
        self.label.setPlaceholderText("optional, e.g. Stage L rack")
        form.addRow("Family", self.family)
        form.addRow("Model", self.model)
        form.addRow("IP address", self.host)
        form.addRow("Port", self.port)
        form.addRow("Label", self.label)
        self.note = QLabel()
        self.note.setWordWrap(True)
        form.addRow(self.note)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)
        self.family.currentIndexChanged.connect(self._family_changed)
        self._family_changed()

    def _family_changed(self):
        fam = self.family.currentData()
        self.model.clear()
        if fam == "shure":
            self.model.addItem("Auto-detect (asks the device)", "auto")
        for m in models_for_family(fam):
            self.model.addItem(f"{m.model} — {m.description}", m.model)
        self.port.setValue(FAMILIES[fam][1])
        self.note.setText(
            "Sennheiser SR 2050 IEM: add each channel as its own device (its own IP)."
            if fam == "sennheiser_g4" else "")

    def _accept(self):
        if not self.host.text().strip():
            dialogs.warning(self, "Add device", "Enter an IP address.")
            return
        self.accept()

    def spec(self) -> DeviceSpec:
        return DeviceSpec(self.family.currentData(), self.host.text().strip(), self.port.value(),
                          self.model.currentData(), self.label.text().strip())


class ChannelEditor(QGroupBox):
    """Edits the writable parameters of one channel."""

    def __init__(self, ctx: AppContext):
        super().__init__("Channel")
        self.ctx = ctx
        self.uid: str | None = None
        self.channel = 0
        self.profile: ModelProfile | None = None
        self.widgets: dict[str, QWidget] = {}
        self.original: dict[str, Any] = {}
        self.form = QFormLayout()
        lay = QVBoxLayout(self)
        self.info = QLabel("Select a channel to edit it.")
        self.info.setWordWrap(True)
        lay.addWidget(self.info)
        lay.addLayout(self.form)
        row = QHBoxLayout()
        self.apply_btn = QPushButton("Apply")
        self.revert_btn = QPushButton("Revert")
        self.apply_btn.clicked.connect(self.apply)
        self.revert_btn.clicked.connect(self.load_values)
        row.addStretch()
        row.addWidget(self.revert_btn)
        row.addWidget(self.apply_btn)
        lay.addLayout(row)
        lay.addStretch()
        self._set_enabled(False)

    def _set_enabled(self, on: bool):
        self.apply_btn.setEnabled(on)
        self.revert_btn.setEnabled(on)

    def clear(self):
        while self.form.rowCount():
            self.form.removeRow(0)
        self.widgets.clear()
        self.uid = None
        self.info.setText("Select a channel to edit it.")
        self._set_enabled(False)

    def edit(self, uid: str, channel: int):
        entry = self.ctx.manager.entries.get(uid)
        if not entry or not entry.driver.profile:
            self.clear()
            return
        if (uid, channel) == (self.uid, self.channel):
            return
        self.clear()
        self.uid, self.channel, self.profile = uid, channel, entry.driver.profile
        self.setTitle(f"{self.ctx.manager.name(uid)} — channel {channel}")
        band = entry.state.device.get("band")
        lo, hi = self.profile.bands.get(band or "", (0, 0))
        for p in self.profile.params:
            if not p.writable:
                continue
            if p.kind == STR:
                w = QLineEdit()
                if p.width:
                    w.setMaxLength(p.width)
            elif p.kind == FREQ:
                w = QDoubleSpinBox()
                w.setDecimals(3)
                w.setSingleStep(0.025)
                w.setSuffix(" MHz")
                if hi:
                    w.setRange(lo / 1000, hi / 1000)
                else:
                    w.setRange(0, 3000)
            elif p.kind == INT:
                w = QSpinBox()
                w.setRange(p.lo if p.lo is not None else -1000, p.hi if p.hi is not None else 1000)
                if p.unit:
                    w.setSuffix(f" {p.unit}")
            elif p.kind == BOOL:
                w = QCheckBox()
            else:
                w = QComboBox()
                w.addItems(list(p.choices))
            self.widgets[p.key] = w
            self.form.addRow(p.label, w)
        self.info.setText(f"{self.profile.description}" + (f" · band {band} "
                          f"({lo / 1000:.0f}–{hi / 1000:.0f} MHz)" if hi else ""))
        self.load_values()
        self._set_enabled(True)

    def load_values(self):
        entry = self.ctx.manager.entries.get(self.uid or "")
        if not entry:
            return
        vals = entry.state.channels.get(self.channel, {})
        self.original = {}
        for key, w in self.widgets.items():
            v = vals.get(key)
            self.original[key] = v
            if v is None:
                continue
            if isinstance(w, QLineEdit):
                w.setText(str(v))
            elif isinstance(w, QDoubleSpinBox):
                w.setValue(v / 1000)
            elif isinstance(w, QSpinBox):
                w.setValue(int(v))
            elif isinstance(w, QCheckBox):
                w.setChecked(bool(v))
            elif isinstance(w, QComboBox):
                w.setCurrentText(str(v))

    def _value(self, key: str):
        w = self.widgets[key]
        if isinstance(w, QLineEdit):
            return w.text()
        if isinstance(w, QDoubleSpinBox):
            return int(round(w.value() * 1000))
        if isinstance(w, QSpinBox):
            return w.value()
        if isinstance(w, QCheckBox):
            return w.isChecked()
        return w.currentText()

    def apply(self):
        if not self.uid:
            return
        changed = {k: self._value(k) for k in self.widgets if self._value(k) != self.original.get(k)}
        if "frequency" in changed and changed["frequency"] % 25:
            dialogs.warning(self, "Frequency", "Frequency must be on a 25 kHz step.")
            return
        for key, val in changed.items():
            self.ctx.manager.set_param(self.uid, self.channel, key, val)
            self.original[key] = val


class DevicesTab(QWidget):
    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        self.items: dict[str, QTreeWidgetItem] = {}
        m = ctx.manager

        bar = QHBoxLayout()
        add = QPushButton("Add device…")
        scan = QPushButton("Scan network…")
        rem = QPushButton("Remove")
        demo = QPushButton("Start demo rack (simulators)")
        add.clicked.connect(self.add_device)
        scan.clicked.connect(self.scan_network)
        rem.clicked.connect(self.remove_selected)
        demo.clicked.connect(self.start_demo)
        bar.addWidget(add)
        bar.addWidget(scan)
        bar.addWidget(rem)
        bar.addStretch()
        bar.addWidget(demo)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(COLS))
        self.tree.setHeaderLabels(COLS)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.header().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.tree.header().setStretchLastSection(True)
        self.tree.itemSelectionChanged.connect(self._selection_changed)

        self.editor = ChannelEditor(ctx)

        console = QGroupBox("Raw command console (selected device)")
        cl = QVBoxLayout(console)
        row = QHBoxLayout()
        self.raw_input = QLineEdit()
        self.raw_input.setPlaceholderText("Shure: GET 1 ALL   ·   Sennheiser: Frequency")
        self.raw_input.returnPressed.connect(self.send_raw)
        send = QPushButton("Send")
        send.clicked.connect(self.send_raw)
        row.addWidget(self.raw_input)
        row.addWidget(send)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        self.log.setFont(QFont("Menlo", 11))
        cl.addLayout(row)
        cl.addWidget(self.log)

        right = QSplitter(Qt.Vertical)
        right.addWidget(self.editor)
        right.addWidget(console)
        split = QSplitter(Qt.Horizontal)
        split.addWidget(self.tree)
        split.addWidget(right)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)

        lay = QVBoxLayout(self)
        lay.addLayout(bar)
        lay.addWidget(split)

        m.device_changed.connect(self.update_device)
        m.devices_changed.connect(self.rebuild)
        m.log.connect(self.append_log)

    # ------------------------------------------------------------- list
    def rebuild(self):
        current = set(self.ctx.manager.entries)
        for uid in list(self.items):
            if uid not in current:
                idx = self.tree.indexOfTopLevelItem(self.items.pop(uid))
                self.tree.takeTopLevelItem(idx)
        for uid in current:
            if uid not in self.items:
                it = QTreeWidgetItem(self.tree)
                it.setData(0, Qt.UserRole, (uid, 0))
                it.setExpanded(True)
                f = it.font(0)
                f.setBold(True)
                it.setFont(0, f)
                self.items[uid] = it
            self.update_device(uid)

    def update_device(self, uid: str):
        e = self.ctx.manager.entries.get(uid)
        it = self.items.get(uid)
        if not e or not it:
            return
        prof = e.driver.profile
        it.setText(0, self.ctx.manager.name(uid))
        band = e.state.device.get("band") or ""
        it.setText(1, f"{prof.model if prof else e.spec.model}{'  ' + band if band else ''}")
        it.setText(9, f"{e.status}  {e.spec.host}:{e.spec.port}")
        it.setForeground(9, QColor(STATUS_COLOR.get(e.status, "#888")))
        if not prof:
            return
        nch = prof.channels
        while it.childCount() < nch:
            c = QTreeWidgetItem(it)
            c.setData(0, Qt.UserRole, (uid, it.childCount()))
        it.setExpanded(True)
        stale = e.status != "online"
        for ch in range(1, nch + 1):
            c = it.child(ch - 1)
            vals = e.state.channels.get(ch, {})
            c.setText(0, f"Ch {ch}")
            c.setText(1, str(vals.get("name") or ""))
            for key, col in KEY_COL.items():
                c.setText(col, fmt(prof.param(key), vals.get(key)))
                c.setForeground(col, QColor("#999") if stale else self.tree.palette().text().color())

    def _selected(self) -> tuple[str, int] | None:
        it = self.tree.currentItem()
        return it.data(0, Qt.UserRole) if it else None

    def _selection_changed(self):
        sel = self._selected()
        if sel and sel[1]:
            self.editor.edit(*sel)

    # ----------------------------------------------------------- actions
    def add_device(self):
        dlg = AddDeviceDialog(self)
        if dlg.exec() == QDialog.Accepted:
            spec = dlg.spec()
            self.ctx.show_data.devices.append(spec)
            self.ctx.manager.add_device(spec)

    def scan_network(self):
        from .scan_dialog import ScanDialog
        ScanDialog(self.ctx, self).exec()

    def remove_selected(self):
        sel = self._selected()
        if not sel:
            return
        uid = sel[0]
        if dialogs.question(self, "Remove device",
                                f"Remove {self.ctx.manager.name(uid)} from the show?") != QMessageBox.Yes:
            return
        self.ctx.show_data.devices = [d for d in self.ctx.show_data.devices if d.uid != uid]
        if self.editor.uid == uid:
            self.editor.clear()
        self.ctx.manager.remove_device(uid)

    def start_demo(self):
        self.ctx.manager.start_simulators(self._demo_started)

    def _demo_started(self, rack, err):
        if err:
            dialogs.warning(self, "Demo rack", f"Could not start simulators:\n{err}")
            return
        have = {(d.host, d.port) for d in self.ctx.show_data.devices}
        for s in rack:
            if ("127.0.0.1", s.port) in have:
                continue
            fam = "sennheiser_g4" if s.model.startswith("SR") else "shure"
            spec = DeviceSpec(fam, "127.0.0.1", s.port, "auto" if fam == "shure" else s.model, s.label)
            self.ctx.show_data.devices.append(spec)
            self.ctx.manager.add_device(spec)
        self.append_log("Demo rack started on 127.0.0.1 (simulated devices).")

    def send_raw(self):
        sel = self._selected()
        cmd = self.raw_input.text().strip()
        if not sel or not cmd:
            return
        uid = sel[0]
        self.append_log(f"→ {self.ctx.manager.name(uid)}: {cmd}")

        def done(lines, err):
            text = f"  ! {err}" if err else ("\n".join(f"  ← {ln}" for ln in lines) or "  (no reply)")
            self.append_log(text)
        self.ctx.manager.raw(uid, cmd, done)

    def append_log(self, text: str):
        self.log.appendPlainText(text)
