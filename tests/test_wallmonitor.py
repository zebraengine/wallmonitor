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
            "handle_temp_c": round(thermal.idle_handle_c(ambient_c), 2),
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
    t0_temp = thermal.idle_handle_c(ambient_c)
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
        temp += dt * ((thermal.idle_handle_c(ambient_final) - temp) / tau_s)
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
        elif into < 1500:
            current = amps  # full rate for 25 min...
        else:
            current = amps / 2  # ...then derated for hours (most samples)
        temp = t_inf - (t_inf - temp0) * math.exp(-into / tau_s) if into < 1500 else 60.0
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


async def test_thermal_fit_rejects_window_short_against_tau(db):
    # A steady window that ends before the plateau shows can't separate rise
    # from tau: the fitter trades a lower rise for a faster tau and passes
    # every other gate with a fine RMSE. Seen on a real install as 21 min
    # charges fitting 8 C under the rest and dragging the drift baseline
    # down. The gate judges span against the install's median tau, not the
    # fit's own (the biased quantity), so a truncated segment can't vouch
    # for itself.
    now = time.time()
    tau_s = 720.0
    for i in range(4):  # establish the install's tau from full-length ramps
        _seed_thermal_session(db, now - (6 - i) * 7200, ambient_c=25.0, tau_s=tau_s, charge_s=1500.0)
    short_start = now - 7200
    _seed_thermal_session(db, short_start, ambient_c=25.0, tau_s=tau_s, charge_s=720.0)  # 1.0 tau
    fits = thermal.fit_sessions(db, now)
    assert len(fits) == 4, "the four plateau-observing ramps fit"
    assert all(abs(fit["start_ts"] - short_start) > 120 for fit in fits), "the 1 tau window does not"
    # The boundary itself: a 1.8 tau window is the shortest that passes.
    _seed_thermal_session(db, now - 3600, ambient_c=25.0, tau_s=tau_s,
                          charge_s=thermal.MIN_SPAN_TAU * tau_s + 60.0)
    assert len(thermal.fit_sessions(db, now)) == 5


async def test_thermal_fit_first_fit_judged_against_default_tau(db):
    # Fresh install, no history: the gate has no earlier fits to judge a
    # window against, and the fit's own tau is exactly the quantity a
    # truncated window biases low. Floored at DEFAULT_TAU_MIN, a 12 min
    # first charge is rejected even though its own tau (6 min) would have
    # let it vouch for itself at 1.8 tau = 10.8 min.
    now = time.time()
    _seed_thermal_session(db, now - 7200, ambient_c=25.0, tau_s=360.0, charge_s=720.0)
    assert thermal.fit_sessions(db, now) == []
    # A charge that clears 1.8 x DEFAULT_TAU_MIN is the first to teach the model.
    _seed_thermal_session(db, now - 3600, ambient_c=25.0, tau_s=360.0,
                          charge_s=thermal.MIN_SPAN_TAU * thermal.DEFAULT_TAU_MIN * 60.0 + 60.0)
    fits = thermal.fit_sessions(db, now)
    assert len(fits) == 1 and abs(fits[0]["tau_min"] - 6.0) < 1.0


async def test_thermal_fit_slow_tau_install_still_fits(db):
    # A heavier cable or enclosed handle can have a tau near 20 min. The
    # steady-prefix window must scale with the install's tau: a fixed 30 min
    # cap would leave every window under 1.8 tau and the install blind.
    now = time.time()
    tau_s, rise = 1200.0, 30.0
    for i in range(4):
        _seed_thermal_session(db, now - (5 - i) * 4 * 3600, ambient_c=22.0, tau_s=tau_s,
                              rise_ref_c=rise, charge_s=3900.0)
    fits = thermal.fit_sessions(db, now)
    assert len(fits) == 4
    for fit in fits:
        assert abs(fit["tau_min"] - 20.0) < 2.0
        assert fit["rise_ref_c"] is not None and abs(fit["rise_ref_c"] - rise) < 3.0


