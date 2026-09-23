"""Application-side alarms (over-voltage / over-current).

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


@dataclass
class AlarmEvent:
    kind: str       # "over_voltage" | "over_current"
    value: float
    limit: float
    message: str


class AlarmMonitor:
    """Feed live samples with :meth:`check`; returns newly tripped alarms.

    An alarm trips once the limit is exceeded for ``samples`` consecutive
    readings and re-arms only after the value falls below the limit minus the
    hysteresis, so a sustained fault produces a single event.
    """

    def __init__(self, config: AlarmConfig):
        self.config = config
        self._count = {"over_voltage": 0, "over_current": 0}
        self._latched = {"over_voltage": False, "over_current": False}

    def reset(self) -> None:
        for k in self._count:
            self._count[k] = 0
            self._latched[k] = False

    def check(self, live: p.Live) -> list[AlarmEvent]:
        c = self.config
        checks = (
            ("over_voltage", c.over_voltage_enabled, live.voltage, c.over_voltage, "V", "Over-voltage"),
            ("over_current", c.over_current_enabled, live.amps, c.over_current, "A", "Over-current"),
        )
        events = []
        for kind, enabled, value, limit, unit, label in checks:
            if not enabled or value is None or limit <= 0:
                self._count[kind] = 0
                self._latched[kind] = False
                continue
            if value > limit:
                self._count[kind] += 1
                if not self._latched[kind] and self._count[kind] >= max(1, c.samples):
                    self._latched[kind] = True
                    events.append(AlarmEvent(kind, value, limit,
                                             f"{label}: {value:.3f} {unit} > limit {limit:g} {unit}."))
            else:
                self._count[kind] = 0
                if value < limit * (1 - c.hysteresis):
                    self._latched[kind] = False
        return events

    def active(self) -> list[str]:
        return [k for k, v in self._latched.items() if v]
