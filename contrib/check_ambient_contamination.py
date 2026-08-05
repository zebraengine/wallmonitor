#!/usr/bin/env python3
"""Check whether a fixed ambient sensor is picking up heat from the charger.

Once a stationary LAN sensor reports, its readings take the top tier and the
thermal model trusts them completely — which promotes sensor *placement* from
an installation detail to a correctness concern. The model's premise is
``handle_temp = ambient + rise``. A sensor close enough to the connector to be
warmed by it reports an ambient that climbs whenever charging heats the
surrounding air, cancelling part of the very rise the model exists to measure.
That under-predicts derates *and* makes a degrading install look healthy, so
the failure mode hides a problem rather than inventing one.

**A positive ambient drift during a charge is not, on its own, evidence of
contamination.** An uninsulated garage genuinely warms over an afternoon
charge, and that is real ambient change the model should track. Worse, a
naive correlation gives a false positive: on a hot afternoon ambient rises
*and* the handle rises — the handle partly *because* ambient rose. Shared
diurnal trend produces correlation with no coupling at all.

Three discriminators separate the mechanisms, and this script computes all
of them:

1. **Detrended correlation.** Fit a line to ambient across the segment and
   correlate the *residual* against handle temperature. Slow diurnal drift is
   removed; fast minute-scale tracking survives. Contamination follows the
   handle within minutes because it is radiative/convective transfer over a
   couple of feet of air; weather does not.

2. **Snap-back.** When current stops the handle immediately starts falling.
   A contaminated sensor falls with it. A diurnal trend does not reverse
   direction on cue, so comparing the ambient slope during the segment against
   the slope just after it separates the two.

3. **Dry-heat signature.** The charger warms nearby air without adding water
   vapor, so a coupled bump lifts temperature while the dew point holds flat
   (relative humidity falls to compensate). A bump that carries its dew point
   along means new air reached the sensor — an opened door, a front — not
   radiant transfer. Sensor noise cannot fake the dry signature: at constant
   humidity the dew point inherits nearly every temperature move, so only
   genuinely falling RH holds it flat. Needs a sensor that reports humidity;
   segments without it show n/a and lean on the other two.

Deliberately does NOT reuse ``ambient_drift_c`` from ``fit_sessions()``.
That field only exists on segments clearing the model's fit-acceptance gates
(``MIN_SEGMENT_S`` = 480 s and friends) — gates that exist so a segment
teaches the *thermal model* something, which is a different question from
whether the *sensor* is sited correctly. Inheriting them would discard
perfectly usable evidence: a six-minute top-off that warms the handle 10 C is
useless for fitting tau but perfectly good for asking whether ambient moved.

Read-only; never writes to --db. Point it at a copy if the monitor may be
writing concurrently.

Example:
    ./check_ambient_contamination.py --db /path/to/a/copy/of/wallmonitor.db
"""

from __future__ import annotations

import argparse
import math
import sqlite3
import statistics
from dataclasses import dataclass

MIN_SEGMENT_S = 180.0  # far below the model's fit gate; we only need a thermal signal
MIN_RISE_C = 3.0  # the handle must actually have warmed, or there is nothing to test
MIN_CURRENT_A = 6.0  # J1772 floor: below this the vehicle is not really charging
POST_WINDOW_S = 900.0  # how long to watch ambient after current stops
AMBIENT_MAX_AGE_S = 300.0  # an ambient sample this far from a moment is not "at" it


@dataclass
class Segment:
    start_ts: float
    end_ts: float
    mean_current_a: float
    handle_start_c: float
    handle_peak_c: float
    ambient_start_c: float
    ambient_end_c: float
    detrended_r: float | None
    dewpoint_r: float | None
    slope_during_c_per_min: float
    slope_after_c_per_min: float | None
    n_ambient: int

    @property
    def duration_min(self) -> float:
        return (self.end_ts - self.start_ts) / 60.0

    @property
    def handle_rise_c(self) -> float:
        return self.handle_peak_c - self.handle_start_c

    @property
    def ambient_drift_c(self) -> float:
        return self.ambient_end_c - self.ambient_start_c


def _linear_slope(points: list[tuple[float, float]]) -> float:
    """Least-squares slope, per second. Zero when the x-spread is degenerate."""
    n = len(points)
    if n < 2:
        return 0.0
    mean_x = sum(x for x, _ in points) / n
    mean_y = sum(y for _, y in points) / n
    var = sum((x - mean_x) ** 2 for x, _ in points)
    if var < 1e-9:
        return 0.0
    cov = sum((x - mean_x) * (y - mean_y) for x, y in points)
    return cov / var


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 4:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx < 1e-9 or sy < 1e-9:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


