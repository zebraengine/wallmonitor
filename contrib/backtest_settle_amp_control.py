#!/usr/bin/env python3
"""Closed-loop backtest of an amp controller: what would the handle have
done, session by session, had a different controller been setting the
current?

backtest_derate_amp_control.py replays recorded temperatures, so it can say
when a controller *would have acted* but not what would have happened next
— the recorded handle followed the amps the deployed controller chose. This
script closes the loop:

1. **Replay until divergence.** The session's real samples are fed to the
   real ``thermal.predict()`` for as long as the simulated controller's
   command matches what the car really drew. The first minutes — the
   S-shaped start and the noise that fool a young trajectory fit — are the
   recorded ones, not a model's idea of them.
2. **Plant after.** From the first tick the commands differ, a per-session
   plant takes over: two time constants (handle, and the slower cable soak)
   plus a short sensor lag, fitted to that session's own recorded current
   and handle temperature. It has been run alongside the real currents up to
   that point, so its state is warm, and the real-minus-plant offset at the
   handover decays over the fast time constant.
3. **Model as of the session's start.** ``fit_history`` is frozen just
   before the session began, as in backtest_derate_amp_control.py, so a
   session never leaks into its own forecast.

A plant fitted to the day it is judged on flatters every controller, so
each session is also run against **hotter plants** (--stress): the ambient
1 and 2 C above the fitted one, and the cable soaking 1 and 2 C more at
full rate. The perturbation rides on the replayed samples too.

Scored per controller: charger trips (must be zero), peak handle
temperature, amp changes, amp-hours delivered in the session's real
charging time. Sessions whose plant fits poorly are counted and left out
of the totals rather than trusted.

Read-only: never writes to --db. Point it at a copy.

Example:
    ./backtest_settle_amp_control.py --db ../wallmonitor-copy.db --stress
"""

from __future__ import annotations

import argparse
import datetime
import math
import sqlite3
import sys
from dataclasses import dataclass, field, replace
from multiprocessing import Pool

import derate_amp_control
import settle_amp_control

from wallmonitor import thermal
from wallmonitor.db import Database

SAMPLE_S = 2.0  # the poller's cadence
TICK_S = 30.0  # the daemon's
FULL_A = 48.7  # what the car draws at an uncapped 48 A
CMD_OFFSET_A = 0.6  # a 45 A cap draws ~45.6 A
DERATE_A = 24.0  # the charger's 50% fold-back, held for the rest of the session
POOR_FIT_RMSE_C = 0.6
STRESS_CASES = (("ambient", 1.0), ("ambient", 2.0), ("soak", 1.0), ("soak", 2.0))


# ---------------------------------------------------------------------------
# plant


@dataclass
class Plant:
    """T = a + y1 + y2, where x1 and x2 relax toward g*r(I) and h*r(I) with
    time constants tau1 and tau2 (minutes), r(I) = (I/48)^n, and y trails x
    by `lag` minutes (the reading trails the heat)."""

    a: float
    g: float
    h: float
    tau1: float
    tau2: float
    n: float
    lag: float
    rmse: float
    x1: float = 0.0
    x2: float = 0.0
    y1: float = 0.0
    y2: float = 0.0

    def r(self, amps: float) -> float:
        return (max(amps, 0.0) / thermal.REF_CURRENT_A) ** self.n

    def temp(self) -> float:
        return self.a + (self.y1 + self.y2 if self.lag else self.x1 + self.x2)

    def step(self, amps: float, dt_s: float) -> float:
        dt = dt_s / 60.0
        drive = self.r(amps)
        self.x1 += (self.g * drive - self.x1) * (1 - math.exp(-dt / self.tau1))
        if self.h:
            self.x2 += (self.h * drive - self.x2) * (1 - math.exp(-dt / self.tau2))
        if self.lag:
            blend = 1 - math.exp(-dt / self.lag)
            self.y1 += (self.x1 - self.y1) * blend
            self.y2 += (self.x2 - self.y2) * blend
        return self.temp()

    def steady(self, amps: float) -> float:
        return self.a + (self.g + self.h) * self.r(amps)