def test_thermal_params_report_prior_deviation():
    # Unfitted: nothing to compare. Fitted near the defaults: reported but
    # not notable. Fitted far off (a fast-tau, low-rise install): notable,
    # with the sign the UI needs to warn that short charges won't fit.
    assert thermal.ThermalParams().prior_deviation() is None
    near = thermal.ThermalParams(tau_min=12.5, rise_ref_c=34.0, tau_fits=3, rise_fits=3)
    dev = near.prior_deviation()
    assert dev["notable"] is False and abs(dev["tau_frac"]) < 0.1
    far = thermal.ThermalParams(tau_min=6.0, rise_ref_c=20.0, tau_fits=3, rise_fits=3)
    dev = far.prior_deviation()
    assert dev["notable"] is True and dev["tau_frac"] < -0.3 and dev["rise_frac"] < -0.3
    assert dev["default_tau_min"] == thermal.DEFAULT_TAU_MIN
    assert far.as_dict()["prior_deviation"] == dev


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
    t0_temp = thermal.idle_handle_c(ambient)
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
                "handle_temp_c": round(thermal.idle_handle_c(ambient), 2),
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
    idle_temp = thermal.idle_handle_c(ambient)
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


async def test_ambient_ingest_ecowitt_and_json(db):
    app = make_app(db, EventBus(), None)
    async with TestClient(TestServer(app)) as client:
        # Ecowitt "customized upload": form-encoded, Fahrenheit / inHg.
        resp = await client.post("/api/ambient", data={
            "PASSKEY": "SECRET", "stationtype": "GW1200B_V1.0.0",
            "tempinf": "77.9", "humidityin": "27", "baromrelin": "29.86",
        })
        assert resp.status == 200
        body = await resp.json()
        assert abs(body["temp_c"] - 25.5) < 0.1
        latest = db.latest_ambient()
        assert abs(latest["temp_c"] - 25.5) < 0.1
        assert abs(latest["humidity_pct"] - 27.0) < 0.1
        assert abs(latest["pressure_hpa"] - 1011.2) < 1.0
        assert latest["source"] == "ecowitt"
        # The gateway's auth token must not be persisted.
        raw = db._rows("SELECT raw FROM ambient_samples ORDER BY id DESC LIMIT 1")[0]["raw"]
        assert "SECRET" not in raw
        # Generic JSON (Shelly action, curl, anything on the LAN).
        resp = await client.post("/api/ambient", json={"temp_c": 31.1, "humidity_pct": 55})
        assert resp.status == 200
        assert abs(db.latest_ambient()["temp_c"] - 31.1) < 0.01
        assert db.latest_ambient()["source"] == "json"
        # No usable temperature -> rejected, nothing stored.
        resp = await client.post("/api/ambient", data={"humidityin": "40"})
        assert resp.status == 400
        # History endpoint returns both samples.
        data = await (await client.get("/api/ambient")).json()
        assert len(data["samples"]) == 2 and data["latest"]["temp_c"] == 31.1
        # A JSON caller may name its source; "car" (normalized) marks a
        # mobile sensor the thermal model treats as second-tier.
        resp = await client.post("/api/ambient", json={"temp_c": 29.4, "source": "Car"})
        assert resp.status == 200 and (await resp.json())["source"] == "car"
        assert db.latest_ambient()["source"] == "car"


def _seed_ambient(db, t_from, t_to, temp_of, dt=60.0, source=None):
    ts = t_from
    while ts <= t_to:
        db.insert_ambient(ts, round(temp_of(ts), 2), source=source)
        ts += dt


async def test_thermal_fit_prefers_measured_ambient(db):
    # A LAN sensor reporting the garage air beats every handle-derived
    # estimate: the fit brackets from measured samples (source "measured")
    # and recovers the true rise even with ambient drifting mid-charge.
    now = time.time()
    start, charge_s = now - 2 * 7200, 1800.0
    _seed_thermal_session(db, start, ambient_c=30.0, ambient_end_c=33.0, charge_s=charge_s)
    ramp = lambda ts: 30.0 + 3.0 * min(1.0, max(0.0, (ts - start) / charge_s))
    _seed_ambient(db, start - 1200, start + charge_s + 600, ramp)
    fits = thermal.fit_sessions(db, now)
    assert len(fits) == 1
    fit = fits[0]
    assert fit["ambient_source"] == "measured"
    assert fit["ambient_drift_c"] is not None and 2.0 < fit["ambient_drift_c"] < 4.0
    assert abs(fit["rise_ref_c"] - 36.0) < 1.5


