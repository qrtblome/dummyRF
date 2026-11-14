"""Frequency coordination: intermodulation maths, conflict checks and a
frequency-plan generator.

All frequencies are integers in kHz.

Intermod products considered:
  3rd order: 2a-b (two transmitters) and a+b-c (three transmitters)
  5th order: 3a-2b (two transmitters), optional

Spacing rules come from a SpacingProfile per slot: minimum carrier-to-carrier
spacing, and minimum spacing between a carrier and 2-tx 3rd, 2-tx 5th and
3-tx 3rd order products (each 0 = ignore).
The defaults are conservative starting points, not manufacturer figures —
adjust them to taste.
"""
from __future__ import annotations

import itertools
import random
import time
from dataclasses import dataclass, field

import numpy as np


@dataclass(frozen=True)
class SpacingProfile:
    key: str
    label: str
    cc_khz: int        # carrier to carrier
    imd3_khz: int      # carrier to 2-tx 3rd order product (2a-b)
    imd5_khz: int      # carrier to 2-tx 5th order product (3a-2b), 0 = ignore
    imd3tx_khz: int    # carrier to 3-tx 3rd order product (a+b-c), 0 = ignore
    step_khz: int = 25


SPACING_PROFILES: dict[str, SpacingProfile] = {p.key: p for p in [
    SpacingProfile("digital_mic", "Digital mic (ULX-D/QLX-D/SLX-D/AD)", 350, 75, 0, 25),
    SpacingProfile("slxd_norm", "SLX-D", 450, 200, 0, 25),
    SpacingProfile("qlxd_norm", "QLX-D", 350, 75, 0, 25),
    SpacingProfile("ulxd_norm", "ULX-D, standard", 350, 75, 0, 25),
    SpacingProfile("ulxd_high_density", "ULX-D, high density", 125, 150, 0, 25),
    SpacingProfile("axient_norm", "Axient Digital, standard", 350, 75, 0, 25),
    SpacingProfile("axient_high_density", "Axient Digital, high density", 125, 150, 0, 25),
    SpacingProfile("analog_iem", "Analog IEM (PSM1000/ADPSM/EW IEM G4)", 400, 150, 50, 50),
    SpacingProfile("ew_iem", "EW IEM G3/G4)", 400, 200, 100, 50),
    SpacingProfile("sr_iem", "SR2050)", 400, 200, 100, 50),
    SpacingProfile("analog_mic", "Analog mic (EW G3/G4)", 350, 125, 50, 50),
    SpacingProfile("blx", "BLX", 500, 225, 175, 125, 50),
    SpacingProfile("act747", "ACT-747", 500, 225, 175, 125, 50),
    SpacingProfile("strict", "Strict / unknown equipment", 500, 200, 100, 100),
]}


@dataclass
class Slot:
    label: str
    profile: str = "digital_mic"
    lo_khz: int = 470000
    hi_khz: int = 608000
    freq_khz: int | None = None
    fixed: bool = False
    device_uid: str | None = None
    channel: int | None = None

    @property
    def spacing(self) -> SpacingProfile:
        return SPACING_PROFILES.get(self.profile, SPACING_PROFILES["strict"])


@dataclass
class Exclusion:
    lo_khz: int
    hi_khz: int
    label: str = ""


@dataclass
class Conflict:
    slot: int
    kind: str      # "band", "excluded", "carrier", "imd3", "imd5", "scan"
    detail: str


@dataclass
class CoordinationResult:
    freqs: dict[int, int]                  # slot index -> kHz
    unplaced: list[int] = field(default_factory=list)
    attempts: int = 0
    seconds: float = 0.0


def tv_channel(ch: int, region: str = "EU") -> Exclusion:
    """UHF TV channel as an exclusion. EU: 8 MHz raster from ch21 @470 MHz.
    US: 6 MHz raster from ch14 @470 MHz."""
    if region == "EU":
        if not 21 <= ch <= 69:
            raise ValueError("EU UHF channels are 21-69")
        lo = 470000 + (ch - 21) * 8000
        return Exclusion(lo, lo + 8000, f"TV {ch} (EU)")
    if region == "US":
        if not 14 <= ch <= 36:
            raise ValueError("US UHF channels are 14-36")
        lo = 470000 + (ch - 14) * 6000
        return Exclusion(lo, lo + 6000, f"TV {ch} (US)")
    raise ValueError(region)


