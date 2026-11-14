"""Live RF scanning.

Backends
--------
* ``RtlPowerScanner`` — RTL-SDR through ``rtl_power`` (from ``brew install
  librtlsdr``).  rtl_power hops across the range and prints one CSV line per
  hop::

      2026-11-14, 15:00:01, 470000000, 472400000, 25000.00, 20, -42.1, -41.8, ...
      date        time      Hz low     Hz high    Hz step  samples  dB values...

  A sweep is complete when the hops wrap back to the start frequency.
* ``SimulatedScanner`` — synthetic sweeps for working without hardware.

Levels
------
rtl_power reports uncalibrated dB relative to the ADC full scale, not dBm.
``level_offset_db`` is added to every value; set it once against a known
signal (or a calibrated scanner) to get roughly dBm.

Every complete sweep goes to ``on_sweep(freqs_khz, levels_dbm)``.  The
``PeakHold`` helper keeps the running maximum.
"""
from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

SweepCallback = Callable[[np.ndarray, np.ndarray], None]

RTL_POWER_PATHS = ["/opt/homebrew/bin/rtl_power", "/usr/local/bin/rtl_power"]
RTL_MAX_KHZ = 1_766_000
RTL_MIN_KHZ = 24_000


def find_rtl_power() -> str | None:
    found = shutil.which("rtl_power")
    if found:
        return found
    return next((p for p in RTL_POWER_PATHS if Path(p).exists()), None)


# ------------------------------------------------------------------ parsing
@dataclass
class Hop:
    timestamp: str
    hz_low: float
    hz_high: float
    hz_step: float
    values: np.ndarray

    @property
    def freqs_hz(self) -> np.ndarray:
        return self.hz_low + np.arange(len(self.values)) * self.hz_step


def parse_rtl_power_line(line: str) -> Hop | None:
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 7:
        return None
    try:
        lo, hi, step = float(parts[2]), float(parts[3]), float(parts[4])
        vals = np.array([float(v) if v not in ("nan", "-nan", "") else np.nan for v in parts[6:]])
    except ValueError:
        return None
    if step <= 0 or hi <= lo:
        return None
    return Hop(f"{parts[0]} {parts[1]}", lo, hi, step, vals)


class SweepAssembler:
    """Collects rtl_power hops into complete sweeps."""

    def __init__(self, start_khz: float, stop_khz: float):
        self.start_khz, self.stop_khz = start_khz, stop_khz
        self._f: list[np.ndarray] = []
        self._v: list[np.ndarray] = []
        self._last_lo: float | None = None

    def feed(self, hop: Hop) -> tuple[np.ndarray, np.ndarray] | None:
        out = None
        # hops restart at the low end: whatever we collected is a full sweep
        if self._last_lo is not None and hop.hz_low <= self._last_lo:
            out = self._finish()
        self._last_lo = hop.hz_low
        self._f.append(hop.freqs_hz / 1000.0)
        self._v.append(hop.values)
        return out

    def flush(self) -> tuple[np.ndarray, np.ndarray] | None:
        return self._finish()

    def _finish(self):
        if not self._f:
            return None
        f = np.concatenate(self._f)
        v = np.concatenate(self._v)
        self._f, self._v = [], []
        keep = (f >= self.start_khz) & (f <= self.stop_khz) & ~np.isnan(v)
        f, v = f[keep], v[keep]
        order = np.argsort(f, kind="stable")
        return (f[order], v[order]) if len(f) else None


class PeakHold:
    def __init__(self):
        self.freqs: np.ndarray | None = None
        self.levels: np.ndarray | None = None
        self.sweeps = 0

    def reset(self):
        self.freqs = self.levels = None
        self.sweeps = 0

    def add(self, freqs: np.ndarray, levels: np.ndarray) -> None:
        if self.freqs is None or len(self.freqs) != len(freqs) or not np.array_equal(self.freqs, freqs):
            # first sweep, or the layout changed (new range): put it on this grid
            if self.freqs is not None and len(self.freqs):
                levels = np.fmax(levels, np.interp(freqs, self.freqs, self.levels,
                                                   left=-np.inf, right=-np.inf))
            self.freqs, self.levels = freqs.copy(), levels.copy()
        else:
            self.levels = np.fmax(self.levels, levels)
        self.sweeps += 1


# ------------------------------------------------------------------ backends
@dataclass
class ScanSettings:
    start_khz: int = 470_000
    stop_khz: int = 700_000
    step_khz: int = 25
    gain_db: float | None = None        # None = automatic gain
    integration_s: float = 1.0
    level_offset_db: float = 0.0
    ppm: int = 0

    def validate(self) -> None:
        if not RTL_MIN_KHZ <= self.start_khz < self.stop_khz <= RTL_MAX_KHZ:
            raise ValueError(f"RTL-SDR range is {RTL_MIN_KHZ / 1000:g}–{RTL_MAX_KHZ / 1000:g} MHz "
                             "and start must be below stop")
        if self.step_khz <= 0:
            raise ValueError("resolution must be positive")


