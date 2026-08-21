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
    """The simulated world, derived purely from the wall clock.

    Phase comes from elapsed-time-modulo-cycle (× speedup), so there is no
    background task to run or state to advance — every request computes the
    world as of "now", and a test can pin `start` to make it deterministic."""

    def __init__(
        self, speedup: float = 1.0, start: float | None = None, split_phase: bool = False,
        serial: str = "SIM12345678901",
    ):
        self.t0 = start if start is not None else time.time()
        self.speedup = speedup
        self.split_phase = split_phase
        self.serial = serial
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
        """Synthesize a /api/1/vitals body for the current phase: current
        ramps in and tapers, the handle warms while charging, and a device
        alert appears mid-charge from the Nth cycle on so alert plumbing
        gets exercised without waiting for real trouble."""
        name, into, cycle_n = self.phase()
        rng = self.rng
        # Grid profile: European three-phase 230 V / 50 Hz by default, or a
        # North American split-phase 240 V / 60 Hz install (nominal sags a
        # few volts under load, matching recorded Gen 3 telemetry).
        if self.split_phase:
            grid_v = 236.0 + rng.uniform(-1.5, 1.5)
            grid_hz = 60.0 + rng.uniform(-0.03, 0.03)
            max_amps = 48.0
        else:
            grid_v = 230.0 + rng.uniform(-1.5, 1.5)
            grid_hz = 50.0 + rng.uniform(-0.02, 0.02)
            max_amps = 16.0
        connected = name in ("connected", "charging", "complete")
        charging = name == "charging"
        # Ramp current up over the first 20s of charging, taper near the end.
        amps = 0.0
        if charging:
            ramp = min(1.0, into / 20.0)
            taper = 1.0 - max(0.0, (into - 200.0) / 40.0) * 0.6
            amps = max_amps * ramp * taper + rng.uniform(-0.2, 0.2)
            if self.split_phase:
                grid_v -= 6.5 * (amps / max_amps)  # voltage sag under load
        if name == "connected":
            session_s = int(into)
        elif name == "charging":
            session_s = int(CYCLE[1][1] + into)
        elif name == "complete":
            session_s = int(CYCLE[1][1] + CYCLE[2][1] + into)
        else:
            session_s = 0
        # session energy: integrate the trapezoid roughly — good enough for a sim
        avg_power_w = 230.0 * 43.0 if self.split_phase else 3 * 230.0 * 14.0
        session_energy = 0.0
        if name == "charging":
            session_energy = avg_power_w * (into / 3600.0)
        elif name == "complete":
            session_energy = avg_power_w * (CYCLE[2][1] / 3600.0)
        alerts = []
        if cycle_n >= self.alert_after_cycles and name == "charging" and 60 < into < 120:
            # Real firmware reports numeric alert IDs (e.g. [27]).
            alerts = [27]
        # Telemetry-verified states: 1 no vehicle, 4 connected idle,
        # 11 charging (contactor closed), 9 connected not charging.
        evse_state = {"idle": 1, "connected": 4, "charging": 11, "complete": 9}[name]
        handle_temp = 20.0 + (12.0 * min(1.0, into / 120.0) if charging else 0.0) + rng.uniform(-0.3, 0.3)
        data = {
            "contactor_closed": charging,
            "vehicle_connected": connected,
            "session_s": session_s,
            "grid_v": round(grid_v, 1),
            "grid_hz": round(grid_hz, 3),
            "vehicle_current_a": round(amps, 1),
            # Split-phase Gen 3 telemetry is odd and reproduced from a real
            # install's recording: each leg (and neutral) reads roughly half
            # the vehicle current, and voltageC sits near half of grid_v.
            # Summing V×I here would be wrong — that's what --split-phase's
            # grid_v × vehicle_current power path exists to handle.
            "currentA_a": round((amps * 0.46 if self.split_phase else amps) if charging else 0.1, 1),
            "currentB_a": round((amps * 0.54 if self.split_phase else amps) if charging else 0.1, 1),
            "currentC_a": round((amps * 0.53 if self.split_phase else amps) if charging else 0.1, 1),
            "currentN_a": round(amps * 0.46 if self.split_phase and charging else rng.uniform(0.0, 0.4), 1),
            "voltageA_v": round(grid_v + (3.5 if self.split_phase else 0.0) + rng.uniform(-0.5, 0.5), 1),
            "voltageB_v": round(grid_v + (3.9 if self.split_phase else 0.0) + rng.uniform(-0.5, 0.5), 1),
            "voltageC_v": round(grid_v / 2.0 if self.split_phase else grid_v + rng.uniform(-0.5, 0.5), 1),
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
            # Real firmware reports [1] whenever a vehicle is connected (even
            # while charging) and [4, 8] when unplugged.
            "evse_not_ready_reasons": [1] if connected else [4, 8],
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
            "serial_number": self.serial,
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
    """The four real device endpoints; vitals additionally reproduces the
    firmware's occasional literal-nan JSON quirk."""
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
    port: int = 0, speedup: float = 1.0, start: float | None = None, split_phase: bool = False,
    serial: str = "SIM12345678901",
) -> tuple[web.AppRunner, int]:
    """Start the simulator on localhost. Returns (runner, bound_port)."""
    state = SimState(speedup=speedup, start=start, split_phase=split_phase, serial=serial)
    runner = web.AppRunner(make_app(state))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    bound = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    return runner, bound
