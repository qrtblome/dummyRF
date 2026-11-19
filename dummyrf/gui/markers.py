"""Channel markers for the spectrum plot: a coloured line per carrier and a
readable "pill" label (name + frequency) just right of it, stacked into rows so
labels never overlap — re-laid out whenever the view is zoomed or resized.
"""
from __future__ import annotations

import zlib

import pyqtgraph as pg
from PySide6.QtCore import QRectF, QTimer
from PySide6.QtGui import QColor, QPainterPath, QPen

# one colour per spacing profile (= equipment type); unknown keys get a stable colour
EQUIPMENT_COLORS = {
    # digital mics: greens / teals / blues
    "digital_mic": "#2ecc71", "ulxd_norm": "#27c48f", "ulxd_high_density": "#1abc9c",
    "qlxd_norm": "#3fd0c9", "slxd_norm": "#5dade2", "axient_norm": "#48c9b0",
    "axient_high_density": "#76d7c4",
    # in-ear monitors: yellows / oranges
    "analog_iem": "#f1c40f", "ew_iem": "#f5b041", "sr_iem": "#f39c12",
    # analog mics: pinks / purples
    "analog_mic": "#e67e22", "blx": "#ec7fb3", "act747": "#bb8fce",
    "strict": "#ecf0f1",
}
_FALLBACK = ["#e74c3c", "#a569bd", "#5499c7", "#52be80", "#f4d03f", "#eb984e", "#85929e"]


def equipment_color(profile: str) -> str:
    return EQUIPMENT_COLORS.get(profile) or _FALLBACK[zlib.crc32(profile.encode()) % len(_FALLBACK)]


class PillLabel(pg.TextItem):
    """TextItem with a rounded, filled, outlined background."""

    RADIUS = 7

    def __init__(self, html: str, border: str, fill=(18, 18, 22, 215)):
        super().__init__(html=html, anchor=(0, 0.5))
        self._border = QPen(QColor(border), 1.5)
        self._fill = pg.mkBrush(*fill)

    def paint(self, p, *args):
        super().paint(p, *args)        # keeps the item's screen transform up to date
        r: QRectF = self.textItem.mapRectToParent(self.textItem.boundingRect())
        path = QPainterPath()
        path.addRoundedRect(r, self.RADIUS, self.RADIUS)
        p.setRenderHint(p.RenderHint.Antialiasing, True)
        p.setPen(self._border)
        p.setBrush(self._fill)
        p.drawPath(path)

    def width_px(self) -> float:
        return self.textItem.boundingRect().width()

    def height_px(self) -> float:
        return self.textItem.boundingRect().height()


class ChannelMarkers:
    """Owns the lines and labels on one PlotItem."""

    GAP_PX = 6          # space between line and label
    ROW_GAP_PX = 4
    TOP_PX = 6
    LINE_WIDTH = 2.5

    def __init__(self, plot_item: pg.PlotItem):
        self.pi = plot_item
        self.items: list[tuple[float, pg.InfiniteLine, PillLabel | None]] = []
        self._timer = QTimer()
        self._timer.setSingleShot(True)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self.layout)
        vb = plot_item.getViewBox()
        vb.sigRangeChanged.connect(lambda *_: self._timer.start())
        vb.sigResized.connect(lambda *_: self._timer.start())

    def set(self, carriers: list[tuple[float, str, str, str]], labels: bool = True) -> None:
        """carriers: (freq_mhz, name, profile, tooltip). Call after the plot was cleared."""
        self.items = []
        for f, name, profile, tip in sorted(carriers):
            color = equipment_color(profile)
            line = pg.InfiniteLine(f, angle=90, movable=False,
                                   pen=pg.mkPen(color, width=self.LINE_WIDTH))
            line.setToolTip(tip)
            line.setZValue(5)
            self.pi.addItem(line)
            label = None
            if labels:
                label = PillLabel(
                    f"<div style='color:#f2f2f2; font-size:11px; padding:1px 3px'>"
                    f"<b>{_esc(name)}</b><br><span style='color:{color}'>{f:.3f}</span></div>",
                    border=color)
                label.setZValue(10)
                label.setToolTip(tip)
                self.pi.addItem(label, ignoreBounds=True)
            self.items.append((f, line, label))
        self.layout()

    def layout(self) -> None:
        """Place labels right of their lines, in rows from the top, without overlap."""
        vb = self.pi.getViewBox()
        (x0, x1), (y0, y1) = vb.viewRange()
        w_px, h_px = max(vb.width(), 1), max(vb.height(), 1)
        dx, dy = (x1 - x0) / w_px, (y1 - y0) / h_px          # data units per pixel
        row_ends: list[float] = []                            # right edge of the last label per row
        for f, _line, label in self.items:
            if label is None:
                continue
            w = label.width_px()
            label.setAnchor((-self.GAP_PX / max(w, 1), 0.5))   # a few pixels right of the line
            left = f + self.GAP_PX * dx
            right = left + (w + self.GAP_PX) * dx
            row = next((i for i, end in enumerate(row_ends) if end < left), None)
            if row is None:
                row = len(row_ends)
                row_ends.append(right)
            else:
                row_ends[row] = right
            h = label.height_px() + self.ROW_GAP_PX
            y = y1 - (self.TOP_PX + row * h + h / 2) * dy
            label.setPos(f, y)


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
