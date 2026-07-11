"""Polling engine: reads the Wall Connector and records everything.

Safety model (the "highest fidelity the device can safely operate" part):
- Requests are strictly sequential — never more than one in flight. The Gen 3
  Wall Connector runs a small embedded web server that degrades under
  concurrent or very aggressive polling.
- Vitals cadence adapts: tight (default 2s) while a vehicle is attached,
  relaxed (default 5s) while idle. Lower-value endpoints (wifi, lifetime,
  version) poll far less often.
- Consecutive failures back off exponentially up to a cap, so a struggling or
  rebooting device is left alone instead of hammered.

Timekeeping: every stored record and event carries the host's UTC epoch time
captured when the response arrived. One clock for samples, sessions, alerts
and events, so timelines line up exactly. The charger's own uptime_s is also
stored with each sample so device-side time can be cross-referenced.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import aiohttp

from tesla_wall_connector import WallConnector
from tesla_wall_connector.exceptions import WallConnectorError
from tesla_wall_connector.wifi_status import WifiStatus

from .config import Config
from .db import Database

log = logging.getLogger("wallmonitor.poller")


class EventBus:
    """Fan-out of live updates to SSE subscribers."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def publish(self, message: dict) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                # Slow consumer: drop it rather than stall the poller.
                self._subscribers.discard(q)


def _total_power(raw: dict, split_phase: bool) -> float | None:
    try:
        if split_phase:
            return round(raw["grid_v"] * raw["vehicle_current_a"], 1)
        return round(
            raw["voltageA_v"] * raw["currentA_a"]
            + raw["voltageB_v"] * raw["currentB_a"]
            + raw["voltageC_v"] * raw["currentC_a"],
            1,
        )
    except (KeyError, TypeError):
        return None


