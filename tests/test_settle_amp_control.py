"""settle_amp_control.decide() is pure, like the daemon's. The behaviors
pinned here are the ones the closed-loop backtest found the hard way: one
reading per steady stretch (a draft counted every tick and held 48 A into a
64.3 C peak), stepping down on the live stretch's own reading when it runs
hotter than the session's average (the whole-history run: seven trips
without it), and the handle outranking the estimate near the trip."""

import importlib.util
import math
import pathlib
import sys

CONTRIB = pathlib.Path(__file__).parent.parent / "contrib"


def _load(name):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, CONTRIB / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclass annotation resolution needs this
    spec.loader.exec_module(module)
    return module


_load("derate_amp_control")  # settle imports Action and the trip point from it
sac = _load("settle_amp_control")

MODEL = {"tau_min": 11.0, "rise_ref_c": 36.0, "ref_current_a": 48.0, "current_exp": 1.5, "trip_c": 65.0}
TAU_S = 11.0 * 60.0


def _thermal(ts, handle, current=48.7, steady=None, prior=25.0, state="charging"):
    forecast = {"basis": "trajectory" if steady is not None else "model", "window_current_a": current,
                "prior_ambient_c": prior, "prior_ambient_sd_c": 0.3}
    if steady is not None:
        forecast.update(steady_state_c=steady, steady_state_se_c=0.1)
    return {"state": state, "ts": ts, "handle_c": handle, "current_a": current, "model": MODEL, "forecast": forecast}


def _run(ticks, cfg=None, state=None):
    """Feed (ts, handle, current, steady) ticks; return the actions and final state."""
    cfg = cfg or sac.Config()
    state = state or sac.State(last_session_state="idle")
    actions = []
    for ts, handle, current, steady in ticks:
        action, state, _why = sac.decide(_thermal(ts, handle, current, steady), state, cfg)
        actions.append(action)
    return actions, state


def test_idle_does_nothing_and_session_end_restores():
    action, state, _ = sac.decide(_thermal(0, 25.0, state="idle"), sac.State(), sac.Config())
    assert action.kind == "none"
    capped = sac.State(last_session_state="charging", cap=42.0)
    action, state, _ = sac.decide(_thermal(0, 40.0, state="idle"), capped, sac.Config())
    assert (action.kind, action.value) == ("restore", 48.0)
    assert state.cap is None


def test_front_load_holds_full_rate_while_the_handle_is_cold():
    # A plateau far over the trip, but the handle is 30 C under it: no move yet.
    ticks = [(30.0 * i, 30.0 + i, 48.7, 72.0 if i >= 4 else None) for i in range(1, 12)]
    actions, state = _run(ticks)
    assert all(action.kind == "none" for action in actions)
    assert state.frontloading


def _closed_loop(theta, prior, minutes, cfg=None, start_handle=30.0):
    """Run decide() against a handle that answers its commands: one time
    constant toward theta + rise(I), the car drawing 0.6 A over the cap, and
    a trajectory forecast (the true plateau) from two minutes into each
    stretch. Returns ([(minute, kind, amps)], peak handle, final state)."""
    cfg = cfg or sac.Config()
    state = sac.State(last_session_state="idle")
    handle, command, stretch_ticks = start_handle, cfg.normal_amps, 0
    moves, peak = [], handle
    for i in range(1, int(minutes * 2) + 1):
        current = command + 0.6
        plateau = theta + sac._rise(MODEL, current)
        handle = plateau + (handle - plateau) * math.exp(-30.0 / TAU_S)
        peak = max(peak, handle)
        stretch_ticks += 1
        steady = plateau if stretch_ticks >= 4 else None
        action, state, _why = sac.decide(_thermal(30.0 * i, handle, current, steady, prior), state, cfg)
        if action.kind != "none":
            moves.append((i / 2, action.kind, action.value))
            command, stretch_ticks = action.value, 0
    return moves, peak, state


