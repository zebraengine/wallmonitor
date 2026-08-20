# Ambient sensing

*[← back to the README](../README.md)*

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