class Poller:
    def __init__(self, cfg: Config, db: Database, bus: EventBus, session: aiohttp.ClientSession):
        self.cfg = cfg
        self.db = db
        self.bus = bus
        self.wc = WallConnector(host=cfg.host, timeout=cfg.request_timeout, session=session)
        # per-endpoint scheduling state: next due time and current backoff interval
        self._due: dict[str, float] = {"vitals": 0.0, "wifi": 0.0, "lifetime": 0.0, "version": 0.0}
        self._fail_streak: dict[str, int] = dict.fromkeys(self._due, 0)
        # device state tracking
        self._prev_vitals: dict | None = None
        self._prev_wifi: dict | None = None
        self._session_id: int | None = None
        self._active_alerts: set[str] = set()
        self._offline = False
        self.last_poll_ok_ts: float | None = None
        self.last_poll_error: str | None = None
        self.started_ts = time.time()
        self._stop = asyncio.Event()

    # ---------- lifecycle ----------

    async def start(self) -> None:
        now = time.time()
        await asyncio.to_thread(self._startup_reconcile, now)
        self._task = asyncio.create_task(self._run(), name="wallmonitor-poller")

    def _startup_reconcile(self, now: float) -> None:
        self.db.add_event(now, "monitor_start", {"host": self.cfg.host})
        # A session left open by a previous run: keep it only if the vehicle is
        # still connected once we get our first sample; remember it for now.
        self._session_id = self.db.open_session_id()
        for alert in self.db.active_alerts():
            if alert["source"] == "device":
                self._active_alerts.add(alert["alert"])

    async def stop(self) -> None:
        self._stop.set()
        task = getattr(self, "_task", None)
        if task:
            await task
        await asyncio.to_thread(self.db.add_event, time.time(), "monitor_stop", None)

    async def _run(self) -> None:
        handlers = {
            "vitals": self._handle_vitals,
            "wifi": self._handle_wifi,
            "lifetime": self._handle_lifetime,
            "version": self._handle_version,
        }
        while not self._stop.is_set():
            now = time.time()
            # Strictly sequential: at most one endpoint polled per iteration.
            # Most-overdue first, so a tight vitals cadence can't starve the
            # slower endpoints.
            due_now = [ep for ep, t in self._due.items() if now >= t]
            if due_now:
                endpoint = min(due_now, key=lambda ep: self._due[ep])
                await self._poll(endpoint, handlers[endpoint])
            next_due = min(self._due.values())
            delay = max(0.05, min(next_due - time.time(), 1.0))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except TimeoutError:
                pass

    # ---------- polling core ----------

    def _base_interval(self, endpoint: str) -> float:
        if endpoint == "vitals":
            active = bool(self._prev_vitals and self._prev_vitals.get("vehicle_connected"))
            return self.cfg.vitals_interval_active if active else self.cfg.vitals_interval_idle
        if endpoint == "wifi":
            return self.cfg.wifi_interval
        if endpoint == "lifetime":
            return self.cfg.lifetime_interval
        return self.cfg.version_interval

    async def _poll(self, endpoint: str, handler) -> None:
        try:
            raw = await self.wc.api.async_request(endpoint if endpoint != "wifi" else "wifi_status")
        except (WallConnectorError, aiohttp.ClientError, OSError, asyncio.CancelledError) as ex:
            if isinstance(ex, asyncio.CancelledError):
                raise
            await self._on_poll_error(endpoint, ex)
            return
        ts = time.time()
        self.last_poll_ok_ts = ts
        self._fail_streak[endpoint] = 0
        self._due[endpoint] = ts + self._base_interval(endpoint)
        if self._offline:
            self._offline = False
            self.last_poll_error = None
            await asyncio.to_thread(self.db.clear_alert, ts, "Wall Connector unreachable", "monitor")
            await asyncio.to_thread(self.db.add_event, ts, "poll_recovered", {"endpoint": endpoint})
            self.bus.publish({"type": "event", "ts": ts, "kind": "poll_recovered", "detail": {"endpoint": endpoint}})
        try:
            await handler(ts, raw)
        except Exception:
            log.exception("handler for %s failed; raw=%s", endpoint, json.dumps(raw)[:500])

    async def _on_poll_error(self, endpoint: str, ex: Exception) -> None:
        ts = time.time()
        streak = self._fail_streak[endpoint] = self._fail_streak[endpoint] + 1
        base = self._base_interval(endpoint)
        backoff = min(base * (self.cfg.backoff_factor ** min(streak, 8)), self.cfg.backoff_max)
        self._due[endpoint] = ts + backoff
        self.last_poll_error = f"{type(ex).__name__}: {ex}"
        log.warning("poll %s failed (streak %d, retry in %.0fs): %s", endpoint, streak, backoff, ex)
        # Declare the device offline after 3 consecutive vitals failures.
        if endpoint == "vitals" and streak == 3 and not self._offline:
            self._offline = True
            await asyncio.to_thread(self.db.raise_alert, ts, "Wall Connector unreachable", "monitor")
            await asyncio.to_thread(self.db.add_event, ts, "poll_error", {"endpoint": endpoint, "error": str(ex)})
            self.bus.publish(
                {"type": "event", "ts": ts, "kind": "poll_error", "detail": {"endpoint": endpoint, "error": str(ex)}}
            )

    # ---------- handlers ----------

    async def _handle_vitals(self, ts: float, raw: dict) -> None:
        prev = self._prev_vitals
        power = _total_power(raw, self.cfg.split_phase)

        # Charger reboot detection: uptime went backwards.
        if prev is not None and isinstance(raw.get("uptime_s"), (int, float)) and isinstance(
            prev.get("uptime_s"), (int, float)
        ) and raw["uptime_s"] < prev["uptime_s"] - 5:
            await self._event(ts, "charger_reboot", {"uptime_before": prev["uptime_s"], "uptime_after": raw["uptime_s"]})

        connected = bool(raw.get("vehicle_connected"))
        was_connected = bool(prev.get("vehicle_connected")) if prev is not None else None

        # Session lifecycle
        if connected and self._session_id is None:
            self._session_id = await asyncio.to_thread(self.db.start_session, ts)
            await self._event(ts, "session_start", {"session_id": self._session_id})
        elif not connected and self._session_id is not None:
            sid = self._session_id
            self._session_id = None
            reason = "vehicle_disconnected" if was_connected else "not_connected_on_startup"
            await asyncio.to_thread(self.db.close_session, sid, ts, reason)
            await self._event(ts, "session_end", {"session_id": sid, "reason": reason})

        # Contactor transitions (actual charging on/off)
        if prev is not None and bool(raw.get("contactor_closed")) != bool(prev.get("contactor_closed")):
            kind = "charging_start" if raw.get("contactor_closed") else "charging_stop"
            await self._event(ts, kind, {"session_id": self._session_id})

        # EVSE state transitions
        if prev is not None and raw.get("evse_state") != prev.get("evse_state"):
            await self._event(
                ts, "evse_state_change", {"from": prev.get("evse_state"), "to": raw.get("evse_state")}
            )

        # Device alert diffing
        alerts_now = {str(a) for a in raw.get("current_alerts") or []}
        for alert in alerts_now - self._active_alerts:
            await asyncio.to_thread(self.db.raise_alert, ts, alert, "device")
            await self._event(ts, "alert_raised", {"alert": alert})
        for alert in self._active_alerts - alerts_now:
            await asyncio.to_thread(self.db.clear_alert, ts, alert, "device")
            await self._event(ts, "alert_cleared", {"alert": alert})
        self._active_alerts = alerts_now

        row_id = await asyncio.to_thread(self.db.insert_vitals, ts, raw, self._session_id, power)
        self._prev_vitals = raw
        self.bus.publish(
            {
                "type": "vitals",
                "ts": ts,
                "id": row_id,
                "session_id": self._session_id,
                "total_power_w": power,
                "data": raw,
            }
        )

    async def _handle_wifi(self, ts: float, raw: dict) -> None:
        prev = self._prev_wifi
        if prev is not None:
            if bool(raw.get("wifi_connected")) != bool(prev.get("wifi_connected")):
                kind = "wifi_reconnected" if raw.get("wifi_connected") else "wifi_disconnected"
                await self._event(ts, kind, {"ssid": raw.get("wifi_ssid")})
                if kind == "wifi_disconnected":
                    await asyncio.to_thread(self.db.raise_alert, ts, "Charger Wi-Fi disconnected", "wifi")
                else:
                    await asyncio.to_thread(self.db.clear_alert, ts, "Charger Wi-Fi disconnected", "wifi")
            if bool(raw.get("internet")) != bool(prev.get("internet")):
                await self._event(ts, "internet_restored" if raw.get("internet") else "internet_lost", None)
        # The device reports the SSID base64-encoded on most firmware; store
        # the decoded form in the column (the raw JSON keeps the original).
        try:
            ssid = WifiStatus(raw).wifi_ssid
        except KeyError:
            ssid = None
        await asyncio.to_thread(self.db.insert_wifi, ts, raw, ssid)
        self._prev_wifi = raw
        self.bus.publish({"type": "wifi", "ts": ts, "data": {**raw, "wifi_ssid_decoded": ssid}})

    async def _handle_lifetime(self, ts: float, raw: dict) -> None:
        await asyncio.to_thread(self.db.insert_lifetime, ts, raw)
        self.bus.publish({"type": "lifetime", "ts": ts, "data": raw})

    async def _handle_version(self, ts: float, raw: dict) -> None:
        latest = await asyncio.to_thread(self.db.latest_version)
        if latest is not None and latest.get("firmware_version") != raw.get("firmware_version"):
            await self._event(
                ts,
                "firmware_changed",
                {"from": latest.get("firmware_version"), "to": raw.get("firmware_version")},
            )
        if latest is None or json.loads(latest["raw"]) != raw:
            await asyncio.to_thread(self.db.insert_version, ts, raw)

    async def _event(self, ts: float, kind: str, detail: dict | None) -> None:
        await asyncio.to_thread(self.db.add_event, ts, kind, detail)
        self.bus.publish({"type": "event", "ts": ts, "kind": kind, "detail": detail})

    # ---------- status ----------

    def status(self) -> dict[str, Any]:
        return {
            "host": self.cfg.host,
            "offline": self._offline,
            "last_poll_ok_ts": self.last_poll_ok_ts,
            "last_poll_error": self.last_poll_error,
            "started_ts": self.started_ts,
            "session_id": self._session_id,
            "intervals": {
                "vitals_active": self.cfg.vitals_interval_active,
                "vitals_idle": self.cfg.vitals_interval_idle,
                "wifi": self.cfg.wifi_interval,
                "lifetime": self.cfg.lifetime_interval,
            },
        }
