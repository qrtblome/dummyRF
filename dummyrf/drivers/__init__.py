from __future__ import annotations

from ..profiles import ALL_MODELS
from .base import DeviceState, Driver, DriverError
from .sennheiser import SennheiserG4Driver
from .shure import ShureDriver

DRIVERS: dict[str, type[Driver]] = {
    "shure": ShureDriver,
    "sennheiser_g4": SennheiserG4Driver,
}


def make_driver(family: str, host: str, port: int, model: str | None = None) -> Driver:
    """``model`` None/"auto" lets Shure devices report their own model."""
    profile = ALL_MODELS.get(model) if model and model != "auto" else None
    return DRIVERS[family](host, port, profile)


__all__ = ["Driver", "DriverError", "DeviceState", "make_driver", "DRIVERS",
           "ShureDriver", "SennheiserG4Driver"]