# ------------------------------------------------------------------ products
def imd_products(freqs: list[int], fifth: bool = False) -> list[tuple[int, int, tuple[int, ...]]]:
    """All IMD products of ``freqs`` as (freq_khz, order, source indices)."""
    out = []
    n = len(freqs)
    for i, j in itertools.permutations(range(n), 2):
        out.append((2 * freqs[i] - freqs[j], 3, (i, i, j)))
        if fifth:
            out.append((3 * freqs[i] - 2 * freqs[j], 5, (i, i, i, j, j)))
    for i, j in itertools.combinations(range(n), 2):
        for k in range(n):
            if k != i and k != j:
                out.append((freqs[i] + freqs[j] - freqs[k], 3, (i, j, k)))
    return out


def check(slots: list[Slot], exclusions: list[Exclusion] = (), fifth: bool = True,
          scan: tuple[np.ndarray, np.ndarray] | None = None,
          threshold_dbm: float | None = None) -> list[Conflict]:
    """Report every rule a set of assigned frequencies breaks."""
    idx = [i for i, s in enumerate(slots) if s.freq_khz]
    f = {i: slots[i].freq_khz for i in idx}
    out: list[Conflict] = []
    for i in idx:
        s = slots[i]
        if not s.lo_khz <= f[i] <= s.hi_khz:
            out.append(Conflict(i, "band", f"{mhz(f[i])} outside band {mhz(s.lo_khz)}-{mhz(s.hi_khz)}"))
        for e in exclusions:
            if e.lo_khz <= f[i] <= e.hi_khz:
                out.append(Conflict(i, "excluded", f"{mhz(f[i])} inside exclusion {e.label or ''} "
                                                   f"{mhz(e.lo_khz)}-{mhz(e.hi_khz)}"))
        if scan is not None and threshold_dbm is not None:
            lvl = scan_level_near(scan, f[i], 100)
            if lvl is not None and lvl > threshold_dbm:
                out.append(Conflict(i, "scan", f"scan shows {lvl:.1f} dBm near {mhz(f[i])}"))
    for a, b in itertools.combinations(idx, 2):
        need = max(slots[a].spacing.cc_khz, slots[b].spacing.cc_khz)
        if abs(f[a] - f[b]) < need:
            for x, y in ((a, b), (b, a)):
                out.append(Conflict(x, "carrier", f"only {abs(f[a]-f[b])} kHz from "
                                                  f"{slots[y].label} ({mhz(f[y])})"))
    freqs = [f[i] for i in idx]
    for prod, order, src in imd_products(freqs, fifth):
        srcs = {idx[k] for k in src}
        for v in idx:
            if v in srcs:
                continue
            sp = slots[v].spacing
            ntx = len(set(src))
            tol = sp.imd5_khz if order == 5 else (sp.imd3tx_khz if ntx == 3 else sp.imd3_khz)
            if tol and abs(prod - f[v]) < tol:
                names = " ".join(slots[idx[k]].label for k in sorted(set(src)))
                ordinal = "3rd" if order == 3 else "5th"
                out.append(Conflict(v, f"imd{order}",
                                    f"{ntx}-tx {ordinal}-order product {mhz(prod)} from [{names}] "
                                    f"is {abs(prod - f[v])} kHz away"))
    return out


def mhz(khz: int) -> str:
    return f"{khz / 1000:.3f}"


def scan_level_near(scan, freq_khz: int, half_bw_khz: int) -> float | None:
    fs, ls = scan
    lo = np.searchsorted(fs, freq_khz - half_bw_khz)
    hi = np.searchsorted(fs, freq_khz + half_bw_khz, side="right")
    if hi <= lo:
        # nearest point if the scan is coarser than the window
        if len(fs) == 0:
            return None
        k = int(np.clip(np.searchsorted(fs, freq_khz), 1, len(fs) - 1))
        return float(max(ls[k - 1], ls[k]))
    return float(ls[lo:hi].max())


