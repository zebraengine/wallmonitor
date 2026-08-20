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
