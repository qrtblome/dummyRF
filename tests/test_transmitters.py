import json

import pytest

from dummyrf import library, uke_import

NTC_HDR = ("Lp.;Status;Numer pozwolenia/decyzji;Data pozwolenia/decyzji;Ważny od;Ważny do;Wnioskodawca;"
           "Nazwa multipleksu;Kanał/blok częstotliwościowy;Czestotliwość środkowa [MHz];"
           "Szerokość kanału [MHz];Nazwa stacji;Lokalizacja stacji;Województwo;Typ nadajnika;"
           "Producent nadajnika;Maksymalna dopuszczalna moc wyjściowa nadajnika [kW];Typ anteny;"
           "Konfiguracja systemu antenowego;Producent anteny;Dł.geogr. (WGS84);Sz.geogr. (WGS84);"
           "Wysokość lokalizacji [m. npm];Wysokość anteny [m. npt];Polaryzacja;System emisji;"
           "ERP[dBW];ERP[kW];Charakterystyka;"
           + ";".join(f"{a}°" for a in range(0, 360, 10)) + ";identyfikator")
UKF_HDR = ("Lp.;Status;Numer pozwolenia/decyzji;Data pozwolenia/decyzji;Ważny od;Ważny do;"
           "Data odbioru decyzji;Wnioskodawca;Nadawca;Program;Data na wniosku;Nr wniosku;"
           "Identyfikator;F [MHz];Nazwa stacji;Lokalizacja stacji;Województwo;Typ nadajnika;"
           "Producent nadajnika;Maksymalna dopuszczalna moc wyjściowa nadajnika;Dok. zgodności;"
           "Organ wydający;Nr świadectwa homologacji/deklaracja zgodności;Typ anteny;"
           "Konfiguracja systemu antenowego;Producent anteny;Numer Koncesji/Decyzji KRRiT;"
           "Data wydania Koncesji/Decyzji KRRiT;Dł. geogr.;Sz. geogr.;Układ wsp.;H ter.;H ant.;"
           "Pol.;Syst.;ERP[dBW];ERP[kW];MPX;Char.;"
           + ";".join(f"{a}°" for a in range(0, 360, 10)) + ";id wystąpienia;")
ZEROS = ";".join("0" for _ in range(36))


def ntc_row(lp, status, decided, valid_to, mux, ch, mhz, width, name, region, lon, lat, erp, txid):
    return (f"{lp};{status};X;{decided};{decided};{valid_to};Emitel;{mux};{ch};{mhz};{width};{name};"
            f"{name}, ul. Testowa;{region};T;P;1;A;1x1;P;\"{lon}\";\"{lat}\";100;150;H;DVB-T2;"
            f"50;{erp};ND;{ZEROS};{txid}")


def ukf_row(lp, status, decided, valid_to, program, mhz, name, region, lon, lat, erp, txid):
    return (f"{lp};{status};X;{decided};{decided};{valid_to};{decided};W;N;{program};{decided};1;"
            f"{txid};{mhz};{name};{name};{region};T;P;1;d;o;1;A;1x1;P;1;{decided};\"{lon}\";\"{lat}\";"
            f"WGS84;80;73;V;4;30;{erp};3;ND;{ZEROS};{txid};")


@pytest.fixture
def uke_dir(tmp_path):
    ntc = [NTC_HDR,
           ntc_row(1, "Pozwolenie", "01.02.2025", "31.12.2030", "MUX3", "27", "522", "8", "Poznań Piątkowo",
                   "WIELKOPOLSKIE", "16E54'20\"\"", "52N27'33\"\"", "100", "1"),
           # a later change of the same transmitter (id 1): new power wins
           ntc_row(2, "Zmiana", "01.06.2026", "31.12.2030", "MUX3", "27", "522", "8", "Poznań Piątkowo",
                   "WIELKOPOLSKIE", "16E54'20\"\"", "52N27'33\"\"", "120", "1"),
           ntc_row(3, "Uchylenie", "01.06.2026", "31.12.2030", "MUX9", "40", "626", "8", "Gdzieś",
                   "slaskie", "19E00'00\"\"", "50N00'00\"\"", "1", "2"),
           ntc_row(4, "Pozwolenie", "01.06.2026", "31.12.2030", "MUX8", "7", "191,5", "7", "Katowice",
                   "slaskie", "19E01'00\"\"", "50N16'00\"\"", "10", "3")]
    (tmp_path / "pozwolenia_ntc_r_2026-10-23.csv").write_bytes("\r\n".join(ntc).encode("cp1250"))
    ukf = [UKF_HDR,
           # same tower as Piątkowo but ~150 m away, latitude wrongly marked "E" as in UKE data
           ukf_row(1, "Pozwolenie", "2020-01-01", "2030-01-01", "Program 2", "89,1", "Poznań",
                   "Wielkopolskie", "16E54'25\"\"", "52E27'36\"\"", "10", "10"),
           ukf_row(2, "Czasowe", "2026-05-01", "2026-08-18", "Okazja", "105,5", "Koncert",
                   "MAZOWIECKIE", "21E00'00\"\"", "52E14'00\"\"", "0,1", "11")]
    (tmp_path / "pozwolenie_ukf_r_2026-10-23.csv").write_bytes(
        b"\xef\xbb\xbf" + "\r\n".join(ukf).encode("utf-8"))
    (tmp_path / "pozwolenie_ukf_h_2026-10-23.csv").write_text("ignored history")
    return tmp_path


