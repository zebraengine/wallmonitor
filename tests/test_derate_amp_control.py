"""decide() is pure, so the whole cap/restore decision table tests without
a wallmonitor server, an ESP32, or a vehicle. The behaviors under test
reflect three real incidents, all 2026-08-01 through 2026-08-03: trust only
`trajectory`/`model` bases, never `hypothetical`, and debounce over several
confirming polls; capping and restoring are NOT symmetric — capping trusts
`model` basis too (the safe direction, and `trajectory` alone left a
multi-minute blind spot after every amp change), while restoring stays
narrow: `trajectory` only, one step at a time, and gated on real thermal
margin; and a step-up undone by another cap soon after backs off
exponentially before the next attempt, giving up entirely after repeated
quick reversals in the same session — the *speed* of one swing wasn't the
only problem, the *frequency* of retrying when conditions genuinely hadn't
recovered was its own; and finally (2026-08-04) a `will_trip: false` verdict
whose projected plateau sits within the model's own fit error is a coin flip
rather than an answer, so it is treated as a trip signal instead of being
trusted."""

import importlib.util
import json
import pathlib
import sys

import pytest

spec = importlib.util.spec_from_file_location(
    "derate_amp_control",
    pathlib.Path(__file__).parent.parent / "contrib" / "derate_amp_control.py",
)
dac = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = dac  # dataclass annotation resolution needs this
spec.loader.exec_module(dac)


def _cfg(**kw):
    kw.setdefault("normal_amps", 48.0)
    kw.setdefault("lead_time_min", 20.0)
    kw.setdefault("confirm_ticks", 3)
    kw.setdefault("min_cap_delta_a", 1.0)
    kw.setdefault("restore_step_a", 2.0)
    kw.setdefault("restore_margin_c", 3.0)
    kw.setdefault("restore_backoff_base", 2.0)
    kw.setdefault("max_restore_attempts", 3)
    kw.setdefault("reattempt_window_min", 15.0)
    kw.setdefault("forecast_confidence_k", 2.0)
    kw.setdefault("min_amps", 6.0)
    return dac.Config(**kw)


def _thermal(
    state="charging",
    basis="trajectory",
    will_trip=True,
    mtt: float | None = 10.0,
    suggested: float | None = 44.0,
    handle_c: float | None = 50.0,  # well under trip - margin unless a test says otherwise
    ts: float | None = 1_000_000.0,
    current_a: float | None = 48.0,
    # Default plateau sits far below the trip so the confidence guard stays
    # dormant unless a test deliberately puts it in play.
    steady_state_c: float | None = 55.0,
    steady_state_se_c: float | None = None,
    fit_rmse_c: float | None = 0.3,
    trip_c: float = 65.0,
    # Absent by default: the ladder is the behavior against a server that
    # doesn't report a sustainable current, and most tests pin that.
    sustainable: float | None = None,
):
    return {
        "state": state,
        "handle_c": handle_c,
        "ts": ts,
        "current_a": current_a,
        "model": {"fit_rmse_c": fit_rmse_c, "trip_c": trip_c},
        "forecast": {
            "basis": basis,
            "will_trip": will_trip,
            "minutes_to_trip": mtt,
            "suggested_max_a": suggested,
            "steady_state_c": steady_state_c,
            "steady_state_se_c": steady_state_se_c,
            "sustainable_max_a": sustainable,
        },
    }


def _run(thermal, state, cfg, times=1):
    action = dac.Action("none")
    for _ in range(times):
        action, state, reason = dac.decide(thermal, state, cfg)
    return action, state, reason


def test_idle_with_no_cap_is_a_noop():
    action, state, reason = dac.decide(_thermal(state="idle"), dac.State(), _cfg())
    assert action.kind == "none" and "idle" in reason
    assert not state.capped


def test_idle_after_a_cap_restores_immediately():
    # Session-end restore skips the step-up/margin gating entirely — there's
    # no more climb to protect against once charging has actually stopped.
    capped = dac.State(capped=True, cap_value=44.0, last_session_state="charging")
    action, state, _ = dac.decide(_thermal(state="idle"), capped, _cfg())
    assert action.kind == "restore" and action.value == 48.0
    assert not state.capped and state.cap_value is None


def test_hypothetical_and_insufficient_basis_never_act():
    cfg = _cfg(confirm_ticks=2)
    state = dac.State(last_session_state="charging")
    for basis in ("hypothetical", "insufficient"):
        thermal = _thermal(basis=basis, will_trip=True, mtt=5.0, suggested=40.0)
        action, state, reason = _run(thermal, state, cfg, times=5)
        assert action.kind == "none"
        assert state.trip_streak == 0 and state.clear_streak == 0
        assert f"basis={basis}" in reason


def test_model_basis_triggers_a_cap():
    # The safe direction: model basis is trusted for capping, unlike restoring.
    cfg = _cfg(confirm_ticks=2)
    state = dac.State(last_session_state="charging")
    thermal = _thermal(basis="model", will_trip=True, mtt=6.0, suggested=42.0)
    action, state, _ = _run(thermal, state, cfg, times=2)
    assert action.kind == "cap" and action.value == 42.0
    assert state.capped and state.cap_value == 42.0


def test_model_basis_clear_does_not_restore():
    # The risky direction stays narrow: model-basis "clear" never counts.
    cfg = _cfg(confirm_ticks=2)
    state = dac.State(capped=True, cap_value=44.0, last_session_state="charging")
    thermal = _thermal(basis="model", will_trip=False, mtt=None, suggested=None)
    action, state, reason = _run(thermal, state, cfg, times=5)
    assert action.kind == "none"
    assert state.capped and state.cap_value == 44.0 and state.clear_streak == 0
    assert "basis=model" in reason


def test_trajectory_cap_requires_confirm_ticks_then_fires():
    cfg = _cfg(confirm_ticks=3)
    state = dac.State(last_session_state="charging")
    thermal = _thermal(will_trip=True, mtt=8.0, suggested=44.0)

    action, state, _ = dac.decide(thermal, state, cfg)
    assert action.kind == "none" and state.trip_streak == 1
    action, state, _ = dac.decide(thermal, state, cfg)
    assert action.kind == "none" and state.trip_streak == 2

    action, state, reason = dac.decide(thermal, state, cfg)
    assert action.kind == "cap" and action.value == 44.0
    assert state.capped and state.cap_value == 44.0 and state.trip_streak == 0
    assert "44" in reason


