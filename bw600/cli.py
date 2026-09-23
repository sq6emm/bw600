"""Command line interface: ``python -m bw600 <command>``."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import sys
import time

from . import protocol as p
from .device import BW600, DeviceError, find_devices

FLOAT_CMDS = {
    "value": p.Cmd.SET_VALUE,
    "cutoff": p.Cmd.CUTOFF_VOLTAGE,
    "full-voltage": p.Cmd.FULL_VOLTAGE,
    "full-current": p.Cmd.FULL_CURRENT,
    "ocp": p.Cmd.OVER_CURRENT,
    "opp": p.Cmd.OVER_POWER,
    "ntc-otp": p.Cmd.NTC_OVER_TEMP,
    "mos-otp": p.Cmd.MOS_OVER_TEMP,
    "cal-voltage": p.Cmd.CAL_VOLTAGE,
    "cal-current": p.Cmd.CAL_CURRENT,
    "cal-temp": p.Cmd.CAL_TEMP,
}
BYTE_CMDS = {
    "brightness": p.Cmd.WORK_BRIGHTNESS,
    "standby-brightness": p.Cmd.STANDBY_BRIGHTNESS,
    "standby-time": p.Cmd.STANDBY_TIME,
    "language": p.Cmd.LANGUAGE,
}
ACTIONS = {
    "clear": p.Cmd.CLEAR_CAPACITY,
    "zero": p.Cmd.DATA_ZERO,
    "factory-reset": p.Cmd.FACTORY_RESET,
}


def wait_for(dev: BW600, attr: str, timeout: float = 3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if getattr(dev, attr) is not None:
            return getattr(dev, attr)
        time.sleep(0.05)
    if dev.error:
        raise DeviceError(dev.error)
    raise DeviceError("No reply from device.")


def print_status(dev: BW600) -> None:
    live = wait_for(dev, "live")
    s = wait_for(dev, "settings")
    print(f"Device        : {dev.info.name} (serial {dev.info.serial}, {dev.info.path})")
    print(f"Mode          : {p.mode_name(s.mode)}")
    print(f"State         : {'RUNNING' if live.running else 'idle'}  (status {live.status_a}/{live.status_b})")
    print(f"Voltage       : {live.voltage:.3f} V")
    direction = "discharging" if live.discharging else ("charging" if live.running else "")
    print(f"Current       : {live.amps:.3f} A {direction}")
    print(f"Power         : {live.power:.3f} W")
    print(f"Resistance    : {live.resistance:.3f} Ω")
    print(f"Capacity      : {live.capacity:.1f} mAh")
    print(f"Energy        : {live.energy:.3f} Wh")
    print(f"Temps         : probe {live.ntc_temp:.1f} °C, MOS {live.mos_temp:.1f} °C, CPU {live.cpu_temp:.1f} °C")
    print(f"Fan level     : {live.fan:g}")
    print("Settings:")
    label, unit = p.SET_VALUE_LABEL.get(s.mode, ("Set value", ""))
    print(f"  {label:<24}: {s.set_value:g} {unit}")
    print(f"  Cut-off voltage         : {s.cutoff_voltage:g} V")
    print(f"  Full voltage            : {s.full_voltage:g} V")
    print(f"  Full (end) current      : {s.full_current:g} A")
    print(f"  Time limit              : {s.time_limit_h} h {s.time_limit_m} min")
    print(f"  Over-current            : {s.over_current:g} A")
    print(f"  Over-power              : {s.over_power:g} W")
    print(f"  Probe over-temperature  : {s.ntc_over_temp:g} °C")
    print(f"  MOS over-temperature    : {s.mos_over_temp:g} °C")
    print(f"  Brightness work/standby : {s.work_brightness} / {s.standby_brightness}")
    print(f"  Standby time            : {s.standby_time}")
    print(f"  Language                : {s.language}")
    print(f"  Calibration V / I / T   : {s.cal_voltage:g} / {s.cal_current:g} / {s.cal_temp:g}")


def cmd_monitor(dev: BW600, args) -> None:
    writer = None
    if args.csv:
        f = open(args.csv, "w", newline="", encoding="utf-8")
        writer = csv.writer(f)
        writer.writerow(["DATE", "VOLTAGE(V)", "CURRENT(A)", "POWER(W)", "RESISTANCE(Ω)", "E_QUANTITY(Wh)",
                         "E_CAPACITY(mAh)", "NTC_TEMP(℃)", "CPU_TEMP(℃)", "MOS_TEMP(℃)", "FAN_SPEED"])
    last = None
    print("time      V         A         W         mAh        Wh       MOS°C  run")
    try:
        while True:
            live = dev.live
            if live is not None and live is not last:
                last = live
                now = dt.datetime.now()
                print(f"{now:%H:%M:%S}  {live.voltage:8.3f}  {live.amps:8.3f}  {live.power:8.3f}  "
                      f"{live.capacity:9.1f}  {live.energy:7.3f}  {live.mos_temp:5.1f}  {'ON' if live.running else 'off'}")
                if writer:
                    writer.writerow([now.strftime("%Y-%m-%d_%H:%M:%S"), live.voltage, live.current, live.power,
                                     live.resistance, live.energy, live.capacity, live.ntc_temp, live.cpu_temp,
                                     live.mos_temp, live.fan])
                    f.flush()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass


def cmd_raw(dev: BW600, args) -> None:
    def show(direction, data):
        if direction == "RX" or args.tx:
            print(f"{time.monotonic():10.3f} {direction} {p.hexdump(data)}", flush=True)
    dev.on_raw.append(show)
    if args.send:
        dev.send(bytes.fromhex(args.send))
    try:
        time.sleep(args.seconds)
    except KeyboardInterrupt:
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="bw600", description="ATORCH BW600 electronic load control")
    ap.add_argument("--device", help="hidraw node (default: auto-detect)")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("gui", help="start the graphical application (default)")
    sub.add_parser("list", help="list connected devices")
    sub.add_parser("status", help="print measurements and settings")
    m = sub.add_parser("monitor", help="stream measurements")
    m.add_argument("--csv", help="also write to CSV file")
    m.add_argument("--interval", type=float, default=0.5)
    sub.add_parser("start", help="switch the load ON")
    sub.add_parser("stop", help="switch the load OFF")
    s = sub.add_parser("set", help="change a setting")
    s.add_argument("name", choices=sorted(list(FLOAT_CMDS) + list(BYTE_CMDS) + ["time"]))
    s.add_argument("value", help="number, or H:MM for time")
    a = sub.add_parser("action", help="clear / zero / factory-reset")
    a.add_argument("name", choices=sorted(ACTIONS))
    a.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    r = sub.add_parser("raw", help="dump raw HID frames (debugging)")
    r.add_argument("--seconds", type=float, default=5)
    r.add_argument("--tx", action="store_true", help="also show transmitted frames")
    r.add_argument("--send", help="hex frame to send once")
    args = ap.parse_args(argv)

    if args.cmd in (None, "gui"):
        from .gui import main as gui_main
        gui_main(args.device)
        return 0
    if args.cmd == "list":
        devs = find_devices()
        for d in devs:
            print(f"{d.path}  {d.name}  serial={d.serial}")
        if not devs:
            print("No BW600 found.")
        return 0 if devs else 1

    try:
        with BW600(args.device) as dev:
            if args.cmd == "status":
                print_status(dev)
            elif args.cmd == "monitor":
                cmd_monitor(dev, args)
            elif args.cmd == "raw":
                cmd_raw(dev, args)
            elif args.cmd in ("start", "stop"):
                wait_for(dev, "live")
                dev.run(args.cmd == "start")
                time.sleep(0.6)
                print("Load is", "ON" if dev.live.running else "OFF")
            elif args.cmd == "set":
                wait_for(dev, "settings")
                if args.name == "time":
                    h, _, mm = args.value.partition(":")
                    dev.set_time_limit(int(h), int(mm or 0))
                elif args.name in FLOAT_CMDS:
                    dev.set_float(FLOAT_CMDS[args.name], float(args.value))
                else:
                    dev.set_byte(BYTE_CMDS[args.name], int(args.value))
                time.sleep(1.0)
                print_status(dev)
            elif args.cmd == "action":
                if not args.yes and input(f"Really '{args.name}'? [y/N] ").lower() != "y":
                    return 1
                wait_for(dev, "live")
                dev.simple(ACTIONS[args.name])
                time.sleep(0.6)
                print("done")
    except DeviceError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0
