#!/usr/bin/env python3
"""Calibrate the idle handle offset against a stationary ambient sensor.

The thermal model's handle-proxy fallback reads garage air from the idle
handle temperature via ``ambient_from_idle_handle`` — which is only as good
as the offset model behind it. This script measures that offset directly
from recorded data and reports whether the constants in ``thermal.py``
still describe it, with enough statistics attached that the answer is
evidence rather than a plausible-looking mean.

Method, and why each gate exists:

- **Settled idle only.** Samples count only when the contactor is open, no
  current flows, and at least ``--settle-hours`` have passed since the last
  charging sample: the handle needs ~3 time constants (tau ~20 min measured
  on this install) to shed charge heat, and including the decay would bias
  the offset upward.

- **Quasi-static ambient only.** The handle lags air by ~20 min, so during
  fast ambient swings the pairing (handle now, ambient now) is wrong even
  though both sensors are right. Samples are kept only when ambient moved
  less than ``--max-drift-c`` over the prior 30 min.

- **Per-segment means, not per-sample stats.** Consecutive samples are
  massively autocorrelated (both sensors move on hour scales); treating
  them as independent would shrink the error bars by ~sqrt(n) for free.
  Contiguous idle runs are collapsed to one observation each, and all
  inference runs on segment means.

- **Day-clustered robustness.** Segments within a day share weather, so the
  ambient-slope significance is jackknifed leave-one-day-out — the most
  conservative independence unit the data offers.

Outputs the measured mean offset with CI, the day/night split, the
offset-vs-ambient regression (the 2026-08 calibration on this install:
night ~2.3 C, hot afternoon ~0.7 C, slope -0.124 C/C), the ambient range
the data covers, and drop-in constants for ``thermal.py``. Run it again
when a new season extends the covered ambient range — the clamp in
``idle_offset_c`` marks where the current calibration stops being evidence.
"""

from __future__ import annotations

import argparse
import math
import sqlite3
from datetime import datetime


def _rows(conn: sqlite3.Connection, sql: str, params: tuple) -> list[dict]:
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _interp(series: list[tuple[float, float]], ts: float, max_gap: float = 300.0) -> float | None:
    """Linear interpolation with a gap guard: None when the bracketing
    samples are too far apart to trust the line between them."""
    if not series or ts < series[0][0] or ts > series[-1][0]:
        return None
    lo, hi = 0, len(series) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if series[mid][0] <= ts:
            lo = mid
        else:
            hi = mid
    (t0, v0), (t1, v1) = series[lo], series[hi]
    if t1 - t0 > max_gap:
        return None
    if t1 == t0:
        return v0
    return v0 + (ts - t0) / (t1 - t0) * (v1 - v0)


