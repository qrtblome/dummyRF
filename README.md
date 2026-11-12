# dummyRF

RF manager for Shure and Sennheiser wireless, written in Python (PySide6).

- **Remote control**: Shure ULX-D, QLX-D, SLX-D, Axient Digital, PSM1000 (P10T), Axient Digital PSM (ADTQ);
  Sennheiser EW IEM G4 (SR IEM G4 / SR 2050 IEM).
- **Frequency coordination**: inventory, 2-tx 3rd / 3-tx 3rd / 2-tx 5th intermod avoidance, TV-channel and
  manual exclusions, scan-peak avoidance, conflict check, push the plan back to linked devices.
- **Spectrum view**: import scans (WWB / RF Explorer / generic CSV), overlay carriers, exclusions and IMD products.
- **Simulators** for every supported model, so it all runs without hardware.

## Install

Keep the virtualenv **outside OneDrive**. OneDrive moves files off-disk and breaks the code signatures
of the Qt libraries, and macOS then refuses to load PySide6.

```bash
python3 -m venv ~/.venvs/dummyrf
~/.venvs/dummyrf/bin/pip install -e ".[dev]"
```

## Run

```bash
~/.venvs/dummyrf/bin/python -m dummyrf            # the app
~/.venvs/dummyrf/bin/python -m dummyrf --demo     # app + simulated rack + demo scan
~/.venvs/dummyrf/bin/python -m dummyrf.sim        # simulators only (add --host 0.0.0.0 to expose them)
~/.venvs/dummyrf/bin/python -m pytest             # tests
```

## Layout

| Path | What |
|---|---|
| `dummyrf/profiles.py` | **Model profiles**: protocol command names, value encodings, channel counts, bands |
| `dummyrf/drivers/shure.py` | Shure command-string protocol, TCP 2202 (`< GET 1 CHAN_NAME >`) |
| `dummyrf/drivers/sennheiser.py` | Sennheiser G3/G4 text protocol, UDP 53212 (`Frequency 548000`) |
| `dummyrf/coordination.py` | IMD maths, `check()`, `coordinate()` generator, spacing profiles |
| `dummyrf/scan.py` | Scan import |
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
device discovery (SLP/mDNS), live meter streaming (`METER_RATE` / `Push`), and scan import from receivers.
