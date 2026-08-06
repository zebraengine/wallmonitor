# wallmonitor

A **local-only** companion for the Tesla Wall Connector Gen 3 that grew well
beyond reading data off the charger: it records everything the device
reports, turns that history into a thermal model fitted to *your* install,
forecasts derates before they happen (with the charge-current cap that
avoids them), watches for connector degradation with honest statistics, and
pushes actionable warnings to your browser or phone. Built on the
`tesla-wall-connector` library in the parent directory.

Everything stays on your machine and your LAN: the only network traffic is
HTTP GETs to the charger's local API, storage is a local SQLite file, and the
web UI serves no external assets (no CDNs, fonts, or analytics).

**At a glance:**

- Full-fidelity recording — every response stored with its complete raw JSON
- Live dashboard (SSE) with rolling charts and an active-alert banner,
  including a live derate-forecast chart: the measured handle temperature
  against the model's projected plateau and the trip threshold, updated with
  every 30 s forecast tick — the same values the amp controller acts on
- Session review: energy, peak/average power, per-phase telemetry, drillable charts
- Event timeline with range presets, category filters, and paging
- Alert decoding and EVSE-state labels, each marked verified vs community-reported
- Wi-Fi health history and connectivity events
- **Thermal derate forecast** — a per-install fitted model predicts alert 40
  live and suggests the highest current that avoids the 50% foldback
- **Automatic derate prevention** — an optional daemon caps the vehicle's
  charge current through a least-privilege BLE pairing when the forecast
  firms up, and restores it once the risk clears
- **Degradation watch** — ambient-corrected heat-rise trend with confidence
  intervals and a verified-baseline anchor, so "getting worse" is a
  statistical claim, not a vibe
- **Actionable notifications, local-only** — browser push and LAN
  webhook/self-hosted ntfy for phones, with a systemd + Docker deploy recipe
- Resilience: seamless restarts, downtime recorded as explicit gap events,
  sensor-glitch quarantine

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
   events with an active-alert banner across every page. The event timeline
   has its own range presets, per-category filters (charging, EVSE state,
   alerts & thermal, connectivity, monitor), and incremental paging, so the
   page stays fast as history grows instead of rendering thousands of rows. Alert codes are run
   through `wallmonitor/alert_codes.json` — Tesla doesn't document the numeric
   codes the local API reports, so only verified entries are labeled; unknown
   codes render honestly with guidance (the Tesla app names active alerts —
   confirm a code there, then add it to the JSON). The Alerts page includes
   Tesla's official LED fault categories as a reference. The same
   verify-before-label policy covers EVSE state names: states 1, 4, 9, and 11
   are named from telemetry cross-checked against vehicle presence, contactor,
   and power (the community-circulated names had 9 and 11 swapped), and the
   UI marks every state label as verified or community-reported.
