"""An ACN session with one device: SDT channel + DMP property access.

Mirrors what Wireless Workbench does (from captures against an SLXD4D):

    -> JOIN (device CID, member 1, our channel, seq = channel)
    <- JOIN_ACCEPT  and  <- JOIN (device's reciprocal channel)
    -> JOIN_ACCEPT
    -> CONNECT DMP   <- CONNECT_ACCEPT   (and the reverse)
    ... DMP GET / SET / SUBSCRIBE in reliable wrappers ...
    ACK with the device's last reliable sequence number every ~1.5 s
    (the channel expires after 5 s of silence)

Reliable wrappers are not retransmitted: on a show LAN loss is rare and every
operation has a timeout.
"""
from __future__ import annotations

import asyncio
import random
import struct
import uuid
from typing import Callable

from . import wire as w

EventCallback = Callable[[w.DmpItem], None]


class AcnError(Exception):
    pass


class _Proto(asyncio.DatagramProtocol):
    def __init__(self, owner: "AcnSession"):
        self.owner = owner

    def datagram_received(self, data, addr):
        self.owner._received(data)

    def error_received(self, exc):
        self.owner.last_error = exc


class AcnSession:
    KEEPALIVE_S = 1.5
    TIMEOUT_S = 3.0

    def __init__(self, host: str, port: int, device_cid: str | bytes,
                 my_cid: bytes | None = None, on_event: EventCallback | None = None):
        self.host, self.port = host, port
        self.device_cid = device_cid if isinstance(device_cid, bytes) else uuid.UUID(device_cid).bytes
        self.my_cid = my_cid or uuid.uuid4().bytes
        self.on_event = on_event
        self.channel = random.randint(0x1000, 0xEFFF)
        self.total = self.rel = self.channel        # WWB starts both sequences at its channel number
        self.oldest = self.channel + 1
        self.member = 1                              # the device's member id on our channel
        self.device_channel: int | None = None
        self.device_rel: int | None = None
        self._tr: asyncio.DatagramTransport | None = None
        self._joined = asyncio.Event()
        self._connected = asyncio.Event()
        self._waiters: dict[tuple[int, int], list[asyncio.Future]] = {}
        self._keepalive: asyncio.Task | None = None
        self.last_error: Exception | None = None
        self.open = False

    # ------------------------------------------------------------ lifecycle
    async def connect(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            self._tr, _ = await loop.create_datagram_endpoint(
                lambda: _Proto(self), remote_addr=(self.host, self.port))
        except OSError as e:
            raise AcnError(f"cannot open UDP to {self.host}:{self.port}: {e}") from e
        self._send(w.join(self.device_cid, self.member, self.channel, self.channel))
        try:
            await asyncio.wait_for(self._joined.wait(), self.TIMEOUT_S)
            self._ack()
            self._send_rel(w.client_block(self.member, w.PROTO_SDT,
                                          w.sdt(w.CONNECT, struct.pack("!I", w.PROTO_DMP))))
            await asyncio.wait_for(self._connected.wait(), self.TIMEOUT_S)
        except asyncio.TimeoutError as e:
            await self.close()
            raise AcnError(f"{self.host}:{self.port} did not accept an ACN session") from e
        self.open = True
        self._keepalive = asyncio.create_task(self._keepalive_loop())

    async def close(self) -> None:
        if self._keepalive:
            self._keepalive.cancel()
            self._keepalive = None
        if self._tr is not None:
            if self.open:
                try:
                    self._send_rel(w.client_block(self.member, w.PROTO_SDT,
                                                  w.sdt(w.DISCONNECT, struct.pack("!I", w.PROTO_DMP))))
                    self._send_rel(w.client_block(self.member, w.PROTO_SDT, w.sdt(w.LEAVE, b"")))
                    await asyncio.sleep(0.05)
                except OSError:
                    pass
            self._tr.close()
            self._tr = None
        self.open = False
        for futs in self._waiters.values():
            for f in futs:
                if not f.done():
                    f.set_exception(AcnError("session closed"))
        self._waiters.clear()

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def _keepalive_loop(self):
        while True:
            await asyncio.sleep(self.KEEPALIVE_S)
            self._ack()

    # ------------------------------------------------------------ sending
    def _send(self, sdt_payload: bytes) -> None:
        if self._tr is None:
            raise AcnError("session not open")
        self._tr.sendto(w.root(self.my_cid, sdt_payload))

    def _send_rel(self, blocks: bytes) -> None:
        self.total += 1
        self.rel += 1
        self._send(w.wrapper(w.REL_WRAP, self.channel, self.total, self.rel, self.oldest, blocks))

    def _ack(self) -> None:
        if self.device_rel is None or self._tr is None:
            return
        self.total += 1
        block = w.client_block(self.member, w.PROTO_SDT, w.sdt(w.ACK, struct.pack("!I", self.device_rel)))
        self._send(w.wrapper(w.UNREL_WRAP, self.channel, self.total, self.rel, self.oldest, block))

    def _dmp(self, vector: int, items: list[bytes]) -> None:
        self._send_rel(w.client_block(self.member, w.PROTO_DMP, w.dmp(vector, items)))

    def _expect(self, ops: tuple[int, ...], address: int) -> asyncio.Future:
        fut = asyncio.get_running_loop().create_future()
        for op in ops:
            self._waiters.setdefault((op, address), []).append(fut)
        return fut

    # ------------------------------------------------------------ DMP API
    async def get(self, addresses: list[int]) -> dict[int, bytes | None]:
        """Read properties. Value bytes, or None when the device refuses."""
        futs = {a: self._expect((w.GET_REPLY, w.GET_FAIL), a) for a in addresses}
        self._dmp(w.GET, [w.addr(a) for a in addresses])
        out = {}
        for a, f in futs.items():
            try:
                item: w.DmpItem = await asyncio.wait_for(f, self.TIMEOUT_S)
            except asyncio.TimeoutError as e:
                raise AcnError(f"no reply to GET 0x{a:08x}") from e
            out[a] = None if item.op == w.GET_FAIL else item.value
        return out

    async def get_int(self, address: int, signed: bool = False) -> int:
        v = (await self.get([address]))[address]
        if v is None:
            raise AcnError(f"device refused GET 0x{address:08x}")
        return w.int_value(v, signed)

    def set(self, values: list[tuple[int, bytes]]) -> None:
        """Write properties (fire-and-forget; the device reports failures as SET_FAIL)."""
        self._dmp(w.SET, [w.addr(a) + v for a, v in values])

    async def subscribe(self, addresses: list[int]) -> None:
        futs = {a: self._expect((w.SUBSCRIBE_ACCEPT, w.SUBSCRIBE_REJECT), a) for a in addresses}
        self._dmp(w.SUBSCRIBE, [w.addr(a) for a in addresses])
        for a, f in futs.items():
            try:
                item = await asyncio.wait_for(f, self.TIMEOUT_S)
            except asyncio.TimeoutError:
                continue          # the SLX-D doesn't confirm every subscription
            if item.op == w.SUBSCRIBE_REJECT:
                raise AcnError(f"device rejected subscription to 0x{a:08x}")

    def unsubscribe(self, addresses: list[int]) -> None:
        self._dmp(w.UNSUBSCRIBE, [w.addr(a) for a in addresses])

    # ------------------------------------------------------------ receiving
    def _received(self, data: bytes) -> None:
        if not data.startswith(w.PREAMBLE):
            return
        for rvec, rcid, rdata in w.split(data[len(w.PREAMBLE):], 4, 16):
            if w.int_value(rvec) != w.PROTO_SDT:
                continue
            for svec, _, sdata in w.split(rdata, 1, 0):
                if not svec:
                    continue
                self._sdt(svec[0], sdata)

    def _sdt(self, vector: int, data: bytes) -> None:
        if vector == w.JOIN_ACCEPT and len(data) >= 26:
            pass                                        # the device's JOIN follows
        elif vector == w.JOIN_REFUSE:
            self.last_error = AcnError("device refused the session")
        elif vector == w.JOIN and len(data) >= 30:
            _, member, ch, _recip, _tot, rel = struct.unpack("!16sHHHII", data[:30])
            self.device_channel, self.device_rel = ch, rel
            self._send(w.join_accept(self.device_cid, ch, member, rel, self.channel))
            self._joined.set()
        elif vector in (w.REL_WRAP, w.UNREL_WRAP) and len(data) >= 20:
            _ch, _tot, rel, _old, first, last, _mak = struct.unpack("!HIIIHHH", data[:20])
            if vector == w.REL_WRAP:
                self.device_rel = rel
            for _mvec, mhdr, mdata in w.split(data[20:], 2, 6):
                proto = w.int_value(mhdr[:4]) if len(mhdr) >= 4 else None
                if proto == w.PROTO_SDT:
                    self._sdt_inner(mdata)
                elif proto == w.PROTO_DMP:
                    self._dmp_in(mdata)
            if first != w.NO_MEMBER and first <= 1 <= last:
                self._ack()

    def _sdt_inner(self, data: bytes) -> None:
        for vec, _, d in w.split(data, 1, 0):
            if not vec:
                continue
            if vec[0] == w.CONNECT:
                self._send_rel(w.client_block(self.member, w.PROTO_SDT, w.sdt(w.CONNECT_ACCEPT, d)))
            elif vec[0] == w.CONNECT_ACCEPT:
                self._connected.set()
            elif vec[0] in (w.LEAVING, w.DISCONNECTING):
                self.open = False

    def _dmp_in(self, data: bytes) -> None:
        for vec, hdr, d in w.split(data, 1, 1):
            if not vec or not hdr:
                continue
            for item in w.parse_dmp(vec[0], hdr[0], d):
                futs = self._waiters.pop((item.op, item.address), [])
                for f in futs:
                    if not f.done():
                        f.set_result(item)
                if futs:
                    # the other op this future was waiting for is now moot
                    for key in list(self._waiters):
                        self._waiters[key] = [f for f in self._waiters[key] if not f.done()]
                        if not self._waiters[key]:
                            del self._waiters[key]
                if item.op in (w.EVENT, w.SYNC_EVENT) and self.on_event:
                    self.on_event(item)
