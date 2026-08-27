"""Per-install idle-offset calibration: estimator, gates, hysteresis, and
the model's reach into the proxy-tier fits and the forecast."""

import json
import math
import time

import pytest

from wallmonitor import calibration, thermal
from wallmonitor.db import Database


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    yield database
    database.close()


TRUE = thermal.IdleOffset(ref_c=3.0, slope=-0.08, ambient_ref_c=30.0, ambient_range_c=(15.0, 45.0))


def _seed_idle_days(db, now, days, true_model=TRUE, source="ecowitt", sensor=True, tau_s=660.0):
    """`days` of a garage: ambient swings 24->34 C daily, the handle sits at
    true_model.handle_c(ambient) with first-order lag, one 40-min charge
    each afternoon so settle gating has something to gate. Sensor samples
    every 60 s when `sensor`."""
    t = now - days * 86400.0
    handle = true_model.handle_c(24.0)
    while t < now:
        hour = (t % 86400.0) / 3600.0
        ambient = 29.0 + 5.0 * math.sin((hour - 9.0) / 24.0 * 2 * math.pi)
        charging = 14.0 <= hour < 14.67
        target = ambient + 30.0 if charging else true_model.handle_c(ambient)
        handle += 10.0 * (target - handle) / tau_s
        db.insert_vitals(t, {
            "vehicle_connected": 1 if charging else 0, "contactor_closed": 1 if charging else 0,
            "vehicle_current_a": 48.0 if charging else 0.0, "handle_temp_c": round(handle, 3),
            "pcba_temp_c": 40.0, "mcu_temp_c": 45.0,
        }, None, 11000.0 if charging else 0.0)
        if sensor and int(t) % 60 == 0:
            db.insert_ambient(t, round(ambient, 2), source=source)
        t += 10.0


def test_estimator_recovers_seeded_offset_model(db):
    now = time.time()
    now -= now % 60
    _seed_idle_days(db, now, 6)
    cal = calibration.calibrate(db, now, lookback_days=10)
    assert cal is not None and cal.n_days >= 5 and cal.n_segments >= 8
    # Offset at the reference ambient and the slope both land on the truth.
    assert abs(cal.offset_ref_c - TRUE.offset_c(30.0)) < 0.25
    assert abs(cal.slope - TRUE.slope) < 0.03
    assert cal.residual_sd_c < 0.3
    assert calibration.gate(cal) is None


def test_car_source_is_not_ground_truth(db):
    now = time.time()
    now -= now % 60
    _seed_idle_days(db, now, 6, source="car")
    assert calibration.calibrate(db, now, lookback_days=10) is None


def test_no_sensor_keeps_builtin(db):
    now = time.time()
    _seed_idle_days(db, now, 3, sensor=False)
    old, new, why = calibration.maybe_adopt(db, now, thermal.IDLE_OFFSET_SETTING)
    assert new is None and why is None
    assert thermal.load_idle_offset(db) is thermal.BUILTIN_IDLE_OFFSET


def test_adoption_gates_and_hysteresis(db):
    now = time.time()
    now -= now % 60
    _seed_idle_days(db, now, 6)
    old, new, why = calibration.maybe_adopt(db, now, thermal.IDLE_OFFSET_SETTING)
    assert why is None and old is None and new["source"] == "calibrated"
    stored = thermal.load_idle_offset(db)
    assert stored.source == "calibrated" and abs(stored.offset_c(30.0) - TRUE.offset_c(30.0)) < 0.25
    assert stored.ambient_se_c < 0.5  # the calibration's own scatter, not the 1.5 default
    # Same data again: no material change, nothing rewritten.
    old2, new2, why2 = calibration.maybe_adopt(db, now, thermal.IDLE_OFFSET_SETTING)
    assert new2 is None and why2 == "no material change" and old2 == new
    # Gates: an implausible fit never becomes the model.
    bad = calibration.Calibration(
        n_samples=1000, n_segments=20, n_days=5, mean_offset_c=8.0, sd_c=0.3, ci95_c=(7.8, 8.2),
        slope=0.0, slope_se=0.01, ambient_lo_c=20.0, ambient_hi_c=35.0, offset_ref_c=8.0,
        residual_sd_c=0.3, day_mean_c=None, night_mean_c=None, from_ts=0.0, to_ts=1.0)
    assert "outside" in calibration.gate(bad)
    steep = calibration.Calibration(**{**bad.as_dict(), "offset_ref_c": 1.0, "mean_offset_c": 1.0, "slope": -0.9})
    assert "implausible" in calibration.gate(steep)
    thin = calibration.Calibration(**{**bad.as_dict(), "offset_ref_c": 1.0, "mean_offset_c": 1.0, "n_days": 2})
    assert "days" in calibration.gate(thin)
    # Narrow coverage can't support a slope: the model degrades to a constant.
    narrow = calibration.Calibration(**{**bad.as_dict(), "offset_ref_c": 1.0, "mean_offset_c": 1.2,
                                        "slope": -0.3, "ambient_lo_c": 27.0, "ambient_hi_c": 29.0})
    model = calibration.proposed_model(narrow, now)
    assert model["slope"] == 0.0 and abs(model["ref_c"] - 1.2) < 1e-6


