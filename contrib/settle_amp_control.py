#!/usr/bin/env python3
"""Candidate replacement for derate_amp_control's derate decision: settle on
the highest sustainable charge current in one or two moves instead of
hunting for it.

**Experimental — not wired into the daemon.** It exists so the closed-loop
backtest (backtest_settle_amp_control.py) can score it against the deployed
``decide()`` on the whole history before anything is deployed.

Why: derate_amp_control's restore path decides to step up from the plateau
at the *present* current plus the handle's instantaneous margin, never from
the plateau at the *target* current, and its cap path trusts a
two-minute-old trajectory fit. 2026-10-02 (session 101): 49 -> 40 -> 46 ->
44 -> 46 -> 48 -> 45 -> 41 A in eighteen minutes, then the quick-reversal
back-off pinned the session at 41 A when ~44 A was sustainable.

The idea is one number tracked per session — the **effective ambient**
``theta = plateau - rise(I)``, what the handle would settle at with no
current — and one target derived from it:

    I* = largest whole amp with  theta + k * sd + rise(I) <= trip - soak_margin

``theta`` is an inverse-variance fusion of

- a prior: the ambient sensor (or the pre-session idle-handle proxy), and
- **one reading per steady-current stretch**: that stretch's projected
  plateau minus the model's rise at its current. A stretch is a single
  reading however long it runs — consecutive ticks are the same data, and
  counting them as independent lets stale early readings outvote the live
  one (that bug, in a draft of this file, held 48 A into a 64.3 C peak). A
  young stretch is distrusted explicitly: its sd is floored at
  ``floor_sd_c + young_sd_c * exp(-age / tau)``, because the projection's
  own standard error measures residual noise, not how wrong a single
  exponential is two minutes after a current change.

The moves:

- **Front-load.** The first minutes at full rate are thermally free (the
  handle is 20-30 C under the trip) and are the best reading of the day
  there is, taken at the reference current with no exponent extrapolation.
  Hold full rate until the handle is within ``frontload_c`` of the target
  plateau, then make one planned drop to I* (at ``k_down``: this is a first
  landing, not a step-up, and landing 1 A high is corrected by an ordinary
  1 A step down).
- **Down** whenever I* (at ``k_down``) is at least 1 A under the present
  current. Not rate-limited, and judged on the higher of the fused theta
  and the present stretch's own reading: the cable soaks through a hot
  session, so earlier readings are of a cooler cable.
- **Up** only when I* (at ``k_up``) is at least 1 A over, the present
  stretch is at least a time constant old, the handle has margin, and a
  time constant has passed since the last move. A step-up undone within a
  time constant widens ``k_up`` for the rest of the session — it never
  gives up and pins the session low.
- **Emergency:** handle at ``emergency_c`` and still rising (against itself
  two minutes ago, not the last tick) — cut 2 A below the present current,
  or below I* if that is lower, whatever the estimate says.

``decide()`` takes the same ``/api/thermal`` dict as the daemon's, plus
three forecast fields the server does not serve yet (the backtest computes
them): ``window_current_a``, ``prior_ambient_c``/``prior_ambient_sd_c``, and
a ``steady_state_c`` projected over a tight current band (3% of the
current, not 10%) so a 2-3 A step starts a fresh window instead of mixing
two currents in one fit.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

from derate_amp_control import TRIP_HANDLE_C, Action


@dataclass
class Config:
    normal_amps: float = 48.0
    min_amps: float = 16.0
    k_up: float = 1.5
    k_down: float = 0.5
    soak_margin_c: float = 1.5  # the cable keeps soaking after the handle looks settled
    prior_sd_c: float = 2.5  # floor on the prior's sd, whatever its source claims
    floor_sd_c: float = 1.0  # sd of a mature stretch's reading
    young_sd_c: float = 4.0  # extra sd of a brand-new stretch, decaying over tau
    drift_c_per_min: float = 0.025  # a finished stretch's reading ages as the cable soaks
    frontload_c: float | None = 4.0  # None: land on I* at the first tick instead
    up_handle_max_c: float = 63.5
    emergency_c: float = 63.5
    reversal_k_step: float = 0.5
    # A stretch younger than this many time constants reads low after a
    # step down (the fixed-tau projection on a still-cooling cable: session
    # 101 read 58.4 C at 24 min against 60.8 C settled), so it may raise the
    # estimate but not pull it under the last long stretch's. 0 disables.
    young_guard_tau: float = 2.0
    # "Rising" is the handle against itself this long ago, by at least
    # rising_min_c: one tick of a 0.1 C sensor is a coin flip at the plateau.
    live_down_tau: float = 0.5  # stretch age (in tau) from which its own reading can force a step down
    rising_window_s: float = 120.0
    rising_min_c: float = 0.3


@dataclass
class State:
    last_session_state: str | None = None
    cap: float | None = None  # None: uncapped, drawing normal_amps
    frontloading: bool = False
    stretch_start_ts: float | None = None
    stretch_current_a: float | None = None
    live: tuple[float, float] | None = None  # (theta, sd) of the present stretch
    done: list[tuple[float, float, float]] = field(default_factory=list)  # finished stretches: (ts, theta, sd)
    last_move_ts: float | None = None
    last_up_ts: float | None = None
    k_extra: float = 0.0
    handle_log: list[tuple[float, float]] = field(default_factory=list)  # recent (ts, handle_c)
    anchor_theta_c: float | None = None  # theta of the last stretch that ran long enough to trust
    theta_c: float | None = None  # the fused estimate, kept for the log


def _rise(model: dict, amps: float) -> float:
    return model["rise_ref_c"] * (amps / model["ref_current_a"]) ** model["current_exp"]


def _amps_for(theta: float, headroom_c: float, model: dict, cfg: Config) -> float:
    """Largest whole amp whose plateau sits headroom_c + soak_margin_c under the trip."""
    budget = model.get("trip_c", TRIP_HANDLE_C) - cfg.soak_margin_c - theta - headroom_c
    if budget <= 0:
        return cfg.min_amps
    amps = model["ref_current_a"] * (budget / model["rise_ref_c"]) ** (1.0 / model["current_exp"])
    return float(max(cfg.min_amps, min(cfg.normal_amps, math.floor(amps))))


def decide(thermal: dict, state: State, cfg: Config) -> tuple[Action, State, str]:
    """What to do, the state to persist, and why."""
    forecast = thermal.get("forecast") or {}
    model = thermal.get("model") or {}
    now = thermal.get("ts")
    handle = thermal.get("handle_c")

    if thermal.get("state") != "charging":
        new = State(last_session_state=thermal.get("state"))
        if state.cap is not None:
            return Action("restore", cfg.normal_amps), new, "session ended: restoring normal rate"
        return Action("none"), new, "idle: nothing to do"

    session_started = state.last_session_state != "charging"
    st = State(last_session_state="charging") if session_started else replace(state)
    tau_s = model["tau_min"] * 60.0
    trip_c = model.get("trip_c", TRIP_HANDLE_C)

    # One steady-current stretch is one reading. Tracked here rather than
    # read off the server's sliding sample buffer, whose window start moves
    # every tick once a stretch outlasts it.
    current = forecast.get("window_current_a") or thermal.get("current_a")
    if st.stretch_current_a is None or abs(current - st.stretch_current_a) > max(1.0, 0.03 * current):
        if st.live is not None:
            st.done = st.done + [(now, *st.live)]
            if cfg.young_guard_tau and now - st.stretch_start_ts >= cfg.young_guard_tau * tau_s:
                st.anchor_theta_c = st.live[0]
        st.stretch_start_ts, st.stretch_current_a, st.live = now, current, None
    age_s = now - st.stretch_start_ts
    if forecast.get("basis") == "trajectory" and isinstance(forecast.get("steady_state_c"), (int, float)):
        se = forecast.get("steady_state_se_c") or 0.1
        sd = max(se, cfg.floor_sd_c + cfg.young_sd_c * math.exp(-age_s / tau_s))
        st.live = (forecast["steady_state_c"] - _rise(model, current), sd)

    readings: list[tuple[float, float]] = []
    prior = forecast.get("prior_ambient_c")
    if isinstance(prior, (int, float)):
        readings.append((prior, max(cfg.prior_sd_c, forecast.get("prior_ambient_sd_c") or 0.0)))
    for ts, theta, sd in st.done:
        readings.append((theta, math.hypot(sd, cfg.drift_c_per_min * (now - ts) / 60.0)))
    if st.live is not None:
        readings.append(st.live)
    if not readings:
        st.frontloading = st.frontloading or session_started
        return Action("none"), st, "no ambient and no trajectory yet: holding"
    weight = sum(1.0 / sd**2 for _, sd in readings)
    theta = sum(value / sd**2 for value, sd in readings) / weight
    sd = math.sqrt(1.0 / weight)
    if st.anchor_theta_c is not None and age_s < cfg.young_guard_tau * tau_s:
        theta = max(theta, st.anchor_theta_c)
    st.theta_c = theta

    # The fusion treats theta as a constant; it is not — the cable soaks
    # through a hot session and every earlier reading is of a cooler one.
    # Averaging them in is optimistic exactly when it matters, so stepping
    # *down* is decided on whichever is higher: the fused estimate or the
    # present stretch's own reading once it is half a time constant old.
    # (Whole-history backtest: without this, hot days landed 3-4 A high and
    # walked down 1 A at a time into the trip — seven sessions.)
    theta_down = theta
    if st.live is not None and age_s >= cfg.live_down_tau * tau_s:
        theta_down = max(theta, st.live[0])

    cur = st.cap if st.cap is not None else cfg.normal_amps
    k_up = cfg.k_up + st.k_extra
    i_down = _amps_for(theta_down, cfg.k_down * sd, model, cfg)
    i_up = _amps_for(theta, k_up * sd, model, cfg)
    rising = False
    if isinstance(handle, (int, float)):
        log = [(ts, value) for ts, value in st.handle_log if now - ts <= cfg.rising_window_s + 45.0] + [(now, handle)]
        st.handle_log = log
        earlier = [value for ts, value in log if now - ts >= cfg.rising_window_s]
        rising = bool(earlier) and handle - earlier[-1] >= cfg.rising_min_c
    tag = f"theta {theta:.1f}+-{sd:.1f}C from {len(readings)} readings, I* {i_down:g}A down / {i_up:g}A up"

    def move(target: float, why: str) -> tuple[Action, State, str]:
        going_up = target > cur
        undone = not going_up and st.last_up_ts is not None and now - st.last_up_ts <= tau_s
        new = replace(
            st,
            cap=None if target >= cfg.normal_amps else float(target),
            last_move_ts=now,
            last_up_ts=now if going_up else st.last_up_ts,
            k_extra=st.k_extra + (cfg.reversal_k_step if undone else 0.0),
        )
        if target >= cfg.normal_amps:
            return Action("restore", cfg.normal_amps), new, f"{why}: {cur:g} -> {cfg.normal_amps:g}A ({tag})"
        return Action("cap", float(target)), new, f"{why}: {cur:g} -> {target:g}A ({tag})"

    if session_started:
        if cfg.frontload_c is not None:
            st.frontloading = True
            return Action("none"), st, f"front-loading at full rate ({tag})"
        if i_up < cur:
            return move(i_up, "landing")
        return Action("none"), st, f"landing at full rate ({tag})"

    if st.frontloading:
        drop_at = trip_c - cfg.soak_margin_c - k_up * sd - cfg.frontload_c
        if isinstance(handle, (int, float)) and handle >= drop_at:
            st.frontloading = False
            if i_down < cur:
                return move(i_down, f"front-load done at {handle:.1f}C")
            return Action("none"), st, f"front-load done at {handle:.1f}C, full rate holds ({tag})"
        return Action("none"), st, f"front-loading at full rate ({tag})"

    # The handle itself outranks every estimate: this close to the trip and
    # still climbing, cut 2 A below wherever we are (or below I*, if lower).
    if isinstance(handle, (int, float)) and handle >= cfg.emergency_c and rising:
        return move(max(cfg.min_amps, min(cur, i_down) - 2.0), f"handle {handle:.1f}C and rising")
    if i_down <= cur - 1.0:
        return move(i_down, "stepping down")
    if i_up >= cur + 1.0 and cur < cfg.normal_amps:
        mature = st.live is not None and (age_s >= tau_s or st.live[1] <= 1.5)
        rested = st.last_move_ts is None or now - st.last_move_ts >= tau_s
        cool = isinstance(handle, (int, float)) and handle <= cfg.up_handle_max_c
        if mature and rested and cool:
            return move(i_up, "stepping up")
    return Action("none"), st, f"holding {cur:g}A ({tag})"
