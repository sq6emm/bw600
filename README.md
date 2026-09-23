# ATORCH BW600 control for Linux

by SQ6EMM

![BW600 control app: a 5 A discharge stopped at the 9 V cut-off](docs/screenshot.png)

Desktop application and CLI for the ATORCH BW600 electronic load and battery tester (firmware V2.0.5) over its USB-HID cable (`0483:5750`). It needs only Python 3.10+ with Tkinter and matplotlib, and talks to `/dev/hidraw*` directly.

## Setup

```sh
./install-udev-rule.sh        # once: gives your user access to the device (needs sudo)
python3 -m bw600              # start the GUI
pip install --user .          # optional: installs the `bw600` command
```

## GUI

- **Measurements:** voltage, current, power, resistance, capacity (mAh), energy (Wh), probe/MOS/CPU temperatures, fan, and the run state. There's also a big START/STOP button.
- **Chart:** live curves you can pick, with a 1 min to all-data window, pause, and export to CSV or PNG.
- **Recording:** continuous CSV log, with columns compatible with the vendor software.
- **Test setup:**
  - the set value, which means current, voltage, resistance or power depending on the device mode
  - cut-off voltage, charge full voltage and charge end current
  - the time limit
- **Protection:** over-current, over-power, and over-temperature for both the probe (NTC) and the MOSFET.
- **System:**
  - working and standby brightness, and standby time
  - language
  - clearing the capacity counters, zeroing the readings, and factory reset
- **Stop reasons:** when the load switches itself off, a banner and popup say why, for example "Cut-off voltage reached: 8.941 V ≤ 9 V". The BW600 doesn't report a reason, so the app infers it from the last readings and your limits. It recognises:
  - the cut-off voltage
  - the time limit
  - over-current, over-power and over-temperature (probe and MOSFET)
  - a completed charge
  - a lost input
  - a stop made on the device itself
- **Application alarms (Protection tab):** your own over-voltage, under-voltage and over-current limits, checked on every reading (4× a second), even while idle. The under-voltage alarm ignores readings below 0.5 V, which means nothing is connected. When one trips you get a red banner and a popup, and the load can switch off automatically. The limits are saved to `~/.config/bw600/alarms.json`.
- **Calibration:** voltage, current and temperature factors. Don't change these unless you really need to.
- **Raw / Info:** a HID frame log and a hex-send box, for debugging.

The device reports its mode (CC, CV, CR, CP, internal resistance, power-supply test, cable test, the charge/discharge programs), but the protocol has no command to change it. Choose the mode on the BW600 itself.

## CLI

```sh
bw600 status                       # measurements + all settings
bw600 monitor --csv log.csv        # stream readings, report stops with their reason
bw600 monitor --max-voltage 14.6 --min-voltage 10.5 --max-current 5   # with alarms
                                   # (add --warn-only to keep the load running)
bw600 start | stop                 # load on/off
bw600 set value 2.5                # setpoint (A / V / Ω / W per mode)
bw600 set cutoff 3.0               # also: full-voltage full-current ocp opp ntc-otp mos-otp
bw600 set time 1:30                # time limit h:mm
bw600 set brightness 9             # also: standby-brightness standby-time language
bw600 action clear                 # also: zero, factory-reset
bw600 raw --tx --seconds 3         # dump HID traffic
```

## Protocol

I reverse-engineered this from the vendor's Windows software.

**Host to device:**

- Frame: `55 05 <addr=01> <cmd> d0 d1 d2 d3 EE FF`, zero-padded to 64 bytes.
- Floats are big-endian IEEE-754.

**Device to host:** 64 bytes, `AA 05 <addr> <type> … EE FF`.

- **Type `03`, settings:** 11 big-endian floats starting at offset 4, in this order: set value, temperature/voltage/current calibration, cut-off voltage, full voltage, full current, OCP, OPP, NTC OTP, MOS OTP. After them come these bytes: mode, language, working brightness, standby brightness, standby time, time-limit hours, time-limit minutes.
- **Type `05`, live data:** little-endian u32 values ÷ 1000, starting at offset 8, in this order: V, A, W, Ω, Wh, mAh, then the CPU, probe and MOS temperatures and the fan level. Byte `0x34` is the run flag, and byte `0x3C` holds the sign bits.

| cmd | function | payload |
|-----|----------|---------|
| 03 / 05 | read settings / live data | poll |
| 20 | language | d0 = 1..4 |
| 21 | set value | float |
| 22 / 23 / 24 | working brightness / standby brightness / standby time | d3 |
| 25 | run | d0 = 1 on, 0 off |
| 26 / 27 / 28 | temperature / voltage / current calibration | float |
| 29 / 2A / 2B | cut-off voltage / full voltage / full current | float |
| 2C / 2D / 2E / 2F | OCP / OPP / NTC OTP / MOS OTP | float |
| 31 | time limit | d0 = value, d3 = 1 hours / 2 minutes |
| 32 / 33 / 34 | clear capacity / factory reset / zero readings | – |

`55 05 09 02 …` starts the firmware bootloader, so this tool refuses to send cmd `02`. Firmware updating isn't implemented.
