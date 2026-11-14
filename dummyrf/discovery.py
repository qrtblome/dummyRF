"""Network discovery: sweep a subnet for Shure and Sennheiser devices.

We don't use ICMP ping: it needs root on macOS and doesn't tell us what the
device is.  Instead every address is probed on the control port itself:

* Shure: TCP connect to 2202, then ``< GET MODEL >`` / ``< GET DEVICE_ID >``.
* Sennheiser G4: a ``Name`` UDP datagram to 53212; anything that answers is a
  G4 unit (it answers with its channel name).

A device that replies is identified straight away (model, name, band).
"""
from __future__ import annotations

import asyncio
import ipaddress
import platform
import re
import socket
import subprocess
from dataclasses import dataclass
from typing import Callable, Iterable

from .drivers.sennheiser import parse_lines
from .drivers.shure import parse_message
from .profiles import match_shure_model

MAX_HOSTS = 4096


@dataclass
class Interface:
    name: str           # en0
    label: str          # Wi-Fi / Ethernet adapter name if known
    ip: str
    netmask: str

    @property
    def network(self) -> ipaddress.IPv4Network:
        return ipaddress.IPv4Network(f"{self.ip}/{self.netmask}", strict=False)

    @property
    def is_loopback(self) -> bool:
        return self.ip.startswith("127.")

    def __str__(self) -> str:
        net = self.network
        lbl = f"{self.label} ({self.name})" if self.label and self.label != self.name else self.name
        return f"{lbl} — {self.ip} / {self.netmask}  ({net.num_addresses - 2 if net.prefixlen < 31 else net.num_addresses} hosts)"


@dataclass
class Found:
    family: str         # "shure" / "sennheiser_g4"
    host: str
    port: int
    model: str          # profile model name, or what the device reported
    supported: bool
    name: str = ""      # device id / channel name
    band: str = ""


