"""Diagnostics columns: migration, backfill, COALESCE fallback, payload trims."""

import sqlite3
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from wallmonitor.db import Database
from wallmonitor.poller import EventBus
from wallmonitor.web import make_app


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    yield database
    database.close()


def _raw(i: int) -> dict:
    return {
        "vehicle_connected": True, "contactor_closed": True, "session_s": i,
        "grid_v": 230.123456, "vehicle_current_a": 16.0, "handle_temp_c": 30.0 + i * 0.123456,
        "pilot_high_v": 8.9 + i * 0.001, "pilot_low_v": -11.9, "prox_v": 1.5,
        "relay_coil_v": 11.9, "relay_k2_v": 0.0,
    }


def test_columns_written_on_insert_and_served(db):
    now = time.time()
    for i in range(5):
        db.insert_vitals(now - 50 + i * 10, _raw(i), 1, 3680.0)
    row = db._rows("SELECT pilot_high_v, prox_v, relay_k1_v FROM vitals_samples ORDER BY id LIMIT 1")[0]
    assert row["prox_v"] == 1.5 and row["pilot_high_v"] == pytest.approx(8.9)
    assert row["relay_k1_v"] == 11.9  # relay_coil_v fallback for older firmware naming
    samples = db.vitals_range(now - 60, now)
    assert [s["relay_k1_v"] for s in samples] == [11.9] * 5


def test_backfill_fills_old_rows_and_queries_work_before_it(db, tmp_path):
    now = time.time()
    for i in range(25):
        db.insert_vitals(now - 300 + i * 10, _raw(i), 1, 3680.0)
    # Simulate rows recorded before the columns existed.
    with db._lock:
        db._conn.execute("UPDATE vitals_samples SET pilot_high_v=NULL, pilot_low_v=NULL, prox_v=NULL, "
                         "relay_k1_v=NULL, relay_k2_v=NULL")
        db._conn.commit()
    # COALESCE fallback: the un-backfilled rows still chart correctly…
    samples = db.vitals_range(now - 400, now)
    assert all(s["prox_v"] == 1.5 for s in samples)
    # …and in the bucketed path too.
    bucketed = db.vitals_range(now - 400, now, max_points=5)
    assert len(bucketed) <= 6 and all(b["prox_v"] == pytest.approx(1.5) for b in bucketed)

    seen = []
    touched = db.backfill_diag_columns(chunk=7, progress=lambda done, total: seen.append((done, total)))
    assert touched == 25
    assert seen and seen[-1][0] >= seen[-1][1]
    nulls = db._rows("SELECT COUNT(*) AS n FROM vitals_samples WHERE prox_v IS NULL")[0]["n"]
    assert nulls == 0
    assert db.get_setting("diag_backfill_done") == "1"
    # Second run is a no-op, and range queries now take the raw-free path.
    assert db.backfill_diag_columns() == 0
    assert db._diag_backfilled is True
    assert all(s["prox_v"] == 1.5 for s in db.vitals_range(now - 400, now))


def test_migration_adds_columns_to_an_old_database(tmp_path):
    path = str(tmp_path / "old.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE vitals_samples (id INTEGER PRIMARY KEY, ts REAL NOT NULL, "
                "session_id INTEGER, raw TEXT NOT NULL)")
    con.execute("INSERT INTO vitals_samples (ts, raw) VALUES (1.0, '{\"prox_v\": 1.5}')")
    con.commit()
    con.close()
    database = Database(path)
    try:
        cols = {r[1] for r in database._conn.execute("PRAGMA table_info(vitals_samples)")}
        assert {"pilot_high_v", "pilot_low_v", "prox_v", "relay_k1_v", "relay_k2_v"} <= cols
        assert database.backfill_diag_columns() == 1
        assert database._rows("SELECT prox_v FROM vitals_samples")[0]["prox_v"] == 1.5
    finally:
        database.close()


async def test_payload_rounded_and_gzipped(db):
    now = time.time()
    for i in range(400):
        db.insert_vitals(now - 4000 + i * 10, _raw(i), 1, 3680.123456)
    samples = db.vitals_range(now - 5000, now, max_points=50)
    for s in samples:
        for key, value in s.items():
            if key != "ts" and isinstance(value, float):
                assert value == round(value, 2), (key, value)

    app = make_app(db, EventBus(), None)
    async with TestClient(TestServer(app)) as client:
        res = await client.get(f"/api/vitals?from={now - 5000}&to={now}&points=400",
                               headers={"Accept-Encoding": "gzip"}, auto_decompress=True)
        assert res.status == 200
        assert res.headers.get("Content-Encoding") == "gzip"
        body = await res.json()
        assert len(body["samples"]) == 400
        # SSE/static untouched: the HTML page is not compressed.
        page = await client.get("/", headers={"Accept-Encoding": "gzip"})
        assert page.headers.get("Content-Encoding") is None
