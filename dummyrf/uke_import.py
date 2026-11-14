"""Import Polish broadcast transmitters from UKE permit exports.

UKE (Urząd Komunikacji Elektronicznej) publishes permit registers as CSV:

    pozwolenia_ntc_r_<date>.csv   digital terrestrial TV (DVB-T/T2, 5G Broadcast)
    pozwolenia_dab_r_<date>.csv   T-DAB
    pozwolenie_ukf_r_<date>.csv   FM radio (UKF)

``_r`` is the current register; ``_h`` also contains superseded decisions
(every ``_r`` permit is in ``_h``), so only ``_r`` files are used.

TV/DAB files are Windows-1250, FM files UTF-8 with BOM; both use ``;`` and
decimal commas.  Coordinates are DMS like ``19E19'41"`` (FM rows sometimes
carry an "E" on the latitude — the letter is only trusted for S/W).

Per transmitter id the newest decision wins; revoked ones ("Uchylenie") and
permits that expired before the export date are dropped.  Transmitters are
grouped into stations by site name and position.

Run ``python -m dummyrf.uke_import`` to rebuild ``data/transmitters_pl.json``.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import re
import sys
from pathlib import Path

SRC_DIR = Path(__file__).parent / "data" / "transmitters"
OUT_FILE = Path(__file__).parent / "data" / "transmitters_pl.json"

FM_WIDTH_KHZ = 200


def read_csv(path: Path) -> list[dict[str, str]]:
    raw = path.read_bytes()
    text = raw[3:].decode("utf-8") if raw.startswith(b"\xef\xbb\xbf") else raw.decode("cp1250")
    return list(csv.DictReader(text.splitlines(), delimiter=";"))


def num(s: str | None) -> float | None:
    if s is None:
        return None
    s = s.strip().replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


_DMS = re.compile(r"(\d+)\s*([NSEW])\s*(\d+)'\s*(\d+(?:[.,]\d+)?)")


def dms(s: str, latitude: bool) -> float | None:
    """'19E19\\'41"' -> 19.3281. Only S/W flip the sign (some rows say E for latitude)."""
    m = _DMS.search(s or "")
    if not m:
        return None
    deg, hemi, mins, secs = int(m.group(1)), m.group(2), int(m.group(3)), float(m.group(4).replace(",", "."))
    v = deg + mins / 60 + secs / 3600
    return -v if hemi in "SW" else v


def date(s: str) -> dt.date | None:
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def _pick(row: dict, *names: str) -> str:
    for n in names:
        if n in row and row[n] is not None:
            return row[n].strip()
    return ""


REGIONS = ["DOLNOŚLĄSKIE", "KUJAWSKO-POMORSKIE", "LUBELSKIE", "LUBUSKIE", "ŁÓDZKIE",
           "MAŁOPOLSKIE", "MAZOWIECKIE", "OPOLSKIE", "PODKARPACKIE", "PODLASKIE", "POMORSKIE",
           "ŚLĄSKIE", "ŚWIĘTOKRZYSKIE", "WARMIŃSKO-MAZURSKIE", "WIELKOPOLSKIE",
           "ZACHODNIOPOMORSKIE"]
_ASCII = str.maketrans("ĄĆĘŁŃÓŚŹŻ", "ACELNOSZZ")
_REGION_BY_ASCII = {r.translate(_ASCII): r for r in REGIONS}


def region(s: str) -> str:
    """'slaskie' / 'SLASKIE' / 'Śląskie' -> 'ŚLĄSKIE'."""
    u = s.strip().upper()
    return _REGION_BY_ASCII.get(u.translate(_ASCII), u)


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    from math import asin, cos, radians, sin, sqrt
    p1, p2 = radians(lat1), radians(lat2)
    dp, dl = p2 - p1, radians(lon2 - lon1)
    a = sin(dp / 2) ** 2 + cos(p1) * cos(p2) * sin(dl / 2) ** 2
    return 2 * 6371.0 * asin(sqrt(a))


SITE_MERGE_KM = 0.3   # FM and TV permits for one tower differ by a couple of hundred metres


def _pattern(row: dict) -> list[float] | None:
    vals = [num(row.get(f"{a}°")) for a in range(0, 360, 10)]
    if any(v is None for v in vals) or not any(vals):
        return None
    return [round(v, 1) for v in vals]


def _service(kind: str, system: str) -> str:
    if kind == "dab":
        return "DAB+"
    if kind == "ukf":
        return "FM"
    return system or "DVB-T2"


