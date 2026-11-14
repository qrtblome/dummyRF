"""Rough received-level prediction for broadcast transmitters at a venue.

A simple model on purpose — no terrain data, no clutter maps:

* **Okumura-Hata** (urban / suburban / open area) — empirical, built for
  150–1500 MHz, 1–20 km, transmitter 30–200 m, receiver 1–10 m.  Outside those
  ranges the formula is used anyway with heights clamped; treat the result as
  an estimate.  Below 1 km the loss blends smoothly into free space.
* **Free space** — the worst case (highest level); no obstacles at all.
* Beyond the radio horizon (4.12·(√h_tx + √h_rx) km) an extra loss is added.

Inputs come from the UKE register: ERP (kW, relative to a half-wave dipole),
antenna height above ground, centre frequency and — for directional antennas —
the attenuation every 10° of azimuth.  The result is the channel power in dBm
at a 0 dBi receiving antenna, i.e. roughly what a wireless-mic receiver's
antenna sees across the whole TV/DAB channel.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

ENVIRONMENTS = {
    "urban": "Urban (city centre)",
    "suburban": "Suburban / town",
    "open": "Open area / countryside",
    "free": "Free space (worst case)",
}
DIPOLE_DBI = 2.15
DEFAULT_TX_HEIGHT_M = 50.0


@dataclass
class ModelSettings:
    environment: str = "suburban"
    rx_height_m: float = 3.0          # mic receiver antenna above ground
    extra_loss_db: float = 0.0        # e.g. 10–15 dB for an indoor venue
    use_patterns: bool = True         # apply directional antenna patterns from the register
    exclude_above_dbm: float = -85.0  # tick for exclusion at or above this level
    adjacent_above_dbm: float = -55.0 # also exclude the neighbouring channels


# ------------------------------------------------------------------ geometry
def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing from point 1 to point 2, degrees clockwise from north."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def pattern_loss_db(pattern_db: list[float] | None, azimuth_deg: float) -> float:
    """Attenuation of a directional antenna towards ``azimuth`` (36 values, 10° apart)."""
    if not pattern_db or len(pattern_db) != 36:
        return 0.0
    a = azimuth_deg % 360.0
    i = int(a // 10) % 36
    frac = (a - i * 10) / 10.0
    return pattern_db[i] * (1 - frac) + pattern_db[(i + 1) % 36] * frac


# ------------------------------------------------------------------ path loss
def free_space_loss_db(f_mhz: float, d_km: float) -> float:
    return 32.45 + 20 * math.log10(f_mhz) + 20 * math.log10(max(d_km, 0.001))


def hata_loss_db(f_mhz: float, d_km: float, h_tx: float, h_rx: float, environment: str) -> float:
    """Okumura-Hata median path loss (small/medium city receiver correction)."""
    f = min(max(f_mhz, 150.0), 1500.0)
    ht = min(max(h_tx, 30.0), 200.0)
    hr = min(max(h_rx, 1.0), 10.0)
    lf = math.log10(f)
    a_hr = (1.1 * lf - 0.7) * hr - (1.56 * lf - 0.8)
    urban = (69.55 + 26.16 * lf - 13.82 * math.log10(ht) - a_hr
             + (44.9 - 6.55 * math.log10(ht)) * math.log10(d_km))
    if environment == "urban":
        return urban
    if environment == "suburban":
        return urban - 2 * math.log10(f / 28.0) ** 2 - 5.4
    return urban - 4.78 * lf ** 2 + 18.33 * lf - 40.94          # open area


def path_loss_db(f_mhz: float, d_km: float, h_tx: float, h_rx: float, environment: str) -> float:
    d_km = max(d_km, 0.01)
    fs = free_space_loss_db(f_mhz, d_km)
    if environment == "free":
        loss = fs
    elif d_km >= 1.0:
        loss = max(fs, hata_loss_db(f_mhz, d_km, h_tx, h_rx, environment))
    else:
        # Hata starts at 1 km: blend from free space at 0 km to Hata at 1 km
        excess_1km = max(0.0, hata_loss_db(f_mhz, 1.0, h_tx, h_rx, environment)
                         - free_space_loss_db(f_mhz, 1.0))
        loss = fs + excess_1km * d_km
    horizon = 4.12 * (math.sqrt(max(h_tx, 1.0)) + math.sqrt(max(h_rx, 1.0)))
    if d_km > horizon:
        loss += 10.0 + 0.5 * (d_km - horizon)       # crude diffraction penalty
    return loss


# ------------------------------------------------------------------ level
def received_dbm(erp_kw: float, f_mhz: float, d_km: float, h_tx_m: float | None,
                 settings: ModelSettings, pattern_db: list[float] | None = None,
                 azimuth_deg: float | None = None) -> float:
    """Channel power at a 0 dBi antenna at the venue."""
    if not erp_kw or erp_kw <= 0:
        return -math.inf
    eirp_dbm = 10 * math.log10(erp_kw * 1e3) + 30 + DIPOLE_DBI
    h_tx = h_tx_m if h_tx_m and h_tx_m > 0 else DEFAULT_TX_HEIGHT_M
    level = eirp_dbm - path_loss_db(f_mhz, d_km, h_tx, settings.rx_height_m, settings.environment)
    if settings.use_patterns and azimuth_deg is not None:
        level -= pattern_loss_db(pattern_db, azimuth_deg)
    return level - settings.extra_loss_db


def verdict(level_dbm: float, s: ModelSettings) -> str:
    if level_dbm >= s.adjacent_above_dbm:
        return "very strong — exclude channel and neighbours"
    if level_dbm >= s.exclude_above_dbm:
        return "receivable — exclude channel"
    if level_dbm >= s.exclude_above_dbm - 10:
        return "weak — usable with care"
    return "negligible"
