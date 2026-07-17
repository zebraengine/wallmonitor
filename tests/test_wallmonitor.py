"""End-to-end tests: simulator → poller → DB → web API."""

import asyncio
import math
import time

import aiohttp
import pytest

from wallmonitor import thermal
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
    queue = bus.subscribe()
    async with aiohttp.ClientSession() as client:
        poller = Poller(cfg, db, bus, client)
        await poller.start()
        try:
            # A full simulated cycle is ~355 sim-seconds = ~6 wall seconds at 60x.
            await _wait_for(lambda: db.counts()["vitals_samples"] >= 20)
            await _wait_for(lambda: db.counts()["sessions"] >= 1)
            # Wait for a session to complete (end_ts set).
            closed = await _wait_for(
                lambda: [session for session in db.sessions_range(0, time.time() + 1) if session["end_ts"]] or None
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
    kinds = {event["kind"] for event in events}
    assert "monitor_start" in kinds
    assert "session_start" in kinds
    assert "session_end" in kinds
    assert "charging_start" in kinds
    # SSE bus delivered live messages
    assert not queue.empty()
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
        assert res.headers.get("Cache-Control") == "no-cache"

        for name in ("app.js", "style.css"):
            res = await client.get(f"/static/{name}")
            assert res.status == 200
            assert res.headers.get("Cache-Control") == "no-cache"

        status = await (await client.get("/api/status")).json()
        assert status["vitals"]["total_power_w"] == 11040.0
        assert status["wifi"]["rssi"] == -60

        sessions = await (await client.get("/api/sessions")).json()
        assert len(sessions["sessions"]) == 1
        assert sessions["sessions"][0]["energy_wh"] == 49 * 15.0

        detail = await (await client.get(f"/api/sessions/{sid}")).json()
        assert detail["session"]["id"] == sid
        assert len(detail["samples"]) == 50
        assert any(event["kind"] == "session_end" for event in detail["events"])

        vit = await (await client.get(f"/api/vitals?from={now - 400}&to={now}")).json()
        assert len(vit["samples"]) == 50

        # Downsampling kicks in when points < samples
        vit2 = await (await client.get(f"/api/vitals?from={now - 400}&to={now}&points=10")).json()
        assert len(vit2["samples"]) <= 12

        events = await (await client.get("/api/events")).json()
        assert any(event["kind"] == "session_end" for event in events["events"])

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
    assert [row["handle_temp_c"] for row in rows] == [33.0, None, 33.2]
    # Bucketed averages must ignore the sentinel, not blend it in.
    bucketed = db.vitals_range(now - 20, now, max_points=1)
    assert abs(bucketed[0]["handle_temp_c"] - 33.1) < 0.01
    # The raw JSON keeps the original value for full fidelity.
    latest = db.latest_vitals()
    assert latest["handle_temp_c"] == 33.2


async def test_session_start_backdated_from_charger_timer(db):
    # Start the poller mid-charge: the simulator reports session_s ~135 (sim
    # seconds since plug-in) at 60x, so the first session must be backdated.
    sim_runner, port = await start_simulator(speedup=60.0, start=time.time() - 175.0 / 60.0)
    cfg = Config(host=f"127.0.0.1:{port}", vitals_interval_active=0.05, vitals_interval_idle=0.05, min_interval=0.01)
    async with aiohttp.ClientSession() as client:
        poller = Poller(cfg, db, EventBus(), client)
        await poller.start()
        try:
            await _wait_for(lambda: db.counts()["sessions"] >= 1)
        finally:
            await poller.stop()
        await sim_runner.cleanup()
    session = db.sessions_range(0, time.time() + 1)[-1]
    first_sample = db._rows("SELECT MIN(ts) AS ts FROM vitals_samples WHERE session_id = ?", (session["id"],))[0]["ts"]
    assert first_sample is not None
    # session_s was ~135 at first observation, so start_ts predates it by minutes.
    assert session["start_ts"] < first_sample - 60
    events = db.events_range(0, time.time() + 1, kinds=["session_start"])
    import json as _json

    details = [_json.loads(event["detail"]) for event in events if event["detail"]]
    assert any(detail.get("backdated_s", 0) > 60 for detail in details)


async def test_not_ready_reason_change_event(db):
    sim_runner, port = await start_simulator(speedup=60.0)
    cfg = Config(host=f"127.0.0.1:{port}", vitals_interval_active=0.05, vitals_interval_idle=0.05, min_interval=0.01)
    async with aiohttp.ClientSession() as client:
        poller = Poller(cfg, db, EventBus(), client)
        await poller.start()
        try:
            # Simulator reports [1] while not charging and [] while charging,
            # so a full idle→charging transition must produce a change event.
            await _wait_for(
                lambda: db.events_range(0, time.time() + 1, kinds=["evse_not_ready_change"]) or None, timeout=20.0
            )
        finally:
            await poller.stop()
        await sim_runner.cleanup()


async def test_lifetime_api_and_diag_fields(db):
    now = time.time()
    for i in range(5):
        db.insert_lifetime(now - 400 + i * 60, {"energy_wh": 1000 + i * 500, "charge_starts": 10, "charging_time_s": 100})
    db.insert_vitals(now, {"pilot_high_v": 8.6, "pilot_low_v": -11.8, "prox_v": 1.2, "relay_k1_v": 11.9, "relay_k2_v": 0.0}, None, 0.0)

    rows = db.lifetime_range(now - 3600, now)
    assert len(rows) == 5 and rows[-1]["energy_wh"] == 3000

    vit = db.vitals_range(now - 60, now + 1)
    assert vit[0]["pilot_high_v"] == 8.6
    assert vit[0]["relay_k1_v"] == 11.9

    app = make_app(db, EventBus(), None)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        data = await (await client.get("/api/lifetime")).json()
        assert len(data["samples"]) == 5
    finally:
        await client.close()


async def test_device_alert_decoding_pipeline(db):
    # Jump the simulator to cycle 2, ~70s into charging, where it raises alert [27].
    sim_runner, port = await start_simulator(speedup=60.0, start=time.time() - 480.0 / 60.0)
    cfg = Config(host=f"127.0.0.1:{port}", vitals_interval_active=0.05, vitals_interval_idle=0.05, min_interval=0.01)
    async with aiohttp.ClientSession() as client:
        poller = Poller(cfg, db, EventBus(), client)
        await poller.start()
        try:
            await _wait_for(lambda: [alert for alert in db.active_alerts() if alert["source"] == "device"] or None, timeout=15.0)
        finally:
            await poller.stop()
        await sim_runner.cleanup()
    device_alerts = [alert for alert in db.alerts_range(0, time.time() + 1) if alert["source"] == "device"]
    assert device_alerts and device_alerts[0]["alert"] == "27"

    app = make_app(db, EventBus(), None)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        res = await client.get("/api/alert-codes")
        assert res.status == 200
        body = await res.json()
        assert "codes" in body and "categories" in body
        assert len(body["categories"]) >= 7
        # Code 40 was confirmed in the Tesla app against an active alert:
        # "High temperature detected; charging is limited".
        code40 = body["codes"]["40"]
        assert code40["verified"] is True
        assert "temperature" in code40["label"].lower()
    finally:
        await client.close()


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

    gaps = [event for event in db.events_range(0, time.time() + 1) if event["kind"] == "monitor_gap"]
    assert len(gaps) == 1
    import json as _json

    detail = _json.loads(gaps[0]["detail"])
    assert abs(detail["offline_since"] - (old + 10)) < 1
    assert detail["gap_s"] > 3000


def _seed_idle(db, t_from, t_to, ambient_c, dt=10.0):
    ts = t_from
    while ts < t_to:
        db.insert_vitals(ts, {
            "vehicle_connected": 0, "contactor_closed": 0, "vehicle_current_a": 0.0,
            "handle_temp_c": round(ambient_c + thermal.IDLE_OFFSET_C, 2),
            "pcba_temp_c": 38.0, "mcu_temp_c": 46.0,
        }, None, 0.0)
        ts += dt


def _seed_thermal_session(db, start_ts, ambient_c, tau_s=720.0, rise_ref_c=36.0,
                          amps=48.6, charge_s=1500.0, dt=10.0):
    """Idle lead-in plus a charging ramp that follows the first-order model."""
    _seed_idle(db, start_ts - 1800, start_ts, ambient_c, dt)
    sid = db.start_session(start_ts)
    t0_temp = ambient_c + thermal.IDLE_OFFSET_C
    t_inf = ambient_c + rise_ref_c * (amps / thermal.REF_CURRENT_A) ** 2
    ts = start_ts
    while ts <= start_ts + charge_s:
        temp = t_inf - (t_inf - t0_temp) * math.exp(-(ts - start_ts) / tau_s)
        db.insert_vitals(ts, {
            "vehicle_connected": 1, "contactor_closed": 1, "vehicle_current_a": amps,
            "handle_temp_c": round(temp, 3), "pcba_temp_c": 55.0, "mcu_temp_c": 50.0,
        }, sid, amps * 233.0)
        ts += dt
    db.close_session(sid, start_ts + charge_s, "vehicle_disconnected")
    return sid


async def test_thermal_fit_recovers_model(db):
    now = time.time()
    _seed_thermal_session(db, now - 3600, ambient_c=35.4)
    params = thermal.fit_history(db, now)
    assert params.fitted and params.tau_fits == 1 and params.rise_fits == 1
    assert abs(params.tau_min - 12.0) < 1.5
    assert abs(params.rise_ref_c - 36.0) < 3.0


async def test_thermal_predict_charging_trajectory(db):
    now = time.time()
    start = now - 600  # 10 minutes into a hot-day session, mid-ramp
    _seed_thermal_session(db, start, ambient_c=35.4, charge_s=600.0)
    params = thermal.ThermalParams()  # defaults; prediction should still land
    out = thermal.predict(db, now, params)
    assert out["state"] == "charging"
    forecast = out["forecast"]
    assert forecast["basis"] == "trajectory"
    assert forecast["will_trip"] is True
    # Analytic time-to-trip from the seeded model is ~8.8 min.
    assert 5.0 < forecast["minutes_to_trip"] < 13.0
    assert forecast["steady_state_c"] > thermal.TRIP_HANDLE_C
    # Seeded ambient 35.4 C implies a ~42 A cap avoids the trip entirely.
    assert forecast["suggested_max_a"] is not None
    assert abs(forecast["suggested_max_a"] - 42.0) <= 1.0


async def test_thermal_predict_cooling_after_current_cut(db):
    # Live-validated scenario: heat at full rate to near the trip point, then
    # cut current — the handle decays toward a lower equilibrium. The steady
    # state must be allowed to sit below the current handle temperature
    # (an earlier clamp floored it at handle-0.5, hiding the cool-down).
    now = time.time()
    tau_s, ambient = 720.0, 35.0
    sid = db.start_session(now - 1320)
    t_hot = ambient + 36.0 * (48.6 / 48.0) ** 2
    ts = now - 1320
    while ts < now - 420:
        temp = t_hot - (t_hot - 37.0) * math.exp(-(ts - (now - 1320)) / tau_s)
        db.insert_vitals(ts, {
            "vehicle_connected": 1, "contactor_closed": 1, "vehicle_current_a": 48.6,
            "handle_temp_c": round(temp, 3), "pcba_temp_c": 55.0, "mcu_temp_c": 50.0,
        }, sid, 11300.0)
        ts += 10.0
    peak = temp
    t_low = ambient + 36.0 * (30.0 / 48.0) ** 2
    while ts <= now:
        temp = t_low + (peak - t_low) * math.exp(-(ts - (now - 420)) / tau_s)
        db.insert_vitals(ts, {
            "vehicle_connected": 1, "contactor_closed": 1, "vehicle_current_a": 30.0,
            "handle_temp_c": round(temp, 3), "pcba_temp_c": 55.0, "mcu_temp_c": 50.0,
        }, sid, 7000.0)
        ts += 10.0

    out = thermal.predict(db, now, thermal.ThermalParams())
    assert out["state"] == "charging"
    forecast = out["forecast"]
    assert forecast["basis"] == "trajectory"
    assert forecast["will_trip"] is False
    # Cooling toward ~49 C while the handle still reads ~56 C.
    assert forecast["steady_state_c"] < out["handle_c"] - 3.0
    assert abs(forecast["steady_state_c"] - t_low) < 3.0


async def test_thermal_predict_current_step_in_back_to_back_session(db):
    # Field-observed gap: a session that starts back-to-back (no idle stretch
    # to read ambient from) and then steps its charge current. The step resets
    # the live trajectory window, pre-session ambient is unavailable, and the
    # old code went dark ("insufficient") minutes into an active session. The
    # steady run still in the buffer implies the ambient instead.
    now = time.time()
    tau_s, ambient, rise = 720.0, 30.0, 36.0
    sid = db.start_session(now - 700)
    t_inf_hi = ambient + rise * (40.0 / 48.0) ** 2
    ts = now - 700
    while ts < now - 60:  # ~10.5 min steady at 40 A
        temp = t_inf_hi - (t_inf_hi - 32.0) * math.exp(-(ts - (now - 700)) / tau_s)
        db.insert_vitals(ts, {
            "vehicle_connected": 1, "contactor_closed": 1, "vehicle_current_a": 40.0,
            "handle_temp_c": round(temp, 3), "pcba_temp_c": 55.0, "mcu_temp_c": 50.0,
        }, sid, 9300.0)
        ts += 10.0
    peak = temp
    t_inf_lo = ambient + rise * (32.0 / 48.0) ** 2
    while ts <= now:  # only ~60 s at the new 32 A — too short for a live window
        temp = t_inf_lo + (peak - t_inf_lo) * math.exp(-(ts - (now - 60)) / tau_s)
        db.insert_vitals(ts, {
            "vehicle_connected": 1, "contactor_closed": 1, "vehicle_current_a": 32.0,
            "handle_temp_c": round(temp, 3), "pcba_temp_c": 55.0, "mcu_temp_c": 50.0,
        }, sid, 7400.0)
        ts += 10.0

    out = thermal.predict(db, now, thermal.ThermalParams())
    assert out["state"] == "charging"
    forecast = out["forecast"]
    assert forecast["basis"] == "model"
    assert forecast["ambient_source"] == "recent_trajectory"
    # Steady state rescaled to the new 32 A: ambient + 36*(32/48)^2 = 46 C.
    assert abs(forecast["steady_state_c"] - t_inf_lo) < 2.5
    assert forecast["will_trip"] is False


async def test_thermal_predict_insufficient_reports_why(db):
    # With no usable window, no pre-session idle, and no earlier steady run,
    # the forecast is honestly "insufficient" — but distinguishes a session
    # that truly just started from one whose current just changed.
    now = time.time()
    sid = db.start_session(now - 50)
    ts = now - 50
    while ts <= now:
        db.insert_vitals(ts, {
            "vehicle_connected": 1, "contactor_closed": 1, "vehicle_current_a": 48.0,
            "handle_temp_c": 33.0, "pcba_temp_c": 55.0, "mcu_temp_c": 50.0,
        }, sid, 11200.0)
        ts += 10.0
    out = thermal.predict(db, now, thermal.ThermalParams())
    assert out["state"] == "charging"
    assert out["forecast"] == {"basis": "insufficient", "will_trip": None, "reason": "warming_up"}

    stepped = Database(":memory:")
    try:
        # 100 s at 40 A, 100 s at 46 A, 40 s at 32 A: every run too short for
        # a window, but the session is past its opening ramp — the honest
        # story is "current changed", not "just started".
        sid = stepped.start_session(now - 240)
        ts = now - 240
        while ts <= now:
            amps = 40.0 if ts < now - 140 else 46.0 if ts < now - 40 else 32.0
            stepped.insert_vitals(ts, {
                "vehicle_connected": 1, "contactor_closed": 1, "vehicle_current_a": amps,
                "handle_temp_c": 33.0, "pcba_temp_c": 55.0, "mcu_temp_c": 50.0,
            }, sid, amps * 233.0)
            ts += 10.0
        out = thermal.predict(stepped, now, thermal.ThermalParams())
        assert out["forecast"] == {"basis": "insufficient", "will_trip": None, "reason": "current_changed"}
    finally:
        stepped.close()


async def test_thermal_minutes_to_trip_ordering():
    # Settling below the trip point wins over "currently above it": a handle
    # at 66 C cooling toward 47 C is recovering from a derate, not tripping.
    assert thermal._minutes_to_trip(66.0, 47.5, 12.0) is None
    # Above the trip point and staying there: already tripped.
    assert thermal._minutes_to_trip(66.0, 70.0, 12.0) == 0.0


async def test_thermal_predict_idle_forecast(db):
    now = time.time()
    _seed_idle(db, now - 1200, now, ambient_c=35.4)
    params = thermal.ThermalParams()
    out = thermal.predict(db, now, params)
    assert out["state"] == "idle"
    assert abs(out["ambient_c"] - 35.4) < 0.3
    assert out["ambient_stable"] is True
    forecast = out["forecast"]
    assert forecast["will_trip"] is True  # 35.4 + 36 rise is well past the 65 C trip
    assert 12.0 < forecast["minutes_to_trip"] < 30.0
    assert abs(forecast["safe_ambient_max_c"] - 29.0) < 0.1
    assert forecast["suggested_max_a"] == 42.0  # floor(48*sqrt((63-35.4)/36))

    # A cool garage never trips at full rate, so there is no cap to suggest.
    cool = Database(":memory:")
    try:
        _seed_idle(cool, now - 1200, now, ambient_c=20.0)
        out = thermal.predict(cool, now, params)
        assert out["forecast"]["will_trip"] is False
        assert out["forecast"]["suggested_max_a"] is None
    finally:
        cool.close()


async def test_thermal_fit_survives_ramp_and_midsession_derate(db):
    # Regression: real sessions start with a current ramp (worsened by bucket
    # averaging) and can derate to 50% midway. A whole-session median current
    # put the full-rate ramp outside the steady band and produced zero fits.
    now = time.time()
    start = now - 4 * 3600
    _seed_idle(db, start - 1800, start, ambient_c=35.4)
    sid = db.start_session(start)
    tau_s, rise, amps = 720.0, 36.0, 48.6
    t_inf = 35.4 + rise * (amps / 48.0) ** 2
    ts, temp0 = start, 37.4
    while ts <= start + 3 * 3600:
        into = ts - start
        if into < 60:
            current = amps * into / 60.0  # ramp-up
        elif into < 1200:
            current = amps  # full rate for 20 min...
        else:
            current = amps / 2  # ...then derated for hours (most samples)
        temp = t_inf - (t_inf - temp0) * math.exp(-into / tau_s) if into < 1200 else 60.0
        db.insert_vitals(ts, {
            "vehicle_connected": 1, "contactor_closed": 1, "vehicle_current_a": round(current, 2),
            "handle_temp_c": round(temp, 3), "pcba_temp_c": 55.0, "mcu_temp_c": 50.0,
        }, sid, current * 233.0)
        ts += 10.0
    db.close_session(sid, start + 3 * 3600, "vehicle_disconnected")

    fits = thermal.fit_sessions(db, now)
    assert len(fits) == 1, "the full-rate ramp before the derate must fit"
    assert abs(fits[0]["tau_min"] - 12.0) < 1.5
    assert abs(fits[0]["current_a"] - amps) < 1.0
    assert fits[0]["rise_ref_c"] is not None and abs(fits[0]["rise_ref_c"] - rise) < 3.0


async def test_thermal_drift_detection(db):
    now = time.time()
    # Four healthy sessions, then three running hotter at the same current —
    # the signature of added resistance in the current path.
    rises = [36.0, 36.5, 35.8, 36.2, 42.0, 41.5, 42.3]
    for i, rise in enumerate(rises):
        _seed_thermal_session(db, now - (len(rises) - i) * 7200, ambient_c=25.0, rise_ref_c=rise)
    fits = thermal.fit_sessions(db, now)
    assert len(fits) == len(rises)
    drift = thermal.detect_drift(fits)
    assert drift is not None and drift["drifting"] is True
    assert 4.0 < drift["delta_c"] < 8.0

    # Prediction params follow the median (this is why drift needs its own watch).
    params = thermal.fit_history(db, now, fits=fits)
    assert params.fitted

    # Too little history: no verdict either way.
    assert thermal.detect_drift(fits[:4]) is None


async def test_thermal_drift_poller_alert(db):
    now = time.time()
    rises = [36.0, 36.5, 35.8, 36.2, 42.0, 41.5, 42.3]
    for i, rise in enumerate(rises):
        _seed_thermal_session(db, now - (len(rises) - i) * 7200, ambient_c=25.0, rise_ref_c=rise)
    cfg = Config(host="127.0.0.1:1")
    bus = EventBus()
    async with aiohttp.ClientSession() as client:
        poller = Poller(cfg, db, bus, client)
        await poller._check_thermal_drift(now)
    alerts = db.active_alerts()
    assert any(alert["alert"] == thermal.DRIFT_ALERT and alert["source"] == "monitor" for alert in alerts)
    events = db.events_range(now - 1, now + 1)
    assert any(event["kind"] == "thermal_drift" for event in events)


async def test_thermal_suggest_max_current():
    params = thermal.ThermalParams()
    assert thermal.suggest_max_current(35.4, params) == 42.0
    assert thermal.suggest_max_current(45.0, params) == 33.0
    assert thermal.suggest_max_current(20.0, params) is None  # full rate already safe
    assert thermal.suggest_max_current(64.0, params) is None  # no rate avoids the trip


async def test_thermal_api_endpoint(db):
    app = make_app(db, EventBus(), None)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        res = await client.get("/api/thermal")
        assert res.status == 200
        body = await res.json()
        # Empty DB: defaults reported honestly, nothing to forecast.
        assert body["state"] == "no_data"
        assert body["model"]["fitted"] is False
        assert body["model"]["tau_min"] == thermal.DEFAULT_TAU_MIN
        assert body["model"]["trip_c"] == thermal.TRIP_HANDLE_C
        assert body["drift"] is None and body["session_fits"] == []

        _seed_idle(db, time.time() - 900, time.time(), ambient_c=22.0)
        res = await client.get("/api/thermal?refit=1")
        body = await res.json()
        assert body["state"] == "idle"
        assert body["forecast"]["will_trip"] is False
    finally:
        await client.close()


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
    assert any(alert["alert"] == "Wall Connector unreachable" for alert in alerts)
