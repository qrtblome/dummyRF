"""Message boxes drawn by Qt instead of native macOS alerts.

On macOS 27 a native NSAlert can crash the whole process while rendering its
icon (CoreUI CUINamedVectorGlyph raises an Objective-C exception inside the
modal loop).  QMessageBox's static helpers use NSAlert, so every message box in
the app goes through here instead.  Return values match QMessageBox's static
functions, so ``question(...) == QMessageBox.Yes`` keeps working.
"""
from __future__ import annotations

from PySide6.QtWidgets import QMessageBox, QWidget


def _box(parent: QWidget | None, icon: QMessageBox.Icon, title: str, text: str,
         buttons: QMessageBox.StandardButton, default: QMessageBox.StandardButton
         ) -> QMessageBox.StandardButton:
    box = QMessageBox(icon, title, text, buttons, parent)
    box.setOption(QMessageBox.Option.DontUseNativeDialog, True)
    box.setDefaultButton(default)
    return QMessageBox.StandardButton(box.exec())


def question(parent, title: str, text: str) -> QMessageBox.StandardButton:
    return _box(parent, QMessageBox.Question, title, text,
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)


def information(parent, title: str, text: str) -> QMessageBox.StandardButton:
    return _box(parent, QMessageBox.Information, title, text, QMessageBox.Ok, QMessageBox.Ok)


def warning(parent, title: str, text: str) -> QMessageBox.StandardButton:
    return _box(parent, QMessageBox.Warning, title, text, QMessageBox.Ok, QMessageBox.Ok)


def critical(parent, title: str, text: str) -> QMessageBox.StandardButton:
    return _box(parent, QMessageBox.Critical, title, text, QMessageBox.Ok, QMessageBox.Ok)