def test_will_trip_beyond_lead_time_is_neutral_not_reset():
    cfg = _cfg(confirm_ticks=3, lead_time_min=20.0)
    state = dac.State(last_session_state="charging", trip_streak=2, clear_streak=1)
    far_off = _thermal(will_trip=True, mtt=45.0, suggested=44.0)  # beyond lead time
    action, state, _ = dac.decide(far_off, state, cfg)
    assert action.kind == "none"
    # Too weak a signal to move either counter in either direction.
    assert state.trip_streak == 2 and state.clear_streak == 1


def test_restore_steps_up_gradually_instead_of_snapping_to_full():
    cfg = _cfg(confirm_ticks=2, restore_step_a=2.0, restore_margin_c=3.0)
    state = dac.State(capped=True, cap_value=40.0, last_session_state="charging")
    clear = _thermal(will_trip=False, mtt=None, suggested=None, handle_c=50.0)  # 15C of margin

    action, state, _ = dac.decide(clear, state, cfg)
    assert action.kind == "none" and state.clear_streak == 1 and state.cap_value == 40.0

    # 2nd confirming tick: steps up by restore_step_a, does NOT jump to 48.
    action, state, reason = dac.decide(clear, state, cfg)
    assert action.kind == "cap" and action.value == 42.0
    assert state.capped and state.cap_value == 42.0 and state.clear_streak == 0
    assert "stepping up" in reason

    # Each further step needs its OWN confirm cycle, not a free ride.
    action, state, _ = dac.decide(clear, state, cfg)
    assert action.kind == "none" and state.clear_streak == 1 and state.cap_value == 42.0


def test_restore_jumps_to_the_sustainable_current_in_one_move():
    # 2026-09-14: climbing 2 A at a time from a 32 A probe would have taken
    # ~30 min to find the 44 A the model could name at once — every rung
    # resets the trajectory window. With the server reporting a sustainable
    # current, the first confirmed-clear restore goes straight there.
    cfg = _cfg(confirm_ticks=1, restore_step_a=2.0, restore_margin_c=3.0)
    state = dac.State(capped=True, cap_value=32.0, last_session_state="charging")
    clear = _thermal(will_trip=False, mtt=None, suggested=None, handle_c=50.0, sustainable=44.0)
    action, state, reason = dac.decide(clear, state, cfg)
    assert action.kind == "cap" and action.value == 44.0
    assert state.capped and state.cap_value == 44.0 and state.last_step_up_ts == clear["ts"]
    assert "jumping to 44A" in reason


def test_restore_jump_to_normal_amps_fully_lifts():
    cfg = _cfg(confirm_ticks=1, restore_step_a=2.0, restore_margin_c=3.0)
    state = dac.State(capped=True, cap_value=32.0, last_session_state="charging")
    clear = _thermal(will_trip=False, mtt=None, suggested=None, handle_c=50.0, sustainable=48.0)
    action, state, reason = dac.decide(clear, state, cfg)
    assert action.kind == "restore" and action.value == 48.0
    assert not state.capped and "fully restoring" in reason


def test_restore_never_jumps_below_a_ladder_step():
    # The trajectory is the measurement; if it reads clear with margin, a
    # model that says "you are already at the limit" doesn't get to hold
    # the cap where it is. The old single step is the floor for progress.
    cfg = _cfg(confirm_ticks=1, restore_step_a=2.0, restore_margin_c=3.0)
    state = dac.State(capped=True, cap_value=40.0, last_session_state="charging")
    clear = _thermal(will_trip=False, mtt=None, suggested=None, handle_c=50.0, sustainable=39.0)
    action, state, reason = dac.decide(clear, state, cfg)
    assert action.kind == "cap" and action.value == 42.0
    assert "stepping up to 42A" in reason


def test_restore_falls_back_to_the_ladder_after_a_quick_reversal():
    # The model is trusted once per session. A jump that got capped again
    # is the model having been wrong about today; the next climb crawls.
    cfg = _cfg(confirm_ticks=1, restore_step_a=2.0, restore_margin_c=3.0)
    state = dac.State(capped=True, cap_value=40.0, last_session_state="charging", restore_attempts=1)
    clear = _thermal(will_trip=False, mtt=None, suggested=None, handle_c=50.0, sustainable=46.0)
    action, state, _ = dac.decide(clear, state, cfg)
    assert action.kind == "none"  # backoff: 1 * 2^1 = 2 confirming polls now
    action, state, reason = dac.decide(clear, state, cfg)
    assert action.kind == "cap" and action.value == 42.0
    assert "stepping up" in reason


def test_restore_fully_lifts_once_the_step_reaches_normal_amps():
    cfg = _cfg(confirm_ticks=1, restore_step_a=2.0, restore_margin_c=3.0)
    state = dac.State(capped=True, cap_value=47.0, last_session_state="charging")
    clear = _thermal(will_trip=False, mtt=None, suggested=None, handle_c=50.0)
    action, state, reason = dac.decide(clear, state, cfg)
    assert action.kind == "restore" and action.value == 48.0
    assert not state.capped and state.cap_value is None
    assert "fully restoring" in reason


def test_restore_holds_when_handle_still_close_to_trip():
    # 2026-08-03: restoring while the handle was still ~0.6-1C under the
    # trip point immediately restarted the climb, twice, before a real
    # alert 40 fired. The margin gate exists specifically to prevent this.
    cfg = _cfg(confirm_ticks=1, restore_margin_c=3.0)
    state = dac.State(capped=True, cap_value=42.0, last_session_state="charging")
    still_hot = _thermal(will_trip=False, mtt=None, suggested=None, handle_c=64.4)  # 0.6C of margin
    action, state, reason = dac.decide(still_hot, state, cfg)
    assert action.kind == "none"
    assert state.capped and state.cap_value == 42.0  # held, not stepped
    assert "holding" in reason and "0.6" in reason

    # Confirmation streak still counts while held — once margin clears, it
    # doesn't need to re-earn confirm_ticks from scratch.
    cooled = _thermal(will_trip=False, mtt=None, suggested=None, handle_c=61.0)  # 4C of margin
    action, state, reason = dac.decide(cooled, state, cfg)
    assert action.kind == "cap" and action.value == 44.0


