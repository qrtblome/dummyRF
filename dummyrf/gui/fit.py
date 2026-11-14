"""Keep windows and widgets inside the screen.

* ``compact_combo`` / ``tame_combos`` — a QComboBox normally asks for the
  width of its longest entry; with 741 transmitter sites that alone was ~900 px.
  Combos now ask for a modest width and their drop-down list grows to show
  full entries instead.
* ``fit_to_screen`` — size a window from the available screen area instead of
  fixed pixels, and pull a restored geometry back on-screen (e.g. after
  unplugging a bigger monitor).
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QRect
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QComboBox, QSizePolicy,
                               QWidget)


class _WidePopup(QObject):
    """Widen a combo's drop-down list when it opens.

    Setting a minimum width on the list permanently also raises the combo's
    own minimum width (that is what made the window too wide), so the list is
    widened only while it is shown."""

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Show:
            view = obj.findChild(QAbstractItemView) if not isinstance(obj, QAbstractItemView) else obj
            combo = self.parent()
            if view is not None and combo.count():
                screen = (combo.screen() or QApplication.primaryScreen()).availableGeometry()
                wanted = min(view.sizeHintForColumn(0) + 40, int(screen.width() * 0.8))
                popup = view.window()
                if popup.width() < wanted:
                    popup.resize(wanted, popup.height())
                    # keep it on screen
                    if popup.geometry().right() > screen.right():
                        popup.move(max(screen.left(), screen.right() - wanted), popup.y())
        return False


def compact_combo(chars: int = 14, parent: QWidget | None = None) -> QComboBox:
    """A QComboBox that asks for ``chars`` characters of width, not the width of
    its longest entry.  The policy must be set before the combo is first
    measured — Qt never recomputes a combo's minimum width afterwards — so
    every combo in the GUI is created through here."""
    combo = QComboBox(parent)
    combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
    combo.setMinimumContentsLength(chars)
    return combo


def tame_combos(root: QWidget) -> None:
    """Let every combo's drop-down list open wide enough for its entries."""
    for combo in root.findChildren(QComboBox):
        if not combo.property("_wide_popup"):
            combo.view().window().installEventFilter(_WidePopup(combo))
            combo.setProperty("_wide_popup", True)


def elastic(label: QWidget) -> None:
    """Let a status label shrink instead of forcing the window wider."""
    label.setMinimumWidth(1)
    label.setSizePolicy(QSizePolicy.Ignored, label.sizePolicy().verticalPolicy())


def available(widget: QWidget | None = None) -> QRect:
    screen = (widget.screen() if widget is not None else None) or QApplication.primaryScreen()
    return screen.availableGeometry()


def fit_to_screen(win: QWidget, frac_w: float = 0.9, frac_h: float = 0.9,
                  max_w: int | None = None, max_h: int | None = None) -> None:
    """Resize ``win`` to a fraction of the screen (optionally capped) and centre it."""
    area = available(win)
    w = int(area.width() * frac_w)
    h = int(area.height() * frac_h)
    if max_w:
        w = min(w, max_w)
    if max_h:
        h = min(h, max_h)
    w = max(w, min(win.minimumSizeHint().width(), area.width()))
    h = max(h, min(win.minimumSizeHint().height(), area.height()))
    win.resize(w, h)
    win.move(area.x() + (area.width() - w) // 2, area.y() + (area.height() - h) // 2)


def keep_on_screen(win: QWidget) -> None:
    """After restoreGeometry(): shrink / move the window back into the screen."""
    area = available(win)
    g = win.frameGeometry()
    w = min(win.width(), area.width())
    h = min(win.height(), area.height())
    if (w, h) != (win.width(), win.height()):
        win.resize(w, h)
        g = win.frameGeometry()
    x = min(max(g.x(), area.x()), area.right() - g.width() + 1)
    y = min(max(g.y(), area.y()), area.bottom() - g.height() + 1)
    if (x, y) != (g.x(), g.y()):
        win.move(x, y)
