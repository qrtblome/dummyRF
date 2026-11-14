"""Common driver interface."""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..profiles import BOOL, ENUM, FREQ, INT, STR, ModelProfile, Param


class DriverError(Exception):
    pass


@dataclass
class DeviceState:
    model: str = ""
    device: dict[str, Any] = field(default_factory=dict)
    channels: dict[int, dict[str, Any]] = field(default_factory=dict)


def decode_value(p: Param, raw: str) -> Any:
    """Protocol string -> python value. Returns None if it cannot be parsed."""
    raw = raw.strip()
    if raw.startswith("{") and raw.endswith("}"):
        raw = raw[1:-1].strip()
    if raw in p.unknown:
        return None
    try:
        if p.kind == STR:
            return raw
        if p.kind == FREQ:
            return int(raw)
        if p.kind == INT:
            return int(raw) - p.offset
        if p.kind == BOOL:
            if raw.upper() == p.true_raw.upper():
                return True
            if raw.upper() == p.false_raw.upper():
                return False
            return None
        if p.kind == ENUM:
            for c in p.choices:
                if c.upper() == raw.upper():
                    return c
            return raw
    except ValueError:
        return None
    return raw


def encode_value(p: Param, value: Any) -> str:
    """python value -> protocol string (without Shure braces)."""
    if p.kind == STR:
        s = str(value)
        return s[: p.width] if p.width else s
    if p.kind == FREQ:
        v = int(value)
        return str(v).zfill(p.width) if p.width else str(v)
    if p.kind == INT:
        v = int(value)
        if p.lo is not None and v < p.lo or p.hi is not None and v > p.hi:
            raise DriverError(f"{p.label}: {v} outside {p.lo}..{p.hi}")
        return str(v + p.offset)
    if p.kind == BOOL:
        return p.true_raw if value else p.false_raw
    if p.kind == ENUM:
        if str(value) not in p.choices:
            raise DriverError(f"{p.label}: {value!r} not in {p.choices}")
        return str(value)
    return str(value)


class Driver(ABC):
    family = ""
    timeout = 1.0

    def __init__(self, host: str, port: int, profile: ModelProfile | None = None):
        self.host = host
        self.port = port
        self.profile = profile
        self.connected = False

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def refresh(self) -> DeviceState: ...

    @abstractmethod
    async def set(self, channel: int, key: str, value: Any) -> None: ...

    @abstractmethod
    async def raw(self, command: str, listen: float = 0.6) -> list[str]:
        """Send a raw protocol command and return lines received for ``listen`` s."""

    def _param(self, key: str) -> Param:
        if self.profile is None:
            raise DriverError("model unknown — connect first")
        p = self.profile.param(key)
        if p is None:
            raise DriverError(f"{self.profile.model} has no parameter {key!r}")
        if not p.writable:
            raise DriverError(f"{p.label} is read-only")
        return p

    @staticmethod
    async def _gather(coros) -> list:
        return await asyncio.gather(*coros, return_exceptions=True)
