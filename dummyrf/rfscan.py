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

from .drivers.base import RefusedError

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


class ReceiverSweepScanner(Scanner):
    """Scan with a wireless receiver channel: retune it step by step across the
    range and read its RF level at each step.

    Shure receivers have no scan command over the network (the scan WWB runs
    uses Shure's private protocol), but a ULX-D reports ``RX_RF_LVL`` for the
    frequency a channel is tuned to.  The channel is off its frequency for the
    whole sweep — its audio drops out — and its original frequency is put back
    when the sweep ends, however it ends.  Resolution is the receiver's channel
    filter (~200 kHz); it is slow (~0.4 s per step on a ULX-D).
    """
    name = "Receiver sweep"

    def __init__(self, settings: ScanSettings, on_sweep: SweepCallback,
                 on_status: Callable[[str], None] | None = None, *, driver, channel: int,
                 settle_s: float = 0.2, continuous: bool = True):
        super().__init__(settings, on_sweep, on_status)
        self.driver = driver
        self.channel = channel
        self.settle_s = settle_s
        self.continuous = continuous
        self.original_khz: int | None = None
        self.restored = False
        self.refused: list[int] = []          # frequencies the receiver would not tune to

    def grid(self) -> np.ndarray:
        s = self.settings
        step = max(25, int(round(s.step_khz / 25)) * 25)        # receivers tune in 25 kHz steps
        lo = -(-s.start_khz // 25) * 25
        return np.arange(lo, s.stop_khz + 1, step, dtype=np.float64)

    def check(self) -> None:
        prof = self.driver.profile
        pf = prof.param("frequency") if prof else None
        pl = prof.param("rf_level") if prof else None
        if not (pf and pf.writable and pl):
            raise RuntimeError(f"{prof.model if prof else 'this device'} can't sweep: "
                               "it needs a tunable frequency and an RF level readout")
        if len(self.grid()) < 2:
            raise ValueError("the range must cover at least two steps")

    async def run(self) -> None:
        self.check()
        drv, ch = self.driver, self.channel
        freqs = self.grid()
        # Before touching the frequency: can this receiver report its RF level at
        # all?  (An SLX-D rejects RX_RF_LVL — it must not be retuned for nothing.)
        try:
            if await drv.read(ch, "rf_level") is None:
                raise RuntimeError("no value")
        except Exception as e:  # noqa: BLE001
            prof = drv.profile
            raise RuntimeError(f"{prof.model if prof else 'this receiver'} doesn't report its RF "
                               f"level ({e}) — it can't be used for a sweep. "
                               "Its frequency was not changed.") from e
        self.original_khz = await drv.read(ch, "frequency")
        self.running = True
        sweeps = 0
        try:
            while self.running:
                levels = np.full(len(freqs), np.nan)
                for i, f in enumerate(freqs):
                    if not self.running:
                        break
                    try:
                        await drv.set(ch, "frequency", int(f))
                    except RefusedError:
                        # the receiver can't tune there (e.g. 470.000 on a ULX-D G50):
                        # leave the point blank and carry on
                        self.refused.append(int(f))
                        if len(self.refused) > len(freqs) // 2:
                            raise RuntimeError(f"the receiver refused {len(self.refused)} of "
                                               f"{len(freqs)} frequencies — is the range inside "
                                               "its band?")
                        continue
                    await asyncio.sleep(self.settle_s)
                    lvl = await drv.read(ch, "rf_level")
                    levels[i] = np.nan if lvl is None else float(lvl)
                    if i % 4 == 0 or i == len(freqs) - 1:
                        self.on_status(f"Sweep {sweeps + 1}: {f / 1000:.3f} MHz ({i + 1}/{len(freqs)})")
                        self._emit((freqs, levels.copy()))        # live: unmeasured points stay blank
                sweeps += 1
                if not self.continuous:
                    break
        finally:
            self.running = False
            if self.original_khz:
                for _ in range(3):           # put the channel back, whatever happened
                    try:
                        await drv.set(ch, "frequency", int(self.original_khz))
                        self.restored = True
                        break
                    except Exception:  # noqa: BLE001
                        await asyncio.sleep(0.5)
                self.on_status(f"Channel back on {self.original_khz / 1000:.3f} MHz"
                               if self.restored else
                               f"⚠ could not restore {self.original_khz / 1000:.3f} MHz — check the receiver!")


class SlxdAcnScanner(Scanner):
    """SLX-D's own RF scan, run over ACN like Wireless Workbench does it.

    The receiver is found by its SLP announcement (session port + CID), a
    session is opened, the scan runs on one channel — which is off its
    frequency meanwhile; the receiver tunes back by itself when done — and
    the results stream in as the receiver measures them.
    """
    name = "SLX-D scan (native)"

    def __init__(self, settings: ScanSettings, on_sweep: SweepCallback,
                 on_status: Callable[[str], None] | None = None, *, host: str, channel: int,
                 announce_s: float = 9.0, sdt_port: int | None = None, cid: str | None = None):
        super().__init__(settings, on_sweep, on_status)
        self.host, self.channel = host, channel
        self.announce_s = announce_s
        self.sdt_port, self.cid = sdt_port, cid
        self.session = None
        self.result = None
        self.completed = False

    async def run(self) -> None:
        from .acn import discovery
        from .acn.session import AcnSession
        from .acn.slxd import SlxdScan

        self.running = True
        s = self.settings
        try:
            if not (self.sdt_port and self.cid):
                self.on_status("Looking for the receiver's ACN announcement…")
                ann = await discovery.find(self.host, self.announce_s)
                if ann is None:
                    raise RuntimeError(f"no ACN announcement from {self.host} — is it on this network "
                                       "(and not blocked by the Mac's Local Network setting)?")
                self.sdt_port, self.cid = ann.sdt_port, ann.cid
            self.on_status(f"Opening a session with {self.host}…")
            self.session = AcnSession(self.host, self.sdt_port, self.cid)
            await self.session.connect()

            def data(res):
                self.result = res
                self._emit((res.freqs_khz, res.levels_dbm.copy()))

            def progress(pct):
                self.on_status(f"Receiver scanning channel {self.channel}: {pct} %")

            step = max(25, int(round(s.step_khz / 25)) * 25)
            scan = SlxdScan(self.session, self.channel, s.start_khz, s.stop_khz, step,
                            on_data=data, on_progress=progress)
            self.on_status(f"Receiver scanning channel {self.channel}: starting…")
            task = asyncio.create_task(scan.run())
            while not task.done():
                if not self.running:          # Stop pressed: leave; the receiver finishes on its own
                    task.cancel()
                    break
                await asyncio.sleep(0.2)
            if task.done() and not task.cancelled():
                self.result = task.result()
                self.completed = True
                self._emit((self.result.freqs_khz, self.result.levels_dbm.copy()))
        finally:
            self.running = False
            if self.session is not None:
                await self.session.close()


BACKENDS = {"rtl": RtlPowerScanner, "sim": SimulatedScanner}
