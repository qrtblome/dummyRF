"""Shure command-string protocol driver (ULX-D, QLX-D, SLX-D, Axient Digital,
PSM1000, Axient Digital PSM).

Wire format (TCP, default port 2202)::

    -> < GET 1 CHAN_NAME >
    <- < REP 1 CHAN_NAME {Lead Vox} >
    -> < SET 1 FREQUENCY 578350 >
    <- < REP 1 FREQUENCY 578350 >
    -> < GET MODEL >
    <- < REP MODEL {ULXD4Q} >

Devices also send unsolicited REP messages when something changes on the
front panel; those update the cache too.
"""
from __future__ import annotations

import asyncio
import collections
import logging
from typing import Any

from ..profiles import STR, ModelProfile, Param, match_shure_model
from .base import DeviceState, Driver, DriverError, decode_value, encode_value

log = logging.getLogger(__name__)

Key = tuple[int, str]  # (channel or 0 for device-level, COMMAND)


def parse_message(msg: str) -> tuple[str, int, str, str] | None:
    """Parse the inside of ``< ... >``. Returns (verb, channel, cmd, value)."""
    parts = msg.strip().split(None, 1)
    if not parts:
        return None
    verb = parts[0].upper()
    rest = parts[1] if len(parts) > 1 else ""
    channel = 0
    tok = rest.split(None, 1)
    if tok and tok[0].isdigit():
        channel = int(tok[0])
        rest = tok[1] if len(tok) > 1 else ""
    tok = rest.split(None, 1)
    if not tok:
        return (verb, channel, "", "")
    return (verb, channel, tok[0].upper(), tok[1].strip() if len(tok) > 1 else "")


def format_command(verb: str, channel: int, cmd: str, value: str | None = None) -> str:
    parts = ["<", verb]
    if channel:
        parts.append(str(channel))
    parts.append(cmd)
    if value is not None:
        parts.append(value)
    parts.append(">")
    return " ".join(parts)


class ShureDriver(Driver):
    family = "shure"

    def __init__(self, host: str, port: int = 2202, profile: ModelProfile | None = None):
        super().__init__(host, port, profile)
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task | None = None
        self._waiters: dict[Key, list[asyncio.Future]] = collections.defaultdict(list)
        self._cache: dict[Key, str] = {}
        self._listeners: list[list[str]] = []
        self.errors = 0

    # ------------------------------------------------------------- transport
    async def connect(self) -> None:
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), self.timeout * 3)
        except (OSError, asyncio.TimeoutError) as e:
            raise DriverError(f"cannot connect to {self.host}:{self.port}: {e}") from e
        self._task = asyncio.create_task(self._read_loop())
        self.connected = True
        if self.profile is None:
            model = await self._query(0, "MODEL")
            prof = match_shure_model(model.strip("{} "))
            if prof is None:
                await self.close()
                raise DriverError(f"unsupported Shure model {model!r}")
            self.profile = prof

    async def close(self) -> None:
        self.connected = False
        if self._task:
            self._task.cancel()
            self._task = None
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except OSError:
                pass
            self._writer = None
        for futs in self._waiters.values():
            for f in futs:
                if not f.done():
                    f.set_exception(DriverError("connection closed"))
        self._waiters.clear()

    async def _read_loop(self) -> None:
        buf = ""
        try:
            while True:
                data = await self._reader.read(4096)
                if not data:
                    break
                buf += data.decode("ascii", errors="replace")
                while True:
                    start = buf.find("<")
                    if start < 0:
                        buf = ""
                        break
                    end = buf.find(">", start)
                    if end < 0:
                        buf = buf[start:]
                        break
                    self._handle(buf[start + 1:end])
                    buf = buf[end + 1:]
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            self.connected = False
            for futs in self._waiters.values():
                for f in futs:
                    if not f.done():
                        f.set_exception(DriverError("connection lost"))
            self._waiters.clear()

    def _handle(self, msg: str) -> None:
        line = f"< {msg.strip()} >"
        for lst in self._listeners:
            lst.append(line)
        parsed = parse_message(msg)
        if parsed is None:
            return
        verb, ch, cmd, value = parsed
        if verb != "REP":
            return
        if cmd == "ERR":
            self.errors += 1
            log.warning("%s:%s replied ERR", self.host, self.port)
            return
        key = (ch, cmd)
        self._cache[key] = value
        for f in self._waiters.pop(key, []):
            if not f.done():
                f.set_result(value)

    def _send(self, line: str) -> None:
        if not self._writer or not self.connected:
            raise DriverError("not connected")
        self._writer.write(line.encode("ascii", errors="replace"))

    async def _query(self, channel: int, cmd: str, verb: str = "GET", value: str | None = None) -> str:
        fut = asyncio.get_running_loop().create_future()
        self._waiters[(channel, cmd)].append(fut)
        self._send(format_command(verb, channel, cmd, value))
        try:
            return await asyncio.wait_for(fut, self.timeout)
        except asyncio.TimeoutError as e:
            raise DriverError(f"no reply to {verb} {channel or ''} {cmd}") from e

    # ------------------------------------------------------------------- API
    async def refresh(self) -> DeviceState:
        prof = self.profile
        jobs: list[tuple[int, Param]] = [(0, p) for p in prof.device_params]
        for ch in range(1, prof.channels + 1):
            jobs += [(ch, p) for p in prof.params]
        results = await self._gather(self._query(ch, p.cmd) for ch, p in jobs)
        state = DeviceState(model=prof.model)
        for ch in range(1, prof.channels + 1):
            state.channels[ch] = {}
        for (ch, p), res in zip(jobs, results):
            if isinstance(res, Exception):
                continue
            val = decode_value(p, res)
            if ch == 0:
                state.device[p.key] = val
            else:
                state.channels[ch][p.key] = val
        if not self.connected:
            raise DriverError("connection lost")
        return state

    async def set(self, channel: int, key: str, value: Any) -> None:
        p = self._param(key)
        raw = encode_value(p, value)
        if p.kind == STR:
            raw = "{" + raw + "}"
        reply = await self._query(channel, p.cmd, "SET", raw)
        got = decode_value(p, reply)
        if p.kind == STR:
            ok = str(got).strip() == str(value)[: p.width or None].strip()
        else:
            ok = got == decode_value(p, raw)
        if not ok:
            raise DriverError(f"device answered {reply!r} after setting {p.label}")

    async def set_device(self, key: str, value: Any) -> None:
        p = next((p for p in self.profile.device_params if p.key == key), None)
        if p is None or not p.writable:
            raise DriverError(f"cannot set {key}")
        raw = encode_value(p, value)
        await self._query(0, p.cmd, "SET", "{" + raw + "}" if p.kind == STR else raw)

    async def raw(self, command: str, listen: float = 0.6) -> list[str]:
        command = command.strip()
        if not command.startswith("<"):
            command = f"< {command} >"
        lines: list[str] = []
        self._listeners.append(lines)
        try:
            self._send(command)
            await asyncio.sleep(listen)
        finally:
            self._listeners.remove(lines)
        return lines
