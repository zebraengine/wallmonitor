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
``trajectory`` basis (this session's own proven data, never ``model``), only
one ``--restore-step-a`` at a time rather than snapping straight back to
``--normal-amps``, and never while the handle is still within
``--restore-margin-c`` of the trip point even if the trajectory reads clear.
The same 2026-08-03 session showed why: capping straight back to 48A the
moment a trajectory read clear, twice, immediately restarted the climb both
times, converting a caught derate into three near-misses before the third
one wasn't caught in time.

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
   below the trip point, stepped up ``--restore-step-a`` at a time — see
   above;
2. the charging session ends (``state`` leaves ``charging``) — the normal,
   expected end of any cap, restored immediately since there's no more
   climb to protect against;
3. a new session starts while the on-disk state still says "capped" from a
   run that never got to close one out (crash, daemon restart, etc.) — a
   safety net so a stale cap can never silently persist into a session that
   never earned it.

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
from dataclasses import asdict, dataclass, fields, replace

TRIP_HANDLE_C = 65.0  # mirrors wallmonitor/thermal.py's TRIP_HANDLE_C (Gen 3, firmware 26.18.0)


@dataclass
class Config:
    normal_amps: float = 48.0
    lead_time_min: float = 20.0
    confirm_ticks: int = 3
    min_cap_delta_a: float = 1.0
    restore_step_a: float = 2.0
    restore_margin_c: float = 3.0


@dataclass
class State:
    capped: bool = False
    cap_value: float | None = None
    trip_streak: int = 0
    clear_streak: int = 0
    last_session_state: str | None = None


@dataclass
class Action:
    kind: str  # "none" | "cap" | "restore"
    value: float | None = None


def decide(thermal: dict, state: State, cfg: Config) -> tuple[Action, State, str]:
    """Pure decision logic: what to do, the state to persist, and why."""
    session_state = thermal.get("state")
    forecast = thermal.get("forecast") or {}
    basis = forecast.get("basis")
    will_trip = forecast.get("will_trip")
    mtt = forecast.get("minutes_to_trip")
    suggested = forecast.get("suggested_max_a")
    handle_c = thermal.get("handle_c")

    session_started = session_state == "charging" and state.last_session_state != "charging"
    new_state = replace(state, last_session_state=session_state)

    if session_state != "charging":
        new_state = replace(new_state, trip_streak=0, clear_streak=0)
        if state.capped:
            new_state = replace(new_state, capped=False, cap_value=None)
            return Action("restore", cfg.normal_amps), new_state, "session ended: restoring normal rate"
        return Action("none"), new_state, "idle: nothing to do"

    if session_started and state.capped:
        # A previous run set "capped" but never saw this session close out
        # (crash, daemon restart, ...). Never let that carry into a new
        # session on trust alone.
        new_state = replace(new_state, capped=False, cap_value=None, trip_streak=0, clear_streak=0)
        return (
            Action("restore", cfg.normal_amps),
            new_state,
            ("new session started with a stale cap from a previous run: restoring first"),
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
            final_state = replace(next_state, capped=True, cap_value=suggested_a, trip_streak=0)
            return (
                Action("cap", suggested_a),
                final_state,
                (f"basis={basis} predicts a trip in {mtt:.1f}min: capping to {suggested_a:g}A"),
            )
        return Action("none"), replace(next_state, trip_streak=0), "already capped near the suggested value"

    # Restore path: deliberately narrower than the cap path. Only
    # `trajectory` basis (never `model`), only a step at a time, and only
    # with real thermal margin — see the module docstring for the incident
    # that justifies every one of these guards.
    if basis == "trajectory" and will_trip is False:
        streak = state.clear_streak + 1
        next_state = replace(new_state, clear_streak=streak, trip_streak=0)
        if streak >= cfg.confirm_ticks and state.capped and state.cap_value is not None:
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
            next_value = min(cfg.normal_amps, state.cap_value + cfg.restore_step_a)
            if next_value >= cfg.normal_amps:
                final_state = replace(next_state, capped=False, cap_value=None, clear_streak=0)
                return (
                    Action("restore", cfg.normal_amps),
                    final_state,
                    (f"trajectory clear, {margin_c:.1f}C of margin: fully restoring to {cfg.normal_amps:g}A"),
                )
            final_state = replace(next_state, capped=True, cap_value=next_value, clear_streak=0)
            return (
                Action("cap", next_value),
                final_state,
                (
                    f"trajectory clear, {margin_c:.1f}C of margin: stepping up to {next_value:g}A "
                    f"(still under {cfg.normal_amps:g}A)"
                ),
            )
        return (
            Action("none"),
            next_state,
            (f"trajectory clear ({streak}/{cfg.confirm_ticks} confirming polls, capped={state.capped})"),
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


# ---------------------------------------------------------------------------
# wallmonitor / tesla-ble access


def fetch_thermal(base_url: str) -> dict:
    with urllib.request.urlopen(f"{base_url}/api/thermal", timeout=10) as resp:
        return json.load(resp)


def set_charging_amps(tesla_ble_url: str, amps: float) -> None:
    req = urllib.request.Request(
        f"{tesla_ble_url}/number/charging_amps/set?value={amps:g}",
        data=b"",  # forces Content-Length: 0; the ESPHome web_server 411s without it
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()


# ---------------------------------------------------------------------------


def load_state(path: str) -> State:
    try:
        with open(path) as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return State()
    known = {f.name for f in fields(State)}
    return State(**{k: v for k, v in raw.items() if k in known})


def save_state(path: str, state: State) -> None:
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
        help="raise the cap by at most this much per confirmed-clear cycle, "
        "instead of snapping straight back to --normal-amps (default %(default)s)",
    )
    parser.add_argument(
        "--restore-margin-c",
        type=float,
        default=3.0,
        help="never step up while the handle is within this many degrees C "
        "of the trip point, even if the trajectory reads clear (default %(default)s)",
    )
    parser.add_argument(
        "--state-file",
        default="/tmp/derate_amp_control.state.json",
        help="remembers cap state and debounce streaks between runs",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the decision without changing the charger")
    args = parser.parse_args(argv)

    cfg = Config(
        normal_amps=args.normal_amps,
        lead_time_min=args.lead_time_min,
        confirm_ticks=args.confirm_ticks,
        min_cap_delta_a=args.min_cap_delta_a,
        restore_step_a=args.restore_step_a,
        restore_margin_c=args.restore_margin_c,
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

    try:
        set_charging_amps(args.tesla_ble, action.value)
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"error: failed to {verb} charging_amps to {action.value:g}A ({exc})", file=sys.stderr)
        return 1

    save_state(args.state_file, new_state)
    print(f"{verb} applied: {action.value:g}A ({reason})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
