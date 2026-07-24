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
                          amps=48.6, charge_s=1500.0, dt=10.0,
                          ambient_end_c=None, cooldown_s=0.0):
    """Idle lead-in plus a charging ramp that follows the first-order model.

    With ambient_end_c set, ambient drifts linearly across the charge (the
    heat-wave / overnight-cooling scenario) and the ramp comes from
    integrating the lag ODE against the moving ambient. cooldown_s appends
    post-session idle decay samples — the tail the fitter reads the load
    window's end ambient from.
    """
    _seed_idle(db, start_ts - 1800, start_ts, ambient_c, dt)
    sid = db.start_session(start_ts)
    t0_temp = ambient_c + thermal.IDLE_OFFSET_C
    rise_at = rise_ref_c * (amps / thermal.REF_CURRENT_A) ** 2
    temp = t0_temp
    ts = start_ts
    while ts <= start_ts + charge_s:
        if ambient_end_c is None:
            t_inf = ambient_c + rise_at
            temp = t_inf - (t_inf - t0_temp) * math.exp(-(ts - start_ts) / tau_s)
        db.insert_vitals(ts, {
            "vehicle_connected": 1, "contactor_closed": 1, "vehicle_current_a": amps,
            "handle_temp_c": round(temp, 3), "pcba_temp_c": 55.0, "mcu_temp_c": 50.0,
        }, sid, amps * 233.0)
        if ambient_end_c is not None:
            ambient_now = ambient_c + (ambient_end_c - ambient_c) * (ts - start_ts) / charge_s
            temp += dt * ((ambient_now + rise_at - temp) / tau_s)
        ts += dt
    db.close_session(sid, start_ts + charge_s, "vehicle_disconnected")
    ambient_final = ambient_end_c if ambient_end_c is not None else ambient_c
    while ts <= start_ts + charge_s + cooldown_s:
        temp += dt * ((ambient_final + thermal.IDLE_OFFSET_C - temp) / tau_s)
        db.insert_vitals(ts, {
            "vehicle_connected": 0, "contactor_closed": 0, "vehicle_current_a": 0.0,
            "handle_temp_c": round(temp, 3), "pcba_temp_c": 45.0, "mcu_temp_c": 48.0,
        }, None, 0.0)
        ts += dt
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


async def test_thermal_fit_covers_late_charging_segments(db):
    # A session shaped like real overnight use: a plug-in burst too short to
    # fit, hours of connected idle, then distinct charging segments (vehicle
    # top-off, preconditioning, or a charging schedule) long after session
    # start. The fitter must find each qualifying ramp where it actually is —
    # not only inside the session's first 45 minutes.
    now = time.time()
    start = now - 11 * 3600
    ambient, tau_s, rise, amps = 23.0, 720.0, 36.0, 48.6
    _seed_idle(db, start - 1800, start, ambient)
    sid = db.start_session(start)
    t0_temp = ambient + thermal.IDLE_OFFSET_C
    t_inf = ambient + rise * (amps / thermal.REF_CURRENT_A) ** 2

    def charge(seg_start, seg_len):
        ts = seg_start
        while ts <= seg_start + seg_len:
            temp = t_inf - (t_inf - t0_temp) * math.exp(-(ts - seg_start) / tau_s)
            db.insert_vitals(ts, {
                "vehicle_connected": 1, "contactor_closed": 1, "vehicle_current_a": amps,
                "handle_temp_c": round(temp, 3), "pcba_temp_c": 55.0, "mcu_temp_c": 50.0,
            }, sid, amps * 233.0)
            ts += 10.0
        return ts

    def idle(t_from, t_to):
        ts = t_from
        while ts < t_to:
            db.insert_vitals(ts, {
                "vehicle_connected": 1, "contactor_closed": 0, "vehicle_current_a": 0.0,
                "handle_temp_c": round(ambient + thermal.IDLE_OFFSET_C, 2),
                "pcba_temp_c": 38.0, "mcu_temp_c": 46.0,
            }, sid, 0.0)
            ts += 10.0

    ts = charge(start, 300.0)               # plug-in burst, below MIN_SEGMENT_S
    idle(ts, start + 4 * 3600)
    seg_a = start + 4 * 3600
    ts = charge(seg_a, 1500.0)              # first qualifying ramp, 4 h in
    idle(ts, start + 8 * 3600)
    seg_b = start + 8 * 3600
    ts = charge(seg_b, 1500.0)              # second qualifying ramp, 8 h in
    db.close_session(sid, ts, "vehicle_disconnected")

    fits = thermal.fit_sessions(db, now)
    assert len(fits) == 2, "both late ramps fit; the short burst does not"
    assert all(fit["session_id"] == sid for fit in fits)
    assert abs(fits[0]["start_ts"] - seg_a) < 120
    assert abs(fits[1]["start_ts"] - seg_b) < 120
    for fit in fits:
        assert fit["rise_ref_c"] is not None and abs(fit["rise_ref_c"] - rise) < 3.0
        assert abs(fit["current_a"] - amps) < 1.0