def test_restore_holds_with_no_handle_reading():
    cfg = _cfg(confirm_ticks=1)
    state = dac.State(capped=True, cap_value=42.0, last_session_state="charging")
    no_handle = _thermal(will_trip=False, mtt=None, suggested=None, handle_c=None)
    action, state, reason = dac.decide(no_handle, state, cfg)
    assert action.kind == "none" and state.capped
    assert "no handle reading" in reason


def test_quick_reversal_backs_off_the_next_attempt():
    # 2026-08-03: stepping straight back up on every confirmed-clear read,
    # with no memory of how well the LAST step-up held, meant the daemon
    # kept retrying at the same pace even though conditions clearly hadn't
    # recovered — a real derate happened on the third retry. A step-up
    # reversed by another cap soon after should make the next attempt wait
    # longer, not retry at the same cadence.
    cfg = _cfg(confirm_ticks=2, restore_backoff_base=2.0, reattempt_window_min=15.0)
    state = dac.State(capped=True, cap_value=40.0, last_session_state="charging", trip_streak=0)

    clear_t0 = _thermal(will_trip=False, mtt=None, suggested=None, handle_c=50.0, ts=1000.0)
    action, state, _ = dac.decide(clear_t0, state, cfg)
    action, state, reason = dac.decide(clear_t0, state, cfg)
    assert action.kind == "cap" and action.value == 42.0  # stepped up
    assert state.last_step_up_ts == 1000.0 and state.restore_attempts == 0

    # Reversed by a cap just 5 minutes later — well inside the 15min window.
    soon_hot = _thermal(will_trip=True, mtt=5.0, suggested=40.0, ts=1300.0)
    action, state, reason = dac.decide(soon_hot, state, cfg)
    action, state, reason = dac.decide(soon_hot, state, cfg)
    assert action.kind == "cap" and action.value == 40.0
    assert state.restore_attempts == 1
    assert "quick reversal" in reason

    # The next step-up attempt now needs confirm_ticks * backoff_base = 4
    # confirming polls, not 2.
    clear_t1 = _thermal(will_trip=False, mtt=None, suggested=None, handle_c=50.0, ts=1400.0)
    for _ in range(3):
        action, state, _ = dac.decide(clear_t1, state, cfg)
        assert action.kind == "none"
    action, state, _ = dac.decide(clear_t1, state, cfg)
    assert action.kind == "cap" and action.value == 42.0  # the 4th confirming poll


def test_slow_reversal_resets_backoff():
    # A step-up that holds LONGER than the reattempt window before needing
    # another cap is real recovery, not a fluke — it should get a clean
    # slate, not carry forward a growing penalty from an unrelated episode.
    cfg = _cfg(confirm_ticks=2, restore_backoff_base=2.0, reattempt_window_min=15.0)
    state = dac.State(
        capped=True, cap_value=40.0, last_session_state="charging", restore_attempts=2, last_step_up_ts=1000.0
    )
    # A cap fires 20 minutes (1200s) after the last step-up — outside the
    # window. suggested=35.0 (well under cap_value - min_cap_delta_a) so
    # this actually tightens rather than being a no-op "already capped".
    long_after = _thermal(will_trip=True, mtt=5.0, suggested=35.0, ts=1000.0 + 1200.0)
    action, state, _ = dac.decide(long_after, state, cfg)
    action, state, reason = dac.decide(long_after, state, cfg)
    assert action.kind == "cap" and action.value == 35.0
    assert state.restore_attempts == 0
    assert "quick reversal" not in reason


def test_gives_up_after_max_restore_attempts():
    cfg = _cfg(confirm_ticks=1, max_restore_attempts=2)
    state = dac.State(
        capped=True, cap_value=40.0, last_session_state="charging", restore_attempts=2, last_step_up_ts=None
    )
    clear = _thermal(will_trip=False, mtt=None, suggested=None, handle_c=50.0, ts=5000.0)
    action, state, reason = dac.decide(clear, state, cfg)
    assert action.kind == "none"
    assert state.capped and state.cap_value == 40.0  # never even attempts the step
    assert "giving up" in reason

    # Stays given up across further ticks too, not just once.
    action, state, reason = dac.decide(clear, state, cfg)
    assert action.kind == "none" and state.cap_value == 40.0 and "giving up" in reason


def test_restore_attempts_reset_on_new_session():
    cfg = _cfg(confirm_ticks=1)
    exhausted = dac.State(
        capped=True, cap_value=40.0, last_session_state="idle", restore_attempts=3, last_step_up_ts=2000.0
    )
    thermal = _thermal(will_trip=True, mtt=5.0, suggested=40.0, ts=9000.0)
    action, state, _ = dac.decide(thermal, exhausted, cfg)
    assert action.kind == "restore"  # stale-cap safety net fires first
    assert state.restore_attempts == 0 and state.last_step_up_ts is None


def test_trajectory_clear_with_nothing_capped_is_a_noop():
    cfg = _cfg(confirm_ticks=2)
    state = dac.State(last_session_state="charging")
    clear = _thermal(will_trip=False, mtt=None, suggested=None)
    action, state, _ = _run(clear, state, cfg, times=5)
    assert action.kind == "none" and not state.capped


def test_recap_only_tightens_when_suggestion_drops_enough():
    cfg = _cfg(confirm_ticks=1, min_cap_delta_a=1.0)
    already_capped = dac.State(capped=True, cap_value=44.0, last_session_state="charging")

    # A suggestion that's barely lower shouldn't churn a new POST.
    barely_lower = _thermal(will_trip=True, mtt=8.0, suggested=43.5)
    action, state, reason = dac.decide(barely_lower, already_capped, cfg)
    assert action.kind == "none" and "already capped" in reason
    assert state.cap_value == 44.0

    # A real drop re-caps to the tighter value.
    much_lower = _thermal(will_trip=True, mtt=5.0, suggested=40.0)
    action, state, _ = dac.decide(much_lower, already_capped, cfg)
    assert action.kind == "cap" and action.value == 40.0


