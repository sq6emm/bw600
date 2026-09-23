"""WiFi (Tuya local protocol) transport for the BW600-DK.

The WiFi models carry a Tuya module; once paired with the Smart Life / Tuya app,
the device can be controlled on the local network with its *local key*
(obtained once with ``python -m tinytuya wizard``, saved in
~/.config/bw600/tuya/devices.json). Requires the optional ``tinytuya`` package.

Data points (from the product's Tuya thing model, product 1tdki3ux74q1sgoi),
checked against the USB readings of a BW600-DK:

  101 voltage (V, /100)        ro   112 cut-off voltage (V, /100)   rw
  102 current (A, /1000)       ro   113 CPU temperature (°C, /10)   ro
  103 power (W, /100)          ro   114 MOS/heatsink temp (°C, /10) ro
  104 load on/off              rw   115 child lock                  rw
  105 capacity (mAh)           ro   116 test-record slot (enum)     rw
  106 energy (Wh, /100)        ro   117 probe over-temperature (°C) rw
  107 screen brightness 1..9   rw   118 probe temperature (°C, /10) ro
  108 mode (enum)              rw   119 clear capacity + energy     rw
  109 set value (/100)         rw   120 charge end current (A, /100) rw
  110 time limit (hours, /100) rw   121 MOS over-temperature (°C)   rw
  111 charge full voltage (/100) rw 122 live refresh (1 s instead of 60 s) rw
"""

from __future__ import annotations

import json
import os
import threading
import time

from . import protocol as p
from .device import BACKUP_DIR, BW600, DeviceError, DeviceInfo

TUYA_DIR = os.path.join(BACKUP_DIR, "tuya")
DEVICES_FILE = os.path.join(TUYA_DIR, "devices.json")

MODE_TO_ENUM = {
    p.Mode.CC: "CC", p.Mode.CV: "CV", p.Mode.CR: "CR", p.Mode.CP: "CP",
    p.Mode.INTERNAL_RESISTANCE: "BRT", p.Mode.POWER_SUPPLY_TEST: "PT", p.Mode.CABLE_TEST: "CRT",
    p.Mode.CHARGE_DISCHARGE_CHARGE: "D_C_D", p.Mode.CDCDC: "D_C_D_C_D",
}
ENUM_TO_MODE = {v: k for k, v in MODE_TO_ENUM.items()}

# settings attribute -> (dp, scale divisor, integer on the wire)
FLOAT_DPS = {
    p.Cmd.SET_VALUE: ("set_value", 109, 100),
    p.Cmd.FULL_VOLTAGE: ("full_voltage", 111, 100),
    p.Cmd.CUTOFF_VOLTAGE: ("cutoff_voltage", 112, 100),
    p.Cmd.FULL_CURRENT: ("full_current", 120, 100),
    p.Cmd.NTC_OVER_TEMP: ("ntc_over_temp", 117, 1),
    p.Cmd.MOS_OVER_TEMP: ("mos_over_temp", 121, 1),
}
DP_LIMITS = {109: (0, 10_000_000), 111: (120, 25000), 112: (80, 25000), 120: (0, 2500),
             117: (0, 150), 121: (40, 150), 107: (1, 9), 110: (0, 9959)}

WIFI_CAPABILITIES = frozenset({
    "run", "mode", "set_value", "cutoff_voltage", "full_voltage", "full_current", "time_limit",
    "ntc_over_temp", "mos_over_temp", "work_brightness", "clear_capacity",
})
# DP 115 (child lock) exists in the Tuya model but the BW600-DK firmware ignores it
# (writes are not acknowledged and it is never reported), so it is not offered.


class NotOverWifi(DeviceError):
    pass


def load_config(path: str = DEVICES_FILE) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            devices = json.load(f)
    except OSError:
        raise DeviceError(f"No Tuya device configuration ({path}). Run 'python -m tinytuya wizard' "
                          "there first (see README).") from None
    devices = [d for d in devices if d.get("product_id") == "1tdki3ux74q1sgoi"] or devices
    if not devices or not devices[0].get("key"):
        raise DeviceError(f"{path} contains no device with a local key.")
    return devices[0]