def test_front_load_drops_once_to_the_target_when_the_handle_nears_the_plateau():
    # Effective ambient 33 C: full rate would settle at 69.7 C. Open loop
    # (a handle that keeps climbing whatever is commanded) this scenario ends
    # in a run of emergency cuts, which says nothing about the planned drop;
    # the handle has to follow the cap for "once" to mean anything.
    moves, peak, state = _closed_loop(theta=33.0, prior=31.0, minutes=120)
    # One move in two hours, and it is the largest whole amp that fits:
    # 42 A settles at 63.1 C, 43 A would settle at 64.2 C.
    assert [(kind, amps) for _minute, kind, amps in moves] == [("cap", 42.0)]
    assert not state.frontloading
    assert peak < MODEL["trip_c"] - sac.Config().soak_margin_c


def test_a_long_stretch_is_one_reading_not_one_per_tick():
    # 40 minutes at one current: the stretch must never be banked as finished.
    ticks = [(30.0 * i, 50.0, 48.7, 60.0) for i in range(1, 81)]
    _actions, state = _run(ticks)
    assert state.done == []
    assert state.live is not None


def test_a_current_change_banks_the_stretch_and_starts_a_new_one():
    ticks = [(30.0 * i, 50.0, 48.7, 60.0) for i in range(1, 30)]
    ticks += [(30.0 * i, 50.0, 44.6, 57.0) for i in range(30, 40)]
    _actions, state = _run(ticks)
    assert len(state.done) == 1
    assert state.stretch_current_a == 44.6


def test_steps_down_on_the_live_reading_when_it_runs_hotter_than_the_average():
    # Capped at 44 A with a cool prior and a cool earlier stretch; the present
    # stretch, well past half a time constant, projects over the trip. The
    # fused average alone would hold; the live reading must force the step.
    cfg = sac.Config(frontload_c=None)
    state = sac.State(last_session_state="charging", cap=44.0, stretch_start_ts=0.0, stretch_current_a=44.6,
                      done=[(0.0, 26.0, 1.0)], last_move_ts=0.0)
    action, state, why = sac.decide(_thermal(TAU_S, 60.0, 44.6, steady=66.0, prior=25.0), state, cfg)
    assert action.kind == "cap" and action.value < 44.0, why


def test_emergency_cut_ignores_the_estimate():
    # The estimate says 44 A is fine; the handle is at 63.6 C and has climbed
    # 0.5 C in two minutes. Cut 2 A regardless.
    cfg = sac.Config(frontload_c=None)
    state = sac.State(last_session_state="charging", cap=44.0, stretch_start_ts=0.0, stretch_current_a=44.6,
                      handle_log=[(TAU_S - 120.0, 63.1)], last_move_ts=0.0)
    action, _state, why = sac.decide(_thermal(TAU_S, 63.6, 44.6, steady=62.0, prior=25.0), state, cfg)
    assert (action.kind, action.value) == ("cap", 42.0), why


def test_one_tick_of_sensor_noise_is_not_rising():
    cfg = sac.Config(frontload_c=None)
    state = sac.State(last_session_state="charging", cap=44.0, stretch_start_ts=0.0, stretch_current_a=44.6,
                      handle_log=[(TAU_S - 120.0, 63.5), (TAU_S - 30.0, 63.5)], last_move_ts=0.0)
    action, _state, _why = sac.decide(_thermal(TAU_S, 63.6, 44.6, steady=62.0, prior=25.0), state, cfg)
    assert action.kind == "none"


def test_step_up_waits_for_a_mature_stretch_and_a_rested_controller():
    cfg = sac.Config(frontload_c=None)
    base = dict(last_session_state="charging", cap=40.0, stretch_current_a=40.6)
    # Young stretch, just moved: hold even though the estimate allows more.
    young = sac.State(**base, stretch_start_ts=0.0, last_move_ts=0.0)
    action, _s, _w = sac.decide(_thermal(120.0, 50.0, 40.6, steady=52.0, prior=22.0), young, cfg)
    assert action.kind == "none"
    # A time constant on, same reading: now it may go up, and by more than 1 A.
    mature = sac.State(**base, stretch_start_ts=0.0, last_move_ts=0.0)
    action, _s, why = sac.decide(_thermal(TAU_S + 30.0, 50.0, 40.6, steady=52.0, prior=22.0), mature, cfg)
    assert action.kind in ("cap", "restore") and action.value > 41.0, why
