"""Runtime configuration for wallmonitor.

All values come from CLI arguments with environment-variable fallbacks so the
app can run as a service. Poll cadence defaults are chosen to stay well within
what the Wall Connector Gen 3's small embedded web server handles reliably:
requests are always sequential (never concurrent), the vitals cadence only
tightens while a vehicle is attached, and repeated failures back the poller
off exponentially instead of hammering a struggling device.
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass


@dataclass
class Config:
    host: str
    port: int = 8480
    bind: str = "127.0.0.1"
    db_path: str = "wallmonitor.db"
    demo: bool = False
    split_phase: bool = False
    # Poll cadence (seconds). Vitals tighten while a vehicle is connected.
    vitals_interval_active: float = 2.0
    vitals_interval_idle: float = 5.0
    wifi_interval: float = 30.0
    lifetime_interval: float = 60.0
    version_interval: float = 6 * 3600.0
    request_timeout: float = 5.0
    # Error backoff
    backoff_factor: float = 1.6
    backoff_max: float = 60.0
    # Floor: never poll any endpoint faster than this, whatever the flags say.
    min_interval: float = 1.0

    def clamp(self) -> "Config":
        self.vitals_interval_active = max(self.min_interval, self.vitals_interval_active)
        self.vitals_interval_idle = max(self.min_interval, self.vitals_interval_idle)
        self.wifi_interval = max(self.min_interval, self.wifi_interval)
        self.lifetime_interval = max(self.min_interval, self.lifetime_interval)
        return self


def _env(name: str, default):
    val = os.getenv(name)
    if val is None:
        return default
    if isinstance(default, bool):
        return val.lower() in ("1", "true", "yes", "on")
    if isinstance(default, float):
        return float(val)
    if isinstance(default, int):
        return int(val)
    return val


def parse_args(argv: list[str] | None = None) -> Config:
    parser = argparse.ArgumentParser(
        prog="wallmonitor",
        description="Local-only monitoring UI for a Tesla Wall Connector Gen 3",
    )
    parser.add_argument(
        "--host",
        default=_env("WM_WC_HOST", ""),
        help="Hostname or IP of the Wall Connector on your LAN (env: WM_WC_HOST)",
    )
    parser.add_argument("--port", type=int, default=_env("WM_PORT", 8480), help="Web UI port (env: WM_PORT)")
    parser.add_argument(
        "--bind",
        default=_env("WM_BIND", "127.0.0.1"),
        help="Web UI bind address; default localhost only (env: WM_BIND)",
    )
    parser.add_argument(
        "--db", dest="db_path", default=_env("WM_DB", "wallmonitor.db"), help="SQLite database path (env: WM_DB)"
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        default=_env("WM_DEMO", False),
        help="Run against a built-in Wall Connector simulator instead of real hardware (env: WM_DEMO)",
    )
    parser.add_argument(
        "--vitals-active",
        type=float,
        default=_env("WM_VITALS_ACTIVE", 2.0),
        help="Vitals poll interval in seconds while a vehicle is connected (default 2)",
    )
    parser.add_argument(
        "--vitals-idle",
        type=float,
        default=_env("WM_VITALS_IDLE", 5.0),
        help="Vitals poll interval in seconds while idle (default 5)",
    )
    parser.add_argument("--wifi-interval", type=float, default=_env("WM_WIFI_INTERVAL", 30.0))
    parser.add_argument("--lifetime-interval", type=float, default=_env("WM_LIFETIME_INTERVAL", 60.0))
    parser.add_argument(
        "--split-phase",
        action="store_true",
        default=_env("WM_SPLIT_PHASE", False),
        help="Compute total power for a North American split-phase install (env: WM_SPLIT_PHASE)",
    )
    args = parser.parse_args(argv)

    if not args.demo and not args.host:
        parser.error("--host (or WM_WC_HOST) is required unless --demo is set")

    return Config(
        host=args.host,
        port=args.port,
        bind=args.bind,
        db_path=args.db_path,
        demo=bool(args.demo),
        split_phase=bool(args.split_phase),
        vitals_interval_active=args.vitals_active,
        vitals_interval_idle=args.vitals_idle,
        wifi_interval=args.wifi_interval,
        lifetime_interval=args.lifetime_interval,
    ).clamp()
