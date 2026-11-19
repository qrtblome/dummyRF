"""A simulated SLX-D speaking ACN (SDT + DMP), modelled on captures of a real
SLXD4D talking to Wireless Workbench.  Enough for dummyRF's ACN client: session
join/connect, GET/SET/SUBSCRIBE, and RF scans that stream results.
"""
from __future__ import annotations

import asyncio
import random
import struct
import uuid

from ..acn import slxd, wire as w
from .devices import sim_rf_dbm


class AcnSlxdSim(asyncio.DatagramProtocol):
    def __init__(self, band=(562000, 606000), freqs=(563200, 565050), scan_step_s: float = 0.02,
                 bins_per_event: int = 17, cid: str | None = None):
        self.cid = uuid.UUID(cid).bytes if cid else uuid.uuid4().bytes
        self.band = band
        self.props: dict[int, int] = {slxd.CAP_MIN_KHZ: 470000, slxd.CAP_STEP_KHZ: 25}
        for ch, f in enumerate(freqs, start=1):
            self.props[slxd.ch_base(ch) + slxd.FREQ] = f
            for off in (slxd.PROGRESS, slxd.COMPLETED, slxd.RUNNING, slxd.STATE):
                self.props[slxd.ch_base(ch) + off] = 0
        self.ignore_trigger = False      # to test a receiver that never starts
        self.scan_step_s = scan_step_s
        self.bins_per_event = bins_per_event
        self.peer = None
        self.my_channel = random.randint(0x1000, 0xEFFF)
        self.total = self.rel = 64
        self.peer_channel = None
        self.subscribed: set[int] = set()
        self.sets: list[tuple[int, bytes]] = []
        self.scans_run = 0
        self.rnd = random.Random(3)
        self._scan_task: asyncio.Task | None = None

    # ------------------------------------------------------------ server
    async def start(self, host="127.0.0.1", port=0):
        loop = asyncio.get_running_loop()
        self.tr, _ = await loop.create_datagram_endpoint(lambda: self, local_addr=(host, port))
        self.port = self.tr.get_extra_info("sockname")[1]
        return self

    async def stop(self):
        if self._scan_task:
            self._scan_task.cancel()
        self.tr.close()

    def connection_made(self, transport):
        self.tr = transport

    # ------------------------------------------------------------ sending
    def _send(self, sdt_payload: bytes):
        if self.peer:
            self.tr.sendto(w.root(self.cid, sdt_payload), self.peer)

    def _wrap(self, kind: int, blocks: bytes):
        self.total += 1
        if kind == w.REL_WRAP:
            self.rel += 1
        self._send(w.wrapper(kind, self.my_channel, self.total, self.rel, 65, blocks))

    def _dmp_out(self, vector: int, items: list[bytes], header=w.DMP_ADDR4, reliable=True):
        self._wrap(w.REL_WRAP if reliable else w.UNREL_WRAP,
                   w.client_block(1, w.PROTO_DMP, w.dmp(vector, items, header)))

    def _event(self, address: int, value: bytes, reliable=False):
        if address in self.subscribed or slxd.ALL_EVENTS in self.subscribed:
            self._dmp_out(w.EVENT, [w.addr(address) + value], reliable=reliable)

    # ------------------------------------------------------------ receiving
    def datagram_received(self, data, addr):
        if not data.startswith(w.PREAMBLE):
            return
        self.peer = addr
        for rvec, rcid, rdata in w.split(data[len(w.PREAMBLE):], 4, 16):
            self.peer_cid = rcid
            for svec, _, sdata in w.split(rdata, 1, 0):
                v = svec[0]
                if v == w.JOIN:
                    _, member, ch, _r, tot, rel = struct.unpack("!16sHHHII", sdata[:30])
                    self.peer_channel = ch
                    self._send(w.join_accept(rcid, ch, member, rel, self.my_channel))
                    self._send(w.sdt(w.JOIN, rcid + struct.pack("!HHHII", 1, self.my_channel, ch,
                                                                 self.total, self.rel)
                                     + b"\x00" + w.CHANNEL_PARAMS + b"\xff"))
                elif v in (w.REL_WRAP, w.UNREL_WRAP):
                    for _m, mhdr, mdata in w.split(sdata[20:], 2, 6):
                        proto = w.int_value(mhdr[:4])
                        if proto == w.PROTO_SDT:
                            for v2, _, d2 in w.split(mdata, 1, 0):
                                if v2[0] == w.CONNECT:
                                    self._wrap(w.REL_WRAP, w.client_block(1, w.PROTO_SDT,
                                                                          w.sdt(w.CONNECT_ACCEPT, d2)))
                        elif proto == w.PROTO_DMP:
                            self._dmp_in(mdata)

    def _dmp_in(self, data: bytes):
        for vec, hdr, d in w.split(data, 1, 1):
            for it in w.parse_dmp(vec[0], hdr[0], d):
                if it.op == w.GET:
                    if it.address in self.props:
                        size = 4 if self.props[it.address] > 255 else 1
                        self._dmp_out(w.GET_REPLY, [w.addr(it.address)
                                                    + self.props[it.address].to_bytes(size, "big")])
                    else:
                        self._dmp_out(w.GET_FAIL, [w.addr(it.address) + b"\x01"])
                elif it.op == w.SUBSCRIBE:
                    self.subscribed.add(it.address)
                    self._dmp_out(w.SUBSCRIBE_ACCEPT, [w.addr(it.address)])
                elif it.op == w.UNSUBSCRIBE:
                    self.subscribed.discard(it.address)
                elif it.op == w.SET:
                    self.sets.append((it.address, it.value))
                    self.props[it.address] = w.int_value(it.value)
                    if (it.address & 0xFFFF) == slxd.TRIGGER and not self.ignore_trigger:
                        ch = ((it.address - 0x02000000) >> 16) + 1
                        self._scan_task = asyncio.create_task(self._scan(ch))

    # ------------------------------------------------------------ scan
    async def _scan(self, ch: int):
        b = slxd.ch_base(ch)
        start, stop, step = self.props[b + slxd.START], self.props[b + slxd.STOP], self.props[b + slxd.STEP]
        base, tune = self.props[slxd.CAP_MIN_KHZ], self.props[slxd.CAP_STEP_KHZ]
        self.scans_run += 1
        self._event(b + slxd.PROGRESS, b"\x00", True)
        self._event(b + slxd.COMPLETED, b"\x00", True)
        self._event(b + slxd.RUNNING, b"\x01", True)
        freqs = list(range(start, stop + 1, step))
        n_ev = (len(freqs) + self.bins_per_event - 1) // self.bins_per_event
        for e in range(n_ev):
            chunk = freqs[e * self.bins_per_event:(e + 1) * self.bins_per_event]
            vals = b"".join(struct.pack("!h", int(round(sim_rf_dbm(f, self.rnd)))) for f in chunk)
            first_index = (chunk[0] - base) // tune
            self._dmp_out(w.EVENT, [w.addr(slxd.DATA_BASE + first_index)
                                    + struct.pack("!II", step // tune, len(chunk)) + vals],
                          header=w.DMP_RANGE_ARRAY4, reliable=False)
            pct = int((e + 1) * 100 / n_ev) // 5 * 5
            self._event(b + slxd.PROGRESS, bytes([pct]), True)
            await asyncio.sleep(self.scan_step_s)
        self._event(b + slxd.RUNNING, b"\x00", True)
        self._event(b + slxd.COMPLETED, b"\x01", True)