class TuyaBW600(BW600):
    """BW600-DK over WiFi. Same interface as the USB client; features the Tuya
    data points don't cover raise NotOverWifi."""

    transport = "WiFi"
    capabilities = WIFI_CAPABILITIES

    def __init__(self, config: dict | None = None, interval: float = 1.0):
        try:
            import tinytuya
        except ImportError:
            raise DeviceError("WiFi needs the 'tinytuya' package: pip install tinytuya "
                              "(see README, 'WiFi').") from None
        self._tinytuya = tinytuya
        cfg = config or load_config()
        self.cfg = cfg
        self.info = DeviceInfo(f"tuya://{cfg.get('ip') or '?'}",
                               cfg.get("name") or "ATORCH BW600-DK (WiFi)", cfg["id"])
        self._init_state(interval)
        self._dps: dict = {}
        self._pending: dict[int, object] = {}        # dp -> value; a newer write replaces an older one
        self._stop_verify_until = 0.0
        self._stop_retries = 0
        self._wake = threading.Event()               # set by new commands: send them right away
        self._refresh_before = None
        self.dev = None

    # -- connection ---------------------------------------------------------
    def _connect(self):
        tt = self._tinytuya
        ip = self.cfg.get("ip")
        if not ip:
            found = tt.find_device(self.cfg["id"])
            ip = found.get("ip") if found else None
            if not ip:
                raise DeviceError("BW600-DK not found on the network.")
            self.cfg["ip"] = ip
        d = tt.Device(self.cfg["id"], ip, self.cfg["key"], version=float(self.cfg.get("version") or 3.5))
        d.set_socketTimeout(5)
        # Not persistent: the device pushes extra status messages after each write, and on a
        # persistent socket later reads return those stale messages (the view lags behind).
        d.set_socketPersistent(False)
        self.info = DeviceInfo(f"tuya://{ip}", self.info.name, self.info.serial)
        self.dev = d

    @property
    def online(self) -> bool:
        return self.error is None and time.monotonic() - self.last_rx < 6.0

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self.dev:
            try:
                if self._refresh_before is False:       # restore the live-refresh switch
                    self.dev.set_value(122, False, nowait=True)
                self.dev.close()
            except Exception:
                pass

    # -- commands (queued, executed by the poll thread) ----------------------
    def _set_dp(self, dp: int, value) -> None:
        lo_hi = DP_LIMITS.get(dp)
        if lo_hi and isinstance(value, int) and not isinstance(value, bool) and not lo_hi[0] <= value <= lo_hi[1]:
            raise ValueError(f"value out of range for this setting ({lo_hi[0]}..{lo_hi[1]} raw)")
        with self._lock:
            self._pending.pop(dp, None)
            self._pending[dp] = value
            if dp == 104 and value is False:           # make sure a stop really happens
                self._stop_verify_until = time.monotonic() + 3.0
                self._stop_retries = 0
        self._wake.set()

    def send(self, report: bytes, _calibration: bool = False) -> None:
        raise NotOverWifi("Raw USB frames can't be sent over WiFi.")

    def run(self, on: bool, note: str | None = None) -> None:
        if not on:
            self._stop_requested_at = time.monotonic()
            self._stop_note = note
        else:
            self.alarms.reset()
        self._set_dp(104, bool(on))

    def set_float(self, cmd: p.Cmd, value: float) -> None:
        if cmd not in FLOAT_DPS:
            raise NotOverWifi("This setting is only available over USB.")
        _name, dp, scale = FLOAT_DPS[cmd]
        self._set_dp(dp, int(round(value * scale)))

    def set_byte(self, cmd: p.Cmd, value: int) -> None:
        if cmd != p.Cmd.WORK_BRIGHTNESS:
            raise NotOverWifi("This setting is only available over USB.")
        if not 1 <= value <= 9:
            raise ValueError("over WiFi the brightness must be 1..9")
        self._set_dp(107, int(value))

    def set_time_limit(self, hours: int, minutes: int) -> None:
        if not (0 <= hours and 0 <= minutes <= 59):
            raise ValueError("hours >= 0, minutes 0..59")
        self._set_dp(110, int(round((hours + minutes / 60) * 100)))   # decimal hours, 2 decimals

    def set_mode(self, mode: int) -> None:
        if self.live and self.live.running:
            raise DeviceError("Stop the load before changing the mode.")
        if mode not in MODE_TO_ENUM:
            raise NotOverWifi(f"{p.mode_name(mode)} can only be selected over USB.")
        self._set_dp(108, MODE_TO_ENUM[mode])

    def set_child_lock(self, on: bool) -> None:
        self._set_dp(115, bool(on))

    def simple(self, cmd: p.Cmd) -> None:
        if cmd == p.Cmd.CLEAR_CAPACITY:
            self._set_dp(119, True)
            return
        raise NotOverWifi("This action is only available over USB.")

    def set_language(self, option: int) -> None:
        raise NotOverWifi("Language can only be set over USB.")

    def write_calibration(self, values: dict) -> None:
        raise NotOverWifi("Calibration can only be changed over USB.")

    def factory_reset(self) -> str:
        raise NotOverWifi("Factory reset is only available over USB.")

    def apply_settings(self, snapshot: dict) -> list[str]:
        raise NotOverWifi("Restoring settings is only available over USB.")

    def request_settings(self) -> None:
        pass  # every poll returns all data points

    # -- data points -> Live / Settings ----------------------------------------
    def _to_live(self, d: dict) -> p.Live:
        def val(dp, div):
            v = d.get(str(dp))
            return None if v is None else v / div
        live = p.Live(voltage=val(101, 100), current=val(102, 1000), power=val(103, 100),
                      energy=val(106, 100), capacity=val(105, 1), cpu_temp=val(113, 10),
                      mos_temp=val(114, 10), ntc_temp=val(118, 10), running=bool(d.get("104")))
        if live.voltage is not None and live.current:
            live.resistance = live.voltage / live.current
        live.flags = 0x80 if live.running and (live.current or 0) > 0 else 0   # shown as discharging
        live.current = -live.current if live.flags and live.current else live.current
        live.raw = json.dumps(d).encode()
        return live

    def _to_settings(self, d: dict) -> p.Settings:
        s = self.settings or p.Settings()
        for _cmd, (name, dp, scale) in FLOAT_DPS.items():
            if str(dp) in d:
                setattr(s, name, d[str(dp)] / scale)
        if "108" in d:
            s.mode = ENUM_TO_MODE.get(d["108"], s.mode)
        if "107" in d:
            s.work_brightness = d["107"]
        if "110" in d:
            total_min = round(d["110"] * 0.6)          # hundredths of an hour -> minutes
            s.time_limit_h, s.time_limit_m = divmod(total_min, 60)
        s.child_lock = bool(d.get("115", False))
        s.record_slot = d.get("116")
        s.raw = b""
        return s

    # -- poll thread -----------------------------------------------------------
    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                if self.dev is None:
                    self._connect()
                    st = self.dev.status()
                    if "Error" in st:
                        raise DeviceError(st.get("Error"))
                    self._refresh_before = st.get("dps", {}).get("122")
                    if self._refresh_before is not True:
                        self.dev.set_value(122, True)          # 1 s updates while we are connected
                with self._lock:
                    pending, self._pending = list(self._pending.items()), {}
                for dp, value in pending:
                    self._emit_raw("TX", json.dumps({dp: value}).encode())
                    r = self.dev.set_value(dp, value)
                    if isinstance(r, dict) and "Error" in r:
                        raise DeviceError(f"write DP {dp}: {r.get('Error')}")
                    if isinstance(r, dict):
                        self._dps.update(r.get("dps") or {})
                st = self.dev.status()
                if not st or "Error" in st:
                    raise DeviceError(st.get("Error") if st else "no reply")
                self._dps.update(st.get("dps", {}))
                self._verify_stop()
                self.last_rx = time.monotonic()
                self._emit_raw("RX", json.dumps(st.get("dps", {})).encode())
                self._process_settings(self._to_settings(self._dps))
                self._process_live(self._to_live(self._dps))
                self.error = None
                backoff = 1.0
                self._wake.wait(self.interval)
                self._wake.clear()
            except Exception as e:  # network errors: reconnect with backoff
                self.error = f"WiFi: {e}"
                try:
                    if self.dev:
                        self.dev.close()
                except Exception:
                    pass
                self.dev = None
                if backoff >= 8:
                    self.cfg["ip"] = None               # DHCP may have moved it: rediscover
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30)

    def _verify_stop(self) -> None:
        """After a stop command: resend while the device still reports running."""
        if not self._stop_verify_until:
            return
        if not self._dps.get("104"):
            self._stop_verify_until = 0.0
            return
        if time.monotonic() >= self._stop_verify_until:
            if self._stop_retries >= 3:
                self._stop_verify_until = 0.0
                raise DeviceError("the load did not stop over WiFi — stop it on the device or over USB!")
            self._stop_retries += 1
            self._stop_verify_until = time.monotonic() + 3.0
            with self._lock:
                self._pending.setdefault(104, False)

    def start(self) -> "TuyaBW600":
        self._thread = threading.Thread(target=self._run, name="bw600-wifi", daemon=True)
        self._thread.start()
        return self