async def test_thermal_predict_idle_prefers_measured_ambient(db):
    now = time.time()
    # Handle still warm from a recent charge (proxy would read 35 C); the
    # sensor says the garage is actually 30 C.
    _seed_idle(db, now - 600, now, 35.0)
    db.insert_ambient(now - 60, 30.0)
    out = thermal.predict(db, now, thermal.ThermalParams())
    assert out["state"] == "idle"
    assert out["ambient_source"] == "measured"
    assert abs(out["ambient_c"] - 30.0) < 0.01
    # A stale sensor (silent > freshness window) falls back to the proxy.
    db2_ambient_ts = now - 3600
    db._execute("UPDATE ambient_samples SET ts = ?", (db2_ambient_ts,))
    out = thermal.predict(db, now, thermal.ThermalParams())
    assert out["ambient_source"] == "idle_handle"
    assert abs(out["ambient_c"] - 35.0) < 0.1


async def test_thermal_car_ambient_yields_to_stationary_sensor(db):
    # A parked vehicle's sensor is a real garage thermometer, but it drives
    # away and reads high after drives — so it fills in only when nothing
    # stationary reports, and a stationary sample wins even when the car's
    # is newer.
    now = time.time()
    _seed_idle(db, now - 600, now, 35.0)  # proxy alone would say 35 C
    db.insert_ambient(now - 90, 30.0, source="car")
    out = thermal.predict(db, now, thermal.ThermalParams())
    assert out["ambient_source"] == "measured_car"
    assert abs(out["ambient_c"] - 30.0) < 0.01
    db.insert_ambient(now - 300, 28.0, source="ecowitt")
    out = thermal.predict(db, now, thermal.ThermalParams())
    assert out["ambient_source"] == "measured"
    assert abs(out["ambient_c"] - 28.0) < 0.01
    # The window-median reader applies the same precedence: interleaved car
    # samples don't pollute a stationary sensor's median.
    base = now - 7200
    for i in range(5):
        db.insert_ambient(base + i * 60, 31.0 + i * 0.1, source="car")
        db.insert_ambient(base + i * 60 + 30, 26.0, source="ecowitt")
    val, tag = thermal._measured_ambient(db, base - 60, base + 400)
    assert tag == "measured" and abs(val - 26.0) < 0.01


async def test_thermal_fit_car_ambient_tagged(db):
    # With only the car reporting, its samples still beat the handle proxy
    # for the fit bracket — and the fit says the read came from the car.
    now = time.time()
    start, charge_s = now - 2 * 7200, 1800.0
    _seed_thermal_session(db, start, ambient_c=30.0, charge_s=charge_s)
    _seed_ambient(db, start - 1200, start + charge_s + 600, lambda _ts: 30.0, source="car")
    fits = thermal.fit_sessions(db, now)
    assert len(fits) == 1
    assert fits[0]["ambient_source"] == "measured_car"
    assert abs(fits[0]["rise_ref_c"] - 36.0) < 1.5


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
    assert drift is not None and drift["confident"] is False
    assert drift["delta_ci95_c"][0] < 0
    # Past the floor, inside the scatter: a lead, not an alert — and the
    # effective threshold says how much this install needs to confirm.
    assert drift["drifting"] is False and drift["lead"] is True
    assert drift["threshold_c"] > drift["floor_c"] and drift["delta_c"] < drift["threshold_c"]


def test_thermal_drift_threshold_follows_install_scatter():
    # Same delta, two installs: the quiet one's interval clears zero and
    # it alarms; the noisy one's doesn't and it gets a lead. The threshold
    # is the larger of the materiality floor and what the scatter needs.
    def fits(rises):
        return [{"start_ts": 1000.0 * i, "rise_ref_c": r, "current_a": 48.5, "ambient_drift_c": None}
                for i, r in enumerate(rises)]
    quiet = thermal.detect_drift(fits([36.0, 36.2, 35.9, 36.1, 36.0, 35.8, 39.0, 39.2, 38.9]))
    noisy = thermal.detect_drift(fits([33.0, 39.0, 34.0, 38.0, 35.0, 37.0, 39.0, 39.2, 38.9]))
    assert abs(quiet["delta_c"] - 3.0) < 0.2 and abs(noisy["delta_c"] - 3.0) < 0.6
    assert quiet["drifting"] is True and quiet["lead"] is False
    assert abs(quiet["threshold_c"] - thermal.DRIFT_WARN_C) < 0.01  # the floor binds
    assert noisy["drifting"] is False and noisy["lead"] is True
    assert noisy["threshold_c"] > thermal.DRIFT_WARN_C  # the scatter binds
    # A confirmed but immaterial increase is not drift either.
    tiny = thermal.detect_drift(fits([36.0, 36.1, 35.9, 36.0, 36.1, 35.9, 36.8, 36.9, 36.7]))
    assert tiny["confident"] is True and tiny["drifting"] is False and tiny["lead"] is False


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
        await poller.recheck_thermal_drift(now)
    alerts = db.active_alerts()
    assert any(alert["alert"] == thermal.DRIFT_ALERT and alert["source"] == "monitor" for alert in alerts)
    events = db.events_range(now - 1, now + 1)
    assert any(event["kind"] == "thermal_drift" for event in events)


