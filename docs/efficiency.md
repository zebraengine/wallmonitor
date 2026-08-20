# Measured charging efficiency

*[← back to the README](../README.md)*

The amp controller exists to charge fast without tripping the thermal
foldback — which raises an obvious question: does running at high current
near the derate threshold cost anything in efficiency? Measured on this
install (59 AC sessions ≥3 kWh over about five weeks of summer,
cross-referenced between the Wall Connector's own session meter, the
vehicle's AC-side metering as logged by TeslaMate, and a whole-home energy
monitor at the electrical panel), the answer splits cleanly:

- **Current level barely matters within the normal range.** Vehicle-side
  efficiency (energy added to the battery ÷ AC energy at the vehicle inlet)
  was ~95% and flat from 33 A to 48 A — per-bin means 94.0–95.7%, the
  differences within session-to-session noise. The last few amps buy speed,
  not efficiency.
- **The loss is mostly fixed overhead, not I²R.** Total loss ran roughly
  450–600 W nearly independent of current — dominated by the vehicle's
  onboard-charger electronics and coolant pump, which run for the whole
  session regardless of rate. That constant is exactly why *slow* charging
  is the expensive mode: at the 50% foldback's lower rate, a ~500 W overhead
  against a halved power figure implies roughly 3–5 points worse efficiency,
  and a deep cap toward 16 A (3.8 kW) implies ~85–88% — 7–10 points worse —
  while the session runs correspondingly longer. (Estimated from the
  measured overhead; this dataset contains no derated sessions to measure
  directly, which is the controller doing its job.)
- **Wall-to-battery, the full chain is ~92%.** On clean single-charge
  sessions the Wall Connector's meter read 2.6–3.5% above the vehicle's
  AC-side figure (cable and handle resistance, plus honest cross-meter
  disagreement), putting true wall-to-battery efficiency at 92.1–92.7%.

So the efficiency claim for the amp controller is deliberately modest: a
capped-but-steady current in the 40s loses nothing measurable against full
rate, and the real saving is avoiding the low-rate foldback tail where the
fixed overhead eats efficiency points. The speed argument ("a steady cap
beats folding back to 50%") and the efficiency argument point the same way.

Three measurement notes, learned the hard way:

- **The wall-vs-vehicle delta is a degradation signal in its own right.**
  Rising contact resistance shows up as exactly this number trending upward
  at unchanged current — an independent line of evidence alongside the
  thermal degradation watch, measured in energy rather than temperature.
- **Long plug-ins inflate naive comparisons.** A vehicle parked overnight
  draws through the charger for things its own logs don't book as charging
  energy (scheduled-departure preconditioning, state-of-charge top-offs,
  standby). Multi-draw plug-in windows showed 5–11% apparent wall-vs-vehicle
  deltas that are real consumption, not loss. Compare per contiguous charge
  segment, not per plug-in — the same lesson the [thermal fitter](thermal-model.md) learned.
- **A panel-level monitor is a meter check, not an efficiency instrument.**
  On sessions with a quiet house baseline, whole-home power minus baseline
  matched the Wall Connector's meter to ≤1% — independent validation that
  the session energies above rest on a trustworthy meter. With air
  conditioning cycling, the same subtraction scattered ±8–15% per session,
  far too noisy to resolve sub-percent segments like branch-wiring loss.
  And the monitor's ML-disaggregated "EV" device tracked every session but
  read 2–10% high: useful as detection, not as metering.

These are one install's numbers — a different cable run, vehicle, or climate
will shift them — but the shape (flat efficiency across the upper current
range, fixed overhead dominating, slow tails as the expensive mode) is
physics, not coincidence.
