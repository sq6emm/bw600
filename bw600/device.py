"""Linux hidraw transport and background poller for the ATORCH BW600."""

from __future__ import annotations

import glob
import os
import select
import threading
import time
from dataclasses import dataclass
from typing import Callable

from . import protocol as p


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
    def send(self, report: bytes) -> None:
        if len(report) >= 4 and report[:2] == p.TX_HEAD and report[3] in p.FORBIDDEN_RAW:
            raise ValueError("Firmware-upgrade commands are blocked.")
        with self._lock:
            self._queue.append(report)

    def run(self, on: bool) -> None:
        self.send(p.run_frame(on, self.addr))

    def start_load(self) -> None:
        self.run(True)

    def stop_load(self) -> None:
        self.run(False)

    def set_float(self, cmd: p.Cmd, value: float) -> None:
        self.send(p.float_frame(cmd, value, self.addr))
        self.request_settings()

    def set_value(self, value: float) -> None:
        self.set_float(p.Cmd.SET_VALUE, value)

    def set_byte(self, cmd: p.Cmd, value: int) -> None:
        self.send(p.byte_frame(cmd, value, self.addr))
        self.request_settings()

    def set_time_limit(self, hours: int, minutes: int) -> None:
        for f in p.time_limit_frames(hours, minutes, self.addr):
            self.send(f)
        self.request_settings()

    def set_language(self, option: int) -> None:
        self.send(p.language_frame(option, self.addr))
        self.request_settings()

    def simple(self, cmd: p.Cmd) -> None:
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
                self.live = live
                for cb in list(self.on_live):
                    cb(live)
        elif t == 0x03:
            s = p.parse_settings(buf)
            if s:
                self.settings = s
                for cb in list(self.on_settings):
                    cb(s)
        elif t == 0x04:
            addr = p.parse_scan(buf)
            if addr is not None:
                self.addr = addr

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
