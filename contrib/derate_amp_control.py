#!/usr/bin/env python3
"""Cap Tesla charge current when wallmonitor's thermal model predicts a
derate, and restore it once the risk clears.

wallmonitor's /api/thermal forecasts a handle-temperature trip from three
increasingly precise bases as a charging session accumulates data:
``hypothetical`` (pre-session, pure extrapolation from historical fits),
``model`` (a little live data blended with the historical prior), and
``trajectory`` (an exponential curve fit to this session's own readings).
Live testing (2026-08-01, a full 48A session) showed the historical prior
runs hot relative to reality — ``hypothetical``/``model`` basis predicted a
trip in ~24-27min while the session's actual ``trajectory`` fit, once it
had enough points, correctly settled on a steady-state handle temp several
degrees under the trip threshold and never tripped. So this daemon acts
**only** on ``trajectory``-basis forecasts, and only once a signal has held
for several consecutive polls — a single noisy fit should not flip a real
amp change on the charger.

The cap is applied through an ESPHome ``esphome-tesla-ble`` device's
web_server REST API (``POST /number/charging_amps/set?value=<A>``), paired
with the vehicle using the least-privilege ``CHARGING_MANAGER`` role.

Restores happen three ways, in order of how eagerly they should fire:
1. the trajectory forecast itself reports ``will_trip: false`` for
   ``--confirm-ticks`` consecutive polls (the risk genuinely passed);
2. the charging session ends (``state`` leaves ``charging``) — the normal,
   expected end of any cap;
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


@dataclass
class Config:
    normal_amps: float = 48.0
    lead_time_min: float = 20.0
    confirm_ticks: int = 3
    min_cap_delta_a: float = 1.0


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

    trusted = basis == "trajectory"

    # Cap path: only reachable once mtt/suggested have passed isinstance
    # checks, so `suggested` is a real number everywhere below this point.
    if (
        trusted
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
                (f"trajectory predicts a trip in {mtt:.1f}min ({streak}/{cfg.confirm_ticks} confirming polls)"),
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
                (f"trajectory predicts a trip in {mtt:.1f}min: capping to {suggested_a:g}A"),
            )
        return Action("none"), replace(next_state, trip_streak=0), "already capped near the suggested value"

    # Clear path: trajectory basis explicitly says no trip is coming.
    if trusted and will_trip is False:
        streak = state.clear_streak + 1
        next_state = replace(new_state, clear_streak=streak, trip_streak=0)
        if streak >= cfg.confirm_ticks and state.capped:
            final_state = replace(next_state, capped=False, cap_value=None, clear_streak=0)
            return (
                Action("restore", cfg.normal_amps),
                final_state,
                (f"trajectory clear for {cfg.confirm_ticks} consecutive polls: restoring"),
            )
        return (
            Action("none"),
            next_state,
            (f"trajectory clear ({streak}/{cfg.confirm_ticks} confirming polls, capped={state.capped})"),
        )

    if not trusted:
        return Action("none"), new_state, f"basis={basis}: not a trajectory fit yet, ignoring for control"

    # Trusted trajectory basis, but neither a qualifying cap nor clear signal
    # (e.g. will_trip True but beyond lead time) — too weak to move either
    # streak; leave both exactly where they were.
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
