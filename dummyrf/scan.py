"""Scan file import.

Accepts Shure Wireless Workbench 7 binary scans (.sdb3) and the common
text/CSV layouts: Shure Wireless Workbench CSV export,
RF Explorer CSV, Sennheiser WSM/CSV exports and plain "frequency, level"
files.  Each data line must contain a frequency and a level as its first two
numbers; header and comment lines are skipped.  The frequency unit is guessed
from magnitude (Hz, kHz or MHz).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np

_NUM = re.compile(r"[-+]?\d+(?:[.,]\d+)?(?:[eE][-+]?\d+)?")


@dataclass
class Scan:
    name: str
    freqs_khz: np.ndarray   # sorted, float
    levels_dbm: np.ndarray
    path: str = ""
    # True when the levels are real dBm (Shure receivers, WWB .sdb3); False for
    # sources with only roughly known levels (RTL-SDR, generic CSV, demo)
    calibrated: bool = False

    @property
    def span(self) -> tuple[float, float]:
        return float(self.freqs_khz[0]), float(self.freqs_khz[-1])


def _numbers(line: str) -> list[float]:
    # Allow both "578.350;-92,5" (EU decimal comma with ; separator) and
    # "578.350,-92.5".  If ";" or tab separates fields, commas are decimals.
    if ";" in line or "\t" in line:
        fields = re.split(r"[;\t]", line)
        out = []
        for f in fields:
            m = _NUM.search(f.strip())
            if m:
                out.append(float(m.group().replace(",", ".")))
        return out
    return [float(m) for m in re.findall(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", line)]


def _to_khz(f: np.ndarray) -> np.ndarray:
    med = float(np.median(f))
    if med > 1e7:      # Hz
        return f / 1000.0
    if med > 1e4:      # kHz
        return f
    return f * 1000.0  # MHz


def parse_scan_text(text: str, name: str = "scan") -> Scan:
    fs, ls = [], []
    for line in text.splitlines():
        s = line.strip()
        if not s or s[0] in "#;/" or s[0].isalpha():
            continue
        nums = _numbers(s)
        if len(nums) < 2:
            continue
        f, lvl = nums[0], nums[1]
        if f <= 0 or not -200 <= lvl <= 50:
            continue
        fs.append(f)
        ls.append(lvl)
    if len(fs) < 2:
        raise ValueError(f"{name}: no frequency/level data found")
    f = _to_khz(np.array(fs, dtype=np.float64))
    lv = np.array(ls, dtype=np.float64)
    order = np.argsort(f, kind="stable")
    f, lv = f[order], lv[order]
    # merge duplicate frequencies by max-hold
    uf, idx = np.unique(f, return_index=True)
    if len(uf) != len(f):
        lv = np.maximum.reduceat(lv, idx)
        f = uf
    return Scan(name, f, lv)


SDB3_MAGIC = b"//@ShureScan"


def parse_sdb3(data: bytes, name: str = "scan") -> Scan:
    """Shure Wireless Workbench 7 scan file (.sdb3).

    Layout (seen in files written by WWB 7.8.1)::

        //@ShureScan
        { JSON header: AmplUnits, FreqUnits, BitWidth, "Scale Factor",
          NoDataValue, BinarySchema [...] }
        @Binary:
        sweep, sweep, ...          each laid out as BinarySchema says:
                                   "@Swp", id, timestamp (4 bytes each),
                                   one signed big-endian BitWidth value per
                                   frequency point of every FreqRange, crc16
        @Extended:
        { JSON: Band, Channel, ScanName, AppendedSpecialSweeps, ... }
        @End ...

    All sweeps (the scan and WWB's appended peak hold) are combined by max-hold.
    """
    if not data.startswith(SDB3_MAGIC):
        raise ValueError(f"{name}: not a WWB .sdb3 scan")
    k = data.find(b"@Binary:")
    if k < 0:
        raise ValueError(f"{name}: no @Binary section")
    try:
        hdr = json.loads(data[data.index(b"{"):k].decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise ValueError(f"{name}: bad .sdb3 header: {e}") from e
    freq_mult = {"hz": 0.001, "khz": 1.0, "mhz": 1000.0}.get(str(hdr.get("FreqUnits", "KHz")).lower())
    if freq_mult is None:
        raise ValueError(f"{name}: unknown FreqUnits {hdr.get('FreqUnits')!r}")
    width = int(hdr.get("BitWidth", 16)) // 8
    scale = float(hdr.get("Scale Factor", 1)) or 1.0
    nodata = hdr.get("NoDataValue")

    # walk the schema once to get the record layout
    layout = []          # ("skip", n) | ("curve", freqs ndarray)
    freqs_all = []
    for field_ in hdr.get("BinarySchema", []):
        if "Curve" in field_:
            fr = []
            for r in field_["Curve"].get("FreqRanges", []):
                n = (int(r["EndFreq"]) - int(r["StartFreq"])) // int(r["StepFreq"]) + 1
                fr.append(np.arange(n) * float(r["StepFreq"]) + float(r["StartFreq"]))
            f = np.concatenate(fr) * freq_mult if fr else np.array([])
            layout.append(("curve", f))
            freqs_all.append(f)
        else:
            layout.append(("skip", int(field_.get("Bytes", 0))))
    if not freqs_all:
        raise ValueError(f"{name}: .sdb3 has no curve in its schema")
    rec_len = sum(v if kind == "skip" else len(v) * width for kind, v in layout)
    dtype = np.dtype(f">i{width}")

    body = data[k + len(b"@Binary:"):]
    pos, best = 0, None
    while body.startswith(b"@Swp", pos) and pos + rec_len <= len(body):
        values = []
        q = pos
        for kind, v in layout:
            if kind == "skip":
                q += v
            else:
                raw = np.frombuffer(body, dtype=dtype, count=len(v), offset=q).astype(np.float64)
                if nodata is not None:
                    raw[raw == nodata] = np.nan
                values.append(raw / scale)
                q += len(v) * width
        sweep = np.concatenate(values)
        best = sweep if best is None else np.fmax(best, sweep)
        pos += rec_len
    if best is None:
        raise ValueError(f"{name}: .sdb3 contains no sweeps")

    ext = {}
    e = body.find(b"@Extended:", pos)
    if e >= 0:
        try:
            end = body.index(b"@End", e)
            ext = json.loads(body[e + len(b"@Extended:"):end].decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            ext = {}
    title = (ext.get("ScanName") or [None])[0] or hdr.get("Title") or name
    model = hdr.get("ScannerModel") or ""
    band = ext.get("Band") or ""
    tag = f"({model} {band})".replace(" )", ")") if model else ""
    label = title if not tag or tag in title else f"{title} {tag}"

    freqs = np.concatenate(freqs_all)
    ok = ~np.isnan(best)
    order = np.argsort(freqs[ok], kind="stable")
    return Scan(label, freqs[ok][order], best[ok][order], calibrated=True)   # WWB: receiver dBm


def load_scan(path: str | Path) -> Scan:
    p = Path(path)
    data = p.read_bytes()
    if data.startswith(SDB3_MAGIC):
        sc = parse_sdb3(data, p.stem)
    else:
        sc = parse_scan_text(data.decode("utf-8", errors="replace"), p.stem)
    sc.path = str(p)
    return sc


def combine_max(scans: list[Scan], step_khz: float = 25.0) -> tuple[np.ndarray, np.ndarray] | None:
    """Max-hold of several scans on a common grid (for coordination)."""
    if not scans:
        return None
    lo = min(s.freqs_khz[0] for s in scans)
    hi = max(s.freqs_khz[-1] for s in scans)
    grid = np.arange(lo, hi + step_khz, step_khz)
    out = np.full(len(grid), -200.0)
    for s in scans:
        inside = (grid >= s.freqs_khz[0]) & (grid <= s.freqs_khz[-1])
        out[inside] = np.maximum(out[inside], np.interp(grid[inside], s.freqs_khz, s.levels_dbm))
        # also keep narrow peaks the interpolation might skip
        k = np.clip(np.round((s.freqs_khz - lo) / step_khz).astype(int), 0, len(grid) - 1)
        np.maximum.at(out, k, s.levels_dbm)
    return grid, out


def synthetic_scan(lo_khz: int = 470000, hi_khz: int = 700000, seed: int = 1) -> Scan:
    """A fake scan with a noise floor and a few TV carriers, for demos."""
    rng = np.random.default_rng(seed)
    f = np.arange(lo_khz, hi_khz, 50.0)
    lv = -105 + rng.normal(0, 2.0, len(f))
    for ch in rng.choice(np.arange(21, 49), 5, replace=False):
        c0 = 470000 + (ch - 21) * 8000
        m = (f >= c0 + 250) & (f <= c0 + 7750)
        lv[m] = -60 + rng.normal(0, 1.5, m.sum())
    for _ in range(12):
        c = rng.uniform(lo_khz, hi_khz)
        lv += 40 * np.exp(-((f - c) / 30) ** 2) * (rng.random() > 0.3)
    return Scan("synthetic demo scan", f, lv)


# ------------------------------------------------------------------ export
def crc16_arc(data: bytes) -> int:
    """CRC-16/ARC (poly 0x8005 reflected, init 0) — what WWB puts after each
    .sdb3 sweep, computed over the record from "@Swp" to the last value."""
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def _regrid(scan: Scan, step_khz: int) -> tuple[int, np.ndarray]:
    """Put a scan on an exact kHz grid (max of the points in each bin)."""
    start = int(np.ceil(scan.freqs_khz[0] / step_khz) * step_khz)
    stop = int(np.floor(scan.freqs_khz[-1] / step_khz) * step_khz)
    grid = np.arange(start, stop + 1, step_khz, dtype=np.float64)
    out = np.interp(grid, scan.freqs_khz, scan.levels_dbm)
    idx = np.clip(np.round((scan.freqs_khz - start) / step_khz).astype(int), 0, len(grid) - 1)
    inside = (scan.freqs_khz >= start - step_khz / 2) & (scan.freqs_khz <= stop + step_khz / 2)
    np.maximum.at(out, idx[inside], scan.levels_dbm[inside])
    return start, out


def write_sdb3(scan: Scan, path: str | Path, step_khz: int = 25, scanner: str = "dummyRF",
               when=None) -> None:
    """Write ``scan`` as a WWB 7 .sdb3 file (one sweep)."""
    import datetime
    when = when or datetime.datetime.now()
    start, vals = _regrid(scan, step_khz)
    stop = start + (len(vals) - 1) * step_khz
    nodata = -1400
    raw = np.where(np.isnan(vals), nodata, np.clip(np.round(vals * 10), -32768, 32767)).astype(">i2")
    hdr = {
        "AmplUnits": "dBm",
        "BinarySchema": [
            {"Bytes": 4, "DataValue": "start-of-sweep"},
            {"Bytes": 4, "DataValue": "id"},
            {"Bytes": 4, "DataValue": "timestamp"},
            {"Curve": {"Color": "#6692fc", "CoordinationSource": True,
                       "FreqRanges": [{"EndFreq": stop, "StartFreq": start, "StepFreq": step_khz}],
                       "Name": "Default curve", "ResolutionBandWidth": step_khz}},
            {"Bytes": 2, "DataValue": "crc16"},
        ],
        "BitWidth": 16,
        "FreqUnits": "KHz",
        "NoDataValue": nodata,
        "Scale Factor": 10,
        "ScannerModel": scanner,
        "ScannerName": scanner,
        "StartDate": when.strftime("%m/%d/%Y"),
        "StartTime": when.strftime("%H:%M:%S"),
        "Title": scan.name,
        "Version": "1.0.0.0",
    }
    record = b"@Swp" + (1).to_bytes(4, "big") + (0).to_bytes(4, "big") + raw.tobytes()
    record += crc16_arc(record).to_bytes(2, "big")
    ext = {"Creator": "dummyRF", "ScanName": [scan.name], "UserCurveColors": ["#6692fc"]}
    data = (SDB3_MAGIC + b"\n" + json.dumps(hdr, indent="\t", separators=(",", " : ")).encode()
            + b"\n@Binary:" + record
            + b"@Extended:\n" + json.dumps(ext, indent="\t", separators=(",", " : ")).encode()
            + b"\n@End" + (1).to_bytes(4, "big") + (1).to_bytes(4, "big"))
    Path(path).write_bytes(data)


def write_csv(scan: Scan, path: str | Path) -> None:
    lines = ["Frequency (MHz),Level (dBm)"]
    lines += [f"{f / 1000:.4f},{lv:.1f}" for f, lv in zip(scan.freqs_khz, scan.levels_dbm)
              if not np.isnan(lv)]
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


# ------------------------------------------------------------------ normalisation
FLOOR_PERCENTILE = 20     # 80 % of the points lie above the noise-floor estimate


def noise_floor(scan: Scan) -> float:
    """Estimate of the scan's noise floor (dB), robust to strong signals."""
    v = scan.levels_dbm[np.isfinite(scan.levels_dbm)]
    return float(np.percentile(v, FLOOR_PERCENTILE)) if len(v) else float("nan")


def normalize(scans: list[Scan]) -> list[tuple[Scan, float]]:
    """Shift every scan so its noise floor matches a reference scan.

    The reference is the first calibrated scan (real dBm from a receiver), or
    the first scan if none is calibrated.  Returns (shifted copy, shift in dB).
    """
    if not scans:
        return []
    ref = next((s for s in scans if s.calibrated), scans[0])
    ref_floor = noise_floor(ref)
    out = []
    for s in scans:
        shift = 0.0 if s is ref or not np.isfinite(ref_floor) else ref_floor - noise_floor(s)
        if not np.isfinite(shift):
            shift = 0.0
        out.append((Scan(s.name, s.freqs_khz, s.levels_dbm + shift, s.path, s.calibrated), shift))
    return out