6. **Thermal derate forecast** — the Gen 3 raises alert 40 ("high temperature
   detected") when its plug-handle sensor hits 65 °C, halving charge current
   for the rest of the session. The handle warms along a first-order lag whose
   parameters (`wallmonitor/thermal.py`) are fitted per-install from your own
   recorded charging ramps (with defaults from a telemetry-verified alert-40
   event), and the idle handle sits ~2 °C above ambient, so the charger doubles
   as its own thermometer. Fitting is per charging **segment**, not per
   session: one plug-in routinely contains several distinct draws hours
   apart — the vehicle's own state-of-charge top-offs, scheduled-departure
   preconditioning, or a charging schedule (common with time-of-use rates or
   home batteries). The charger reports no "scheduled charging" state for any
   of these (telemetry-verified: with a vehicle-side schedule armed overnight
   it idles in ordinary connected states until the car draws), so the fitter
   finds each segment's opening ramp wherever it occurs in the session and
   lets the quality gates decide what teaches the model — no configuration or
   "monitoring mode" needed. The unit of thermal analysis is the **load
   window** — the stretch where current actually flows — and ambient is a
   **bracket, not a point**: read at the window's start from the flat idle
   stretch before it (or, when a segment starts on a still-warm handle —
   stop/resume, a post-derate resume — from the previous charge's
   **cool-down tail** extrapolated to its asymptote at the install's fitted
   τ, so exactly the hardest-working segments aren't the ones excluded from
   degradation tracking), and read again at the window's end from the
   charge's own cool-down tail. When both ends read, the fit is de-trended
   against the ambient ramp between them: a garage that warms 3 °C during an
   afternoon charge (or cools overnight) is measured and removed instead of
   masquerading as connector resistance — a single start-of-window ambient
   silently assumes the weather held still for the whole charge, and a
   baseline recorded in one season would otherwise bias every comparison
   that follows. Fits that could only read one end fall back to the point
   ambient and say so. The Live page forecasts: during charging, whether
   and when the current session will derate (from the handle's live
   trajectory); when idle, the estimated ambient and whether a full-rate
   charge started now would trip. When a derate is coming it also suggests
   the highest vehicle charge-current cap that stays under the limit —
   a steady capped rate charges faster than full rate folding back to 50%.
   During cool-down — after a current cut or a derate — the forecast reports
   the true lower equilibrium the handle is settling toward ("recovering",
   not "tripping"). When a mid-session current change resets the live
   trajectory window, or sessions run back-to-back with no idle gap to read
   ambient from, the forecast bridges with ambient inferred from the newest
   steady run still in the buffer instead of going dark.
   **Field-validated live:** steering the vehicle's charge
   current down on the forecast's advice kept a session 0.7 °C under the
   trip point, and in a deliberate full-rate test the trajectory forecast
   predicted the actual alert-40 raise to within seconds. `/api/thermal`
   returns the fitted model, the live forecast, every per-segment fit, and
   the drift verdict.
7. **Degradation watch** — the same per-segment fits feed a trend: rising heat at
   unchanged current means added resistance (loose lug, degrading contact),
   so when recent segments' fitted rise climbs past the baseline the poller
   raises a monitor alert and the Alerts page charts the fitted-rise trend.
   The watch compares only sessions near the install's *recent* operating
   current: cap the vehicle at a new amperage and the watch follows,
   rather than judging forever against a current the install no longer
   uses; ambient-bracketed fits are clean enough under the I² normalization
   to pool in from a wider current band, so a baseline recorded at 48 A
   keeps judging charges after a cap to 40 A instead of the verdict going
   dark. The verdict also **carries its own uncertainty**: per-side spread
   (MAD) and a small-sample Student-t ~95% confidence interval on the
   delta, with a separate `confident` flag — the alert threshold is a
   tripwire, and the UI and notifications distinguish "statistically
   confirmed" from "a lead from a four-fit baseline". And because a
   baseline is only as meaningful as the hardware behind it, a
   **verified-baseline anchor** (button on the Alerts page, or
   `POST /api/thermal/baseline-anchor`) excludes all fits recorded before a
   hardware inspection: from then on the comparison means "vs verified
   healthy", not "vs the first charges the monitor happened to see".
   A **rise-vs-ambient scatter** on the same page separates the remaining
   confounder the fits can't remove: ambient is subtracted per fit, so a
   healthy install shows a flat cloud regardless of garage temperature — a
   cloud still sloping upward with ambient exposes an environment effect
   the model doesn't carry (multi-day heat soak of cable and structure in
   an uninsulated garage), while an elevated-but-flat cloud is the genuine
   added-resistance signature.
