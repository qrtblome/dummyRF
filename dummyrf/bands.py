"""Which frequency band is a device in?

Some receivers report it (``RF_BAND``); a ULX-D does not — its command-string
interface has no band at all (checked on a ULXD4D, fw 2.7.6).  So the band is
resolved in this order:

1. what the device reports,
2. what the user chose for that device (saved in the show),
3. inferred from the channels' current frequencies: every band of the model
   that contains all of them.  Bands with the same edges (ULX-D G50 and G51,
   both 470–534 MHz) make the range certain even when the name is not.  When
   candidates have different edges the overlap is used — the device can
   certainly tune there — and the result is marked ambiguous.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .profiles import ModelProfile


@dataclass
class BandInfo:
    name: str                 # "G51", "G50/G51", "H51 or J50A", "" (unknown)
    lo_khz: int
    hi_khz: int
    source: str               # "device" | "manual" | "inferred" | "unknown"
    candidates: list[str] = field(default_factory=list)

    @property
    def ambiguous(self) -> bool:
        return self.source == "inferred" and " or " in self.name

    def label(self) -> str:
        if self.source == "unknown":
            return "band ?"
        tag = {"device": "", "manual": " (set)", "inferred": " (guessed)"}[self.source]
        return f"{self.name}{tag}"

    def describe(self) -> str:
        rng = f"{self.lo_khz / 1000:g}–{self.hi_khz / 1000:g} MHz"
        if self.source == "unknown":
            return f"band unknown — using the model's whole range {rng}"
        if self.ambiguous:
            return f"band {self.name}? — guessed from the frequencies; using the overlap {rng}"
        if self.source == "inferred":
            return f"band {self.name} ({rng}), guessed from the channel frequencies"
        return f"band {self.name} ({rng})" + (" — set by you" if self.source == "manual" else "")


def _span(profile: ModelProfile) -> tuple[int, int]:
    return (min(lo for lo, _ in profile.bands.values()),
            max(hi for _, hi in profile.bands.values()))


def resolve_band(profile: ModelProfile, reported: str | None = None, manual: str = "",
                 freqs_khz: list[int | None] | None = None) -> BandInfo:
    bands = profile.bands
    if not bands:
        return BandInfo("", 0, 0, "unknown")
    if reported and reported in bands:
        lo, hi = bands[reported]
        return BandInfo(reported, lo, hi, "device")
    if manual and manual in bands:
        lo, hi = bands[manual]
        return BandInfo(manual, lo, hi, "manual")
    fs = [f for f in (freqs_khz or []) if f]
    lo_all, hi_all = _span(profile)
    if not fs:
        return BandInfo("", lo_all, hi_all, "unknown")
    cands = [n for n, (lo, hi) in bands.items() if all(lo <= f <= hi for f in fs)]
    if not cands:
        return BandInfo("", lo_all, hi_all, "unknown")
    ranges = {bands[n] for n in cands}
    if len(ranges) == 1:
        lo, hi = next(iter(ranges))
        return BandInfo("/".join(sorted(cands)), lo, hi, "inferred", cands)
    lo = max(bands[n][0] for n in cands)
    hi = min(bands[n][1] for n in cands)
    return BandInfo(" or ".join(sorted(cands, key=lambda n: bands[n])), lo, hi, "inferred", cands)