async def test_thermal_fit_cooldown_tail_ambient(db):
    # Stop/resume: the second ramp starts 8 minutes after the first charge
    # stopped, on a handle still ~15 °C above idle — no flat idle window
    # exists, so pre-idle ambient fails. The cool-down tail (exponential
    # decay toward ambient + idle offset at the shared tau) must supply
    # ambient instead, so the hardest-working segments still feed the
    # degradation watch.
    now = time.time()
    start = now - 3 * 3600
    ambient, tau_s, rise, amps = 25.0, 720.0, 36.0, 48.6
    idle_temp = ambient + thermal.IDLE_OFFSET_C
    t_inf = ambient + rise * (amps / thermal.REF_CURRENT_A) ** 2
    _seed_idle(db, start - 1800, start, ambient)
    sid = db.start_session(start)

    def charge(seg_start, seg_len, temp0):
        ts = seg_start
        while ts <= seg_start + seg_len:
            temp = t_inf - (t_inf - temp0) * math.exp(-(ts - seg_start) / tau_s)
            db.insert_vitals(ts, {
                "vehicle_connected": 1, "contactor_closed": 1, "vehicle_current_a": amps,
                "handle_temp_c": round(temp, 3), "pcba_temp_c": 55.0, "mcu_temp_c": 50.0,
            }, sid, amps * 233.0)
            ts += 10.0
        return ts, temp

    def cooldown(t_from, t_to, temp0):
        ts, temp = t_from, temp0
        while ts < t_to:
            temp = idle_temp + (temp0 - idle_temp) * math.exp(-(ts - t_from) / tau_s)
            db.insert_vitals(ts, {
                "vehicle_connected": 1, "contactor_closed": 0, "vehicle_current_a": 0.0,
                "handle_temp_c": round(temp, 3), "pcba_temp_c": 40.0, "mcu_temp_c": 46.0,
            }, sid, 0.0)
            ts += 10.0
        return temp

    ts, end_temp = charge(start, 1500.0, idle_temp)      # first ramp, from idle temp
    resume_at = ts + 480.0                                # 8-min gap: splits segments,
    hot_temp = cooldown(ts, resume_at, end_temp)          # handle still hot at resume
    assert hot_temp - idle_temp > 10.0, "test setup: handle must still be hot at resume"
    ts, _ = charge(resume_at, 1500.0, hot_temp)           # resumed ramp from hot start
    db.close_session(sid, ts, "vehicle_disconnected")

    fits = thermal.fit_sessions(db, now)
    assert len(fits) == 2
    first, second = fits
    assert first["ambient_source"] == "pre_idle"
    assert abs(first["rise_ref_c"] - rise) < 3.0
    # The resumed segment previously lost its rise fit entirely; now the
    # cool-down tail supplies ambient and the fit lands on the seeded rise.
    assert second["ambient_source"] == "cooldown_tail"
    assert second["rise_ref_c"] is not None
    assert abs(second["rise_ref_c"] - rise) < 3.0