def test_new_session_clears_a_stale_cap_before_anything_else():
    cfg = _cfg(confirm_ticks=1)
    stale = dac.State(capped=True, cap_value=44.0, last_session_state="idle")
    # This tick's forecast would otherwise immediately qualify for a cap,
    # but the stale cap from a run that never saw its session end must be
    # cleared first, not compounded.
    thermal = _thermal(will_trip=True, mtt=5.0, suggested=40.0)
    action, state, reason = dac.decide(thermal, stale, cfg)
    assert action.kind == "restore" and action.value == 48.0
    assert not state.capped and state.cap_value is None
    assert "stale cap" in reason


def test_session_start_with_no_stale_cap_evaluates_normally():
    cfg = _cfg(confirm_ticks=1)
    fresh = dac.State(last_session_state=None)
    thermal = _thermal(will_trip=True, mtt=5.0, suggested=40.0)
    action, state, _ = dac.decide(thermal, fresh, cfg)
    assert action.kind == "cap" and action.value == 40.0
    assert state.capped and state.cap_value == 40.0


def test_confidence_guard_caps_when_plateau_is_within_model_error():
    # 2026-08-04, observed live: a projected 64.6C plateau against a 65.0C
    # trip with ~0.31C fit RMSE — the forecast said "no trip", but at 1.3
    # sigma that is a coin flip, and nothing in the logic could act on it.
    cfg = _cfg(confirm_ticks=2, forecast_confidence_k=2.0, restore_step_a=2.0)
    state = dac.State(last_session_state="charging")
    marginal = _thermal(will_trip=False, mtt=None, suggested=None, steady_state_c=64.6, fit_rmse_c=0.31, current_a=45.0)

    action, state, reason = dac.decide(marginal, state, cfg)
    assert action.kind == "none" and state.trip_streak == 1
    assert "sigma" in reason and "confirming polls" in reason

    action, state, reason = dac.decide(marginal, state, cfg)
    assert action.kind == "cap" and action.value == 43.0  # 45 - restore_step_a
    assert state.capped and state.cap_value == 43.0
    assert "too uncertain to trust" in reason


def test_confidence_guard_dormant_when_plateau_is_comfortably_clear():
    # The same session settled at a genuinely stable 63.8C plateau at 45A.
    # A raw "handle within X degrees of trip" rule would have banned that;
    # this guard must not, because the *forecast* is confident there.
    cfg = _cfg(confirm_ticks=1, forecast_confidence_k=2.0)
    state = dac.State(capped=True, cap_value=45.0, last_session_state="charging")
    confident = _thermal(will_trip=False, mtt=None, suggested=None, steady_state_c=63.8, fit_rmse_c=0.31, handle_c=50.0)
    action, state, reason = dac.decide(confident, state, cfg)
    # 65.0 - 63.8 = 1.2C = 3.9 sigma: comfortably clear, so the normal
    # restore path runs instead of the guard.
    assert action.kind == "cap" and action.value == 47.0  # stepping UP, not down
    assert "stepping up" in reason


def test_confidence_guard_disabled_by_zero_k():
    cfg = _cfg(confirm_ticks=1, forecast_confidence_k=0.0)
    state = dac.State(last_session_state="charging")
    marginal = _thermal(will_trip=False, mtt=None, suggested=None, steady_state_c=64.9, fit_rmse_c=0.31)
    action, state, _ = dac.decide(marginal, state, cfg)
    assert action.kind == "none" and not state.capped


def test_confidence_guard_skipped_without_model_error():
    # An unfitted model publishes no RMSE; with no uncertainty estimate
    # there is nothing to reason about, so the guard must stay out of the way.
    cfg = _cfg(confirm_ticks=1)
    state = dac.State(last_session_state="charging")
    no_rmse = _thermal(will_trip=False, mtt=None, suggested=None, steady_state_c=64.9, fit_rmse_c=None)
    action, state, _ = dac.decide(no_rmse, state, cfg)
    assert action.kind == "none" and not state.capped


def test_confidence_guard_respects_min_amps_floor():
    cfg = _cfg(confirm_ticks=1, min_amps=6.0, restore_step_a=2.0)
    state = dac.State(capped=True, cap_value=7.0, last_session_state="charging")
    marginal = _thermal(will_trip=False, mtt=None, suggested=None, steady_state_c=64.8, fit_rmse_c=0.31)
    action, state, _ = dac.decide(marginal, state, cfg)
    assert action.kind == "cap" and action.value == 6.0

    # Already at the floor: no further stepping, and no churn of POSTs.
    at_floor = dac.State(capped=True, cap_value=6.0, last_session_state="charging")
    action, _, reason = dac.decide(marginal, at_floor, cfg)
    assert action.kind == "none" and "floor" in reason


def test_confidence_guard_only_trusts_trajectory_basis():
    # model basis has no session-specific plateau worth second-guessing.
    cfg = _cfg(confirm_ticks=1)
    state = dac.State(last_session_state="charging")
    marginal_model = _thermal(
        basis="model", will_trip=False, mtt=None, suggested=None, steady_state_c=64.9, fit_rmse_c=0.31
    )
    action, state, _ = dac.decide(marginal_model, state, cfg)
    assert action.kind == "none" and not state.capped


def test_event_for_cap_records_the_change_and_its_justification():
    thermal = {
        "current_a": 40.0,
        "handle_c": 61.0,
        "forecast": {"basis": "trajectory", "minutes_to_trip": 12.0, "steady_state_c": 67.0},
    }
    kind, detail = dac.event_for(dac.Action("cap", 32.0), "trip in 12min", thermal, dac.State())
    assert kind == "amp_capped"
    assert detail["from_a"] == 40.0 and detail["to_a"] == 32.0
    assert detail["basis"] == "trajectory" and detail["minutes_to_trip"] == 12.0