def _dew_point_c(temp_c: float, humidity_pct: float | None) -> float | None:
    """Magnus approximation. None when humidity is absent or implausible
    (ingest range-checks temperature but stores humidity unvalidated)."""
    if humidity_pct is None or not 0.0 < humidity_pct <= 100.0:
        return None
    b, c = 17.62, 243.12
    gamma = math.log(humidity_pct / 100.0) + (b * temp_c) / (c + temp_c)
    return (c * gamma) / (b - gamma)


def _detrended_r(points: list[tuple[float, float]], seg: list[dict]) -> float | None:
    """Correlate a series' residual (after removing its linear time trend)
    against the handle temperature nearest each sample — the shared core of
    discriminators 1 and 3."""
    if len(points) < 4:
        return None
    slope = _linear_slope(points)
    mean_t = sum(t for t, _ in points) / len(points)
    mean_v = sum(v for _, v in points) / len(points)
    residuals = [v - (mean_v + slope * (t - mean_t)) for t, v in points]
    handles = [min(seg, key=lambda s: abs(s["ts"] - t))["handle_temp_c"] for t, _ in points]
    return _pearson(residuals, handles)


def find_segments(conn: sqlite3.Connection, lookback_days: float) -> list[list[dict]]:
    """Contiguous stretches where current actually flowed, coarsely split on gaps."""
    newest = conn.execute("SELECT MAX(ts) AS t FROM vitals_samples").fetchone()["t"] or 0.0
    rows = conn.execute(
        """SELECT ts, vehicle_current_a, contactor_closed,
                  CASE WHEN handle_temp_c >= 255 THEN NULL ELSE handle_temp_c END AS handle_temp_c
           FROM vitals_samples WHERE ts >= ? ORDER BY ts""",
        (newest - lookback_days * 86400,),
    ).fetchall()

    segments, current = [], []
    for row in rows:
        charging = bool(row["contactor_closed"]) and (row["vehicle_current_a"] or 0) >= MIN_CURRENT_A
        if charging and row["handle_temp_c"] is not None:
            # A long gap means a new segment even if current never read zero
            # (monitor downtime, or a sample the device declined to answer).
            if current and row["ts"] - current[-1]["ts"] > 300:
                segments.append(current)
                current = []
            current.append(dict(row))
        elif current:
            segments.append(current)
            current = []
    if current:
        segments.append(current)
    return [s for s in segments if s[-1]["ts"] - s[0]["ts"] >= MIN_SEGMENT_S]


def ambient_between(conn: sqlite3.Connection, t_from: float, t_to: float, source: str) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT ts, temp_c, humidity_pct FROM ambient_samples WHERE ts >= ? AND ts <= ? AND source = ? ORDER BY ts",
            (t_from, t_to, source),
        ).fetchall()
    ]


def analyse(conn: sqlite3.Connection, seg: list[dict], source: str) -> Segment | None:
    t0, t1 = seg[0]["ts"], seg[-1]["ts"]
    amb = ambient_between(conn, t0 - AMBIENT_MAX_AGE_S, t1 + AMBIENT_MAX_AGE_S, source)
    inside = [a for a in amb if t0 <= a["ts"] <= t1]
    if len(inside) < 4:
        return None  # not enough measured ambient covering this segment to say anything

    handles = [s["handle_temp_c"] for s in seg]
    if max(handles) - handles[0] < MIN_RISE_C:
        return None  # no thermal signal to test against

    # Detrend ambient across the segment, then correlate the residual with the
    # handle temperature interpolated to the same instants. Removing the linear
    # trend is what stops a shared diurnal rise from masquerading as coupling.
    temp_points = [(a["ts"], a["temp_c"]) for a in inside]
    slope_s = _linear_slope(temp_points)

    # Same test on the dew point. Charger heat is dry, so a coupled bump leaves
    # dew point flat; a bump of arrived air carries its dew point with it.
    dew_points = [(a["ts"], d) for a in inside if (d := _dew_point_c(a["temp_c"], a["humidity_pct"])) is not None]

    after = [a for a in amb if t1 < a["ts"] <= t1 + POST_WINDOW_S]
    slope_after = _linear_slope([(a["ts"], a["temp_c"]) for a in after]) * 60.0 if len(after) >= 3 else None

    return Segment(
        start_ts=t0,
        end_ts=t1,
        mean_current_a=statistics.fmean(s["vehicle_current_a"] for s in seg),
        handle_start_c=handles[0],
        handle_peak_c=max(handles),
        ambient_start_c=inside[0]["temp_c"],
        ambient_end_c=inside[-1]["temp_c"],
        detrended_r=_detrended_r(temp_points, seg),
        dewpoint_r=_detrended_r(dew_points, seg),
        slope_during_c_per_min=slope_s * 60.0,
        slope_after_c_per_min=slope_after,
        n_ambient=len(inside),
    )


