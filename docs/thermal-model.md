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
  at 48 A. Defaults come from a telemetry-verified alert-40 event on one
  install and are replaced as sessions accumulate. Once fitted, the
  dashboard's model note says so when this install landed far (>30%) from
  those priors: forecasts before the first fit were governed by numbers
  that did not describe this charger, and an install with a τ well under
  the default has a standing cost — the fitter judges a charge's window
  against the default τ, so only charges of ~22 min or more at steady
  current teach the model there.
- **How rise scales with current is fitted too.** Joule heating says rise
  ∝ I², and that is the prior — but a real handle carries heat that does
  not scale with current (the charger's own electronics, cable heat soak),
  so measured plateaus fall off more gently as current drops. On one
  install a 32 A probe settled 4 °C above what I² predicted, and every
  forecast at an off-reference current inherited the error — including
  the current the amp controller was told to restore to. Once the
  free-running fits span ≥ 6 A of current, the exponent *n* in
  rise = rise₄₈ · (I/48)ⁿ is fitted by log-log regression (`current_exp`
  in `/api/thermal`, with its standard error), every fit's `rise_ref_c` is
  re-normalized with it, and the model note says so.
- **The charger is its own thermometer.** Idle, the handle sits ~1–2 °C above
  ambient (an ambient-dependent offset), so ambient can be read without any
  extra sensor. The offset model ships as a seed from one install and is
  **recalibrated to yours automatically** once a stationary ambient sensor
  has overlapped a few days of idle time; without one it stands, labelled,
  with a stated ±1.5 °C uncertainty on every proxy read. A LAN ambient
  sensor or the car's thermometer, when present, take precedence — see
  [Ambient sensing](ambient-sensors.md).

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

## Free-running windows, and the ones the charger wrote

The other way a plateau can be fictitious is that something chose it. The
fitter's steady-current band is 10 % of the reference current, which is wide
enough to hide a substantial reduction: 48.6 A trimmed to 44.7 A never
leaves it. The ramp then flattens *because the current fell*, and the
exponential reads that flattening as the plateau — a lower rise paired with
a faster τ, clearing every other gate with an excellent RMSE.

Who moved the current matters less than that it moved. On one install it was
mostly the monitor's own doing: the optional [amp
controller](amp-control.md) caps on this model's forecast, 285 times in a
month, and the vehicle tapers on its own besides. The charger's *internal*
foldback — the one alert 40 raises, counted by lifetime `thermal_foldbacks`
— had not fired once in the same period. So this is first of all a feedback
loop: the forecast caps the current, the cap contaminates the fit, the fit
feeds the forecast.

The bias is not random either. The controller caps sooner in a hot garage,
so the under-read arrives and leaves with the weather. Measured on that
install: windows whose current sagged fitted a median +33.3 °C rise where
the same charger's steady windows fitted +37.2 °C.

So every fit records `current_sag_a` — how far current fell from the head
of the window to its tail, compared by quarter-medians — and
`free_plateau`, true when that sag stayed inside 1.5 % of the window's
current. On the install above the two populations did not overlap: steady
windows sagged ≤ 0.6 %, regulated ones ≥ 3.9 %.

Regulated fits still describe what the handle actually did, so the
forecast keeps them. The degradation watch cannot use them at all.

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
means added resistance — a loose lug, a degrading contact — so when the
fitted rise climbs over time, the poller raises a monitor alert and the
Alerts page charts the fitted-rise trend.

### What it estimates, and why not a median split

The watch **regresses fitted rise on time** across the whole comparable
history, holding ambient and charge current, and reads the *time*
coefficient. The reported Δ is that slope times the observed span.

It did once compare a recent median against a baseline median, and that
asks the wrong question. "Are the last few fits higher?" is answered for
you by anything that moved with the calendar: a garage that cooled between
the two halves, or a vehicle capped to a lower current whose (48/I)ⁿ
normalization then lifts every recent fit at once. On one install the
split reported **+7.2 °C with a 95 % CI of [5.4, 9.1]** — "statistically
confirmed" — for a connector whose rise, regressed on time with ambient and
current held, was moving +0.01 ± 0.04 °C/day. The confidence was real. It
was confidence in the wrong estimand.

Regression fixes three things at once:

- **Confounders become covariates.** Ambient and charge current are
  adjusted for instead of assumed away, and each one's coefficient is
  reported so you can see what it was worth.
- **Every fit counts.** The estimate uses the whole history rather than
  three fits against a handful, which is where the precision comes from.