def test_event_for_restore_steps_from_the_active_cap_not_live_current():
    # Live current can lag the cap (taper, sampling); the cap is what we set.
    thermal = {"current_a": 30.7, "handle_c": 55.0, "forecast": {"basis": "trajectory", "steady_state_c": 58.0}}
    prev = dac.State(capped=True, cap_value=32.0)
    kind, detail = dac.event_for(dac.Action("restore", 34.0), "clear streak met", thermal, prev)
    assert kind == "amp_restored"
    assert detail["from_a"] == 32.0 and detail["to_a"] == 34.0


def test_confidence_guard_prefers_projection_se_over_fit_rmse():
    # Same 0.4C gap, but the projection itself is wide (SE 0.8): 0.5 sigma.
    # Under the old fit_rmse denominator (0.1) this would read 4 sigma and
    # the guard would sleep through exactly the case it exists for.
    cfg = _cfg(confirm_ticks=1, forecast_confidence_k=2.0, restore_step_a=2.0)
    state = dac.State(last_session_state="charging")
    wide = _thermal(will_trip=False, mtt=None, suggested=None, steady_state_c=64.6,
                    steady_state_se_c=0.8, fit_rmse_c=0.1, current_a=45.0)
    action, state, reason = dac.decide(wide, state, cfg)
    assert action.kind == "cap" and action.value == 43.0
    assert "proj se" in reason and "too uncertain" in reason


def test_confidence_guard_trusts_a_tight_projection_near_the_trip():
    # 0.5C gap over a tight per-projection SE (0.12) is >4 sigma: the
    # forecast has earned trust even this close to the limit. The old
    # constant denominator (0.31) would have called this 1.6 sigma and
    # stepped down, costing charge rate for no reason.
    cfg = _cfg(confirm_ticks=1, forecast_confidence_k=2.0)
    state = dac.State(capped=True, cap_value=45.0, last_session_state="charging")
    tight = _thermal(will_trip=False, mtt=None, suggested=None, steady_state_c=64.5,
                     steady_state_se_c=0.12, fit_rmse_c=0.31, handle_c=50.0)
    action, state, reason = dac.decide(tight, state, cfg)
    assert action.kind == "cap" and action.value == 47.0  # normal restore step-up
    assert "stepping up" in reason


def test_confidence_guard_falls_back_to_fit_rmse_without_se():
    # Older server: no steady_state_se_c in the payload. The guard keeps
    # its previous behavior against fit_rmse_c.
    cfg = _cfg(confirm_ticks=1, forecast_confidence_k=2.0, restore_step_a=2.0)
    state = dac.State(last_session_state="charging")
    legacy = _thermal(will_trip=False, mtt=None, suggested=None, steady_state_c=64.6,
                      steady_state_se_c=None, fit_rmse_c=0.31, current_a=45.0)
    action, state, reason = dac.decide(legacy, state, cfg)
    assert action.kind == "cap" and action.value == 43.0
    assert "fit rmse" in reason


# --- calibration probe -------------------------------------------------
# The degradation watch can only compare windows the current held steady
# through, and on an install where this daemon caps often those are scarce
# and land wherever the capping happened to stop. The probe manufactures one
# on a cadence. Its whole contract is precedence: safety outranks it, it
# outranks restoring, and it never reports a window it did not actually hold.


def _probe_cfg(**kw):
    kw.setdefault("probe_amps", 32.0)
    kw.setdefault("probe_interval_days", 30.0)
    kw.setdefault("probe_hold_min", 40.0)
    return _cfg(**kw)


def test_probe_disabled_by_default_changes_nothing():
    # Zero probe_amps must leave the daemon byte-for-byte as it was for
    # every install that never asked for this.
    thermal = _thermal(will_trip=False, handle_c=50.0)
    plain = dac.decide(thermal, dac.State(last_session_state="charging"), _cfg())
    off = dac.decide(thermal, dac.State(last_session_state="charging"), _probe_cfg(probe_amps=0.0))
    assert plain == off


def test_probe_starts_when_due_and_caps_to_the_probe_current():
    state = dac.State(last_session_state="charging")  # never probed
    action, new, reason = dac.decide(_thermal(will_trip=False), state, _probe_cfg())
    assert action.kind == "cap" and action.value == 32.0
    assert new.probe_started_ts == 1_000_000.0 and new.last_probe_ts is None
    assert "probe due" in reason


def test_probe_not_due_until_the_interval_elapses():
    recent = dac.State(last_session_state="charging", last_probe_ts=1_000_000.0 - 10 * 86400)
    action, new, _ = dac.decide(_thermal(will_trip=False), recent, _probe_cfg())
    assert action.kind == "none" and new.probe_started_ts is None
    due = dac.State(last_session_state="charging", last_probe_ts=1_000_000.0 - 31 * 86400)
    action, _, _ = dac.decide(_thermal(will_trip=False), due, _probe_cfg())
    assert action.kind == "cap" and action.value == 32.0


def test_probe_holds_against_the_restore_path():
    # Stepping back toward full rate is exactly what would ruin the window,
    # so a trajectory-clear signal that would normally step up must not.
    held = dac.State(
        last_session_state="charging", capped=True, cap_value=32.0,
        probe_started_ts=1_000_000.0 - 10 * 60, clear_streak=9,
    )
    action, new, reason = dac.decide(
        _thermal(will_trip=False, handle_c=45.0, ts=1_000_000.0), held, _probe_cfg())
    assert action.kind == "none" and "probe holding" in reason
    assert new.cap_value == 32.0 and new.probe_started_ts is not None
    # and it cannot bank confirming polls to spend the moment it ends
    assert new.clear_streak == 0


def test_probe_completes_after_the_hold_and_restores():
    # No sustainable current from the server (older wallmonitor): release
    # to full rate as before.
    held = dac.State(
        last_session_state="charging", capped=True, cap_value=32.0,
        probe_started_ts=1_000_000.0 - 41 * 60,
    )
    action, new, reason = dac.decide(_thermal(will_trip=False), held, _probe_cfg())
    assert action.kind == "restore" and action.value == 48.0
    assert "probe complete" in reason
    assert new.probe_started_ts is None and new.last_probe_ts == 1_000_000.0
    assert not new.capped


