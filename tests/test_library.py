import json

from dummyrf import library
from dummyrf.coordination import Slot, coordinate
from dummyrf.profiles import ALL_MODELS


def test_shipped_library_loads():
    lib = library.load([library.PACKAGE_DIR])
    assert lib.errors == []
    names = {m.full_name for m in lib.models}
    assert {"Shure BLX", "Shure SLXD", "Shure SLXD Dual", "Mipro ACT-747"} <= names
    assert lib.model("Mipro ACT-747").channels_per_unit == 4
    uhf = lib.plans["dvbt2_uhf"]
    e = uhf.exclusion("21")
    assert (e.lo_khz, e.hi_khz) == (470000, 478000)
    dab = lib.plans["dab"].exclusion("12A")
    assert dab.hi_khz - dab.lo_khz == 1536
    st = next(s for s in lib.stations if "Piątkowo" in s.name)
    excl = lib.station_exclusions(st)
    assert len(excl) == 2 and any((x.lo_khz, x.hi_khz) == (518000, 526000) for x in excl)  # DVB-T2 ch27


def test_bands_override_profiles():
    lib = library.load([library.PACKAGE_DIR])
    before = dict(ALL_MODELS["SLXD4D"].bands)
    try:
        updated = library.apply_bands_to_profiles(lib)
        assert "SLXD4" in updated and "SLXD4D" in updated
        assert ALL_MODELS["SLXD4D"].bands["L56"] == (650000, 694000)   # from devices.json
        assert "ULXD4Q" not in updated                                  # no library data
    finally:
        ALL_MODELS["SLXD4D"].bands.clear()
        ALL_MODELS["SLXD4D"].bands.update(before)


def test_user_dir_overrides(tmp_path):
    (tmp_path / "devices.json").write_text(json.dumps({
        "band_sets": {"Shure SLXD": [{"name": "G59", "low_khz": 470000, "high_khz": 500000}]},
        "brands": [{"name": "Shure", "models": [{"name": "SLXD", "channels_per_unit": 1,
                                                 "band_set": "Shure SLXD"}]}]}))
    (tmp_path / "stations.json").write_text("not json")
    lib = library.load([library.PACKAGE_DIR, tmp_path])
    assert [b.name for b in lib.model("Shure SLXD").bands] == ["G59"]
    assert any("stations.json" in e for e in lib.errors)
    assert lib.stations  # shipped stations survive a broken user file


def test_coordinate_with_library_equipment():
    lib = library.load([library.PACKAGE_DIR])
    mipro = lib.model("Mipro ACT-747")
    band = next(b for b in mipro.bands if b.name == "5UA")
    slots = [Slot(f"M{i}", "analog_mic", band.lo_khz, band.hi_khz) for i in range(8)]
    excl = lib.station_exclusions(lib.stations[1])
    res = coordinate(slots, excl, seed=1, time_limit=3)
    assert not res.unplaced
    assert all(not (e.lo_khz <= f <= e.hi_khz) for f in res.freqs.values() for e in excl)
