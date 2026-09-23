import pytest

from bw600 import protocol as p

# Frames captured from a real BW600 V2.0.5 (idle, CC mode, 12 V on the input).
LIVE = bytes.fromhex(
    "AA050105 00000000 ED2E0000 00000000 00000000 77969800 1C670000 3AE25900"
    " 606D0000 1C570000 CE510000 00000000 00000000 00000000 00000000 0000EEFF")
SETTINGS = bytes.fromhex(
    "AA050103 3F800000 3F800000 3F8036F9 3F6A221A 40400000 42100000 3D4CCCCD"
    " 42500000 44188000 42960000 42F00000 00030903 3C00000A 66666666 6666EEFF")


def test_frames_match_vendor_software():
    assert p.run_frame(True) == bytes.fromhex("55050125 01000000 EEFF")
    assert p.run_frame(False) == bytes.fromhex("55050125 00000000 EEFF")
    assert p.float_frame(p.Cmd.SET_VALUE, 1.5) == bytes.fromhex("55050121 3FC00000 EEFF")
    assert p.poll_frame(p.Cmd.READ_LIVE) == bytes.fromhex("55050105 0B008CEE FF")
    assert p.read_settings_frame() == bytes.fromhex("55050103 000000EE FF")
    assert p.byte_frame(p.Cmd.WORK_BRIGHTNESS, 9) == bytes.fromhex("55050122 00000009 EEFF")
    assert p.language_frame(3) == bytes.fromhex("55050120 03000000 EEFF")
    assert p.time_limit_frames(2, 30) == [bytes.fromhex("55050131 02000001 EEFF"),
                                          bytes.fromhex("55050131 1E000002 EEFF")]


def test_parse_live():
    live = p.parse_live(LIVE)
    assert live.voltage == pytest.approx(12.013)
    assert live.current == 0
    assert live.resistance == pytest.approx(9999.991)
    assert live.energy == pytest.approx(26.396)
    assert live.capacity == pytest.approx(5890.618)
    assert live.cpu_temp == pytest.approx(28.0)
    assert live.ntc_temp == pytest.approx(22.300)
    assert live.mos_temp == pytest.approx(20.942)
    assert not live.running


def test_negative_current_flag():
    buf = bytearray(LIVE)
    buf[0x0C:0x10] = (1500).to_bytes(4, "little")
    buf[0x3C] = 0x80
    assert p.parse_live(bytes(buf)).current == pytest.approx(-1.5)


def test_out_of_range_keeps_previous():
    prev = p.parse_live(LIVE)
    buf = bytearray(LIVE)
    buf[0x08:0x0C] = (600_000).to_bytes(4, "little")
    assert p.parse_live(bytes(buf), prev).voltage == prev.voltage


def test_parse_settings():
    s = p.parse_settings(SETTINGS)
    assert s.mode == p.Mode.CC
    assert s.set_value == 1.0
    assert s.cutoff_voltage == 3.0
    assert s.full_voltage == 36.0
    assert s.full_current == pytest.approx(0.05)
    assert s.over_current == 52.0
    assert s.over_power == 610.0
    assert s.ntc_over_temp == 75.0
    assert s.mos_over_temp == 120.0
    assert (s.language, s.work_brightness, s.standby_brightness, s.standby_time) == (3, 9, 3, 60)
    assert (s.time_limit_h, s.time_limit_m) == (0, 0)


def test_wrong_type_rejected():
    assert p.parse_settings(LIVE) is None
    assert p.parse_live(SETTINGS) is None


# Captured while discharging the battery at 1 A (CC mode).
RUNNING = bytes.fromhex(
    "AA050105 00000000 EF2D0000 E7030000 EE2D0000 F32D0000 68670000 5AFB5900"
    " 18790000 FF540000 39560000 30750000 00000000 01000000 00000000 8000EEFF")


def test_parse_running_discharge():
    live = p.parse_live(RUNNING)
    assert live.running and live.discharging
    assert live.current == pytest.approx(-0.999)
    assert live.amps == pytest.approx(0.999)
    assert live.voltage == pytest.approx(11.759)
    assert live.power == pytest.approx(11.758)
    assert live.fan == 30.0


def _live(v, i, running=True, **kw):
    return p.Live(voltage=v, current=-i, power=v * i, running=running, flags=0x80, **kw)


