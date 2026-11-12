"""Device model profiles.

Everything protocol-specific that is likely to need correcting against real
hardware lives here as data: command keywords, value encodings, channel counts
and frequency bands.  None of this has been verified on physical units yet —
entries are marked ``verified=False`` until someone confirms them on gear.

Frequencies are integers in kHz throughout the code base.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Canonical parameter keys used by the GUI and coordination code.
NAME = "name"
FREQUENCY = "frequency"
GAIN = "gain"
MUTE = "mute"
RF_POWER = "rf_power"
BATTERY = "battery"
RF_LEVEL = "rf_level"
AUDIO_LEVEL = "audio_level"
GROUP_CHANNEL = "group_channel"

# Value kinds
STR = "str"        # free text, max ``width`` chars
FREQ = "freq"      # frequency, kHz int
INT = "int"        # integer, protocol value = value + offset
BOOL = "bool"      # on/off
ENUM = "enum"      # one of ``choices``


@dataclass(frozen=True)
class Param:
    key: str            # canonical key (see constants above)
    cmd: str            # protocol keyword, e.g. "CHAN_NAME" or "Frequency"
    label: str
    kind: str
    writable: bool = True
    width: int = 0      # STR max length / FREQ digit count (Shure)
    offset: int = 0     # INT: raw = value + offset
    lo: int | None = None
    hi: int | None = None
    unit: str = ""
    choices: tuple[str, ...] = ()
    true_raw: str = "ON"    # BOOL encodings
    false_raw: str = "OFF"


@dataclass(frozen=True)
class ModelProfile:
    family: str                 # "shure" or "sennheiser_g4"
    model: str                  # e.g. "ULXD4Q"
    description: str
    channels: int
    params: tuple[Param, ...]
    bands: dict[str, tuple[int, int]] = field(default_factory=dict)  # kHz
    coord_profile: str = "digital_mic"
    device_params: tuple[Param, ...] = ()  # params without a channel index
    verified: bool = False

    def param(self, key: str) -> Param | None:
        for p in self.params:
            if p.key == key:
                return p
        return None


def _mhz(lo: float, hi: float) -> tuple[int, int]:
    return int(round(lo * 1000)), int(round(hi * 1000))


# --------------------------------------------------------------------------
# Shure command-string protocol (TCP 2202)
# --------------------------------------------------------------------------

SHURE_DEVICE_PARAMS = (
    Param("model", "MODEL", "Model", STR, writable=False),
    Param("device_id", "DEVICE_ID", "Device ID", STR, width=31),
    Param("band", "RF_BAND", "Band", STR, writable=False),
    Param("fw_ver", "FW_VER", "Firmware", STR, writable=False),
)


def _shure_rx_params(name_width: int, battery_cmd: str, has_mute: bool) -> tuple[Param, ...]:
    params = [
        Param(NAME, "CHAN_NAME", "Name", STR, width=name_width),
        Param(FREQUENCY, "FREQUENCY", "Frequency", FREQ, width=6),
        Param(GAIN, "AUDIO_GAIN", "Gain", INT, offset=18, lo=-18, hi=42, unit="dB"),
        Param(GROUP_CHANNEL, "GROUP_CHAN", "Group/Ch", STR, writable=False),
        Param(BATTERY, battery_cmd, "Battery", INT, writable=False, unit="bars"),
        Param(RF_LEVEL, "RX_RF_LVL", "RF", INT, writable=False, offset=128, unit="dBm"),
        Param(AUDIO_LEVEL, "AUDIO_LVL", "Audio", INT, writable=False, offset=50, unit="dBFS"),
    ]
    if has_mute:
        params.insert(3, Param(MUTE, "AUDIO_MUTE", "Mute", BOOL))
    return tuple(params)


def _shure_tx_params(name_width: int) -> tuple[Param, ...]:
    return (
        Param(NAME, "CHAN_NAME", "Name", STR, width=name_width),
        Param(FREQUENCY, "FREQUENCY", "Frequency", FREQ, width=6),
        Param(RF_POWER, "RF_TX_LVL", "RF power", ENUM, choices=("LOW", "NORMAL", "HIGH")),
        Param(MUTE, "RF_MUTE", "RF mute", BOOL),
        Param(GROUP_CHANNEL, "GROUP_CHAN", "Group/Ch", STR, writable=False),
        Param(AUDIO_LEVEL, "AUDIO_IN_LVL", "Input", INT, writable=False, offset=50, unit="dBFS"),
    )


ULXD_BANDS = {
    "G50": _mhz(470, 534), "G51": _mhz(470, 534), "H50": _mhz(534, 598),
    "H51": _mhz(534, 598), "J50A": _mhz(572, 608), "K51": _mhz(606, 670),
    "L50": _mhz(632, 696), "P51": _mhz(710, 782),
}
QLXD_BANDS = {
    "G50": _mhz(470, 534), "H50": _mhz(534, 598), "K51": _mhz(606, 670),
    "L50": _mhz(632, 696),
}
SLXD_BANDS = {
    "G59": _mhz(470, 514), "H55": _mhz(514, 558), "J52": _mhz(558, 602),
    "K59": _mhz(606, 650), "L56": _mhz(646, 690),
}
AD_BANDS = {
    "G56": _mhz(470, 636), "G57": _mhz(470, 616), "K54": _mhz(606, 810),
    "K55": _mhz(606, 694),
}
PSM1000_BANDS = {
    "G10E": _mhz(470, 542), "H8E": _mhz(518, 584), "J8E": _mhz(554, 626),
    "K10E": _mhz(596, 668), "L8E": _mhz(626, 668),
}

SHURE_MODELS = [
    ModelProfile("shure", "ULXD4", "Shure ULX-D single receiver", 1,
                 _shure_rx_params(8, "BATT_BARS", False), ULXD_BANDS, "digital_mic", SHURE_DEVICE_PARAMS),
    ModelProfile("shure", "ULXD4D", "Shure ULX-D dual receiver", 2,
                 _shure_rx_params(8, "BATT_BARS", False), ULXD_BANDS, "digital_mic", SHURE_DEVICE_PARAMS),
    ModelProfile("shure", "ULXD4Q", "Shure ULX-D quad receiver", 4,
                 _shure_rx_params(8, "BATT_BARS", False), ULXD_BANDS, "digital_mic", SHURE_DEVICE_PARAMS),
    ModelProfile("shure", "QLXD4", "Shure QLX-D receiver", 1,
                 _shure_rx_params(8, "BATT_BARS", False), QLXD_BANDS, "digital_mic", SHURE_DEVICE_PARAMS),
    ModelProfile("shure", "SLXD4", "Shure SLX-D single receiver", 1,
                 _shure_rx_params(8, "BATT_BARS", False), SLXD_BANDS, "digital_mic", SHURE_DEVICE_PARAMS),
    ModelProfile("shure", "SLXD4D", "Shure SLX-D dual receiver", 2,
                 _shure_rx_params(8, "BATT_BARS", False), SLXD_BANDS, "digital_mic", SHURE_DEVICE_PARAMS),
    ModelProfile("shure", "AD4D", "Shure Axient Digital dual receiver", 2,
                 _shure_rx_params(31, "TX_BATT_BARS", True), AD_BANDS, "digital_mic", SHURE_DEVICE_PARAMS),
    ModelProfile("shure", "AD4Q", "Shure Axient Digital quad receiver", 4,
                 _shure_rx_params(31, "TX_BATT_BARS", True), AD_BANDS, "digital_mic", SHURE_DEVICE_PARAMS),
    ModelProfile("shure", "P10T", "Shure PSM1000 transmitter", 2,
                 _shure_tx_params(8), PSM1000_BANDS, "analog_iem", SHURE_DEVICE_PARAMS),
    ModelProfile("shure", "ADTQ", "Shure Axient Digital PSM quad transmitter", 4,
                 _shure_tx_params(31), AD_BANDS, "analog_iem", SHURE_DEVICE_PARAMS),
]

# --------------------------------------------------------------------------
# Sennheiser evolution wireless G4 (UDP 53212, plain-text commands)
# --------------------------------------------------------------------------

G4_BANDS = {
    "A1": _mhz(470, 516), "A": _mhz(516, 558), "AS": _mhz(520, 558),
    "G": _mhz(566, 608), "GB": _mhz(606, 648), "B": _mhz(626, 668),
    "C": _mhz(734, 776), "D": _mhz(780, 822), "E": _mhz(823, 865),
}

SENNHEISER_G4_IEM_PARAMS = (
    Param(NAME, "Name", "Name", STR, width=8),
    Param(FREQUENCY, "Frequency", "Frequency", FREQ),
    Param(MUTE, "Mute", "RF mute", BOOL, true_raw="1", false_raw="0"),
    Param(GAIN, "Sensitivity", "Sensitivity", INT, lo=-42, hi=0, unit="dB"),
    Param(RF_POWER, "RfPower", "RF power", ENUM, choices=("Low", "Standard", "High")),
)

SENNHEISER_MODELS = [
    ModelProfile("sennheiser_g4", "SR IEM G4", "Sennheiser SR IEM G4 / SR 2050 IEM (one channel)", 1,
                 SENNHEISER_G4_IEM_PARAMS, G4_BANDS, "analog_iem",
                 (Param("band", "Band", "Band", STR, writable=False),)),
]

ALL_MODELS: dict[str, ModelProfile] = {m.model: m for m in SHURE_MODELS + SENNHEISER_MODELS}

FAMILIES = {
    "shure": ("Shure (TCP 2202)", 2202),
    "sennheiser_g4": ("Sennheiser EW G4 IEM (UDP 53212)", 53212),
}


def models_for_family(family: str) -> list[ModelProfile]:
    return [m for m in ALL_MODELS.values() if m.family == family]


def match_shure_model(reported: str) -> ModelProfile | None:
    """Map a MODEL string reported by a Shure device to a profile."""
    rep = reported.strip().upper().replace("-", "")
    best = None
    for m in SHURE_MODELS:
        if rep.startswith(m.model) and (best is None or len(m.model) > len(best.model)):
            best = m
    return best
