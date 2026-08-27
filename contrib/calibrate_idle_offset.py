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

After adopting new constants, remember that fit history is recomputed, not
stored: every fit whose ambient came through the handle proxy
(``ambient_source`` "pre_idle"/"cooldown_tail") shifts to the new model on
the next read, while measured-sourced fits stay put. Before reading
rise_ref movement as degradation across a recalibration, compare within
one ambient_source tier or re-anchor the verified baseline
(POST /api/thermal/baseline-anchor).
"""

from __future__ import annotations

import argparse
import json
import sqlite3

from wallmonitor import calibration, thermal


def _rows(conn: sqlite3.Connection, sql: str, params: tuple) -> list[dict]:
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--db", required=True, help="path to a wallmonitor.db (read-only; use a copy)")
    parser.add_argument("--lookback-days", type=float, default=30.0)
    parser.add_argument("--settle-hours", type=float, default=1.0,
                        help="idle time required after charging before samples count")
    parser.add_argument("--max-drift-c", type=float, default=0.5,
                        help="max ambient change over the prior 30 min (quasi-static gate)")
    parser.add_argument("--min-seg-span-s", type=float, default=1200.0)
    parser.add_argument("--min-seg-samples", type=int, default=10)
    args = parser.parse_args(argv)

    # The estimator itself lives in the package (wallmonitor.calibration) and
    # is what the running monitor refits from daily; this script is the same
    # code over the same SQL, for a copy of a database on any machine.
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    now = _rows(conn, "SELECT MAX(ts) AS t FROM vitals_samples", ())[0]["t"]
    t_from = now - args.lookback_days * 86400.0
    amb = [(r["ts"], r["temp_c"]) for r in _rows(conn, calibration.AMBIENT_SQL,
                                                  (t_from, now + 1, calibration.CAR_SOURCE))]
    if len(amb) < 100:
        print(f"only {len(amb)} stationary ambient samples in range — nothing to calibrate against")
        return 1
    vit = _rows(conn, calibration.VITALS_SQL, (t_from, now + 1))
    cal = calibration.estimate(vit, amb, settle_s=args.settle_hours * 3600.0, max_drift_c=args.max_drift_c,
                               min_seg_span_s=args.min_seg_span_s, min_seg_samples=args.min_seg_samples)
    if cal is None:
        print("too few settled, quasi-static idle segments for inference — widen the lookback, "
              "or check that the sensor overlaps idle periods")
        return 1

    print(f"{cal.n_samples} settled quasi-static idle samples in {cal.n_segments} segments over {cal.n_days} days")
    print(f"mean offset {cal.mean_offset_c:.2f} C  (sd {cal.sd_c:.2f}, 95% CI "
          f"[{cal.ci95_c[0]:.2f}, {cal.ci95_c[1]:.2f}])")
    if cal.day_mean_c is not None:
        print(f"  day (08-20): mean {cal.day_mean_c:.2f}")
    if cal.night_mean_c is not None:
        print(f"  night: mean {cal.night_mean_c:.2f}")
    t_slope = cal.slope / cal.slope_se if cal.slope_se and cal.slope_se == cal.slope_se and cal.slope_se > 0 else float("nan")
    print(f"offset vs ambient: slope {cal.slope:.4f} C/C (day-jackknife se {cal.slope_se:.4f}, t {t_slope:.1f}), "
          f"coverage {cal.ambient_lo_c:.1f}..{cal.ambient_hi_c:.1f} C; segment residual sd {cal.residual_sd_c:.2f} C")

    model = calibration.proposed_model(cal, now)
    why = calibration.gate(cal)
    print()
    print("model this implies (what the running monitor would adopt" + (f" — but gated: {why})" if why else "):"))
    print(json.dumps(model, indent=2))
    print()
    print("drop-in constants for thermal.py, if you prefer to change the seed:")
    print(f"  IDLE_OFFSET_REF_C = {model['ref_c']:.2f}")
    print(f"  IDLE_OFFSET_SLOPE = {model['slope']:.4f}")
    print(f"  IDLE_OFFSET_AMBIENT_REF_C = {model['ambient_ref_c']:.1f}")
    print(f"  IDLE_OFFSET_AMBIENT_RANGE_C = ({model['ambient_range_c'][0]:.1f}, {model['ambient_range_c'][1]:.1f})")
    current = thermal.load_idle_offset(_Settings(conn)) if _has_settings(conn) else thermal.BUILTIN_IDLE_OFFSET
    print(f"currently in effect on this database: {current.source} "
          f"({current.ref_c:.2f} C at {current.ambient_ref_c:.0f} C, slope {current.slope:.4f})")
    return 0


class _Settings:
    """Just enough of Database for thermal.load_idle_offset over a raw connection."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def get_setting(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None


def _has_settings(conn: sqlite3.Connection) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'").fetchone())


if __name__ == "__main__":
    raise SystemExit(main())
