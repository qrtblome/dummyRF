"""Find ACN devices by listening to their SLP announcements.

Shure receivers multicast an SLPv2 attribute reply to 239.255.254.253:8427
every ~4 s, unprompted, e.g. for an SLXD4D:

    (cid=DD24468A-0000-11DD-A000-000EDDCCCCCC),(acn-fctn=SLXD4D_RX),
    (acn-uacn=SLXD4D J53),(csl-esta.dmp=esta.sdt/192.168.0.19:57383;...)

which gives everything needed to open a session: the session (SDT) address
and port, the device CID — and the model and band.  The multicast group must be
joined on each adapter: without naming one, macOS uses the default route
(often Wi-Fi), not the cable the receivers are on.
"""
from __future__ import annotations

import asyncio
import re
import socket
import struct
from dataclasses import dataclass

GROUP = "239.255.254.253"
PORT = 8427


@dataclass
class Announcement:
    ip: str                 # where the announcement came from
    cid: str
    function: str           # acn-fctn, e.g. "SLXD4D_RX"
    name: str               # acn-uacn, e.g. "SLXD4D J53"
    sdt_host: str
    sdt_port: int

    @property
    def model(self) -> str:
        return self.name.split()[0] if self.name else self.function.split("_")[0]

    @property
    def band(self) -> str:
        parts = self.name.split()
        return parts[1] if len(parts) > 1 else ""


def parse(data: bytes, src_ip: str) -> Announcement | None:
    txt = data.decode("latin-1", errors="replace")
    sdt = re.search(r"esta\.sdt/([\d.]+):(\d+)", txt)
    cid = re.search(r"cid=([0-9A-Fa-f-]{36})", txt)
    if not (sdt and cid):
        return None
    fctn = re.search(r"acn-fctn=([^)]*)", txt)
    uacn = re.search(r"acn-uacn=([^)]*)", txt)
    return Announcement(src_ip, cid.group(1).lower(), fctn.group(1) if fctn else "",
                        uacn.group(1).strip() if uacn else "", sdt.group(1), int(sdt.group(2)))


def _interface_addresses() -> list[str]:
    try:
        import psutil
        return [a.address for addrs in psutil.net_if_addrs().values() for a in addrs
                if a.family == socket.AF_INET and not a.address.startswith("127.")]
    except Exception:  # noqa: BLE001
        return []


def _socket(interfaces: list[str]) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)   # WWB may be listening too
    s.bind(("", PORT))
    joined = 0
    for ip in interfaces or ["0.0.0.0"]:
        try:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                         struct.pack("4s4s", socket.inet_aton(GROUP), socket.inet_aton(ip)))
            joined += 1
        except OSError:
            continue
    if not joined:
        s.close()
        raise OSError("could not join the ACN discovery group on any adapter")
    s.setblocking(False)
    return s


class _Collector(asyncio.DatagramProtocol):
    def __init__(self):
        self.found: dict[str, Announcement] = {}
        self.got = asyncio.Event()

    def datagram_received(self, data, addr):
        a = parse(data, addr[0])
        if a and a.function and not a.function.upper().startswith("WWB"):
            self.found[a.ip] = a
            self.got.set()


async def listen(seconds: float = 5.0, want_ip: str | None = None,
                 interfaces: list[str] | None = None) -> dict[str, Announcement]:
    """Collect announcements for ``seconds`` (or until ``want_ip`` is heard)."""
    loop = asyncio.get_running_loop()
    sock = _socket(interfaces if interfaces is not None else _interface_addresses())
    tr, proto = await loop.create_datagram_endpoint(_Collector, sock=sock)
    try:
        end = loop.time() + seconds
        while loop.time() < end:
            if want_ip and want_ip in proto.found:
                break
            proto.got.clear()
            try:
                await asyncio.wait_for(proto.got.wait(), max(0.05, end - loop.time()))
            except asyncio.TimeoutError:
                break
        return dict(proto.found)
    finally:
        tr.close()


async def find(ip: str, seconds: float = 9.0) -> Announcement | None:
    """The announcement of the device at ``ip`` (they repeat every ~4 s)."""
    return (await listen(seconds, want_ip=ip)).get(ip)