async def test_thermal_fit_debiases_in_window_ambient_drift(db):
    # The heat-wave failure mode: the garage warms during the charge, a point
    # ambient read at the window start goes stale, and the fitted rise
    # absorbs the weather — indistinguishable from connector resistance.
    # Bracketing reads the end ambient from the charge's own cool-down tail
    # and de-trends the fit; both drift directions must recover the true
    # rise, and the fit must record how much the ambient moved.
    now = time.time()
    warming = _seed_thermal_session(db, now - 4 * 7200, ambient_c=30.0, ambient_end_c=33.0,
                                    charge_s=1800.0, cooldown_s=1500.0)
    cooling = _seed_thermal_session(db, now - 2 * 7200, ambient_c=28.0, ambient_end_c=26.0,
                                    charge_s=1800.0, cooldown_s=1500.0)
    fits = thermal.fit_sessions(db, now)
    assert [fit["session_id"] for fit in fits] == [warming, cooling]
    warm_fit, cool_fit = fits
    assert warm_fit["ambient_drift_c"] is not None and 2.0 < warm_fit["ambient_drift_c"] < 4.0
    assert cool_fit["ambient_drift_c"] is not None and -3.0 < cool_fit["ambient_drift_c"] < -1.0
    # Without the bracket the warming fit reads ~+3 °C hot and the cooling
    # fit ~2 °C cold; de-trended, both land on the seeded 36 °C.
    assert abs(warm_fit["rise_ref_c"] - 36.0) < 1.5
    assert abs(cool_fit["rise_ref_c"] - 36.0) < 1.5
    # And a start-only fit (no cool-down tail recorded) still works the old
    # way, flagged as such.
    _seed_thermal_session(db, now - 7200, ambient_c=27.0)
    fits = thermal.fit_sessions(db, now)
    assert fits[-1]["ambient_drift_c"] is None
    assert fits[-1]["ambient_end_c"] is None
    assert fits[-1]["rise_ref_c"] is not None


async def test_thermal_drift_follows_current_change(db):
    # The user caps the vehicle at a new charge current (e.g. 48 A -> 40 A to
    # stay under the derate on hot days). The old all-history median kept
    # "typical" at 48 A forever: every new session was off-current, the drift
    # verdict froze on stale data, and an active alert could never clear or
    # re-confirm. Typical must follow the install's recent operating point.
    now = time.time()
    for i, rise in enumerate([36.0, 36.5, 35.8, 36.2, 36.1, 36.4]):
        _seed_thermal_session(db, now - (14 - i) * 7200, ambient_c=25.0, rise_ref_c=rise)
    for i, rise in enumerate([36.3, 36.0, 36.2]):
        _seed_thermal_session(db, now - (7 - i) * 7200, ambient_c=25.0, rise_ref_c=rise, amps=40.6)
    fits = thermal.fit_sessions(db, now)
    drift = thermal.detect_drift(fits)
    # Three 40 A fits and no 40 A baseline yet: the honest "can't judge yet"
    # (which clears a stale alert) rather than a verdict frozen at 48 A.
    assert drift is None
    # More 40 A history accumulates — the watch re-arms at the new current
    # and a genuine same-current increase is still flagged.
    for i, rise in enumerate([36.1, 42.0, 41.8, 42.3]):
        _seed_thermal_session(db, now - (4 - i) * 7200, ambient_c=25.0, rise_ref_c=rise, amps=40.6)
    fits = thermal.fit_sessions(db, now)
    drift = thermal.detect_drift(fits)
    assert drift is not None and drift["drifting"] is True
    assert abs(drift["typical_current_a"] - 40.6) < 0.1
    assert drift["off_current_n"] == 6  # the old 48 A history sits out


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


