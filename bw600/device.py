"""Linux hidraw transport and background poller for the ATORCH BW600."""

from __future__ import annotations

import collections
import glob
import json
import os
import select
import threading
import time
from dataclasses import dataclass
from typing import Callable

from . import protocol as p
from .alarms import AlarmConfig, AlarmEvent, AlarmMonitor


@dataclass
class DeviceInfo:
    path: str
    name: str
    serial: str


def find_devices() -> list[DeviceInfo]:
    """Return all hidraw nodes that belong to a BW600 (0483:5750)."""
    found = []
    want = f"{p.VID:08X}:{p.PID:08X}"
    for node in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            with open(os.path.join(node, "device", "uevent")) as f:
                env = dict(line.strip().split("=", 1) for line in f if "=" in line)
        except OSError:
            continue
        if want in env.get("HID_ID", "").upper():
            found.append(DeviceInfo(
                path="/dev/" + os.path.basename(node),
                name=env.get("HID_NAME", "ATORCH BW600"),
                serial=env.get("HID_UNIQ", ""),
            ))
    return found


class DeviceError(Exception):
    pass


class CalibrationLocked(DeviceError):
    pass


class FactoryResetLocked(DeviceError):
    pass


def settings_snapshot_path(serial: str) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return os.path.join(BACKUP_DIR, f"settings-{serial or 'unknown'}-{stamp}.json")