def test_load_idle_offset_rejects_garbage(db):
    db.set_setting(thermal.IDLE_OFFSET_SETTING, "not json")
    assert thermal.load_idle_offset(db) is thermal.BUILTIN_IDLE_OFFSET
    db.set_setting(thermal.IDLE_OFFSET_SETTING, json.dumps({"ref_c": 1, "slope": -1.2, "ambient_ref_c": 30,
                                                            "ambient_range_c": [20, 30]}))
    assert thermal.load_idle_offset(db) is thermal.BUILTIN_IDLE_OFFSET  # slope would blow up the inversion
    db.set_setting(thermal.IDLE_OFFSET_SETTING, json.dumps({"ref_c": 2.5, "slope": -0.1, "ambient_ref_c": 30,
                                                            "ambient_range_c": [20, 40], "source": "calibrated",
                                                            "residual_sd_c": 0.4}))
    model = thermal.load_idle_offset(db)
    assert model.source == "calibrated" and abs(model.ambient_from_handle(model.handle_c(27.0)) - 27.0) < 1e-9


def test_proxy_fits_follow_the_calibrated_model(db):
    # A garage whose real idle offset is 3 C, no sensor at charge time: under
    # the built-in seed (1.4 C) every pre-idle fit reads ambient ~1.6 C high
    # and the rise ~1.6 C low. With the install's own model adopted, the
    # same rows fit the seeded rise.
    now = time.time()
    rise, ambient = 36.0, 27.0
    # Seed ramps whose idle lead-in sits at the *true* handle temperature.
    for i in range(3):
        start = now - (4 - i) * 7200
        _seed_idle_true(db, start - 1800, start, ambient)
        _seed_ramp(db, start, ambient, rise, TRUE)
    naive = thermal.fit_sessions(db, now)
    assert len(naive) == 3
    bias = TRUE.offset_c(ambient) - thermal.BUILTIN_IDLE_OFFSET.offset_c(ambient)
    assert bias > 1.0
    for fit in naive:
        assert fit["ambient_source"] == "pre_idle"
        assert abs(fit["rise_ref_c"] - (rise - bias)) < 1.0  # biased low by the offset error
    db.set_setting(thermal.IDLE_OFFSET_SETTING, json.dumps({
        "ref_c": TRUE.ref_c, "slope": TRUE.slope, "ambient_ref_c": TRUE.ambient_ref_c,
        "ambient_range_c": list(TRUE.ambient_range_c), "source": "calibrated", "residual_sd_c": 0.3}))
    calibrated = thermal.fit_sessions(db, now)
    for fit in calibrated:
        assert abs(fit["rise_ref_c"] - rise) < 1.0
    # The forecast reports whose model it is, and the proxy's uncertainty.
    out = thermal.predict(db, now, thermal.fit_history(db, now, fits=calibrated))
    assert out["model"]["idle_offset"]["source"] == "calibrated"
    assert out["model"]["idle_offset"]["ambient_se_c"] == 0.3


def test_idle_forecast_states_proxy_uncertainty(db):
    now = time.time()
    _seed_idle_true(db, now - 900, now, 26.0)
    out = thermal.predict(db, now, thermal.ThermalParams())
    assert out["state"] == "idle" and out["ambient_source"] == "idle_handle"
    assert out["ambient_se_c"] == thermal.IDLE_OFFSET_UNCALIBRATED_SE_C
    assert out["model"]["idle_offset"]["source"] == "built-in"


def _seed_idle_true(db, t_from, t_to, ambient_c, model=TRUE, dt=10.0):
    ts = t_from
    while ts < t_to:
        db.insert_vitals(ts, {"vehicle_connected": 1, "contactor_closed": 0, "vehicle_current_a": 0.0,
                              "handle_temp_c": round(model.handle_c(ambient_c), 2),
                              "pcba_temp_c": 38.0, "mcu_temp_c": 46.0}, None, 0.0)
        ts += dt


def _seed_ramp(db, start_ts, ambient_c, rise_ref_c, model=TRUE, tau_s=720.0, amps=48.6, charge_s=1500.0, dt=10.0):
    sid = db.start_session(start_ts)
    t0 = model.handle_c(ambient_c)
    t_inf = ambient_c + rise_ref_c * (amps / thermal.REF_CURRENT_A) ** 2
    ts = start_ts
    while ts <= start_ts + charge_s:
        temp = t_inf - (t_inf - t0) * math.exp(-(ts - start_ts) / tau_s)
        db.insert_vitals(ts, {"vehicle_connected": 1, "contactor_closed": 1, "vehicle_current_a": amps,
                              "handle_temp_c": round(temp, 3), "pcba_temp_c": 55.0, "mcu_temp_c": 50.0},
                         sid, amps * 233.0)
        ts += dt
    db.close_session(sid, start_ts + charge_s, "vehicle_disconnected")
