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
    try:
        return float(request.query[name])
    except (KeyError, ValueError):
        return default


def make_app(db: Database, bus: EventBus, poller: Poller | None) -> web.Application:
    app = web.Application()

    # Without cache headers browsers cache heuristically, so after an update
    # they may keep running stale UI code until a manual hard refresh.
    # no-cache forces revalidation on every load — trivial cost on a LAN.
    no_cache = {"Cache-Control": "no-cache"}

    async def index(_request: web.Request) -> web.Response:
        html = resources.files(STATIC_PKG).joinpath("index.html").read_text()
        return web.Response(text=html, content_type="text/html", headers=no_cache)

    async def static_file(request: web.Request) -> web.Response:
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
        return web.json_response({"session": session, "samples": samples, "events": events})

    async def api_alerts(request: web.Request) -> web.Response:
        now = time.time()
        t_from = _float_q(request, "from", now - 90 * 24 * 3600)
        t_to = _float_q(request, "to", now)
        active = await asyncio.to_thread(db.active_alerts)
        history = await asyncio.to_thread(db.alerts_range, t_from, t_to)
        return web.json_response({"active": active, "history": history})

    async def api_events(request: web.Request) -> web.Response:
        now = time.time()
        t_from = _float_q(request, "from", now - 7 * 24 * 3600)
        t_to = _float_q(request, "to", now)
        kinds = request.query.get("kinds")
        kind_list = [kind for kind in kinds.split(",") if kind] if kinds else None
        rows = await asyncio.to_thread(db.events_range, t_from, t_to, kind_list)
        return web.json_response({"events": rows})

    # Model parameters change only as new sessions land, so the (SQLite-heavy)
    # history fit is cached; the live prediction is computed on every call.
    thermal_fit: dict = {"params": None, "fits": [], "ts": 0.0}

    async def api_thermal(request: web.Request) -> web.Response:
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
        return web.json_response(
            {
                "server_ts": now,
                **result,
                "drift": thermal.detect_drift(thermal_fit["fits"]),
                "session_fits": thermal_fit["fits"],
            }
        )

    async def api_stream(request: web.Request) -> web.StreamResponse:
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
    app.router.add_get("/api/events", api_events)
    app.router.add_get("/api/stream", api_stream)
    return app
