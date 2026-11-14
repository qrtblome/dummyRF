"""Device manager: runs drivers on an asyncio loop in a background thread and
reports to the Qt GUI through signals (signals emitted from another thread are
delivered on the GUI thread automatically).
"""
from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal, Slot

from .drivers import DeviceState, Driver, DriverError, make_driver
from .show import DeviceSpec


@dataclass
class DeviceEntry:
    spec: DeviceSpec
    driver: Driver
    state: DeviceState = field(default_factory=DeviceState)
    status: str = "connecting"
    last_ok: float = 0.0
    task: asyncio.Task | None = None


class DeviceManager(QObject):
    device_changed = Signal(str)          # uid
    devices_changed = Signal()            # added / removed
    log = Signal(str)
    _invoke = Signal(object)              # run a callable on the GUI thread

    def __init__(self, poll_interval: float = 2.0):
        super().__init__()
        self._invoke.connect(self._run_in_gui)
        self.poll_interval = poll_interval
        self.entries: dict[str, DeviceEntry] = {}
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, name="rf-io", daemon=True)
        self._thread.start()
        self._sims: list = []

    # --------------------------------------------------------------- helpers
    @Slot(object)
    def _run_in_gui(self, fn) -> None:
        fn()

    def call_in_gui(self, fn: Callable[[], None]) -> None:
        """Run ``fn`` on the GUI thread (safe to call from the I/O thread)."""
        self._invoke.emit(fn)

    def submit(self, coro, on_done: Callable[[Any, Exception | None], None] | None = None) -> Future:
        """Run ``coro`` on the I/O loop; ``on_done(result, error)`` is called on the GUI thread."""
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        if on_done:
            def _cb(f: Future):
                try:
                    res, err = f.result(), None
                except Exception as e:  # noqa: BLE001
                    res, err = None, e
                self._invoke.emit(lambda: on_done(res, err))
            fut.add_done_callback(_cb)
        return fut

    def shutdown(self) -> None:
        async def _stop():
            for e in list(self.entries.values()):
                if e.task:
                    e.task.cancel()
                await e.driver.close()
            for s in self._sims:
                await s.stop()
        try:
            self.submit(_stop()).result(3)
        except Exception:  # noqa: BLE001
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)

    # --------------------------------------------------------------- devices
    def add_device(self, spec: DeviceSpec) -> None:
        drv = make_driver(spec.family, spec.host, spec.port, spec.model)
        entry = DeviceEntry(spec, drv)
        self.entries[spec.uid] = entry

        async def _start():
            entry.task = asyncio.create_task(self._poll(entry))
        self.submit(_start())
        self.devices_changed.emit()

    def remove_device(self, uid: str) -> None:
        entry = self.entries.pop(uid, None)
        if not entry:
            return

        async def _stop():
            if entry.task:
                entry.task.cancel()
            await entry.driver.close()
        self.submit(_stop())
        self.devices_changed.emit()

    async def _poll(self, e: DeviceEntry) -> None:
        backoff = 1.0
        while True:
            try:
                if not e.driver.connected:
                    e.status = "connecting"
                    self.device_changed.emit(e.spec.uid)
                    await e.driver.connect()
                    self.log.emit(f"{self.name(e.spec.uid)}: connected ({e.driver.profile.model})")
                    backoff = 1.0
                e.state = await e.driver.refresh()
                e.status = "online"
                e.last_ok = time.time()
                self.device_changed.emit(e.spec.uid)
                await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                raise
            except Exception as ex:  # noqa: BLE001 — keep polling whatever happens
                if e.status != "offline":
                    self.log.emit(f"{self.name(e.spec.uid)}: {ex}")
                e.status = "offline"
                await e.driver.close()
                self.device_changed.emit(e.spec.uid)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 15.0)

    def name(self, uid: str) -> str:
        e = self.entries.get(uid)
        if not e:
            return uid
        return e.spec.label or f"{e.spec.host}:{e.spec.port}"

    # --------------------------------------------------------------- control
    def set_param(self, uid: str, channel: int, key: str, value: Any,
                  on_done: Callable[[Any, Exception | None], None] | None = None) -> None:
        e = self.entries[uid]

        async def _set():
            await e.driver.set(channel, key, value)
            e.state = await e.driver.refresh()
            self.device_changed.emit(uid)

        def _done(res, err):
            label = f"{self.name(uid)} ch{channel} {key} = {value!r}"
            self.log.emit(f"{label}: {'FAILED — ' + str(err) if err else 'ok'}")
            if on_done:
                on_done(res, err)
        self.submit(_set(), _done)

    def raw(self, uid: str, command: str, on_done: Callable[[Any, Exception | None], None]) -> None:
        e = self.entries[uid]

        async def _raw():
            if not e.driver.connected:
                raise DriverError("device offline")
            return await e.driver.raw(command)
        self.submit(_raw(), on_done)

    # ------------------------------------------------------------ simulators
    def start_simulators(self, on_done: Callable[[Any, Exception | None], None]) -> None:
        from .sim import DEMO_RACK, start_rack

        async def _start():
            if not self._sims:
                self._sims = await start_rack(DEMO_RACK)
            return DEMO_RACK
        self.submit(_start(), on_done)
