"""Tkinter desktop application for the ATORCH BW600 electronic load."""

from __future__ import annotations

import collections
import csv
import datetime as dt
import queue
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import matplotlib

matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402

from . import protocol as p  # noqa: E402
from .alarms import AlarmConfig, AlarmEvent  # noqa: E402
from .device import BW600, DeviceError, find_devices  # noqa: E402

REFRESH_MS = 200
HISTORY = 36000  # samples kept for the chart (~2.5 h at 4 Hz)

READOUTS = (
    ("voltage", "Voltage", "V", 3),
    ("current", "Current", "A", 3),
    ("power", "Power", "W", 3),
    ("resistance", "Resistance", "Ω", 3),
    ("capacity", "Capacity", "mAh", 1),
    ("energy", "Energy", "Wh", 3),
    ("ntc_temp", "Probe temp", "°C", 1),
    ("mos_temp", "MOS temp", "°C", 1),
    ("cpu_temp", "CPU temp", "°C", 1),
    ("fan", "Fan", "%", 0),
)

SERIES = (
    ("voltage", "Voltage (V)", "#1f77b4", "left"),
    ("current", "Current (A)", "#d62728", "right"),
    ("power", "Power (W)", "#9467bd", "right"),
    ("capacity", "Capacity (mAh)", "#2ca02c", None),
    ("energy", "Energy (Wh)", "#8c564b", None),
    ("ntc_temp", "Probe temp (°C)", "#e377c2", None),
    ("mos_temp", "MOS temp (°C)", "#ff7f0e", None),
    ("cpu_temp", "CPU temp (°C)", "#bcbd22", None),
)

CSV_FIELDS = ("voltage", "current", "power", "resistance", "energy", "capacity",
              "ntc_temp", "cpu_temp", "mos_temp", "fan")
CSV_HEADER = ("DATE", "VOLTAGE(V)", "CURRENT(A)", "POWER(W)", "RESISTANCE(Ω)",
              "E_QUANTITY(Wh)", "E_CAPACITY(mAh)", "NTC_TEMP(℃)", "CPU_TEMP(℃)",
              "MOS_TEMP(℃)", "FAN_SPEED")

# (label, command, unit, settings attribute)
FLOAT_SETTINGS_TEST = (
    ("Cut-off (end) voltage", p.Cmd.CUTOFF_VOLTAGE, "V", "cutoff_voltage"),
    ("Charge full voltage", p.Cmd.FULL_VOLTAGE, "V", "full_voltage"),
    ("Charge end current", p.Cmd.FULL_CURRENT, "A", "full_current"),
)
FLOAT_SETTINGS_PROTECT = (
    ("Over-current protection", p.Cmd.OVER_CURRENT, "A", "over_current"),
    ("Over-power protection", p.Cmd.OVER_POWER, "W", "over_power"),
    ("Probe (NTC) over-temperature", p.Cmd.NTC_OVER_TEMP, "°C", "ntc_over_temp"),
    ("MOSFET over-temperature", p.Cmd.MOS_OVER_TEMP, "°C", "mos_over_temp"),
)
FLOAT_SETTINGS_CAL = (
    ("Voltage calibration", p.Cmd.CAL_VOLTAGE, "", "cal_voltage"),
    ("Current calibration", p.Cmd.CAL_CURRENT, "", "cal_current"),
    ("Temperature calibration", p.Cmd.CAL_TEMP, "", "cal_temp"),
)
CYCLE_SETTING = ("Cycle count (cycle test)", p.Cmd.CYCLE_COUNT, "cycle_count")
BYTE_SETTINGS = (
    ("Working screen brightness", p.Cmd.WORK_BRIGHTNESS, "work_brightness"),
    ("Standby screen brightness", p.Cmd.STANDBY_BRIGHTNESS, "standby_brightness"),
    ("Standby time", p.Cmd.STANDBY_TIME, "standby_time"),
)


def fmt(v, digits=3):
    return "—" if v is None else f"{v:.{digits}f}"


