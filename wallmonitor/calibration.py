"""Per-install calibration of the idle handle offset.

The handle-proxy ambient (``ambient ≈ idle handle − offset``) rests on a
model of how far above garage air the idle handle settles. The built-in
model was fitted on one install; this module refits it from *this*
install's own history whenever a stationary ambient sensor gives ground
truth, so the shipped constants are the seed every install starts from
rather than the calibration every install is stuck with.

Estimator (shared, verbatim, with ``contrib/calibrate_idle_offset.py``):

- **Settled idle only.** Contactor open, no current, and at least
  ``settle_s`` since the last charging sample — the handle needs a few time
  constants to shed charge heat; including the decay biases the offset up.
- **Quasi-static ambient only.** The handle lags air by ~tau, so during fast
  ambient swings the pairing (handle now, ambient now) is wrong even though
  both sensors are right. Samples count only when ambient moved less than
  ``max_drift_c`` over the prior 30 min.
- **Per-segment means, not per-sample stats.** Consecutive samples are
  massively autocorrelated; treating them as independent would shrink the
  error bars by ~sqrt(n) for free. Contiguous idle runs collapse to one
  observation each and all inference runs on segment means.
- **Day-clustered slope error.** Segments within a day share weather, so
  the offset-vs-ambient slope's significance is jackknifed leave-one-day-out.

Adoption is separate from estimation and deliberately conservative: sanity
gates (segment count, coverage, a bounded slope — the proxy inverts through
``1 + slope``), and hysteresis so the model only moves on a material
change. Every proxy-tier fit in history is reinterpreted under the adopted
model on its next read, and a drift comparison spanning the change sees a
step — so adoption is recorded as an event, not done silently.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime

# Ambient-sample sources that are stationary garage thermometers. A parked
# car's sensor (source "car") drives away and reads high after drives; it
# is never calibration ground truth.
CAR_SOURCE = "car"

# The one SQL both loaders use, so the in-process estimator and the contrib
# script see identical rows (255 is the handle sensor's glitch sentinel).
VITALS_SQL = (
    "SELECT ts, total_power_w, contactor_closed, vehicle_current_a, "
    "CASE WHEN handle_temp_c >= 255 THEN NULL ELSE handle_temp_c END AS handle_temp_c "
    "FROM vitals_samples WHERE ts >= ? AND ts < ? ORDER BY ts"
)
AMBIENT_SQL = (
    "SELECT ts, temp_c FROM ambient_samples "
    "WHERE ts >= ? AND ts < ? AND temp_c IS NOT NULL AND (source IS NULL OR source != ?) ORDER BY ts"
)

AMBIENT_REF_C = 30.0  # the offset is reported at this ambient


@dataclass(frozen=True)
class Calibration:
    """One estimate: what the data says, with its uncertainty attached."""

    n_samples: int
    n_segments: int
    n_days: int
    mean_offset_c: float
    sd_c: float
    ci95_c: tuple[float, float]
    slope: float
    slope_se: float
    ambient_lo_c: float
    ambient_hi_c: float
    offset_ref_c: float  # offset at AMBIENT_REF_C from the regression line
    residual_sd_c: float  # segment scatter around the fitted line
    day_mean_c: float | None
    night_mean_c: float | None
    from_ts: float
    to_ts: float

    def as_dict(self) -> dict:
        return asdict(self)


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


def estimate(vitals: list[dict], ambient: list[tuple[float, float]], *, settle_s: float = 3600.0,
             max_drift_c: float = 0.5, min_seg_span_s: float = 1200.0, min_seg_samples: int = 10,
             min_segments: int = 8) -> Calibration | None:
    """The estimator. ``vitals`` are raw rows (VITALS_SQL shape, oldest
    first); ``ambient`` is the stationary sensor series. None when the
    data can't support an estimate — never a number from too little."""
    if len(ambient) < 100:
        return None
    last_charge = None
    samples: list[tuple[float, float, float]] = []  # ts, handle, ambient
    for r in vitals:
        if (r["total_power_w"] or 0) > 50:
            last_charge = r["ts"]
            continue
        if r["contactor_closed"] or (r["vehicle_current_a"] or 0) >= 1:
            continue
        if r["handle_temp_c"] is None:
            continue
        if last_charge is not None and r["ts"] - last_charge < settle_s:
            continue
        a_now = _interp(ambient, r["ts"])
        a_prev = _interp(ambient, r["ts"] - 1800.0)
        if a_now is None or a_prev is None or abs(a_now - a_prev) >= max_drift_c:
            continue
        samples.append((r["ts"], r["handle_temp_c"], a_now))
    if not samples:
        return None

    segs: list[list[tuple[float, float, float]]] = [[samples[0]]]
    for s in samples[1:]:
        if s[0] - segs[-1][-1][0] > 600.0:
            segs.append([s])
        else:
            segs[-1].append(s)
    segs = [g for g in segs if len(g) >= min_seg_samples and g[-1][0] - g[0][0] >= min_seg_span_s]
    if len(segs) < min_segments:
        return None

    seg_off = [sum(h - a for _, h, a in g) / len(g) for g in segs]
    seg_amb = [sum(a for _, _, a in g) / len(g) for g in segs]
    seg_day = [datetime.fromtimestamp((g[0][0] + g[-1][0]) / 2).strftime("%Y-%m-%d") for g in segs]
    seg_hour = [datetime.fromtimestamp((g[0][0] + g[-1][0]) / 2).hour for g in segs]

    n = len(segs)
    mean, sd = _mean_sd(seg_off)
    se = sd / math.sqrt(n)
    tcrit = 2.045 if n < 60 else 2.0

    day = [o for o, h in zip(seg_off, seg_hour) if 8 <= h < 20]
    night = [o for o, h in zip(seg_off, seg_hour) if not 8 <= h < 20]

    mx, _ = _mean_sd(seg_amb)
    sxx = sum((x - mx) ** 2 for x in seg_amb)
    slope = sum((x - mx) * (y - mean) for x, y in zip(seg_amb, seg_off)) / sxx if sxx > 0 else 0.0
    days = sorted(set(seg_day))
    jk = []
    for d in days:
        keep = [(x, y) for x, y, sd_ in zip(seg_amb, seg_off, seg_day) if sd_ != d]
        if len(keep) < 2:
            continue
        kx = [x for x, _ in keep]
        ky = [y for _, y in keep]
        kmx = sum(kx) / len(kx)
        kmy = sum(ky) / len(ky)
        ksxx = sum((x - kmx) ** 2 for x in kx)
        if ksxx > 0:
            jk.append(sum((x - kmx) * (y - kmy) for x, y in zip(kx, ky)) / ksxx)
    if len(jk) >= 2:
        mj = sum(jk) / len(jk)
        slope_se = math.sqrt((len(jk) - 1) / len(jk) * sum((v - mj) ** 2 for v in jk))
    else:
        slope_se = float("nan")
    offset_ref = mean + slope * (AMBIENT_REF_C - mx)
    resid = [o - (offset_ref + slope * (a - AMBIENT_REF_C)) for o, a in zip(seg_off, seg_amb)]
    residual_sd = math.sqrt(sum(r * r for r in resid) / max(n - 2, 1))
    return Calibration(
        n_samples=len(samples),
        n_segments=n,
        n_days=len(days),
        mean_offset_c=mean,
        sd_c=sd,
        ci95_c=(mean - tcrit * se, mean + tcrit * se),
        slope=slope,
        slope_se=slope_se,
        ambient_lo_c=min(seg_amb),
        ambient_hi_c=max(seg_amb),
        offset_ref_c=offset_ref,
        residual_sd_c=residual_sd,
        day_mean_c=_mean_sd(day)[0] if len(day) >= 2 else None,
        night_mean_c=_mean_sd(night)[0] if len(night) >= 2 else None,
        from_ts=samples[0][0],
        to_ts=samples[-1][0],
    )