async def _drift_notification(db, rises):
    """Seed sessions with the given fitted rises, run the drift recheck, and
    return the single (kind, body, detail) the poller tried to send."""
    now = time.time()
    for i, rise in enumerate(rises):
        _seed_thermal_session(db, now - (len(rises) - i) * 7200, ambient_c=25.0, rise_ref_c=rise)
    sent = []
    async with aiohttp.ClientSession() as client:
        poller = Poller(Config(host="127.0.0.1:1"), db, EventBus(), client)

        async def capture(kind, title, body, detail):
            sent.append((kind, body, detail))

        poller._notify = capture
        await poller.recheck_thermal_drift(now)
        # A second recheck with nothing new must not push again.
        await poller.recheck_thermal_drift(now + 1)
    assert len(sent) == 1
    return sent[0]


async def test_thermal_drift_confirmed_notifies_high_priority(db):
    # Tight baseline, tight recent, big step: the interval clears zero, and
    # that is the verdict worth interrupting a phone for.
    kind, body, detail = await _drift_notification(db, [36.0, 36.5, 35.8, 36.2, 42.0, 41.5, 42.3])
    assert detail["drifting"] is True and kind == "thermal_drift"
    assert "statistically confirmed" in body
    assert Poller.NTFY_PRIORITY[kind] == "high"
    assert any(a["alert"] == thermal.DRIFT_ALERT for a in db.active_alerts())


async def test_thermal_drift_lead_notifies_default_priority(db):
    # Scattered baseline, modest step past the floor: the interval straddles
    # zero. With ~3.4 C session-to-session scatter the 2.5 C floor sits near
    # one sigma, so this is a lead for the dashboard and a quiet push that
    # says what it would take to confirm — not an alert.
    kind, body, detail = await _drift_notification(db, [31.0, 38.0, 33.0, 38.0, 32.0, 37.0, 39.5, 38.0, 39.0])
    assert detail["lead"] is True and detail["drifting"] is False
    assert kind == "thermal_drift_lead" and "to confirm" in body
    assert Poller.NTFY_PRIORITY[kind] == "default"
    # A lead is not an alert: no banner, no alert row — an event only.
    assert not any(a["alert"] == thermal.DRIFT_ALERT for a in db.active_alerts())
    kinds = [e["kind"] for e in db.events_range(time.time() - 60, time.time() + 60)]
    assert "thermal_drift_lead" in kinds and "thermal_drift" not in kinds


async def test_baseline_anchor_reevaluates_drift_alert(db):
    # Moving the anchor must re-judge the active alert immediately — not at
    # the next session end, which can be a full plugged-in day away. Setting
    # it right after an inspection clears the now-unjustified alert; clearing
    # it restores the old baseline and the verdict (and alert) that come
    # with it.
    now = time.time()
    rises = [36.0, 36.5, 35.8, 36.2, 42.0, 41.5, 42.3]
    for i, rise in enumerate(rises):
        _seed_thermal_session(db, now - (len(rises) - i) * 7200, ambient_c=25.0, rise_ref_c=rise)
    bus = EventBus()
    async with aiohttp.ClientSession() as session:
        poller = Poller(Config(host="127.0.0.1:1"), db, bus, session)
        await poller.recheck_thermal_drift(now)  # what a session close does
        drift_active = lambda: any(
            a["alert"] == thermal.DRIFT_ALERT for a in db.active_alerts())
        assert drift_active()
        app = make_app(db, bus, poller)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/thermal/baseline-anchor",
                                     json={"ts": now - 4 * 7200})
            assert resp.status == 200
            assert not drift_active(), "anchor set must clear the stale alert at once"
            resp = await client.delete("/api/thermal/baseline-anchor")
            assert resp.status == 200
            assert drift_active(), "anchor cleared must restore the old verdict at once"
    kinds = [e["kind"] for e in db.events_range(now - 10, time.time() + 10)]
    assert kinds.count("thermal_drift") == 2 and "thermal_drift_cleared" in kinds


