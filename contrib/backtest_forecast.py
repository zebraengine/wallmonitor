#!/usr/bin/env python3
"""Score the thermal forecast against what the handle actually did, over
every recorded session.

Three model changes in one evening were each checked by replaying one or
two hand-picked moments, and each found its counter-example within the
hour. This replays the whole history and scores every way the forecast can
be made against the plateau the handle actually reached, per scenario, so
a change to the model is judged on all of it at once.

Two questions, scored separately:

1. **In-run forecast.** While the current holds steady, how far off is the
   projected plateau — the live forecast — as a function of how long it has
   held? Trajectory basis (the run's own samples so far, the install's tau)
   against model basis (an ambient plus the current law).

2. **Cross-current prediction.** At every current change, and at every
   session start, what would the plateau at the *new* current have been
   predicted as from each ambient on offer — the LAN sensor, the ambient the
   previous run's trajectory implies, the warmer of the two, the idle handle
   before the session — under each current law: the I^2 prior, the
   install's fitted exponent, and the fitted exponent with the ambient
   term, each fitted with the scored session left out. This is the number
   a restore, or the end of a calibration probe, is decided on.

Ground truth is the plateau observed in a run that held its current for at
least --observe-tau time constants: an exponential fitted to the whole run
with tau free, the same fit the model's own parameters come from. Shorter
runs are still inputs (they imply an ambient) but never truth.

Errors are predicted minus actual. Negative is optimistic — the handle ran
hotter than promised — which is the direction that trips the charger.

Read-only. Point it at a copy of wallmonitor.db, or at the live file: SQLite
WAL readers do not block the writer.

Example:
    uv run python contrib/backtest_forecast.py --db ../wallmonitor.db
    uv run python contrib/backtest_forecast.py --db ../wallmonitor.db --json out.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from statistics import median

from wallmonitor import thermal
from wallmonitor.db import Database

MIN_RUN_S = 120.0
TICK_S = 30.0
FIRST_TICK_S = 120.0
LAST_TICK_S = 1200.0
OPTIMISTIC_C = 2.0  # a plateau under-read by this much is the dangerous kind of wrong
PROBE_CURRENT_FRAC = 0.75
PROBE_MIN_S = 1800.0
HOT_AMBIENT_C = 29.0
WARM_START_C = 3.0  # handle this far above its idle level at a session's first run
BUCKETS_MIN = ((2, 5), (5, 10), (10, 20))


@dataclass
class Run:
    session_id: int
    index: int
    start_ts: float
    end_ts: float
    current_a: float
    samples: list[tuple[float, float]] = field(repr=False)
    handle_start_c: float = 0.0
    plateau_c: float | None = None  # truth: see --truth
    plateau_rmse_c: float | None = None
    fit_c: float | None = None  # exponential over the whole run, tau free
    fit_tau_min: float | None = None
    window_c: float | None = None  # the same fit over the first PREFIX_SPAN_MIN_S — what the model's fitter sees
    last_c: float | None = None  # median handle over the run's last 3 min — model-free
    max_c: float | None = None
    sensor_ambient_c: float | None = None  # LAN/car sensor at the run's start
    idle_ambient_c: float | None = None  # from the idle handle before the session (first run)
    implied_ambient_c: float | None = None  # this run's own trajectory, through the current law
    kind: str = ""  # cold_start | warm_start | step_down | step_up
    probe: bool = False
    hot: bool = False

    @property
    def span_s(self) -> float:
        return self.end_ts - self.start_ts


@dataclass
class TickScore:
    session_id: int
    kind: str
    probe: bool
    hot: bool
    minutes: float
    basis: str
    error_c: float


@dataclass
class BoundaryScore:
    session_id: int
    kind: str
    probe: bool
    hot: bool
    from_a: float | None
    to_a: float
    actual_c: float
    predictions: dict[str, float]  # "<ambient>/<law>" -> predicted plateau


# ---------------------------------------------------------------------------


def split_runs(rows: list[dict]) -> list[list[dict]]:
    """Steady-current runs, the way the live forecast sees them: a run ends
    when current leaves the band around its first sample, when the
    contactor opens, or after a charging gap. Ramp-up samples fall into
    runs too short to keep."""
    runs: list[list[dict]] = []
    current: list[dict] = []
    for row in rows:
        amps = row.get("vehicle_current_a") or 0.0
        if not row.get("contactor_closed") or amps < 6.0 or row.get("handle_temp_c") is None:
            if current:
                runs.append(current)
                current = []
            continue
        if current:
            ref = current[0]["vehicle_current_a"]
            if abs(amps - ref) > max(2.0, 0.1 * ref) or row["ts"] - current[-1]["ts"] > thermal.SEGMENT_SPLIT_GAP_S:
                runs.append(current)
                current = []
        current.append(row)
    if current:
        runs.append(current)
    return [run for run in runs if run[-1]["ts"] - run[0]["ts"] >= MIN_RUN_S]


def observe_plateau(run: Run, tau_min: float, observe_tau: float, truth: str) -> None:
    """Fill in what the run actually reached. `truth` picks which reading
    becomes plateau_c: "fit" — an exponential over the whole run with tau
    free (needs >= observe_tau time constants; rejects a poor fit); "last" —
    the handle's own median over the last three minutes, model-free (needs
    >= observe_tau + 2 time constants, so the lag has worked itself out)."""
    samples = run.samples
    span = run.span_s
    run.max_c = max(temp for _, temp in samples)
    tail = [temp for ts, temp in samples if samples[-1][0] - ts <= 180.0]
    run.last_c = median(tail) if tail else None
    fit = thermal._fit_exponential(samples)
    if fit is not None:
        run.fit_tau_min, run.fit_c, run.plateau_rmse_c = fit[0] / 60.0, fit[1], fit[2]
    head = [(ts, temp) for ts, temp in samples if ts - samples[0][0] <= thermal.PREFIX_SPAN_MIN_S]
    if len(head) >= thermal.MIN_SEGMENT_SAMPLES:
        window_fit = thermal._fit_exponential(head)
        run.window_c = window_fit[1] if window_fit is not None else None
    if truth == "fit":
        if span >= observe_tau * tau_min * 60.0 and fit is not None and fit[2] <= thermal.MAX_FIT_RMSE_C:
            run.plateau_c = fit[1]
    elif span >= (observe_tau + 2.0) * tau_min * 60.0 and run.last_c is not None:
        run.plateau_c = run.last_c


LAWS = {
    # name -> (exponent pinned?, ambient_coef pinned?) — None means fitted
    "I2": dict(exponent=thermal.DEFAULT_CURRENT_EXP, ambient_coef=thermal.DEFAULT_AMBIENT_COEF),
    "n": dict(ambient_coef=thermal.DEFAULT_AMBIENT_COEF),
    "n+k": dict(),
}


def params_without(fits: list[dict], sid: int, **law) -> thermal.ThermalParams:
    """Model parameters from every fit but the scored session's own, under a
    current law with the given terms pinned (see LAWS)."""
    others = [dict(fit) for fit in fits if fit["session_id"] != sid]
    return thermal.params_from_fits(others, **law)


def build_runs(db: Database, sess: dict, params: thermal.ThermalParams, observe_tau: float,
               idle_model: thermal.IdleOffset, truth: str = "fit") -> list[Run]:
    rows = db.vitals_range(sess["start_ts"] - 1, sess["end_ts"] + 1, 500_000)
    runs: list[Run] = []
    for index, raw in enumerate(split_runs(rows)):
        samples = [(row["ts"], row["handle_temp_c"]) for row in raw]
        run = Run(
            session_id=sess["id"], index=index, start_ts=samples[0][0], end_ts=samples[-1][0],
            current_a=median(row["vehicle_current_a"] for row in raw), samples=samples,
            handle_start_c=samples[0][1],
        )
        observe_plateau(run, params.tau_min, observe_tau, truth)
        measured = thermal._measured_ambient(db, run.start_ts - thermal.MEASURED_AMBIENT_WINDOW_S, run.start_ts + 60)
        run.sensor_ambient_c = measured[0] if measured is not None else None
        if index == 0:
            run.idle_ambient_c = thermal._ambient_before(db, sess["start_ts"], idle_model)
        if len(samples) >= thermal.TRAJECTORY_MIN_SAMPLES:
            t_inf, _se = thermal._project_t_inf(samples, params.tau_min)
            run.implied_ambient_c = params.ambient_from_plateau(t_inf, run.current_a)
        run.probe = run.current_a <= PROBE_CURRENT_FRAC * thermal.REF_CURRENT_A and run.span_s >= PROBE_MIN_S
        ambient_for_hot = run.sensor_ambient_c if run.sensor_ambient_c is not None else run.idle_ambient_c
        run.hot = ambient_for_hot is not None and ambient_for_hot >= HOT_AMBIENT_C
        if index == 0:
            baseline = ambient_for_hot
            idle_level = thermal.idle_handle_c(baseline, idle_model) if baseline is not None else None
            run.kind = (
                "warm_start" if idle_level is not None and run.handle_start_c > idle_level + WARM_START_C
                else "cold_start"
            )
        else:
            run.kind = "step_down" if run.current_a < runs[-1].current_a else "step_up"
        runs.append(run)
    return runs


def score_ticks(run: Run, params: thermal.ThermalParams) -> list[TickScore]:
    """The live forecast, tick by tick, against the plateau this run reached."""
    if run.plateau_c is None:
        return []
    scores: list[TickScore] = []
    t0 = run.start_ts
    model_sensor = (
        params.plateau_at(run.current_a, run.sensor_ambient_c) if run.sensor_ambient_c is not None else None
    )
    tick = FIRST_TICK_S
    while tick <= min(LAST_TICK_S, run.span_s):
        window = [(ts, temp) for ts, temp in run.samples if ts - t0 <= tick]
        minutes = tick / 60.0
        if len(window) >= thermal.TRAJECTORY_MIN_SAMPLES:
            t_inf, _se = thermal._project_t_inf(window, params.tau_min)
            scores.append(TickScore(run.session_id, run.kind, run.probe, run.hot, minutes, "trajectory", t_inf - run.plateau_c))
        if model_sensor is not None:
            scores.append(TickScore(run.session_id, run.kind, run.probe, run.hot, minutes, "model/sensor", model_sensor - run.plateau_c))
        tick += TICK_S
    return scores


def score_boundary(run: Run, prev: Run | None, laws: dict[str, thermal.ThermalParams]) -> BoundaryScore | None:
    """What each ambient, under each current law, would have predicted for
    the plateau at this run's current, from what was known when it started."""
    if run.plateau_c is None:
        return None
    ambients: dict[str, float] = {}
    if run.sensor_ambient_c is not None:
        ambients["sensor"] = run.sensor_ambient_c
    if prev is None:
        if run.idle_ambient_c is not None:
            ambients["idle"] = run.idle_ambient_c
    else:
        if prev.implied_ambient_c is not None:
            ambients["implied"] = prev.implied_ambient_c
            if run.sensor_ambient_c is not None:
                ambients["warmer"] = max(run.sensor_ambient_c, prev.implied_ambient_c)
    if not ambients:
        return None
    predictions = {
        f"{name}/{law}": params.plateau_at(run.current_a, ambient)
        for name, ambient in ambients.items()
        for law, params in laws.items()
    }
    return BoundaryScore(
        run.session_id, run.kind, run.probe, run.hot,
        prev.current_a if prev is not None else None, run.current_a, run.plateau_c, predictions,
    )