def test_probe_completes_to_the_sustainable_current():
    # 2026-09-14, first live probe: released to 48 A on a day the idle
    # forecast had already said full rate would trip, so the controller had
    # to cap again 3.5 min later (32 -> 48 -> 42 A). The probe's own plateau
    # is the best measurement of the day; its end goes straight to the
    # current that plateau implies is sustainable.
    held = dac.State(
        last_session_state="charging", capped=True, cap_value=32.0,
        probe_started_ts=1_000_000.0 - 41 * 60,
    )
    action, new, reason = dac.decide(_thermal(will_trip=False, sustainable=44.0), held, _probe_cfg())
    assert action.kind == "cap" and action.value == 44.0
    assert "probe complete" in reason and "sustainable 44A" in reason
    assert new.capped and new.cap_value == 44.0
    assert new.probe_started_ts is None and new.last_probe_ts == 1_000_000.0
    # Counts as a step-up, so a cap soon after is a quick reversal and backs off.
    assert new.last_step_up_ts == 1_000_000.0


def test_probe_completes_holding_when_nothing_higher_is_sustainable():
    held = dac.State(
        last_session_state="charging", capped=True, cap_value=32.0,
        probe_started_ts=1_000_000.0 - 41 * 60,
    )
    action, new, reason = dac.decide(_thermal(will_trip=False, sustainable=30.0), held, _probe_cfg())
    assert action.kind == "none"
    assert new.capped and new.cap_value == 32.0 and "holding" in reason
    assert new.probe_started_ts is None and new.last_probe_ts == 1_000_000.0


def test_probe_completes_to_full_rate_when_that_is_sustainable():
    held = dac.State(
        last_session_state="charging", capped=True, cap_value=32.0,
        probe_started_ts=1_000_000.0 - 41 * 60,
    )
    action, new, _ = dac.decide(_thermal(will_trip=False, sustainable=48.0), held, _probe_cfg())
    assert action.kind == "restore" and action.value == 48.0 and not new.capped


def test_safety_cap_below_probe_current_wins_and_abandons_the_probe():
    # A real thermal decision outranks calibration, and a window whose
    # current just moved teaches nothing — so it is abandoned, not banked.
    held = dac.State(
        last_session_state="charging", capped=True, cap_value=32.0,
        probe_started_ts=1_000_000.0 - 5 * 60, trip_streak=2,
    )
    action, new, reason = dac.decide(
        _thermal(will_trip=True, mtt=5.0, suggested=28.0), held, _probe_cfg())
    assert action.kind == "cap" and action.value == 28.0
    assert new.probe_started_ts is None
    assert "probe abandoned" in reason
    # Abandoned, not completed: the next session must retry it.
    assert new.last_probe_ts is None


def test_probe_abandoned_by_an_unplug_does_not_consume_the_slot():
    held = dac.State(
        last_session_state="charging", capped=True, cap_value=32.0,
        probe_started_ts=1_000_000.0 - 5 * 60,
    )
    _, new, _ = dac.decide(_thermal(state="idle"), held, _probe_cfg())
    assert new.probe_started_ts is None and new.last_probe_ts is None


def test_probe_does_not_start_from_a_lower_thermal_cap():
    # A session already capped under the probe current has bigger problems
    # than calibration; forcing it up to 32 A would be actively unsafe.
    capped_low = dac.State(
        last_session_state="charging", capped=True, cap_value=26.0, clear_streak=0)
    action, new, _ = dac.decide(
        _thermal(will_trip=False, handle_c=63.0), capped_low, _probe_cfg())
    assert action.kind != "cap" or (action.value or 0) >= 32.0
    assert new.probe_started_ts is None


def _plan_cfg(**kw):
    kw.setdefault("probe_amps", (32.0, 40.0))
    kw.setdefault("probe_cable", ("cold", "warm"))
    kw.setdefault("probe_replicates", 2)
    kw.setdefault("probe_plan_interval_days", 7.0)
    kw.setdefault("probe_interval_days", 30.0)
    kw.setdefault("probe_hold_min", 40.0)
    kw.setdefault("probe_cold_gap_h", 4.0)
    kw.setdefault("probe_warm_min", 30.0)
    return _cfg(**kw)


def _simulate(cfg, days, session_min=120, sessions_per_day=1, start_state=None, capped_at=None):
    """Walk decide() through `days` of daily charging sessions, 30 s ticks,
    the car following every cap instantly and a clear, cool forecast
    throughout (sustainable = full rate). Returns (probe starts as
    [(day, minute_into_session, amps, cable)], final state)."""
    t0 = 1_000_000.0
    # A fresh state file knows no previous charge, so day 0 could never be
    # cold-cable; give the daemon a last charge 12 h before the timeline.
    state = start_state or dac.State(last_charging_ts=t0 - 12 * 3600)
    starts = []
    for day in range(days):
        for n in range(sessions_per_day):
            begin = t0 + day * 86400 + n * (24 * 3600 / sessions_per_day)
            # an idle tick shortly before charging begins
            _, state, _ = dac.decide(_thermal(state="idle", ts=begin - 60, current_a=0.0), state, cfg)
            ts = begin
            while ts < begin + session_min * 60:
                if capped_at is not None:
                    current = capped_at
                    state = dac.replace(state, capped=True, cap_value=capped_at) if not state.probe_started_ts else state
                else:
                    current = state.cap_value if state.capped and state.cap_value else cfg.normal_amps
                snap = _thermal(will_trip=False, mtt=None, suggested=None, handle_c=45.0, ts=ts,
                                current_a=current, sustainable=cfg.normal_amps)
                action, state, reason = dac.decide(snap, state, cfg)
                if action.probe is not None:
                    starts.append((day, round((ts - begin) / 60), action.probe["amps"], action.probe["cable"]))
                ts += 30.0
            _, state, _ = dac.decide(_thermal(state="idle", ts=ts + 60, current_a=0.0), state, cfg)
    return starts, state