async def test_derate_forecast_warns_then_clears(db):
    # A charging handle on a trajectory toward 65 °C inside the warning
    # horizon must raise the actionable alert with a suggested current cap,
    # and the warning must clear when charging stops.
    now = time.time()
    ambient, tau_s, amps = 33.0, 720.0, 48.0
    t_inf = ambient + thermal.DEFAULT_RISE_REF_C  # 69 °C steady state at 48 A
    t0_temp = thermal.idle_handle_c(ambient)
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
    queue = bus.subscribe()
    async with aiohttp.ClientSession() as client:
        poller = Poller(cfg, db, bus, client)
        await poller._check_derate_forecast(now, {"contactor_closed": 1, "vehicle_current_a": amps})
        # Every computed forecast is streamed for the live chart, ahead of the
        # edge-triggered alert frames.
        frame = queue.get_nowait()
        assert frame["type"] == "thermal" and frame["state"] == "charging"
        assert frame["forecast"]["will_trip"] is True
        assert frame["model"]["trip_c"] == thermal.TRIP_HANDLE_C
        # ...and recorded, so the chart can re-seed its history on mount.
        recorded = db.forecast_range(now - 1, now + 1)
        assert len(recorded) == 1
        assert recorded[0]["will_trip"] == 1
        assert recorded[0]["steady_state_c"] == frame["forecast"]["steady_state_c"]
        assert recorded[0]["tau_min"] == frame["model"]["tau_min"]
        alerts = db.active_alerts()
        assert any(a["alert"] == thermal.DERATE_ALERT and a["source"] == "monitor" for a in alerts)
        events = db.events_range(now - 1, now + 1, kinds=["derate_warning"])
        assert events, "derate_warning event must be recorded"
        import json as _json
        detail = _json.loads(events[0]["detail"])
        assert 0 < detail["minutes_to_trip"] <= thermal.DERATE_WARN_MIN
        assert detail["suggested_max_a"] == 43.0

        # Charging stops -> warning clears, and no thermal frame is streamed.
        while not queue.empty():
            queue.get_nowait()
        await poller._check_derate_forecast(now + 1, {"contactor_closed": 0, "vehicle_current_a": 0.0})
        assert not any(a["alert"] == thermal.DERATE_ALERT for a in db.active_alerts())
        assert db.events_range(now - 1, now + 2, kinds=["derate_warning_cleared"])
        while not queue.empty():
            assert queue.get_nowait()["type"] != "thermal"


async def test_event_ingest_records_amp_controller_actions(db):
    # The BLE amp controller runs out-of-process; POST /api/events is how its
    # cap/restore actions become first-class timeline events. Kinds are
    # allowlisted so the log stays curated, and the timestamp is server-side.
    import json as _json

    bus = EventBus()
    queue = bus.subscribe()
    app = make_app(db, bus, None)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/events",
            json={"kind": "amp_capped", "detail": {"from_a": 48.0, "to_a": 32.0, "basis": "trajectory"}},
        )
        assert resp.status == 200
        for bad in (
            {"kind": "session_start"},  # real kind, but not the controller's to write
            {"kind": "amp_capped", "detail": "not-an-object"},
            {"kind": "amp_capped", "detail": {"reason": "x" * 3000}},
        ):
            resp = await client.post("/api/events", json=bad)
            assert resp.status == 400
    events = db.events_range(0, time.time() + 1, kinds=["amp_capped"])
    assert len(events) == 1
    assert _json.loads(events[0]["detail"])["to_a"] == 32.0
    frame = queue.get_nowait()
    assert frame["type"] == "event" and frame["kind"] == "amp_capped"


async def test_forecasts_api_serves_recorded_snapshots(db):
    now = time.time()
    out = {
        "state": "charging", "handle_c": 55.0, "current_a": 40.0,
        "model": {"tau_min": 10.5, "fit_rmse_c": 0.3, "trip_c": 65.0},
        "forecast": {"basis": "trajectory", "steady_state_c": 62.0, "will_trip": False,
                     "minutes_to_trip": None, "trip_ts": None},
    }
    db.insert_forecast(now - 60, out, session_id=7)
    app = make_app(db, EventBus(), None)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(f"/api/forecasts?from={now - 900}&to={now}")
        data = await resp.json()
    assert resp.status == 200 and len(data["samples"]) == 1
    row = data["samples"][0]
    assert row["steady_state_c"] == 62.0 and row["will_trip"] == 0
    assert row["tau_min"] == 10.5 and row["session_id"] == 7


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
        await poller.recheck_thermal_drift(now)
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