# ---------------------------------------------------------------------------


def _pct(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(p * (len(ordered) - 1))))]


def _stats(errors: list[float]) -> str:
    if not errors:
        return f"{'-':>5} {'-':>6} {'-':>6} {'-':>6} {'-':>5}"
    absolute = [abs(e) for e in errors]
    optimistic = sum(1 for e in errors if e < -OPTIMISTIC_C) / len(errors)
    return f"{len(errors):>5} {median(errors):>+6.2f} {median(absolute):>6.2f} {_pct(absolute, 0.9):>6.2f} {optimistic:>5.0%}"


STATS_HEADER = f"{'n':>5} {'bias':>6} {'|med|':>6} {'|p90|':>6} {'opt':>5}"


def report(runs: list[Run], ticks: list[TickScore], boundaries: list[BoundaryScore],
           params: thermal.ThermalParams, observe_tau: float) -> None:
    observed = [run for run in runs if run.plateau_c is not None]
    print(
        f"model: tau {params.tau_min:.2f} min, rise {params.rise_ref_c:.1f} C at {thermal.REF_CURRENT_A:g} A "
        f"and {thermal.AMBIENT_REF_C:g} C, n = {params.current_exp:.2f}, k = {params.ambient_coef:+.3f} C/C "
        f"({params.current_exp_fits} fits)"
    )
    print(
        f"runs: {len(runs)} steady-current runs in {len({run.session_id for run in runs})} sessions; "
        f"{len(observed)} held >= {observe_tau:g} tau and are scored as truth"
    )
    kinds = sorted({run.kind for run in observed})
    print("  by kind:", ", ".join(f"{kind} {sum(1 for r in observed if r.kind == kind)}" for kind in kinds),
          f"| probe {sum(1 for r in observed if r.probe)}, hot {sum(1 for r in observed if r.hot)}")
    print()
    print("== In-run forecast: projected plateau vs observed, by minutes at steady current")
    print("   (bias = median signed error, predicted - actual; opt = share optimistic by > "
          f"{OPTIMISTIC_C:g} C)")
    bases = sorted({tick.basis for tick in ticks})
    print(f"{'basis':<14}" + "".join(f"  {lo:>2}-{hi:<2} min {STATS_HEADER}" for lo, hi in BUCKETS_MIN))
    for basis in bases:
        row = f"{basis:<14}"
        for lo, hi in BUCKETS_MIN:
            errors = [t.error_c for t in ticks if t.basis == basis and lo <= t.minutes < hi]
            row += f"  {'':>10}{_stats(errors)}"
        print(row)
    print()
    print("  trajectory basis by scenario, 5-10 min:")
    for kind in kinds:
        errors = [t.error_c for t in ticks if t.basis == "trajectory" and t.kind == kind and 5 <= t.minutes < 10]
        print(f"    {kind:<12} {_stats(errors)}")
    for flag in ("probe", "hot"):
        errors = [t.error_c for t in ticks if t.basis == "trajectory" and getattr(t, flag) and 5 <= t.minutes < 10]
        print(f"    {flag:<12} {_stats(errors)}")
    print()
    print("== Cross-current prediction: plateau at the new current, from what was known at the change")
    print("   ambient: sensor = LAN/car sensor at the change; implied = previous run's trajectory through the")
    print("   current law; warmer = max(sensor, implied); idle = handle before the session (session start only)")
    print("   law: I2 = the I^2 prior; n = fitted exponent; n+k = fitted exponent and ambient term;")
    print("   each fitted with the scored session left out")
    methods = sorted({m for b in boundaries for m in b.predictions})
    groups: list[tuple[str, list[BoundaryScore]]] = [(kind, [b for b in boundaries if b.kind == kind]) for kind in kinds]
    groups += [("probe", [b for b in boundaries if b.probe]), ("hot", [b for b in boundaries if b.hot]),
               ("all", boundaries)]
    for label, group in groups:
        if not group:
            continue
        print(f"  {label} ({len(group)} changes)          {STATS_HEADER}")
        for method in methods:
            errors = [b.predictions[method] - b.actual_c for b in group if method in b.predictions]
            if errors:
                print(f"    {method:<14} {_stats(errors)}")
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", required=True, help="path to a wallmonitor.db (read-only)")
    parser.add_argument("--lookback-days", type=float, default=180.0)
    parser.add_argument("--observe-tau", type=float, default=3.0,
                        help="a run must hold this many time constants for its plateau to count as truth (default %(default)s)")
    parser.add_argument("--truth", choices=("fit", "last"), default="fit",
                        help="what counts as the plateau a run reached: an exponential fitted over the whole run "
                             "(fit), or the handle's own last three minutes, needing two more time constants (last)")
    parser.add_argument("--json", help="also write every run, tick score and boundary score here")
    parser.add_argument("--verbose", action="store_true", help="list every run")
    args = parser.parse_args(argv)

    db = Database(args.db)
    now = time.time()
    fits = thermal.fit_sessions(db, now, lookback_days=args.lookback_days)
    params = thermal.fit_history(db, now, fits=fits)
    idle_model = thermal.load_idle_offset(db)
    sessions = [
        sess for sess in db.sessions_range(now - args.lookback_days * 86400, now)
        if sess.get("end_ts") and (sess.get("charging_s") or 0) >= thermal.MIN_SEGMENT_S
    ]
    sessions.sort(key=lambda sess: sess["start_ts"])

    runs: list[Run] = []
    ticks: list[TickScore] = []
    boundaries: list[BoundaryScore] = []
    for sess in sessions:
        laws = {name: params_without(fits, sess["id"], **pins) for name, pins in LAWS.items()}
        session_runs = build_runs(db, sess, laws["n+k"], args.observe_tau, idle_model, args.truth)
        for index, run in enumerate(session_runs):
            ticks.extend(score_ticks(run, laws["n+k"]))
            boundary = score_boundary(run, session_runs[index - 1] if index else None, laws)
            if boundary is not None:
                boundaries.append(boundary)
            if args.verbose:
                fmt = lambda value: f"{value:5.1f}" if value is not None else "    -"  # noqa: E731
                print(
                    f"s{run.session_id:<4} run{run.index} {run.kind:<10} {run.current_a:5.1f}A "
                    f"{run.span_s / 60:5.0f}min handle {run.handle_start_c:5.1f} -> last {fmt(run.last_c)} "
                    f"max {fmt(run.max_c)} | fit {fmt(run.fit_c)} (tau {fmt(run.fit_tau_min)}) "
                    f"30min-fit {fmt(run.window_c)} | truth {fmt(run.plateau_c)} | "
                    f"sensor {fmt(run.sensor_ambient_c)} implied {fmt(run.implied_ambient_c)}"
                    f"{' probe' if run.probe else ''}{' hot' if run.hot else ''}"
                )
        runs.extend(session_runs)

    report(runs, ticks, boundaries, params, args.observe_tau)
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(
                {
                    "model": params.as_dict(),
                    "runs": [{k: v for k, v in asdict(run).items() if k != "samples"} for run in runs],
                    "ticks": [asdict(tick) for tick in ticks],
                    "boundaries": [asdict(b) for b in boundaries],
                },
                fh,
                indent=1,
            )
        print(f"wrote {args.json}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