# --------------------------------------------------------------- generator
class _Placed:
    """Placed carriers and their IMD products, kept incrementally.

    Every forbidden point is stored as (centre, tol, div).  For a candidate
    slot with tolerance t_s the forbidden window is |C - centre| < max(tol, t_s)/div.
    """

    def __init__(self, fifth: bool):
        self.fifth = fifth
        self.f: list[int] = []
        self.cc: list[int] = []
        self.i3: list[int] = []
        self.i5: list[int] = []
        self.i3t: list[int] = []
        # forbidden points: 2-tx 3rd, 2-tx 5th, 3-tx 3rd
        self.p3: list[tuple[np.ndarray, np.ndarray, float]] = []
        self.p5: list[tuple[np.ndarray, np.ndarray, float]] = []
        self.p3t: list[tuple[np.ndarray, np.ndarray, float]] = []

    def add(self, x: int, sp: SpacingProfile) -> None:
        f = np.array(self.f, dtype=np.int64)
        i3 = np.array(self.i3, dtype=np.int64)
        i5 = np.array(self.i5, dtype=np.int64)
        i3t = np.array(self.i3t, dtype=np.int64)
        if len(f):
            # 2-tx 3rd order involving x: 2x-p, 2p-x  (also covers victims)
            self.p3.append((2 * x - f, np.maximum(i3, sp.imd3_khz), 1.0))
            self.p3.append((2 * f - x, np.maximum(i3, sp.imd3_khz), 1.0))
            # the next candidate C must not make 2C - p land on a placed f
            # (or on x): C = (f + p) / 2, window halves
            self.p3.append(((x + f) / 2.0, np.maximum(i3, sp.imd3_khz), 2.0))
            if len(f) >= 2:
                a, b = np.triu_indices(len(f), 1)
                fa, fb = f[a], f[b]
                ta = np.maximum(np.maximum(i3t[a], i3t[b]), sp.imd3tx_khz)
                # 3-tx: x+p-q, p+q-x (covers any of the three, or a 4th, as victim)
                self.p3t.append((x + fa - fb, ta, 1.0))
                self.p3t.append((x + fb - fa, ta, 1.0))
                self.p3t.append((fa + fb - x, ta, 1.0))
            if self.fifth:
                t5 = np.maximum(i5, sp.imd5_khz)
                self.p5.append((3 * x - 2 * f, t5, 1.0))
                self.p5.append((3 * f - 2 * x, t5, 1.0))
                # candidate C with 3C - 2p = f  or 3p - 2C = f
                self.p5.append(((x + 2 * f) / 3.0, t5, 3.0))
                self.p5.append(((f + 2 * x) / 3.0, t5, 3.0))
                self.p5.append(((3 * x - f) / 2.0, t5, 2.0))
                self.p5.append(((3 * f - x) / 2.0, t5, 2.0))
        # products of x with itself and future candidates are covered at raster time
        self.f.append(x)
        self.cc.append(sp.cc_khz)
        self.i3.append(sp.imd3_khz)
        self.i5.append(sp.imd5_khz)
        self.i3t.append(sp.imd3tx_khz)

    def copy(self) -> "_Placed":
        q = _Placed(self.fifth)
        for name in ("f", "cc", "i3", "i5", "i3t", "p3", "p5", "p3t"):
            setattr(q, name, list(getattr(self, name)))
        return q

    def mask(self, grid: np.ndarray, sp: SpacingProfile, lo: int, step: int) -> np.ndarray:
        n = len(grid)
        bad = np.zeros(n + 1, dtype=np.int32)

        def mark(centres, tols):
            if len(centres) == 0:
                return
            a = np.ceil((centres - tols - lo) / step + 1e-9).astype(np.int64)
            b = np.floor((centres + tols - lo) / step - 1e-9).astype(np.int64) + 1
            a = np.clip(a, 0, n)
            b = np.clip(b, 0, n)
            ok = b > a
            np.add.at(bad, a[ok], 1)
            np.add.at(bad, b[ok], -1)

        if self.f:
            f = np.array(self.f, dtype=np.float64)
            mark(f, np.maximum(np.array(self.cc), sp.cc_khz).astype(np.float64))
        for store, t_s in ((self.p3, sp.imd3_khz), (self.p5, sp.imd5_khz),
                           (self.p3t, sp.imd3tx_khz)):
            for centres, tols, div in store:
                if len(centres) == 0:
                    continue
                eff = np.maximum(tols, t_s).astype(np.float64) / div
                mark(centres.astype(np.float64), eff)
        return np.cumsum(bad[:-1]) == 0