def test_probe_plan_visits_every_condition_least_replicated_first_then_goes_monthly():
    starts, state = _simulate(_plan_cfg(), days=85)
    # Weekly while collecting: cold probes open a session, warm ones wait
    # for 30 min at full rate in the same session; then two of each and the
    # cadence relaxes to monthly, cycling from the top of the plan again.
    assert [(d, a, c) for d, _, a, c in starts] == [
        (0, 32.0, "cold"), (7, 32.0, "warm"), (14, 40.0, "cold"), (21, 40.0, "warm"),
        (28, 32.0, "cold"), (35, 32.0, "warm"), (42, 40.0, "cold"), (49, 40.0, "warm"),
        (79, 32.0, "cold"),
    ]
    assert all(m == 0 for _, m, _, c in starts if c == "cold")
    assert all(m == 30 for _, m, _, c in starts if c == "warm")
    assert len(state.probes_done) == 9
    assert {(p["amps"], p["cable"]) for p in state.probes_done[:8]} == {
        (32.0, "cold"), (32.0, "warm"), (40.0, "cold"), (40.0, "warm")}
    assert not state.capped  # every probe restored afterwards


def test_probe_cold_needs_a_long_gap_since_the_last_charge():
    # Sessions two hours apart never let the cable cool: a cold-only plan
    # starts nothing, however overdue it gets.
    busy = dac.State(last_charging_ts=1_000_000.0 - 3600)
    starts, _ = _simulate(_plan_cfg(probe_cable=("cold",)), days=21, session_min=60, sessions_per_day=12,
                          start_state=busy)
    assert starts == []
    # A fresh state file knows no previous charge: the first session cannot
    # be called cold, the second can.
    starts, _ = _simulate(_plan_cfg(probe_cable=("cold",)), days=2, start_state=dac.State())
    assert [(d, m) for d, m, _, _ in starts] == [(1, 0)]
    # ...and a daemon upgraded mid-session does not know when it began.
    mid = dac.State(last_session_state="charging", last_charging_ts=1_000_000.0 - 30)
    action, new, _ = dac.decide(_thermal(will_trip=False, ts=1_000_000.0), mid, _plan_cfg(probe_cable=("cold",)))
    assert action.probe is None and new.charging_since_ts is None


def test_probe_warm_needs_full_rate_uncapped_first():
    # With a thermal cap standing the current is not full rate, so a
    # warm-only plan cannot start; the restore path is not blocked by the
    # wait either.
    starts, _ = _simulate(_plan_cfg(probe_cable=("warm",)), days=8, capped_at=40.0)
    assert starts == []
    # Uncapped, it starts exactly at the warm threshold and never at the session's opening.
    starts, _ = _simulate(_plan_cfg(probe_cable=("warm",)), days=8)
    assert [(d, m) for d, m, _, _ in starts] == [(0, 30), (7, 30)]


def test_probe_waits_for_the_condition_that_needs_it_most_unless_overdue():
    cfg = _plan_cfg(probe_amps=(32.0,))
    done = [{"ts": 1.0, "amps": 32.0, "cable": "cold"}] * 2  # cold has its replicates, warm has none
    last_charge = 1_000_000.0 - 12 * 3600
    fresh = dac.State(probes_done=done, last_probe_ts=1_000_000.0 - 8 * 86400, last_charging_ts=last_charge)
    starts, _ = _simulate(cfg, days=1, start_state=fresh)
    assert [(m, c) for _, m, _, c in starts] == [(30, "warm")]  # cold was eligible at minute 0 and was passed over
    # Fifteen days since the last probe — more than twice the plan interval
    # — and the slot goes to whatever is eligible now.
    overdue = dac.State(probes_done=done, last_probe_ts=1_000_000.0 - 15 * 86400, last_charging_ts=last_charge)
    starts, _ = _simulate(cfg, days=1, start_state=overdue)
    assert [(m, c) for _, m, _, c in starts] == [(0, "cold")]


def test_probe_single_current_any_cable_keeps_the_monthly_cadence():
    # The pre-plan configuration: one current, whenever due. The plan
    # interval and the replicate count must not touch it.
    starts, _ = _simulate(_probe_cfg(probe_replicates=2, probe_plan_interval_days=7.0), days=45)
    assert [(d, c) for d, _, _, c in starts] == [(0, "any"), (30, "any")]


def test_probe_one_per_session():
    # A 3 h session with a 32 A cold probe complete at 40 min: the warm
    # condition becomes eligible later in the same session and must wait
    # for the next one.
    starts, _ = _simulate(_plan_cfg(probe_amps=(32.0,)), days=2, session_min=180)
    assert [(d, c) for d, _, _, c in starts] == [(0, "cold")]  # day 1 is inside the 7-day interval


def test_probe_start_event_carries_its_condition():
    action = dac.Action("cap", 40.0, probe={"amps": 40.0, "cable": "warm"})
    kind, detail = dac.event_for(action, "calibration probe due (warm cable)", _thermal(), dac.State())
    assert kind == "amp_capped" and detail["probe"] == {"amps": 40.0, "cable": "warm"}
    _, plain = dac.event_for(dac.Action("cap", 44.0), "trip", _thermal(), dac.State())
    assert "probe" not in plain


def test_probe_cli_parses_a_plan_and_rejects_a_bad_one(capsys):
    unreachable = ["--tesla-ble", "http://127.0.0.1:1", "--wallmonitor", "http://127.0.0.1:1", "--dry-run"]
    assert dac.main(["--probe-amps", "32,40", "--probe-cable", "cold,warm", *unreachable]) == 0
    assert "cannot reach wallmonitor" in capsys.readouterr().err
    for bad in (["--probe-cable", "any,cold"], ["--probe-cable", "hot"], ["--probe-amps", "32,x"], ["--probe-amps", "60"]):
        with pytest.raises(SystemExit):
            dac.main([*bad, *unreachable])


