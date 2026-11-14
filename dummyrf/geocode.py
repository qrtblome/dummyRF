"""Turn what the user types into a venue position.

Offline (always available)
    * coordinates: ``52.4064, 16.9252``
    * Polish postal code: ``60-685`` or ``60685``
    * town / village name: ``Poznań``, ``poznan``, ``Śrem``
    from ``data/postcodes_pl.tsv.gz`` — GeoNames postal codes (www.geonames.org,
    CC BY 4.0).  Rebuild it from a fresh ``PL.txt`` with
    ``python -m dummyrf.geocode build PL.txt``.

Online (only when asked)
    full addresses and venue names through OpenStreetMap Nominatim.  Its usage
    policy allows at most one request per second, with an identifying
    User-Agent; we send one request per click.

A postal code gives the centre of its area — exact enough to find the
broadcast transmitters around a venue.
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import re
import statistics
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from .textfold import fold

DATA_FILE = Path(__file__).parent / "data" / "postcodes_pl.tsv.gz"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "dummyRF/0.1 (wireless microphone RF coordination tool)"

# GeoNames uses English region names; the transmitter list uses Polish ones
VOIVODESHIP = {
    "Lower Silesia": "DOLNOŚLĄSKIE", "Kujawsko-Pomorskie": "KUJAWSKO-POMORSKIE",
    "Lublin": "LUBELSKIE", "Lubusz": "LUBUSKIE", "Łódź Voivodeship": "ŁÓDZKIE",
    "Lesser Poland": "MAŁOPOLSKIE", "Mazovia": "MAZOWIECKIE", "Opole Voivodeship": "OPOLSKIE",
    "Subcarpathia": "PODKARPACKIE", "Podlasie": "PODLASKIE", "Pomerania": "POMORSKIE",
    "Silesia": "ŚLĄSKIE", "Świętokrzyskie": "ŚWIĘTOKRZYSKIE",
    "Warmia-Masuria": "WARMIŃSKO-MAZURSKIE", "Greater Poland": "WIELKOPOLSKIE",
    "West Pomerania": "ZACHODNIOPOMORSKIE",
}


@dataclass
class Place:
    label: str
    lat: float
    lon: float
    source: str        # "coordinates" | "postal code" | "town" | "online"

    def __str__(self) -> str:
        return f"{self.label} ({self.lat:.4f}, {self.lon:.4f})"


# ------------------------------------------------------------------ build
def build(src: Path, out: Path = DATA_FILE) -> int:
    """GeoNames PL.txt -> compact gzip TSV: code, place, region, county, lat, lon."""
    rows = []
    with open(src, encoding="utf-8") as f:
        for r in csv.reader(f, delimiter="\t"):
            if len(r) < 11 or r[0] != "PL":
                continue
            rows.append((r[1], r[2], VOIVODESHIP.get(r[3], r[3].upper()), r[5],
                         f"{float(r[9]):.4f}", f"{float(r[10]):.4f}"))
    rows.sort()
    buf = io.StringIO()
    csv.writer(buf, delimiter="\t", lineterminator="\n").writerows(rows)
    out.write_bytes(gzip.compress(buf.getvalue().encode("utf-8"), compresslevel=9, mtime=0))
    return len(rows)


# ------------------------------------------------------------------ offline
class Gazetteer:
    def __init__(self, path: Path = DATA_FILE):
        self.by_code: dict[str, list[tuple]] = defaultdict(list)
        self.by_town: dict[str, list[tuple]] = defaultdict(list)   # folded place -> rows
        if not path.exists():
            return
        text = gzip.decompress(path.read_bytes()).decode("utf-8")
        for code, place, region, county, lat, lon in csv.reader(io.StringIO(text), delimiter="\t"):
            row = (code, place, region, county, float(lat), float(lon))
            self.by_code[code].append(row)
            self.by_town[fold(place)].append(row)

    @property
    def available(self) -> bool:
        return bool(self.by_code)

    def postal(self, code: str) -> list[Place]:
        code = re.sub(r"\D", "", code)
        code = f"{code[:2]}-{code[2:]}"
        out = []
        for c, place, region, county, lat, lon in self.by_code.get(code, []):
            out.append(Place(f"{c} {place} ({county}, {region.title()})", lat, lon, "postal code"))
        return out

    def town(self, name: str, limit: int = 15) -> list[Place]:
        q = fold(name).strip()
        if not q:
            return []
        exact = [k for k in self.by_town if k == q]
        starts = sorted(k for k in self.by_town if k.startswith(q) and k != q)
        out = []
        for key in exact + starts:
            groups = defaultdict(list)          # same name in different counties
            for row in self.by_town[key]:
                groups[(row[1], row[3], row[2])].append(row)
            def rank(g):
                (place, county, _), rows = g
                # county seats first (Śrem the town before villages called Śrem), then size
                seat = fold(place)[:4] in fold(county) or fold(place) == fold(county)
                return (not seat, -len(rows))
            for (place, county, region), rows in sorted(groups.items(), key=rank):
                lat = statistics.median(r[4] for r in rows)
                lon = statistics.median(r[5] for r in rows)
                codes = f"{len(rows)} postal codes" if len(rows) > 1 else rows[0][0]
                out.append(Place(f"{place} ({county}, {region.title()}) — {codes}", lat, lon, "town"))
            if len(out) >= limit:
                break
        return out[:limit]


_GAZ: Gazetteer | None = None


def gazetteer() -> Gazetteer:
    global _GAZ
    if _GAZ is None:
        _GAZ = Gazetteer()
    return _GAZ


POSTAL_RE = re.compile(r"^\s*\d{2}-?\d{3}\s*$")


def lookup(text: str) -> list[Place]:
    """Offline: coordinates, postal code or town name. Empty list = not found."""
    from .library import parse_coords
    text = text.strip()
    if not text:
        return []
    try:
        lat, lon = parse_coords(text)
        return [Place(f"{lat:.5f}, {lon:.5f}", lat, lon, "coordinates")]
    except ValueError:
        pass
    if POSTAL_RE.match(text):
        return gazetteer().postal(text)
    return gazetteer().town(text)


# ------------------------------------------------------------------ online
def search_online(query: str, limit: int = 8, timeout: float = 10.0) -> list[Place]:
    """OpenStreetMap Nominatim: addresses, venues, towns. One request per call."""
    params = urllib.parse.urlencode({"q": query, "format": "jsonv2", "limit": limit,
                                     "addressdetails": 0, "accept-language": "pl,en"})
    req = urllib.request.Request(f"{NOMINATIM_URL}?{params}", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return [Place(item.get("display_name", query), float(item["lat"]), float(item["lon"]), "online")
            for item in data if "lat" in item and "lon" in item]


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] == "build":
        n = build(Path(argv[2]))
        print(f"{DATA_FILE.name}: {n} postal-code entries ({DATA_FILE.stat().st_size // 1024} KB)")
        return 0
    for place in lookup(" ".join(argv[1:])):
        print(place)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