async def test_thermal_drift_ignores_off_current_sessions(db):
    now = time.time()
    # Six healthy sessions at the usual 48.6 A, then one at a reduced 40.6 A
    # whose fitted rise normalizes high — the (48/40.6)^2 extrapolation
    # amplifying ordinary error, not a hardware change. It must be excluded
    # from the comparison rather than allowed to swing the recent median.
    for i, rise in enumerate([36.0, 36.5, 35.8, 36.2, 36.1, 36.4]):
        _seed_thermal_session(db, now - (8 - i) * 7200, ambient_c=25.0, rise_ref_c=rise)
    _seed_thermal_session(db, now - 2 * 7200, ambient_c=25.0, rise_ref_c=42.5, amps=40.6)
    fits = thermal.fit_sessions(db, now)
    assert len(fits) == 7
    drift = thermal.detect_drift(fits)
    assert drift is not None and drift["drifting"] is False
    assert drift["off_current_n"] == 1
    assert abs(drift["typical_current_a"] - 48.6) < 1.0

    # A genuine same-current increase must still be flagged even with the
    # off-current session in the mix.
    _seed_thermal_session(db, now - 7200, ambient_c=25.0, rise_ref_c=42.0)
    _seed_thermal_session(db, now - 3600, ambient_c=25.0, rise_ref_c=41.8)
    fits = thermal.fit_sessions(db, now)
    drift = thermal.detect_drift(fits)
    assert drift is not None and drift["drifting"] is True
    assert drift["off_current_n"] == 1


async def test_thermal_drift_confidence_interval(db):
    # The verdict must carry its own uncertainty. A tight cluster on both
    # sides of a big delta is a confirmed finding; the same delta built on a
    # scattered baseline is a lead — drifting (tripwire) but not confident.
    now = time.time()
    for i, rise in enumerate([36.0, 36.5, 35.8, 36.2, 42.0, 41.5, 42.3]):
        _seed_thermal_session(db, now - (7 - i) * 7200, ambient_c=25.0, rise_ref_c=rise)
    drift = thermal.detect_drift(thermal.fit_sessions(db, now))
    assert drift["drifting"] is True and drift["confident"] is True
    ci_lo, ci_hi = drift["delta_ci95_c"]
    assert ci_lo < drift["delta_c"] < ci_hi and ci_lo > 0
    assert drift["baseline_mad_c"] < 1.0 and drift["recent_mad_c"] < 1.0


async def test_thermal_drift_wide_scatter_is_not_confident(db):
    now = time.time()
    # Baseline scattered over 6 °C: the recent increase clears the tripwire
    # but the interval straddles zero — the UI and notification must say
    # "lead, not conviction" instead of presenting the delta as exact.
    for i, rise in enumerate([30.0, 36.0, 31.0, 35.8, 36.2, 42.0, 36.6]):
        _seed_thermal_session(db, now - (7 - i) * 7200, ambient_c=25.0, rise_ref_c=rise)
    drift = thermal.detect_drift(thermal.fit_sessions(db, now))
    assert drift is not None and drift["drifting"] is True
    assert drift["confident"] is False
    assert drift["delta_ci95_c"][0] < 0


async def test_thermal_drift_pools_bracketed_cross_current_fits(db):
    # The vehicle gets capped 48.6 -> 40.6 A. Same-current-only comparison
    # would go dark (no 40 A baseline); ambient-bracketed fits are clean
    # enough under the I^2 normalization to keep the 48 A baseline judging
    # the new 40 A charges from the wider pooling band.
    now = time.time()
    for i, rise in enumerate([36.0, 36.5, 35.8, 36.2, 36.1, 36.4]):
        _seed_thermal_session(db, now - (12 - i) * 7200, ambient_c=25.0, rise_ref_c=rise,
                              cooldown_s=900.0, ambient_end_c=25.0)
    for i, rise in enumerate([36.3, 36.0, 36.2]):
        _seed_thermal_session(db, now - (3 - i) * 7200, ambient_c=25.0, rise_ref_c=rise,
                              amps=40.6, cooldown_s=900.0, ambient_end_c=25.0)
    fits = thermal.fit_sessions(db, now)
    assert all(fit["ambient_drift_c"] is not None for fit in fits)
    drift = thermal.detect_drift(fits)
    assert drift is not None, "bracketed 48 A baseline must keep judging 40 A charges"
    assert drift["drifting"] is False
    assert abs(drift["typical_current_a"] - 40.6) < 0.1
    assert drift["cross_current_n"] == 6  # the 48 A baseline, pooled in
    # Un-bracketed off-current fits must still be excluded (the old rule).
    # (Seeded clear of session 9's cool-down tail so neither ambient read is
    # contaminated by interleaved samples.)
    _seed_thermal_session(db, now - 1800, ambient_c=25.0, rise_ref_c=42.5, amps=32.0)
    drift = thermal.detect_drift(thermal.fit_sessions(db, now))
    assert drift is not None and drift["off_current_n"] >= 1


