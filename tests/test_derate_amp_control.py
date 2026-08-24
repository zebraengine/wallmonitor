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
import pathlib
import sys

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
