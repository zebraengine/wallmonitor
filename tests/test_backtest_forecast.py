"""The forecast backtest on a synthetic history whose truth is known: a
cold-start run at full rate followed by a step-down, both long enough to
plateau. The tool must find both runs, read both plateaus, score the live
forecast against them, and score the cross-current prediction at the step
from what was known at the change."""

import importlib.util
import math
import pathlib
import sys
import time

import pytest

from wallmonitor import thermal
from wallmonitor.db import Database

spec = importlib.util.spec_from_file_location(
    "backtest_forecast", pathlib.Path(__file__).parent.parent / "contrib" / "backtest_forecast.py"
)
bt = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bt
spec.loader.exec_module(bt)

TAU_S = 720.0
RISE_REF = 36.0


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    yield database
    database.close()


def _seed(db, start_ts, ambient_c, steps, dt=10.0):
    """Idle lead-in, then charging segments [(amps, seconds), ...] that
    follow the first-order model with the I^2 law, integrated so a
    step-down cools toward its lower plateau. Sensor samples every minute."""
    ts = start_ts - 1800
    while ts < start_ts:
        db.insert_vitals(ts, {
            "vehicle_connected": 0, "contactor_closed": 0, "vehicle_current_a": 0.0,
            "handle_temp_c": round(thermal.idle_handle_c(ambient_c), 2), "pcba_temp_c": 38.0, "mcu_temp_c": 46.0,
        }, None, 0.0)
        ts += dt
    for minute in range(-30, int(sum(s for _, s in steps) / 60) + 1):
        db.insert_ambient(start_ts + 60 * minute, ambient_c)
    sid = db.start_session(start_ts)
    temp = thermal.idle_handle_c(ambient_c)
    ts = start_ts
    for amps, seconds in steps:
        end = ts + seconds
        while ts < end:
            db.insert_vitals(ts, {
                "vehicle_connected": 1, "contactor_closed": 1, "vehicle_current_a": amps,
                "handle_temp_c": round(temp, 3), "pcba_temp_c": 55.0, "mcu_temp_c": 50.0,
            }, sid, amps * 233.0)
            t_inf = ambient_c + RISE_REF * (amps / thermal.REF_CURRENT_A) ** 2
            temp += dt * (t_inf - temp) / TAU_S
            ts += dt
    db.close_session(sid, ts, "vehicle_disconnected")
    return sid


def test_backtest_scores_a_cold_start_and_a_step_down(db, tmp_path, capsys):
    now = time.time()
    # History for the model to fit from, then the session under test.
    for i in range(4):
        _seed(db, now - (8 - i) * 7200, ambient_c=25.0, steps=[(48.6, 2400)])
    sid = _seed(db, now - 7200, ambient_c=25.0, steps=[(48.6, 2700), (40.0, 2700)])

    out = tmp_path / "bt.json"
    assert bt.main(["--db", str(tmp_path / "test.db"), "--json", str(out)]) == 0
    text = capsys.readouterr().out
    assert "cold_start" in text and "step_down" in text

    import json
    data = json.loads(out.read_text())
    runs = [r for r in data["runs"] if r["session_id"] == sid]
    assert [r["kind"] for r in runs] == ["cold_start", "step_down"]
    assert [round(r["current_a"], 1) for r in runs] == [48.6, 40.0]
    # Both runs held >= 3 tau, so both plateaus are observed, and they are
    # the seeded ones.
    expected = [25.0 + RISE_REF * (a / 48.0) ** 2 for a in (48.6, 40.0)]
    for run, plateau in zip(runs, expected):
        assert run["plateau_c"] is not None and abs(run["plateau_c"] - plateau) < 1.0
    # The live forecast converges onto the plateau as the run matures.
    late = [t["error_c"] for t in data["ticks"]
            if t["session_id"] == sid and t["basis"] == "trajectory" and 10 <= t["minutes"] < 20]
    assert late and max(abs(e) for e in late) < 1.5
    # At the step-down, every ambient on offer predicts the lower plateau —
    # the history is I^2 and isothermal, so sensor, implied and warmer agree.
    step = [b for b in data["boundaries"] if b["session_id"] == sid and b["kind"] == "step_down"]
    assert len(step) == 1 and step[0]["from_a"] == pytest.approx(48.6, abs=0.1)
    for method, predicted in step[0]["predictions"].items():
        assert abs(predicted - step[0]["actual_c"]) < 1.5, (method, predicted, step[0]["actual_c"])
    assert {m.split("/")[0] for m in step[0]["predictions"]} == {"sensor", "implied", "warmer"}
    assert {m.split("/")[1] for m in step[0]["predictions"]} == {"I2", "n"}
    # ...and the session start is scored from the sensor and the idle handle.
    start = [b for b in data["boundaries"] if b["session_id"] == sid and b["kind"] == "cold_start"]
    assert len(start) == 1 and start[0]["from_a"] is None
    assert {m.split("/")[0] for m in start[0]["predictions"]} == {"sensor", "idle"}


def test_backtest_tags_probe_runs_from_the_controller_event(db, tmp_path, capsys):
    now = time.time()
    for i in range(4):
        _seed(db, now - (8 - i) * 7200, ambient_c=25.0, steps=[(48.6, 2400)])
    start = now - 7200
    sid = _seed(db, start, ambient_c=25.0, steps=[(48.6, 2700), (32.0, 2700)])
    db.add_event(start + 2700 - 20, "amp_capped", {
        "to_a": 32.0, "reason": "calibration probe due (warm cable): capping to 32A for 40min",
        "probe": {"amps": 32.0, "cable": "warm"},
    })
    out = tmp_path / "bt.json"
    assert bt.main(["--db", str(tmp_path / "test.db"), "--json", str(out)]) == 0
    import json
    data = json.loads(out.read_text())
    step = [r for r in data["runs"] if r["session_id"] == sid and r["kind"] == "step_down"]
    assert len(step) == 1 and step[0]["probe"] is True and step[0]["probe_cable"] == "warm"
    assert "probe 32A warm" in capsys.readouterr().out
    # The full-rate run before it is not a probe.
    assert not [r for r in data["runs"] if r["session_id"] == sid and r["kind"] == "cold_start"][0]["probe"]


def test_split_runs_drops_ramp_samples_and_splits_on_a_current_change():
    def row(i, amps, closed=1):
        return {"ts": 1000.0 + 2.0 * i, "contactor_closed": closed, "vehicle_current_a": amps, "handle_temp_c": 30.0}

    rows = [row(i, a) for i, a in enumerate([6.1, 14.6, 23.9, 31.1, 32.6, 32.7, 32.9, 37.6, 43.3] + [32.5] * 200 + [44.0] * 100)]
    runs = bt.split_runs(rows)
    assert [len(r) for r in runs] == [200, 100]
    assert runs[0][0]["vehicle_current_a"] == 32.5 and runs[1][0]["vehicle_current_a"] == 44.0
    # A contactor drop ends a run; a short remainder is dropped.
    rows = [row(i, 48.0) for i in range(100)] + [row(100, 0.0, closed=0)] + [row(101 + i, 48.0) for i in range(10)]
    assert [len(r) for r in bt.split_runs(rows)] == [100]
