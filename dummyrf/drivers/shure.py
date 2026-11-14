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

Measured on a ULXD4D (fw 2.7.6): the device works through commands one at a
time, ~0.1 s each, and answers an unknown command with a bare ``< REP ERR >``
that doesn't say which command failed.  So the driver keeps exactly one
command in flight (an ERR then unambiguously belongs to it), and refreshes
with ``< GET 0 ALL >``, which returns every parameter of every channel in
~0.5 s instead of ~1.7 s for individual GETs.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..profiles import STR, ModelProfile, Param, match_shure_model
from .base import DeviceState, Driver, DriverError, RefusedError, decode_value, encode_value

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


class _Unsupported(DriverError):
    """The device answered ``REP ERR`` to this command."""


class ShureDriver(Driver):
    family = "shure"
    set_timeout = 2.0
    all_quiet = 0.25      # GET 0 ALL is complete after this long without a reply
    all_max = 4.0

    def __init__(self, host: str, port: int = 2202, profile: ModelProfile | None = None):
        super().__init__(host, port, profile)
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._pending: tuple[Key, asyncio.Future] | None = None
        self._cache: dict[Key, str] = {}
        self._listeners: list[list[str]] = []
        self._unsupported: set[Key] = set()   # (0 for device-level / 1 for channel, CMD)
        self._all_ok: bool | None = None       # does the device support GET 0 ALL?
        self._rx_count = 0
        self._err_count = 0
        self._last_rx = 0.0
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

    def _fail_pending(self, msg: str) -> None:
        if self._pending and not self._pending[1].done():
            self._pending[1].set_exception(DriverError(msg))

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
        self._fail_pending("connection closed")

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
            self._fail_pending("connection lost")

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
        loop = asyncio.get_running_loop()
        self._last_rx = loop.time()
        self._rx_count += 1
        if cmd == "ERR":
            self.errors += 1
            self._err_count += 1
            if self._pending and not self._pending[1].done():
                key = self._pending[0]
                self._pending[1].set_exception(_Unsupported(f"device rejected {key[1]}"))
            return
        key = (ch, cmd)
        self._cache[key] = value
        if self._pending and self._pending[0] == key and not self._pending[1].done():
            self._pending[1].set_result(value)

    def _send(self, line: str) -> None:
        if not self._writer or not self.connected:
            raise DriverError("not connected")
        self._writer.write(line.encode("ascii", errors="replace"))

    async def _query(self, channel: int, cmd: str, verb: str = "GET", value: str | None = None,
                     timeout: float | None = None) -> str:
        """One command, one reply. Only one command is ever in flight."""
        async with self._lock:
            fut = asyncio.get_running_loop().create_future()
            self._pending = ((channel, cmd), fut)
            try:
                self._send(format_command(verb, channel, cmd, value))
                return await asyncio.wait_for(fut, timeout or self.timeout)
            except asyncio.TimeoutError as e:
                raise DriverError(f"no reply to {verb} {channel or ''} {cmd}") from e
            finally:
                self._pending = None

    async def _get_all(self) -> bool:
        """``< GET 0 ALL >`` into the cache. False if the device doesn't support it."""
        loop = asyncio.get_running_loop()
        async with self._lock:
            rx0, err0 = self._rx_count, self._err_count
            t0 = loop.time()
            self._send("< GET 0 ALL >")
            while True:
                await asyncio.sleep(0.05)
                if not self.connected:
                    raise DriverError("connection lost")
                now = loop.time()
                got = self._rx_count - rx0
                if self._err_count > err0 and got == self._err_count - err0:
                    return False                       # only an ERR came back
                if got and now - self._last_rx > self.all_quiet:
                    return True
                if now - t0 > self.all_max:
                    return got > 0

    # ------------------------------------------------------------------- API
    async def refresh(self) -> DeviceState:
        prof = self.profile
        self._cache.clear()   # only report what the device says now
        if self._all_ok is not False:
            self._all_ok = await self._get_all()
        jobs: list[tuple[int, Param]] = [(0, p) for p in prof.device_params]
        for ch in range(1, prof.channels + 1):
            jobs += [(ch, p) for p in prof.params]
        # anything GET 0 ALL didn't cover is asked for one by one
        for ch, p in jobs:
            scope = (min(ch, 1), p.cmd)
            if (ch, p.cmd) in self._cache or scope in self._unsupported:
                continue
            try:
                await self._query(ch, p.cmd)
            except _Unsupported:
                self._unsupported.add(scope)
            except DriverError:
                if not self.connected:
                    raise
        if not self.connected:
            raise DriverError("connection lost")
        state = DeviceState(model=prof.model)
        for ch in range(1, prof.channels + 1):
            state.channels[ch] = {}
        for ch, p in jobs:
            raw = self._cache.get((ch, p.cmd))
            if raw is None:
                continue
            val = decode_value(p, raw)
            if ch == 0:
                state.device[p.key] = val
            else:
                state.channels[ch][p.key] = val
        return state

    async def set(self, channel: int, key: str, value: Any) -> None:
        p = self._param(key)
        raw = encode_value(p, value)
        if p.kind == STR:
            raw = "{" + raw + "}"
        try:
            reply = await self._query(channel, p.cmd, "SET", raw, timeout=self.set_timeout)
        except _Unsupported as e:
            raise RefusedError(f"device refused {p.label} = {value!r} (REP ERR)") from e
        got = decode_value(p, reply)
        if p.kind == STR:
            ok = str(got).strip() == str(value)[: p.width or None].strip()
        else:
            ok = got == decode_value(p, raw)
        if not ok:
            raise RefusedError(f"device kept {reply!r} instead of {p.label} = {value!r}")

    async def read(self, channel: int, key: str) -> Any:
        p = self.profile.param(key) if self.profile else None
        if p is None:
            raise DriverError(f"{self.profile.model if self.profile else 'device'} has no {key!r}")
        return decode_value(p, await self._query(channel, p.cmd))

    async def set_device(self, key: str, value: Any) -> None:
        p = next((p for p in self.profile.device_params if p.key == key), None)
        if p is None or not p.writable:
            raise DriverError(f"cannot set {key}")
        raw = encode_value(p, value)
        await self._query(0, p.cmd, "SET", "{" + raw + "}" if p.kind == STR else raw,
                          timeout=self.set_timeout)

    async def raw(self, command: str, listen: float = 0.6) -> list[str]:
        command = command.strip()
        if not command.startswith("<"):
            command = f"< {command} >"
        lines: list[str] = []
        async with self._lock:
            self._listeners.append(lines)
            try:
                self._send(command)
                await asyncio.sleep(listen)
            finally:
                self._listeners.remove(lines)
        return lines
