# Thermal derate forecast & degradation watch

*[← back to the README](../README.md)*

## What the charger does

The Gen 3 raises alert 40 ("high temperature detected") when its plug-handle
sensor hits 65 °C, halving charge current for the rest of the session. The
handle warms along a first-order lag: an exponential approach to a plateau
set by ambient temperature plus a rise that depends on current. Everything
below is built on measuring that lag for *your* install and using it before
the trip happens.

## The model

- **Parameters are fitted per install** (`wallmonitor/thermal.py`) from your
  own recorded charging ramps: the time constant τ and the steady-state rise
  at 48 A. Defaults come from a telemetry-verified alert-40 event and are
  replaced as sessions accumulate.
- **The charger is its own thermometer.** Idle, the handle sits ~1–2 °C above
  ambient (a calibrated, ambient-dependent offset — see
  `contrib/calibrate_idle_offset.py`), so ambient can be read without any
  extra sensor. A LAN ambient sensor or the car's thermometer, when present,
  take precedence — see [Ambient sensing](ambient-sensors.md).

## What gets fitted: segments, not sessions

One plug-in routinely contains several distinct draws hours apart — the
vehicle's own state-of-charge top-offs, scheduled-departure preconditioning,
or a charging schedule (common with time-of-use rates or home batteries). The
charger reports no "scheduled charging" state for any of these
(telemetry-verified: with a vehicle-side schedule armed overnight it idles in
ordinary connected states until the car draws).

So the fitter works per **charging segment**: it finds each segment's opening
ramp wherever it occurs in the session and lets the quality gates decide
what teaches the model. No configuration or "monitoring mode" is needed.

One gate deserves spelling out: the steady-current window must span at
least 1.8× the install's median τ (≈ 20 min at a typical 11 min τ). Shorter
than that the plateau is never observed, and the exponential can explain
the same samples with a lower rise and a faster τ — passing every other
gate with a fine RMSE while under-reading the rise by several degrees. Such
fits are not "noisy"; they are biased low, and a few of them in a drift
baseline manufacture a degradation verdict. The span is judged against the
install's τ, not the fit's own, so a truncated segment cannot vouch for
itself.

The unit of thermal analysis is the **load window** — the stretch where
current actually flows.

## Ambient is a bracket, not a point

A single start-of-window ambient silently assumes the weather held still for
the whole charge, and a baseline recorded in one season would then bias
every comparison that follows. Instead each window's ambient is read at
both ends:

- **At the start**, from the flat idle stretch before the window. When a
  segment starts on a still-warm handle — a stop/resume, a post-derate
  resume — there is no idle stretch, so ambient comes from the previous
  charge's **cool-down tail**, extrapolated to its asymptote at the install's
  fitted τ. That keeps exactly the hardest-working segments from being the
  ones excluded from degradation tracking.
- **At the end**, from the charge's own cool-down tail.

When both ends read, the fit is **de-trended** against the ambient ramp
between them: a garage that warms 3 °C during an afternoon charge (or cools
overnight) is measured and removed instead of masquerading as connector
resistance. Fits that could only read one end fall back to the point
ambient and say so (`ambient_source` on every fit in `/api/thermal`).

## The live forecast

The Live page answers a different question depending on state:

- **While charging** — whether and when the current session will derate,
  from the handle's live trajectory.
- **While idle** — the estimated ambient, and whether a full-rate charge
  started now would trip.
- **When a derate is coming** — the highest vehicle charge-current cap that
  stays under the limit. A steady capped rate charges faster than full rate
  folding back to 50 %; the optional [amp controller](amp-control.md) can
  apply that cap automatically.
- **During cool-down** — after a current cut or a derate, the forecast
  reports the true lower equilibrium the handle is settling toward
  ("recovering", not "tripping").

When a mid-session current change resets the live trajectory window, or
sessions run back-to-back with no idle gap to read ambient from, the
forecast bridges with ambient inferred from the newest steady run still in
the buffer instead of going dark.

Each trajectory projection also reports its own standard error
(`steady_state_se_c`) — wide early in a window, tight near the plateau —
which is what the [amp controller](amp-control.md)'s confidence guard
weighs margins against. Every 30 s tick is recorded, so the session page
can show in hindsight what was predicted against what the handle did. The line is labelled *predicted
plateau (if this current holds)* for a reason: it is the asymptote at the
present current, not where a six-minute top-off will stop — see the faint
model-only ticks before trajectory data exists.

**Field-validated live:** steering the vehicle's charge current down on the
forecast's advice kept a session 0.7 °C under the trip point, and in a
deliberate full-rate test the trajectory forecast predicted the actual
alert-40 raise to within seconds.

`/api/thermal` returns the fitted model, the live forecast, every
per-segment fit, and the drift verdict.

## Degradation watch

The same per-segment fits feed a trend. Rising heat at unchanged current
means added resistance — a loose lug, a degrading contact — so when recent
segments' fitted rise climbs past the baseline, the poller raises a monitor
alert and the Alerts page charts the fitted-rise trend.

### What it compares

- **Only sessions near the install's recent operating current.** Cap the
  vehicle at a new amperage and the watch follows, rather than judging
  forever against a current the install no longer uses.
- **Pooled across a wider current band when the fits are clean.**
  Ambient-bracketed fits are clean enough under the I² normalization to pool
  in, so a baseline recorded at 48 A keeps judging charges after a cap to
  40 A instead of the verdict going dark.

### How sure it is

The verdict **carries its own uncertainty**: per-side spread (MAD) and a
small-sample Student-t ~95 % confidence interval on the delta, with a
separate `confident` flag. The alert threshold is a tripwire; the UI and
notifications distinguish "statistically confirmed" from "a lead from a
four-fit baseline".

### What it compares against

A baseline is only as meaningful as the hardware behind it. The
**verified-baseline anchor** (button on the Alerts page, or
`POST /api/thermal/baseline-anchor`) excludes all fits recorded before a
hardware inspection: from then on the comparison means "vs verified
healthy", not "vs the first charges the monitor happened to see".

### The confounder the fits can't remove

A **rise-vs-ambient scatter** on the same page separates the one thing
left. Ambient is subtracted per fit, so a healthy install shows a flat cloud
regardless of garage temperature:

- a cloud **still sloping upward with ambient** exposes an environment
  effect the model doesn't carry — multi-day heat soak of cable and
  structure in an uninsulated garage;
- an **elevated-but-flat** cloud is the genuine added-resistance signature.
