# dummyRF

RF manager for Shure and Sennheiser wireless, written in Python (PySide6).

- **Remote control**: Shure ULX-D, QLX-D, SLX-D, Axient Digital, PSM1000 (P10T), Axient Digital PSM (ADTQ);
  Sennheiser EW IEM G4 (SR IEM G4 / SR 2050 IEM).
- **Frequency coordination**: inventory, 2-tx 3rd / 3-tx 3rd / 2-tx 5th intermod avoidance, TV-channel and
  manual exclusions, scan-peak avoidance, conflict check, push the plan back to linked devices.
- **Spectrum view**: import scans (WWB / RF Explorer / generic CSV), overlay carriers, exclusions and IMD products.
- **Network scan**: pick an adapter (Wi-Fi / Ethernet), sweep its subnet and add every device that answers.
  Devices can also be added by IP while offline; they connect by themselves once reachable.
- **Simulators** for every supported model, so it all runs without hardware.

## Install

Keep the virtualenv **out of any cloud-synced folder** (OneDrive, iCloud Desktop & Documents, Dropbox).
Sync tools evict files, break the Qt libraries' code signatures, or (iCloud) set the macOS "hidden" flag on
everything inside `.venv`. Qt then skips its own platform plugin and the app aborts at start with
`Could not find the Qt platform plugin "cocoa"`. `~/.venvs/` is a safe place:

```bash
python3 -m venv ~/.venvs/dummyrf
source ~/.venvs/dummyrf/bin/activate
cd /path/to/dummyRF.py
pip install -e ".[dev]"
```

## Run

```bash
source ~/.venvs/dummyrf/bin/activate
python -m dummyrf            # the app
python -m dummyrf --demo     # app + simulated rack + demo scan
python -m dummyrf.sim        # simulators only (add --host 0.0.0.0 to expose them)
python -m pytest             # tests
```

## Building the desktop app (macOS)

```bash
packaging/build_app.sh          # -> ~/Applications/dummyRF.app
packaging/build_app.sh --zip    # … plus a .zip to copy to another Mac
```

PyInstaller bundles Python, Qt and the data (transmitter list, postal codes) into a normal `.app`; the
script installs PyInstaller into the virtualenv if needed. The app is a snapshot — run the script again
after changing the code.

- It builds in `~/Library/Caches/dummyRF-build`, not in the project: in an iCloud/OneDrive folder the sync
  attributes break code signing and the app wouldn't start.
- The app is signed only for your own Mac (ad-hoc). Allow **Local Network** access on first launch.
  Giving it to others without warnings needs an Apple Developer ID to sign and notarise it.
- The build matches your Mac's processor (Apple Silicon here). Windows needs its own build on a Windows PC.

## Scanning with wireless receivers

Shure receivers have no scan command over their command-string port; Wireless Workbench scans over ACN.
Three ways to use receiver scans (Spectrum tab):

- **Auto-load WWB scans** — scan with your receivers in WWB as usual; every scan WWB saves to
  `~/Documents/Shure/Frequency Plots` (folder changeable under **WWB…**) appears in dummyRF within a few
  seconds and can be used for coordination. **WWB… → Load latest WWB scan now** fetches the newest one.
- **SLX-D scan (native)** — runs the SLX-D's own RF scan without WWB, over ACN (ANSI E1.17, the protocol
  WWB uses): dummyRF finds the receiver by its announcement, opens a session, starts the scan on the chosen
  channel and streams the results (dBm per 25 kHz) as they arrive — about a minute for a whole band. The
  channel's audio is off during the scan, as in WWB; the receiver tunes back by itself. Code: `dummyrf/acn/`.
- **Receiver sweep (Shure)** — dummyRF retunes one receiver channel step by step across its band and reads
  its RF level (`RX_RF_LVL`). Works on ULX-D (not SLX-D: it doesn't report the level; dummyRF checks this
  before changing anything). The channel's audio is off during the sweep and its frequency is restored
  afterwards. Slow (~0.4 s per step) and coarse (~200 kHz receiver filter) — use a spare channel.

**Levels:** each scanner source has its own level offset, remembered between runs — RTL-SDR starts at
−70 dB (it reads about 70 dB above Shure receivers), Shure receivers at 0 dB (real dBm). The Spectrum tab's
**Normalize** option shifts every scan so its noise floor matches a reference scan (the first one from a Shure
receiver or WWB, else the first); the shift is shown in the legend and coordination uses the same levels.

## Finding devices on the network

