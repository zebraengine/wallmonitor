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
   events with an active-alert banner across every page. Alert codes are run
   through `wallmonitor/alert_codes.json` — Tesla doesn't document the numeric
   codes the local API reports, so only verified entries are labeled; unknown
   codes render honestly with guidance (the Tesla app names active alerts —
   confirm a code there, then add it to the JSON). The Alerts page includes
   Tesla's official LED fault categories as a reference. The same
   verify-before-label policy covers EVSE state names: the two states that
   matter most (9 and 11) are named from telemetry cross-checked against
   contactor and power — the community-circulated names had them swapped —
   and the UI marks every state label as verified or community-reported.
6. **Thermal derate forecast** — the Gen 3 raises alert 40 ("high temperature
   detected") when its plug-handle sensor hits 65 °C, halving charge current
   for the rest of the session. The handle warms along a first-order lag whose
   parameters (`wallmonitor/thermal.py`) are fitted per-install from your own
   recorded session ramps (with defaults from a telemetry-verified alert-40
   event), and the idle handle sits ~2 °C above ambient, so the charger doubles
   as its own thermometer. The Live page forecasts: during charging, whether
   and when the current session will derate (from the handle's live
   trajectory); when idle, the estimated ambient and whether a full-rate
   charge started now would trip. When a derate is coming it also suggests
   the highest vehicle charge-current cap that stays under the limit —
   a steady capped rate charges faster than full rate folding back to 50%.
   The same per-session fits feed a **degradation watch**: rising heat at
   unchanged current means added resistance (loose lug, degrading contact),
   so when recent sessions' fitted rise climbs past the baseline the poller
   raises a monitor alert and the Alerts page charts the per-session trend.
   During cool-down — after a current cut or a derate — the forecast reports
   the true lower equilibrium the handle is settling toward ("recovering",
   not "tripping"). **Field-validated live:** steering the vehicle's charge
   current down on the forecast's advice kept a session 0.7 °C under the
   trip point, and in a deliberate full-rate test the trajectory forecast
   predicted the actual alert-40 raise to within seconds. `/api/thermal`
   returns the fitted model, the live forecast, every per-session fit, and
   the drift verdict.
7. **One synchronized clock** — every sample, session boundary, alert, and
   event is stamped with the host's UTC time the moment it was observed, and
   rendered in your local timezone by one shared formatter, so you can line up
   any error with the exact operating conditions around it. The charger's own
   `uptime_s` is stored with each sample for device-side cross-reference.

## Data handling & resilience

- **Storage** is a single SQLite file (`wallmonitor.db`, WAL mode) next to
  where you run the app, or wherever `--db` points. Back up that one file and
  you have your complete history. There is no retention limit; expect very
  roughly 10–25 MB/day depending on how often a vehicle is attached. The
  charger itself keeps no history — the monitor's database *is* the history,
  starting from the first time it runs.
- **Restarts are seamless.** On startup the poller reopens a still-open
  charging session if the vehicle stayed plugged in, closes it out if the
  vehicle left while the monitor was down, and clears any stale
  "unreachable" alert from a previous run.
- **Downtime is recorded, not hidden.** A graceful shutdown writes a
  `monitor_stop` event; after a hard stop (power loss, host reboot for
  updates), the next start compares the clock against the last recorded
  activity and writes an explicit `monitor_gap` event with the exact window
  and duration. A quiet stretch in the timeline is therefore always
  distinguishable from an unmonitored one — useful when the app runs on an
  always-on box that reboots itself periodically.
- **Sensor glitches are quarantined.** Gen 3 firmware reports **255 (0xFF)**
  for a temperature when a sensor read is momentarily invalid (commonly the
  handle thermistor during connector state transitions). The raw JSON keeps
  the sentinel for fidelity, but every interpreted surface — charts, live
  tiles, and downsampled averages — treats ≥255 °C as "no reading" so a
  phantom 255 °C spike (or a poisoned bucket average) never appears.
- **Recorded event kinds:** session start/end, charging start/stop, EVSE
  state changes (states 9/11 named from telemetry verified against contactor
  and power — the community-reported names for those two are swapped — the
  rest community-reported), device alerts
  raised/cleared, charger reboots (uptime went backwards), charger
  unreachable/recovered, Wi-Fi disconnect/reconnect, internet lost/restored,
  firmware version changes, and monitor start/stop/gap.

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
| `--demo` | `WM_DEMO` | off | Run against the built-in simulator |

A hard floor of 1 s per endpoint is enforced regardless of flags.

## Run as a service (Ubuntu / systemd)

For an always-on box, `deploy/install-service.sh` installs wallmonitor as a
systemd service that starts on boot, restarts on failure, and survives
unattended OS updates (any downtime shows up as a `monitor_gap` event in the
timeline):

```bash
cd monitor
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
