"""Protocol simulators for Shure and Sennheiser G4 devices.

They speak the same wire format as the drivers expect, so the whole app can be
developed and demoed without hardware.  They are *not* a reference for real
device behaviour — they are built from the same model profiles as the drivers.
"""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field

from ..drivers.base import decode_value, encode_value
from ..drivers.shure import format_command, parse_message
from ..profiles import (AUDIO_LEVEL, BATTERY, BOOL, ENUM, FREQUENCY, GROUP_CHANNEL, INT, NAME,
                        RF_LEVEL, STR, ALL_MODELS, ModelProfile, Param)

LIVE_KEYS = {BATTERY, RF_LEVEL, AUDIO_LEVEL}


def _initial(p: Param, ch: int, band: tuple[int, int], rnd: random.Random):
    if p.key == NAME:
        return f"CH {ch}"
    if p.key == FREQUENCY:
        lo, hi = band
        return lo + 25 * rnd.randrange((hi - lo) // 25)
    if p.key == GROUP_CHANNEL:
        return "--,--"
    if p.kind == BOOL:
        return False
    if p.kind == ENUM:
        return p.choices[len(p.choices) // 2]
    if p.kind == INT:
        return max(p.lo if p.lo is not None else 0, min(0, p.hi if p.hi is not None else 0))
    if p.kind == STR:
        return ""
    return 0


def _live(p: Param, rnd: random.Random):
    if p.key == BATTERY:
        return rnd.choice([3, 4, 5])
    if p.key == RF_LEVEL:
        return rnd.randint(-85, -45)
    if p.key == AUDIO_LEVEL:
        return rnd.randint(-45, -10)
    return 0


# A fixed little RF world for the simulators, so a receiver sweep finds something:
# DVB-T multiplexes (8 MHz wide) and two narrow carriers on a -100 dBm floor.
SIM_TV_MUXES_KHZ = [(490000, -66.0), (522000, -58.0), (562000, -70.0), (626000, -62.0)]
SIM_CARRIERS_KHZ = [(505000, -48.0), (590000, -44.0)]


def sim_rf_dbm(freq_khz: float, rnd: random.Random | None = None) -> float:
    """RF level a simulated receiver measures when tuned to ``freq_khz``."""
    lvl = -100.0
    for centre, peak in SIM_TV_MUXES_KHZ:
        if abs(freq_khz - centre) <= 3900:
            lvl = max(lvl, peak)
    for centre, peak in SIM_CARRIERS_KHZ:
        lvl = max(lvl, peak - ((freq_khz - centre) / 60.0) ** 2)   # ~200 kHz receiver filter
    return lvl + (rnd.gauss(0, 1.0) if rnd else 0.0)


@dataclass
class SimDevice:
    profile: ModelProfile
    band: str
    device_id: str = ""
    values: dict[tuple[int, str], object] = field(default_factory=dict)
    rnd: random.Random = field(default_factory=random.Random)

    def __post_init__(self):
        lo_hi = self.profile.bands[self.band]
        for ch in range(1, self.profile.channels + 1):
            for p in self.profile.params:
                self.values[(ch, p.key)] = _initial(p, ch, lo_hi, self.rnd)
        self.values[(0, "model")] = self.profile.model
        self.values[(0, "device_id")] = self.device_id or f"SIM-{self.profile.model}"
        self.values[(0, "band")] = self.band
        self.values[(0, "fw_ver")] = "0.0.0-sim"

    def get(self, ch: int, p: Param):
        if p.key == RF_LEVEL and (ch, FREQUENCY) in self.values:
            # what the receiver hears at the frequency it is tuned to
            return int(round(sim_rf_dbm(self.values[(ch, FREQUENCY)], self.rnd)))
        if p.key in LIVE_KEYS:
            return _live(p, self.rnd)
        return self.values.get((ch, p.key))

    def set(self, ch: int, p: Param, value) -> bool:
        if not p.writable:
            return False
        if p.key == FREQUENCY:
            lo, hi = self.profile.bands[self.band]
            if not lo <= value <= hi or value % 25:
                return False
        if p.kind == INT and value is not None:
            if (p.lo is not None and value < p.lo) or (p.hi is not None and value > p.hi):
                return False
        if value is None:
            return False
        self.values[(ch, p.key)] = value
        return True


# --------------------------------------------------------------------- Shure
class ShureSimServer:
    # A real ULXD4D takes ~0.09 s per command and works through them one at a time.
    def __init__(self, dev: SimDevice, host: str = "127.0.0.1", port: int = 2202,
                 delay: float = 0.09):
        self.dev, self.host, self.port, self.delay = dev, host, port, delay
        self._server: asyncio.base_events.Server | None = None
        self._clients: set[asyncio.StreamWriter] = set()

    def _lookup(self, ch: int, cmd: str) -> Param | None:
        params = self.dev.profile.device_params if ch == 0 else self.dev.profile.params
        return next((p for p in params if p.cmd == cmd), None)

    def _rep(self, ch: int, p: Param) -> str:
        val = self.dev.get(ch, p)
        raw = encode_value(p, val) if val is not None else ""
        if p.kind == STR:
            raw = "{" + raw.ljust(p.width or len(raw)) + "}"
        return format_command("REP", ch, p.cmd, raw)

    def handle(self, msg: str) -> list[str]:
        parsed = parse_message(msg)
        if not parsed:
            return ["< REP ERR >"]
        verb, ch, cmd, value = parsed
        nch = self.dev.profile.channels
        if cmd == "ALL" and verb == "GET":
            out = []
            if ch == 0:
                out += [self._rep(0, p) for p in self.dev.profile.device_params]
                chans = range(1, nch + 1)
            else:
                chans = [ch]
            for c in chans:
                out += [self._rep(c, p) for p in self.dev.profile.params]
            return out
        if ch > nch:
            return ["< REP ERR >"]
        p = self._lookup(ch, cmd)
        if p is None:
            return ["< REP ERR >"]
        if verb == "GET":
            return [self._rep(ch, p)]
        if verb == "SET":
            if not self.dev.set(ch, p, decode_value(p, value)):
                return ["< REP ERR >"]
            rep = self._rep(ch, p)
            # real devices broadcast changes to every connected client
            self._broadcast(rep)
            return []
        return ["< REP ERR >"]

    def _broadcast(self, line: str) -> None:
        for w in list(self._clients):
            w.write(line.encode())

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._clients.add(writer)
        buf = ""
        try:
            while data := await reader.read(4096):
                buf += data.decode("ascii", errors="replace")
                while (s := buf.find("<")) >= 0 and (e := buf.find(">", s)) >= 0:
                    msg = buf[s + 1:e]
                    buf = buf[e + 1:]
                    if self.delay:
                        await asyncio.sleep(self.delay)
                    for line in self.handle(msg):
                        writer.write(line.encode())
                    await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            self._clients.discard(writer)
            writer.close()

    async def start(self):
        self._server = await asyncio.start_server(self._client, self.host, self.port)

    async def stop(self):
        if self._server:
            self._server.close()
            for w in list(self._clients):
                w.close()
            await self._server.wait_closed()


# ------------------------------------------------------------ Sennheiser G4
class _G4Proto(asyncio.DatagramProtocol):
    def __init__(self, sim: "SennheiserSimServer"):
        self.sim = sim

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr):
        replies = []
        for line in data.decode("latin-1").replace("\n", "\r").split("\r"):
            if line.strip():
                replies += self.sim.handle(line.strip())
        if replies:
            self.transport.sendto(("\r".join(replies) + "\r").encode("latin-1"), addr)


class SennheiserSimServer:
    def __init__(self, dev: SimDevice, host: str = "127.0.0.1", port: int = 53212):
        self.dev, self.host, self.port = dev, host, port
        self._transport = None

    def _lookup(self, cmd: str) -> tuple[int, Param] | None:
        for p in self.dev.profile.params:
            if p.cmd.lower() == cmd.lower():
                return 1, p
        for p in self.dev.profile.device_params:
            if p.cmd.lower() == cmd.lower():
                return 0, p
        return None

    def handle(self, line: str) -> list[str]:
        parts = line.split(None, 1)
        cmd, value = parts[0], (parts[1] if len(parts) > 1 else None)
        if cmd.lower() == "push":
            return [f"Push {value or '0 0 0'}"]
        found = self._lookup(cmd)
        if not found:
            return []  # real G4 units ignore unknown commands
        ch, p = found
        if value is not None:
            self.dev.set(ch, p, decode_value(p, value))
        val = self.dev.get(ch, p)
        return [f"{p.cmd} {encode_value(p, val) if val is not None else ''}".rstrip()]

    async def start(self):
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _G4Proto(self), local_addr=(self.host, self.port))

    async def stop(self):
        if self._transport:
            self._transport.close()


