"""ATORCH BW600 USB-HID protocol.

Reverse engineered from the vendor PC software (Load_Application.exe v1.0.8,
"ATORCH LOAD_Tester Software" for CL24/BW150/BW600/DL150).

Host -> device (zero padded to one 64 byte HID report)::

    55 05 <addr> <cmd> d0 d1 d2 d3 EE FF

Values are big-endian; floating point settings are IEEE-754 float32.

Device -> host (64 byte report)::

    AA 05 <addr> <type> ...payload... EE FF      (EE FF at offsets 62/63)

type 0x03 - settings snapshot (big-endian float32 fields + bytes)
type 0x05 - live measurements (little-endian uint32, value * 1000)
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field, fields
from enum import IntEnum

VID = 0x0483
PID = 0x5750
REPORT_SIZE = 64
DEFAULT_ADDR = 0x01

TX_HEAD = b"\x55\x05"
RX_HEAD = 0xAA
TAIL = b"\xee\xff"


class Cmd(IntEnum):
    """Command byte (offset 3 of a host frame)."""

    READ_SETTINGS = 0x03   # 55 05 01 03 00 00 00 EE FF -> type 03 reply
    READ_LIVE = 0x05       # 55 05 01 05 0B 00 8C EE FF -> type 05 reply
    LANGUAGE = 0x20        # d0 = 1..4
    SET_VALUE = 0x21       # float: current (CC) / voltage (CV) / resistance (CR) / power (CP)
    WORK_BRIGHTNESS = 0x22   # d3 = level 0..9
    STANDBY_BRIGHTNESS = 0x23  # d3 = level 0..9
    STANDBY_TIME = 0x24    # d3 = value
    RUN = 0x25             # d0 = 1 start, 0 stop
    CAL_TEMP = 0x26        # float
    CAL_VOLTAGE = 0x27     # float
    CAL_CURRENT = 0x28     # float
    CUTOFF_VOLTAGE = 0x29  # float, discharge end ("zero") voltage
    FULL_VOLTAGE = 0x2A    # float, charge full voltage
    FULL_CURRENT = 0x2B    # float, charge end current
    OVER_CURRENT = 0x2C    # float
    OVER_POWER = 0x2D      # float
    NTC_OVER_TEMP = 0x2E   # float
    MOS_OVER_TEMP = 0x2F   # float
    TIME_LIMIT = 0x31      # d0 = value, d3 = 1 (hours) / 2 (minutes)
    CLEAR_CAPACITY = 0x32  # "clear current" button: resets accumulated Ah/Wh
    FACTORY_RESET = 0x33
    DATA_ZERO = 0x34       # zero offset of measurements
    CYCLE_COUNT = 0x37     # d3 = number of cycles for the charge/discharge cycle test
    SELECT_MODE = 0x47     # 0x47 + Mode (0..9); no payload. Found in the device firmware.


# Valid ranges of the single-byte settings.
BYTE_RANGES = {
    Cmd.WORK_BRIGHTNESS: (0, 9),
    Cmd.STANDBY_BRIGHTNESS: (0, 9),
    Cmd.STANDBY_TIME: (0, 255),
    Cmd.CYCLE_COUNT: (1, 255),
}


def check_byte(cmd: int, value: int) -> int:
    lo, hi = BYTE_RANGES.get(cmd, (0, 255))
    if not lo <= value <= hi:
        raise ValueError(f"value must be {lo}..{hi}")
    return value


# Calibration: only written through BW600.write_calibration() after an explicit
# unlock, and only with factors inside CAL_LIMITS.
CAL_COMMANDS = frozenset({Cmd.CAL_TEMP, Cmd.CAL_VOLTAGE, Cmd.CAL_CURRENT})
CAL_FIELDS = {Cmd.CAL_VOLTAGE: "cal_voltage", Cmd.CAL_CURRENT: "cal_current", Cmd.CAL_TEMP: "cal_temp"}
CAL_LIMITS = (0.5, 1.5)


def check_calibration(value: float) -> float:
    lo, hi = CAL_LIMITS
    if not (lo <= value <= hi):  # also rejects NaN
        raise ValueError(f"calibration factor must be between {lo} and {hi}")
    return value


# Settings restored by BW600.apply_settings() (calibration is separate and locked).
RESTORABLE_FLOATS = {
    "set_value": Cmd.SET_VALUE, "cutoff_voltage": Cmd.CUTOFF_VOLTAGE, "full_voltage": Cmd.FULL_VOLTAGE,
    "full_current": Cmd.FULL_CURRENT, "over_current": Cmd.OVER_CURRENT, "over_power": Cmd.OVER_POWER,
    "ntc_over_temp": Cmd.NTC_OVER_TEMP, "mos_over_temp": Cmd.MOS_OVER_TEMP,
}
RESTORABLE_BYTES = {
    "work_brightness": Cmd.WORK_BRIGHTNESS, "standby_brightness": Cmd.STANDBY_BRIGHTNESS,
    "standby_time": Cmd.STANDBY_TIME, "cycle_count": Cmd.CYCLE_COUNT,
}


def firmware_version(device_name: str) -> str | None:
    """'APP ATORCH BW600 V2.0.5' -> '2.0.5' (the USB product string carries the version)."""
    import re
    m = re.search(r"V(\d+(?:\.\d+)+)", device_name or "")
    return m.group(1) if m else None


# Commands that must never be sent from this tool (firmware upgrade path).
FORBIDDEN_RAW = {0x02}


class Mode(IntEnum):
    CC = 0
    CV = 1
    CR = 2
    CP = 3
    INTERNAL_RESISTANCE = 4
    POWER_SUPPLY_TEST = 5
    CABLE_TEST = 6
    CHARGE_DISCHARGE_CHARGE = 7
    CDCDC = 8
    CYCLE_TEST = 9


MODE_NAMES = {
    Mode.CC: "Constant current (CC)",
    Mode.CV: "Constant voltage (CV)",
    Mode.CR: "Constant resistance (CR)",
    Mode.CP: "Constant power (CP)",
    Mode.INTERNAL_RESISTANCE: "Internal resistance test",
    Mode.POWER_SUPPLY_TEST: "Power supply test",
    Mode.CABLE_TEST: "Cable test",
    Mode.CHARGE_DISCHARGE_CHARGE: "Charge-discharge-charge",
    Mode.CDCDC: "Charge-discharge x2-charge",
    Mode.CYCLE_TEST: "Charge/discharge cycle test",
}

# Short names for the command line.
MODE_KEYS = {
    "cc": Mode.CC, "cv": Mode.CV, "cr": Mode.CR, "cp": Mode.CP,
    "ir": Mode.INTERNAL_RESISTANCE, "psu": Mode.POWER_SUPPLY_TEST, "cable": Mode.CABLE_TEST,
    "cdc": Mode.CHARGE_DISCHARGE_CHARGE, "cdcdc": Mode.CDCDC, "cycle": Mode.CYCLE_TEST,
}

# Modes whose settings reply carries no meaningful set value.
NO_SET_VALUE_MODES = {Mode.INTERNAL_RESISTANCE, Mode.POWER_SUPPLY_TEST, Mode.CABLE_TEST}

# (label, unit) of the SET_VALUE parameter per mode, as shown by the vendor app.
SET_VALUE_LABEL = {
    Mode.CC: ("Set current", "A"),
    Mode.CV: ("Set voltage", "V"),
    Mode.CR: ("Set resistance", "Ω"),
    Mode.CP: ("Set power", "W"),
}


def mode_name(mode: int | None) -> str:
    if mode is None:
        return "?"
    try:
        return MODE_NAMES[Mode(mode)]
    except ValueError:
        return f"Unknown ({mode})"


# ---------------------------------------------------------------------------
# Host -> device frames
# ---------------------------------------------------------------------------

def frame(cmd: int, data: bytes = b"\x00\x00\x00\x00", addr: int = DEFAULT_ADDR) -> bytes:
    """Build a 10 byte command frame."""
    if len(data) != 4:
        raise ValueError("payload must be 4 bytes")
    return TX_HEAD + bytes([addr & 0xFF, cmd & 0xFF]) + data + TAIL


def poll_frame(cmd: int, addr: int = DEFAULT_ADDR) -> bytes:
    """9 byte poll frame exactly as sent by the vendor software timer."""
    return TX_HEAD + bytes([addr & 0xFF, cmd & 0xFF, 0x0B, 0x00, 0x8C]) + TAIL


def read_settings_frame(addr: int = DEFAULT_ADDR) -> bytes:
    return TX_HEAD + bytes([addr & 0xFF, Cmd.READ_SETTINGS, 0, 0, 0]) + TAIL


def float_frame(cmd: int, value: float, addr: int = DEFAULT_ADDR) -> bytes:
    return frame(cmd, struct.pack(">f", float(value)), addr)


def byte_frame(cmd: int, value: int, addr: int = DEFAULT_ADDR) -> bytes:
    """Single byte value in d3 (brightness / standby sliders, combo boxes)."""
    return frame(cmd, bytes([0, 0, 0, int(value) & 0xFF]), addr)


def run_frame(on: bool, addr: int = DEFAULT_ADDR) -> bytes:
    return frame(Cmd.RUN, bytes([1 if on else 0, 0, 0, 0]), addr)


def language_frame(option: int, addr: int = DEFAULT_ADDR) -> bytes:
    return frame(Cmd.LANGUAGE, bytes([option & 0xFF, 0, 0, 0]), addr)


def time_limit_frames(hours: int, minutes: int, addr: int = DEFAULT_ADDR) -> list[bytes]:
    if not (0 <= hours <= 255 and 0 <= minutes <= 59):
        raise ValueError("hours 0..255, minutes 0..59")
    return [
        frame(Cmd.TIME_LIMIT, bytes([hours, 0, 0, 1]), addr),
        frame(Cmd.TIME_LIMIT, bytes([minutes, 0, 0, 2]), addr),
    ]


def mode_frame(mode: int, addr: int = DEFAULT_ADDR) -> bytes:
    mode = Mode(mode)
    return frame(Cmd.SELECT_MODE + mode, b"\x00\x00\x00\x00", addr)


def simple_frame(cmd: int, addr: int = DEFAULT_ADDR) -> bytes:
    return frame(cmd, b"\x00\x00\x00\x00", addr)


def pad(report: bytes) -> bytes:
    if len(report) > REPORT_SIZE:
        raise ValueError("report too long")
    return report + bytes(REPORT_SIZE - len(report))


# ---------------------------------------------------------------------------
# Device -> host frames
# ---------------------------------------------------------------------------

@dataclass
class Settings:
    set_value: float = 0.0
    cal_temp: float = 0.0
    cal_voltage: float = 0.0
    cal_current: float = 0.0
    cutoff_voltage: float = 0.0
    full_voltage: float = 0.0
    full_current: float = 0.0
    over_current: float = 0.0
    over_power: float = 0.0
    ntc_over_temp: float = 0.0
    mos_over_temp: float = 0.0
    mode: int = 0
    language: int = 0
    work_brightness: int = 0
    standby_brightness: int = 0
    standby_time: int = 0
    time_limit_h: int = 0
    time_limit_m: int = 0
    cycle_count: int = 0
    raw: bytes = field(default=b"", repr=False)


# Order of the eleven big-endian floats at offsets 4..47 of a type 03 frame.
_SETTINGS_FLOATS = (
    "set_value", "cal_temp", "cal_voltage", "cal_current", "cutoff_voltage",
    "full_voltage", "full_current", "over_current", "over_power",
    "ntc_over_temp", "mos_over_temp",
)
_SETTINGS_BYTES = (
    "mode", "language", "work_brightness", "standby_brightness",
    "standby_time", "time_limit_h", "time_limit_m", "cycle_count",
)


@dataclass
class Live:
    voltage: float | None = None       # V
    current: float | None = None       # A, signed as the vendor app: negative while discharging
    power: float | None = None         # W
    resistance: float | None = None    # Ω
    energy: float | None = None        # Wh
    capacity: float | None = None      # mAh
    ntc_temp: float | None = None      # °C (external probe)
    cpu_temp: float | None = None      # °C
    mos_temp: float | None = None      # °C
    fan: float | None = None           # fan duty in % (set automatically by the firmware)
    running: bool = False
    status_a: int = 0                  # byte 0x35
    status_b: int = 0                  # byte 0x36
    flags: int = 0                     # byte 0x3c (sign bits)
    raw: bytes = field(default=b"", repr=False)

    @property
    def discharging(self) -> bool:
        """Flag 0x80: current flows into the load (the vendor app shows it negative)."""
        return bool(self.flags & 0x80)

    @property
    def amps(self) -> float | None:
        """Current magnitude; use ``discharging`` for the direction."""
        return None if self.current is None else abs(self.current)

    def as_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self) if f.name != "raw"}


# (field, offset, max raw value accepted, sign flag bit in byte 0x3c)
_LIVE_LAYOUT = (
    ("voltage", 0x08, 500_000, 0),
    ("current", 0x0C, 1_500_000, 0x80),
    ("power", 0x10, 420_000_000, 0),
    ("resistance", 0x14, 1_000_000_000, 0),
    ("energy", 0x18, 1_000_000_000, 0),
    ("capacity", 0x1C, 1_000_000_000, 0),
    ("cpu_temp", 0x20, 300_000, 0x40),   # whole degrees
    ("ntc_temp", 0x24, 300_000, 0x20),   # external probe
    ("mos_temp", 0x28, 300_000, 0x10),
    ("fan", 0x2C, 10_000_000, 0),
)


def is_reply(buf: bytes) -> bool:
    return len(buf) >= 10 and buf[0] == RX_HEAD and buf[1] == 0x05


def reply_type(buf: bytes) -> int | None:
    return buf[3] if is_reply(buf) else None


def parse_settings(buf: bytes) -> Settings | None:
    if not is_reply(buf) or buf[3] != 0x03 or len(buf) < 64 or buf[62:64] != TAIL:
        return None
    s = Settings(raw=bytes(buf))
    for i, name in enumerate(_SETTINGS_FLOATS):
        setattr(s, name, struct.unpack_from(">f", buf, 4 + 4 * i)[0])
    for i, name in enumerate(_SETTINGS_BYTES):
        setattr(s, name, buf[0x30 + i])
    return s


def parse_live(buf: bytes, previous: Live | None = None) -> Live | None:
    if not is_reply(buf) or buf[3] != 0x05 or len(buf) < 64:
        return None
    flags = buf[0x3C]
    live = Live(raw=bytes(buf), flags=flags)
    for name, off, limit, sign in _LIVE_LAYOUT:
        raw = struct.unpack_from("<I", buf, off)[0]
        if raw < limit:
            value = raw / 1000.0
            if sign and flags & sign:
                value = -value
        else:  # out of range: the vendor app keeps the previous value
            value = getattr(previous, name) if previous else None
        setattr(live, name, value)
    live.running = buf[0x34] != 0
    live.status_a = buf[0x35]
    live.status_b = buf[0x36]
    return live


def parse_scan(buf: bytes) -> int | None:
    """Device scan reply (type 04): returns the device address."""
    if is_reply(buf) and buf[3] == 0x04 and buf[8:10] == TAIL and buf[4] == 1:
        return buf[2]
    return None


def hexdump(b: bytes) -> str:
    return " ".join(f"{x:02X}" for x in b)


# ---------------------------------------------------------------------------
# Stop reason
# ---------------------------------------------------------------------------
# The device does not report why it switched the load off: only the run flag
# (byte 0x34) changes. The reason is inferred from the readings just before
# the stop, compared with the configured limits.

@dataclass
class StopEvent:
    reason: str        # short machine-readable key
    message: str       # human readable explanation
    inferred: bool = True


def infer_stop_reason(window: list[Live], settings: Settings | None,
                      run_seconds: float | None = None,
                      requested: bool = False) -> StopEvent:
    """Explain a running -> idle transition.

    ``window`` holds the live samples of the last few seconds while the load
    was running (oldest first). ``run_seconds`` is how long the load ran as
    observed by this program; ``requested`` is True if we sent the stop.
    """
    if requested:
        return StopEvent("user", "Stopped from this application.", inferred=False)
    samples = [s for s in window if s.voltage is not None]
    if not samples or settings is None:
        return StopEvent("unknown", "Stopped by the device (it does not report a reason).")

    v_min = min(s.voltage for s in samples)
    v_last = samples[-1].voltage
    i_max = max((s.amps or 0) for s in samples)
    p_max = max((s.power or 0) for s in samples)
    t_mos = max((s.mos_temp or 0) for s in samples)
    t_ntc = max((s.ntc_temp or 0) for s in samples)
    charging = any(s.running and not s.discharging and (s.amps or 0) > 0.01 for s in samples)

    def near(value, limit, rel=0.01, abs_=0.02):
        return value >= limit - max(abs_, rel * abs(limit))

    if v_min < 0.5 and v_last < 0.5:
        return StopEvent("no_input", f"Input voltage lost ({v_last:.3f} V): battery/source disconnected?")
    if settings.mos_over_temp > 0 and near(t_mos, settings.mos_over_temp, abs_=1.0):
        return StopEvent("mos_otp", f"MOSFET over-temperature: {t_mos:.1f} °C ≥ {settings.mos_over_temp:g} °C.")
    if settings.ntc_over_temp > 0 and near(t_ntc, settings.ntc_over_temp, abs_=1.0):
        return StopEvent("ntc_otp", f"Probe over-temperature: {t_ntc:.1f} °C ≥ {settings.ntc_over_temp:g} °C.")
    if settings.over_current > 0 and near(i_max, settings.over_current):
        return StopEvent("ocp", f"Over-current protection: {i_max:.3f} A ≥ {settings.over_current:g} A.")
    if settings.over_power > 0 and near(p_max, settings.over_power):
        return StopEvent("opp", f"Over-power protection: {p_max:.2f} W ≥ {settings.over_power:g} W.")
    if charging:
        if settings.full_voltage > 0 and near(max(s.voltage for s in samples), settings.full_voltage):
            return StopEvent("charged", f"Charge complete: full voltage {settings.full_voltage:g} V reached.")
        if settings.full_current > 0 and min(s.amps or 0 for s in samples) <= settings.full_current * 1.1:
            return StopEvent("charged", f"Charge complete: current fell to the end current {settings.full_current:g} A.")
    elif settings.cutoff_voltage > 0 and v_min <= settings.cutoff_voltage + max(0.02, 0.01 * settings.cutoff_voltage):
        return StopEvent("cutoff", f"Cut-off voltage reached: {v_min:.3f} V ≤ {settings.cutoff_voltage:g} V "
                                   f"(discharge finished).")
    limit_s = (settings.time_limit_h * 60 + settings.time_limit_m) * 60
    if limit_s and run_seconds is not None and run_seconds >= limit_s - 5:
        return StopEvent("time", f"Time limit reached ({settings.time_limit_h} h {settings.time_limit_m} min).")
    return StopEvent("device", "Stopped on the device (button/knob) or for a reason it does not report.")