def _mean_sd(vals: list[float]) -> tuple[float, float]:
    mu = sum(vals) / len(vals)
    if len(vals) < 2:
        return mu, float("nan")
    return mu, math.sqrt(sum((v - mu) ** 2 for v in vals) / (len(vals) - 1))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--db", required=True, help="path to a wallmonitor.db (read-only; use a copy)")
    parser.add_argument("--source", default="ecowitt", help="ambient source tag (default %(default)s)")
    parser.add_argument("--lookback-days", type=float, default=30.0)
    parser.add_argument("--settle-hours", type=float, default=1.0,
                        help="idle time required after charging before samples count")
    parser.add_argument("--max-drift-c", type=float, default=0.5,
                        help="max ambient change over the prior 30 min (quasi-static gate)")
    parser.add_argument("--min-seg-span-s", type=float, default=1200.0)
    parser.add_argument("--min-seg-samples", type=int, default=10)
    args = parser.parse_args(argv)

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    now = _rows(conn, "SELECT MAX(ts) AS t FROM vitals_samples", ())[0]["t"]
    t_from = now - args.lookback_days * 86400.0

    amb = [
        (r["ts"], r["temp_c"])
        for r in _rows(
            conn,
            "SELECT ts, temp_c FROM ambient_samples "
            "WHERE ts >= ? AND source = ? AND temp_c IS NOT NULL ORDER BY ts",
            (t_from, args.source),
        )
    ]
    if len(amb) < 100:
        print(f"only {len(amb)} '{args.source}' ambient samples in range — nothing to calibrate against")
        return 1

    vit = _rows(
        conn,
        "SELECT ts, total_power_w, contactor_closed, vehicle_current_a, "
        "CASE WHEN handle_temp_c >= 255 THEN NULL ELSE handle_temp_c END AS handle_temp_c "
        "FROM vitals_samples WHERE ts >= ? ORDER BY ts",
        (t_from,),
    )

    settle_s = args.settle_hours * 3600.0
    last_charge = None
    samples: list[tuple[float, float, float]] = []  # ts, handle, ambient
    for r in vit:
        if (r["total_power_w"] or 0) > 50:
            last_charge = r["ts"]
            continue
        if r["contactor_closed"] or (r["vehicle_current_a"] or 0) >= 1:
            continue
        if r["handle_temp_c"] is None:
            continue
        if last_charge is not None and r["ts"] - last_charge < settle_s:
            continue
        a_now = _interp(amb, r["ts"])
        a_prev = _interp(amb, r["ts"] - 1800.0)
        if a_now is None or a_prev is None or abs(a_now - a_prev) >= args.max_drift_c:
            continue
        samples.append((r["ts"], r["handle_temp_c"], a_now))

    if not samples:
        print("no settled quasi-static idle samples — is the sensor overlapping idle periods?")
        return 1

    # Collapse to contiguous segments (split on >10 min gaps).
    segs: list[list[tuple[float, float, float]]] = [[samples[0]]]
    for s in samples[1:]:
        if s[0] - segs[-1][-1][0] > 600.0:
            segs.append([s])
        else:
            segs[-1].append(s)
    segs = [g for g in segs
            if len(g) >= args.min_seg_samples and g[-1][0] - g[0][0] >= args.min_seg_span_s]
    if len(segs) < 8:
        print(f"only {len(segs)} usable idle segments — too few for inference; widen the lookback")
        return 1

    seg_off = [sum(h - a for _, h, a in g) / len(g) for g in segs]
    seg_amb = [sum(a for _, _, a in g) / len(g) for g in segs]
    seg_day = [datetime.fromtimestamp((g[0][0] + g[-1][0]) / 2).strftime("%Y-%m-%d") for g in segs]
    seg_hour = [datetime.fromtimestamp((g[0][0] + g[-1][0]) / 2).hour for g in segs]

    n = len(segs)
    mean, sd = _mean_sd(seg_off)
    se = sd / math.sqrt(n)
    tcrit = 2.045 if n < 60 else 2.0
    print(f"{len(samples)} settled quasi-static idle samples in {n} segments over "
          f"{len(set(seg_day))} days")
    print(f"mean offset {mean:.2f} C  (sd {sd:.2f}, 95% CI "
          f"[{mean - tcrit * se:.2f}, {mean + tcrit * se:.2f}])")

    day = [o for o, h in zip(seg_off, seg_hour) if 8 <= h < 20]
    night = [o for o, h in zip(seg_off, seg_hour) if not 8 <= h < 20]
    for label, vals in (("day (08-20)", day), ("night", night)):
        if len(vals) >= 2:
            mu, s = _mean_sd(vals)
            print(f"  {label}: mean {mu:.2f} sd {s:.2f} (n={len(vals)})")

    # offset ~ ambient regression, day-jackknifed slope error
    mx, _ = _mean_sd(seg_amb)
    my = mean
    sxx = sum((x - mx) ** 2 for x in seg_amb)
    slope = sum((x - mx) * (y - my) for x, y in zip(seg_amb, seg_off)) / sxx
    days = sorted(set(seg_day))
    jk = []
    for d in days:
        keep = [(x, y) for x, y, sd_ in zip(seg_amb, seg_off, seg_day) if sd_ != d]
        kx = [x for x, _ in keep]
        ky = [y for _, y in keep]
        kmx = sum(kx) / len(kx)
        kmy = sum(ky) / len(ky)
        ksxx = sum((x - kmx) ** 2 for x in kx)
        jk.append(sum((x - kmx) * (y - kmy) for x, y in zip(kx, ky)) / ksxx)
    mj = sum(jk) / len(jk)
    se_slope = math.sqrt((len(jk) - 1) / len(jk) * sum((v - mj) ** 2 for v in jk))
    lo_a, hi_a = min(seg_amb), max(seg_amb)
    print(f"offset vs ambient: slope {slope:.4f} C/C "
          f"(day-jackknife se {se_slope:.4f}, t {slope / se_slope:.1f}), "
          f"coverage {lo_a:.1f}..{hi_a:.1f} C")

    ref = 30.0
    off_ref = my + slope * (ref - mx)
    print()
    print("drop-in constants for thermal.py (linear model):")
    print(f"  IDLE_OFFSET_REF_C = {off_ref:.2f}")
    print(f"  IDLE_OFFSET_SLOPE = {slope:.4f}")
    print(f"  IDLE_OFFSET_AMBIENT_REF_C = {ref:.1f}")
    print(f"  IDLE_OFFSET_AMBIENT_RANGE_C = ({math.floor(lo_a):.1f}, {math.ceil(hi_a * 2) / 2:.1f})")

    try:
        from wallmonitor import thermal

        rms_cur = math.sqrt(sum((o - thermal.idle_offset_c(a)) ** 2
                                for o, a in zip(seg_off, seg_amb)) / n)
        rms_new = math.sqrt(sum((o - (off_ref + slope * (a - ref))) ** 2
                                for o, a in zip(seg_off, seg_amb)) / n)
        print(f"segment RMS error — current thermal.py model: {rms_cur:.2f} C, "
              f"this fit: {rms_new:.2f} C")
    except ImportError:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
