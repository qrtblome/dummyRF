"""Shared state between the GUI tabs."""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from ..manager import DeviceManager
from ..scan import Scan, combine_max, normalize
from ..show import Show


class AppContext(QObject):
    inventory_changed = Signal()
    exclusions_changed = Signal()
    scans_changed = Signal()
    settings_changed = Signal()

    def __init__(self, manager: DeviceManager):
        super().__init__()
        self.manager = manager
        self.show_data = Show()
        self.scans: list[Scan] = []

    def display_scans(self) -> list[tuple[Scan, float]]:
        """Scans as shown and used for coordination: normalised when that's on.
        Returns (scan, shift dB)."""
        if self.show_data.settings.normalize_scans and len(self.scans) > 1:
            return normalize(self.scans)
        return [(s, 0.0) for s in self.scans]

    def combined_scan(self):
        return combine_max([s for s, _ in self.display_scans()])

    def notify_all(self):
        self.inventory_changed.emit()
        self.exclusions_changed.emit()
        self.scans_changed.emit()
        self.settings_changed.emit()
