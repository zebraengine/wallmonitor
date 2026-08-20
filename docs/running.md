# Running wallmonitor

*[← back to the README](../README.md)*

## Run

```bash
git clone https://github.com/zebraengine/wallmonitor
cd wallmonitor
uv sync

# against your real Wall Connector: find its IP in your router's client
# list — it registers a DHCP hostname like TeslaWallConnector_XXXXXX
# (same suffix as its setup Wi-Fi SSID), which some routers make
# resolvable by name. The device does not answer mDNS, so a bare
# ".local" name won't work; when in doubt, use the IP:
uv run python -m wallmonitor --host 192.168.1.50

# North American split-phase install (power = grid_v × vehicle_current):
uv run python -m wallmonitor --host 192.168.1.50 --split-phase

# no hardware? demo mode runs a built-in simulator:
uv run python -m wallmonitor --demo

# the simulator honors --split-phase too: a 240 V / 60 Hz North American
# install (48 A, with the Gen 3's odd split-phase per-leg telemetry)
# instead of the European 230 V / 50 Hz three-phase default:
uv run python -m wallmonitor --demo --split-phase
```

Then open <http://127.0.0.1:8480>. The UI binds to localhost by default; use
`--bind 0.0.0.0` to reach it from other devices on your LAN.

Data lands in `wallmonitor.db` (override with `--db /path/to.db`). Back that
one file up and you have your full history.

The UI shows charger internals in plain language: "plug handle" is the
connector that goes into the car (the temperature that matters for derating),
"circuit board (PCBA)" is the main electronics board, and "processor (MCU)"
is the charger's microcontroller.

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
| `--notify-url` | `WM_NOTIFY_URL` | — | LAN webhook that receives actionable warnings |
| `--notify-format` | `WM_NOTIFY_FORMAT` | `json` | Webhook payload: `json` object, or `ntfy` (plain text + ntfy headers) |
| `--demo` | `WM_DEMO` | off | Run against the built-in simulator |

A hard floor of 1 s per endpoint is enforced regardless of flags.

## Run as a service (Ubuntu / systemd)

For an always-on box, `deploy/install-service.sh` installs wallmonitor as a
systemd service that starts on boot, restarts on failure, and survives
unattended OS updates (any downtime shows up as a `monitor_gap` event in the
timeline):

```bash
cd wallmonitor
sudo ./deploy/install-service.sh --host 192.168.1.50 --split-phase
```

The service runs as your (non-root) user with `--bind 0.0.0.0` by default so
other machines your firewall permits can reach the UI. Options mirror the app
flags (`--port`, `--bind`, `--db`, `--demo`, `--user`); `--uninstall` removes
the service and leaves code and database untouched. Requires
[uv](https://docs.astral.sh/uv/) installed for the service user.

Check on it with `systemctl status wallmonitor` or follow logs with
`journalctl -u wallmonitor -f`.

There is no authentication in the app — your firewall (e.g. UniFi zone
policies) is the access control for the UI.

## Tests

```bash
uv run pytest
```

The suite runs the full pipeline against the simulator: polling, session
detection, alert lifecycle, backoff when unreachable, and the web API — plus
the thermal model end to end: fit recovery from synthetic session ramps,
trip-time prediction, cool-down and recovery handling, the suggested-cap
math, drift detection, and regressions seeded from real recorded session
shapes (current ramp-up, mid-session derate).