# ------------------------------------------------------------------- racks
@dataclass
class SimSpec:
    model: str
    band: str
    port: int
    label: str


DEMO_RACK = [
    SimSpec("ULXD4Q", "G51", 22020, "ULXD4Q (sim)"),
    SimSpec("AD4D", "G56", 22021, "AD4D (sim)"),
    SimSpec("QLXD4", "H50", 22022, "QLXD4 (sim)"),
    SimSpec("SLXD4D", "G59", 22023, "SLXD4D (sim)"),
    SimSpec("P10T", "G10E", 22024, "PSM1000 P10T (sim)"),
    SimSpec("ADTQ", "G56", 22025, "ADPSM ADTQ (sim)"),
    SimSpec("SR IEM G4", "A1", 53212, "IEM G4 #1 (sim)"),
    SimSpec("SR IEM G4", "A1", 53213, "IEM G4 #2 (sim)"),
]


def make_server(spec: SimSpec, host: str = "127.0.0.1", seed: int | None = None):
    prof = ALL_MODELS[spec.model]
    dev = SimDevice(prof, spec.band, device_id=spec.label,
                    rnd=random.Random(seed if seed is not None else spec.port))
    cls = ShureSimServer if prof.family == "shure" else SennheiserSimServer
    return cls(dev, host, spec.port)


async def start_rack(specs=DEMO_RACK, host: str = "127.0.0.1") -> list:
    servers = [make_server(s, host) for s in specs]
    for s in servers:
        await s.start()
    return servers