def _settings(**kw):
    s = p.parse_settings(SETTINGS)
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def test_stop_reason_cutoff():
    # Real case: 5 A discharge, 9 V cut-off, voltage 9.003 V then the device stopped.
    window = [_live(9.060, 5.0), _live(9.033, 5.0), _live(9.003, 5.0), _live(10.422, 0.072)]
    ev = p.infer_stop_reason(window, _settings(cutoff_voltage=9.0), run_seconds=60)
    assert ev.reason == "cutoff" and ev.inferred


def test_stop_reason_user_and_unknown():
    s = _settings()
    assert p.infer_stop_reason([_live(12.0, 1.0)], s, requested=True).reason == "user"
    assert p.infer_stop_reason([_live(11.8, 1.0)], s, run_seconds=30).reason == "device"


def test_stop_reason_time_limit_and_temps():
    s = _settings(time_limit_h=0, time_limit_m=1)
    assert p.infer_stop_reason([_live(11.8, 1.0)], s, run_seconds=60).reason == "time"
    hot = [_live(11.8, 1.0, mos_temp=120.4)]
    assert p.infer_stop_reason(hot, _settings(), run_seconds=10).reason == "mos_otp"


def test_alarm_debounce_and_rearm():
    from bw600.alarms import AlarmConfig, AlarmMonitor
    mon = AlarmMonitor(AlarmConfig(over_voltage_enabled=True, over_voltage=12.0,
                                   over_current_enabled=True, over_current=4.0))
    assert mon.check(_live(12.5, 1.0)) == []            # first reading over: debounced
    ev = mon.check(_live(12.6, 1.0))
    assert [e.kind for e in ev] == ["over_voltage"]
    assert mon.check(_live(12.7, 1.0)) == []            # latched: no repeat
    assert mon.check(_live(11.9, 1.0)) == []            # within hysteresis: still latched
    assert mon.active() == ["over_voltage"]
    mon.check(_live(11.5, 1.0))                         # re-armed
    assert mon.active() == []
    mon.check(_live(12.5, 1.0))
    assert [e.kind for e in mon.check(_live(12.5, 1.0))] == ["over_voltage"]
    mon.check(_live(10.0, 5.0))
    assert [e.kind for e in mon.check(_live(10.0, 5.0))] == ["over_current"]


def test_alarm_disabled():
    from bw600.alarms import AlarmConfig, AlarmMonitor
    mon = AlarmMonitor(AlarmConfig())
    for _ in range(5):
        assert mon.check(_live(50.0, 50.0)) == []


def test_under_voltage_alarm():
    from bw600.alarms import AlarmConfig, AlarmMonitor
    mon = AlarmMonitor(AlarmConfig(under_voltage_enabled=True, under_voltage=10.0))
    assert mon.check(_live(0.0, 0.0, running=False)) == []   # nothing connected: ignored
    assert mon.check(_live(0.0, 0.0, running=False)) == []
    assert mon.check(_live(11.0, 5.0)) == []
    assert mon.check(_live(9.9, 5.0)) == []                  # debounce
    ev = mon.check(_live(9.8, 5.0))
    assert [e.kind for e in ev] == ["under_voltage"] and "<" in ev[0].message
    assert mon.check(_live(9.7, 5.0)) == []                  # latched
    assert mon.check(_live(10.1, 0.0)) == []                 # inside hysteresis: still latched
    assert mon.active() == ["under_voltage"]
    mon.check(_live(10.5, 0.0))                              # re-armed
    assert mon.active() == []
    mon.check(_live(9.5, 5.0))
    assert [e.kind for e in mon.check(_live(9.5, 5.0))] == ["under_voltage"]


def test_mode_frames():
    # Verified on a BW600 V2.0.5: 0x47 + mode selects the mode.
    assert p.mode_frame(p.Mode.CC) == bytes.fromhex("55050147 00000000 EEFF")
    assert p.mode_frame(p.Mode.CV) == bytes.fromhex("55050148 00000000 EEFF")
    assert p.mode_frame(p.Mode.CYCLE_TEST) == bytes.fromhex("55050150 00000000 EEFF")
    assert {p.MODE_KEYS[k] for k in p.MODE_KEYS} == set(p.Mode)
    with pytest.raises(ValueError):
        p.mode_frame(10)
