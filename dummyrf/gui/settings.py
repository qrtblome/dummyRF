"""Per-user app settings (window size, scanner offsets, WWB folder …).

Everything goes through ``app_settings()`` so tests can redirect it: with the
environment variable DUMMYRF_SETTINGS set to a file path, an INI file is used
instead of the user's real settings.  (QSettings.setDefaultFormat does not
affect QSettings(organization, application), so it can't be used for that.)
"""
from __future__ import annotations

import os

from PySide6.QtCore import QSettings


def app_settings() -> QSettings:
    path = os.environ.get("DUMMYRF_SETTINGS")
    if path:
        return QSettings(path, QSettings.IniFormat)
    return QSettings("dummyRF", "dummyRF")
