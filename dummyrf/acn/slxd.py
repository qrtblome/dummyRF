"""SLX-D RF scan over ACN.

Property map worked out from captures of Wireless Workbench scanning with an
SLXD4D (fw from 2026), verified against the scan WWB saved (identical values):

    capability   0x01070201  lowest tunable frequency, kHz   (470000)
                 0x01070202  tuning step, kHz                (25)
    channel n    base 0x02000000 + (n-1) * 0x10000
      +0x804  current frequency, kHz
      +0x900  scan start, kHz          +0x901  scan stop, kHz
      +0x902  scan step, kHz           +0x909  scan option (WWB writes 1)
      +0x903  start trigger (WWB writes 0)
      +0x910  progress %   +0x920  completed   +0x931  running
      +0x90e  acknowledge after completion (WWB writes 0)
      +0x90f  scan state — 0 before WWB starts a scan; 255 while the receiver
              would not start one (seen with WWB open / after an aborted scan)
    results      EVENTs, 4-byte address + range array:
                 address 0x10000000 + (f - lowest) / step, 17 × int16 dBm each

The scanning channel is off its frequency for the whole scan (as in WWB); the
receiver tunes back by itself when it finishes.
"""
from __future__ import annotations

import asyncio
import struct
import time
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from . import wire as w
from .session import AcnError, AcnSession

CAP_MIN_KHZ = 0x01070201
CAP_STEP_KHZ = 0x01070202
DATA_BASE = 0x10000000
ALL_EVENTS = 0x1FFFFFFF        # WWB subscribes to this before starting a scan

FREQ, START, STOP, STEP, OPTION, TRIGGER = 0x804, 0x900, 0x901, 0x902, 0x909, 0x903
PROGRESS, COMPLETED, RUNNING, CLEAR = 0x910, 0x920, 0x931, 0x90E
STATE = 0x90F   # WWB reads this before every scan and only goes ahead on 0


def ch_base(channel: int) -> int:
    return 0x02000000 + (channel - 1) * 0x10000


@dataclass
class ScanResult:
    freqs_khz: np.ndarray
    levels_dbm: np.ndarray
    complete: bool = False
    progress: int = 0
    extra: dict = field(default_factory=dict)


class SlxdScan:
    """Run one scan on one channel of an open session."""

    def __init__(self, session: AcnSession, channel: int, start_khz: int, stop_khz: int,
                 step_khz: int = 25,
                 on_data: Callable[[ScanResult], None] | None = None,
                 on_progress: Callable[[int], None] | None = None):
        self.s, self.channel = session, channel
        self.start_khz, self.stop_khz, self.step_khz = start_khz, stop_khz, step_khz
        self.on_data, self.on_progress = on_data, on_progress
        self.base_khz = 470000
        self.tune_step = 25
        self.result: ScanResult | None = None
        self._done = asyncio.Event()
        self._running_seen = False
        self._started = asyncio.Event()
        self._last_event = 0.0

    START_TIMEOUT_S = 10.0     # trigger -> "running"/progress
    SILENCE_TIMEOUT_S = 30.0   # longest gap between updates during a scan

    def _event(self, item: w.DmpItem) -> None:
        b = ch_base(self.channel)
        self._last_event = time.monotonic()
        if item.address >> 24 == DATA_BASE >> 24 and item.count > 1 and self.result is not None:
            first = self.base_khz + (item.address - DATA_BASE) * self.tune_step
            for k in range(item.count):
                f = first + k * item.increment * self.tune_step
                i = (f - self.start_khz) // self.step_khz
                if 0 <= i < len(self.result.freqs_khz) and 2 * k + 2 <= len(item.value):
                    self.result.levels_dbm[i] = w.int_value(item.value[2 * k:2 * k + 2], signed=True)
            if self.on_data:
                self.on_data(self.result)
        elif item.address == b + PROGRESS and item.value:
            p = item.value[-1]
            if p:
                self._started.set()
            if self.result is not None:
                self.result.progress = p
            if self.on_progress:
                self.on_progress(p)
        elif item.address == b + RUNNING and item.value:
            if item.value[-1]:
                self._running_seen = True
                self._started.set()
            elif self._running_seen:
                self._done.set()
        elif item.address == b + COMPLETED and item.value and item.value[-1]:
            self._done.set()

    async def run(self, timeout_s: float = 600.0) -> ScanResult:
        s, b = self.s, ch_base(self.channel)
        prev_handler = s.on_event
        s.on_event = self._event
        try:
            state = await s.get_int(b + STATE)
            if state != 0:
                # 255 after a scan that wasn't acknowledged (and it can come back on its
                # own); WWB's post-scan acknowledgement brings it back to 0
                s.set([(b + CLEAR, b"\x00")])
                await asyncio.sleep(0.5)
                state = await s.get_int(b + STATE)
            if state != 0:
                raise AcnError(f"the receiver is not ready to scan (scan state {state}; WWB sees 0 "
                               "before it starts a scan). Close Wireless Workbench or wait for a "
                               "running scan to finish, then try again.")
            self.base_khz = await s.get_int(CAP_MIN_KHZ)
            self.tune_step = await s.get_int(CAP_STEP_KHZ) or 25
            n = (self.stop_khz - self.start_khz) // self.step_khz + 1
            freqs = self.start_khz + np.arange(n) * self.step_khz
            self.result = ScanResult(freqs.astype(np.float64), np.full(n, np.nan))
            await s.subscribe([b + PROGRESS, b + COMPLETED, b + RUNNING])
            s.set([(b + START, struct.pack("!I", self.start_khz)),
                   (b + STOP, struct.pack("!I", self.stop_khz)),
                   (b + STEP, struct.pack("!I", self.step_khz)),
                   (b + OPTION, b"\x01")])
            await s.subscribe([ALL_EVENTS])
            loop = asyncio.get_running_loop()
            s.set([(b + TRIGGER, b"\x00")])
            self._last_event = time.monotonic()
            try:
                await asyncio.wait_for(self._started.wait(), self.START_TIMEOUT_S)
            except asyncio.TimeoutError as e:
                raise AcnError("the receiver did not start the scan "
                               f"(no 'running' report within {self.START_TIMEOUT_S:.0f} s)") from e
            end = loop.time() + timeout_s
            while not self._done.is_set():
                if loop.time() > end:
                    raise AcnError("the scan did not finish in time")
                if time.monotonic() - self._last_event > self.SILENCE_TIMEOUT_S:
                    raise AcnError(f"the receiver stopped reporting for {self.SILENCE_TIMEOUT_S:.0f} s "
                                   "during the scan")
                try:
                    await asyncio.wait_for(self._done.wait(), 1.0)
                except asyncio.TimeoutError:
                    pass
            self.result.complete = True
            s.set([(b + CLEAR, b"\x00")])          # as WWB does once the scan is done
            return self.result
        finally:
            s.on_event = prev_handler
            try:
                s.unsubscribe([ALL_EVENTS])
            except AcnError:
                pass
