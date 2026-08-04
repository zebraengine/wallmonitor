"""decide() is pure, so the whole cap/restore decision table tests without
a wallmonitor server, an ESP32, or a vehicle. The behaviors under test
reflect two real incidents: (2026-08-01) trust only `trajectory`/`model`
bases, never `hypothetical`, and debounce over several confirming polls;
(2026-08-03) capping and restoring are NOT symmetric — capping trusts
`model` basis too (the safe direction, and `trajectory` alone left a
multi-minute blind spot after every amp change), while restoring stays
narrow: `trajectory` only, one step at a time, and gated on real thermal
margin, because snapping straight back to full current converted a caught
derate into repeated near-misses before one wasn't caught in time."""

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
    return dac.Config(**kw)


def _thermal(
    state="charging",
    basis="trajectory",
    will_trip=True,
    mtt: float | None = 10.0,
    suggested: float | None = 44.0,
    handle_c: float | None = 50.0,  # well under trip - margin unless a test says otherwise
):
    return {
        "state": state,
        "handle_c": handle_c,
        "forecast": {
            "basis": basis,
            "will_trip": will_trip,
            "minutes_to_trip": mtt,
            "suggested_max_a": suggested,
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
