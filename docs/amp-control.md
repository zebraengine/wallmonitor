# Automatic derate prevention (BLE amp control)

*[← back to the README](../README.md)*

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
  `trajectory` basis, never straight back to `--normal-amps` on trust, and
  never while the handle is within `--restore-margin-c` of the trip point
  even if the trajectory reads clear. Snapping straight back to full
  current, twice, immediately restarted the climb both times during live
  testing — turning a caught derate into repeated near-misses before a
  third one wasn't caught in time.
- **Where it restores *to* is the model's answer, in one move.** The server
  reports `sustainable_max_a` on every forecast: the highest current whose
  modelled plateau stays under the trip point at today's ambient (the LAN
  sensor when one reports, else the ambient the live trajectory implies —
  the sensor is preferred because a plateau measured at a low current
  carries the current law's extrapolation error, and it comes back doubled
  when rescaled to a high one). The daemon restores straight to that (or to full rate
  when that is what it says), and lets the trajectory and the confidence
  guard trim the last amp or two. It used to climb `--restore-step-a` at a
  time instead — but every rung resets the trajectory window, so a climb
  from 32 A took half an hour to find the same number. The model is trusted
  once per session: after a quick reversal it has already been wrong about
  today, and the climb falls back to single `--restore-step-a` steps.

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
the trip point, and every projection carries uncertainty. The forecast
reports its own: `steady_state_se_c`, the standard error of the projected
plateau, computed per 30 s tick from the same regression that produces the
plateau (floored at the handle sensor's 0.1 °C resolution). Early in a
trajectory window, before much curvature is visible, it is honestly wide —
measured on a real 43 A stretch: 0.8 °C at 24 seconds in, 0.11 °C by three
minutes, ~0.04 °C near the plateau. When plateau and trip point are within
`--forecast-confidence-k` times that error (default 2), the "no trip"
verdict is a coin flip dressed up as a decision, so the daemon steps down
instead of trusting it; when the projection is tight, the guard trusts it
even close to the limit. (`fit_rmse_c`, the historical model-adequacy
constant the guard originally used, remains the fallback for servers that
don't report a per-projection error.) Live testing motivated this: a
projected 64.6 °C plateau against a 65.0 °C trip — a coin-flip call that
nothing in the logic had authority to act on. Note this is deliberately
*not* a "handle is within X degrees of the trip" rule: the same session
settled into a genuinely stable 63.8 °C plateau that such a rule would have
banned outright. Proximity to the trip is not the danger; proximity plus an
untrustworthy forecast is. As a window matures its SE shrinks, and the
guard relaxes tick by tick on its own.

A cap fully lifts three ways: the trajectory forecast reports the risk has
passed *and* the handle has real thermal margin (restored to the
sustainable current, see above), the charging session ends (restored immediately — no more climb to
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

## The calibration probe

The daemon exists to move charge current, which makes it the one component
that can *stop* moving it on purpose — and that turns out to be worth as much
as the capping.

The [degradation watch](thermal-model.md#degradation-watch) can only compare
charges whose current held steady through the ramp; anything else fits a
plateau that a current change produced rather than the connector. On an
install where this daemon caps often, those steady windows are scarce and
land at whatever current the capping happened to stop at — which is a moving
target, and one correlated with the weather, since a hot garage triggers
capping sooner. Measured on one install: 285 caps in a month, 7 of 18 fitted
windows contaminated, and the surviving ones split across three different
currents.

`--probe-amps` fixes that by manufacturing a clean window on a cadence:

```bash
sudo ./deploy/install-derate-amp-control.sh --tesla-ble http://<esp32-host> --probe-amps 32
```

Once every `--probe-interval-days` (default 30), the first charging session
to come along is held at `--probe-amps` for `--probe-hold-min` (default 40)
minutes, then restored to the sustainable current its own plateau implies —
the probe is the best measurement of today's conditions the session will
get, so its end is the one moment a restore target is most trustworthy.
(Releasing to full rate instead, on a day the model already knew full rate
would trip, produced a 32 → 48 → 42 A oscillation.) Pick a current low enough that neither this daemon
nor the vehicle wants to reduce it — on a 48 A install where foldback starts
around 61 °C, 32 A plateaus near 53 °C with room to spare. Hold it for more
than ~3x the install's time constant (`model.tau_min` in `/api/thermal`) so
the handle actually reaches that plateau instead of being extrapolated to it.

The result is a repeatable operating point: same current, unregulated, once a
month. Comparing those to each other is a degradation test with no
extrapolation across currents in it at all. They also break the collinearity
between charge current and the calendar, which is what otherwise stops the
watch's regression from telling a cap apart from a trend.

Precedence is the whole contract, and it is deliberately simple:

- **A thermal cap below the probe current always wins.** It is applied, and
  the probe is abandoned — a window whose current just moved teaches nothing.
- **The probe outranks restoring.** Stepping back toward full rate is exactly
  what would ruin the measurement, so no step-up happens while it holds.
- **An abandoned probe does not count.** Only a hold that ran its full length
  updates the cadence, so a session that unplugs early — or one that needed a
  real cap — simply retries next time.

### A probe plan

One current, whenever due, gives the degradation watch its repeatable
point. It cannot tell the forecast the two things the recorded history
cannot: how rise scales with current in the band restores actually land in,
and how much of the handle's heat is the *cable* still warm from the charge
before. The [forecast backtest](thermal-model.md#measuring-the-forecast)
found that heat-soak history to be the forecast's one remaining error — and
found it inseparable from ambient in the history, because the controller
chose every run's current on the strength of the model itself. A probe at
the same current with a cold cable and a warm one measures it directly.

```bash
sudo ./deploy/install-derate-amp-control.sh --tesla-ble http://<esp32-host> \
  --probe-amps 32,40 --probe-cable cold,warm
```

The plan is every current × every cable condition. **cold** is a probe
started in a session's first three minutes after `--probe-cold-gap-h`
(default 4) without charging; **warm** is a mid-session step-down after
`--probe-warm-min` (default 30) at full rate, uncapped; **any** is the
original behavior. The least-replicated condition goes first, and if it can
still be met later in the session — a warm probe needs its 30 min first —
the daemon waits for it rather than spending the slot on an easier one
(unless the slot is overdue by a whole interval, when anything eligible
will do). One probe per session. Probes run every
`--probe-plan-interval-days` (default 7) until each condition has
`--probe-replicates` (default 2) completions, then every
`--probe-interval-days` as before, cycling. Each probe's start is recorded
in its `amp_capped` event (`detail.probe`), which is how the backtest groups
probe runs by condition.

Two currents × two conditions × two replicates is eight probes: about two
months at the weekly cadence, each costing ~40 min at reduced current. In a
cooling season that is also a fixed-current sweep across a 15 °C ambient
range — the test of an ambient effect that a summer's worth of controller-
chosen history could not provide.

The probe is off unless `--probe-amps` is set. An install whose charges
already run unregulated at full rate does not need one; `regulated_n` on the
Alerts page says whether yours does.

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
