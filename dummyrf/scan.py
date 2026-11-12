"""Scan file import.

Accepts the common text/CSV layouts: Shure Wireless Workbench CSV export,
RF Explorer CSV, Sennheiser WSM/CSV exports and plain "frequency, level"
files.  Each data line must contain a frequency and a level as its first two
numbers; header and comment lines are skipped.  The frequency unit is guessed
from magnitude (Hz, kHz or MHz).
"""
from __future__ import annotations

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


def load_scan(path: str | Path) -> Scan:
    p = Path(path)
    text = p.read_text(encoding="utf-8", errors="replace")
    sc = parse_scan_text(text, p.stem)
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