async def test_thermal_baseline_anchor(db):
    now = time.time()
    rises = [36.0, 36.5, 35.8, 36.2, 42.0, 41.5, 42.3]
    for i, rise in enumerate(rises):
        _seed_thermal_session(db, now - (len(rises) - i) * 7200, ambient_c=25.0, rise_ref_c=rise)
    fits = thermal.fit_sessions(db, now)
    assert thermal.detect_drift(fits)["drifting"] is True
    # Anchoring after the old baseline (hardware inspected, verified) leaves
    # only the three newest fits: too thin to judge, verdict honestly None.
    anchor = now - 4 * 7200
    assert thermal.detect_drift(fits, anchor_ts=anchor) is None
    # The API round-trip: set, read back through /api/thermal, clear.
    app = make_app(db, EventBus(), None)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/thermal/baseline-anchor", json={"ts": anchor})
        assert resp.status == 200 and (await resp.json())["baseline_anchor_ts"] == anchor
        data = await (await client.get("/api/thermal?refit=1")).json()
        assert data["baseline_anchor_ts"] == anchor
        assert data["drift"] is None
        resp = await client.delete("/api/thermal/baseline-anchor")
        assert resp.status == 200 and (await resp.json())["baseline_anchor_ts"] is None
        data = await (await client.get("/api/thermal?refit=1")).json()
        assert data["baseline_anchor_ts"] is None
        assert data["drift"]["drifting"] is True
    events = db.events_range(now - 10, time.time() + 10)
    kinds = [event["kind"] for event in events]
    assert "baseline_anchor_set" in kinds and "baseline_anchor_cleared" in kinds


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


async def test_derate_forecast_warns_then_clears(db):
    # A charging handle on a trajectory toward 65 °C inside the warning
    # horizon must raise the actionable alert with a suggested current cap,
    # and the warning must clear when charging stops.
    now = time.time()
    ambient, tau_s, amps = 33.0, 720.0, 48.0
    t_inf = ambient + thermal.DEFAULT_RISE_REF_C  # 69 °C steady state at 48 A
    t0_temp = ambient + thermal.IDLE_OFFSET_C
    start = now - 810.0
    ts = start
    while ts <= now:
        temp = t_inf - (t_inf - t0_temp) * math.exp(-(ts - start) / tau_s)
        db.insert_vitals(ts, {
            "vehicle_connected": 1, "contactor_closed": 1, "vehicle_current_a": amps,
            "handle_temp_c": round(temp, 3), "pcba_temp_c": 55.0, "mcu_temp_c": 50.0,
        }, None, amps * 233.0)
        ts += 10.0

    cfg = Config(host="127.0.0.1:1")
    bus = EventBus()
    async with aiohttp.ClientSession() as client:
        poller = Poller(cfg, db, bus, client)
        await poller._check_derate_forecast(now, {"contactor_closed": 1, "vehicle_current_a": amps})
        alerts = db.active_alerts()
        assert any(a["alert"] == thermal.DERATE_ALERT and a["source"] == "monitor" for a in alerts)
        events = db.events_range(now - 1, now + 1, kinds=["derate_warning"])
        assert events, "derate_warning event must be recorded"
        import json as _json
        detail = _json.loads(events[0]["detail"])
        assert 0 < detail["minutes_to_trip"] <= thermal.DERATE_WARN_MIN
        assert detail["suggested_max_a"] == 43.0

        # Charging stops -> warning clears.
        await poller._check_derate_forecast(now + 1, {"contactor_closed": 0, "vehicle_current_a": 0.0})
        assert not any(a["alert"] == thermal.DERATE_ALERT for a in db.active_alerts())
        assert db.events_range(now - 1, now + 2, kinds=["derate_warning_cleared"])


