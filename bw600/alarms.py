"""Application-side alarms (over-voltage / under-voltage / over-current).

The BW600 itself only protects against over-current, over-power and
over-temperature, at limits meant to protect the load. These alarms are
checked by this program against limits chosen for the device under test, and
can switch the load off.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass

from . import protocol as p

CONFIG_PATH = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
                           "bw600", "alarms.json")


@dataclass
class AlarmConfig:
    over_voltage_enabled: bool = False
    over_voltage: float = 15.0        # V
    under_voltage_enabled: bool = False
    under_voltage: float = 10.0       # V (readings below NO_INPUT_V are ignored)
    over_current_enabled: bool = False
    over_current: float = 10.0        # A
    stop_load: bool = True            # switch the load off when an alarm trips
    samples: int = 2                  # consecutive readings needed (debounce)
    hysteresis: float = 0.02          # relative margin before the alarm re-arms

    @classmethod
    def load(cls, path: str = CONFIG_PATH) -> "AlarmConfig":
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        except (OSError, ValueError, TypeError):
            return cls()

    def save(self, path: str = CONFIG_PATH) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2)


# Below this the input is considered disconnected, not under-voltage.
NO_INPUT_V = 0.5


@dataclass
class AlarmEvent:
    kind: str       # "over_voltage" | "under_voltage" | "over_current"
    value: float
    limit: float
    message: str


class AlarmMonitor:
    """Feed live samples with :meth:`check`; returns newly tripped alarms.

    An alarm trips once the limit is crossed for ``samples`` consecutive
    readings and re-arms only after the value is back inside the limit by the
    hysteresis margin, so a sustained fault produces a single event.
    """

    def __init__(self, config: AlarmConfig):
        self.config = config
        self._count = {"over_voltage": 0, "under_voltage": 0, "over_current": 0}
        self._latched = {"over_voltage": False, "under_voltage": False, "over_current": False}

    def reset(self) -> None:
        for k in self._count:
            self._count[k] = 0
            self._latched[k] = False

    def check(self, live: p.Live) -> list[AlarmEvent]:
        c = self.config
        under_v = live.voltage if live.voltage is not None and live.voltage >= NO_INPUT_V else None
        checks = (  # (kind, enabled, value, limit, unit, label, sign: +1 = trips above, -1 = below)
            ("over_voltage", c.over_voltage_enabled, live.voltage, c.over_voltage, "V", "Over-voltage", 1),
            ("under_voltage", c.under_voltage_enabled, under_v, c.under_voltage, "V", "Under-voltage", -1),
            ("over_current", c.over_current_enabled, live.amps, c.over_current, "A", "Over-current", 1),
        )
        events = []
        for kind, enabled, value, limit, unit, label, sign in checks:
            if not enabled or limit <= 0:
                self._count[kind] = 0
                self._latched[kind] = False
                continue
            if value is None:  # no reading / no input: keep state, count nothing
                self._count[kind] = 0
                continue
            if sign * (value - limit) > 0:
                self._count[kind] += 1
                if not self._latched[kind] and self._count[kind] >= max(1, c.samples):
                    self._latched[kind] = True
                    cmp = ">" if sign > 0 else "<"
                    events.append(AlarmEvent(kind, value, limit,
                                             f"{label}: {value:.3f} {unit} {cmp} limit {limit:g} {unit}."))
            else:
                self._count[kind] = 0
                # re-arm once back inside the limit by the hysteresis margin
                if sign * (value - limit * (1 - sign * c.hysteresis)) < 0:
                    self._latched[kind] = False
        return events

    def active(self) -> list[str]:
        return [k for k, v in self._latched.items() if v]