def test_probe_state_survives_an_old_state_file(tmp_path):
    # A daemon upgrade reads state written before the probe existed. It must
    # default the new fields rather than crash — and a probe must then be
    # due, not silently skipped by a missing last_probe_ts.
    path = tmp_path / "state.json"
    path.write_text('{"capped": true, "cap_value": 40.0, "last_session_state": "charging"}')
    state = dac.load_state(str(path))
    assert state.capped and state.cap_value == 40.0
    assert state.probe_started_ts is None and state.last_probe_ts is None
    action, _, reason = dac.decide(_thermal(will_trip=False), state, _probe_cfg())
    assert action.kind == "cap" and action.value == 32.0 and "probe due" in reason
    # and the round-trip back to disk keeps the new fields
    dac.save_state(str(path), dac.State(probe_started_ts=1.0, last_probe_ts=2.0))
    assert dac.load_state(str(path)).last_probe_ts == 2.0
    # A hold in progress recorded before the plan existed carries no
    # current; that daemon had exactly one, and the hold continues at it.
    path.write_text('{"capped": true, "cap_value": 32.0, "last_session_state": "charging", '
                    '"probe_started_ts": 999000.0}')
    state = dac.load_state(str(path))
    assert state.probes_done == [] and state.probe_amps is None
    action, new, reason = dac.decide(_thermal(will_trip=False), state, _probe_cfg())
    assert action.kind == "none" and "probe holding 32A" in reason and new.cap_value == 32.0


def _probe_events(start_ts, amps=32.0, cable="cold", next_after_min=41.0, next_reason="probe complete"):
    events = [{"ts": start_ts, "kind": "amp_capped",
               "detail": json.dumps({"to_a": amps, "reason": "calibration probe due", "probe": {"amps": amps, "cable": cable}})}]
    if next_after_min is not None:
        events.append({"ts": start_ts + next_after_min * 60, "kind": "amp_restored",
                       "detail": json.dumps({"to_a": 48.0, "reason": next_reason})})
    return events


def test_probe_history_rebuilt_from_events_keeps_the_plan_on_cadence():
    # The 2026-09-24 incident: a reboot cleared /tmp three days after a
    # 32 A cold probe, and the next session probed 32 A cold again. Rebuilt
    # from the event log, that session is not due; a week on, the plan moves
    # to a condition with no replicates rather than repeating the first.
    t0 = 1_000_000.0
    last_charge = t0 - 12 * 3600
    for days_ago, expected in ((3, []), (7, [(30, 32.0, "warm")])):
        session_start = t0 - days_ago * 86400
        sessions = [{"start_ts": session_start - 5, "end_ts": session_start + 14 * 3600}]
        done, last = dac.probe_history_from_events(_probe_events(session_start + 45), sessions, t0 - 3600, _plan_cfg())
        assert done == [{"ts": session_start + 45 + 2400, "amps": 32.0, "cable": "cold"}]
        assert last == session_start - 5  # anchored at the plug-in, as decide() anchors it
        state = dac.State(probes_done=done, last_probe_ts=last, probe_history_checked=True, last_charging_ts=last_charge)
        starts, _ = _simulate(_plan_cfg(), days=1, start_state=state)
        assert [(m, a, c) for _, m, a, c in starts] == expected


def test_probe_history_counts_only_holds_that_ran_their_full_length():
    cfg = _plan_cfg()
    now = 2_000_000.0
    abandoned = _probe_events(1_000_000.0, next_after_min=12.0, next_reason="session ended: restoring normal rate")
    # a probe that ends on a sustainable current no higher than its own logs nothing at completion
    silent = _probe_events(1_100_000.0, amps=40.0, cable="warm", next_after_min=None)
    legacy = [{"ts": 1_200_000.0, "kind": "amp_capped",
               "detail": {"to_a": 32.0, "reason": "calibration probe due: capping to 32A for 40min"}},
              {"ts": 1_200_000.0 + 2460, "kind": "amp_restored", "detail": None}]
    thermal_cap = [{"ts": 1_300_000.0, "kind": "amp_capped", "detail": json.dumps({"to_a": 44.0, "reason": "plateau"})}]
    events = list(reversed(abandoned + silent + legacy + thermal_cap))  # the API returns newest first
    done, last = dac.probe_history_from_events(events, [], now, cfg)
    assert [(p["amps"], p["cable"]) for p in done] == [(40.0, "warm"), (32.0, "any")]
    assert last == 1_200_000.0  # no session known: anchored at the start itself
    # ...but a probe still inside its hold is not yet complete
    done, last = dac.probe_history_from_events(silent, [], 1_100_000.0 + 600, cfg)
    assert done == [] and last is None


def test_main_rebuilds_an_empty_probe_history_once(tmp_path, monkeypatch, capsys):
    state_file = tmp_path / "state.json"
    args = ["--tesla-ble", "http://127.0.0.1:1", "--wallmonitor", "http://wm", "--state-file", str(state_file),
            "--probe-amps", "32,40", "--probe-cable", "cold,warm"]
    snap = _thermal(will_trip=False, ts=1_000_000.0, current_a=48.0)
    monkeypatch.setattr(dac, "fetch_thermal", lambda url: snap)
    calls = []

    def recovered(url, state, cfg, now_ts):
        calls.append(url)
        return dac.replace(state, probes_done=[{"ts": 1.0, "amps": 32.0, "cable": "cold"}],
                           last_probe_ts=snap["ts"] - 3 * 86400, probe_history_checked=True)

    monkeypatch.setattr(dac, "recover_probe_history", recovered)
    assert dac.main(args) == 0
    assert calls == ["http://wm"] and "rebuilt from the event log: 1" in capsys.readouterr().out
    assert dac.load_state(str(state_file)).probe_history_checked
    dac.main(args)
    assert len(calls) == 1  # never again once checked


def test_main_holds_the_plan_when_the_history_cannot_be_rebuilt(tmp_path, monkeypatch, capsys):
    # Brand-new state with a cold session starting: without the fallback
    # this is exactly the tick that would probe off-cadence.
    state_file = tmp_path / "state.json"
    dac.save_state(str(state_file), dac.State(last_charging_ts=1_000_000.0 - 12 * 3600))
    monkeypatch.setattr(dac, "fetch_thermal", lambda url: _thermal(will_trip=False, ts=1_000_000.0, current_a=48.0))

    def unreachable(*a):
        raise dac.urllib.error.URLError("refused")

    monkeypatch.setattr(dac, "recover_probe_history", unreachable)
    args = ["--tesla-ble", "http://127.0.0.1:1", "--wallmonitor", "http://wm", "--state-file", str(state_file),
            "--probe-amps", "32", "--probe-cable", "cold", "--dry-run"]
    assert dac.main(args) == 0
    out = capsys.readouterr()
    assert "no probe this tick" in out.err and "would cap" not in out.out