def charging_run(db_path: str, session_id: int, max_gap_s: float = 60.0) -> list[tuple[float, float, float]]:
    """(ts, amps, handle_c) over the session's first unbroken charging run."""
    con = sqlite3.connect(db_path)
    rows = con.execute(
        "SELECT ts, vehicle_current_a, handle_temp_c FROM vitals_samples "
        "WHERE session_id = ? AND contactor_closed = 1 AND vehicle_current_a >= 6 "
        "AND handle_temp_c IS NOT NULL AND handle_temp_c < 255 ORDER BY ts",
        (session_id,),
    ).fetchall()
    con.close()
    run: list[tuple[float, float, float]] = []
    for row in rows:
        if run and row[0] - run[-1][0] > max_gap_s:
            break
        run.append(row)
    return run


def _unit_responses(run, tau1: float, tau2: float | None, n: float, lag: float) -> list[list[float]]:
    """The design matrix rows [1, fast response, slow response] to the run's
    own current, for unit gains — the plant is linear in (a, g, h)."""
    x1 = x2 = y1 = y2 = 0.0
    prev = run[0][0]
    rows = []
    for ts, amps, _temp in run:
        dt = (ts - prev) / 60.0
        prev = ts
        drive = (amps / thermal.REF_CURRENT_A) ** n
        x1 += (drive - x1) * (1 - math.exp(-dt / tau1))
        if tau2:
            x2 += (drive - x2) * (1 - math.exp(-dt / tau2))
        if lag:
            blend = 1 - math.exp(-dt / lag) if dt > 0 else 0.0
            y1 += (x1 - y1) * blend
            y2 += (x2 - y2) * blend
        fast, slow = (y1, y2) if lag else (x1, x2)
        rows.append([1.0, fast, slow] if tau2 else [1.0, fast])
    return rows


def fit_plant(run, n: float, two_tau: bool = True) -> Plant | None:
    """Grid over the time constants and lag, OLS for the gains inside."""
    temps = [temp for _, _, temp in run]
    best: tuple | None = None
    for tau1 in [x / 2 for x in range(6, 41)]:  # 3..20 min
        for tau2 in ((12, 15, 20, 25, 30, 40, 50, 60, 90) if two_tau else (None,)):
            if tau2 is not None and tau2 < 1.5 * tau1:
                continue
            for lag in (0.0, 0.25, 0.5, 1.0):
                solved = thermal._ols(temps, _unit_responses(run, tau1, tau2, n, lag))
                if solved is None:
                    continue
                coef, _se, rmse, _dof = solved
                if any(gain < 0 for gain in coef[1:]):
                    continue
                if best is None or rmse < best[0]:
                    best = (rmse, tau1, tau2, lag, coef)
    if best is None:
        return None
    rmse, tau1, tau2, lag, coef = best
    return Plant(a=coef[0], g=coef[1], h=coef[2] if tau2 else 0.0, tau1=tau1, tau2=tau2 or 1.0, n=n, lag=lag, rmse=rmse)


def fit_session_plant(run, n: float) -> tuple[Plant | None, str]:
    """Two time constants when the session's data can tell them apart;
    one when it cannot (a single current held throughout leaves the slow
    gain free to run away); None when neither fits."""
    plant = fit_plant(run, n, two_tau=True)
    if plant is not None and plant.rmse <= POOR_FIT_RMSE_C and plant.steady(FULL_A) - plant.a <= 60.0:
        return plant, "two-tau"
    plant = fit_plant(run, n, two_tau=False)
    if plant is not None and plant.rmse <= POOR_FIT_RMSE_C and plant.steady(FULL_A) - plant.a <= 60.0:
        return plant, "one-tau"
    return None, "poor fit"


def perturbed(plant: Plant, kind: str, delta_c: float) -> Plant:
    if kind == "ambient":  # the day runs hotter than the fit (and the sensor) says
        return replace(plant, a=plant.a + delta_c)
    if kind == "soak":  # the cable soaks delta_c more at full rate
        return replace(plant, h=plant.h + delta_c / plant.r(FULL_A), tau2=plant.tau2 if plant.h else 20.0)
    return replace(plant)


# ---------------------------------------------------------------------------
# closed loop


class SimDB(Database):
    """The real DB, except vitals from sim_start on are the simulated ones."""

    def __init__(self, path: str, sim_start: float):
        super().__init__(path)
        self.sim_start = sim_start
        self.synth: list[dict] = []

    def vitals_range(self, t_from, t_to, max_points=1500):
        real = []
        if t_from < self.sim_start:
            real = [
                row
                for row in super().vitals_range(t_from, min(t_to, self.sim_start - 0.001), max_points)
                if row["ts"] < self.sim_start
            ]
        return real + [row for row in self.synth if t_from <= row["ts"] <= t_to]