class App(tk.Tk):
    def __init__(self, path: str | None = None):
        super().__init__()
        self.title("ATORCH BW600 Control — by SQ6EMM")
        self.geometry("1280x870")
        self.minsize(1000, 680)
        self.dev: BW600 | None = None
        self.path = path
        self.events: queue.Queue = queue.Queue()
        self.t0 = time.monotonic()
        self.hist_t: collections.deque = collections.deque(maxlen=HISTORY)
        self.hist: dict[str, collections.deque] = {
            k: collections.deque(maxlen=HISTORY) for k, *_ in SERIES}
        self.csv_file = None
        self.csv_writer = None
        self.raw_enabled = tk.BooleanVar(value=False)
        self.settings_loaded = False
        self.run_started: float | None = None
        self.run_elapsed = 0.0
        self.entries: dict[str, tk.StringVar] = {}
        self.scales: dict[str, tk.IntVar] = {}
        self.current_labels: dict[str, tk.StringVar] = {}

        self._style()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(100, self.connect)
        self.after(REFRESH_MS, self.refresh)

    # ------------------------------------------------------------------ UI
    def _style(self):
        s = ttk.Style(self)
        if "clam" in s.theme_names():
            s.theme_use("clam")
        s.configure("Big.TLabel", font=("DejaVu Sans Mono", 22, "bold"))
        s.configure("Unit.TLabel", font=("DejaVu Sans", 12))
        s.configure("Cap.TLabel", font=("DejaVu Sans", 9), foreground="#666")
        s.configure("Status.TLabel", font=("DejaVu Sans", 10, "bold"))
        s.configure("Run.TButton", font=("DejaVu Sans", 14, "bold"), padding=10)
        s.configure("Danger.TButton", foreground="#b00020")

    def _build(self):
        top = ttk.Frame(self, padding=(10, 6))
        top.pack(fill="x")
        self.status_var = tk.StringVar(value="Disconnected")
        ttk.Label(top, textvariable=self.status_var, style="Status.TLabel").pack(side="left")
        ttk.Button(top, text="Reconnect", command=self.reconnect).pack(side="right")
        self.mode_var = tk.StringVar(value="Mode: ?")
        ttk.Label(top, textvariable=self.mode_var, style="Status.TLabel").pack(side="right", padx=20)

        self.stop_var = tk.StringVar(value="")
        self.stop_banner = tk.Label(self, textvariable=self.stop_var, anchor="w", padx=10, pady=4,
                                    bg="#fff3cd", fg="#664d03", font=("DejaVu Sans", 10, "bold"))
        self.stop_banner.bind("<Button-1>", lambda _e: self.stop_banner.pack_forget())

        body = ttk.Frame(self, padding=(10, 0, 10, 10))
        self.body = body
        body.pack(fill="both", expand=True)

        left = ttk.Frame(body)
        left.pack(side="left", fill="y", padx=(0, 10))
        self._build_readouts(left)

        nb = ttk.Notebook(body)
        nb.pack(side="left", fill="both", expand=True)
        self._build_chart(nb)
        self._build_test(nb)
        self._build_protection(nb)
        self._build_system(nb)
        self._build_calibration(nb)
        self._build_console(nb)

    def _build_readouts(self, parent):
        box = ttk.LabelFrame(parent, text="Measurements", padding=8)
        box.pack(fill="x")
        self.readout_vars = {}
        for i, (key, label, unit, _d) in enumerate(READOUTS):
            ttk.Label(box, text=label, style="Cap.TLabel").grid(row=2 * i, column=0, sticky="w")
            var = tk.StringVar(value="—")
            size = "Big.TLabel" if i < 3 else "TLabel"
            lbl = ttk.Label(box, textvariable=var, style=size, width=11, anchor="e")
            if i >= 3:
                lbl.configure(font=("DejaVu Sans Mono", 13, "bold"))
            lbl.grid(row=2 * i + 1, column=0, sticky="e")
            ttk.Label(box, text=unit, style="Unit.TLabel").grid(row=2 * i + 1, column=1, sticky="w")
            self.readout_vars[key] = var

        self.elapsed_var = tk.StringVar(value="Run time (this session): 0:00:00")
        ttk.Label(box, textvariable=self.elapsed_var).grid(row=40, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.devstate_var = tk.StringVar(value="")
        ttk.Label(box, textvariable=self.devstate_var, style="Cap.TLabel").grid(row=41, column=0, columnspan=2, sticky="w")

        self.run_btn = ttk.Button(parent, text="START", style="Run.TButton", command=self.toggle_run)
        self.run_btn.pack(fill="x", pady=6)

        rec = ttk.LabelFrame(parent, text="Recording", padding=8)
        rec.pack(fill="x")
        self.rec_btn = ttk.Button(rec, text="Record CSV…", command=self.toggle_record)
        self.rec_btn.pack(fill="x")
        self.rec_var = tk.StringVar(value="Not recording")
        ttk.Label(rec, textvariable=self.rec_var, style="Cap.TLabel", wraplength=220).pack(fill="x")

    def _build_chart(self, nb):
        frame = ttk.Frame(nb, padding=6)
        nb.add(frame, text="Chart")
        ctl = ttk.Frame(frame)
        ctl.pack(fill="x")
        self.series_on = {}
        for key, label, _c, axis in SERIES:
            var = tk.BooleanVar(value=axis is not None)
            ttk.Checkbutton(ctl, text=label, variable=var).pack(side="left", padx=3)
            self.series_on[key] = var
        ctl2 = ttk.Frame(frame)
        ctl2.pack(fill="x", pady=4)
        ttk.Label(ctl2, text="Window:").pack(side="left")
        self.window_var = tk.StringVar(value="5 min")
        ttk.Combobox(ctl2, textvariable=self.window_var, state="readonly", width=8,
                     values=("1 min", "5 min", "15 min", "1 h", "All")).pack(side="left", padx=4)
        self.pause_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(ctl2, text="Pause", variable=self.pause_var).pack(side="left", padx=10)
        ttk.Button(ctl2, text="Clear chart", command=self.clear_history).pack(side="right", padx=2)
        ttk.Button(ctl2, text="Export PNG…", command=self.export_png).pack(side="right", padx=2)
        ttk.Button(ctl2, text="Export data CSV…", command=self.export_history).pack(side="right", padx=2)

        self.fig = Figure(figsize=(8, 5), dpi=100)
        self.ax = self.fig.add_subplot(111)
        self.ax2 = self.ax.twinx()
        self.canvas = FigureCanvasTkAgg(self.fig, master=frame)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)

    def _setting_row(self, parent, row, label, unit, key, apply):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=3)
        var = tk.StringVar()
        ttk.Entry(parent, textvariable=var, width=12).grid(row=row, column=1, padx=6)
        ttk.Label(parent, text=unit, width=4).grid(row=row, column=2, sticky="w")
        ttk.Button(parent, text="Set", command=apply).grid(row=row, column=3, padx=4)
        cur = tk.StringVar(value="device: —")
        ttk.Label(parent, textvariable=cur, style="Cap.TLabel").grid(row=row, column=4, sticky="w", padx=8)
        self.entries[key] = var
        self.current_labels[key] = cur
        return var

    def _build_test(self, nb):
        frame = ttk.Frame(nb, padding=12)
        nb.add(frame, text="Test setup")

        md = ttk.LabelFrame(frame, text="Mode", padding=10)
        md.pack(fill="x", pady=(0, 10))
        self.mode_choice = tk.StringVar()
        self.mode_combo = ttk.Combobox(md, textvariable=self.mode_choice, state="readonly", width=32,
                                       values=[p.MODE_NAMES[m] for m in p.Mode])
        self.mode_combo.grid(row=0, column=0)
        ttk.Button(md, text="Set mode", command=self.apply_mode).grid(row=0, column=1, padx=6)
        self.current_labels["mode"] = tk.StringVar(value="device: —")
        ttk.Label(md, textvariable=self.current_labels["mode"], style="Cap.TLabel").grid(row=0, column=2, padx=8)
        ttk.Label(md, style="Cap.TLabel", text="The load must be off to change the mode.").grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))

        g = ttk.LabelFrame(frame, text="Load setpoint", padding=10)
        g.pack(fill="x")
        self.setval_label = tk.StringVar(value="Set value")
        self.setval_unit = tk.StringVar(value="")
        ttk.Label(g, textvariable=self.setval_label).grid(row=0, column=0, sticky="w")
        self.entries["set_value"] = tk.StringVar()
        ttk.Entry(g, textvariable=self.entries["set_value"], width=12).grid(row=0, column=1, padx=6)
        ttk.Label(g, textvariable=self.setval_unit, width=4).grid(row=0, column=2, sticky="w")
        ttk.Button(g, text="Set", command=lambda: self.apply_float(p.Cmd.SET_VALUE, "set_value")).grid(row=0, column=3)
        self.current_labels["set_value"] = tk.StringVar(value="device: —")
        ttk.Label(g, textvariable=self.current_labels["set_value"], style="Cap.TLabel").grid(row=0, column=4, padx=8)
        ttk.Label(g, style="Cap.TLabel", wraplength=620, justify="left",
                  text="The meaning follows the selected mode: current (CC), voltage (CV), "
                       "resistance (CR) or power (CP).").grid(
            row=1, column=0, columnspan=5, sticky="w", pady=(6, 0))

        t = ttk.LabelFrame(frame, text="Battery test limits", padding=10)
        t.pack(fill="x", pady=10)
        for i, (label, cmd, unit, key) in enumerate(FLOAT_SETTINGS_TEST):
            self._setting_row(t, i, label, unit, key, lambda c=cmd, k=key: self.apply_float(c, k))

        cy = ttk.LabelFrame(frame, text="Charge/discharge cycle test", padding=10)
        cy.pack(fill="x", pady=(0, 10))
        label, cmd, key = CYCLE_SETTING
        ttk.Label(cy, text=label).grid(row=0, column=0, sticky="w")
        self.scales[key] = tk.IntVar(value=0)
        lo, hi = p.BYTE_RANGES[cmd]
        ttk.Spinbox(cy, from_=lo, to=hi, textvariable=self.scales[key], width=6).grid(row=0, column=1, padx=6)
        ttk.Button(cy, text="Set", command=lambda: self.apply_byte(cmd, self.scales[key])).grid(row=0, column=2)
        self.current_labels[key] = tk.StringVar(value="device: —")
        ttk.Label(cy, textvariable=self.current_labels[key], style="Cap.TLabel").grid(row=0, column=3, padx=8, sticky="w")

        tl = ttk.LabelFrame(frame, text="Time limit (0:00 = unlimited)", padding=10)
        tl.pack(fill="x")
        self.entries["time_h"] = tk.StringVar()
        self.entries["time_m"] = tk.StringVar()
        ttk.Spinbox(tl, from_=0, to=255, textvariable=self.entries["time_h"], width=5).grid(row=0, column=0)
        ttk.Label(tl, text="h").grid(row=0, column=1, padx=(2, 8))
        ttk.Spinbox(tl, from_=0, to=59, textvariable=self.entries["time_m"], width=5).grid(row=0, column=2)
        ttk.Label(tl, text="min").grid(row=0, column=3, padx=(2, 8))
        ttk.Button(tl, text="Set", command=self.apply_time).grid(row=0, column=4)
        self.current_labels["time"] = tk.StringVar(value="device: —")
        ttk.Label(tl, textvariable=self.current_labels["time"], style="Cap.TLabel").grid(row=0, column=5, padx=8)

        ttk.Label(frame, style="Cap.TLabel", wraplength=700, justify="left",
                  text="Warning: set the battery's full-charge and discharge-end voltages correctly before "
                       "running charge/discharge tests, otherwise the battery can be damaged.").pack(
            fill="x", pady=10)
        ttk.Button(frame, text="Re-read settings from device", command=self.reload_settings).pack(anchor="w")

    def _build_protection(self, nb):
        frame = ttk.Frame(nb, padding=12)
        nb.add(frame, text="Protection")
        g = ttk.LabelFrame(frame, text="Protection thresholds", padding=10)
        g.pack(fill="x")
        for i, (label, cmd, unit, key) in enumerate(FLOAT_SETTINGS_PROTECT):
            self._setting_row(g, i, label, unit, key, lambda c=cmd, k=key: self.apply_float(c, k))
        ttk.Label(frame, style="Cap.TLabel", wraplength=700, justify="left",
                  text="These limits are enforced by the BW600 itself to protect the load.").pack(fill="x", pady=(4, 12))

        cfg = AlarmConfig.load()
        a = ttk.LabelFrame(frame, text="Application alarms (checked by this program)", padding=10)
        a.pack(fill="x")
        self.alarm_vars = {
            "over_voltage_enabled": tk.BooleanVar(value=cfg.over_voltage_enabled),
            "over_voltage": tk.StringVar(value=f"{cfg.over_voltage:g}"),
            "under_voltage_enabled": tk.BooleanVar(value=cfg.under_voltage_enabled),
            "under_voltage": tk.StringVar(value=f"{cfg.under_voltage:g}"),
            "over_current_enabled": tk.BooleanVar(value=cfg.over_current_enabled),
            "over_current": tk.StringVar(value=f"{cfg.over_current:g}"),
            "stop_load": tk.BooleanVar(value=cfg.stop_load),
        }
        for row, (key, label, unit) in enumerate((("over_voltage", "Over-voltage alarm above", "V"),
                                                   ("under_voltage", "Under-voltage alarm below", "V"),
                                                   ("over_current", "Over-current alarm above", "A"))):
            ttk.Checkbutton(a, text=label, variable=self.alarm_vars[key + "_enabled"]).grid(row=row, column=0, sticky="w", pady=3)
            ttk.Entry(a, textvariable=self.alarm_vars[key], width=10).grid(row=row, column=1, padx=6)
            ttk.Label(a, text=unit).grid(row=row, column=2, sticky="w")
        ttk.Checkbutton(a, text="Switch the load OFF when an alarm trips",
                        variable=self.alarm_vars["stop_load"]).grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Button(a, text="Apply & save", command=self.apply_alarms).grid(row=4, column=0, sticky="w", pady=(8, 0))
        self.alarm_state_var = tk.StringVar(value="")
        ttk.Label(a, textvariable=self.alarm_state_var, style="Cap.TLabel").grid(row=4, column=1, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Label(frame, style="Cap.TLabel", wraplength=700, justify="left",
                  text="Checked on every reading (4× per second), also while the load is idle — e.g. it warns when "
                       "a too-high voltage is connected. An alarm triggers once when the limit is exceeded for two "
                       "readings in a row and re-arms once the value is back 2 % inside the limit. The under-voltage alarm "
                       "ignores readings below 0.5 V (nothing connected). Settings are saved "
                       "to ~/.config/bw600/alarms.json and are also used by 'bw600 monitor'.").pack(fill="x", pady=6)

    def _build_system(self, nb):
        frame = ttk.Frame(nb, padding=12)
        nb.add(frame, text="System")
        disp = ttk.LabelFrame(frame, text="Display", padding=10)
        disp.pack(fill="x")
        for i, (label, cmd, key) in enumerate(BYTE_SETTINGS):
            ttk.Label(disp, text=label).grid(row=i, column=0, sticky="w", pady=3)
            var = tk.IntVar(value=0)
            lo, hi = p.BYTE_RANGES[cmd]
            sb = ttk.Spinbox(disp, from_=lo, to=hi, textvariable=var, width=6)
            sb.grid(row=i, column=1, padx=6)
            ttk.Button(disp, text="Set", command=lambda c=cmd, v=var: self.apply_byte(c, v)).grid(row=i, column=2)
            ttk.Label(disp, text=f"({lo}–{hi})", style="Cap.TLabel").grid(row=i, column=4, sticky="w")
            cur = tk.StringVar(value="device: —")
            ttk.Label(disp, textvariable=cur, style="Cap.TLabel").grid(row=i, column=3, padx=8, sticky="w")
            self.scales[key] = var
            self.current_labels[key] = cur

        lang = ttk.LabelFrame(frame, text="Language", padding=10)
        lang.pack(fill="x", pady=10)
        for text, opt in (("中文", 1), ("English", 3)):
            ttk.Button(lang, text=text, command=lambda o=opt: self.apply_lang(o)).pack(side="left", padx=4)
        ttk.Label(lang, text="Alt:", style="Cap.TLabel").pack(side="left", padx=(12, 2))
        for text, opt in (("中文 (2)", 2), ("English (4)", 4)):
            ttk.Button(lang, text=text, command=lambda o=opt: self.apply_lang(o)).pack(side="left", padx=4)
        self.current_labels["language"] = tk.StringVar(value="device: —")
        ttk.Label(lang, textvariable=self.current_labels["language"], style="Cap.TLabel").pack(side="left", padx=8)

        act = ttk.LabelFrame(frame, text="Data", padding=10)
        act.pack(fill="x")
        ttk.Button(act, text="Clear capacity / energy counters",
                   command=lambda: self.confirm_simple(p.Cmd.CLEAR_CAPACITY, "Clear the accumulated capacity (mAh) and energy (Wh)?")).pack(side="left", padx=4)
        ttk.Button(act, text="Zero readings (data zero)",
                   command=lambda: self.confirm_simple(p.Cmd.DATA_ZERO, "Zero the measurement offsets? Do this with nothing connected to the input.")).pack(side="left", padx=4)
        ttk.Button(act, text="Factory reset", style="Danger.TButton",
                   command=lambda: self.confirm_simple(p.Cmd.FACTORY_RESET, "Restore ALL device settings to factory defaults?")).pack(side="left", padx=4)

    def _build_calibration(self, nb):
        frame = ttk.Frame(nb, padding=12)
        nb.add(frame, text="Calibration")
        ttk.Label(frame, foreground="#b00020", wraplength=700, justify="left",
                  text="Reference calibration factors. The manufacturer advises NOT to change these under "
                       "normal circumstances — wrong values make every measurement wrong. Note the current "
                       "values before changing anything.").pack(fill="x", pady=(0, 10))
        g = ttk.LabelFrame(frame, text="Calibration factors", padding=10)
        g.pack(fill="x")
        for i, (label, cmd, unit, key) in enumerate(FLOAT_SETTINGS_CAL):
            self._setting_row(g, i, label, unit, key,
                              lambda c=cmd, k=key: self.apply_float(c, k, confirm=True))

    def _build_console(self, nb):
        frame = ttk.Frame(nb, padding=6)
        nb.add(frame, text="Raw / Info")
        ctl = ttk.Frame(frame)
        ctl.pack(fill="x")
        ttk.Checkbutton(ctl, text="Show raw frames", variable=self.raw_enabled).pack(side="left")
        ttk.Label(ctl, text="Send hex:").pack(side="left", padx=(20, 4))
        self.raw_entry = tk.StringVar(value="55 05 01 03 00 00 00 EE FF")
        ttk.Entry(ctl, textvariable=self.raw_entry, width=40, font=("DejaVu Sans Mono", 10)).pack(side="left")
        ttk.Button(ctl, text="Send", command=self.send_raw).pack(side="left", padx=4)
        ttk.Button(ctl, text="Clear", command=lambda: self.console.delete("1.0", "end")).pack(side="left")
        self.console = tk.Text(frame, font=("DejaVu Sans Mono", 9), height=20, wrap="none")
        self.console.pack(fill="both", expand=True, pady=4)
        self.info_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.info_var, style="Cap.TLabel", justify="left").pack(fill="x")

    # ------------------------------------------------------------ device
    def connect(self):
        try:
            self.dev = BW600(self.path)
        except DeviceError as e:
            self.dev = None
            self.status_var.set(f"⚠ {e}")
            self.after(3000, self.connect)
            return
        self.dev.on_live.append(lambda live: self.events.put(("live", live)))
        self.dev.on_settings.append(lambda s: self.events.put(("settings", s)))
        self.dev.on_raw.append(self._raw_cb)
        self.dev.on_stop.append(lambda ev: self.events.put(("stop", ev)))
        self.dev.on_alarm.append(lambda ev: self.events.put(("alarm", ev)))
        self.apply_alarms(save=False)
        self.dev.start()
        self.dev.request_settings()
        self.settings_loaded = False
        info = self.dev.info
        self.info_var.set(f"Device: {info.name}   serial: {info.serial}   node: {info.path}")

    def reconnect(self):
        if self.dev:
            self.dev.close()
            self.dev = None
        self.connect()

    def _raw_cb(self, direction, data):
        if self.raw_enabled.get():
            self.events.put(("raw", (direction, data)))

    def _need_dev(self) -> bool:
        if not self.dev or not self.dev.online:
            messagebox.showwarning("BW600", "Device is not connected.")
            return False
        return True

    def apply_float(self, cmd, key, confirm=False):
        if not self._need_dev():
            return
        try:
            value = float(self.entries[key].get().replace(",", "."))
        except ValueError:
            messagebox.showerror("BW600", "Please enter a number.")
            return
        if confirm and not messagebox.askyesno("BW600", f"Write calibration value {value}?"):
            return
        self.dev.set_float(cmd, value)

    def apply_byte(self, cmd, var):
        if not self._need_dev():
            return
        try:
            self.dev.set_byte(cmd, int(var.get()))
        except (ValueError, tk.TclError) as e:
            messagebox.showerror("BW600", str(e) if isinstance(e, ValueError) and "must be" in str(e)
                                 else "Please enter a whole number.")

    def apply_time(self):
        if not self._need_dev():
            return
        try:
            h = int(self.entries["time_h"].get() or 0)
            m = int(self.entries["time_m"].get() or 0)
            self.dev.set_time_limit(h, m)
        except ValueError as e:
            messagebox.showerror("BW600", str(e))

    def apply_lang(self, opt):
        if self._need_dev():
            self.dev.set_language(opt)

    def apply_mode(self):
        if not self._need_dev():
            return
        names = {v: k for k, v in p.MODE_NAMES.items()}
        mode = names.get(self.mode_choice.get())
        if mode is None:
            return
        try:
            self.dev.set_mode(mode)
        except DeviceError as e:
            messagebox.showwarning("BW600", str(e))

    def confirm_simple(self, cmd, question):
        if self._need_dev() and messagebox.askyesno("BW600", question):
            self.dev.simple(cmd)

    def reload_settings(self):
        if self._need_dev():
            self.settings_loaded = False
            self.dev.request_settings()

    def toggle_run(self):
        if not self._need_dev():
            return
        self.stop_banner.pack_forget()
        running = bool(self.dev.live and self.dev.live.running)
        self.dev.run(not running)

    def send_raw(self):
        if not self._need_dev():
            return
        try:
            data = bytes.fromhex(self.raw_entry.get())
            self.dev.send(data)
        except ValueError as e:
            messagebox.showerror("BW600", str(e))

    # ------------------------------------------------------------ updates
    def refresh(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "live":
                    self.on_live(payload)
                elif kind == "settings":
                    self.on_settings(payload)
                elif kind == "stop":
                    self.on_stop(payload)
                elif kind == "alarm":
                    self.on_alarm(payload)
                elif kind == "raw":
                    d, data = payload
                    self.console.insert("end", f"{dt.datetime.now():%H:%M:%S.%f}"[:-3] + f" {d} {p.hexdump(data)}\n")
                    self.console.see("end")
        except queue.Empty:
            pass
        if self.dev:
            if self.dev.error:
                self.status_var.set(f"⚠ {self.dev.error}")
            elif self.dev.online:
                self.status_var.set(f"● Online — {self.dev.info.name} ({self.dev.info.path})")
            else:
                self.status_var.set("○ Waiting for device…")
        self.after(REFRESH_MS, self.refresh)

    def on_live(self, live: p.Live):
        for key, _l, _u, digits in READOUTS:
            value = live.amps if key == "current" else getattr(live, key)
            self.readout_vars[key].set(fmt(value, digits))
        self.run_btn.configure(text="STOP" if live.running else "START")
        if live.running and self.run_started is None:
            self.run_started = time.monotonic()
        elif not live.running and self.run_started is not None:
            self.run_elapsed += time.monotonic() - self.run_started
            self.run_started = None
        total = self.run_elapsed + (time.monotonic() - self.run_started if self.run_started else 0)
        h, rem = divmod(int(total), 3600)
        self.elapsed_var.set(f"Run time (this session): {h}:{rem // 60:02d}:{rem % 60:02d}")
        if live.running and (live.amps or 0) >= 0.01:
            state = "RUNNING — discharging" if live.discharging else "RUNNING — charging"
        elif live.running:
            state = "RUNNING"
        else:
            state = "idle"
        self.devstate_var.set(f"{state}   status {live.status_a}/{live.status_b}")
        now = time.monotonic() - self.t0
        self.hist_t.append(now)
        for key, *_ in SERIES:
            self.hist[key].append(live.amps if key == "current" else getattr(live, key))
        if self.csv_writer:
            self.csv_writer.writerow([dt.datetime.now().strftime("%Y-%m-%d_%H:%M:%S")] +
                                     ["" if getattr(live, k) is None else getattr(live, k) for k in CSV_FIELDS])
            self.csv_file.flush()
        if not self.pause_var.get() and len(self.hist_t) % 2 == 0:
            self.redraw()

    def on_stop(self, ev: p.StopEvent):
        stamp = dt.datetime.now().strftime("%H:%M:%S")
        text = f"{stamp}  Load stopped — {ev.message}"
        if ev.inferred:
            text += "  (reason inferred from readings)"
        self.console.insert("end", text + "\n")
        self.console.see("end")
        if self.csv_file:
            self.rec_var.set(f"{self.rec_var.get().splitlines()[0]}\nlast stop: {stamp} {ev.reason}")
        if ev.reason == "user":
            self.stop_banner.pack_forget()
            return
        if ev.reason == "alarm":  # the alarm itself was already announced
            return
        self.stop_banner.configure(bg="#fff3cd", fg="#664d03")
        self.stop_var.set(text + "   (click to dismiss)")
        self.stop_banner.pack(fill="x", before=self.body)
        self.bell()
        # Let the pending "stopped" readings update the display before the modal dialog blocks.
        self.after(300, lambda: messagebox.showinfo("BW600 — load stopped", ev.message + ("\n\n(The device does not report the reason; "
                            "it was inferred from the last readings and your limits.)" if ev.inferred else "")))

    def on_alarm(self, ev: AlarmEvent):
        stopping = self.dev and self.dev.alarms.config.stop_load and self.dev.live and self.dev.live.running
        stamp = dt.datetime.now().strftime("%H:%M:%S")
        text = f"{stamp}  ALARM — {ev.message}" + ("  Load switched OFF." if stopping else "")
        self.console.insert("end", text + "\n")
        self.console.see("end")
        self.stop_var.set(text + "   (click to dismiss)")
        self.stop_banner.configure(bg="#f8d7da", fg="#58151c")
        self.stop_banner.pack(fill="x", before=self.body)
        self.bell()
        self.after(300, lambda: messagebox.showwarning("BW600 — alarm", text[10:]))

    def apply_alarms(self, save: bool = True):
        v = self.alarm_vars
        try:
            cfg = AlarmConfig(
                over_voltage_enabled=v["over_voltage_enabled"].get(),
                over_voltage=float(v["over_voltage"].get().replace(",", ".")),
                under_voltage_enabled=v["under_voltage_enabled"].get(),
                under_voltage=float(v["under_voltage"].get().replace(",", ".")),
                over_current_enabled=v["over_current_enabled"].get(),
                over_current=float(v["over_current"].get().replace(",", ".")),
                stop_load=v["stop_load"].get(),
            )
        except ValueError:
            messagebox.showerror("BW600", "Alarm limits must be numbers.")
            return
        if save:
            cfg.save()
        if self.dev:
            self.dev.alarms.config = cfg
            self.dev.alarms.reset()
        parts = []
        if cfg.over_voltage_enabled:
            parts.append(f"V > {cfg.over_voltage:g} V")
        if cfg.under_voltage_enabled:
            parts.append(f"V < {cfg.under_voltage:g} V")
        if cfg.over_current_enabled:
            parts.append(f"I > {cfg.over_current:g} A")
        self.alarm_state_var.set(("Active: " + ", ".join(parts) + (" → stop load" if cfg.stop_load else " → warn only"))
                                 if parts else "No application alarms enabled.")

    def on_settings(self, s: p.Settings):
        self.mode_var.set(f"Mode: {p.mode_name(s.mode)}")
        label, unit = p.SET_VALUE_LABEL.get(s.mode, ("Set value", ""))
        self.setval_label.set(label)
        self.setval_unit.set(unit)
        self.current_labels["mode"].set(f"device: {p.mode_name(s.mode)}")
        if not self.settings_loaded or not self.mode_choice.get():
            self.mode_choice.set(p.mode_name(s.mode))
        for key in ["set_value"] + [k for *_x, k in FLOAT_SETTINGS_TEST + FLOAT_SETTINGS_PROTECT + FLOAT_SETTINGS_CAL]:
            if key == "set_value" and s.mode in p.NO_SET_VALUE_MODES:
                self.current_labels[key].set("device: n/a in this mode")
                continue
            val = getattr(s, key)
            self.current_labels[key].set(f"device: {val:.4g}")
            if not self.settings_loaded:
                self.entries[key].set(f"{val:.4g}")
        for _l, _c, key in BYTE_SETTINGS + (CYCLE_SETTING,):
            self.current_labels[key].set(f"device: {getattr(s, key)}")
            if not self.settings_loaded:
                self.scales[key].set(getattr(s, key))
        self.current_labels["language"].set(f"device: {s.language}")
        self.current_labels["time"].set(f"device: {s.time_limit_h} h {s.time_limit_m} min")
        if not self.settings_loaded:
            self.entries["time_h"].set(str(s.time_limit_h))
            self.entries["time_m"].set(str(s.time_limit_m))
        self.settings_loaded = True

    def _window_seconds(self):
        return {"1 min": 60, "5 min": 300, "15 min": 900, "1 h": 3600}.get(self.window_var.get())

    def redraw(self):
        ax, ax2 = self.ax, self.ax2
        ax.clear()
        ax2.clear()
        if not self.hist_t:
            self.canvas.draw_idle()
            return
        t = list(self.hist_t)
        win = self._window_seconds()
        start = 0
        if win:
            limit = t[-1] - win
            while start < len(t) and t[start] < limit:
                start += 1
        t = t[start:]
        lines = []
        for key, label, color, axis in SERIES:
            if not self.series_on[key].get():
                continue
            y = list(self.hist[key])[start:]
            y = [float("nan") if v is None else v for v in y]
            target = ax if axis in (None, "left") else ax2
            lines += target.plot(t, y, color=color, label=label, linewidth=1.2)
        ax.set_xlabel("Time (s)")
        ax.ticklabel_format(useOffset=False)
        ax2.ticklabel_format(useOffset=False)
        ax.set_ylabel("V  /  mAh, Wh, °C")
        ax2.yaxis.tick_right()
        ax2.yaxis.set_label_position("right")
        ax2.set_ylabel("A  /  W")
        ax.grid(True, alpha=0.3)
        if lines:
            ax.legend(lines, [ln.get_label() for ln in lines], loc="upper left", fontsize=8)
        self.canvas.draw_idle()

    # ------------------------------------------------------------ files
    def toggle_record(self):
        if self.csv_file:
            self.csv_file.close()
            self.csv_file = self.csv_writer = None
            self.rec_btn.configure(text="Record CSV…")
            self.rec_var.set("Not recording")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", filetypes=[("CSV", "*.csv")],
            initialfile=f"LoadData_{dt.datetime.now():%Y_%m_%d_%H%M%S}.csv")
        if not path:
            return
        self.csv_file = open(path, "w", newline="", encoding="utf-8")
        self.csv_writer = csv.writer(self.csv_file)
        self.csv_writer.writerow(CSV_HEADER)
        self.rec_btn.configure(text="Stop recording")
        self.rec_var.set(f"Recording to {path}")

    def export_history(self):
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["t_s"] + [label for _k, label, *_ in SERIES])
            for i, t in enumerate(self.hist_t):
                w.writerow([f"{t:.2f}"] + ["" if self.hist[k][i] is None else self.hist[k][i] for k, *_ in SERIES])

    def export_png(self):
        path = filedialog.asksaveasfilename(defaultextension=".png", filetypes=[("PNG", "*.png")])
        if path:
            self.fig.savefig(path, dpi=150)

    def clear_history(self):
        self.hist_t.clear()
        for d in self.hist.values():
            d.clear()
        self.t0 = time.monotonic()
        self.redraw()

    def on_close(self):
        if self.csv_file:
            self.csv_file.close()
        if self.dev:
            self.dev.close()
        self.destroy()


def main(path: str | None = None):
    App(path).mainloop()
