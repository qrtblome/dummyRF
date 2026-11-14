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
``transmitters_*.json``
    broadcast transmitter sites, e.g. ``transmitters_pl.json`` built from the
    Polish UKE registers by ``python -m dummyrf.uke_import``

Band sets also override the hard-coded bands of network-controllable models
(``profiles.py``) when the library model name prefixes the profile model, e.g.
library "SLXD" -> profiles SLXD4 and SLXD4D.
"""
from __future__ import annotations

import json
import math
import re
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
    channel: str = ""
    erp_kw: float = 0.0
    freq_khz: int = 0
    width_khz: int = 0
    service: str = ""            # DVB-T2, DAB+, FM, …
    label: str = ""              # mux or programme name
    polarization: str = ""
    h_antenna_m: float | None = None

    @property
    def is_fm(self) -> bool:
        return self.service == "FM"

    def exclusion(self, station: str) -> Exclusion | None:
        if not (self.freq_khz and self.width_khz):
            return None
        erp = f" ({self.erp_kw:g} kW)" if self.erp_kw else ""
        what = " ".join(x for x in (self.service, self.channel, self.label) if x)
        half = self.width_khz // 2
        return Exclusion(self.freq_khz - half, self.freq_khz + half, f"{station}: {what}{erp}")


@dataclass
class Station:
    name: str
    lat: float
    lon: float
    transmitters: list[Transmitter] = field(default_factory=list)
    location: str = ""
    region: str = ""
    h_terrain_m: float | None = None


@dataclass
class Library:
    band_sets: dict[str, list[Band]] = field(default_factory=dict)
    models: list[EquipmentModel] = field(default_factory=list)
    plans: dict[str, ChannelPlan] = field(default_factory=dict)
    sites: list[Station] = field(default_factory=list)      # transmitter registers (transmitters_*.json)
    site_sources: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def model(self, full_name: str) -> EquipmentModel | None:
        return next((m for m in self.models if m.full_name == full_name), None)

    def station_exclusions(self, station: Station, include_fm: bool = False) -> list[Exclusion]:
        """Exclusions for a site's transmitters (FM left out unless asked for:
        it never reaches wireless-mic bands)."""
        out = []
        for t in station.transmitters:
            if t.is_fm and not include_fm:
                continue
            if (e := t.exclusion(station.name)) is not None:
                out.append(e)
        return out

    def nearby(self, lat: float, lon: float, radius_km: float) -> list[tuple[float, Station]]:
        """Transmitter sites within ``radius_km``, nearest first."""
        out = []
        for st in self.sites:
            if abs(st.lat - lat) > radius_km / 100 + 0.1:     # cheap pre-filter (1° ≈ 111 km)
                continue
            d = distance_km(lat, lon, st.lat, st.lon)
            if d <= radius_km:
                out.append((d, st))
        return sorted(out, key=lambda x: x[0])


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


_COORD = re.compile(r"""^\s*(-?\d+(?:[.,]\d+)?)\s*°?\s*([NS])?\s*[,; ]\s*(-?\d+(?:[.,]\d+)?)\s*°?\s*([EW])?\s*$""",
                    re.I)


def parse_coords(text: str) -> tuple[float, float]:
    """'52.4064, 16.9252' / '52,4064; 16,9252' / '52.4N 16.9E' -> (lat, lon)."""
    m = _COORD.match(text.replace(" ", " "))
    if not m:
        raise ValueError("enter coordinates as latitude, longitude — e.g. 52.4064, 16.9252")
    lat = float(m.group(1).replace(",", "."))
    lon = float(m.group(3).replace(",", "."))
    if (m.group(2) or "").upper() == "S":
        lat = -abs(lat)
    if (m.group(4) or "").upper() == "W":
        lon = -abs(lon)
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("coordinates out of range")
    return lat, lon


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


def _merge_sites(lib: Library, data: dict, src: Path) -> None:
    if not isinstance(data, dict) or data.get("format") != "dummyrf-transmitters":
        lib.errors.append(f"{src}: not a dummyRF transmitter list")
        return
    sites = []
    for s in data.get("stations", []):
        try:
            txs = [Transmitter(channel=str(t.get("channel", "")), erp_kw=float(t.get("erp_kw") or 0),
                               freq_khz=int(t["freq_khz"]), width_khz=int(t["width_khz"]),
                               service=t.get("service", ""),
                               label=t.get("mux") or t.get("program") or "",
                               polarization=t.get("polarization", ""),
                               h_antenna_m=t.get("h_antenna_m"))
                   for t in s.get("transmitters", [])]
            sites.append(Station(s["name"], float(s["lat"]), float(s["lon"]), txs,
                                 s.get("location", ""), s.get("region", ""), s.get("h_terrain_m")))
        except (KeyError, TypeError, ValueError) as e:
            lib.errors.append(f"{src}: site {s.get('name', '?')!r}: {e}")
    lib.sites += sites
    lib.site_sources.append(f"{src.name} ({data.get('as_of', '?')}, {len(sites)} sites)")


def load(dirs: list[Path] | None = None) -> Library:
    lib = Library()
    for d in dirs if dirs is not None else [PACKAGE_DIR, USER_DIR]:
        for f in sorted(d.glob("transmitters_*.json")) if d.is_dir() else []:
            if (data := _read(f, lib.errors)) is not None:
                _merge_sites(lib, data, f)
        if (data := _read(d / "devices.json", lib.errors)) is not None:
            _merge_devices(lib, data, d / "devices.json")
        if (data := _read(d / "channel_plans.json", lib.errors)) is not None:
            _merge_plans(lib, data, d / "channel_plans.json")
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