def drawn(cmd: float | None) -> float:
    return FULL_A if cmd is None else min(FULL_A, cmd + CMD_OFFSET_A)


def settle_forecast(result: dict, db: SimDB, now: float, params: thermal.ThermalParams, session_start: float) -> dict:
    """The forecast fields settle_amp_control wants and predict() does not
    serve yet: the prior ambient, the trajectory window's current, and the
    plateau projected over a tight current band (3% rather than 10%), so a
    2-3 A step starts a fresh window instead of mixing two currents."""
    forecast = result.get("forecast")
    if result.get("state") != "charging" or not forecast:
        return result
    idle_model = thermal.load_idle_offset(db)
    measured = thermal._latest_measured_ambient(db, now)
    if measured is not None:
        forecast["prior_ambient_c"], forecast["prior_ambient_sd_c"] = measured[0], 0.3
    else:
        forecast["prior_ambient_c"] = thermal._ambient_before(db, session_start, idle_model)
        forecast["prior_ambient_sd_c"] = idle_model.ambient_se_c
    recent = [row for row in db.vitals_range(now - 900, now, 2000) if row.get("handle_temp_c") is not None]
    last = recent[-1]
    current = last["vehicle_current_a"]
    band = max(1.0, 0.03 * current)
    window = []
    for sample in reversed(recent):
        if not sample.get("contactor_closed") or last["ts"] - sample["ts"] > 360:
            break
        if abs(sample["vehicle_current_a"] - current) > band:
            break
        window.append(sample)
    window.reverse()
    forecast["window_current_a"] = sorted(s["vehicle_current_a"] for s in window)[len(window) // 2]
    points = [(s["ts"], s["handle_temp_c"]) for s in window]
    if len(points) >= thermal.TRAJECTORY_MIN_SAMPLES and points[-1][0] - points[0][0] >= thermal.TRAJECTORY_MIN_SPAN_S:
        t_inf, se = thermal._project_t_inf(points, params.tau_min)
        forecast["basis"] = "trajectory"
        forecast["steady_state_c"] = round(t_inf, 1)
        forecast["steady_state_se_c"] = round(max(se, 0.1), 2) if se is not None else None
    elif forecast.get("basis") == "trajectory":
        forecast["basis"] = "model"  # the tight band leaves too short a window for a slope
    return result


@dataclass
class Controller:
    name: str
    decide: object
    new_state: object
    cfg: object
    settle_fields: bool


def controllers(soak_margins: list[float], unguarded: bool = False) -> list[Controller]:
    out = [
        Controller(
            "deployed",
            derate_amp_control.decide,
            lambda: derate_amp_control.State(last_session_state="idle"),
            derate_amp_control.Config(forecast_confidence_k=4.0, probe_amps=()),
            False,
        )
    ]
    for margin in soak_margins:
        out.append(
            Controller(
                f"settle m{margin:g}",
                settle_amp_control.decide,
                lambda: settle_amp_control.State(last_session_state="idle"),
                settle_amp_control.Config(soak_margin_c=margin),
                True,
            )
        )
    if unguarded:
        # As first reviewed: no young-stretch guard, "rising" on one tick.
        out.append(
            Controller(
                "settle raw",
                settle_amp_control.decide,
                lambda: settle_amp_control.State(last_session_state="idle"),
                settle_amp_control.Config(young_guard_tau=0.0, rising_window_s=0.0, rising_min_c=0.05),
                True,
            )
        )
    return out


@dataclass
class Result:
    trip_min: float | None = None
    peak_c: float = 0.0
    amp_hours: float = 0.0
    final_a: float = 0.0
    moves: list[tuple[float, float, str]] = field(default_factory=list)  # (minute, to amps, why)


def simulate(db_path, session_id, run, plant: Plant, base: Plant, params, ctl: Controller, session_start: float) -> Result:
    """One session under one controller. `plant` may be a perturbed copy of
    `base` (the fitted one); while replaying, the perturbation rides on the
    real samples as (plant - base)."""
    t0, t_end = run[0][0], run[-1][0]
    db = SimDB(db_path, t0)
    plant, base = replace(plant), replace(base)
    state = ctl.new_state()
    cmd: float | None = None
    replaying, derated = True, False
    offset = 0.0
    index = 0
    out = Result()
    t = prev_t = t0
    next_tick = t0 + TICK_S
    while t <= t_end:
        if replaying and index < len(run):
            ts, real_a, real_temp = run[index]
            settled = abs(run[index - 1][1] - real_a) < 1.0
            if ts - t0 > 30 and settled and abs(drawn(cmd) - real_a) > 1.5:
                replaying = False
                offset = real_temp - base.temp()
        if replaying and index < len(run):
            t, amps, temp = run[index]
            index += 1
            plant.step(amps, t - prev_t)
            base.step(amps, t - prev_t)
            temp += plant.temp() - base.temp()
        else:
            replaying = False
            amps = DERATE_A if derated else drawn(cmd)
            offset *= math.exp(-(t - prev_t) / (plant.tau1 * 60.0))
            temp = plant.step(amps, t - prev_t) + offset
        if temp >= thermal.TRIP_HANDLE_C and not derated:
            derated = True
            out.trip_min = (t - t0) / 60.0
        db.synth.append(
            {"ts": t, "session_id": session_id, "vehicle_current_a": round(amps, 1), "handle_temp_c": round(temp, 1),
             "contactor_closed": 1, "vehicle_connected": 1}
        )
        out.peak_c = max(out.peak_c, temp)
        out.amp_hours += amps * (t - prev_t) / 3600.0
        out.final_a = amps
        prev_t = t
        if t >= next_tick:
            next_tick += TICK_S
            result = thermal.predict(db, t, params)
            if ctl.settle_fields:
                settle_forecast(result, db, t, params, session_start)
            action, state, reason = ctl.decide(result, state, ctl.cfg)
            new_cmd = float(action.value) if action.kind == "cap" else None if action.kind == "restore" else cmd
            if new_cmd != cmd:
                out.moves.append(((t - t0) / 60.0, new_cmd if new_cmd is not None else 48.0, reason))
                cmd = new_cmd
        t = run[index][0] if replaying and index < len(run) else t + SAMPLE_S
    db.close()
    return out


def best_constant(plant: Plant, duration_s: float) -> int:
    """Highest whole-amp command held from the start that never trips."""
    best = 6
    for cmd in range(6, 49):
        trial = replace(plant)
        amps = drawn(None if cmd >= 48 else cmd)
        elapsed = 0.0
        while elapsed <= duration_s and trial.step(amps, SAMPLE_S) < thermal.TRIP_HANDLE_C:
            elapsed += SAMPLE_S
        if elapsed > duration_s:
            best = cmd
    return best


# ---------------------------------------------------------------------------
# driver


def run_session(job: tuple) -> dict:
    db_path, session_id, soak_margins, stress, min_run_min, unguarded = job
    db = Database(db_path)
    session = db.session(session_id)
    run = charging_run(db_path, session_id)
    info = {"id": session_id, "start": session["start_ts"], "cases": {}}
    if len(run) < 10 or run[-1][0] - run[0][0] < min_run_min * 60.0:
        db.close()
        return {**info, "skip": "short"}
    start = session["start_ts"]
    params = thermal.fit_history(db, start - 1, fits=thermal.fit_sessions(db, start - 1))
    ambient = thermal._latest_measured_ambient(db, run[0][0])
    real_trip = any(str(a["alert"]) == "40" for a in db.alerts_range(run[0][0], run[-1][0]))
    real_moves = len(db.events_range(run[0][0], run[-1][0], kinds=["amp_capped", "amp_restored"]))
    db.close()
    plant, fit_kind = fit_session_plant(run, params.current_exp)
    info.update(
        minutes=(run[-1][0] - run[0][0]) / 60.0, ambient=ambient[0] if ambient else None,
        real_peak=max(temp for *_, temp in run), real_trip=real_trip, real_moves=real_moves, fit=fit_kind,
    )
    if plant is None:
        return {**info, "skip": "poor plant fit"}
    info.update(rmse=plant.rmse, steady_full=plant.steady(FULL_A))
    cases = [("nominal", 0.0)] + (list(STRESS_CASES) if stress else [])
    for kind, delta in cases:
        hot = perturbed(plant, kind, delta)
        entry = {"best": best_constant(hot, run[-1][0] - run[0][0]), "ctl": {}}
        for ctl in controllers(soak_margins, unguarded):
            entry["ctl"][ctl.name] = simulate(db_path, session_id, run, hot, plant, params, ctl, start)
        info["cases"][f"{kind}+{delta:g}"] = entry
    return info


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", required=True, help="path to a wallmonitor.db (read-only; use a copy)")
    parser.add_argument("--sessions", help="comma-separated session ids (default: every session)")
    parser.add_argument("--soak-margin", default="1.5", help="settle soak margins to score, comma-separated")
    parser.add_argument("--stress", action="store_true", help="also run each session against hotter plants")
    parser.add_argument("--unguarded", action="store_true", help="also score settle without its review guards")
    parser.add_argument("--min-run-min", type=float, default=15.0, help="skip shorter charging runs")
    parser.add_argument("--jobs", type=int, default=8)
    parser.add_argument("-v", "--verbose", action="store_true", help="print every amp change")
    args = parser.parse_args(argv)

    margins = [float(x) for x in args.soak_margin.split(",")]
    if args.sessions:
        ids = [int(x) for x in args.sessions.split(",")]
    else:
        con = sqlite3.connect(args.db)
        ids = [row[0] for row in con.execute("SELECT id FROM sessions ORDER BY id")]
        con.close()
    jobs = [(args.db, sid, margins, args.stress, args.min_run_min, args.unguarded) for sid in ids]
    with Pool(args.jobs) as pool:
        results = pool.map(run_session, jobs)

    totals: dict[tuple[str, str, str], dict] = {}
    skipped: dict[str, int] = {}
    for info in results:
        day = datetime.datetime.fromtimestamp(info["start"]).strftime("%a %m-%d")
        if "skip" in info:
            skipped[info["skip"]] = skipped.get(info["skip"], 0) + 1
            if info["skip"] != "short":
                print(f"session {info['id']} {day}: skipped ({info['skip']})")
            continue
        ambient = f"{info['ambient']:.1f}C" if info["ambient"] is not None else "no sensor"
        print(
            f"session {info['id']} {day}: {info['minutes']:.0f} min, ambient {ambient}, real peak "
            f"{info['real_peak']:.1f}C, real amp changes {info['real_moves']}"
            f"{', REAL TRIP' if info['real_trip'] else ''}; plant {info['fit']} rmse {info['rmse']:.2f}C, "
            f"plateau at full rate {info['steady_full']:.1f}C"
        )
        for case, entry in info["cases"].items():
            print(f"  {case}: best constant {entry['best']} A")
            for name, res in entry["ctl"].items():
                trip = f"TRIP at {res.trip_min:.0f}min" if res.trip_min is not None else "no trip"
                path = " ".join(f"{minute:.0f}m:{amps:g}" for minute, amps, _ in res.moves)
                print(
                    f"    {name:<12} {trip}, peak {res.peak_c:.1f}C, {len(res.moves)} changes, "
                    f"{res.amp_hours:.1f} Ah, ends {res.final_a:.1f}A  [{path}]"
                )
                if args.verbose:
                    for minute, amps, why in res.moves:
                        print(f"        {minute:5.1f}m -> {amps:g}A  {why}")
                # Only a session whose full rate is not sustainable can tell
                # controllers apart; the rest check that nothing caps needlessly.
                limited = info["cases"]["nominal+0"]["best"] < 48
                for group in ("all", "limited" if limited else "full-rate ok"):
                    agg = totals.setdefault(
                        (group, case, name), {"n": 0, "trips": 0, "moves": 0, "ah": 0.0, "peak": 0.0, "moved": 0}
                    )
                    agg["n"] += 1
                    agg["trips"] += res.trip_min is not None
                    agg["moves"] += len(res.moves)
                    agg["moved"] += bool(res.moves)
                    agg["ah"] += res.amp_hours
                    agg["peak"] = max(agg["peak"], res.peak_c)

    print("\nTOTALS (sessions with a usable plant)")
    for (group, case, name), agg in sorted(totals.items(), key=lambda item: item[0][0]):
        print(
            f"  {group:<12} {case:<10} {name:<12} sessions {agg['n']}, trips {agg['trips']}, amp changes {agg['moves']} "
            f"(in {agg['moved']} sessions), {agg['ah']:.1f} Ah, worst peak {agg['peak']:.1f}C"
        )
    if skipped:
        print("skipped: " + ", ".join(f"{count} {why}" for why, count in skipped.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
