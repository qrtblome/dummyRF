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


SHURE_OUI = "00:0e:dd"   # MAC prefix of Shure network interfaces


def shure_hosts_from_arp() -> list[str]:
    """IPv4 addresses whose MAC in the ARP table belongs to Shure.

    The sweep itself fills the ARP table, so call this after probing."""
    try:
        out = subprocess.run(["/usr/sbin/arp", "-an"], capture_output=True, text=True,
                             timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    # Empty output can also mean macOS denied this program Local Network access.
    return parse_arp_shure(out)


def parse_arp_shure(out: str) -> list[str]:
    hosts = []
    for m in re.finditer(r"\(([\d.]+)\) at ([0-9a-fA-F:]+)", out):
        mac = ":".join(part.zfill(2) for part in m.group(2).lower().split(":"))
        if mac.startswith(SHURE_OUI):
            hosts.append(m.group(1))
    return hosts


# ------------------------------------------------------------------ probes
_RETRY = "retry"


async def _probe_shure_once(host: str, port: int, timeout: float):
    """Found, None (nothing Shure there) or _RETRY (refused / closed before answering)."""
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except ConnectionRefusedError:
        return _RETRY
    except (OSError, asyncio.TimeoutError):
        return None
    # Only answers to OUR three questions count.  A ULX-D also pushes status
    # updates to every connected client unasked (e.g. "< REP 2
    # TX_MUTE_BUTTON_STATUS RELEASED >"), more of them while WWB or dummyRF is
    # connected; counting those as answers made scans miss units at random.
    asked = ("MODEL", "DEVICE_ID", "RF_BAND")
    info: dict[str, str] = {}
    errs = 0
    spoke_shure = False   # any well-formed REP at all
    got_data = False
    try:
        writer.write(b"< GET MODEL >< GET DEVICE_ID >< GET RF_BAND >")
        await writer.drain()
        buf = ""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 1.5
        while not ("MODEL" in info and sum(k in info for k in asked) + errs >= len(asked)):
            left = deadline - loop.time()
            if left <= 0:
                break
            try:
                data = await asyncio.wait_for(reader.read(4096), left)
            except asyncio.TimeoutError:
                break
            if not data:
                break
            got_data = True
            buf += data.decode("ascii", errors="replace")
            while (s := buf.find("<")) >= 0 and (e := buf.find(">", s)) >= 0:
                msg = parse_message(buf[s + 1:e])
                buf = buf[e + 1:]
                if not msg or msg[0] != "REP":
                    continue
                spoke_shure = True
                verb, ch, cmd, value = msg
                if cmd == "ERR":
                    errs += 1           # RF_BAND on a ULX-D, for instance
                elif ch == 0 and cmd in asked and cmd not in info:
                    info[cmd] = value.strip("{} ").strip()
    except OSError:
        pass
    finally:
        writer.close()
    if "MODEL" not in info:
        # Shure protocol but no model in time (busy unit), or closed without a
        # word: try again.  Something else on the port: not a Shure device.
        return _RETRY if spoke_shure or not got_data else None
    prof = match_shure_model(info["MODEL"])
    return Found("shure", host, port, prof.model if prof else info["MODEL"], prof is not None,
                 info.get("DEVICE_ID", ""), info.get("RF_BAND", ""))


async def probe_shure(host: str, port: int, timeout: float = 0.6, retries: int = 2) -> Found | None:
    """Ask ``host:port`` for its Shure model.

    A ULX-D has only a few TCP connection slots and a just-closed connection
    holds one for a moment, so back-to-back probes are sometimes refused or
    closed straight away (seen on a ULXD4D, fw 2.7.6).  Those cases are retried
    after a short pause; silent addresses are not.
    """
    for attempt in range(retries + 1):
        r = await _probe_shure_once(host, port, timeout)
        if r is not _RETRY:
            return r
        if attempt < retries:
            await asyncio.sleep(0.4 * (attempt + 1))
    return None


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
        # Second chance for Shure hardware the sweep missed (busy connection
        # slots), recognised by the MAC prefix in the ARP table.
        wanted = set(hosts)
        have = {f.host for f in found}
        missed = [ip for ip in shure_hosts_from_arp() if ip in wanted and ip not in have]
        for ip in missed:
            if progress:
                progress(total, total, f"Shure (TCP) — retrying {ip}")
            r = None
            for port in shure_ports:
                r = await probe_shure(ip, port, timeout=1.5, retries=3)
                if r:
                    break
            found.append(r or Found("shure", ip, shure_ports[0], "Shure device — control port closed",
                                    False))
    if senn_ports:
        n = len(hosts)
        found += await probe_sennheiser(
            hosts, senn_ports,
            progress=(lambda d: progress(d, n, "Sennheiser (UDP)")) if progress else None)
    return found