- **Unseparable confounds declare themselves.** When a covariate cannot be
  told apart from the calendar — a current cap applied once and kept is
  nearly collinear with time — the collinearity inflates the slope's
  standard error and the verdict declines to confirm. That is the honest
  outcome, and it is reached automatically rather than by a rule someone
  had to anticipate.

A covariate earns a column only when the history actually moved in it
(≥ 3 °C of ambient, ≥ 2 A of current); below that it buys nothing and
spends a degree of freedom. `/api/thermal` reports which columns were used
(`covariates`), each coefficient, the residual scatter, and any covariate
that correlated with time past 0.8 (`collinear_with_time`).

### What counts as drift

The alert needs the change to be **material** (≥ 2.5 °C — a confirmed
0.3 °C increase is real but not worth an inspection), **confirmed** — the
slope's 95 % confidence interval, at a small-sample Student-t multiplier,
must clear zero — and **soundly measured** (see the ambient confound
below). The effective threshold is the larger of the floor and what this
install's own scatter demands, and the dashboard shows which one is
binding. A noisy install must show more before the watch alarms; a quiet
one, less.

A change past the floor that fails either of the other two tests is a
**lead**: shown on the dashboard, pushed once at default priority, no alert
row. More sessions either confirm it or dissolve it.

### What it compares

- **Only free-running fits.** Windows the charger was regulating are
  excluded outright — their plateau is a setpoint, not an equilibrium.
  `regulated_n` says how many sat out, and the Alerts page says so too,
  because a verdict resting on four fits should not look like one resting
  on twelve.
- **Only sessions near the install's usual charge current** — the median
  across its whole comparable history, not its newest few fits. The
  regression holds current, so a cap is something to adjust for rather than
  chase, and a stable band cannot be inverted by the occasional
  off-current charge (a monthly calibration probe is exactly such a charge:
  at "newest three", two of them landing together made the *probe* current
  typical and pooled the operating current out of its own comparison).
- **Pooled across a wide current band when the fits are clean.**
  Ambient-bracketed fits join from a wider band, and the regression's own
  current term then *adjusts* them: residual error in the current
  normalization lands on that coefficient instead of masquerading as a
  trend. On the install above, under the I² prior, that coefficient read
  −0.99 °C per amp, which is the whole of the phantom +7.2 °C — and is
  what the fitted exponent now removes at the source. The band is wide enough on purpose to admit a
  [calibration probe](amp-control.md#the-calibration-probe).
- **Never only stale sessions.** If none of the newest few free-running
  charges make it into the comparison, the install has moved to a current
  the band excludes and the watch reports nothing rather than a verdict
  about a way it no longer charges — which also lets a stale alert clear.
  As charges at the new current accumulate they become the median and the
  band follows them.

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

The premise of `rise_ref` is that subtracting ambient leaves a number that
depends on the connector and not on the weather. The watch checks that
premise instead of assuming it: the regression's **ambient coefficient**
is how much rise still moves per degree of garage air after the
subtraction. A healthy install reads flat.

An install reading materially non-zero (≥ 0.3 °C/°C, resolved well enough
to be sure of the sign) has something in the measurement that the model
does not carry, and the watch says so and **caps its verdict at a lead** —
it can never raise an alert until the cause is found. Adjusting for a
confounder is not the same as understanding it.

The **rise-vs-ambient scatter** on the same page shows the shape:

- a cloud **sloping upward with ambient** exposes an environment effect —
  multi-day heat soak of cable and structure in an uninsulated garage;
- a cloud **sloping downward** is the signature of a handle held at a
  fixed temperature by something — most often charger regulation that the
  free-plateau gate did not catch, since a hotter garage means the trim
  starts sooner;
- an **elevated-but-flat** cloud is the genuine added-resistance signature.

### What the watch cannot do, and what to charge to fix it

Everything above measures a plateau the charger allowed. On an install
where full-rate charging always ends in foldback, the only free-running
windows are the low-current ones, and the watch is judging a handful of
fits at a current the install rarely uses.

The fix is a **fixed-condition probe**: once a month, charge at a current
low enough to run unregulated end to end (well under whatever first trims
it), for at least 3 τ. That yields a plateau nobody imposed, at a
repeatable operating point, and comparing those month over month is a
degradation test with no extrapolation in it at all. A single such charge
is worth more to this watch than several at full rate. It also breaks the
collinearity between charge current and the calendar, which is the one
thing that stops the regression from separating a cap from a trend.

The [amp controller](amp-control.md#the-calibration-probe) automates it —
`--probe-amps 32` — because the component that moves charge current is the
one that can hold it still on purpose. Without the controller, setting the
vehicle's charge limit by hand once a month does the same job.
