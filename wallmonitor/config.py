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
import re
import os
from dataclasses import dataclass


@dataclass
class Config:
    """Everything the app runs with, resolved once by parse_args.

    Fields without CLI flags (request_timeout, the backoff pair,
    min_interval) are deliberately not user-tunable: they are the
    guardrails protecting the charger's small embedded web server."""

    host: str
    port: int = 8480
    bind: str = "127.0.0.1"
    db_path: str = "wallmonitor.db"
    demo: bool = False
    split_phase: bool = False
    # Human name for this charger, shown in the UI header, tab title and
    # notifications so several instances side by side stay attributable.
    label: str = ""
    # Sibling instances watching other chargers, as (label, url) pairs;
    # rendered as a switcher in the header so one browser hops between them.
    peers: tuple[tuple[str, str], ...] = ()
    # --discover [RANGE]: sweep the LAN for chargers and exit. None = not
    # requested; "auto" = the host's own subnet; otherwise a CIDR.
    discover: str | None = None
    # Optional LAN webhook: actionable warnings are POSTed here.
    # Local-only by design — point it at something on your own network
    # (Home Assistant, a self-hosted ntfy, node-RED); leave empty to disable.
    notify_url: str = ""
    # Payload shape for the webhook: "json" posts one JSON object; "ntfy"
    # posts plain-text with X-Title/X-Priority/X-Tags headers so notify_url
    # can be a ntfy topic (e.g. http://<lan-host>:8481/wallmonitor) directly.
    notify_format: str = "json"
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
        """Enforce the polling floor whatever the flags said; returns self."""
        self.vitals_interval_active = max(self.min_interval, self.vitals_interval_active)
        self.vitals_interval_idle = max(self.min_interval, self.vitals_interval_idle)
        self.wifi_interval = max(self.min_interval, self.wifi_interval)
        self.lifetime_interval = max(self.min_interval, self.lifetime_interval)
        return self


def _env(name: str, default):
    """Environment fallback for a CLI default, coerced to the default's type.

    The bool check must stay ahead of int: bool is an int subclass, so the
    int branch would otherwise catch bool defaults and int("true") would
    crash at import. Returning a real bool also keeps store_true flags
    honest — WM_DEMO=false must not become a truthy default."""
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


def parse_peer(spec: str) -> tuple[str, str]:
    """'Label=http://host:port' → (label, url). The URL must be absolute
    http(s); the label is whatever the user wants to see on the switch."""
    label, sep, url = spec.partition("=")
    label, url = label.strip(), url.strip()
    if not sep or not label or not re.match(r"^https?://[^\s/]+", url):
        raise ValueError(f"--peer expects LABEL=http://host:port, got {spec!r}")
    return label, url.rstrip("/")


def parse_args(argv: list[str] | None = None) -> Config:
    """Build the Config: CLI flag beats WM_* env var beats hardcoded default.

    One validation rule: --host is required unless --demo runs the built-in
    simulator instead of real hardware, or --discover is asked to find it. Note the boolean flags can only
    assert True — an env-var-enabled --demo/--split-phase has no --no-*
    override; unset the variable instead."""
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
        "--label",
        default=_env("WM_LABEL", ""),
        help="Name for this charger, shown in the UI and notifications — useful when "
        "running one instance per Wall Connector (env: WM_LABEL)",
    )
    parser.add_argument(
        "--peer",
        action="append",
        default=None,
        metavar="LABEL=URL",
        help="Another wallmonitor instance to link from the header, e.g. "
        "'Garage right=http://192.168.1.10:8481'; repeatable (env: WM_PEERS, comma-separated)",
    )
    parser.add_argument(
        "--discover",
        nargs="?",
        const="auto",
        default=None,
        metavar="RANGE",
        help="Find Wall Connectors on the LAN and exit. Sweeps this host's own subnet, "
        "or the private CIDR given (e.g. 192.168.2.0/24). Never leaves the LAN.",
    )
    parser.add_argument(
        "--split-phase",
        action="store_true",
        default=_env("WM_SPLIT_PHASE", False),
        help="Compute total power for a North American split-phase install (env: WM_SPLIT_PHASE)",
    )
    parser.add_argument(
        "--notify-url",
        default=_env("WM_NOTIFY_URL", ""),
        help="Optional LAN webhook that receives actionable warnings as POSTs, "
        "e.g. Home Assistant or a self-hosted ntfy on your own network (env: WM_NOTIFY_URL)",
    )
    parser.add_argument(
        "--notify-format",
        choices=("json", "ntfy"),
        default=_env("WM_NOTIFY_FORMAT", "json"),
        help="Webhook payload shape: 'json' for one JSON object per warning, 'ntfy' for "
        "plain-text + ntfy headers so --notify-url can be a ntfy topic (env: WM_NOTIFY_FORMAT)",
    )
    args = parser.parse_args(argv)

    peer_specs = args.peer if args.peer is not None else [
        part for part in os.getenv("WM_PEERS", "").split(",") if part.strip()
    ]
    try:
        peers = tuple(parse_peer(spec) for spec in peer_specs)
    except ValueError as ex:
        parser.error(str(ex))

    if not args.demo and not args.host and args.discover is None:
        parser.error(
            "--host (or WM_WC_HOST) is required — run `wallmonitor --discover` to find "
            "your Wall Connector's address, or --demo for the built-in simulator"
        )

    return Config(
        host=args.host,
        port=args.port,
        bind=args.bind,
        db_path=args.db_path,
        demo=bool(args.demo),
        split_phase=bool(args.split_phase),
        label=args.label,
        peers=peers,
        discover=args.discover,
        notify_url=args.notify_url,
        notify_format=args.notify_format,
        vitals_interval_active=args.vitals_active,
        vitals_interval_idle=args.vitals_idle,
        wifi_interval=args.wifi_interval,
        lifetime_interval=args.lifetime_interval,
    ).clamp()