def main(argv: list[str] | None = None) -> int:
    import datetime

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--db", required=True, help="path to a wallmonitor.db (read-only; use a copy)")
    parser.add_argument("--source", default="ecowitt", help="ambient source tag to test (default %(default)s)")
    parser.add_argument("--lookback-days", type=float, default=30.0)
    parser.add_argument(
        "--suspect-r",
        type=float,
        default=0.5,
        help="flag a segment whose detrended correlation exceeds this (default %(default)s)",
    )
    args = parser.parse_args(argv)

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    results = [r for r in (analyse(conn, s, args.source) for s in find_segments(conn, args.lookback_days)) if r]
    if not results:
        print(f"no charging segments with '{args.source}' ambient coverage and a real handle rise")
        print("(a sensor only just deployed will have nothing to show until the next substantial charge)")
        return 0

    print(f"{len(results)} testable segment(s) with '{args.source}' ambient coverage\n")
    suspect = dry = moist = 0
    for r in results:
        when = datetime.datetime.fromtimestamp(r.start_ts).strftime("%m-%d %H:%M")
        print(f"{when}  {r.duration_min:.0f} min @ {r.mean_current_a:.1f}A  ({r.n_ambient} ambient samples)")
        print(f"  handle  {r.handle_start_c:.1f} -> {r.handle_peak_c:.1f} C   (rise {r.handle_rise_c:+.1f})")
        print(f"  ambient {r.ambient_start_c:.1f} -> {r.ambient_end_c:.1f} C   (drift {r.ambient_drift_c:+.1f})")
        print(f"  ambient slope during: {r.slope_during_c_per_min:+.3f} C/min", end="")
        if r.slope_after_c_per_min is not None:
            print(f"   after stop: {r.slope_after_c_per_min:+.3f} C/min")
        else:
            print("   after stop: (no data)")
        if r.detrended_r is None:
            print("  detrended correlation vs handle: n/a (too few samples)")
        else:
            flag = "  <-- SUSPECT" if r.detrended_r > args.suspect_r else ""
            print(f"  detrended correlation vs handle: r = {r.detrended_r:+.2f}{flag}")
            if r.dewpoint_r is None:
                print("  detrended dew point vs handle:   n/a (no usable humidity)")
            elif r.detrended_r <= args.suspect_r:
                print(f"  detrended dew point vs handle:   r = {r.dewpoint_r:+.2f}")
            elif r.dewpoint_r > args.suspect_r:
                moist += 1
                print(
                    f"  detrended dew point vs handle:   r = {r.dewpoint_r:+.2f}"
                    "  (moisture tracked too: arrived air, not dry charger heat)"
                )
            else:
                dry += 1
                print(
                    f"  detrended dew point vs handle:   r = {r.dewpoint_r:+.2f}"
                    "  (dew point held flat: dry heat, corroborates coupling)"
                )
            if r.detrended_r > args.suspect_r:
                suspect += 1
        print()

    print("=" * 68)
    if suspect:
        print(f"{suspect}/{len(results)} segment(s) show minute-scale ambient tracking of the handle.")
        if dry:
            print(f"{dry} of them hold dew point flat while temperature climbs — dry heat, which")
            print("arriving humid air cannot fake. Consider relocating the sensor further from")
            print("the connector, or below the cable run rather than above it.")
        if moist:
            print(f"{moist} of them carry dew point along with the bump — new air reached the")
            print("sensor (an opened door, a front), which explains the tracking without any")
            print("coupling. Re-test on a closed-garage segment before moving anything.")
        if not dry and not moist:
            print("That is the contamination signature — consider relocating the sensor further")
            print("from the connector, or below the cable run rather than above it.")
    else:
        print(f"0/{len(results)} segment(s) show minute-scale tracking: no contamination signal.")
        print("Note the strongest test is a LONG (>30 min), HOT (handle >55 C) segment with the")
        print("garage closed. Short or cool segments pass easily and prove little — check the")
        print("per-segment numbers above before treating this as settled.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
