"""aiohttp web application: JSON API + Server-Sent Events + static UI.

Everything is served locally; the page loads no external assets, fonts, or
scripts, consistent with the project's local-network-only requirement.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from importlib import resources

from aiohttp import web

from . import thermal
from .db import Database
from .poller import EventBus, Poller

log = logging.getLogger("wallmonitor.web")

STATIC_PKG = "wallmonitor.static"


def _float_q(request: web.Request, name: str, default: float) -> float:
    """Float query parameter, silently falling back on absent/garbage input
    — range endpoints prefer a default window over a 400."""
    try:
        return float(request.query[name])
    except (KeyError, ValueError):
        return default


def make_app(db: Database, bus: EventBus, poller: Poller | None) -> web.Application:
    """Assemble the HTTP app: static UI, JSON API, SSE stream, two ingests.

    Conventions shared by every handler: range endpoints take from/to/... as
    UTC epoch seconds with sensible default windows; all SQLite work hops to
    a thread (the handlers themselves never block the loop); responses are
    plain JSON dicts ready for app.js. Nothing here talks to the charger —
    reads come from the DB, live data from the poller's EventBus. poller is
    None only in tests that exercise the API without a device.
    """
    app = web.Application()

    # Without cache headers browsers cache heuristically, so after an update
    # they may keep running stale UI code until a manual hard refresh.
    # no-cache forces revalidation on every load — trivial cost on a LAN.
    no_cache = {"Cache-Control": "no-cache"}

    async def index(_request: web.Request) -> web.Response:
        html = resources.files(STATIC_PKG).joinpath("index.html").read_text()
        return web.Response(text=html, content_type="text/html", headers=no_cache)

    async def static_file(request: web.Request) -> web.Response:
        """Serve app.js/style.css from the package, allowlisted by name —
        read per-request from disk, so frontend-only changes deploy with a
        git pull and a browser refresh, no service restart."""
        name = request.match_info["name"]
        if name not in ("app.js", "style.css"):
            raise web.HTTPNotFound()
        content = resources.files(STATIC_PKG).joinpath(name).read_text()
        ctype = "application/javascript" if name.endswith(".js") else "text/css"
        return web.Response(text=content, content_type=ctype, headers=no_cache)

    async def api_alert_codes(_request: web.Request) -> web.Response:
        data = resources.files("wallmonitor").joinpath("alert_codes.json").read_text()
        return web.Response(text=data, content_type="application/json", headers=no_cache)

    async def api_status(_request: web.Request) -> web.Response:
        """One-call snapshot for the header/tiles: newest sample from every
        table, poller health, active alerts, the open session, row counts."""
        now = time.time()
        latest = await asyncio.to_thread(db.latest_vitals)
        wifi = await asyncio.to_thread(db.latest_wifi)
        lifetime = await asyncio.to_thread(db.latest_lifetime)
        version = await asyncio.to_thread(db.latest_version)
        alerts = await asyncio.to_thread(db.active_alerts)
        counts = await asyncio.to_thread(db.counts)
        session = None
        sid = latest.get("session_id") if latest else None
        if sid:
            session = await asyncio.to_thread(db.session, int(sid))
        return web.json_response(
            {
                "server_ts": now,
                "poller": poller.status() if poller else None,
                "vitals": latest,
                "wifi": wifi,
                "lifetime": lifetime,
                "version": version,
                "active_alerts": alerts,
                "active_session": session,
                "counts": counts,
            }
        )

    async def api_vitals(request: web.Request) -> web.Response:
        now = time.time()
        t_from = _float_q(request, "from", now - 3600)
        t_to = _float_q(request, "to", now)
        max_points = int(_float_q(request, "points", 1500))
        rows = await asyncio.to_thread(db.vitals_range, t_from, t_to, min(max_points, 5000))
        return web.json_response({"from": t_from, "to": t_to, "samples": rows})

    async def api_wifi(request: web.Request) -> web.Response:
        now = time.time()
        t_from = _float_q(request, "from", now - 24 * 3600)
        t_to = _float_q(request, "to", now)
        rows = await asyncio.to_thread(db.wifi_range, t_from, t_to)
        return web.json_response({"from": t_from, "to": t_to, "samples": rows})

    async def api_lifetime(request: web.Request) -> web.Response:
        now = time.time()
        t_from = _float_q(request, "from", now - 90 * 24 * 3600)
        t_to = _float_q(request, "to", now)
        rows = await asyncio.to_thread(db.lifetime_range, t_from, t_to)
        return web.json_response({"from": t_from, "to": t_to, "samples": rows})

    async def api_sessions(request: web.Request) -> web.Response:
        now = time.time()
        t_from = _float_q(request, "from", now - 90 * 24 * 3600)
        t_to = _float_q(request, "to", now)
        rows = await asyncio.to_thread(db.sessions_range, t_from, t_to)
        return web.json_response({"sessions": rows})

    async def api_session_detail(request: web.Request) -> web.Response:
        """One session with its samples and events. The 1 s pad around the
        window catches boundary samples; the filter then drops any padded-in
        rows that belong to a *different* session (back-to-back plug-ins)."""
        try:
            sid = int(request.match_info["id"])
        except ValueError:
            raise web.HTTPBadRequest(text="bad session id") from None
        session = await asyncio.to_thread(db.session, sid)
        if session is None:
            raise web.HTTPNotFound(text="no such session")
        end = session["end_ts"] or time.time()
        samples = await asyncio.to_thread(db.vitals_range, session["start_ts"] - 1, end + 1, 2000)
        samples = [
            sample
            for sample in samples
            if sample.get("session_id") == sid or sample["ts"] >= session["start_ts"]
        ]
        events = await asyncio.to_thread(db.events_range, session["start_ts"] - 1, end + 1)
        forecasts = await asyncio.to_thread(db.forecast_range, session["start_ts"] - 1, end + 1)
        forecasts = [row for row in forecasts if row.get("session_id") in (sid, None)]
        return web.json_response(
            {"session": session, "samples": samples, "events": events, "forecasts": forecasts}
        )

    async def api_alerts(request: web.Request) -> web.Response:
        now = time.time()
        t_from = _float_q(request, "from", now - 90 * 24 * 3600)
        t_to = _float_q(request, "to", now)
        active = await asyncio.to_thread(db.active_alerts)
        history = await asyncio.to_thread(db.alerts_range, t_from, t_to)
        return web.json_response({"active": active, "history": history})

    async def api_forecasts(request: web.Request) -> web.Response:
        """Recorded derate-forecast snapshots — what the model said at each
        30 s charging tick, i.e. exactly what the amp controller saw. Seeds
        the live chart's prediction history across reloads and tab switches."""
        now = time.time()
        t_from = _float_q(request, "from", now - 24 * 3600)
        t_to = _float_q(request, "to", now)
        rows = await asyncio.to_thread(db.forecast_range, t_from, t_to)
        return web.json_response({"from": t_from, "to": t_to, "samples": rows})

    async def api_events(request: web.Request) -> web.Response:
        """Event log, newest first; ?kinds=a,b,c filters server-side so the
        timeline's category chips don't pull thousands of unwanted rows."""
        now = time.time()
        t_from = _float_q(request, "from", now - 7 * 24 * 3600)
        t_to = _float_q(request, "to", now)
        kinds = request.query.get("kinds")
        kind_list = [kind for kind in kinds.split(",") if kind] if kinds else None
        rows = await asyncio.to_thread(db.events_range, t_from, t_to, kind_list)
        return web.json_response({"events": rows})

    # The BLE amp controller acts on this monitor's forecasts but runs as a
    # separate process; these are the only kinds it may write, so the timeline
    # stays curated rather than becoming a generic log sink.
    AMP_EVENT_KINDS = ("amp_capped", "amp_restored", "amp_adjust_failed")

    async def api_event_ingest(request: web.Request) -> web.Response:
        """Record an amp-controller action as a first-class event.

        Timestamp is server-side on receipt, like every other ingest path."""
        try:
            body = await request.json()
        except ValueError:
            return web.json_response({"error": "invalid json"}, status=400)
        kind = body.get("kind")
        if kind not in AMP_EVENT_KINDS:
            return web.json_response({"error": "unknown kind"}, status=400)
        detail = body.get("detail")
        if detail is not None and not isinstance(detail, dict):
            return web.json_response({"error": "detail must be an object"}, status=400)
        if detail is not None and len(json.dumps(detail)) > 2048:
            return web.json_response({"error": "detail too large"}, status=400)
        ts = time.time()
        await asyncio.to_thread(db.add_event, ts, kind, detail)
        bus.publish({"type": "event", "ts": ts, "kind": kind, "detail": detail})
        return web.json_response({"ok": True, "ts": ts, "kind": kind})

    # Model parameters change only as new sessions land, so the (SQLite-heavy)
    # history fit is cached; the live prediction is computed on every call.
    thermal_fit: dict = {"params": None, "fits": [], "ts": 0.0}

    async def api_thermal(request: web.Request) -> web.Response:
        """The full thermal picture: fitted model, live forecast, per-segment
        fits, drift verdict, baseline anchor. Also what the BLE amp
        controller polls every 30 s. ?refit busts the 6 h fit cache."""
        now = time.time()
        if (
            thermal_fit["params"] is None
            or now - thermal_fit["ts"] > 6 * 3600
            or "refit" in request.query
        ):
            fits = await asyncio.to_thread(thermal.fit_sessions, db, now)
            thermal_fit["fits"] = fits
            thermal_fit["params"] = thermal.fit_history(db, now, fits=fits)
            thermal_fit["ts"] = now
        result = await asyncio.to_thread(thermal.predict, db, now, thermal_fit["params"])
        anchor = await asyncio.to_thread(thermal.baseline_anchor, db)
        return web.json_response(
            {
                "server_ts": now,
                **result,
                "drift": thermal.detect_drift(thermal_fit["fits"], anchor_ts=anchor),
                "session_fits": thermal_fit["fits"],
                "baseline_anchor_ts": anchor,
            }
        )

    def _num(fields: dict, *keys: str) -> float | None:
        """First key that parses as a float, in priority order — how the
        ambient ingest prefers metric fields over the Ecowitt imperial ones."""
        for key in keys:
            value = fields.get(key)
            if value is None or value == "":
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        return None

    async def api_ambient_ingest(request: web.Request) -> web.Response:
        """Receive a garage ambient reading from a LAN sensor.

        Speaks two dialects: the Ecowitt gateway "customized upload"
        (form-encoded, Fahrenheit/inHg fields — point the gateway's custom
        server at this path) and plain JSON ({"temp_c": ..} with optional
        humidity_pct/pressure_hpa) for anything else on the network (Shelly
        action URLs, a curl in a cron job). Metric wins when both appear.

        JSON may also carry "source" naming the reporter. "car" is special:
        it marks a mobile sensor (a vehicle parked in the garage), which the
        thermal model uses only when no stationary sensor is reporting."""
        if request.content_type == "application/json":
            try:
                fields = dict(await request.json())
            except json.JSONDecodeError:
                return web.json_response({"error": "invalid JSON"}, status=400)
            source = str(fields.pop("source", "") or "").strip().lower()[:32] or "json"
        else:
            fields = dict(await request.post())
            source = "ecowitt"
        fields.pop("PASSKEY", None)  # gateway auth token; not ours to keep
        temp_c = _num(fields, "temp_c")
        if temp_c is None:
            # Ecowitt: tempinf is the gateway's own (indoor) sensor — the one
            # in the garage; tempf/temp1f are add-on RF channels.
            temp_f = _num(fields, "tempinf", "tempf", "temp1f")
            temp_c = (temp_f - 32.0) * 5.0 / 9.0 if temp_f is not None else None
        if temp_c is None or not (-40.0 <= temp_c <= 85.0):
            return web.json_response({"error": "no usable temperature"}, status=400)
        humidity = _num(fields, "humidity_pct", "humidityin", "humidity", "humidity1")
        pressure = _num(fields, "pressure_hpa")
        if pressure is None:
            inhg = _num(fields, "baromrelin", "baromabsin")
            pressure = inhg * 33.8639 if inhg is not None else None
        now = time.time()
        await asyncio.to_thread(db.insert_ambient, now, temp_c, humidity, pressure, fields, source)
        return web.json_response(
            {"ok": True, "ts": now, "temp_c": round(temp_c, 2), "source": source}
        )

    async def api_ambient_history(request: web.Request) -> web.Response:
        now = time.time()
        t_from = _float_q(request, "from", now - 24 * 3600)
        t_to = _float_q(request, "to", now)
        rows = await asyncio.to_thread(db.ambient_range, t_from, t_to)
        latest = await asyncio.to_thread(db.latest_ambient)
        return web.json_response({"samples": rows, "latest": latest})

    async def api_baseline_anchor(request: web.Request) -> web.Response:
        """Set (POST {"ts": ...}, default now) or clear (DELETE) the
        verified-baseline anchor. Fits before the anchor stay on the chart
        but leave the drift comparison: set it after the hardware has been
        inspected and verified so the baseline means "verified healthy".

        Either change re-evaluates the drift verdict right away — the alert
        otherwise only updates when a session ends, which can latch a
        no-longer-justified alert for as long as the vehicle stays plugged
        in (or hide a re-justified one after the anchor is cleared)."""
        if request.method == "DELETE":
            await asyncio.to_thread(db.delete_setting, thermal.BASELINE_ANCHOR_KEY)
            await asyncio.to_thread(db.add_event, time.time(), "baseline_anchor_cleared", None)
            if poller is not None:
                await poller.recheck_thermal_drift(time.time())
            return web.json_response({"baseline_anchor_ts": None})
        try:
            body = await request.json() if request.can_read_body else {}
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid JSON"}, status=400)
        ts = body.get("ts", time.time())
        if not isinstance(ts, (int, float)):
            return web.json_response({"error": "ts must be a number"}, status=400)
        await asyncio.to_thread(db.set_setting, thermal.BASELINE_ANCHOR_KEY, repr(float(ts)))
        await asyncio.to_thread(db.add_event, time.time(), "baseline_anchor_set", {"ts": float(ts)})
        if poller is not None:
            await poller.recheck_thermal_drift(time.time())
        return web.json_response({"baseline_anchor_ts": float(ts)})

    async def api_stream(request: web.Request) -> web.StreamResponse:
        """Server-Sent Events: one EventBus subscription per connection.

        Protocol: every message is an unnamed `data: <json>` frame whose
        `type` field discriminates (vitals | wifi | lifetime | event |
        thermal); `: connected` / `: keepalive` comment frames keep proxies
        and EventSource happy. No event ids, so reconnects start fresh —
        clients re-seed history over the JSON API, which app.js does on
        view mount anyway."""
        response = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            }
        )
        await response.prepare(request)
        queue = bus.subscribe()
        try:
            await response.write(b": connected\n\n")
            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=15.0)
                except TimeoutError:
                    await response.write(b": keepalive\n\n")
                    continue
                payload = json.dumps(msg).encode()
                await response.write(b"data: " + payload + b"\n\n")
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            bus.unsubscribe(queue)
        return response

    app.router.add_get("/", index)
    app.router.add_get("/static/{name}", static_file)
    app.router.add_get("/api/alert-codes", api_alert_codes)
    app.router.add_get("/api/status", api_status)
    app.router.add_get("/api/vitals", api_vitals)
    app.router.add_get("/api/wifi", api_wifi)
    app.router.add_get("/api/lifetime", api_lifetime)
    app.router.add_get("/api/sessions", api_sessions)
    app.router.add_get("/api/sessions/{id}", api_session_detail)
    app.router.add_get("/api/alerts", api_alerts)
    app.router.add_get("/api/thermal", api_thermal)
    app.router.add_post("/api/thermal/baseline-anchor", api_baseline_anchor)
    app.router.add_delete("/api/thermal/baseline-anchor", api_baseline_anchor)
    app.router.add_post("/api/ambient", api_ambient_ingest)
    app.router.add_get("/api/ambient", api_ambient_history)
    app.router.add_get("/api/forecasts", api_forecasts)
    app.router.add_get("/api/events", api_events)
    app.router.add_post("/api/events", api_event_ingest)
    app.router.add_get("/api/stream", api_stream)
    return app