def calibrate(db, now: float, lookback_days: float = 30.0, **kwargs) -> Calibration | None:
    """Estimate from a Database, day-chunked so a month of raw rows never
    sits in memory at once."""
    t_from = now - lookback_days * 86400.0
    ambient = db.ambient_series(t_from, now, exclude_source=CAR_SOURCE)
    if len(ambient) < 100:
        return None
    vitals: list[dict] = []
    t = t_from
    while t < now:
        vitals.extend(db.idle_calibration_rows(t, min(t + 86400.0, now)))
        t += 86400.0
    return estimate(vitals, ambient, **kwargs)


# ---------------- adoption ----------------

# Sanity gates: a fit outside these is more likely a contaminated sensor or
# a pathological window than physics, and must not become the proxy model.
OFFSET_REF_RANGE_C = (-1.0, 5.0)
MAX_ABS_SLOPE = 0.5  # the proxy inverts through (1 + slope)
MIN_RANGE_FOR_SLOPE_C = 3.0  # narrower coverage can't support a slope: use a constant
MIN_DAYS = 3
# Hysteresis: re-estimate daily, adopt only on material change, so the
# proxy-tier fit history doesn't creep a little every day.
ADOPT_DELTA_REF_C = 0.25
ADOPT_DELTA_SLOPE = 0.03
ADOPT_RANGE_EXTEND_C = 2.0


