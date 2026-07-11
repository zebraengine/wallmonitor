"""End-to-end tests: simulator → poller → DB → web API."""

import asyncio
import time

import aiohttp
import pytest

from wallmonitor.config import Config
from wallmonitor.db import Database
from wallmonitor.poller import EventBus, Poller
from wallmonitor.simulator import start_simulator
from wallmonitor.web import make_app

from aiohttp.test_utils import TestClient, TestServer


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    yield database
    database.close()


async def _wait_for(predicate, timeout=15.0, interval=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = predicate()
        if result:
            return result
        await asyncio.sleep(interval)
    raise AssertionError("condition not met within timeout")


async def test_poller_records_and_sessions(db):
    # Speed the simulator up so a full plug-in→charge→unplug cycle fits in seconds.
    sim_runner, port = await start_simulator(speedup=60.0)
    cfg = Config(
        host=f"127.0.0.1:{port}",
        db_path=":memory:",
        vitals_interval_active=0.05,
        vitals_interval_idle=0.05,
        wifi_interval=0.5,
        lifetime_interval=0.5,
        version_interval=5.0,
        min_interval=0.01,
    )
    bus = EventBus()
    q = bus.subscribe()
    async with aiohttp.ClientSession() as client:
        poller = Poller(cfg, db, bus, client)
        await poller.start()
        try:
            # A full simulated cycle is ~355 sim-seconds = ~6 wall seconds at 60x.
            await _wait_for(lambda: db.counts()["vitals_samples"] >= 20)
            await _wait_for(lambda: db.counts()["sessions"] >= 1)
            # Wait for a session to complete (end_ts set).
            closed = await _wait_for(
                lambda: [s for s in db.sessions_range(0, time.time() + 1) if s["end_ts"]] or None
            )
            session = closed[0]
            assert session["energy_wh"] and session["energy_wh"] > 0
            assert session["max_power_w"] and session["max_power_w"] > 1000
            assert session["sample_count"] > 5
            assert session["end_reason"] == "vehicle_disconnected"
        finally:
            await poller.stop()
        await sim_runner.cleanup()

    # Events recorded with the same clock
    events = db.events_range(0, time.time() + 1)
    kinds = {e["kind"] for e in events}
    assert "monitor_start" in kinds
    assert "session_start" in kinds
    assert "session_end" in kinds
    assert "charging_start" in kinds
    # SSE bus delivered live messages
    assert not q.empty()
    # Raw fidelity: full JSON retained
    latest = db.latest_vitals()
    assert latest is not None and latest["raw"].startswith("{")
    # Wifi and lifetime got sampled too
    assert db.counts()["wifi_samples"] >= 1
    assert db.counts()["lifetime_samples"] >= 1
    assert db.latest_version() is not None


async def test_alert_lifecycle(db):
    now = time.time()
    _, new = db.raise_alert(now, "Alert_Test", "device")
    assert new
    _, again = db.raise_alert(now + 1, "Alert_Test", "device")
    assert not again
    assert len(db.active_alerts()) == 1
    assert db.clear_alert(now + 2, "Alert_Test", "device")
    assert db.active_alerts() == []
    history = db.alerts_range(now - 1, now + 3)
    assert len(history) == 1
    assert history[0]["cleared_ts"] is not None


async def test_web_api(db):
    now = time.time()
    sid = db.start_session(now - 300)
    for i in range(50):
        ts = now - 300 + i * 6
        db.insert_vitals(
            ts,
            {
                "vehicle_connected": True,
                "contactor_closed": True,
                "session_s": i * 6,
                "session_energy_wh": i * 15.0,
                "grid_v": 230.0,
                "grid_hz": 50.0,
                "vehicle_current_a": 16.0,
                "currentA_a": 16.0,
                "currentB_a": 16.0,
                "currentC_a": 16.0,
                "voltageA_v": 230.0,
                "voltageB_v": 230.0,
                "voltageC_v": 230.0,
                "pcba_temp_c": 25.0,
                "handle_temp_c": 28.0,
                "mcu_temp_c": 30.0,
                "evse_state": 9,
                "config_status": 5,
                "uptime_s": 1000 + i,
                "current_alerts": [],
            },
            sid,
            11040.0,
        )
    db.close_session(sid, now, "vehicle_disconnected")
    db.insert_wifi(now, {"wifi_connected": True, "internet": True, "wifi_rssi": -60, "wifi_snr": 25, "wifi_signal_strength": 80, "wifi_ssid": "Test", "wifi_infra_ip": "10.0.0.2", "wifi_mac": "AA"})
    db.add_event(now, "session_end", {"session_id": sid})

    app = make_app(db, EventBus(), None)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        res = await client.get("/")
        assert res.status == 200
        assert "Wall Connector Monitor" in await res.text()

        for name in ("app.js", "style.css"):
            res = await client.get(f"/static/{name}")
            assert res.status == 200

        status = await (await client.get("/api/status")).json()
        assert status["vitals"]["total_power_w"] == 11040.0
        assert status["wifi"]["rssi"] == -60

        sessions = await (await client.get("/api/sessions")).json()
        assert len(sessions["sessions"]) == 1
        assert sessions["sessions"][0]["energy_wh"] == 49 * 15.0

        detail = await (await client.get(f"/api/sessions/{sid}")).json()
        assert detail["session"]["id"] == sid
        assert len(detail["samples"]) == 50
        assert any(e["kind"] == "session_end" for e in detail["events"])

        vit = await (await client.get(f"/api/vitals?from={now - 400}&to={now}")).json()
        assert len(vit["samples"]) == 50

        # Downsampling kicks in when points < samples
        vit2 = await (await client.get(f"/api/vitals?from={now - 400}&to={now}&points=10")).json()
        assert len(vit2["samples"]) <= 12

        events = await (await client.get("/api/events")).json()
        assert any(e["kind"] == "session_end" for e in events["events"])

        alerts = await (await client.get("/api/alerts")).json()
        assert alerts["active"] == []

        missing = await client.get("/api/sessions/9999")
        assert missing.status == 404
    finally:
        await client.close()


async def test_temp_sentinel_excluded_from_queries(db):
    now = time.time()
    for i, handle in enumerate([33.0, 255.0, 33.2]):
        db.insert_vitals(now - 10 + i, {"handle_temp_c": handle, "pcba_temp_c": 35.0, "mcu_temp_c": 42.0}, None, 0.0)
    rows = db.vitals_range(now - 20, now)
    assert [r["handle_temp_c"] for r in rows] == [33.0, None, 33.2]
    # Bucketed averages must ignore the sentinel, not blend it in.
    bucketed = db.vitals_range(now - 20, now, max_points=1)
    assert abs(bucketed[0]["handle_temp_c"] - 33.1) < 0.01
    # The raw JSON keeps the original value for full fidelity.
    latest = db.latest_vitals()
    assert latest["handle_temp_c"] == 33.2


async def test_monitor_gap_event_on_restart(db):
    # Simulate a previous run that stopped long ago, then a restart.
    old = time.time() - 3600
    db.add_event(old, "monitor_start", None)
    db.insert_vitals(old + 10, {"vehicle_connected": False, "uptime_s": 1}, None, 0.0)

    sim_runner, port = await start_simulator()
    cfg = Config(host=f"127.0.0.1:{port}", vitals_interval_idle=0.05, min_interval=0.01)
    async with aiohttp.ClientSession() as client:
        poller = Poller(cfg, db, EventBus(), client)
        await poller.start()
        try:
            await _wait_for(lambda: db.counts()["vitals_samples"] >= 2)
        finally:
            await poller.stop()
        await sim_runner.cleanup()

    gaps = [e for e in db.events_range(0, time.time() + 1) if e["kind"] == "monitor_gap"]
    assert len(gaps) == 1
    import json as _json

    detail = _json.loads(gaps[0]["detail"])
    assert abs(detail["offline_since"] - (old + 10)) < 1
    assert detail["gap_s"] > 3000


async def test_backoff_on_unreachable_host(db, unused_tcp_port):
    cfg = Config(
        host=f"127.0.0.1:{unused_tcp_port}",
        vitals_interval_active=0.05,
        vitals_interval_idle=0.05,
        min_interval=0.01,
        request_timeout=0.3,
        backoff_max=1.0,
    )
    bus = EventBus()
    async with aiohttp.ClientSession() as client:
        poller = Poller(cfg, db, bus, client)
        await poller.start()
        try:
            await _wait_for(lambda: poller.status()["offline"], timeout=10.0)
        finally:
            await poller.stop()
    alerts = db.active_alerts()
    assert any(a["alert"] == "Wall Connector unreachable" for a in alerts)
