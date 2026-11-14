import asyncio
import os
import stat
import sys
import textwrap

import numpy as np
import pytest

from dummyrf import rfscan
from dummyrf.scan import Scan, crc16_arc, load_scan, write_csv, write_sdb3


def test_parse_rtl_power_line():
    hop = rfscan.parse_rtl_power_line(
        "2026-11-14, 15:00:01, 470000000, 470100000, 25000.00, 20, -40.5, -41.0, nan, -39.0")
    assert hop.hz_low == 470e6 and len(hop.values) == 4
    assert list(hop.freqs_hz) == [470e6, 470.025e6, 470.05e6, 470.075e6]
    assert np.isnan(hop.values[2])
    assert rfscan.parse_rtl_power_line("garbage") is None


def _hops(start=470000000, n_hops=3, bins=4, step=25000.0, level=-40.0):
    out = []
    for h in range(n_hops):
        lo = start + h * bins * step
        vals = ", ".join(f"{level - i:.1f}" for i in range(bins))
        out.append(f"2026-11-14, 15:00:0{h}, {lo:.0f}, {lo + bins * step:.0f}, {step}, 10, {vals}")
    return out


def test_assembler_completes_sweep_on_wrap():
    asm = rfscan.SweepAssembler(470000, 470300)
    lines = _hops() + _hops(level=-30)
    sweeps = [s for s in (asm.feed(rfscan.parse_rtl_power_line(ln)) for ln in lines) if s]
    assert len(sweeps) == 1                         # second sweep is still open
    f, v = sweeps[0]
    assert f[0] == 470000 and f[-1] == 470275 and len(f) == 12
    last = asm.flush()
    assert last is not None and last[1].max() == -30


def test_peak_hold():
    ph = rfscan.PeakHold()
    f = np.array([1.0, 2.0, 3.0])
    ph.add(f, np.array([-50.0, -60.0, -70.0]))
    ph.add(f, np.array([-55.0, -40.0, np.nan]))
    assert list(ph.levels) == [-50.0, -40.0, -70.0] and ph.sweeps == 2


def test_settings_validation():
    with pytest.raises(ValueError):
        rfscan.ScanSettings(start_khz=10_000, stop_khz=20_000).validate()
    with pytest.raises(ValueError):
        rfscan.ScanSettings(start_khz=600_000, stop_khz=500_000).validate()
    rfscan.ScanSettings().validate()


def test_rtl_power_command():
    sc = rfscan.RtlPowerScanner(rfscan.ScanSettings(start_khz=470000, stop_khz=608000,
                                                    step_khz=25, gain_db=28.0, ppm=3),
                                lambda f, v: None, binary="/x/rtl_power")
    assert sc.command() == ["/x/rtl_power", "-f", "470000k:608000k:25k", "-i", "1", "-c", "0.2",
                            "-d", "0", "-g", "28", "-p", "3", "-"]


@pytest.fixture
def fake_rtl_power(tmp_path):
    """A stand-in for rtl_power: prints two sweeps, then keeps going until killed."""
    script = tmp_path / "rtl_power"
    lines = _hops() + _hops(level=-20)
    script.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import sys, time
        print("Found 1 device(s):", file=sys.stderr, flush=True)
        lines = {lines!r}
        for ln in lines:
            print(ln, flush=True)
        while True:
            for ln in lines[:3]:
                print(ln, flush=True)
            time.sleep(0.2)
        """))
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script)


def test_rtl_power_scanner_end_to_end(fake_rtl_power):
    async def go():
        sweeps, status = [], []
        st = rfscan.ScanSettings(start_khz=470000, stop_khz=470300, level_offset_db=-10)
        sc = rfscan.RtlPowerScanner(st, lambda f, v: sweeps.append((f, v)), status.append,
                                    binary=fake_rtl_power)
        task = asyncio.create_task(sc.run())
        for _ in range(100):
            if len(sweeps) >= 2:
                break
            await asyncio.sleep(0.05)
        await sc.stop()
        await asyncio.wait_for(task, 5)          # stops cleanly, no error
        assert len(sweeps) >= 2
        assert sweeps[0][1].max() == -50.0          # -40 dB + offset -10
        assert sweeps[1][1].max() == -30.0
        assert any("Found" in s for s in status)
        assert not sc.running
    asyncio.run(go())


def test_rtl_power_failure_is_reported(tmp_path):
    script = tmp_path / "rtl_power"
    script.write_text(f"#!{sys.executable}\nimport sys\n"
                      "print('No supported devices found.', file=sys.stderr)\nsys.exit(1)\n")
    script.chmod(0o755)

    async def go():
        sc = rfscan.RtlPowerScanner(rfscan.ScanSettings(), lambda f, v: None, binary=str(script))
        with pytest.raises(RuntimeError, match="No supported devices"):
            await sc.run()
    asyncio.run(go())


def test_simulated_scanner_emits_sweeps():
    async def go():
        got = []
        sc = rfscan.SimulatedScanner(rfscan.ScanSettings(start_khz=470000, stop_khz=480000),
                                     lambda f, v: got.append(v), sweep_s=0.01)
        task = asyncio.create_task(sc.run())
        await asyncio.sleep(0.2)
        await sc.stop()
        await task
        assert len(got) > 3 and len(got[0]) == 401
    asyncio.run(go())


def test_crc16_arc_known_value():
    assert crc16_arc(b"123456789") == 0xBB3D        # CRC-16/ARC check value


def test_sdb3_export_roundtrip(tmp_path):
    f = np.arange(562000, 562500, 24.7)              # not on a clean grid, like rtl_power
    v = -100 + 10 * np.sin(f / 50)
    v[5] = -30.0
    path = tmp_path / "out.sdb3"
    write_sdb3(Scan("RTL-SDR peak hold", f, v), path, step_khz=25, scanner="RTL-SDR")
    data = path.read_bytes()
    assert data.startswith(b"//@ShureScan\n{")
    back = load_scan(path)
    assert back.name == "RTL-SDR peak hold (RTL-SDR)"
    assert back.freqs_khz[0] % 25 == 0 and np.all(np.diff(back.freqs_khz) == 25)
    assert back.levels_dbm.max() == pytest.approx(-30.0)       # peak survives regridding
    # checksum of the single sweep must verify
    k = data.index(b"@Binary:") + 8
    rec = data[k:data.index(b"@Extended:")]
    assert crc16_arc(rec[:-2]) == int.from_bytes(rec[-2:], "big")


def test_csv_export_roundtrip(tmp_path):
    s = Scan("x", np.array([470000.0, 470025.0]), np.array([-90.0, -80.5]))
    write_csv(s, tmp_path / "a.csv")
    back = load_scan(tmp_path / "a.csv")
    assert list(back.freqs_khz) == [470000.0, 470025.0] and list(back.levels_dbm) == [-90.0, -80.5]


@pytest.mark.skipif(not os.path.exists("/Users/szymon/Desktop/huj/huj.sdb3"), reason="local file")
def test_real_wwb_file_roundtrip(tmp_path):
    s = load_scan("/Users/szymon/Desktop/huj/huj.sdb3")
    write_sdb3(s, tmp_path / "w.sdb3")
    t = load_scan(tmp_path / "w.sdb3")
    assert np.array_equal(s.freqs_khz, t.freqs_khz) and np.allclose(s.levels_dbm, t.levels_dbm)
