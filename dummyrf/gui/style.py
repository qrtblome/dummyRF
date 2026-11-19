"""Button colours.

    primary(button)   blue  — the main action of a row (e.g. Import from devices)
    danger(button)    red   — removes something

On macOS a coloured button is drawn by Qt instead of as a native button, so
these get their own shape (rounded, padded) to sit well next to native ones.
To colour another button, call one of these on it after creating it.
"""
from __future__ import annotations

from PySide6.QtWidgets import QPushButton

_BASE = """
QPushButton {{
    background-color: {bg}; color: white; border: 1px solid {border};
    border-radius: 6px; padding: 3px 12px; font-weight: 600;
}}
QPushButton:hover {{ background-color: {hover}; }}
QPushButton:pressed {{ background-color: {pressed}; }}
QPushButton:disabled {{ background-color: #7a7a7a; border-color: #6a6a6a; color: #d0d0d0; }}
"""

PRIMARY = dict(bg="#2d7ff9", border="#1f6ad6", hover="#4a92fb", pressed="#1f6ad6")
DANGER = dict(bg="#d9453d", border="#b8372f", hover="#e5625b", pressed="#b8372f")


def primary(button: QPushButton) -> QPushButton:
    button.setStyleSheet(_BASE.format(**PRIMARY))
    return button


def danger(button: QPushButton) -> QPushButton:
    button.setStyleSheet(_BASE.format(**DANGER))
    return button
