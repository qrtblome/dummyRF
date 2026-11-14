"""Equipment and spectrum library loaded from JSON.

Files (same format in both places; the user folder wins on name clashes):

* ``dummyrf/data/``     — shipped with the app
* ``~/.dummyrf/``       — your own additions / corrections

``devices.json``
    ``band_sets``: {set name: [{name, low_khz, high_khz}]}
    ``brands``:    [{name, models: [{name, channels_per_unit, band_set,
                    half_width_khz, audio_bandwidth_khz}]}]
``channel_plans.json``
    {plan key: {type, width_khz, channels: {channel: centre_khz}}}
``stations.json``
    [{name, lat, lon, transmitters: [{plan, channel, erp_kw}]}]

Band sets also override the hard-coded bands of network-controllable models
(``profiles.py``) when the library model name prefixes the profile model, e.g.
library "SLXD" -> profiles SLXD4 and SLXD4D.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .coordination import Exclusion

PACKAGE_DIR = Path(__file__).parent / "data"
USER_DIR = Path.home() / ".dummyrf"


@dataclass
class Band:
    name: str
    lo_khz: int
    hi_khz: int

    def __str__(self) -> str:
        return f"{self.name}  {self.lo_khz / 1000:g}–{self.hi_khz / 1000:g} MHz"


@dataclass
class EquipmentModel:
    brand: str
    name: str
    channels_per_unit: int
    bands: list[Band]
    half_width_khz: int = 0
    audio_bandwidth_khz: int = 0

    @property
    def full_name(self) -> str:
        return f"{self.brand} {self.name}"


@dataclass
class ChannelPlan:
    key: str
    type: str
    width_khz: int
    channels: dict[str, int]  # channel -> centre kHz

    def exclusion(self, channel: str, label: str = "") -> Exclusion:
        c = self.channels[channel]
        half = self.width_khz // 2
        return Exclusion(c - half, c + half, label or f"{self.type} {channel}")


@dataclass
class Transmitter:
    plan: str
    channel: str
    erp_kw: float = 0.0


@dataclass
class Station:
    name: str
    lat: float
    lon: float
    transmitters: list[Transmitter] = field(default_factory=list)


@dataclass
class Library:
    band_sets: dict[str, list[Band]] = field(default_factory=dict)
    models: list[EquipmentModel] = field(default_factory=list)
    plans: dict[str, ChannelPlan] = field(default_factory=dict)
    stations: list[Station] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def model(self, full_name: str) -> EquipmentModel | None:
        return next((m for m in self.models if m.full_name == full_name), None)

    def station_exclusions(self, station: Station) -> list[Exclusion]:
        out = []
        for t in station.transmitters:
            plan = self.plans.get(t.plan)
            if plan and t.channel in plan.channels:
                out.append(plan.exclusion(t.channel, f"{station.name}: {plan.type} {t.channel}"
                                          + (f" ({t.erp_kw:g} kW)" if t.erp_kw else "")))
            else:
                self.errors.append(f"{station.name}: unknown plan/channel {t.plan} {t.channel}")
        return out


# ------------------------------------------------------------------ loading
def _read(path: Path, errors: list[str]):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        errors.append(f"{path}: {e}")
        return None


def _merge_devices(lib: Library, data: dict, src: Path) -> None:
    for set_name, bands in (data.get("band_sets") or {}).items():
        try:
            lib.band_sets[set_name] = [Band(b["name"], int(b["low_khz"]), int(b["high_khz"]))
                                       for b in bands]
        except (KeyError, TypeError, ValueError) as e:
            lib.errors.append(f"{src}: band set {set_name!r}: {e}")
    for brand in data.get("brands") or []:
        for m in brand.get("models") or []:
            try:
                bands = lib.band_sets.get(m.get("band_set", ""), [])
                if m.get("band_set") and not bands:
                    lib.errors.append(f"{src}: {brand['name']} {m['name']}: "
                                      f"unknown band set {m['band_set']!r}")
                model = EquipmentModel(brand["name"], m["name"], int(m.get("channels_per_unit", 1)),
                                       bands, int(m.get("half_width_khz", 0)),
                                       int(m.get("audio_bandwidth_khz", 0)))
            except (KeyError, TypeError, ValueError) as e:
                lib.errors.append(f"{src}: model {m!r}: {e}")
                continue
            lib.models = [x for x in lib.models if x.full_name != model.full_name] + [model]


def _merge_plans(lib: Library, data: dict, src: Path) -> None:
    for key, p in data.items():
        try:
            lib.plans[key] = ChannelPlan(key, p.get("type", key), int(p["width_khz"]),
                                         {str(k): int(v) for k, v in p["channels"].items()})
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            lib.errors.append(f"{src}: plan {key!r}: {e}")


def _merge_stations(lib: Library, data: list, src: Path) -> None:
    for s in data:
        try:
            st = Station(s["name"], float(s.get("lat", 0)), float(s.get("lon", 0)),
                         [Transmitter(t["plan"], str(t["channel"]), float(t.get("erp_kw", 0)))
                          for t in s.get("transmitters", [])])
        except (KeyError, TypeError, ValueError) as e:
            lib.errors.append(f"{src}: station {s!r}: {e}")
            continue
        lib.stations = [x for x in lib.stations if x.name != st.name] + [st]


def load(dirs: list[Path] | None = None) -> Library:
    lib = Library()
    for d in dirs if dirs is not None else [PACKAGE_DIR, USER_DIR]:
        if (data := _read(d / "devices.json", lib.errors)) is not None:
            _merge_devices(lib, data, d / "devices.json")
        if (data := _read(d / "channel_plans.json", lib.errors)) is not None:
            _merge_plans(lib, data, d / "channel_plans.json")
        if (data := _read(d / "stations.json", lib.errors)) is not None:
            _merge_stations(lib, data, d / "stations.json")
    return lib


def apply_bands_to_profiles(lib: Library) -> list[str]:
    """Replace hard-coded bands of network models with library band sets.
    Returns the profile models that were updated."""
    from .profiles import ALL_MODELS

    updated = []
    for prof in ALL_MODELS.values():
        key = prof.model.replace(" ", "").upper()
        best = None
        for m in lib.models:
            if m.brand.lower() != {"shure": "shure", "sennheiser_g4": "sennheiser"}[prof.family]:
                continue
            name = m.name.replace(" ", "").upper()
            if m.bands and key.startswith(name) and (best is None or len(name) > len(best.name)):
                best = m
        if best:
            prof.bands.clear()
            prof.bands.update({b.name: (b.lo_khz, b.hi_khz) for b in best.bands})
            updated.append(prof.model)
    return updated


_LIBRARY: Library | None = None


def get() -> Library:
    """Library loaded once per process; also applies band sets to profiles."""
    global _LIBRARY
    if _LIBRARY is None:
        _LIBRARY = load()
        apply_bands_to_profiles(_LIBRARY)
    return _LIBRARY