class Scanner:
    name = "scanner"

    def __init__(self, settings: ScanSettings, on_sweep: SweepCallback,
                 on_status: Callable[[str], None] | None = None):
        self.settings = settings
        self.on_sweep = on_sweep
        self.on_status = on_status or (lambda s: None)
        self.running = False

    async def run(self) -> None:
        raise NotImplementedError

    async def stop(self) -> None:
        self.running = False

    def _emit(self, sweep) -> None:
        if sweep is None:
            return
        f, v = sweep
        self.on_sweep(f, v + self.settings.level_offset_db)


class RtlPowerScanner(Scanner):
    name = "RTL-SDR (rtl_power)"

    def __init__(self, *a, binary: str | None = None, device_index: int = 0, **kw):
        super().__init__(*a, **kw)
        self.binary = binary or find_rtl_power()
        self.device_index = device_index
        self._proc: asyncio.subprocess.Process | None = None

    def command(self) -> list[str]:
        s = self.settings
        cmd = [self.binary, "-f", f"{s.start_khz}k:{s.stop_khz}k:{s.step_khz}k",
               "-i", f"{s.integration_s:g}", "-c", "0.2", "-d", str(self.device_index)]
        if s.gain_db is not None:
            cmd += ["-g", f"{s.gain_db:g}"]
        if s.ppm:
            cmd += ["-p", str(s.ppm)]
        return cmd + ["-"]

    async def run(self) -> None:
        self.settings.validate()
        if not self.binary:
            raise FileNotFoundError("rtl_power not found — install it with: brew install librtlsdr")
        self._proc = await asyncio.create_subprocess_exec(
            *self.command(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        self.running = True
        stderr_tail: list[str] = []

        async def read_stderr():
            async for raw in self._proc.stderr:
                line = raw.decode(errors="replace").strip()
                if line:
                    stderr_tail.append(line)
                    del stderr_tail[:-20]
                    if any(k in line for k in ("Found", "Tuner", "Using device", "No supported")):
                        self.on_status(line)
        err_task = asyncio.create_task(read_stderr())
        asm = SweepAssembler(self.settings.start_khz, self.settings.stop_khz)
        self.on_status("Scanning…")
        try:
            async for raw in self._proc.stdout:
                hop = parse_rtl_power_line(raw.decode(errors="replace"))
                if hop is not None:
                    self._emit(asm.feed(hop))
            rc = await self._proc.wait()
            await asyncio.wait_for(err_task, 1)
            if self.running and rc != 0:
                hint = "\n".join(stderr_tail[-4:]) or f"rtl_power exited with code {rc}"
                raise RuntimeError(hint)
        finally:
            self.running = False
            err_task.cancel()
            if self._proc.returncode is None:
                self._proc.terminate()
                try:
                    await asyncio.wait_for(self._proc.wait(), 3)
                except asyncio.TimeoutError:
                    self._proc.kill()

    async def stop(self) -> None:
        self.running = False
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()


class SimulatedScanner(Scanner):
    """Noise floor, a few TV multiplexes and some intermittent carriers."""
    name = "Simulated scanner"

    def __init__(self, *a, seed: int = 7, sweep_s: float = 0.4, **kw):
        super().__init__(*a, **kw)
        self.rng = np.random.default_rng(seed)
        self.sweep_s = sweep_s
        s = self.settings
        self.freqs = np.arange(s.start_khz, s.stop_khz + 1, s.step_khz, dtype=np.float64)
        base = np.full(len(self.freqs), -102.0)
        for c0 in (482000, 522000, 562000, 626000):       # DVB-T muxes, 8 MHz wide
            m = (self.freqs >= c0 - 3800) & (self.freqs <= c0 + 3800)
            base[m] = -62.0
        self.base = base
        self.carriers = self.rng.uniform(s.start_khz, s.stop_khz, 8)

    async def run(self) -> None:
        self.settings.validate()
        self.running = True
        self.on_status("Scanning (simulated)…")
        try:
            while self.running:
                lv = self.base + self.rng.normal(0, 2.0, len(self.freqs))
                for c in self.carriers:
                    if self.rng.random() < 0.35:   # appears now and then
                        lv = np.maximum(lv, -40 - 15 * self.rng.random()
                                        - ((self.freqs - c) / 25.0) ** 2)
                self._emit((self.freqs.copy(), lv))
                await asyncio.sleep(self.sweep_s)
        finally:
            self.running = False


BACKENDS = {"rtl": RtlPowerScanner, "sim": SimulatedScanner}
