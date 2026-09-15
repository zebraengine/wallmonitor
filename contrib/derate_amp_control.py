#!/usr/bin/env python3
"""Cap Tesla charge current when wallmonitor's thermal model predicts a
derate, and restore it once the risk clears.

wallmonitor's /api/thermal forecasts a handle-temperature trip from three
increasingly precise bases as a charging session accumulates data:
``hypothetical`` (pre-session, pure extrapolation from historical fits),
``model`` (a little live data blended with the historical prior), and
``trajectory`` (an exponential curve fit to this session's own readings).
The two directions this daemon can move current are not equally risky, so
they trust that precision hierarchy differently:

**Capping down is the safe direction** — a false-positive cap only costs a
little charging speed — so it acts on ``model`` basis too, not just
``trajectory``. This matters because ``trajectory`` needs several minutes of
steady current to (re)build a window: every amp change resets it, so trusting
only ``trajectory`` leaves a multi-minute blind spot after every cap or
restore. On 2026-08-03 a real alert 40 fired inside exactly that gap — a
restore-to-normal reset the trajectory fit, and the handle climbed back to
the trip point before a fresh trajectory window could confirm and act again.

**Restoring up is the risky direction** — it's what pushes the equilibrium
back toward the trip point — so it stays conservative on every axis: only
``trajectory`` basis (this session's own proven data, never ``model``), never
while the handle is still within ``--restore-margin-c`` of the trip point
even if the trajectory reads clear, and never straight back to
``--normal-amps`` on trust. The same 2026-08-03 session showed why: capping
straight back to 48A the moment a trajectory read clear, twice, immediately
restarted the climb both times, converting a caught derate into three
near-misses before the third one wasn't caught in time.

Where it restores *to* is the server's ``sustainable_max_a``: the highest
current whose modelled plateau stays under the trip point at today's ambient
(the LAN sensor when one reports, else the ambient the live trajectory
implies). One move there, then the trajectory and the confidence guard trim
the last amp or two. The alternative — climbing
``--restore-step-a`` at a time — resets the trajectory window at every rung
and took half an hour to find the same number. The model is trusted once per
session: after a quick reversal it has already been wrong about today, and
the climb falls back to single ``--restore-step-a`` steps (and to that ladder
entirely against a server that doesn't report the field).

Earlier live testing (2026-08-01, a full 48A session) is why ``hypothetical``
basis is never trusted at all: it leans on the historical per-install
baseline, which ran hot relative to reality that night (predicted a trip
~24min out; the session's own trajectory fit correctly ruled one out once it
had enough points).

The cap is applied through an ESPHome ``esphome-tesla-ble`` device's
web_server REST API (``POST /number/charging_amps/set?value=<A>``), paired
with the vehicle using the least-privilege ``CHARGING_MANAGER`` role.

A cap fully lifts three ways, in order of how eagerly they should fire:
1. the trajectory forecast reports ``will_trip: false`` for
   ``--confirm-ticks`` consecutive polls *and* the handle has real margin
   below the trip point, restored to the sustainable current — see above;
2. the charging session ends (``state`` leaves ``charging``) — the normal,
   expected end of any cap, restored immediately since there's no more
   climb to protect against;
3. a new session starts while the on-disk state still says "capped" from a
   run that never got to close one out (crash, daemon restart, etc.) — a
   safety net so a stale cap can never silently persist into a session that
   never earned it.

Stepping up isn't unconditionally retried, either. If a step-up (partial or
full) gets reversed by another cap within ``--reattempt-window-min``, that's
treated as evidence the thermal budget genuinely hasn't recovered yet, not
noise — the *speed* of one swing isn't the only problem repeated attempts
cause; the *frequency* of retrying is its own signal. Each such quick
reversal multiplies the confirm-ticks required before the *next* attempt by
``--restore-backoff-base`` (default 2x, so 3 -> 6 -> 12 ticks...), and after
``--max-restore-attempts`` quick reversals in the same session, the daemon
stops trying to climb back up at all and just holds the last cap for the
rest of that session. A step-up that holds *longer* than the reattempt
window before needing to cap again resets the backoff — real recovery still
gets a clean slate.

Finally, ``will_trip: false`` is a point estimate, not a certainty — it means
the *projected* plateau landed under the trip point, and every projection
carries uncertainty. When plateau and trip point are within
``--forecast-confidence-k`` times the projection's own standard error
(``steady_state_se_c``, computed by the server per 30 s tick — wide early in
a trajectory window, tight near the plateau; ``fit_rmse_c`` is the fallback
for servers that don't report it), that verdict is a coin flip dressed up as
a decision, so the daemon steps down instead of trusting it. Observed live 2026-08-04: a projected 64.6 C plateau against a
65.0 C trip with ~0.31 C fit RMSE — a 1.3-sigma call that nothing in the
logic had authority to act on, since only ``will_trip: true`` could trigger a
cap. It held that time, but by luck rather than by design. Note this is
deliberately *not* a raw "handle is within X degrees of the trip" rule: the
same session settled into a genuinely stable 63.8 C plateau at 45A that such
a rule would have banned outright. Proximity to the trip is not the danger;
proximity plus an untrustworthy forecast is.

Stdlib only. Run it from cron or a systemd timer (see
deploy/install-derate-amp-control.sh); one invocation reads, decides,
optionally acts, and exits — debounce state lives in --state-file between
runs.

Example:
    ./derate_amp_control.py --tesla-ble http://<esp32-host> --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field, fields, replace

TRIP_HANDLE_C = 65.0  # mirrors wallmonitor/thermal.py's TRIP_HANDLE_C (Gen 3, firmware 26.18.0)
COLD_START_WINDOW_S = 180.0  # a cold-cable probe must start within this long of charging beginning
FULL_RATE_FRAC = 0.9  # "at full rate" for the warm-cable clock: at least this fraction of normal_amps
PROBE_LOG_KEEP = 50  # completed probes remembered in the state file
PROBE_CABLES = ("any", "cold", "warm")


@dataclass
class Config:
    """Tuning knobs, all CLI-overridable; defaults are the deployed values.
    The asymmetry between capping (eager, trusts model basis too) and
    restoring (cautious, trajectory-only, stepped, backed-off) is the point
    — see the module docstring for the incidents behind it."""

    normal_amps: float = 48.0
    lead_time_min: float = 20.0
    confirm_ticks: int = 3
    min_cap_delta_a: float = 1.0
    restore_step_a: float = 2.0
    restore_margin_c: float = 3.0
    restore_backoff_base: float = 2.0
    max_restore_attempts: int = 3
    reattempt_window_min: float = 15.0
    forecast_confidence_k: float = 2.0
    min_amps: float = 6.0

    # Calibration probe. The degradation watch can only compare windows the
    # charge current held steady through — and on an install where this
    # daemon caps often, those are scarce and land at whatever current the
    # capping happened to choose. A probe manufactures one deliberately:
    # hold a current low enough that nothing wants to trim it, long enough
    # for the handle to reach its plateau, on a fixed cadence. The result is
    # a repeatable operating point that month-over-month comparison can use
    # without extrapolating across currents.
    #
    # Disabled when empty — this is opt-in, and an install whose charges
    # already run unregulated at full rate does not need it.
    #
    # A probe *plan* is the product of the currents and the cable
    # conditions: "cold" is a probe started in a session's first minutes
    # after a long gap since the last charge; "warm" is a mid-session
    # step-down after a stretch at full rate, when the cable and connector
    # are heat-soaked; "any" is the original behavior — whenever due. The
    # backtest showed the forecast's remaining error is heat soak from the
    # charge before, which the history cannot separate from ambient because
    # the controller chose every run's current on the strength of the model
    # itself; a probe at one current with a cold cable and a warm one
    # measures it directly. Conditions are visited least-replicated first,
    # at the plan interval until each has probe_replicates completions, then
    # at the maintenance interval.
    probe_amps: tuple[float, ...] = ()
    probe_cable: tuple[str, ...] = ("any",)
    probe_interval_days: float = 30.0
    probe_plan_interval_days: float = 7.0
    probe_replicates: int = 2
    probe_hold_min: float = 40.0
    probe_cold_gap_h: float = 4.0
    probe_warm_min: float = 30.0

    def __post_init__(self) -> None:
        if isinstance(self.probe_amps, (int, float)):
            self.probe_amps = (float(self.probe_amps),) if self.probe_amps > 0 else ()
        if isinstance(self.probe_cable, str):
            self.probe_cable = (self.probe_cable,)


@dataclass
class State:
    """What survives between 30 s timer runs, via --state-file: the active
    cap, the debounce streaks, which session state we last saw (so a new
    session resets everything), and the restore-backoff bookkeeping."""

    capped: bool = False
    cap_value: float | None = None
    trip_streak: int = 0
    clear_streak: int = 0
    last_session_state: str | None = None
    restore_attempts: int = 0
    last_step_up_ts: float | None = None
    # Calibration probe: when the current hold began, at what current and
    # cable condition, and when one last ran to completion. Only a
    # *completed* hold updates last_probe_ts and probes_done, so a session
    # that unplugs mid-probe does not consume the slot. probe_session_ts
    # is the session a probe was attempted in: one per session.
    probe_started_ts: float | None = None
    last_probe_ts: float | None = None
    probe_amps: float | None = None
    probe_cable: str | None = None
    probe_session_ts: float | None = None
    probes_done: list[dict] = field(default_factory=list)
    # Session bookkeeping the cable conditions are judged on: the last tick
    # seen charging (so the gap before a session is known), when this
    # session's charging began, that gap, and how long the current has held
    # at full rate uncapped.
    last_charging_ts: float | None = None
    charging_since_ts: float | None = None
    charging_gap_s: float | None = None
    full_rate_since_ts: float | None = None


@dataclass
class Action:
    """decide()'s verdict: do nothing, or set the vehicle's charge current
    to `value` amps. Every non-"none" Action carries a value."""

    kind: str  # "none" | "cap" | "restore"
    value: float | None = None
    probe: dict | None = None  # {"amps", "cable"} when the cap starts a calibration probe


def _decide_thermal(thermal: dict, state: State, cfg: Config) -> tuple[Action, State, str]:
    """The derate-avoidance decision: what to do, the state to persist, and
    why. Knows nothing about the calibration probe — see _apply_probe."""
    session_state = thermal.get("state")
    forecast = thermal.get("forecast") or {}
    basis = forecast.get("basis")
    will_trip = forecast.get("will_trip")
    mtt = forecast.get("minutes_to_trip")
    suggested = forecast.get("suggested_max_a")
    handle_c = thermal.get("handle_c")
    now_ts = thermal.get("ts")
    current_a = thermal.get("current_a")
    steady_state_c = forecast.get("steady_state_c")
    steady_state_se_c = forecast.get("steady_state_se_c")
    model = thermal.get("model") or {}
    fit_rmse_c = model.get("fit_rmse_c")
    trip_c = model.get("trip_c", TRIP_HANDLE_C)

    session_started = session_state == "charging" and state.last_session_state != "charging"
    new_state = replace(state, last_session_state=session_state)

    if session_state != "charging":
        new_state = replace(new_state, trip_streak=0, clear_streak=0, restore_attempts=0, last_step_up_ts=None)
        if state.capped:
            new_state = replace(new_state, capped=False, cap_value=None)
            return Action("restore", cfg.normal_amps), new_state, "session ended: restoring normal rate"
        return Action("none"), new_state, "idle: nothing to do"

    if session_started and state.capped:
        # A previous run set "capped" but never saw this session close out
        # (crash, daemon restart, ...). Never let that carry into a new
        # session on trust alone.
        new_state = replace(
            new_state,
            capped=False,
            cap_value=None,
            trip_streak=0,
            clear_streak=0,
            restore_attempts=0,
            last_step_up_ts=None,
        )
        return (
            Action("restore", cfg.normal_amps),
            new_state,
            ("new session started with a stale cap from a previous run: restoring first"),
        )

    # A step-up (partial or full) undone by another cap soon after isn't
    # noise — it's evidence the thermal budget genuinely hasn't recovered,
    # so retrying immediately would just repeat the same failed attempt at
    # higher frequency. Holding this long since the last step-up is treated
    # as a real recovery and forgives past failures.
    quick_reversal = (
        state.last_step_up_ts is not None
        and isinstance(now_ts, (int, float))
        and (now_ts - state.last_step_up_ts) <= cfg.reattempt_window_min * 60.0
    )

    # Capping down is the safe direction, so both `trajectory` and `model`
    # basis are trusted — see the module docstring for why: `trajectory`
    # alone leaves a multi-minute blind spot after every amp change, right
    # when the situation is most likely to be changing.
    cap_trusted = basis in ("trajectory", "model")

    # Cap path: only reachable once mtt/suggested have passed isinstance
    # checks, so `suggested` is a real number everywhere below this point.
    if (
        cap_trusted
        and will_trip is True
        and isinstance(mtt, (int, float))
        and mtt <= cfg.lead_time_min
        and isinstance(suggested, (int, float))
    ):
        streak = state.trip_streak + 1
        next_state = replace(new_state, trip_streak=streak, clear_streak=0)
        if streak < cfg.confirm_ticks:
            return (
                Action("none"),
                next_state,
                (f"basis={basis} predicts a trip in {mtt:.1f}min ({streak}/{cfg.confirm_ticks} confirming polls)"),
            )
        suggested_a = float(suggested)
        tightening = (
            state.capped and state.cap_value is not None and suggested_a <= state.cap_value - cfg.min_cap_delta_a
        )
        if not state.capped or tightening:
            attempts = state.restore_attempts + 1 if quick_reversal else 0
            final_state = replace(
                next_state, capped=True, cap_value=suggested_a, trip_streak=0, restore_attempts=attempts
            )
            note = f", quick reversal (restore_attempts={attempts})" if quick_reversal else ""
            return (
                Action("cap", suggested_a),
                final_state,
                (f"basis={basis} predicts a trip in {mtt:.1f}min: capping to {suggested_a:g}A{note}"),
            )
        return Action("none"), replace(next_state, trip_streak=0), "already capped near the suggested value"

    # Confidence guard. `will_trip: false` is a point estimate, not a
    # certainty: it means the *projected* plateau landed under the trip
    # point, and that projection carries the model's own fit error. When
    # the two are within `k * fit_rmse_c` of each other the "no trip"
    # verdict is a coin flip dressed up as a decision, so treat it as a
    # trip signal and step down instead of trusting it.
    #
    # 2026-08-04: observed live at 45A with a projected plateau of 64.6 C
    # against a 65.0 C trip and a fit RMSE of ~0.31 C — a 1.3-sigma call
    # the daemon had no authority to act on, since only `will_trip: true`
    # could trigger a cap. It happened to hold, but nothing in the logic
    # made that a decision rather than luck.
    #
    # Deliberately NOT a raw "handle is within X degrees of the trip" rule:
    # that same session settled into a genuinely stable 63.8 C plateau at
    # 45A, which a proximity rule would have banned outright. Proximity to
    # the trip is not the danger; proximity plus an untrustworthy forecast
    # is. As fits improve and fit_rmse_c shrinks, this guard narrows on its
    # own and permits more aggressive operation.
    # The denominator is this projection's own standard error when the
    # server reports one (issue #4): early in a trajectory window the
    # extrapolation is wild and the SE says so; near the plateau it
    # tightens, and the guard relaxes with it. fit_rmse_c — a single
    # model-adequacy constant across historical sessions — remains the
    # fallback for older servers or windows too short for a meaningful SE.
    plateau_c = gap_c = sigma = None
    sigma_src = None
    if (
        basis == "trajectory"
        and will_trip is False
        and isinstance(steady_state_c, (int, float))
        and isinstance(trip_c, (int, float))
    ):
        if isinstance(steady_state_se_c, (int, float)) and steady_state_se_c > 0:
            denom, sigma_src = float(steady_state_se_c), "proj se"
        elif isinstance(fit_rmse_c, (int, float)) and fit_rmse_c > 0:
            denom, sigma_src = float(fit_rmse_c), "fit rmse"
        else:
            denom = None
        if denom is not None:
            plateau_c = float(steady_state_c)
            gap_c = float(trip_c) - plateau_c
            sigma = gap_c / denom

    if sigma is not None and gap_c is not None and plateau_c is not None and sigma < cfg.forecast_confidence_k:
        streak = state.trip_streak + 1
        next_state = replace(new_state, trip_streak=streak, clear_streak=0)
        if streak < cfg.confirm_ticks:
            return (
                Action("none"),
                next_state,
                (
                    f"plateau {plateau_c:.1f}C is only {gap_c:.1f}C under trip "
                    f"({sigma:.1f} sigma vs {sigma_src}, need {cfg.forecast_confidence_k:g}): "
                    f"{streak}/{cfg.confirm_ticks} confirming polls"
                ),
            )
        # Step down from wherever we are now. Unlike the main cap path there
        # is no suggested_max_a to aim at — the forecast believes no cap is
        # needed at all — so back off one step and re-measure. Lower current
        # lowers the projected plateau, so this converges rather than
        # ratcheting.
        basis_a = state.cap_value if state.capped and state.cap_value is not None else current_a
        if not isinstance(basis_a, (int, float)):
            basis_a = cfg.normal_amps
        target = max(cfg.min_amps, float(basis_a) - cfg.restore_step_a)
        if state.capped and state.cap_value is not None and target >= state.cap_value:
            return Action("none"), replace(next_state, trip_streak=0), "already at the confidence-guard floor"
        attempts = state.restore_attempts + 1 if quick_reversal else state.restore_attempts
        final_state = replace(next_state, capped=True, cap_value=target, trip_streak=0, restore_attempts=attempts)
        return (
            Action("cap", target),
            final_state,
            (
                f"plateau {plateau_c:.1f}C only {gap_c:.1f}C under trip ({sigma:.1f} sigma vs {sigma_src}): "
                f"forecast too uncertain to trust, stepping down to {target:g}A"
            ),
        )

    # Restore path: deliberately narrower than the cap path. Only
    # `trajectory` basis (never `model`), never on trust to full rate, gated
    # on real thermal margin, and backed off exponentially after repeated
    # quick reversals — see the module docstring for the incidents that
    # justify every one of these guards.
    if basis == "trajectory" and will_trip is False:
        streak = state.clear_streak + 1
        next_state = replace(new_state, clear_streak=streak, trip_streak=0)

        if state.capped and state.cap_value is not None and state.restore_attempts >= cfg.max_restore_attempts:
            return (
                Action("none"),
                next_state,
                (
                    f"giving up on returning to full rate this session after "
                    f"{state.restore_attempts} quick reversals: holding {state.cap_value:g}A"
                ),
            )

        required = cfg.confirm_ticks * (cfg.restore_backoff_base**state.restore_attempts)
        if streak >= required and state.capped and state.cap_value is not None:
            if not isinstance(handle_c, (int, float)):
                return (
                    Action("none"),
                    next_state,
                    "trajectory clear but no handle reading to confirm margin: holding",
                )
            margin_c = TRIP_HANDLE_C - handle_c
            if margin_c < cfg.restore_margin_c:
                return (
                    Action("none"),
                    next_state,
                    (
                        f"trajectory clear but handle only {margin_c:.1f}C under trip "
                        f"(need {cfg.restore_margin_c:g}C): holding {state.cap_value:g}A"
                    ),
                )
            # One move to the model's sustainable current when the server
            # reports one, instead of a 2 A ladder that resets the trajectory
            # window at every rung. The model is trusted once per session:
            # after a quick reversal it has already been wrong about this
            # session, so the climb falls back to single steps.
            step_value = state.cap_value + cfg.restore_step_a
            sustainable = forecast.get("sustainable_max_a")
            jump = isinstance(sustainable, (int, float)) and state.restore_attempts == 0
            next_value = min(cfg.normal_amps, max(step_value, float(sustainable)) if jump else step_value)
            how = "jumping" if jump and next_value > step_value else "stepping up"
            step_state = replace(next_state, clear_streak=0, last_step_up_ts=now_ts)
            if next_value >= cfg.normal_amps:
                final_state = replace(step_state, capped=False, cap_value=None)
                return (
                    Action("restore", cfg.normal_amps),
                    final_state,
                    (f"trajectory clear, {margin_c:.1f}C of margin: fully restoring to {cfg.normal_amps:g}A"),
                )
            final_state = replace(step_state, capped=True, cap_value=next_value)
            return (
                Action("cap", next_value),
                final_state,
                (
                    f"trajectory clear, {margin_c:.1f}C of margin: {how} to {next_value:g}A "
                    f"(still under {cfg.normal_amps:g}A)"
                ),
            )
        return (
            Action("none"),
            next_state,
            (
                f"trajectory clear ({streak}/{required:g} confirming polls, capped={state.capped}, "
                f"restore_attempts={state.restore_attempts})"
            ),
        )

    # Neither path qualifies: `model` basis reporting clear (not trusted for
    # restoring), a trip predicted but beyond lead time, or no usable
    # forecast at all yet ("insufficient" basis, early in a session). Too
    # weak or untrusted a signal to move either streak in either direction.
    return (
        Action("none"),
        new_state,
        (
            f"basis={basis} will_trip={will_trip} mtt={mtt} "
            f"(trip_streak={new_state.trip_streak} clear_streak={new_state.clear_streak}, "
            f"need {cfg.confirm_ticks})"
        ),
    )


def _apply_probe(
    action: Action, new_state: State, reason: str, thermal: dict, prev: State, cfg: Config
) -> tuple[Action, State, str]:
    """Overlay the calibration probe on the derate decision.

    A layer rather than a branch inside _decide_thermal, because that logic
    is tuned by a string of live incidents and the probe has no business
    reaching into it. The precedence is the only thing that matters here:

    - **Safety always wins.** A cap below the probe current is a real
      thermal decision; it is applied, and the probe is abandoned rather
      than held over a window whose current just moved. An abandoned probe
      does not update last_probe_ts, so the next session retries it.
    - **The probe outranks restoring.** Stepping back up toward full rate is
      exactly what would ruin the measurement, so while a probe holds, this
      returns "none" and the step-up never happens.
    - **A probe only starts from above.** If the current is already at or
      under the probe current there is nothing to hold it down to.
    - **One probe per session**, and the plan's least-replicated condition
      first: if that condition can still be met later in this session (a
      warm-cable probe needs a stretch at full rate first), the daemon
      waits for it rather than spending the slot on an easier one — unless
      the slot is overdue by a whole interval, when any eligible condition
      will do.
    """
    if not cfg.probe_amps:
        return action, new_state, reason
    session_state = thermal.get("state")
    now_ts = thermal.get("ts")
    current_a = thermal.get("current_a")
    has_ts = isinstance(now_ts, (int, float))

    # Not charging: no probe can be running, and any half-finished one is
    # abandoned (its window never completed, so it taught nothing).
    if session_state != "charging":
        return action, replace(
            new_state, probe_started_ts=None, probe_amps=None, probe_cable=None,
            charging_since_ts=None, charging_gap_s=None, full_rate_since_ts=None,
        ), reason

    # Session bookkeeping the cable conditions are judged on. The session
    # started this tick if the previous run saw anything but "charging";
    # a daemon upgraded mid-session knows neither when it began nor the gap
    # before it, and a cold probe simply waits for the next session.
    if prev.last_session_state != "charging":
        since = now_ts if has_ts else None
        gap = (now_ts - prev.last_charging_ts) if has_ts and prev.last_charging_ts is not None else None
    else:
        since, gap = prev.charging_since_ts, prev.charging_gap_s
    at_full_rate = (
        isinstance(current_a, (int, float)) and current_a >= FULL_RATE_FRAC * cfg.normal_amps and not new_state.capped
    )
    full_since = (prev.full_rate_since_ts if prev.full_rate_since_ts is not None else now_ts) if at_full_rate and has_ts else None
    new_state = replace(
        new_state,
        last_charging_ts=now_ts if has_ts else prev.last_charging_ts,
        charging_since_ts=since, charging_gap_s=gap, full_rate_since_ts=full_since,
    )

    probing = prev.probe_started_ts is not None
    # A state file written before the plan existed records no current for a
    # hold in progress; that daemon had exactly one probe current.
    active = (prev.probe_amps if prev.probe_amps is not None else cfg.probe_amps[0]) if probing else None

    if action.kind == "cap" and action.value is not None:
        if probing and action.value < active:
            return (
                action,
                replace(new_state, probe_started_ts=None, probe_amps=None, probe_cable=None),
                f"{reason}; probe abandoned (thermal cap below the {active:g}A probe current)",
            )
        if not probing:
            return action, new_state, reason  # a thermal decision this tick outranks starting a probe

    if probing:
        if not has_ts:
            return Action("none"), new_state, "probe holding (no timestamp to age it against)"
        held_min = (now_ts - prev.probe_started_ts) / 60.0
        if held_min >= cfg.probe_hold_min:
            record = {"ts": now_ts, "amps": active, "cable": prev.probe_cable or "any"}
            # The cadence is anchored at the probe's *session* start, not its
            # completion: a probe that ends 40-70 min into a session would
            # otherwise leave a session exactly one interval later a few
            # minutes short of due at its first tick — the only minutes a
            # cold-cable probe can start in.
            anchor = since if since is not None else now_ts
            done = replace(
                new_state, probe_started_ts=None, probe_amps=None, probe_cable=None, last_probe_ts=anchor,
                probes_done=(prev.probes_done + [record])[-PROBE_LOG_KEEP:], clear_streak=0, trip_streak=0,
            )
            # The probe's own plateau is the best measurement of today's
            # conditions the session will ever have, and the server has
            # already turned it into the sustainable current. Go there.
            # Snapping to normal_amps instead, on a day the model already
            # knew full rate would trip, cost a 32 -> 48 -> 42 A oscillation.
            sustainable = (thermal.get("forecast") or {}).get("sustainable_max_a")
            if isinstance(sustainable, (int, float)) and sustainable < cfg.normal_amps:
                target = float(sustainable)
                if target <= active:
                    return (
                        Action("none"),
                        replace(done, capped=True, cap_value=active),
                        (
                            f"probe complete: held {active:g}A for {held_min:.0f}min; "
                            f"sustainable {target:g}A is no higher, holding"
                        ),
                    )
                return (
                    Action("cap", target),
                    replace(done, capped=True, cap_value=target, last_step_up_ts=now_ts),
                    (
                        f"probe complete: held {active:g}A for {held_min:.0f}min, "
                        f"restoring to the sustainable {target:g}A"
                    ),
                )
            return (
                Action("restore", cfg.normal_amps),
                replace(done, capped=False, cap_value=None),
                (
                    f"probe complete: held {active:g}A for {held_min:.0f}min, "
                    f"restoring to {cfg.normal_amps:g}A"
                ),
            )
        # Hold. clear_streak is pinned at zero so the restore path cannot
        # bank confirming polls while the probe runs and then step up the
        # instant it ends.
        return (
            Action("none"),
            replace(
                new_state,
                probe_started_ts=prev.probe_started_ts,
                probe_amps=active,
                probe_cable=prev.probe_cable,
                capped=True,
                cap_value=active,
                clear_streak=0,
            ),
            f"probe holding {active:g}A ({held_min:.0f}/{cfg.probe_hold_min:g}min)",
        )

    # Start one? Only when due, once per session, from a current above the
    # probe value, and never on top of a thermal cap that is already lower
    # — that session has bigger problems than calibration.
    plan = _probe_plan(cfg)
    counts = _probe_counts(prev, plan)
    complete = all(count >= cfg.probe_replicates for count in counts.values())
    interval_days = cfg.probe_interval_days if (complete or len(plan) == 1) else cfg.probe_plan_interval_days
    if not has_ts or not isinstance(current_a, (int, float)):
        return action, new_state, reason
    elapsed = None if prev.last_probe_ts is None else now_ts - prev.last_probe_ts
    if elapsed is not None and elapsed < interval_days * 86400.0:
        return action, new_state, reason
    if since is not None and prev.probe_session_ts == since:
        return action, new_state, reason
    overdue = elapsed is not None and elapsed >= 2.0 * interval_days * 86400.0
    for amps, cable in sorted(plan, key=lambda cond: (counts[cond], plan.index(cond))):
        already_lower = new_state.capped and new_state.cap_value is not None and new_state.cap_value <= amps
        if already_lower or current_a <= amps + 1.0:
            continue
        status = _cable_status(cable, now_ts, since, gap, full_since, new_state.capped, cfg)
        if status == "eligible":
            return (
                Action("cap", amps, probe={"amps": amps, "cable": cable}),
                replace(
                    new_state, probe_started_ts=now_ts, probe_amps=amps, probe_cable=cable,
                    probe_session_ts=since, capped=True, cap_value=amps,
                ),
                (
                    f"calibration probe due ({cable} cable): capping to {amps:g}A for {cfg.probe_hold_min:g}min "
                    "to measure an unregulated plateau"
                ),
            )
        if status == "pending" and not overdue:
            return action, new_state, reason  # the condition that needs it most may still come this session
    return action, new_state, reason


def _probe_plan(cfg: Config) -> list[tuple[float, str]]:
    return [(amps, cable) for amps in cfg.probe_amps for cable in cfg.probe_cable]


def _probe_counts(state: State, plan: list[tuple[float, str]]) -> dict[tuple[float, str], int]:
    counts = {condition: 0 for condition in plan}
    for done in state.probes_done:
        key = (float(done.get("amps") or 0.0), done.get("cable") or "any")
        if key in counts:
            counts[key] += 1
    return counts


def _cable_status(
    cable: str, now_ts: float, since: float | None, gap_s: float | None, full_since: float | None,
    capped: bool, cfg: Config,
) -> str:
    """Whether a cable condition holds right now ("eligible"), could still
    hold later in this session ("pending"), or cannot ("impossible")."""
    if cable == "any":
        return "eligible"
    if cable == "cold":
        if since is None or gap_s is None or gap_s < cfg.probe_cold_gap_h * 3600.0:
            return "impossible"
        return "eligible" if now_ts - since <= COLD_START_WINDOW_S else "impossible"
    if cable == "warm":
        if full_since is not None and now_ts - full_since >= cfg.probe_warm_min * 60.0:
            return "eligible"
        return "impossible" if capped else "pending"
    return "impossible"


def decide(thermal: dict, state: State, cfg: Config) -> tuple[Action, State, str]:
    """What to do, the state to persist, and why: the derate decision with
    the calibration probe layered over it."""
    action, new_state, reason = _decide_thermal(thermal, state, cfg)
    return _apply_probe(action, new_state, reason, thermal, state, cfg)


# ---------------------------------------------------------------------------
# wallmonitor / tesla-ble access


def fetch_thermal(base_url: str) -> dict:
    """One /api/thermal snapshot — the sole input decide() ever sees."""
    with urllib.request.urlopen(f"{base_url}/api/thermal", timeout=10) as resp:
        return json.load(resp)


def set_charging_amps(tesla_ble_url: str, amps: float) -> None:
    """Apply an amp value through the ESPHome tesla-ble bridge's
    charging_amps number entity (the least-privilege BLE pairing)."""
    req = urllib.request.Request(
        f"{tesla_ble_url}/number/charging_amps/set?value={amps:g}",
        data=b"",  # forces Content-Length: 0; the ESPHome web_server 411s without it
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()


def event_for(action: Action, reason: str, thermal: dict, prev: State) -> tuple[str, dict]:
    """The event-log record for an applied action: what changed, from where,
    and the forecast numbers that justified it."""
    forecast = thermal.get("forecast") or {}
    detail = {
        "from_a": prev.cap_value if prev.capped else thermal.get("current_a"),
        "to_a": action.value,
        "reason": reason,
        "basis": forecast.get("basis"),
        "minutes_to_trip": forecast.get("minutes_to_trip"),
        "steady_state_c": forecast.get("steady_state_c"),
        "handle_c": thermal.get("handle_c"),
    }
    if action.probe is not None:
        detail["probe"] = action.probe  # the backtest groups probe runs by condition from this
    return ("amp_capped" if action.kind == "cap" else "amp_restored", detail)


def post_event(base_url: str, kind: str, detail: dict) -> None:
    """Best-effort: the event log is observability, never control flow."""
    req = urllib.request.Request(
        f"{base_url}/api/events",
        data=json.dumps({"kind": kind, "detail": detail}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"warn: could not record {kind} in the event log ({exc})", file=sys.stderr)


# ---------------------------------------------------------------------------


def load_state(path: str) -> State:
    """State from disk, defaulting on a missing/corrupt file and ignoring
    unknown fields — a daemon upgrade must never crash on old state."""
    try:
        with open(path) as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return State()
    known = {f.name for f in fields(State)}
    return State(**{k: v for k, v in raw.items() if k in known})


def save_state(path: str, state: State) -> None:
    """Atomic write (tmp file + rename): a crash mid-write leaves the old
    state intact rather than a torn file."""
    tmp = f"{path}.tmp"
    with open(tmp, "w") as fh:
        json.dump(asdict(state), fh)
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--tesla-ble",
        required=True,
        help="base URL of the esphome-tesla-ble device's web_server, e.g. http://<esp32-host> (kept out of any repo)",
    )
    parser.add_argument(
        "--wallmonitor", default="http://127.0.0.1:8480", help="wallmonitor base URL (default %(default)s)"
    )
    parser.add_argument(
        "--normal-amps",
        type=float,
        default=48.0,
        help="rate to restore once the derate risk clears (default %(default)s)",
    )
    parser.add_argument(
        "--lead-time-min",
        type=float,
        default=20.0,
        help="only cap when trajectory predicts a trip within this many minutes (default %(default)s)",
    )
    parser.add_argument(
        "--confirm-ticks",
        type=int,
        default=3,
        help="consecutive qualifying polls required before acting, to ride out a noisy fit (default %(default)s)",
    )
    parser.add_argument(
        "--min-cap-delta-a",
        type=float,
        default=1.0,
        help="only tighten an existing cap if the new suggestion drops at "
        "least this much further (default %(default)s)",
    )
    parser.add_argument(
        "--restore-step-a",
        type=float,
        default=2.0,
        help="raise the cap by this much per confirmed-clear cycle when the server reports no "
        "sustainable current, or after the model has already been wrong once this session "
        "(default %(default)s)",
    )
    parser.add_argument(
        "--restore-margin-c",
        type=float,
        default=3.0,
        help="never step up while the handle is within this many degrees C "
        "of the trip point, even if the trajectory reads clear (default %(default)s)",
    )
    parser.add_argument(
        "--restore-backoff-base",
        type=float,
        default=2.0,
        help="each step-up reversed by another cap within --reattempt-window-min "
        "multiplies the confirm-ticks required for the next attempt by this (default %(default)s)",
    )
    parser.add_argument(
        "--max-restore-attempts",
        type=int,
        default=3,
        help="after this many quick reversals in one session, stop trying to climb "
        "back up at all and hold the last cap for the rest of it (default %(default)s)",
    )
    parser.add_argument(
        "--reattempt-window-min",
        type=float,
        default=15.0,
        help="a step-up undone by another cap sooner than this counts as a quick "
        "reversal; longer than this counts as a real recovery (default %(default)s)",
    )
    parser.add_argument(
        "--forecast-confidence-k",
        type=float,
        default=2.0,
        help="treat a 'no trip' forecast as untrustworthy (and cap anyway) when the "
        "projected plateau is within this many fit-RMSE of the trip point; 0 disables "
        "the guard (default %(default)s)",
    )
    parser.add_argument(
        "--min-amps",
        type=float,
        default=6.0,
        help="never step below this (J1772 floor — the vehicle won't charge under it) (default %(default)s)",
    )
    parser.add_argument(
        "--state-file",
        default="/tmp/derate_amp_control.state.json",
        help="remembers cap state and debounce streaks between runs",
    )
    parser.add_argument(
        "--probe-amps",
        default="",
        help=(
            "calibration probe: hold this charge current on a fixed cadence so the degradation watch "
            "gets a plateau nothing trimmed (off by default). A comma-separated list probes each in "
            "turn. Pick currents low enough that neither this daemon nor the vehicle wants to reduce them"
        ),
    )
    parser.add_argument(
        "--probe-cable",
        default="any",
        help=(
            "cable condition(s) to probe under, comma-separated: 'any' (whenever due, the default), "
            "'cold' (a session's first minutes after --probe-cold-gap-h without charging), 'warm' (a "
            "mid-session step-down after --probe-warm-min at full rate). With more than one current or "
            "condition the plan visits the least-replicated one first"
        ),
    )
    parser.add_argument(
        "--probe-interval-days",
        type=float,
        default=Config.probe_interval_days,
        help="days between calibration probes once the plan is complete (default: %(default)s)",
    )
    parser.add_argument(
        "--probe-plan-interval-days",
        type=float,
        default=Config.probe_plan_interval_days,
        help="days between probes while a multi-condition plan is still collecting (default: %(default)s)",
    )
    parser.add_argument(
        "--probe-replicates",
        type=int,
        default=Config.probe_replicates,
        help="completed probes per condition before the plan counts as complete (default: %(default)s)",
    )
    parser.add_argument(
        "--probe-cold-gap-h",
        type=float,
        default=Config.probe_cold_gap_h,
        help="hours without charging before a session counts as cold-cable (default: %(default)s)",
    )
    parser.add_argument(
        "--probe-warm-min",
        type=float,
        default=Config.probe_warm_min,
        help="minutes at full rate, uncapped, before a warm-cable probe may start (default: %(default)s)",
    )
    parser.add_argument(
        "--probe-hold-min",
        type=float,
        default=Config.probe_hold_min,
        help=(
            "minutes to hold the probe current; needs to exceed ~3x the install's thermal time "
            "constant for the handle to reach its plateau (default: %(default)s)"
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="print the decision without changing the charger")
    args = parser.parse_args(argv)

    try:
        probe_amps = tuple(float(part) for part in args.probe_amps.split(",") if part.strip())
    except ValueError:
        parser.error(f"--probe-amps must be a number or a comma-separated list of numbers; got {args.probe_amps!r}")
    probe_amps = tuple(amps for amps in probe_amps if amps > 0)
    for amps in probe_amps:
        if not (args.min_amps <= amps <= args.normal_amps):
            parser.error(
                f"--probe-amps must sit between --min-amps ({args.min_amps:g}) and "
                f"--normal-amps ({args.normal_amps:g}); got {amps:g}"
            )
    probe_cable = tuple(dict.fromkeys(part.strip() for part in args.probe_cable.split(",") if part.strip()))
    if not probe_cable or any(cable not in PROBE_CABLES for cable in probe_cable):
        parser.error(f"--probe-cable takes any of {', '.join(PROBE_CABLES)}; got {args.probe_cable!r}")
    if "any" in probe_cable and len(probe_cable) > 1:
        parser.error("--probe-cable 'any' cannot be combined with cold/warm")

    cfg = Config(
        normal_amps=args.normal_amps,
        lead_time_min=args.lead_time_min,
        confirm_ticks=args.confirm_ticks,
        min_cap_delta_a=args.min_cap_delta_a,
        restore_step_a=args.restore_step_a,
        restore_margin_c=args.restore_margin_c,
        restore_backoff_base=args.restore_backoff_base,
        max_restore_attempts=args.max_restore_attempts,
        reattempt_window_min=args.reattempt_window_min,
        forecast_confidence_k=args.forecast_confidence_k,
        min_amps=args.min_amps,
        probe_amps=probe_amps,
        probe_cable=probe_cable,
        probe_interval_days=args.probe_interval_days,
        probe_plan_interval_days=args.probe_plan_interval_days,
        probe_replicates=args.probe_replicates,
        probe_hold_min=args.probe_hold_min,
        probe_cold_gap_h=args.probe_cold_gap_h,
        probe_warm_min=args.probe_warm_min,
    )
    state = load_state(args.state_file)

    try:
        thermal = fetch_thermal(args.wallmonitor)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"skip: cannot reach wallmonitor ({exc})", file=sys.stderr)
        return 0  # transient; the timer will try again

    action, new_state, reason = decide(thermal, state, cfg)

    if action.kind == "none":
        print(f"skip: {reason}")
        save_state(args.state_file, new_state)
        return 0

    assert action.value is not None  # every non-"none" Action carries a value
    verb = "cap" if action.kind == "cap" else "restore"
    if args.dry_run:
        print(f"would {verb} to {action.value:g}A: {reason}")
        return 0

    # Built from the pre-apply state so from_a reflects where we stepped from.
    kind, detail = event_for(action, reason, thermal, state)

    try:
        set_charging_amps(args.tesla_ble, action.value)
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"error: failed to {verb} charging_amps to {action.value:g}A ({exc})", file=sys.stderr)
        post_event(args.wallmonitor, "amp_adjust_failed", {"attempted": verb, "to_a": action.value, "error": str(exc)})
        return 1

    save_state(args.state_file, new_state)
    print(f"{verb} applied: {action.value:g}A ({reason})")
    post_event(args.wallmonitor, kind, detail)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