def save_settings_snapshot(serial: str, s: p.Settings, path: str | None = None) -> str:
    """Write all settings (including mode and calibration, for reference) to JSON."""
    path = path or settings_snapshot_path(serial)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    data = {k: v for k, v in s.__dict__.items() if k != "raw"}
    data["serial"] = serial
    data["saved"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return path


def load_settings_snapshot(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


BACKUP_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "bw600")


def calibration_backup_path(serial: str) -> str:
    return os.path.join(BACKUP_DIR, f"calibration-{serial or 'unknown'}.json")


def load_calibration_backup(serial: str) -> dict | None:
    """Calibration factors saved the first time this device was seen."""
    try:
        with open(calibration_backup_path(serial), encoding="utf-8") as f:
            data = json.load(f)
        return {k: float(data[k]) for k in p.CAL_FIELDS.values()}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _save_calibration_backup(serial: str, s: p.Settings) -> None:
    path = calibration_backup_path(serial)
    if os.path.exists(path):
        return
    values = {k: getattr(s, k) for k in p.CAL_FIELDS.values()}
    try:
        if not all(p.check_calibration(v) for v in values.values()):
            return
    except ValueError:
        return  # never back up implausible values
    os.makedirs(BACKUP_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({**values, "saved": time.strftime("%Y-%m-%d %H:%M:%S")}, f, indent=2)


class HidrawDevice:
    def __init__(self, path: str):
        self.path = path
        try:
            self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        except PermissionError as e:
            raise DeviceError(
                f"No permission to open {path}. Install the udev rule "
                f"(see README: 'sudo ./install-udev-rule.sh') and replug the device."
            ) from e
        except OSError as e:
            raise DeviceError(f"Cannot open {path}: {e}") from e

    def write(self, report: bytes) -> None:
        # Leading 0x00 = "no report ID"; the kernel strips it and sends 64 bytes.
        data = b"\x00" + p.pad(report)
        try:
            os.write(self.fd, data)
        except OSError as e:
            raise DeviceError(f"write failed: {e}") from e

    def read(self, timeout: float) -> bytes | None:
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return None
        try:
            return os.read(self.fd, 256)
        except BlockingIOError:
            return None
        except OSError as e:
            raise DeviceError(f"read failed: {e}") from e

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass


class BW600:
    """High level BW600 client.

    A background thread alternates the two poll requests used by the vendor
    software (settings / live data), dispatches replies and executes queued
    commands in between so writes never collide with polling.
    """

    def __init__(self, path: str | None = None, interval: float = 0.25):
        if path is None:
            devs = find_devices()
            if not devs:
                raise DeviceError("No ATORCH BW600 (USB 0483:5750) found.")
            path = devs[0].path
            self.info = devs[0]
        else:
            self.info = next((d for d in find_devices() if d.path == path),
                             DeviceInfo(path, "ATORCH BW600", ""))
        self.dev = HidrawDevice(path)
        self.addr = p.DEFAULT_ADDR
        self.interval = interval
        self.live: p.Live | None = None
        self.settings: p.Settings | None = None
        self.last_rx = 0.0
        self.error: str | None = None
        self._queue: list[bytes] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._tick = 0
        self.on_live: list[Callable[[p.Live], None]] = []
        self.on_settings: list[Callable[[p.Settings], None]] = []
        self.on_raw: list[Callable[[str, bytes], None]] = []
        self.on_stop: list[Callable[[p.StopEvent], None]] = []
        self.last_stop: p.StopEvent | None = None
        self._window: collections.deque = collections.deque()
        self._run_since: float | None = None
        self._stop_requested_at = 0.0
        self._stop_note: str | None = None
        self.alarms = AlarmMonitor(AlarmConfig.load())
        self._cal_unlocked_until = 0.0
        self._reset_unlocked_until = 0.0
        self._cal_backup_done = False
        self.on_alarm: list[Callable[[AlarmEvent], None]] = []

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> "BW600":
        self._thread = threading.Thread(target=self._run, name="bw600-poll", daemon=True)
        self._thread.start()
        return self

    def close(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self.dev.close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()

    @property
    def online(self) -> bool:
        return self.error is None and time.monotonic() - self.last_rx < 3.0

    # -- commands ----------------------------------------------------------
    def send(self, report: bytes, _calibration: bool = False) -> None:
        if len(report) >= 4 and report[:2] == p.TX_HEAD and report[3] in p.FORBIDDEN_RAW:
            raise ValueError("Firmware-upgrade commands are blocked.")
        if len(report) >= 4 and report[:2] == p.TX_HEAD and report[3] in p.CAL_COMMANDS and not _calibration:
            raise CalibrationLocked("Calibration can only be changed with write_calibration() after unlocking.")
        if len(report) >= 4 and report[:2] == p.TX_HEAD and report[3] == p.Cmd.FACTORY_RESET and not _calibration:
            raise FactoryResetLocked("Factory reset can only be done with factory_reset() after unlocking.")
        with self._lock:
            self._queue.append(report)

    def run(self, on: bool, note: str | None = None) -> None:
        """Switch the load; ``note`` explains an automatic stop in the stop event."""
        if not on:
            self._stop_requested_at = time.monotonic()
            self._stop_note = note
        else:
            self.alarms.reset()
        self.send(p.run_frame(on, self.addr))

    def start_load(self) -> None:
        self.run(True)

    def stop_load(self) -> None:
        self.run(False)

    # -- calibration (locked by default) -----------------------------------
    @property
    def calibration_unlocked(self) -> bool:
        return time.monotonic() < self._cal_unlocked_until

    def calibration_unlock_remaining(self) -> float:
        return max(0.0, self._cal_unlocked_until - time.monotonic())

    def unlock_calibration(self, seconds: float = 120.0) -> None:
        self._cal_unlocked_until = time.monotonic() + seconds

    def lock_calibration(self) -> None:
        self._cal_unlocked_until = 0.0
        self._reset_unlocked_until = 0.0

    def write_calibration(self, values: dict) -> None:
        """Write calibration factors {Cmd.CAL_*: value}. Requires unlock_calibration();
        every value must be within CAL_LIMITS. Locks again afterwards."""
        if not self.calibration_unlocked:
            raise CalibrationLocked("Calibration is locked. Unlock it first.")
        if self.live and self.live.running:
            raise DeviceError("Stop the load before changing calibration.")
        for cmd, value in values.items():
            if cmd not in p.CAL_COMMANDS:
                raise ValueError(f"not a calibration command: {cmd}")
            p.check_calibration(value)
        try:
            for cmd, value in values.items():
                self.send(p.float_frame(cmd, value, self.addr), _calibration=True)
        finally:
            self.lock_calibration()
        self.request_settings()

    # -- factory reset (locked by default) ---------------------------------
    @property
    def factory_reset_unlocked(self) -> bool:
        return time.monotonic() < self._reset_unlocked_until

    def unlock_factory_reset(self, seconds: float = 60.0) -> None:
        self._reset_unlocked_until = time.monotonic() + seconds

    def lock_factory_reset(self) -> None:
        self._reset_unlocked_until = 0.0

    def factory_reset(self) -> str:
        """Reset all settings and calibration to factory values (the firmware also resets
        the Tuya WiFi module into pairing mode). Requires
        unlock_factory_reset(); saves a settings snapshot first and returns its path."""
        if not self.factory_reset_unlocked:
            raise FactoryResetLocked("Factory reset is locked. Unlock it first.")
        if self.live and self.live.running:
            raise DeviceError("Stop the load before a factory reset.")
        if self.settings is None:
            raise DeviceError("Settings not read yet; refusing to reset without a backup.")
        try:
            path = save_settings_snapshot(self.info.serial, self.settings)
            self.send(p.simple_frame(p.Cmd.FACTORY_RESET, self.addr), _calibration=True)
        finally:
            self.lock_factory_reset()
        self.request_settings()
        return path

    def apply_settings(self, snapshot: dict) -> list[str]:
        """Write the non-calibration settings of a snapshot back (load must be off).
        Returns the names written. Calibration is restored separately (locked)."""
        if self.live and self.live.running:
            raise DeviceError("Stop the load before restoring settings.")
        written = []
        if "mode" in snapshot:
            self.set_mode(int(snapshot["mode"]))
            written.append("mode")
        for name, cmd in p.RESTORABLE_FLOATS.items():
            if name in snapshot and not (name == "set_value" and int(snapshot.get("mode", 0)) in p.NO_SET_VALUE_MODES):
                self.send(p.float_frame(cmd, float(snapshot[name]), self.addr))
                written.append(name)
        for name, cmd in p.RESTORABLE_BYTES.items():
            if name in snapshot:
                self.send(p.byte_frame(cmd, p.check_byte(cmd, int(snapshot[name])), self.addr))
                written.append(name)
        if "language" in snapshot:
            self.send(p.language_frame(int(snapshot["language"]), self.addr))
            written.append("language")
        if "time_limit_h" in snapshot:
            for f in p.time_limit_frames(int(snapshot["time_limit_h"]), int(snapshot.get("time_limit_m", 0)), self.addr):
                self.send(f)
            written.append("time_limit")
        self.request_settings()
        return written

    def set_float(self, cmd: p.Cmd, value: float) -> None:
        if cmd in p.CAL_COMMANDS:
            self.write_calibration({cmd: value})
            return
        self.send(p.float_frame(cmd, value, self.addr))
        self.request_settings()

    def set_value(self, value: float) -> None:
        self.set_float(p.Cmd.SET_VALUE, value)

    def set_byte(self, cmd: p.Cmd, value: int) -> None:
        p.check_byte(cmd, value)
        self.send(p.byte_frame(cmd, value, self.addr))
        self.request_settings()

    def set_time_limit(self, hours: int, minutes: int) -> None:
        for f in p.time_limit_frames(hours, minutes, self.addr):
            self.send(f)
        self.request_settings()

    def set_language(self, option: int) -> None:
        self.send(p.language_frame(option, self.addr))
        self.request_settings()

    def set_mode(self, mode: int) -> None:
        """Select the operating mode (the load must be off)."""
        if self.live and self.live.running:
            raise DeviceError("Stop the load before changing the mode.")
        self.send(p.mode_frame(mode, self.addr))
        self.request_settings()

    def simple(self, cmd: p.Cmd) -> None:
        if cmd == p.Cmd.FACTORY_RESET:
            self.factory_reset()
            return
        self.send(p.simple_frame(cmd, self.addr))
        self.request_settings()

    def request_settings(self) -> None:
        self.send(p.read_settings_frame(self.addr))

    # -- internals ---------------------------------------------------------
    def _emit_raw(self, direction: str, data: bytes) -> None:
        for cb in list(self.on_raw):
            try:
                cb(direction, data)
            except Exception:
                pass

    def _write(self, report: bytes) -> None:
        self.dev.write(report)
        self._emit_raw("TX", report)

    def _drain(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return
            buf = self.dev.read(left)
            if buf is None:
                return
            self._handle(buf)

    def _handle(self, buf: bytes) -> None:
        self.last_rx = time.monotonic()
        self._emit_raw("RX", buf)
        t = p.reply_type(buf)
        if t == 0x05:
            live = p.parse_live(buf, self.live)
            if live:
                event = self._track_run(live)
                self.live = live
                for cb in list(self.on_live):
                    cb(live)
                for alarm in self.alarms.check(live):
                    if self.alarms.config.stop_load and live.running:
                        self.run(False, note=f"Alarm: {alarm.message}")
                    for cb in list(self.on_alarm):
                        cb(alarm)
                if event:
                    for cb in list(self.on_stop):
                        cb(event)
        elif t == 0x03:
            s = p.parse_settings(buf)
            if s:
                self.settings = s
                if not self._cal_backup_done:
                    self._cal_backup_done = True
                    try:
                        _save_calibration_backup(self.info.serial, s)
                    except OSError:
                        pass
                for cb in list(self.on_settings):
                    cb(s)
        elif t == 0x04:
            addr = p.parse_scan(buf)
            if addr is not None:
                self.addr = addr

    def _track_run(self, live: p.Live) -> p.StopEvent | None:
        now = time.monotonic()
        was_running = bool(self.live and self.live.running)
        if live.running:
            if not was_running:
                self._run_since = now
                self._window.clear()
            self._window.append((now, live))
            while self._window and now - self._window[0][0] > 5.0:
                self._window.popleft()
        elif was_running:
            run_s = now - self._run_since if self._run_since else None
            requested = now - self._stop_requested_at < 3.0
            if requested and self._stop_note:
                event = p.StopEvent("alarm", f"Stopped by this application — {self._stop_note}", inferred=False)
            else:
                event = p.infer_stop_reason([s for _t, s in self._window], self.settings, run_s, requested)
            self._stop_note = None
            self.last_stop = event
            self._window.clear()
            self._run_since = None
            return event
        return None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                with self._lock:
                    pending, self._queue = self._queue, []
                for report in pending:
                    self._write(report)
                    self._drain(0.05)
                cmd = p.Cmd.READ_LIVE if self._tick % 2 == 0 else p.Cmd.READ_SETTINGS
                self._tick += 1
                self._write(p.poll_frame(cmd, self.addr))
                self._drain(self.interval)
                self.error = None
            except DeviceError as e:
                self.error = str(e)
                time.sleep(1.0)
