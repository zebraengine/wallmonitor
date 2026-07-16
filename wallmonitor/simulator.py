"""A local Tesla Wall Connector Gen 3 simulator.

Serves the same four endpoints as the real device and walks through a
plausible charge cycle (idle → plug in → charge → complete → unplug) so the
monitor can be exercised end-to-end without hardware. Also reproduces the
device's JSON quirks (occasional literal ``nan``) so the library's workarounds
stay exercised.

Not a byte-perfect emulation — just realistic enough for demos and tests.
"""

from __future__ import annotations

import base64
import json
import math
import random
import time

from aiohttp import web

CYCLE = [
    # (phase name, duration seconds)
    ("idle", 40.0),
    ("connected", 15.0),
    ("charging", 240.0),
    ("complete", 60.0),
]


class SimState:
    def __init__(self, speedup: float = 1.0, start: float | None = None):
        self.t0 = start if start is not None else time.time()
        self.speedup = speedup
        self.boot_ts = self.t0
        self.lifetime_energy_wh = 2_566_837.0
        self.charge_starts = 450
        self.alert_after_cycles = 2  # inject a device alert on the Nth cycle
        self.rng = random.Random(7)

    def _elapsed(self) -> float:
        return (time.time() - self.t0) * self.speedup

    def phase(self) -> tuple[str, float, int]:
        """Current phase name, seconds into the phase, and cycle count."""
        total = sum(duration for _, duration in CYCLE)
        elapsed = self._elapsed()
        cycle_n = int(elapsed // total)
        into = elapsed % total
        for name, dur in CYCLE:
            if into < dur:
                return name, into, cycle_n
            into -= dur
        return "idle", 0.0, cycle_n

    def vitals(self) -> dict:
        name, into, cycle_n = self.phase()
        rng = self.rng
        grid_v = 230.0 + rng.uniform(-1.5, 1.5)
        grid_hz = 50.0 + rng.uniform(-0.02, 0.02)
        connected = name in ("connected", "charging", "complete")
        charging = name == "charging"
        # Ramp current up over the first 20s of charging, taper near the end.
        amps = 0.0
        if charging:
            ramp = min(1.0, into / 20.0)
            taper = 1.0 - max(0.0, (into - 200.0) / 40.0) * 0.6
            amps = 16.0 * ramp * taper + rng.uniform(-0.2, 0.2)
        if name == "connected":
            session_s = int(into)
        elif name == "charging":
            session_s = int(CYCLE[1][1] + into)
        elif name == "complete":
            session_s = int(CYCLE[1][1] + CYCLE[2][1] + into)
        else:
            session_s = 0
        # session energy: integrate the trapezoid roughly — good enough for a sim
        session_energy = 0.0
        if name == "charging":
            session_energy = 3 * 230.0 * 14.0 * (into / 3600.0)
        elif name == "complete":
            session_energy = 3 * 230.0 * 14.0 * (CYCLE[2][1] / 3600.0)
        alerts = []
        if cycle_n >= self.alert_after_cycles and name == "charging" and 60 < into < 120:
            # Real firmware reports numeric alert IDs (e.g. [27]).
            alerts = [27]
        evse_state = {"idle": 1, "connected": 4, "charging": 9, "complete": 11}[name]
        handle_temp = 20.0 + (12.0 * min(1.0, into / 120.0) if charging else 0.0) + rng.uniform(-0.3, 0.3)
        data = {
            "contactor_closed": charging,
            "vehicle_connected": connected,
            "session_s": session_s,
            "grid_v": round(grid_v, 1),
            "grid_hz": round(grid_hz, 3),
            "vehicle_current_a": round(amps, 1),
            "currentA_a": round(amps if charging else 0.1, 1),
            "currentB_a": round(amps if charging else 0.1, 1),
            "currentC_a": round(amps if charging else 0.1, 1),
            "currentN_a": round(rng.uniform(0.0, 0.4), 1),
            "voltageA_v": round(grid_v + rng.uniform(-0.5, 0.5), 1),
            "voltageB_v": round(grid_v + rng.uniform(-0.5, 0.5), 1),
            "voltageC_v": round(grid_v + rng.uniform(-0.5, 0.5), 1),
            "relay_coil_v": 11.9,
            "pcba_temp_c": round(18.0 + (8.0 if charging else 0.0) + rng.uniform(-0.4, 0.4), 1),
            "handle_temp_c": round(handle_temp, 1),
            "mcu_temp_c": round(24.0 + (10.0 if charging else 0.0) + rng.uniform(-0.4, 0.4), 1),
            "uptime_s": int((time.time() - self.boot_ts) * self.speedup),
            "input_thermopile_uv": -151,
            "prox_v": 1.5 if connected else 0.0,
            "pilot_high_v": 8.9 if charging else 11.9,
            "pilot_low_v": -11.9 if connected else 11.9,
            "session_energy_wh": round(session_energy, 1),
            "config_status": 5,
            "evse_state": evse_state,
            "current_alerts": alerts,
            "evse_not_ready_reasons": [] if charging else [1],
        }
        return data

    def lifetime(self) -> dict:
        _, _, cycle_n = self.phase()
        return {
            "contactor_cycles": 175 + cycle_n,
            "contactor_cycles_loaded": 3,
            "alert_count": 1603,
            "thermal_foldbacks": 0,
            "avg_startup_temp": 27.8,
            "charge_starts": self.charge_starts + cycle_n,
            "energy_wh": int(self.lifetime_energy_wh + cycle_n * 2500),
            "connector_cycles": 23 + cycle_n,
            "uptime_s": int((time.time() - self.boot_ts) * self.speedup),
            "charging_time_s": 183022 + cycle_n * int(CYCLE[2][1]),
        }

    def version(self) -> dict:
        return {
            "firmware_version": "24.36.3+gsimulated00",
            "git_branch": "HEAD",
            "part_number": "1529455-02-D",
            "serial_number": "SIM12345678901",
            "web_service": "h3-hermes-prd.sn.tesla.services",
        }

    def wifi_status(self) -> dict:
        # RSSI drifts slowly; dips periodically so the health chart has shape.
        elapsed = self._elapsed()
        rssi = -62 + int(6 * math.sin(elapsed / 90.0)) - (8 if int(elapsed) % 300 < 20 else 0)
        return {
            "wifi_ssid": base64.b64encode(b"HomeNetwork").decode(),
            "wifi_signal_strength": max(0, min(100, 2 * (rssi + 100))),
            "wifi_rssi": rssi,
            "wifi_snr": max(5, rssi + 88),
            "wifi_connected": True,
            "wifi_infra_ip": "127.0.0.1",
            "internet": int(elapsed) % 600 > 30,  # brief internet dropout every 10 sim-minutes
            "wifi_mac": "AA:BB:CC:DD:EE:FF",
        }


def make_app(state: SimState | None = None) -> web.Application:
    state = state or SimState()
    app = web.Application()

    def _json_response(data: dict, quirky: bool = False) -> web.Response:
        body = json.dumps(data)
        if quirky and state.rng.random() < 0.05:
            # The real device occasionally emits literal nan values.
            body = body.replace('"currentN_a": 0.0', '"currentN_a": nan')
        return web.Response(text=body, content_type="application/json")

    async def vitals(_request):
        return _json_response(state.vitals(), quirky=True)

    async def lifetime(_request):
        return _json_response(state.lifetime())

    async def version(_request):
        return _json_response(state.version())

    async def wifi_status(_request):
        return _json_response(state.wifi_status())

    app.router.add_get("/api/1/vitals", vitals)
    app.router.add_get("/api/1/lifetime", lifetime)
    app.router.add_get("/api/1/version", version)
    app.router.add_get("/api/1/wifi_status", wifi_status)
    return app


async def start_simulator(
    port: int = 0, speedup: float = 1.0, start: float | None = None
) -> tuple[web.AppRunner, int]:
    """Start the simulator on localhost. Returns (runner, bound_port)."""
    runner = web.AppRunner(make_app(SimState(speedup=speedup, start=start)))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    bound = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    return runner, bound
