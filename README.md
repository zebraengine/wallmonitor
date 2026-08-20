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

![Live dashboard during a charge, then the session detail view — recorded in demo mode](https://raw.githubusercontent.com/zebraengine/wallmonitor/main/docs/assets/wallmonitor-demo.gif)

## Try it in 30 seconds

No hardware needed — demo mode runs a built-in charger simulator:

```bash
uvx wallmonitor --demo
```

Then open <http://127.0.0.1:8480>.

Against your real Wall Connector (find its IP in your router, or use the
`TeslaWallConnector_XXXXXX.local` hostname; add `--split-phase` on a North
American split-phase install):

```bash
uvx wallmonitor --host 192.168.1.50 --split-phase
```

All history lands in a single `wallmonitor.db` SQLite file — back up that
one file and you have everything. For every option, and for running it as a
systemd service on an always-on box, see [Running wallmonitor](https://github.com/zebraengine/wallmonitor/blob/main/docs/running.md).

## At a glance

- Full-fidelity recording — every response stored with its complete raw JSON
- Live dashboard (SSE) with rolling charts, an active-alert banner, and a
  live derate-forecast chart: measured handle temperature against the
  model's projected plateau and the trip threshold, projection drawn
  forward so the predicted trip intercept is visible on the chart
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
| [Running wallmonitor](https://github.com/zebraengine/wallmonitor/blob/main/docs/running.md) | Every option, systemd service install, tests |
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
