# wallmonitor

A **local-only** companion for the Tesla Wall Connector Gen 3 that grew well
beyond reading data off the charger: it records everything the device
reports, turns that history into a thermal model fitted to *your* install,
forecasts derates before they happen (with the charge-current cap that
avoids them), watches for connector degradation with honest statistics, and
pushes actionable warnings to your browser or phone.

Everything stays on your machine and your LAN: the only network traffic is
HTTP GETs to the charger's local API, storage is a local SQLite file, and the
web UI serves no external assets (no CDNs, fonts, or analytics).

![Reviewing a real charge session: the dashed line is what the forecast predicted at each 30 s tick, converging onto the measured handle temperature — and the event log shows the automatic amp control acting on those predictions](https://raw.githubusercontent.com/zebraengine/wallmonitor/main/docs/assets/wallmonitor-demo.gif)

## Try it in 30 seconds

No hardware needed — demo mode runs a built-in charger simulator:

```bash
uvx wallmonitor --demo
```

Then open <http://127.0.0.1:8480>.

Against your real Wall Connector — let the tool find it on your LAN (a
polite sweep of your own subnet; nothing leaves the network), then run with
the address it prints (add `--split-phase` on a North American install):

```bash
uvx wallmonitor --discover
uvx wallmonitor --host 192.168.1.50 --split-phase
```

On first contact the monitor pins the charger's serial number: if a
different unit ever answers at that address, it alarms and pauses recording
rather than blending two chargers' histories.

More than one Wall Connector? Run one instance per charger — `--label` names
each in the header and notifications, `--peer` links them so the header hops
between dashboards, and the service installer's `--name` does it all per
charger in one command. See [More than one Wall Connector](https://github.com/zebraengine/wallmonitor/blob/main/docs/running.md#more-than-one-wall-connector).

All history lands in a single `wallmonitor.db` SQLite file — back up that
one file and you have everything. For every option, and for running it as a
systemd service on an always-on box, see [Running wallmonitor](https://github.com/zebraengine/wallmonitor/blob/main/docs/running.md).

## At a glance

- Zero-config setup — `--discover` finds the charger on your LAN; the
  serial is pinned on first contact so a swapped or second unit can never
  blend into your history
- Full-fidelity recording — every response stored with its complete raw JSON
  (optional retention trims old raw blobs so the database stays near-flat)
- Live dashboard (SSE) with rolling charts, an active-alert banner, and a
  live derate-forecast chart: measured handle temperature against the
  model's projected plateau and the trip threshold, projection drawn
  forward so the predicted trip intercept is visible on the chart
- Session review: energy, peak/average power, per-phase telemetry, drillable
  charts, and forecast hindsight — what the model predicted at each tick
  against what the handle actually did
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
- Several chargers: one labeled instance each, linked by a header switcher;
  per-install thermal models stay separate by construction

## Works out of the box — everything else is additive

The core needs nothing but the charger. Integrations are optional, degrade
gracefully when absent, and the absence paths are covered by the test suite:
without an ambient sensor the charger doubles as its own thermometer, with
no webhook configured notifications are a tested no-op, and TeslaMate /
ntfy / ESP32-BLE each plug into a generic interface (`POST /api/ambient`, a
plain LAN webhook, a REST call) rather than being wired into the core.

## Documentation

| Page | What's in it |
|---|---|
| [Running wallmonitor](https://github.com/zebraengine/wallmonitor/blob/main/docs/running.md) | Every option, finding your charger, device identity, multiple chargers, systemd service install |
| [What gets recorded](https://github.com/zebraengine/wallmonitor/blob/main/docs/recording.md) | Recording fidelity, sessions, events, alert/EVSE label verification, resilience |
| [Thermal model](https://github.com/zebraengine/wallmonitor/blob/main/docs/thermal-model.md) | The derate forecast and the degradation watch, in full detail |
| [Amp control](https://github.com/zebraengine/wallmonitor/blob/main/docs/amp-control.md) | Automatic derate prevention over BLE: design, guards, backtesting |
| [Ambient sensing](https://github.com/zebraengine/wallmonitor/blob/main/docs/ambient-sensors.md) | Garage sensors (Ecowitt or any JSON poster), placement, contamination checks, the TeslaMate car bridge |
| [Notifications](https://github.com/zebraengine/wallmonitor/blob/main/docs/notifications.md) | Browser push and self-hosted ntfy phone alerts |
| [Measured charging efficiency](https://github.com/zebraengine/wallmonitor/blob/main/docs/efficiency.md) | What three cross-referenced meters say about wall-to-battery efficiency |

## Tests

```bash
uv run pytest
```

The suite runs the full pipeline against the simulator — polling, session
detection, alert lifecycle, the web API, and the thermal model end to end.

## License

[MIT](https://github.com/zebraengine/wallmonitor/blob/main/LICENSE). Built on the
[`tesla-wall-connector`](https://github.com/einarhauks/tesla-wall-connector)
library (MIT, by Einar Bragi Hauksson).

wallmonitor is an independent project, not affiliated with, endorsed by, or
sponsored by Tesla, Inc. "Tesla" and "Tesla Wall Connector" are trademarks of
Tesla, Inc., used here only to identify the hardware this software monitors.
