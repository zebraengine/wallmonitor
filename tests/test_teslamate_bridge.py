"""The bridge's value is its silences: every gate that stops a post is a
situation where the car's thermometer would mislead the thermal model.
decide() is pure, so the whole decision table tests without Postgres,
Docker, or a network."""

import importlib.util
import pathlib
import sys

spec = importlib.util.spec_from_file_location(
    "teslamate_ambient_bridge",
    pathlib.Path(__file__).parent.parent / "contrib" / "teslamate_ambient_bridge.py",
)
bridge = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bridge  # dataclass annotation resolution needs this
spec.loader.exec_module(bridge)

NOW = 1_785_000_000.0
HOME = (39.2000, -77.3000)
AWAY = (39.2500, -77.3500)  # ~7 km out


def _cfg(**kw):
    return bridge.Config(car_id=1, home=kw.pop("home", HOME), **kw)


def _reading(age_s=120.0, temp=28.0, where=HOME):
    lat, lon = where if where else (None, None)
    return bridge.Reading(NOW - age_s, temp, lat, lon)


def _decide(readings, cfg, last_drive_end=0.0, driving=False, plugged=False,
            last_posted=0.0):
    return bridge.decide(NOW, readings, last_drive_end, driving, plugged,
                         last_posted, cfg)


def test_fresh_parked_at_home_posts():
    reading, reason = _decide([_reading(), None], _cfg())
    assert reading is not None and reason == "ok"
    assert reading.temp_c == 28.0


def test_newest_of_parked_and_charging_wins():
    parked, charging = _reading(age_s=400, temp=30.0), _reading(age_s=60, temp=27.0)
    reading, _ = _decide([parked, charging], _cfg())
    assert reading.temp_c == 27.0


def test_stale_reading_is_silence_not_error():
    # An asleep or absent car must go silent so wallmonitor's freshness
    # window expires and the handle proxy takes back over.
    reading, reason = _decide([_reading(age_s=1200)], _cfg())
    assert reading is None and "old" in reason


def test_driving_and_post_drive_cooldown_suppress():
    reading, _ = _decide([_reading()], _cfg(), driving=True)
    assert reading is None
    # Drive ended 10 min ago: inside the 45-min heat-soak window.
    reading, reason = _decide([_reading()], _cfg(), last_drive_end=NOW - 600)
    assert reading is None and "cooldown" in reason
    # Drive ended hours ago: admissible again.
    reading, _ = _decide([_reading()], _cfg(), last_drive_end=NOW - 7200)
    assert reading is not None


def test_away_from_home_suppresses():
    reading, reason = _decide([_reading(where=AWAY)], _cfg())
    assert reading is None and "from home" in reason
    # Home configured but the reading has no fix: err on silence.
    reading, _ = _decide([_reading(where=None)], _cfg())
    assert reading is None


def test_no_home_falls_back_to_plugged_in_gate():
    cfg = _cfg(home=None)
    reading, reason = _decide([_reading()], cfg)
    assert reading is None and "plugged" in reason
    reading, _ = _decide([_reading()], cfg, plugged=True)
    assert reading is not None


def test_dedupe_and_bounds():
    ts = _reading().ts
    reading, reason = _decide([_reading()], _cfg(), last_posted=ts)
    assert reading is None and "already" in reason
    reading, _ = _decide([_reading(temp=120.0)], _cfg())
    assert reading is None


def test_haversine_sanity():
    # One degree of latitude is ~111 km; home-to-home is zero.
    assert bridge.haversine_m(*HOME, *HOME) < 1.0
    assert abs(bridge.haversine_m(39.0, -77.0, 40.0, -77.0) - 111_000) < 500