def parse_row(kind: str, row: dict) -> dict | None:
    lat = dms(_pick(row, "Sz.geogr. (WGS84)", "Sz. geogr."), latitude=True)
    lon = dms(_pick(row, "Dł.geogr. (WGS84)", "Dł. geogr."), latitude=False)
    if lat is None or lon is None:
        return None
    if kind == "ukf":
        f_mhz = num(_pick(row, "F [MHz]"))
        width_khz = FM_WIDTH_KHZ
        channel = f"{f_mhz:g}" if f_mhz else ""
    else:
        f_mhz = num(_pick(row, "Czestotliwość środkowa [MHz]", "Częstotliwość środkowa [MHz]"))
        width_khz = int(round((num(_pick(row, "Szerokość kanału [MHz]")) or 0) * 1000))
        channel = _pick(row, "Kanał/blok częstotliwościowy")
    if not f_mhz:
        return None
    tx = {
        "service": _service(kind, _pick(row, "System emisji")),
        "channel": channel,
        "freq_khz": int(round(f_mhz * 1000)),
        "width_khz": width_khz,
        "erp_kw": num(_pick(row, "ERP[kW]")),
        "erp_dbw": num(_pick(row, "ERP[dBW]")),
        "polarization": _pick(row, "Polaryzacja", "Pol."),
        "h_antenna_m": num(_pick(row, "Wysokość anteny [m. npt]", "H ant.")),
        "status": _pick(row, "Status"),
        "valid_to": (date(_pick(row, "Ważny do")) or dt.date.max).isoformat(),
        "operator": _pick(row, "Wnioskodawca"),
    }
    mux = _pick(row, "Nazwa multipleksu")
    if mux:
        tx["mux"] = mux
    program = _pick(row, "Program")
    if program:
        tx["program"] = program
    pat = _pattern(row)
    if pat and _pick(row, "Charakterystyka", "Char.").upper() == "D":
        tx["pattern_db"] = pat          # attenuation every 10° from north
    site = {
        "name": _pick(row, "Nazwa stacji"),
        "location": _pick(row, "Lokalizacja stacji"),
        "region": region(_pick(row, "Województwo")),
        "lat": round(lat, 5),
        "lon": round(lon, 5),
        "h_terrain_m": num(_pick(row, "Wysokość lokalizacji [m. npm]", "H ter.")),
    }
    decided = date(_pick(row, "Data pozwolenia/decyzji")) or dt.date.min
    tx_id = _pick(row, "identyfikator", "id wystąpienia")
    return {"id": f"{kind}:{tx_id}", "decided": decided, "site": site, "tx": tx}


def kind_of(path: Path) -> str | None:
    m = re.match(r"pozwoleni[ae]_(ntc|dab|ukf)_r_", path.name)
    return m.group(1) if m else None


def export_date(paths: list[Path]) -> dt.date | None:
    ds = [date(m.group(1)) for p in paths if (m := re.search(r"_(\d{4}-\d{2}-\d{2})\.csv$", p.name))]
    ds = [d for d in ds if d]
    return max(ds) if ds else None


def build(src_dir: Path = SRC_DIR) -> dict:
    paths = sorted(p for p in src_dir.glob("*.csv") if kind_of(p))
    if not paths:
        raise FileNotFoundError(f"no UKE *_r_*.csv files in {src_dir}")
    as_of = export_date(paths)
    latest: dict[str, dict] = {}
    counts = {"rows": 0, "unparsed": 0, "revoked": 0, "expired": 0}
    for p in paths:
        kind = kind_of(p)
        for row in read_csv(p):
            counts["rows"] += 1
            rec = parse_row(kind, row)
            if rec is None:
                counts["unparsed"] += 1
                continue
            cur = latest.get(rec["id"])
            if cur is None or rec["decided"] >= cur["decided"]:
                latest[rec["id"]] = rec
    # strongest transmitters first: they name the site they found
    kept = []
    for rec in latest.values():
        tx = rec["tx"]
        if tx["status"].lower() == "uchylenie":
            counts["revoked"] += 1
        elif as_of and tx["valid_to"] < as_of.isoformat():
            counts["expired"] += 1
        else:
            kept.append(rec)
    kept.sort(key=lambda r: -(r["tx"]["erp_kw"] or 0))
    out: list[dict] = []
    for rec in kept:
        s, tx = rec["site"], rec["tx"]
        st = next((c for c in out
                   if abs(c["lat"] - s["lat"]) < 0.01
                   and distance_km(c["lat"], c["lon"], s["lat"], s["lon"]) <= SITE_MERGE_KM), None)
        if st is None:
            st = {**s, "transmitters": []}
            out.append(st)
        elif not st["region"] and s["region"]:
            st["region"] = s["region"]
        dup = next((t for t in st["transmitters"] if t["service"] == tx["service"]
                    and t["freq_khz"] == tx["freq_khz"]
                    and t.get("mux", t.get("program")) == tx.get("mux", tx.get("program"))), None)
        if dup is None:
            st["transmitters"].append(tx)
        else:
            counts["duplicates"] = counts.get("duplicates", 0) + 1
    out.sort(key=lambda s: (s["region"], s["name"]))
    for st in out:
        st["transmitters"].sort(key=lambda t: (t["service"], t["freq_khz"]))
    n_tx = sum(len(s["transmitters"]) for s in out)
    return {
        "format": "dummyrf-transmitters",
        "source": "UKE permit registers: " + ", ".join(p.name for p in paths),
        "as_of": as_of.isoformat() if as_of else "",
        "stats": {**counts, "stations": len(out), "transmitters": n_tx},
        "stations": out,
    }


def main(argv: list[str]) -> int:
    src = Path(argv[1]) if len(argv) > 1 else SRC_DIR
    data = build(src)
    OUT_FILE.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    st = data["stats"]
    print(f"{OUT_FILE.name}: {st['stations']} stations, {st['transmitters']} transmitters "
          f"(from {st['rows']} rows; dropped {st['revoked']} revoked, {st['expired']} expired, "
          f"{st['unparsed']} unreadable, {st.get('duplicates', 0)} duplicates) — as of {data['as_of']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