def gate(cal: Calibration) -> str | None:
    """Why this calibration must not be adopted, or None if it may be."""
    if cal.n_days < MIN_DAYS:
        return f"only {cal.n_days} days of settled idle"
    lo, hi = OFFSET_REF_RANGE_C
    if not (lo <= cal.offset_ref_c <= hi):
        return f"offset {cal.offset_ref_c:.2f} C at {AMBIENT_REF_C:.0f} C outside {lo}..{hi}"
    if abs(cal.slope) > MAX_ABS_SLOPE and cal.ambient_hi_c - cal.ambient_lo_c >= MIN_RANGE_FOR_SLOPE_C:
        return f"slope {cal.slope:.3f} C/C implausible"
    return None


def proposed_model(cal: Calibration, now: float) -> dict:
    """The idle-offset model this calibration implies, as the settings
    JSON shape thermal.IdleOffset reads. A coverage too narrow to support
    a slope yields a constant offset at the covered ambient."""
    width = cal.ambient_hi_c - cal.ambient_lo_c
    slope = cal.slope if width >= MIN_RANGE_FOR_SLOPE_C else 0.0
    if width >= MIN_RANGE_FOR_SLOPE_C:
        ref_c, ambient_ref_c = cal.offset_ref_c, AMBIENT_REF_C
    else:
        ref_c, ambient_ref_c = cal.mean_offset_c, (cal.ambient_lo_c + cal.ambient_hi_c) / 2.0
    return {
        "ref_c": round(ref_c, 3),
        "slope": round(slope, 4),
        "ambient_ref_c": round(ambient_ref_c, 2),
        "ambient_range_c": [round(math.floor(cal.ambient_lo_c * 2) / 2, 1),
                            round(math.ceil(cal.ambient_hi_c * 2) / 2, 1)],
        "source": "calibrated",
        "segments": cal.n_segments,
        "days": cal.n_days,
        "residual_sd_c": round(cal.residual_sd_c, 3),
        "calibrated_ts": now,
    }


def material_change(old: dict | None, new: dict) -> bool:
    """Hysteresis: is `new` different enough from the stored model to be
    worth reinterpreting the proxy-tier history over?"""
    if not old or old.get("source") != "calibrated":
        return True
    ref = AMBIENT_REF_C
    old_at_ref = old["ref_c"] + old["slope"] * (ref - old["ambient_ref_c"])
    new_at_ref = new["ref_c"] + new["slope"] * (ref - new["ambient_ref_c"])
    if abs(new_at_ref - old_at_ref) > ADOPT_DELTA_REF_C:
        return True
    if abs(new["slope"] - old["slope"]) > ADOPT_DELTA_SLOPE:
        return True
    olo, ohi = old["ambient_range_c"]
    nlo, nhi = new["ambient_range_c"]
    return (olo - nlo) > ADOPT_RANGE_EXTEND_C or (nhi - ohi) > ADOPT_RANGE_EXTEND_C


def maybe_adopt(db, now: float, setting_key: str, lookback_days: float = 30.0) -> tuple[dict | None, dict | None, str | None]:
    """Run a calibration and adopt it if it passes the gates and moves the
    model materially. Returns (old_model, new_model, reason): new_model is
    None when nothing was adopted and reason says why (or None when there
    simply wasn't enough data)."""
    cal = calibrate(db, now, lookback_days)
    if cal is None:
        return None, None, None
    why = gate(cal)
    if why:
        return None, None, why
    new = proposed_model(cal, now)
    raw = db.get_setting(setting_key)
    try:
        old = json.loads(raw) if raw else None
    except ValueError:
        old = None
    if not material_change(old, new):
        return old, None, "no material change"
    db.set_setting(setting_key, json.dumps(new))
    return old, new, None
