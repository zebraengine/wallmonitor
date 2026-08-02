#!/usr/bin/env python3
"""Replay derate_amp_control's decide() against real historical sessions to
check whether it would actually have helped.

wallmonitor's thermal.predict() and its model-fitting functions are all
parameterized by an explicit ``now``, and never look past it (verified by
reading thermal.py, not assumed) — so it's possible to reconstruct exactly
what ``/api/thermal`` would have reported at any past instant. That means a
real historical alert 40 (an *actual* derate, not a hypothetical one) can be
replayed through the same decide() the live daemon uses, to see whether the
`trajectory`-only, debounced gating (see derate_amp_control.py's module
docstring) would have caught it with enough lead time to matter.

For each real alert 40 in --db's alert history, this script:

1. finds the session that contains it;
2. fits model params using only sessions that had already **ended** before
   that session **started** — the session under test, and anything after
   it, never leaks into its own baseline (fit_sessions()/fit_history() are
   themselves "as of now" but a session already has an end_ts recorded in a
   historical DB regardless of whether it happened before or after that
   `now`, so freezing `now` at the session's own start is required to keep
   the fit honest);
3. replays predict() every --poll-interval-s seconds from session start
   through the trip (plus a short tail), feeding each result through
   decide() with persistent State, exactly as the live daemon would;
4. reports whether/when decide() would have capped, how much lead time that
   gave versus the real trip, and what wallmonitor's own (simpler, already
   deployed) "Derate predicted" alert did for the same event, for
   comparison.

Read-only: never writes to --db. Point it at a copy, not the live file, if
the daemon or wallmonitor might be writing to it concurrently.

Example:
    ./backtest_derate_amp_control.py --db ../wallmonitor.db
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

from derate_amp_control import Config, State, decide

from wallmonitor import thermal
from wallmonitor.db import Database

POLL_INTERVAL_S = 30.0


@dataclass
class Event:
    alert_id: int
    trip_ts: float
    session_id: int
    session_start: float
    session_end: float | None


def real_trip_events(db: Database, t_from: float, t_to: float) -> list[Event]:
    events = []
    for row in db.alerts_range(t_from, t_to):
        if row.get("alert") != "40":
            continue
        sessions = db.sessions_range(row["first_ts"] - 1, row["first_ts"] + 1)
        session = next((s for s in sessions if s["start_ts"] <= row["first_ts"]), sessions[0] if sessions else None)
        if session is None:
            print(f"skip alert {row['id']}: no enclosing session found", file=sys.stderr)
            continue
        events.append(Event(row["id"], row["first_ts"], session["id"], session["start_ts"], session.get("end_ts")))
    return events


def nearest_predicted_alert(db: Database, trip_ts: float) -> float | None:
    """Lead time wallmonitor's own existing 'Derate predicted' alert gave,
    for comparison — the simpler, already-deployed baseline this daemon
    builds on."""
    candidates = [
        row
        for row in db.alerts_range(trip_ts - 3600, trip_ts)
        if "Derate predicted" in (row.get("alert") or "") and row["first_ts"] <= trip_ts
    ]
    if not candidates:
        return None
    return max(row["first_ts"] for row in candidates)


def replay(db: Database, event: Event, cfg: Config, poll_interval_s: float) -> dict:
    fits = thermal.fit_sessions(db, event.session_start - 1)
    params = thermal.fit_history(db, event.session_start - 1, fits=fits)

    tail_end = (event.session_end or event.trip_ts) + 600
    t = event.session_start
    state = State()
    cap_tick: dict | None = None
    ticks = 0
    while t <= min(event.trip_ts + 600, tail_end):
        result = thermal.predict(db, t, params)
        action, state, reason = decide(result, state, cfg)
        ticks += 1
        if cap_tick is None and action.kind == "cap":
            cap_tick = {
                "ts": t,
                "value": action.value,
                "reason": reason,
                "handle_c": result.get("handle_c"),
                "lead_s": event.trip_ts - t,
            }
        t += poll_interval_s

    return {
        "event": event,
        "params": params,
        "ticks": ticks,
        "cap_tick": cap_tick,
        "predicted_alert_ts": nearest_predicted_alert(db, event.trip_ts),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", required=True, help="path to a wallmonitor.db (read-only; use a copy)")
    parser.add_argument("--lookback-days", type=float, default=180.0)
    parser.add_argument(
        "--poll-interval-s",
        type=float,
        default=POLL_INTERVAL_S,
        help="how often the live daemon would poll (default %(default)s)",
    )
    parser.add_argument("--confirm-ticks", type=int, default=3)
    parser.add_argument("--lead-time-min", type=float, default=20.0)
    args = parser.parse_args(argv)

    db = Database(args.db)
    now = db.alerts_range(0, 1e15)  # cheap way to find the newest ts without a dedicated method
    latest_ts = max((row["last_ts"] for row in now), default=0.0)
    events = real_trip_events(db, latest_ts - args.lookback_days * 86400, latest_ts)

    if not events:
        print("no real alert-40 events found in range")
        return 0

    cfg = Config(confirm_ticks=args.confirm_ticks, lead_time_min=args.lead_time_min)
    print(
        f"{len(events)} real alert-40 event(s) found; replaying with "
        f"confirm_ticks={cfg.confirm_ticks} lead_time_min={cfg.lead_time_min} "
        f"poll_interval_s={args.poll_interval_s:g}\n"
    )

    caught = 0
    for event in events:
        r = replay(db, event, cfg, args.poll_interval_s)
        params = r["params"]
        when = f"tau_min={params.tau_min:.1f} rise_ref_c={params.rise_ref_c:.1f} (fit as of session start)"
        print(f"alert #{event.alert_id}  session {event.session_id}  trip_ts={event.trip_ts:.0f}  {when}")
        if r["predicted_alert_ts"] is not None:
            lead = event.trip_ts - r["predicted_alert_ts"]
            print(f"  existing 'Derate predicted' alert: {lead / 60:.1f}min lead time")
        else:
            print("  existing 'Derate predicted' alert: none fired for this event")
        if r["cap_tick"] is None:
            print(f"  decide(): NEVER would have capped ({r['ticks']} ticks replayed) — MISS")
        else:
            c = r["cap_tick"]
            caught += 1
            print(
                f"  decide(): would cap to {c['value']:g}A at handle={c['handle_c']}C, "
                f"{c['lead_s'] / 60:.1f}min lead time — {c['reason']}"
            )
        print()

    print(
        f"summary: decide() would have caught {caught}/{len(events)} real derates "
        f"with confirm_ticks={cfg.confirm_ticks} lead_time_min={cfg.lead_time_min}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