async def test_notify_webhook_posts_actionable_warning(db):
    from aiohttp import web as aioweb

    received = []

    async def hook(request):
        received.append(await request.json())
        return aioweb.Response()

    app = aioweb.Application()
    app.router.add_post("/hook", hook)
    runner = aioweb.AppRunner(app)
    await runner.setup()
    site = aioweb.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        cfg = Config(host="127.0.0.1:1", notify_url=f"http://127.0.0.1:{port}/hook")
        bus = EventBus()
        async with aiohttp.ClientSession() as client:
            poller = Poller(cfg, db, bus, client)
            await poller._notify("derate_warning", "Thermal derate predicted",
                                 "cap at 43 A", {"suggested_max_a": 43.0})
        assert len(received) == 1
        assert received[0]["kind"] == "derate_warning"
        assert received[0]["detail"]["suggested_max_a"] == 43.0

        # No URL configured -> no-op, no error.
        cfg2 = Config(host="127.0.0.1:1")
        async with aiohttp.ClientSession() as client:
            poller = Poller(cfg2, db, bus, client)
            await poller._notify("x", "t", "b", None)
        assert len(received) == 1
    finally:
        await runner.cleanup()


async def test_notify_ntfy_format(db):
    # ntfy format: plain-text body with title/priority/tags headers, so the
    # webhook URL can be a self-hosted ntfy topic directly.
    from aiohttp import web as aioweb

    received = []

    async def topic(request):
        received.append({"headers": dict(request.headers), "text": await request.text()})
        return aioweb.Response()

    app = aioweb.Application()
    app.router.add_post("/wallmonitor", topic)
    runner = aioweb.AppRunner(app)
    await runner.setup()
    site = aioweb.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        cfg = Config(
            host="127.0.0.1:1",
            notify_url=f"http://127.0.0.1:{port}/wallmonitor",
            notify_format="ntfy",
        )
        bus = EventBus()
        async with aiohttp.ClientSession() as client:
            poller = Poller(cfg, db, bus, client)
            await poller._notify(
                "derate_warning", "Thermal derate predicted",
                "~12 min until the handle hits 65 °C. Set the vehicle's charge current to ≤43 A.",
                {"suggested_max_a": 43.0},
            )
        assert len(received) == 1
        assert received[0]["headers"]["X-Title"] == "Thermal derate predicted"
        assert received[0]["headers"]["X-Priority"] == "urgent"
        assert "zap" in received[0]["headers"]["X-Tags"]
        assert "65 °C" in received[0]["text"]
    finally:
        await runner.cleanup()


async def test_thermal_drift_alert_clears_when_history_too_thin(db):
    # An active drift alert must not linger once there is no longer enough
    # comparable history for a verdict (detect_drift -> None).
    now = time.time()
    db.raise_alert(now - 60, thermal.DRIFT_ALERT, "monitor")
    cfg = Config(host="127.0.0.1:1")
    bus = EventBus()
    async with aiohttp.ClientSession() as client:
        poller = Poller(cfg, db, bus, client)
        await poller._check_thermal_drift(now)
    assert not any(a["alert"] == thermal.DRIFT_ALERT for a in db.active_alerts())
    events = db.events_range(now - 1, now + 1)
    assert any(e["kind"] == "thermal_drift_cleared" for e in events)


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
