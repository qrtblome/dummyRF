"""Sennheiser evolution wireless G3/G4 network driver (UDP, default port 53212).

Wire format: one ASCII command per line, terminated by CR::

    -> Frequency\r            (query)
    <- Frequency 548000\r
    -> Frequency 552100\r     (set, kHz)
    <- Frequency 552100\r
    -> Name LEAD\r

Multiple reply lines may arrive in one datagram.  The device does not answer
unknown commands, so a timeout means "not supported / no reply".
"""
from __future__ import annotations

import asyncio
import collections
from typing import Any

from ..profiles import ALL_MODELS, ModelProfile, Param
from .base import DeviceState, Driver, DriverError, decode_value, encode_value


def parse_lines(data: bytes) -> list[tuple[str, str]]:
    out = []
    text = data.decode("latin-1", errors="replace")
    for line in text.replace("\n", "\r").split("\r"):
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        out.append((parts[0], parts[1] if len(parts) > 1 else ""))
    return out


class _Proto(asyncio.DatagramProtocol):
    def __init__(self, owner: "SennheiserG4Driver"):
        self.owner = owner

    def datagram_received(self, data: bytes, addr) -> None:
        self.owner._on_datagram(data)

    def error_received(self, exc: Exception) -> None:
        self.owner._last_error = exc


class SennheiserG4Driver(Driver):
    family = "sennheiser_g4"

    def __init__(self, host: str, port: int = 53212, profile: ModelProfile | None = None):
        super().__init__(host, port, profile or ALL_MODELS["SR IEM G4"])
        self._transport: asyncio.DatagramTransport | None = None
        self._waiters: dict[str, list[asyncio.Future]] = collections.defaultdict(list)
        self._listeners: list[list[str]] = []
        self._last_error: Exception | None = None

    async def connect(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            self._transport, _ = await loop.create_datagram_endpoint(
                lambda: _Proto(self), remote_addr=(self.host, self.port))
        except OSError as e:
            raise DriverError(f"cannot open UDP to {self.host}:{self.port}: {e}") from e
        self.connected = True
        try:
            # UDP has no connection: prove the device is there by asking for its name.
            await self._query("Name")
        except DriverError:
            await self.close()
            raise DriverError(f"no answer from {self.host}:{self.port}")

    async def close(self) -> None:
        self.connected = False
        if self._transport:
            self._transport.close()
            self._transport = None

    def _on_datagram(self, data: bytes) -> None:
        for cmd, value in parse_lines(data):
            line = f"{cmd} {value}".strip()
            for lst in self._listeners:
                lst.append(line)
            for f in self._waiters.pop(cmd.lower(), []):
                if not f.done():
                    f.set_result(value)

    def _send(self, line: str) -> None:
        if not self._transport:
            raise DriverError("not connected")
        self._transport.sendto((line + "\r").encode("latin-1", errors="replace"))

    async def _query(self, cmd: str, value: str | None = None) -> str:
        fut = asyncio.get_running_loop().create_future()
        self._waiters[cmd.lower()].append(fut)
        self._send(cmd if value is None else f"{cmd} {value}")
        try:
            return await asyncio.wait_for(fut, self.timeout)
        except asyncio.TimeoutError as e:
            raise DriverError(f"no reply to {cmd}") from e

    async def refresh(self) -> DeviceState:
        prof = self.profile
        jobs: list[tuple[int, Param]] = [(0, p) for p in prof.device_params]
        jobs += [(1, p) for p in prof.params]
        results = await self._gather(self._query(p.cmd) for _, p in jobs)
        state = DeviceState(model=prof.model, channels={1: {}})
        answered = 0
        for (ch, p), res in zip(jobs, results):
            if isinstance(res, Exception):
                continue
            answered += 1
            target = state.device if ch == 0 else state.channels[1]
            target[p.key] = decode_value(p, res)
        if not answered:
            self.connected = False
            raise DriverError("device stopped answering")
        return state

    async def set(self, channel: int, key: str, value: Any) -> None:
        p = self._param(key)
        raw = encode_value(p, value)
        reply = await self._query(p.cmd, raw)
        if decode_value(p, reply) != decode_value(p, raw):
            raise DriverError(f"device answered {reply!r} after setting {p.label}")

    async def raw(self, command: str, listen: float = 0.6) -> list[str]:
        lines: list[str] = []
        self._listeners.append(lines)
        try:
            self._send(command.strip())
            await asyncio.sleep(listen)
        finally:
            self._listeners.remove(lines)
        return lines
