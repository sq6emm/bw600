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
