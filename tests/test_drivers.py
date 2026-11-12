import asyncio

import pytest

from dummyrf.drivers import DriverError, make_driver
from dummyrf.drivers.shure import format_command, parse_message
from dummyrf.sim import SimSpec, make_server


def run(coro):
    return asyncio.run(coro)


def test_parse_message():
    assert parse_message(" REP 1 CHAN_NAME {Lead Vox} ") == ("REP", 1, "CHAN_NAME", "{Lead Vox}")
    assert parse_message("REP MODEL {ULXD4Q}") == ("REP", 0, "MODEL", "{ULXD4Q}")
    assert parse_message("REP ERR") == ("REP", 0, "ERR", "")
    assert format_command("SET", 2, "FREQUENCY", "578350") == "< SET 2 FREQUENCY 578350 >"
    assert format_command("GET", 0, "MODEL") == "< GET MODEL >"


@pytest.mark.parametrize("model,band", [
    ("ULXD4Q", "G51"), ("AD4D", "G56"), ("QLXD4", "H50"), ("SLXD4D", "G59"),
    ("P10T", "G10E"), ("ADTQ", "G56"),
])
def test_shure_roundtrip(model, band):
    async def go():
        srv = make_server(SimSpec(model, band, 0, f"{model} test"))
        srv.port = 0
        await srv.start()
        port = srv._server.sockets[0].getsockname()[1]
        drv = make_driver("shure", "127.0.0.1", port, "auto")
        await drv.connect()
        assert drv.profile.model == model
        st = await drv.refresh()
        assert set(st.channels) == set(range(1, drv.profile.channels + 1))
        assert st.device["band"] == band
        lo, hi = drv.profile.bands[band]
        f = lo + 1000
        await drv.set(1, "frequency", f)
        await drv.set(1, "name", "LEAD")
        st = await drv.refresh()
        assert st.channels[1]["frequency"] == f
        assert st.channels[1]["name"] == "LEAD"
        with pytest.raises(DriverError):
            await drv.set(1, "frequency", hi + 10000)  # outside band -> ERR -> no REP
        lines = await drv.raw("GET 1 CHAN_NAME", listen=0.2)
        assert any("CHAN_NAME" in line for line in lines)
        await drv.close()
        await srv.stop()
    run(go())


def test_shure_gain_offset():
    async def go():
        srv = make_server(SimSpec("ULXD4", "G51", 0, "t"))
        srv.port = 0
        await srv.start()
        port = srv._server.sockets[0].getsockname()[1]
        drv = make_driver("shure", "127.0.0.1", port, "ULXD4")
        await drv.connect()
        await drv.set(1, "gain", 12)
        assert srv.dev.values[(1, "gain")] == 12
        lines = await drv.raw("< GET 1 AUDIO_GAIN >", listen=0.2)
        assert "< REP 1 AUDIO_GAIN 30 >" in lines  # 12 dB + 18 offset
        await drv.close()
        await srv.stop()
    run(go())


def test_sennheiser_roundtrip():
    async def go():
        srv = make_server(SimSpec("SR IEM G4", "A1", 0, "iem"))
        srv.port = 0
        await srv.start()
        port = srv._transport.get_extra_info("sockname")[1]
        drv = make_driver("sennheiser_g4", "127.0.0.1", port)
        await drv.connect()
        await drv.set(1, "frequency", 500100)
        await drv.set(1, "name", "DRUMS")
        await drv.set(1, "mute", True)
        st = await drv.refresh()
        ch = st.channels[1]
        assert (ch["frequency"], ch["name"], ch["mute"]) == (500100, "DRUMS", True)
        assert st.device["band"] == "A1"
        await drv.close()
        await srv.stop()
    run(go())


def test_sennheiser_no_device():
    async def go():
        drv = make_driver("sennheiser_g4", "127.0.0.1", 9)  # discard port, nobody answers
        drv.timeout = 0.2
        with pytest.raises(DriverError):
            await drv.connect()
    run(go())
