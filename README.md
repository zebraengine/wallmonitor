# wallmonitor

A **local-only** monitoring, recording, and review UI for a Tesla Wall Connector
Gen 3, built on the `tesla-wall-connector` library in the parent directory.

Everything stays on your machine and your LAN: the only network traffic is
HTTP GETs to the charger's local API, storage is a local SQLite file, and the
web UI serves no external assets (no CDNs, fonts, or analytics).

## What it does

1. **Records everything the charger reports, at the highest safe fidelity.**
   Vitals every 2 s while a vehicle is attached (5 s idle), Wi-Fi status every
   30 s, lifetime counters every 60 s, firmware info every 6 h. Requests are
   strictly sequential and back off exponentially on failures, so the charger's
   small embedded web server is never hammered. Every response is stored with
   its **complete raw JSON** alongside extracted columns — nothing is discarded.
2. **Charging session review** — every plug-in → unplug session is detected,
   aggregated (energy, peak/average power, charging time), listed, and
   drillable into full telemetry charts (power, per-phase current and voltage,
   temperatures) plus the events that happened during it.
3. **Wi-Fi health monitoring** — RSSI / SNR / signal-strength history,
   connect/disconnect and internet-reachability events.
4. **Live session review** — a live dashboard (Server-Sent Events) with
   current power, currents, session energy, EVSE state, and rolling charts.
5. **Alert & error monitoring** — device-reported `current_alerts` are diffed
   into raise/clear alert records with timestamps; charger reboots,
   unreachability, EVSE state changes, and Wi-Fi drops are all first-class
   events with an active-alert banner across every page.
6. **One synchronized clock** — every sample, session boundary, alert, and
   event is stamped with the host's UTC time the moment it was observed, and
   rendered in your local timezone by one shared formatter, so you can line up
   any error with the exact operating conditions around it. The charger's own
   `uptime_s` is stored with each sample for device-side cross-reference.

## Run

```bash
cd monitor
uv sync

# against your real Wall Connector (find its IP in your router, or use
# the TeslaWallConnector_XXXXXX.local hostname):
uv run python -m wallmonitor --host 192.168.1.50

# North American split-phase install (power = grid_v × vehicle_current):
uv run python -m wallmonitor --host 192.168.1.50 --split-phase

# no hardware? demo mode runs a built-in simulator:
uv run python -m wallmonitor --demo
```

Then open <http://127.0.0.1:8480>. The UI binds to localhost by default; use
`--bind 0.0.0.0` to reach it from other devices on your LAN.

Data lands in `wallmonitor.db` (override with `--db /path/to.db`). Back that
one file up and you have your full history.

## Options

| Flag | Env | Default | Meaning |
|---|---|---|---|
| `--host` | `WM_WC_HOST` | — | Wall Connector IP/hostname (required unless `--demo`) |
| `--port` | `WM_PORT` | `8480` | Web UI port |
| `--bind` | `WM_BIND` | `127.0.0.1` | Web UI bind address |
| `--db` | `WM_DB` | `wallmonitor.db` | SQLite path |
| `--split-phase` | `WM_SPLIT_PHASE` | off | Split-phase total-power calculation |
| `--vitals-active` | `WM_VITALS_ACTIVE` | `2.0` | Vitals poll seconds, vehicle attached |
| `--vitals-idle` | `WM_VITALS_IDLE` | `5.0` | Vitals poll seconds, idle |
| `--wifi-interval` | `WM_WIFI_INTERVAL` | `30` | Wi-Fi status poll seconds |
| `--lifetime-interval` | `WM_LIFETIME_INTERVAL` | `60` | Lifetime counters poll seconds |
| `--demo` | `WM_DEMO` | off | Run against the built-in simulator |

A hard floor of 1 s per endpoint is enforced regardless of flags.

## Tests

```bash
uv run pytest
```

The suite runs the full pipeline against the simulator: polling, session
detection, alert lifecycle, backoff when unreachable, and the web API.
