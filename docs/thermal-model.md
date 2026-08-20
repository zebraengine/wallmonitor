# Thermal derate forecast & degradation watch

*[← back to the README](../README.md)*

## Thermal derate forecast

The Gen 3 raises alert 40 ("high temperature
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
## Degradation watch

The same per-segment fits feed a trend: rising heat at
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