def _static_mask(slot: Slot, grid: np.ndarray, exclusions, scan, threshold_dbm) -> np.ndarray:
    ok = np.ones(len(grid), dtype=bool)
    for e in exclusions:
        ok &= ~((grid >= e.lo_khz) & (grid <= e.hi_khz))
    if scan is not None and threshold_dbm is not None:
        fs, ls = scan
        hot = fs[ls > threshold_dbm]
        if len(hot):
            pos = np.searchsorted(hot, grid)
            left = np.abs(grid - hot[np.clip(pos - 1, 0, len(hot) - 1)])
            right = np.abs(hot[np.clip(pos, 0, len(hot) - 1)] - grid)
            ok &= np.minimum(left, right) > 100
    return ok


def coordinate(slots: list[Slot], exclusions: list[Exclusion] = (), *,
               scan: tuple[np.ndarray, np.ndarray] | None = None,
               threshold_dbm: float | None = None, fifth: bool = True,
               time_limit: float = 5.0, max_attempts: int = 500,
               seed: int | None = None) -> CoordinationResult:
    """Find frequencies for every non-fixed slot.

    Fixed slots (``fixed`` and ``freq_khz`` set) are honoured as-is.  The
    first attempt is a deterministic first-fit; later attempts shuffle the
    order slightly and use a one-step look-ahead.  The best attempt (most
    slots placed) within ``time_limit`` seconds is returned.
    """
    rnd = random.Random(seed)
    t0 = time.monotonic()
    fixed = [i for i, s in enumerate(slots) if s.fixed and s.freq_khz]
    free = [i for i, s in enumerate(slots) if not (s.fixed and s.freq_khz)]

    grids = {}
    statics = {}
    for i in free:
        s = slots[i]
        step = s.spacing.step_khz
        lo = -(-s.lo_khz // step) * step
        grid = np.arange(lo, s.hi_khz + 1, step, dtype=np.int64)
        grids[i] = (grid, lo, step)
        statics[i] = _static_mask(s, grid, exclusions, scan, threshold_dbm)

    def order_key(i: int, jitter: float) -> tuple:
        sl = slots[i]
        width = (sl.hi_khz - sl.lo_khz) * (1 + jitter * rnd.random())
        # most constrained band first, then the more tolerant equipment first
        return (width, sl.spacing.imd3_khz + jitter * 100 * rnd.random())

    best: CoordinationResult | None = None
    attempt = 0
    while attempt < max_attempts:
        placed = _Placed(fifth)
        for i in fixed:
            placed.add(slots[i].freq_khz, slots[i].spacing)
        jitter = 0.0 if attempt == 0 else 0.3
        order = sorted(free, key=lambda i: order_key(i, jitter))
        result = CoordinationResult({i: slots[i].freq_khz for i in fixed})
        aborted = False
        for j, i in enumerate(order):
            grid, lo, step = grids[i]
            sp = slots[i].spacing
            cand = np.flatnonzero(statics[i] & placed.mask(grid, sp, lo, step))
            if len(cand) == 0:
                result.unplaced.append(i)
                continue
            pick = cand[0]  # attempt 0: plain first fit (low edge)
            if attempt and j + 1 < len(order):
                # look-ahead: try a few candidates, keep the one that leaves the
                # next slot the most room
                nxt = order[j + 1]
                ngrid, nlo, nstep = grids[nxt]
                nsp = slots[nxt].spacing
                k = min(6, len(cand))
                if attempt % 2:
                    tries = cand[:k]
                else:
                    tries = cand[np.unique(np.linspace(0, len(cand) - 1, k).astype(int))]
                best_free = -1
                for c in tries:
                    trial = placed.copy()
                    trial.add(int(grid[c]), sp)
                    free_n = int((statics[nxt] & trial.mask(ngrid, nsp, nlo, nstep)).sum())
                    if free_n > best_free:
                        best_free, pick = free_n, c
            x = int(grid[pick])
            result.freqs[i] = x
            placed.add(x, sp)
            if best is not None and time.monotonic() - t0 > time_limit:
                aborted = True
                break
        attempt += 1
        if not aborted and (best is None or len(result.unplaced) < len(best.unplaced)):
            best = result
        if not best.unplaced or time.monotonic() - t0 > time_limit:
            break
    best.attempts = attempt
    best.seconds = time.monotonic() - t0
    return best