def test_dms():
    assert uke_import.dms("19E19'41\"", latitude=False) == pytest.approx(19.32806, abs=1e-5)
    assert uke_import.dms("53E01'11\"", latitude=True) == pytest.approx(53.01972, abs=1e-5)
    assert uke_import.dms("nonsense", latitude=True) is None


def test_region_normalisation():
    assert uke_import.region("slaskie") == "ŚLĄSKIE"
    assert uke_import.region("Małopolskie") == "MAŁOPOLSKIE"
    assert uke_import.region("DOLNOSLASKIE") == "DOLNOŚLĄSKIE"


def test_build(uke_dir):
    d = uke_import.build(uke_dir)
    assert d["as_of"] == "2026-10-23"
    assert d["stats"]["revoked"] == 1 and d["stats"]["expired"] == 1
    names = {s["name"]: s for s in d["stations"]}
    assert set(names) == {"Poznań Piątkowo", "Katowice"}           # FM site merged into Piątkowo
    pia = names["Poznań Piątkowo"]
    tv = next(t for t in pia["transmitters"] if t["service"] == "DVB-T2")
    assert tv["erp_kw"] == 120.0 and tv["freq_khz"] == 522000 and tv["width_khz"] == 8000
    fm = next(t for t in pia["transmitters"] if t["service"] == "FM")
    assert fm["freq_khz"] == 89100 and fm["width_khz"] == 200 and fm["program"] == "Program 2"
    kat = names["Katowice"]
    assert kat["region"] == "ŚLĄSKIE" and kat["transmitters"][0]["freq_khz"] == 191500
    assert kat["transmitters"][0]["width_khz"] == 7000


def test_library_loads_sites_and_finds_nearby(uke_dir, tmp_path_factory):
    out = tmp_path_factory.mktemp("lib")
    (out / "transmitters_test.json").write_text(json.dumps(uke_import.build(uke_dir)))
    lib = library.load([out])
    assert lib.errors == [] and len(lib.sites) == 2
    near = lib.nearby(52.4064, 16.9252, 20)
    assert [s.name for _, s in near] == ["Poznań Piątkowo"]
    assert 5 < near[0][0] < 7
    ex = [t.exclusion(lib.plans, "Piątkowo") for t in near[0][1].transmitters]
    tv = next(e for e in ex if e.lo_khz == 518000)
    assert tv.hi_khz == 526000 and "MUX3" in tv.label and "120 kW" in tv.label


def test_parse_coords():
    assert library.parse_coords("52.4064, 16.9252") == (52.4064, 16.9252)
    assert library.parse_coords("52,4064; 16,9252") == (52.4064, 16.9252)
    assert library.parse_coords("52.4N 16.9E") == (52.4, 16.9)
    assert library.parse_coords("40.7 N, 74.0 W") == (40.7, -74.0)
    with pytest.raises(ValueError):
        library.parse_coords("Poznań")


def test_shipped_polish_list():
    lib = library.load([library.PACKAGE_DIR])
    assert lib.errors == []
    assert len(lib.sites) > 700
    services = {t.service for s in lib.sites for t in s.transmitters}
    assert {"DVB-T2", "DAB+", "FM"} <= services
    for s in lib.sites:
        assert 48.9 < s.lat < 55.0 and 14.0 < s.lon < 24.3, s.name
    # Raszyn (Warsaw main TV site) carries several multiplexes
    raszyn = next(s for s in lib.sites if "Raszyn" in s.name)
    assert sum(t.service.startswith("DVB") for t in raszyn.transmitters) >= 5