def _macos_port_names() -> dict[str, str]:
    """en0 -> 'Wi-Fi', en7 -> 'USB 10/100/1000 LAN' (macOS only, best effort)."""
    try:
        out = subprocess.run(["networksetup", "-listallhardwareports"], capture_output=True,
                             text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    names, port = {}, None
    for line in out.splitlines():
        if line.startswith("Hardware Port:"):
            port = line.split(":", 1)[1].strip()
        elif line.startswith("Device:") and port:
            names[line.split(":", 1)[1].strip()] = port
    return names


def list_interfaces(include_loopback: bool = True) -> list[Interface]:
    import psutil

    stats = psutil.net_if_stats()
    labels = _macos_port_names() if platform.system() == "Darwin" else {}
    out = []
    for name, addrs in psutil.net_if_addrs().items():
        if name in stats and not stats[name].isup:
            continue
        for a in addrs:
            if a.family != socket.AF_INET or not a.netmask:
                continue
            iface = Interface(name, labels.get(name, ""), a.address, a.netmask)
            if iface.is_loopback:
                if not include_loopback:
                    continue
                iface.label = "Loopback (this computer — demo rack)"
                iface.netmask = "255.255.255.255"
            out.append(iface)
    # real adapters first, loopback last
    return sorted(out, key=lambda i: (i.is_loopback, i.name))


def host_range(start: str, end: str) -> list[str]:
    a, b = ipaddress.IPv4Address(start.strip()), ipaddress.IPv4Address(end.strip())
    if b < a:
        a, b = b, a
    n = int(b) - int(a) + 1
    if n > MAX_HOSTS:
        raise ValueError(f"{n} addresses — narrow the range to {MAX_HOSTS} or fewer")
    return [str(ipaddress.IPv4Address(int(a) + i)) for i in range(n)]


def default_range(iface: Interface) -> tuple[str, str]:
    net = iface.network
    if net.num_addresses <= 2:
        return str(net.network_address), str(net.broadcast_address)
    hosts = list(net.hosts()) if net.num_addresses <= MAX_HOSTS + 2 else None
    if hosts:
        return str(hosts[0]), str(hosts[-1])
    # very large subnet (e.g. /16): default to the /24 around our own address
    local = ipaddress.IPv4Network(f"{iface.ip}/24", strict=False)
    return str(local.network_address + 1), str(local.broadcast_address - 1)


def parse_ports(text: str) -> list[int]:
    """'2202, 22020-22025' -> [2202, 22020, ..., 22025]"""
    ports: list[int] = []
    for part in re.split(r"[,\s]+", text.strip()):
        if not part:
            continue
        if "-" in part:
            lo, hi = (int(x) for x in part.split("-", 1))
            if hi - lo > 100:
                raise ValueError(f"port range {part} is too wide")
            ports += range(lo, hi + 1)
        else:
            ports.append(int(part))
    for p in ports:
        if not 1 <= p <= 65535:
            raise ValueError(f"bad port {p}")
    return sorted(set(ports))


# ------------------------------------------------------------------ probes
async def probe_shure(host: str, port: int, timeout: float = 0.6) -> Found | None:
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (OSError, asyncio.TimeoutError):
        return None
    info: dict[str, str] = {}
    replies = 0  # REP or ERR, one per question
    try:
        writer.write(b"< GET MODEL >< GET DEVICE_ID >< GET RF_BAND >")
        await writer.drain()
        buf = ""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 1.0
        while replies < 3:
            left = deadline - loop.time()
            if left <= 0:
                break
            try:
                data = await asyncio.wait_for(reader.read(1024), left)
            except asyncio.TimeoutError:
                break
            if not data:
                break
            buf += data.decode("ascii", errors="replace")
            while (s := buf.find("<")) >= 0 and (e := buf.find(">", s)) >= 0:
                msg = parse_message(buf[s + 1:e])
                buf = buf[e + 1:]
                if msg and msg[0] == "REP":
                    replies += 1
                    if msg[2] != "ERR":
                        info[msg[2]] = msg[3].strip("{} ").strip()
    except OSError:
        pass
    finally:
        writer.close()
    if "MODEL" not in info:
        return None  # something listens on the port but it isn't a Shure device
    prof = match_shure_model(info["MODEL"])
    return Found("shure", host, port, prof.model if prof else info["MODEL"], prof is not None,
                 info.get("DEVICE_ID", ""), info.get("RF_BAND", ""))


class _UdpCollector(asyncio.DatagramProtocol):
    def __init__(self):
        self.replies: dict[tuple[str, int], dict[str, str]] = {}

    def datagram_received(self, data: bytes, addr) -> None:
        d = self.replies.setdefault((addr[0], addr[1]), {})
        for cmd, value in parse_lines(data):
            d[cmd.lower()] = value


async def probe_sennheiser(hosts: list[str], ports: list[int], wait: float = 1.5,
                           progress: Callable[[int], None] | None = None) -> list[Found]:
    loop = asyncio.get_running_loop()
    transport, proto = await loop.create_datagram_endpoint(_UdpCollector, local_addr=("0.0.0.0", 0))
    try:
        sent = 0
        for host in hosts:
            for port in ports:
                try:
                    transport.sendto(b"Name\rBand\r", (host, port))
                except OSError:
                    pass
            sent += 1
            if sent % 64 == 0:
                if progress:
                    progress(sent)
                await asyncio.sleep(0.01)  # don't flood the switch / socket buffer
        if progress:
            progress(sent)
        await asyncio.sleep(wait)
    finally:
        transport.close()
    return [Found("sennheiser_g4", h, p, "SR IEM G4", True, d.get("name", ""), d.get("band", ""))
            for (h, p), d in sorted(proto.replies.items()) if "name" in d]


async def scan(hosts: Iterable[str], shure_ports: list[int], senn_ports: list[int],
               progress: Callable[[int, int, str], None] | None = None,
               concurrency: int = 128) -> list[Found]:
    """Probe every host. ``progress(done, total, phase)`` is called as it goes."""
    hosts = list(hosts)
    found: list[Found] = []
    if shure_ports:
        sem = asyncio.Semaphore(concurrency)
        total = len(hosts) * len(shure_ports)
        done = 0

        async def one(h: str, p: int):
            nonlocal done
            async with sem:
                r = await probe_shure(h, p)
            done += 1
            if progress and (done % 16 == 0 or done == total):
                progress(done, total, "Shure (TCP)")
            return r

        res = await asyncio.gather(*(one(h, p) for h in hosts for p in shure_ports))
        found += [r for r in res if r]
    if senn_ports:
        n = len(hosts)
        found += await probe_sennheiser(
            hosts, senn_ports,
            progress=(lambda d: progress(d, n, "Sennheiser (UDP)")) if progress else None)
    return found
