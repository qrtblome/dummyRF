"""Add coordination slots for equipment from the library (JSON) or the
network-controllable model profiles."""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit,
                               QSpinBox)

from .. import library
from ..coordination import SPACING_PROFILES, Slot
from ..profiles import ALL_MODELS
from .fit import compact_combo, tame_combos


@dataclass
class _Choice:
    label: str
    channels: int
    bands: list[tuple[str, int, int]]
    spacing: str


# Equipment with its own spacing profile (keys from coordination.SPACING_PROFILES).
# Matched against the model's full name, upper-case without spaces or dashes.
MODEL_SPACING = {
    "MIPROACT747": "act747",
    "SHUREBLX": "blx"
}


def _spacing_for(name: str) -> str:
    n = name.upper().replace(" ", "")
    for key, profile in MODEL_SPACING.items():
        if key in n.replace("-", ""):
            return profile
    if any(k in n for k in ("PSM", "P10T", "ADTQ", "IEM")):
        return "analog_iem"
    if any(n.startswith(k) or f"SHURE{k}" in n for k in ("ULXD", "QLXD", "SLXD", "AD4", "AXIENT")):
        return "digital_mic"
    return "analog_mic"


def choices() -> list[_Choice]:
    lib = library.get()
    out = [_Choice(m.full_name, m.channels_per_unit,
                   [(b.name, b.lo_khz, b.hi_khz) for b in m.bands], _spacing_for(m.full_name))
           for m in lib.models]
    for p in ALL_MODELS.values():
        brand = "Shure" if p.family == "shure" else "Sennheiser"
        out.append(_Choice(f"{brand} {p.model} (network)", p.channels,
                           [(k, lo, hi) for k, (lo, hi) in p.bands.items()], p.coord_profile))
    return sorted(out, key=lambda c: c.label.lower())


class AddFromLibraryDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add equipment to inventory")
        self.items = choices()
        form = QFormLayout(self)
        self.model = compact_combo()
        for c in self.items:
            self.model.addItem(f"{c.label}  ({c.channels} ch/unit)", c)
        self.band = compact_combo()
        self.units = QSpinBox()
        self.units.setRange(1, 64)
        self.spacing = compact_combo()
        for key, p in SPACING_PROFILES.items():
            self.spacing.addItem(p.label, key)
        self.prefix = QLineEdit()
        self.summary = QLabel()
        form.addRow("Model", self.model)
        form.addRow("Band", self.band)
        form.addRow("Units", self.units)
        form.addRow("Equipment rules", self.spacing)
        form.addRow("Label prefix", self.prefix)
        form.addRow(self.summary)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)
        self.model.currentIndexChanged.connect(self._model_changed)
        self.units.valueChanged.connect(self._summary)
        self._model_changed()
        tame_combos(self)

    def _model_changed(self):
        c: _Choice = self.model.currentData()
        self.band.clear()
        for name, lo, hi in sorted(c.bands, key=lambda b: b[1]):
            self.band.addItem(f"{name}  {lo / 1000:g}–{hi / 1000:g} MHz", (name, lo, hi))
        self.spacing.setCurrentIndex(max(0, self.spacing.findData(c.spacing)))
        self.prefix.setText(c.label.replace(" (network)", ""))
        self._summary()

    def _summary(self):
        c: _Choice = self.model.currentData()
        self.summary.setText(f"Adds {self.units.value() * c.channels} slot(s).")

    def slots(self, start_index: int = 1) -> list[Slot]:
        c: _Choice = self.model.currentData()
        band = self.band.currentData()
        if not band:
            return []
        name, lo, hi = band
        out = []
        for u in range(1, self.units.value() + 1):
            for ch in range(1, c.channels + 1):
                suffix = f"{u}" if c.channels == 1 else f"{u}.{ch}"
                out.append(Slot(f"{self.prefix.text().strip()} {name} #{suffix}",
                                self.spacing.currentData(), lo, hi))
        return out
