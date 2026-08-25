# Running wallmonitor

*[← back to the README](../README.md)*

## Run

```bash
git clone https://github.com/zebraengine/wallmonitor
cd wallmonitor
uv sync

# find your Wall Connector on the LAN (see "Finding your charger" below)…
uv run python -m wallmonitor --discover
# …then watch it:
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
| `--discover [RANGE]` | — | — | Sweep the LAN for Wall Connectors and exit (own subnet, or a private CIDR) |
| `--label` | `WM_LABEL` | — | Name for this charger, shown in the header, tab title and notifications |
| `--peer LABEL=URL` | `WM_PEERS` | — | Link another instance from the header switcher; repeatable (env: comma-separated) |
| `--retain-raw-days` | `WM_RETAIN_RAW_DAYS` | `0` (off) | Trim raw JSON from samples older than N days (min 7); columns stay forever |
| `--compact` | — | — | One-shot VACUUM to reclaim trimmed space; run with the service stopped |
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

## Finding your charger

The Gen 3 announces nothing on the network — no mDNS, no SSDP — and its
DHCP hostname (`TeslaWallConnector_XXXXXX`, same suffix as its setup Wi-Fi
SSID) lives only inside your router. So the tool finds it actively:

```bash
uv run python -m wallmonitor --discover              # this host's own /24
uv run python -m wallmonitor --discover 192.168.2.0/24   # another subnet or VLAN
```

It sweeps port 80 across the range with short timeouts, then sends each
responder exactly one tiny `GET /api/1/version` and keeps only answers
carrying the Wall Connector's signature (firmware, part number and serial
together) — no other device produces that, so there are no false
positives. A /24 takes a second or two. Guardrails: only private (RFC 1918)
ranges are ever swept, nothing wider than a /16 is accepted, and addresses
in the neighbour cache with a Tesla-assigned MAC prefix are probed first.

If the charger sits on a different subnet or VLAN from the machine running
the monitor, pass that range explicitly — the default sweep only covers the
host's own subnet. Discovery (and monitoring) is plain routed HTTP, so it
works across VLANs wherever the router forwards between them — but the
firewall must allow TCP port 80 from the monitor's network to the charger's.
IoT-isolation setups often block that by default; if an explicit range
still finds nothing, that rule is the first thing to check. Nothing found
on a flat network usually means the charger isn't on Wi-Fi yet: commission
it with the Tesla app first.

## Device identity

The database's thermal model, degradation trend and session history
describe one physical charger, so the monitor treats the **serial number**
as the device's identity — not its IP, which is just a DHCP lease. On first
contact it pins the serial it sees (logged as a `device_pinned` event); on
every start and every six hours after, it checks that the device at
`--host` still carries it, *before* recording anything.

If a different serial answers — a DHCP reshuffle swapped two devices, a
second unit was installed, a typo — the monitor raises the alert
**Different charger at this address**, notifies, and pauses recording until
the right charger is back (or `--host` is corrected). Blending another
charger's telemetry into a per-install history is the one unrecoverable
mistake, so it is the one refused outright. Moving the monitor to a new
charger on purpose means starting a new database (`--db`).

## More than one Wall Connector

Run one instance per charger, each with its own database, port and label,
and point them at each other with `--peer` so the header carries a switch:

```bash
uv run python -m wallmonitor --host 192.168.1.50 --label "Garage left"  --db left.db  --port 8480 \
    --peer "Garage right=http://192.168.1.10:8481"
uv run python -m wallmonitor --host 192.168.1.51 --label "Garage right" --db right.db --port 8481 \
    --peer "Garage left=http://192.168.1.10:8480"
```

The label shows in the header, the browser tab title and every
notification, so two dashboards (and two phones' worth of alerts) stay
attributable; the peer chips hop to the other charger's dashboard keeping
the same tab open. `--peer` is repeatable (or `WM_PEERS` as a comma-separated
list). Each instance keeps its own per-install thermal model, which is
exactly what you want: two chargers in two spots have two thermal
environments. `--discover` prints a ready-made command per device when it
finds more than one.

As services, `deploy/install-service.sh --name <name>` does the bookkeeping:
a unit per charger (`wallmonitor-<name>`), its own database (`<name>.db`),
the label, and any `--peer` links:

```bash
sudo ./deploy/install-service.sh --name left  --host 192.168.1.50 --port 8480 --split-phase \
    --peer "right=http://192.168.1.10:8481"
sudo ./deploy/install-service.sh --name right --host 192.168.1.51 --port 8481 --split-phase \
    --peer "left=http://192.168.1.10:8480"
```

A single-process, cross-device view is tracked as future work in issue #10.

## Retention

By default nothing is ever deleted: every sample keeps its complete raw
JSON forever, and the database grows by roughly a gigabyte a month on a
busy install (about 85 % of that is the raw blobs on vitals samples).

`--retain-raw-days N` caps that. Samples older than N days keep **every
extracted column** — charts, session pages, the thermal model, and the
degradation watch see identical history — but their raw JSON blob is
blanked by a daily background pass (chunked, so live polling never waits
more than a moment; vitals wait until the diagnostics backfill has
finished). What is given up is *re-interpretability*: extracting a field
that was never a column, the way the diagnostics columns themselves were
backfilled from raw, becomes impossible for trimmed rows. Forecast
snapshots and version info are never trimmed.

Freed pages are reused by new inserts, so the file stops growing rather
than shrinking. To hand the space back to the filesystem once, stop the
service and run:

```bash
uv run python -m wallmonitor --compact --db /path/to/wallmonitor.db
```

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
flags (`--port`, `--bind`, `--db`, `--demo`, `--user`, and `--name`/`--label`/`--peer`
for multi-charger installs); `--uninstall` removes
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