Devices tab → **Scan network…** (or Tools → Scan network, Ctrl+F). Pick the adapter connected to the RF
network; the address range is filled in from its IP and subnet mask. Each address is probed on the control
ports (Shure TCP 2202, Sennheiser UDP 53212) rather than pinged, so every reply comes back already
identified (model, name, band). A /24 takes a few seconds. Choose **Loopback** to find the demo rack.

On macOS, allow "Local Network" access for Terminal (or the app) when asked, otherwise nothing is found.

## Broadcast transmitters (Poland)

`dummyrf/data/transmitters_pl.json` holds every TV (DVB-T/T2), DAB and FM transmitter from the UKE permit
registers (741 sites, 2,038 transmitters as of 2026-10-23). Coordination tab → **Nearby transmitters…**: enter
the venue's coordinates (or pick a site), a radius, and add the channels you see as exclusions.

To refresh it, download the current `pozwolenia_ntc_r_*.csv`, `pozwolenia_dab_r_*.csv` and
`pozwolenie_ukf_r_*.csv` registers from UKE into `dummyrf/data/transmitters/` and run:

```bash
python -m dummyrf.uke_import
```

The `_h_` (history) files are not needed.

**Predicted levels:** for every transmitter the dialog estimates the channel power at the venue (dBm at the
receivers' antenna) from ERP, frequency, distance, antenna heights and — when the register has one — the
directional antenna pattern, using the Okumura-Hata model (urban / suburban / open) or free space (worst
case). No terrain data: treat it as a guide. Transmitters above the "tick at or above" level are ticked for
exclusion; above the "neighbours" level the adjacent channels are excluded as well. Model settings are
saved with the show (`dummyrf/propagation.py`).

**Venue position:** type a postal code (`60-685`), a town (`Poznań`, `srem`) or coordinates — looked up offline
in `dummyrf/data/postcodes_pl.tsv.gz` (postal codes from [GeoNames](https://www.geonames.org), CC BY 4.0;
refresh with `python -m dummyrf.geocode build PL.txt` from https://download.geonames.org/export/zip/PL.zip).
For a street address or venue name, **Search online** asks OpenStreetMap Nominatim (© OpenStreetMap
contributors); only what you type is sent, one request per click. Other countries can be added as further `transmitters_*.json`
files in the same format, in `dummyrf/data/` or `~/.dummyrf/`.

## Notes

- Message boxes are drawn by Qt rather than as native macOS alerts: on macOS 27 the native alert can crash
  the process while drawing its icon (`dummyrf/gui/dialogs.py`).

## Layout

| Path | What |
|---|---|
| `dummyrf/profiles.py` | **Model profiles**: protocol command names, value encodings, channel counts, bands |
| `dummyrf/drivers/shure.py` | Shure command-string protocol, TCP 2202 (`< GET 1 CHAN_NAME >`) |
| `dummyrf/drivers/sennheiser.py` | Sennheiser G3/G4 text protocol, UDP 53212 (`Frequency 548000`) |
| `dummyrf/coordination.py` | IMD maths, `check()`, `coordinate()` generator, spacing profiles |
| `dummyrf/scan.py` | Scan import |
| `dummyrf/discovery.py` | Adapter list and subnet sweep |
| `dummyrf/manager.py` | Runs drivers on an asyncio thread and polls devices for the GUI |
| `dummyrf/sim/` | Device simulators and the demo rack |
| `dummyrf/gui/` | Devices, Coordination and Spectrum tabs |

## Not verified on hardware yet

The protocol details were written from documentation knowledge and **have not been tested on real units**.
Expect to correct some of them. They are all data in `dummyrf/profiles.py`, so fixes are one-line edits:

- Command keywords per model (e.g. `TX_BATT_BARS` vs `BATT_BARS`, `RF_TX_LVL`, `AUDIO_MUTE`), value offsets
  (gain +18, RF level +128), and name lengths.
- Sennheiser G4 IEM command names beyond `Name` / `Frequency` (`Mute`, `Sensitivity`, `RfPower`).
- Band edges for every model. Check them against your units' labels.
- Spacing defaults in `coordination.py` are conservative starting points, not manufacturer figures.

The **raw command console** on the Devices tab sends anything you type to the selected device and shows every
reply. Use it to check what a real unit actually answers.

## Not in v1 yet

Sennheiser EW-DX / EW-D (SSC protocol), EW G4 mic receivers (same protocol as the IEMs, needs a profile),
Shure SLP multicast discovery, live meter streaming (`METER_RATE` / `Push`), and scan import from receivers.
