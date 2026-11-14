"""Text folding for loose search: case and diacritics ignored."""
from __future__ import annotations

import unicodedata

_EXTRA = str.maketrans({"ł": "l", "Ł": "l", "ø": "o", "đ": "d", "ß": "ss"})


def fold(text: str) -> str:
    """Lower-case, diacritics removed: 'Łódź Śródmieście' -> 'lodz srodmiescie'."""
    text = text.translate(_EXTRA)
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


def matches(entry: str, query: str) -> bool:
    """Every word of ``query`` occurs in ``entry`` (folded, any order)."""
    words = fold(query).split()
    folded = fold(entry)
    return all(w in folded for w in words)
