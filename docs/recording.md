# What gets recorded

*[← back to the README](../README.md)*

wallmonitor records everything the charger reports, with honest bookkeeping
around gaps, glitches, and unverified labels. The numbered points below are
the recording pipeline in the order data flows through it.

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
   confirm a code there, then add it to the JSON). Tesla's official LED fault
   categories live on their own reference page, linked from the Alerts page
   so the event timeline stays one short scroll away. The same
   verify-before-label policy covers EVSE state names: states 1, 4, 9, and 11
   are named from telemetry cross-checked against vehicle presence, contactor,
   and power (the community-circulated names had 9 and 11 swapped), and the
   UI marks every state label as verified or community-reported.

6. **One synchronized clock** — every sample, session boundary, alert, and
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
