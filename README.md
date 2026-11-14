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

## Finding devices on the network

Devices tab → **Scan network…** (or Tools → Scan network, Ctrl+F). Pick the adapter connected to the RF
network; the address range is filled in from its IP and subnet mask. Each address is probed on the control
ports (Shure TCP 2202, Sennheiser UDP 53212) rather than pinged, so every reply comes back already
identified (model, name, band). A /24 takes a few seconds. Choose **Loopback** to find the demo rack.

On macOS, allow "Local Network" access for Terminal (or the app) when asked, otherwise nothing is found.

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
