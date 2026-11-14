"""Show file: devices, frequency inventory, exclusions and scans (JSON)."""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .coordination import Exclusion, Slot


@dataclass
class DeviceSpec:
    family: str
    host: str
    port: int
    model: str = "auto"
    label: str = ""
    uid: str = field(default_factory=lambda: uuid.uuid4().hex[:8])


@dataclass
class Settings:
    fifth_order: bool = True
    use_scan: bool = True
    threshold_dbm: float = -80.0
    time_limit_s: float = 5.0
    poll_interval_s: float = 2.0
    venue_lat: float | None = None
    venue_lon: float | None = None
    venue_label: str = ""
    propagation: dict = field(default_factory=dict)   # propagation.ModelSettings as a dict


@dataclass
class Show:
    devices: list[DeviceSpec] = field(default_factory=list)
    inventory: list[Slot] = field(default_factory=list)
    exclusions: list[Exclusion] = field(default_factory=list)
    scan_paths: list[str] = field(default_factory=list)
    settings: Settings = field(default_factory=Settings)

    def to_json(self) -> str:
        return json.dumps({"format": "dummyrf-show", "version": 1, **asdict(self)}, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "Show":
        d = json.loads(text)
        if d.get("format") != "dummyrf-show":
            raise ValueError("not a dummyRF show file")
        return cls(
            devices=[DeviceSpec(**x) for x in d.get("devices", [])],
            inventory=[Slot(**x) for x in d.get("inventory", [])],
            exclusions=[Exclusion(**x) for x in d.get("exclusions", [])],
            scan_paths=list(d.get("scan_paths", [])),
            settings=Settings(**d.get("settings", {})),
        )

    def save(self, path: str | Path) -> None:
        Path(path).write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "Show":
        return cls.from_json(Path(path).read_text(encoding="utf-8"))