8. **Actionable warnings, local-only** — events a user can actually act on
   are pushed, not just logged: a **predicted derate** while there is still
   time to intercede (with the computed highest charge current that avoids
   the trip — capping the vehicle beats the charger's blunt 50% foldback),
   device alerts as they raise, the degradation watch's inspect-wiring
   warning, and charger-unreachable. Two delivery paths, both consistent
   with the nothing-phones-home rule: **browser notifications** from any
   open dashboard tab (fed by the existing SSE stream — no push service,
   no external traffic; enable via the bell in the header — browsers only
   permit notifications on HTTPS or localhost, so over plain http:// on a
   LAN IP the bell explains itself and you need an SSH tunnel to localhost
   or an HTTPS front-end), and an optional
   **LAN webhook** (`--notify-url` / `WM_NOTIFY_URL`) that POSTs each
   warning as JSON to an endpoint on your own network — Home Assistant, a
   self-hosted ntfy, Node-RED — for warnings while no dashboard is open.
9. **One synchronized clock** — every sample, session boundary, alert, and
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
  state changes (states 1/4/9/11 named from telemetry verified against
  vehicle presence, contactor, and power — the community-reported names for
  9/11 are swapped, and 7 "Error" is contradicted: it only appears as a
  benign plug-in transient — the rest community-reported), device alerts
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

## Phone alerts via self-hosted ntfy

The actionable warnings (predicted derate with a suggested current cap,
device alerts, drift, charger unreachable) can reach a phone through a
[ntfy](https://ntfy.sh) server you run yourself — message content never
leaves your LAN. `deploy/ntfy/docker-compose.yml` runs it next to the
monitor:

```bash
cd monitor/deploy/ntfy
# edit NTFY_BASE_URL in docker-compose.yml to this box's LAN address first
docker compose up -d
cd ..
sudo ./install-service.sh --host <wall-connector-ip> [your other flags] \
  --notify-url http://127.0.0.1:8481/wallmonitor --notify-format ntfy
```

On the phone, install the ntfy app, point it at `http://<box-lan-ip>:8481`
as the default server, and subscribe to the `wallmonitor` topic. Warnings
arrive prioritized (a predicted derate is *urgent* — it's the one you can
act on in the moment by lowering the vehicle's charge current).

iOS caveat, stated plainly: Apple only delivers instant background pushes
through its own push service, so a purely self-hosted server means the iOS
app refreshes on open or periodically instead of instantly. Uncommenting
`NTFY_UPSTREAM_BASE_URL: https://ntfy.sh` in the compose file restores
instant delivery by sending **only a wake-up ping** ("check your server")
through ntfy.sh and Apple — the message content is still fetched from your
own box over the LAN. That's the closest iOS gets to local-only push;
Android needs no upstream at all. Off the home network, a VPN into your
LAN (e.g. WireGuard on the router) keeps everything reachable without
exposing anything.

## Optional: garage ambient sensor

The thermal model normally uses the charger as its own thermometer (idle
handle ≈ ambient + 2 °C) — workable, but it rests on that offset assumption,
goes blind while the handle is warm, and can't separate multi-day heat soak
from real ambient. A cheap Wi-Fi sensor in the garage removes all three
limits: wallmonitor accepts readings at **`POST /api/ambient`** and, whenever
samples cover a window, prefers measured air temperature over every
handle-derived estimate — in per-segment fits (`ambient_source: "measured"`),
the live forecast, and the idle ambient tile. No configuration: the sensor
can appear, disappear, or never exist, and every path falls back to the
handle proxy.

Two dialects are accepted:

- **Ecowitt gateway (GW1100/GW1200)** — in the gateway's local web UI, set
  *Customized Upload* to protocol "Ecowitt", server = the wallmonitor host,
  port `8480`, path `/api/ambient`, interval 60 s. The gateway then POSTs
  its readings (°F/inHg, converted on ingest; its `PASSKEY` is dropped, not
  stored) over the LAN. Never configure the ecowitt.net upload and nothing
  leaves your network.
- **Plain JSON** — anything that can hit a URL:
  `curl -X POST http://<host>:8480/api/ambient -H 'Content-Type: application/json' -d '{"temp_c": 31.1, "humidity_pct": 55}'`
  (Shelly "Actions", Home Assistant automations, a cron job).

Every sample is tagged with its origin (`ecowitt` for the gateway dialect;
JSON callers may pass `"source": "..."`, default `json`). One tag changes
behavior: **`"car"`** marks a mobile sensor — an EV parked in the garage
reporting its own ambient reading (e.g. bridged from TeslaMate). A car is a
real garage thermometer most of the night, but it drives away and its sensor
reads high for a while after a drive, so car samples rank between the
stationary sensor and the handle proxy: they are used only when no
stationary sample covers the window, and the UI labels them distinctly
(`ambient_source: "measured_car"`, "car sensor"). A stationary sample wins
even when a car sample is newer. This means you can feed a vehicle bridge
today and add a dedicated sensor later — the sensor takes precedence
automatically, and the car demotes to backup with no reconfiguration.

`GET /api/ambient` returns recent samples and the latest reading. Humidity
and barometric pressure are stored when provided.

**Where you put the sensor is a correctness concern, not just an
installation detail.** Once it reports, its readings outrank everything else
and the model trusts them completely — and the model's premise is
`handle_temp = ambient + rise`. A sensor close enough to the connector to be
warmed by it reports an ambient that climbs during a charge, cancelling part
of the very rise the model exists to measure. That under-predicts derates
*and* makes a degrading install look healthy, so the failure hides a problem
rather than inventing one. Site it beside or below the cable run rather than
above it (a hot handle's convective plume rises), a few feet of horizontal
offset, close enough to share the connector's air but not to be heated by
it, and mounted with an air gap on a low-mass surface rather than pressed
against thermal mass.

`contrib/check_ambient_contamination.py` verifies this from recorded data:

```bash
uv run python contrib/check_ambient_contamination.py --db /path/to/a/copy/of/wallmonitor.db
```

A positive ambient drift during a charge is *not* on its own evidence of
coupling — an uninsulated garage genuinely warms over an afternoon charge,
and a naive correlation gives a false positive because ambient and handle
both rise together on a hot day. The script instead detrends ambient across
each segment and correlates the *residual* against handle temperature
(removing slow weather trends while preserving minute-scale tracking), and
compares the ambient slope during the charge against the slope just after it
stops — a contaminated sensor cools when the heat source goes away, while a
diurnal trend does not reverse on cue.

When the sensor also reports humidity, a third check separates charger heat
from arrived air: the charger warms air without adding water vapor, so a
coupled bump lifts temperature while the dew point holds flat (relative
humidity falls to compensate), whereas a bump of new air — an opened garage
door, a weather front — carries its dew point with it. Constant humidity
makes the dew point inherit nearly every temperature move, so sensor noise
degrades this check toward "arrived air", never toward a false accusation
of coupling.

### Bridging a TeslaMate vehicle

If [TeslaMate](https://github.com/teslamate-org/teslamate) runs on the same
host, the vehicle parked in your garage is already a logged thermometer:
TeslaMate records the car's outside-temperature sensor every few minutes
while it is awake. `contrib/teslamate_ambient_bridge.py` (stdlib-only)
reads the newest value straight from TeslaMate's Postgres via `docker
exec` — nothing about the TeslaMate stack changes — and POSTs it to
`/api/ambient` as `source: "car"`.

```bash
sudo ./deploy/install-teslamate-bridge.sh --car-id 1 --geofence Home
# or, with no TeslaMate geofence configured (coordinates stay in the local
# systemd unit — never commit them):
sudo ./deploy/install-teslamate-bridge.sh --car-id 1 --home-lat 39.2 --home-lon -77.3
# or zero-config: only posts while a vehicle is plugged into the charger
sudo ./deploy/install-teslamate-bridge.sh --car-id 1
```

The bridge's job is mostly to stay silent. It posts nothing while the car
is away from home, asleep (readings go stale), driving, or within 45
minutes of a drive ending (the sensor housing heat-soaks on the road and
reads high for a while). Silence is safe by design: wallmonitor's
freshness window expires and every consumer falls back to the handle
proxy — and once a stationary sensor reports, its samples outrank the
car's anyway. `journalctl -u teslamate-ambient-bridge` shows each run's
decision and reason.

## Optional: automatic derate prevention (BLE amp control)

The thermal forecast can suggest a lower charge current, but doing anything
about it needs a way to talk to the vehicle. `contrib/derate_amp_control.py`
(stdlib-only) polls `/api/thermal` and, when the forecast firms up, caps the
charge current through an ESP32 running
[esphome-tesla-ble](https://github.com/yoziru/esphome-tesla-ble) — paired
with the **least-privilege `CHARGING_MANAGER` role**, so the key can adjust
charge current and nothing else. No cloud API, no Fleet API, nothing leaves
the LAN.

```bash
sudo ./deploy/install-derate-amp-control.sh --tesla-ble http://<esp32-host>
# dry-run first: prints every decision without touching the charger
sudo ./deploy/install-derate-amp-control.sh --tesla-ble http://<esp32-host> --dry-run
```

`/api/thermal`'s forecast gets more precise as a session runs: `hypothetical`
(pre-session, pure extrapolation from historical fits), `model` (a little
live data blended with that prior), then `trajectory` (an exponential curve
fit to this session's own readings). `hypothetical` is never trusted at all —
live testing found the historical prior it leans on runs hot relative to
reality. `model` and `trajectory` are trusted, but **not symmetrically**:

- **Capping down is the safe direction** (a false-positive cap only costs a
  little charging speed), so it acts on `model` basis too, not just
  `trajectory` — every amp change resets the trajectory window, so trusting
  only `trajectory` leaves a multi-minute blind spot after every cap or
  restore, right when the situation is most likely to be changing. A real
  alert 40 fired inside exactly that gap during live testing.
- **Restoring up is the risky direction** (it's what pushes the equilibrium
  back toward the trip point), so it stays conservative on every axis: only
  `trajectory` basis, one `--restore-step-a` at a time rather than snapping
  straight back to `--normal-amps`, and never while the handle is within
  `--restore-margin-c` of the trip point even if the trajectory reads clear.
  Snapping straight back to full current, twice, immediately restarted the
  climb both times during live testing — turning a caught derate into
  repeated near-misses before a third one wasn't caught in time.

Either direction needs a signal held for `--confirm-ticks` consecutive polls
(default 3) before acting — a single noisy fit can't flip a real amp change.

Stepping up isn't unconditionally retried, either. The *speed* of one swing
wasn't the only problem live testing exposed — the *frequency* of retrying
when the thermal budget genuinely hadn't recovered was its own. If a step-up
(partial or full) gets reversed by another cap within `--reattempt-window-min`
(default 15min), each such quick reversal multiplies the confirm-ticks
required before the *next* attempt by `--restore-backoff-base` (default 2x —
3 polls, then 6, then 12...), and after `--max-restore-attempts` quick
reversals in the same session (default 3), the daemon stops trying to climb
back up at all and just holds the last cap for the rest of that session. A
step-up that holds *longer* than the reattempt window before needing another
cap resets the backoff — real recovery still gets a clean slate, and a new
session always starts fresh.

One more guard covers a subtler failure: **`will_trip: false` is a point
estimate, not a certainty.** It means the *projected* plateau landed under
the trip point, and that projection carries the model's own fit error. When
the two are within `--forecast-confidence-k` times `fit_rmse_c` of each
other (default 2), that verdict is a coin flip dressed up as a decision, so
the daemon steps down instead of trusting it. Live testing produced exactly
this: a projected 64.6 °C plateau against a 65.0 °C trip with ~0.31 °C fit
RMSE — a 1.3-sigma call that nothing in the logic had authority to act on,
since only `will_trip: true` could trigger a cap. Note this is deliberately
*not* a "handle is within X degrees of the trip" rule: the same session
settled into a genuinely stable 63.8 °C plateau that such a rule would have
banned outright. Proximity to the trip is not the danger; proximity plus an
untrustworthy forecast is. As fits improve and `fit_rmse_c` shrinks, the
guard narrows on its own and permits more aggressive operation.

A cap fully lifts three ways: the trajectory forecast reports the risk has
passed *and* the handle has real thermal margin (stepped up gradually, see
above), the charging session ends (restored immediately — no more climb to
protect against), or — a safety net — a new session starts while the
daemon's on-disk state still says "capped" from a run that never saw its
session close out (crash, restart, etc.). That last case always restores
before evaluating anything else, so a stale cap can never silently persist
into a session that never earned it. `journalctl -u derate-amp-control`
shows each run's decision and reason.

Every applied change is also recorded in the monitor's own event log:
`amp_capped` / `amp_restored` events carry the from → to amps and the
forecast numbers that justified the move, and appear on the Alerts & events
timeline under "Alerts & thermal" (and on the live stream, like any other
event). They arrive via `POST /api/events`, a write-only ingest allowlisted
to the controller's event kinds — the controller narrates its actions
without the timeline becoming a generic log sink. A BLE write that fails
records `amp_adjust_failed`, so bridge flakiness shows up in the same place
as the decisions it blocked. Recording is best-effort by design: the event
log is observability, never control flow.

**Checking whether it would actually help, before or after deploying it:**
`contrib/backtest_derate_amp_control.py` replays `decide()` against real
historical sessions read straight from `wallmonitor.db` (point it at a copy,
not the live file). `thermal.predict()` and the model-fitting functions are
all parameterized by an explicit `now` and never look past it, so the
script can reconstruct exactly what `/api/thermal` would have reported at
any past instant — including during a real alert 40, not just a
hypothetical one — and check whether the daemon's gating would have caught
it with enough lead time to matter.

```bash
uv run python contrib/backtest_derate_amp_control.py --db /path/to/a/copy/of/wallmonitor.db
```

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