def test_simulator_split_phase_profile():
    from wallmonitor.poller import _total_power
    from wallmonitor.simulator import SimState

    # Pin the clock mid-charge (55s of idle+connected, then 95s into charging)
    # so the ramp is complete and the taper hasn't started.
    mid_charge = time.time() - 150.0

    na = SimState(split_phase=True, start=mid_charge).vitals()
    assert 59.9 < na["grid_hz"] < 60.1
    assert 44.0 < na["vehicle_current_a"] <= 48.5
    # Recorded Gen 3 split-phase quirks: each leg reads about half the vehicle
    # current (neutral included) and voltageC sits near half of grid_v.
    assert na["currentA_a"] < na["vehicle_current_a"] * 0.6
    assert na["currentN_a"] > na["vehicle_current_a"] * 0.3
    assert abs(na["voltageC_v"] - na["grid_v"] / 2.0) < 2.0
    # The --split-phase power path must land near grid_v × vehicle_current
    # (~11 kW at full rate); the three-phase sum would be wildly different.
    power = _total_power(na, split_phase=True)
    assert 10_000 < power < 11_800

    eu = SimState(split_phase=False, start=mid_charge).vitals()
    assert 49.9 < eu["grid_hz"] < 50.1
    assert 15.0 < eu["vehicle_current_a"] <= 16.5
    assert 10_500 < _total_power(eu, split_phase=False) < 11_800


async def test_session_detail_includes_forecast_history(db):
    now = time.time()
    sid = db.start_session(now - 600)
    db.close_session(sid, now - 60, "vehicle_disconnected")
    out = {
        "state": "charging", "handle_c": 40.0, "current_a": 48.0,
        "model": {"tau_min": 11.0, "fit_rmse_c": 0.4, "trip_c": 65.0},
        "forecast": {"basis": "trajectory", "steady_state_c": 70.0, "will_trip": True,
                     "minutes_to_trip": 12.0, "trip_ts": now - 100},
    }
    # Three ticks converging downward, plus one from another session and one
    # outside the window — the detail payload must include only the first three.
    db.insert_forecast(now - 500, out, session_id=sid)
    out["forecast"]["steady_state_c"] = 58.0
    db.insert_forecast(now - 400, out, session_id=sid)
    out["forecast"]["steady_state_c"] = 54.0
    db.insert_forecast(now - 300, out, session_id=sid)
    db.insert_forecast(now - 450, out, session_id=sid + 1)
    db.insert_forecast(now - 5, out, session_id=None)

    app = make_app(db, EventBus(), None)
    async with TestClient(TestServer(app)) as client:
        detail = await (await client.get(f"/api/sessions/{sid}")).json()
    plateaus = [row["steady_state_c"] for row in detail["forecasts"]]
    assert plateaus == [70.0, 58.0, 54.0]
    assert all(row["session_id"] == sid for row in detail["forecasts"])


def test_project_t_inf_se_wide_early_tight_late():
    # Synthetic exponential toward 62C from 30C, tau 11 min, 0.05C noise
    # (deterministic): the projection's standard error must be far larger
    # from the first quarter of the ramp than from a window that has seen
    # the bend, and the flat-window fallback reports no SE at all.
    import math as m
    tau_s = 11 * 60.0
    curve = [(t, 62.0 - 32.0 * m.exp(-t / tau_s) + 0.05 * m.sin(t)) for t in range(0, 2400, 10)]
    early = curve[:18]      # first 3 minutes
    late = curve[:180]      # 30 minutes, bend well captured
    t_early, se_early = thermal._project_t_inf(early, 11.0)
    t_late, se_late = thermal._project_t_inf(late, 11.0)
    assert se_early is not None and se_late is not None
    assert se_early > 3 * se_late
    assert abs(t_late - 62.0) < 0.5
    # A noiseless flat window regresses to SSE = 0, so the raw SE is 0.0 —
    # numerically true and physically overconfident, which is why predict()
    # floors the published steady_state_se_c at the sensor's 0.1C step.
    flat = [(t, 62.0) for t in range(0, 300, 10)]
    t_flat, se_flat = thermal._project_t_inf(flat, 11.0)
    assert t_flat == 62.0 and se_flat == 0.0
