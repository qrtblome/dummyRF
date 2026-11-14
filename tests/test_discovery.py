import asyncio
import time

import pytest

from dummyrf import discovery
from dummyrf.sim import SimSpec, make_server


def test_parse_ports_and_range():
    assert discovery.parse_ports("2202, 22020-22022") == [2202, 22020, 22021, 22022]
    with pytest.raises(ValueError):
        discovery.parse_ports("1-5000")
    assert discovery.host_range("10.0.0.254", "10.0.1.1") == ["10.0.0.254", "10.0.0.255",
                                                               "10.0.1.0", "10.0.1.1"]
    with pytest.raises(ValueError):
        discovery.host_range("10.0.0.0", "10.0.255.255")


def test_default_range():
    i = discovery.Interface("en0", "Wi-Fi", "192.168.0.100", "255.255.255.0")
    assert discovery.default_range(i) == ("192.168.0.1", "192.168.0.254")
    big = discovery.Interface("en1", "", "10.20.30.40", "255.255.0.0")
    assert discovery.default_range(big) == ("10.20.30.1", "10.20.30.254")


def test_list_interfaces_has_loopback():
    ifaces = discovery.list_interfaces()
    assert any(i.is_loopback for i in ifaces)


def test_scan_finds_simulators():
    async def go():
        servers = [make_server(SimSpec("ULXD4Q", "G51", 0, "Rack A")),
                   make_server(SimSpec("P10T", "G10E", 0, "IEM TX")),
                   make_server(SimSpec("SR IEM G4", "A1", 0, "g4"))]
        for s in servers:
            s.port = 0
            await s.start()
        shure_ports = [s._server.sockets[0].getsockname()[1] for s in servers[:2]]
        g4_port = servers[2]._transport.get_extra_info("sockname")[1]

        # something else listening on TCP that is not a Shure device
        async def junk(r, w):
            w.write(b"HTTP/1.0 400 Bad\r\n\r\n")
            w.close()
        other = await asyncio.start_server(junk, "127.0.0.1", 0)
        other_port = other.sockets[0].getsockname()[1]

        t0 = time.monotonic()
        found = await discovery.scan(["127.0.0.1"], shure_ports + [other_port], [g4_port])
        dt = time.monotonic() - t0
        by_model = {f.model: f for f in found}
        assert set(by_model) == {"ULXD4Q", "P10T", "SR IEM G4"}
        assert by_model["ULXD4Q"].name == "Rack A" and by_model["ULXD4Q"].band == "G51"
        assert by_model["SR IEM G4"].port == g4_port and by_model["SR IEM G4"].band == "A1"
        assert dt < 5
        other.close()
        for s in servers:
            await s.stop()
    asyncio.run(go())


def test_scan_dead_addresses_is_quick():
    # TEST-NET-1 addresses never answer; probes must time out, not hang
    async def go():
        t0 = time.monotonic()
        found = await discovery.scan([f"192.0.2.{i}" for i in range(1, 65)], [2202], [53212])
        assert found == []
        return time.monotonic() - t0
    assert asyncio.run(go()) < 6
