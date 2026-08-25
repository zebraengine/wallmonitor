"""Retention: raw-JSON trim, its gates and cursor, readers on trimmed rows."""

import time

import pytest

from wallmonitor.config import parse_args
from wallmonitor.db import Database


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    yield database
    database.close()


def _fill(db, days_ago, n=20):
    base = time.time() - days_ago * 86400
    for i in range(n):
        ts = base + i * 10
        db.insert_vitals(ts, {"handle_temp_c": 30.0, "vehicle_current_a": 16.0,
                              "prox_v": 1.5, "pilot_high_v": 8.9}, 1, 3680.0)
        db.insert_wifi(ts, {"wifi_rssi": -60, "wifi_connected": True})
        db.insert_lifetime(ts, {"energy_wh": 1000})
        db.insert_ambient(ts, 25.0, raw={"t": 25.0}, source="test")


def _raw_counts(db):
    return {t: db._rows(f"SELECT COUNT(*) AS n FROM {t} WHERE raw != ''")[0]["n"]
            for t in Database.RAW_TRIM_TABLES}


def test_trim_respects_cutoff_and_keeps_columns(db):
    _fill(db, days_ago=30)
    _fill(db, days_ago=1)
    db.set_setting("diag_backfill_done", "1")
    counts = db.trim_raw(time.time() - 7 * 86400)
    assert counts["vitals_samples"] == 20 and counts["wifi_samples"] == 20
    remaining = _raw_counts(db)
    assert all(v == 20 for v in remaining.values()), remaining  # recent rows untouched
    # Columns intact and served for the trimmed era.
    old = db.vitals_range(time.time() - 31 * 86400, time.time() - 29 * 86400)
    assert len(old) == 20 and all(s["handle_temp_c"] == 30.0 and s["prox_v"] == 1.5 for s in old)
    # Bucketed path too (must not touch the blanked raw).
    bucketed = db.vitals_range(time.time() - 31 * 86400, time.time() - 29 * 86400, max_points=4)
    assert bucketed and all(b["prox_v"] == pytest.approx(1.5) for b in bucketed)


def test_vitals_trim_waits_for_diag_backfill(db):
    _fill(db, days_ago=30)
    counts = db.trim_raw(time.time())
    assert "vitals_samples" not in counts  # gated: backfill not done
    assert counts["wifi_samples"] == 20    # other tables trim regardless
    db.set_setting("diag_backfill_done", "1")
    assert db.trim_raw(time.time())["vitals_samples"] == 20


def test_trim_cursor_makes_second_run_cheap_and_correct(db):
    _fill(db, days_ago=30)
    db.set_setting("diag_backfill_done", "1")
    db.trim_raw(time.time() - 7 * 86400)
    # New rows age past the cutoff later; the cursor resumes, not rescans.
    _fill(db, days_ago=8)
    counts = db.trim_raw(time.time() - 7 * 86400)
    assert counts["vitals_samples"] == 20
    assert float(db.get_setting("raw_trim_ts:vitals_samples")) > time.time() - 9 * 86400


def test_vacuum_reclaims_trimmed_space(db):
    for i in range(300):
        db.insert_vitals(time.time() - 40 * 86400 + i, {"handle_temp_c": 30.0, "pad": "x" * 2000}, 1, 0.0)
    db.set_setting("diag_backfill_done", "1")
    db.trim_raw(time.time())
    before, after = db.vacuum()
    # 300 rows x ~2KB of trimmed blob must come back from the file.
    assert before - after > 300_000


def test_retain_flag_validation():
    with pytest.raises(SystemExit):
        parse_args(["--demo", "--retain-raw-days", "3"])
    cfg = parse_args(["--demo", "--retain-raw-days", "30"])
    assert cfg.retain_raw_days == 30.0
    cfg = parse_args(["--demo"])
    assert cfg.retain_raw_days == 0.0
