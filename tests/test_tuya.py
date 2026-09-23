import pytest

pytest.importorskip("tinytuya")

from bw600 import protocol as p  # noqa: E402
from bw600.tuya import NotOverWifi, TuyaBW600  # noqa: E402

# Status of a real BW600-DK over WiFi (idle, CC 5 A, cut-off 9 V) and the USB readings at that moment:
# 11.534 V, 6836 mAh, 35.738 Wh, probe 22.9 °C, MOS 24.0 °C, CPU 32 °C.
DPS = {"101": 1153, "102": 0, "103": 0, "104": False, "105": 6835, "106": 3573, "107": 9, "108": "CC",
       "109": 500, "110": 0, "111": 1500, "112": 900, "113": 320, "114": 239, "116": "Bat01", "117": 75,
       "118": 229, "120": 5, "121": 120, "122": False}
CFG = {"id": "test", "ip": "192.0.2.1", "key": "0123456789abcdef", "version": "3.5", "name": "BW600-DK"}


@pytest.fixture
def w(monkeypatch, tmp_path):
    import bw600.device as d
    monkeypatch.setattr(d, "BACKUP_DIR", str(tmp_path))
    return TuyaBW600(dict(CFG))


def test_live_mapping(w):
    live = w._to_live(DPS)
    assert live.voltage == pytest.approx(11.53) and live.energy == pytest.approx(35.73)
    assert live.capacity == 6835 and live.ntc_temp == pytest.approx(22.9)
    assert live.mos_temp == pytest.approx(23.9) and live.cpu_temp == pytest.approx(32.0)
    assert not live.running and live.resistance is None and live.fan is None


def test_live_running_discharge(w):
    live = w._to_live({**DPS, "104": True, "102": 4990, "101": 1018})
    assert live.running and live.discharging and live.amps == pytest.approx(4.99)
    assert live.resistance == pytest.approx(10.18 / 4.99)


def test_settings_mapping(w):
    s = w._to_settings(DPS)
    assert s.mode == p.Mode.CC and s.set_value == 5.0 and s.cutoff_voltage == 9.0
    assert s.full_voltage == 15.0 and s.full_current == pytest.approx(0.05)
    assert s.ntc_over_temp == 75 and s.mos_over_temp == 120 and s.work_brightness == 9
    assert s.record_slot == "Bat01"


def test_time_limit_is_decimal_hours(w):
    # verified on the device: 1 h 30 min set over USB reads 150 over WiFi
    s = w._to_settings({**DPS, "110": 150})
    assert (s.time_limit_h, s.time_limit_m) == (1, 30)
    w.set_time_limit(1, 30)
    assert w._pending[110] == 150


def test_writes_are_scaled_and_coalesced(w):
    w.set_value(4.5)
    w.set_float(p.Cmd.CUTOFF_VOLTAGE, 9.5)
    w.start_load()
    w.stop_load()                       # replaces the pending start: never start after a stop
    assert w._pending == {109: 450, 112: 950, 104: False}
    assert list(w._pending)[-1] == 104


def test_modes(w):
    w.set_mode(p.Mode.CABLE_TEST)
    assert w._pending[108] == "CRT"
    with pytest.raises(NotOverWifi):
        w.set_mode(p.Mode.CYCLE_TEST)   # not in the Tuya enum


def test_usb_only_features_refused(w):
    for call in (lambda: w.set_float(p.Cmd.OVER_CURRENT, 10),
                 lambda: w.set_byte(p.Cmd.STANDBY_TIME, 30),
                 lambda: w.simple(p.Cmd.DATA_ZERO),
                 lambda: w.set_language(3),
                 lambda: w.factory_reset(),
                 lambda: w.write_calibration({p.Cmd.CAL_VOLTAGE: 1.0}),
                 lambda: w.send(p.run_frame(True))):
        with pytest.raises(NotOverWifi):
            call()
    with pytest.raises(ValueError):
        w.set_byte(p.Cmd.WORK_BRIGHTNESS, 0)   # WiFi brightness is 1..9
    assert not w.supports("calibration") and w.supports("mos_over_temp")


def test_stop_is_verified_and_resent(w, monkeypatch):
    import bw600.tuya as t
    now = [100.0]
    monkeypatch.setattr(t.time, "monotonic", lambda: now[0])
    w.stop_load()
    w._pending.clear()                  # pretend it was sent
    w._dps = {"104": True}              # ...but the device still reports running
    now[0] += 3.1
    w._verify_stop()
    assert w._pending == {104: False}   # resent
    w._dps = {"104": False}
    w._verify_stop()
    assert w._stop_verify_until == 0.0  # confirmed stopped
