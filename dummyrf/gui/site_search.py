"""Search-as-you-type for transmitter-site combo boxes.

Qt's own completer matches letters exactly, so typing "poznan" never found
"Poznań".  This one folds diacritics (ą→a, ł→l, …) and case, and matches when
every typed word appears somewhere in the entry: "piat pozn" finds
"Poznań Piątkowo".
"""
from __future__ import annotations

import unicodedata

from PySide6.QtCore import QSortFilterProxyModel, Qt
from PySide6.QtWidgets import QComboBox, QCompleter

_EXTRA = str.maketrans({"ł": "l", "Ł": "l", "ø": "o", "đ": "d", "ß": "ss"})


def fold(text: str) -> str:
    """Lower-case, diacritics removed: 'Łódź Śródmieście' -> 'lodz srodmiescie'."""
    text = text.translate(_EXTRA)
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


def matches(entry: str, query: str) -> bool:
    words = fold(query).split()
    folded = fold(entry)
    return all(w in folded for w in words)


class _FoldFilter(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._words: list[str] = []
        self._folded: dict[int, str] = {}

    def set_query(self, text: str) -> None:
        self._words = fold(text).split()
        self.invalidateFilter()

    def filterAcceptsRow(self, row: int, parent) -> bool:
        if not self._words:
            return True
        if row not in self._folded:
            self._folded[row] = fold(self.sourceModel().index(row, 0, parent).data() or "")
        f = self._folded[row]
        return all(w in f for w in self._words)


def attach_site_search(combo: QComboBox, max_visible: int = 15) -> None:
    """Make an editable, already-filled combo box search its items loosely."""
    combo.setEditable(True)
    combo.setInsertPolicy(QComboBox.NoInsert)
    proxy = _FoldFilter(combo)
    proxy.setSourceModel(combo.model())
    comp = QCompleter(proxy, combo)
    comp.setCompletionMode(QCompleter.UnfilteredPopupCompletion)  # the proxy does the filtering
    comp.setCaseSensitivity(Qt.CaseInsensitive)
    comp.setMaxVisibleItems(max_visible)
    combo.setCompleter(comp)

    def edited(text: str):
        proxy.set_query(text)
        if text.strip():
            comp.complete()

    def chosen(text: str):
        i = combo.findText(text, Qt.MatchExactly)
        if i >= 0:
            combo.setCurrentIndex(i)
            combo.activated.emit(i)

    combo.lineEdit().textEdited.connect(edited)
    comp.activated[str].connect(chosen)
    combo._site_search = (proxy, comp)   # keep Python references alive with the combo
