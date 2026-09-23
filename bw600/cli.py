"""Command line interface: ``python -m bw600 <command>``."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import sys
import time

from . import protocol as p
from . import firmware as fw
from .device import (BW600, DeviceError, calibration_backup_path, find_devices,
                     load_calibration_backup, load_settings_snapshot, save_settings_snapshot)

CAL_UNLOCK_WORD = "CALIBRATE"
RESET_UNLOCK_WORD = "RESET"

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
    "cycles": p.Cmd.CYCLE_COUNT,
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
    print(f"Firmware      : V{p.firmware_version(dev.info.name) or '?'}")
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
    print(f"Fan           : {live.fan:g} % (automatic)")
    print("Settings:")
    label, unit = p.SET_VALUE_LABEL.get(s.mode, ("Set value", ""))
    value = "n/a in this mode" if s.mode in p.NO_SET_VALUE_MODES else f"{s.set_value:g} {unit}"
    print(f"  {label:<24}: {value}")
    print(f"  Cut-off voltage         : {s.cutoff_voltage:g} V")
    print(f"  Full voltage            : {s.full_voltage:g} V")
    print(f"  Full (end) current      : {s.full_current:g} A")
    print(f"  Time limit              : {s.time_limit_h} h {s.time_limit_m} min")
    print(f"  Cycle count (cycle test): {s.cycle_count}")
    print(f"  Over-current            : {s.over_current:g} A")
    print(f"  Over-power              : {s.over_power:g} W")
    print(f"  Probe over-temperature  : {s.ntc_over_temp:g} °C")
    print(f"  MOS over-temperature    : {s.mos_over_temp:g} °C")
    print(f"  Brightness work/standby : {s.work_brightness} / {s.standby_brightness}")
    print(f"  Standby time            : {s.standby_time}")
    print(f"  Language                : {s.language}")
    print(f"  Calibration V / I / T   : {s.cal_voltage:g} / {s.cal_current:g} / {s.cal_temp:g}")


def confirm_calibration(args, description: str) -> bool:
    """Calibration needs --unlock-calibration AND the unlock word typed at the prompt."""
    if not getattr(args, "unlock_calibration", False):
        print("error: calibration is locked; add --unlock-calibration to change it", file=sys.stderr)
        return False
    print(description)
    if not sys.stdin.isatty():
        print("error: calibration can only be changed interactively", file=sys.stderr)
        return False
    return input(f"Type {CAL_UNLOCK_WORD} to write it: ").strip() == CAL_UNLOCK_WORD


def cmd_firmware(args) -> int:
    devs = find_devices()
    installed = fw.parse_version(devs[0].name) if devs else None
    print("Installed :", "V" + ".".join(map(str, installed)) if installed else "unknown (no device)")
    try:
        files = fw.fetch_available()
    except OSError as e:
        print(f"error: cannot reach {fw.PAGE_URL}: {e}", file=sys.stderr)
        return 2
    for f in files:
        print(f"Published : V{f.version_str}  {f.name}")
    if files and installed:
        print("Status    :", "up to date" if installed >= files[0].version else f"V{files[0].version_str} available")
    if args.download and files:
        import os
        dest = os.path.join(args.download, f"BW600-firmware-V{files[0].version_str}.zip")
        fw.download(files[0], dest)
        print("Downloaded:", dest)
    print("(Flashing is done with the ATORCH Windows tool; this program does not flash firmware.)")
    return 0


def cmd_monitor(dev: BW600, args) -> None:
    writer = None
    if args.csv:
        f = open(args.csv, "w", newline="", encoding="utf-8")
        writer = csv.writer(f)
        writer.writerow(["DATE", "VOLTAGE(V)", "CURRENT(A)", "POWER(W)", "RESISTANCE(Ω)", "E_QUANTITY(Wh)",
                         "E_CAPACITY(mAh)", "NTC_TEMP(℃)", "CPU_TEMP(℃)", "MOS_TEMP(℃)", "FAN_SPEED"])
    last = None
    cfg = dev.alarms.config
    if args.max_voltage is not None:
        cfg.over_voltage_enabled, cfg.over_voltage = True, args.max_voltage
    if args.min_voltage is not None:
        cfg.under_voltage_enabled, cfg.under_voltage = True, args.min_voltage
    if args.max_current is not None:
        cfg.over_current_enabled, cfg.over_current = True, args.max_current
    if args.warn_only:
        cfg.stop_load = False
    dev.alarms.reset()
    dev.on_alarm.append(lambda ev: print(f"{dt.datetime.now():%H:%M:%S}  !!! ALARM: {ev.message}", flush=True))
    dev.on_stop.append(lambda ev: print(f"{dt.datetime.now():%H:%M:%S}  *** load stopped: {ev.message}"
                                        + (" (inferred)" if ev.inferred else ""), flush=True))
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
    ap = argparse.ArgumentParser(prog="bw600", description="ATORCH BW600 electronic load control — by SQ6EMM")
    ap.add_argument("--device", help="hidraw node (default: auto-detect)")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("gui", help="start the graphical application (default)")
    sub.add_parser("list", help="list connected devices")
    sub.add_parser("status", help="print measurements and settings")
    m = sub.add_parser("monitor", help="stream measurements")
    m.add_argument("--csv", help="also write to CSV file")
    m.add_argument("--interval", type=float, default=0.5)
    m.add_argument("--max-voltage", type=float, help="over-voltage alarm limit (V)")
    m.add_argument("--min-voltage", type=float, help="under-voltage alarm limit (V)")
    m.add_argument("--max-current", type=float, help="over-current alarm limit (A)")
    m.add_argument("--warn-only", action="store_true", help="alarms only warn, do not switch the load off")
    sub.add_parser("start", help="switch the load ON")
    sub.add_parser("stop", help="switch the load OFF")
    md = sub.add_parser("mode", help="select the operating mode (load must be off)")
    md.add_argument("name", choices=list(p.MODE_KEYS),
                    help="cc cv cr cp | ir (internal resistance) psu (power supply test) cable | "
                         "cdc (charge-discharge-charge) cdcdc cycle")
    s = sub.add_parser("set", help="change a setting")
    s.add_argument("name", choices=sorted(list(FLOAT_CMDS) + list(BYTE_CMDS) + ["time"]))
    s.add_argument("value", help="number, or H:MM for time")
    s.add_argument("--unlock-calibration", action="store_true", help="required for cal-* settings")
    c = sub.add_parser("calibration", help="show the calibration factors and backup, or restore the backup")
    c.add_argument("action", choices=["show", "restore"])
    c.add_argument("--unlock-calibration", action="store_true", help="required for restore")
    a = sub.add_parser("action", help="clear / zero / factory-reset")
    a.add_argument("name", choices=sorted(ACTIONS))
    a.add_argument("--yes", action="store_true", help="do not ask for confirmation (not for factory-reset)")
    a.add_argument("--unlock-factory-reset", action="store_true", help="required for factory-reset")
    st = sub.add_parser("settings", help="save all settings to a JSON file, or write them back")
    st.add_argument("action", choices=["save", "restore"])
    st.add_argument("file", nargs="?", help="JSON file (default for save: ~/.config/bw600/settings-<serial>-<time>.json)")
    fwp = sub.add_parser("firmware", help="show the installed firmware version and check ATORCH's site for updates")
    fwp.add_argument("--download", metavar="DIR", help="download the newest firmware file into DIR")
    r = sub.add_parser("raw", help="dump raw HID frames (debugging)")
    r.add_argument("--seconds", type=float, default=5)
    r.add_argument("--tx", action="store_true", help="also show transmitted frames")
    r.add_argument("--send", help="hex frame to send once")
    args = ap.parse_args(argv)

    if args.cmd in (None, "gui"):
        from .gui import main as gui_main
        gui_main(args.device)
        return 0
    if args.cmd == "firmware":
        return cmd_firmware(args)
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
            elif args.cmd == "mode":
                wait_for(dev, "live")
                dev.set_mode(p.MODE_KEYS[args.name])
                time.sleep(1.0)
                print("Mode:", p.mode_name(dev.settings.mode))
            elif args.cmd == "set":
                wait_for(dev, "settings")
                if args.name == "time":
                    h, _, mm = args.value.partition(":")
                    dev.set_time_limit(int(h), int(mm or 0))
                elif FLOAT_CMDS.get(args.name) in p.CAL_COMMANDS:
                    cmd = FLOAT_CMDS[args.name]
                    value = p.check_calibration(float(args.value))
                    old = getattr(dev.settings, p.CAL_FIELDS[cmd])
                    if abs(value - old) < 5e-7:
                        print(f"{args.name} is already {old:.7g}; nothing written")
                        return 0
                    if not confirm_calibration(args, f"{args.name}: {old:.7g} -> {value:.7g}"):
                        print("calibration unchanged")
                        return 1
                    dev.unlock_calibration(30)
                    dev.write_calibration({cmd: value})
                elif args.name in FLOAT_CMDS:
                    dev.set_float(FLOAT_CMDS[args.name], float(args.value))
                else:
                    dev.set_byte(BYTE_CMDS[args.name], int(args.value))
                time.sleep(1.0)
                print_status(dev)
            elif args.cmd == "calibration":
                s = wait_for(dev, "settings")
                backup = load_calibration_backup(dev.info.serial)
                print(f"Device : voltage {s.cal_voltage:.6g}  current {s.cal_current:.6g}  temperature {s.cal_temp:.6g}")
                if backup:
                    print(f"Backup : voltage {backup['cal_voltage']:.6g}  current {backup['cal_current']:.6g}  "
                          f"temperature {backup['cal_temp']:.6g}\n         {calibration_backup_path(dev.info.serial)}")
                else:
                    print("Backup : none")
                if args.action == "restore":
                    if not backup:
                        print("error: no backup for this device", file=sys.stderr)
                        return 2
                    if not confirm_calibration(args, "Restore the backup values above?"):
                        print("calibration unchanged")
                        return 1
                    dev.unlock_calibration(30)
                    dev.write_calibration({cmd: backup[field] for cmd, field in p.CAL_FIELDS.items()})
                    time.sleep(1.0)
                    print("restored")
            elif args.cmd == "settings":
                s = wait_for(dev, "settings")
                if args.action == "save":
                    print("saved to", save_settings_snapshot(dev.info.serial, s, args.file))
                else:
                    if not args.file:
                        print("error: give the snapshot file to restore", file=sys.stderr)
                        return 2
                    snap = load_settings_snapshot(args.file)
                    wait_for(dev, "live")
                    written = dev.apply_settings(snap)
                    time.sleep(1.5)
                    print(f"restored {len(written)} settings (calibration unchanged)")
            elif args.cmd == "action" and args.name == "factory-reset":
                if not args.unlock_factory_reset:
                    print("error: factory reset is locked; add --unlock-factory-reset", file=sys.stderr)
                    return 1
                if not sys.stdin.isatty():
                    print("error: factory reset can only be done interactively", file=sys.stderr)
                    return 1
                wait_for(dev, "settings")
                print("This resets ALL settings and the calibration to factory values,")
                print("and on WiFi models also clears the WiFi pairing (re-add the device in the Tuya app).")
                if input(f"Type {RESET_UNLOCK_WORD} to continue: ").strip() != RESET_UNLOCK_WORD:
                    print("nothing was reset")
                    return 1
                wait_for(dev, "live")
                dev.unlock_factory_reset(30)
                path = dev.factory_reset()
                time.sleep(1.0)
                print(f"factory reset done; previous settings saved to {path}")
            elif args.cmd == "action":
                if not args.yes and input(f"Really '{args.name}'? [y/N] ").lower() != "y":
                    return 1
                wait_for(dev, "live")
                dev.simple(ACTIONS[args.name])
                time.sleep(0.6)
                print("done")
    except (DeviceError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0
