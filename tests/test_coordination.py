import numpy as np

from dummyrf.coordination import Exclusion, Slot, check, coordinate, imd_products, tv_channel
from dummyrf.scan import Scan, combine_max, parse_scan_text


def test_imd_products_basic():
    prods = imd_products([500000, 501000], fifth=True)
    freqs = sorted(p for p, _, _ in prods)
    assert 499000 in freqs and 502000 in freqs      # 3rd order 2a-b
    assert 498000 in freqs and 503000 in freqs      # 5th order 3a-2b
    three = imd_products([500000, 501000, 503000])
    assert any(p == 500000 + 501000 - 503000 for p, o, _ in three)


def test_check_finds_imd_hit():
    slots = [Slot("A", freq_khz=500000), Slot("B", freq_khz=501000), Slot("C", freq_khz=502000)]
    kinds = {c.kind for c in check(slots)}
    assert "imd3" in kinds   # 2*501 - 500 = 502


def test_tv_channel():
    e = tv_channel(21)
    assert (e.lo_khz, e.hi_khz) == (470000, 478000)
    assert tv_channel(14, "US").hi_khz == 476000


def _clean(slots, excl=(), fifth=True):
    return [c for c in check(slots, excl, fifth)]


def test_coordinate_mixed_rack_is_clean():
    slots = [Slot(f"MIC{i}", "digital_mic", 470000, 534000) for i in range(16)]
    slots += [Slot(f"IEM{i}", "analog_iem", 470000, 542000) for i in range(8)]
    excl = [tv_channel(23), tv_channel(27)]
    res = coordinate(slots, excl, seed=1, time_limit=10)
    assert not res.unplaced, res.unplaced
    for i, f in res.freqs.items():
        slots[i].freq_khz = f
    assert _clean(slots, excl) == []


def test_coordinate_respects_fixed_and_scan():
    slots = [Slot("FIX", "analog_iem", 470000, 500000, freq_khz=480000, fixed=True)]
    slots += [Slot(f"S{i}", "digital_mic", 470000, 500000) for i in range(6)]
    f = np.arange(470000, 500000, 50.0)
    lv = np.full(len(f), -100.0)
    lv[(f > 485000) & (f < 490000)] = -50
    res = coordinate(slots, scan=(f, lv), threshold_dbm=-80, seed=3)
    assert res.freqs[0] == 480000
    for i, fr in res.freqs.items():
        slots[i].freq_khz = fr
        if i:
            assert not 484900 < fr < 490100
    assert check(slots, scan=(f, lv), threshold_dbm=-80) == []


def test_coordinate_reports_unplaceable():
    slots = [Slot(f"S{i}", "digital_mic", 500000, 501000) for i in range(5)]
    res = coordinate(slots, time_limit=0.5, seed=0)
    assert res.unplaced


def test_parse_scan_formats():
    wwb = "Frequency,Level\n470.000,-98.5\n470.025,-97.0\n470.050,-60\n"
    s = parse_scan_text(wwb)
    assert s.freqs_khz[0] == 470000 and s.levels_dbm[-1] == -60
    rfe = "RF Explorer\nFrequency (MHZ),Amplitude (dBm)\n\"470.000\",\"-90\"\n\"470.100\",\"-91\"\n"
    s = parse_scan_text(rfe)
    assert len(s.freqs_khz) == 2
    eu = "470000000;-99,5\n470025000;-98,0\n"
    s = parse_scan_text(eu)
    assert s.freqs_khz[1] == 470025 and s.levels_dbm[0] == -99.5
    grid, lv = combine_max([s, Scan("b", np.array([470010.0]), np.array([-40.0]))])
    assert lv.max() == -40
