"""Thermal-derate (alert 40) model fitting and prediction.

The Gen 3 Wall Connector raises alert 40 ("High temperature detected;
charging is limited") from its plug-handle sensor: observed on firmware
26.18.0, the alert raises the moment handle_temp_c reaches 65 C, charge
current is cut to 50%, and the alert clears once the handle cools to ~60 C
(the derate persists for the rest of the session).

While charging at steady current the handle follows a first-order lag
toward a steady state that sits a roughly constant rise above ambient:

    T(t) = T_inf - (T_inf - T0) * exp(-t / tau)
    T_inf = ambient + rise_ref * (I / REF_CURRENT_A)^2   (resistive heating)

and at idle the handle settles ~2 C above ambient, so the charger doubles
as its own ambient thermometer. tau and rise_ref are fitted per install
from recorded sessions; the defaults come from a verified alert-40 event
where the fit reproduced the observed time-to-trip within 1%.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import median

from .db import Database

TRIP_HANDLE_C = 65.0  # alert 40 raises here (observed, firmware 26.18.0)
CLEAR_HANDLE_C = 60.0  # ...and clears here, but the 50% derate persists
IDLE_OFFSET_C = 2.0  # idle handle temperature sits about this far above ambient
REF_CURRENT_A = 48.0  # rise_ref_c is normalized to this charge current

DEFAULT_TAU_MIN = 12.0
DEFAULT_RISE_REF_C = 36.0

# Fit acceptance gates: a segment must actually contain a thermal ramp and
# the exponential must describe it well, or it teaches the model nothing.
MIN_SEGMENT_S = 480.0
MIN_SEGMENT_SAMPLES = 12
MIN_RISE_SEEN_C = 4.0
MAX_FIT_RMSE_C = 0.6
TAU_RANGE_MIN = (3.0, 40.0)
RISE_RANGE_C = (10.0, 80.0)


@dataclass
class ThermalParams:
    tau_min: float = DEFAULT_TAU_MIN
    rise_ref_c: float = DEFAULT_RISE_REF_C
    tau_fits: int = 0
    rise_fits: int = 0
    fit_rmse_c: float | None = None

    @property
    def fitted(self) -> bool:
        return self.tau_fits > 0 and self.rise_fits > 0

    def as_dict(self) -> dict:
        return {
            "tau_min": round(self.tau_min, 2),
            "rise_ref_c": round(self.rise_ref_c, 1),
            "ref_current_a": REF_CURRENT_A,
            "trip_c": TRIP_HANDLE_C,
            "clear_c": CLEAR_HANDLE_C,
            "idle_offset_c": IDLE_OFFSET_C,
            "tau_fits": self.tau_fits,
            "rise_fits": self.rise_fits,
            "fit_rmse_c": round(self.fit_rmse_c, 3) if self.fit_rmse_c is not None else None,
            "fitted": self.fitted,
        }


def _fit_exponential(points: list[tuple[float, float]]) -> tuple[float, float, float] | None:
    """Least-squares fit of T(t) = T_inf - (T_inf - T0)*exp(-t/tau).

    T0 is pinned to the first sample; tau is grid-searched and T_inf is the
    closed-form optimum for each tau. Returns (tau_s, t_inf, rmse) or None.
    """
    t0, temp0 = points[0]
    best: tuple[float, float, float] | None = None
    tau = TAU_RANGE_MIN[0] * 60.0
    while tau <= TAU_RANGE_MIN[1] * 60.0:
        xs = [math.exp(-(t - t0) / tau) for t, _ in points]
        num = den = 0.0
        for (_, temp), x in zip(points, xs):
            num += (1.0 - x) * (temp - temp0 * x)
            den += (1.0 - x) ** 2
        if den > 1e-9:
            t_inf = num / den
            sse = sum(
                (t_inf - (t_inf - temp0) * x - temp) ** 2 for (_, temp), x in zip(points, xs)
            )
            rmse = math.sqrt(sse / len(points))
            if best is None or rmse < best[2]:
                best = (tau, t_inf, rmse)
        tau += 15.0
    return best


def _steady_current_prefix(samples: list[dict]) -> list[dict]:
    """The session's first steady-current run.

    The reference current is the median of the first 10 minutes of charging,
    not of the whole session: a session that derates midway spends most of
    its samples at the reduced current, and a whole-session median would put
    the initial full-rate ramp — the part with the thermal signal — outside
    the band. Leading samples still ramping up to the plateau are skipped
    rather than treated as the end of the run.
    """
    charging = [
        s
        for s in samples
        if s.get("contactor_closed") and (s.get("vehicle_current_a") or 0) >= 16
    ]
    if not charging:
        return []
    t0 = charging[0]["ts"]
    i_ref = median(s["vehicle_current_a"] for s in charging if s["ts"] - t0 <= 600)
    band = max(2.0, 0.1 * i_ref)
    prefix: list[dict] = []
    for s in charging:
        if abs(s["vehicle_current_a"] - i_ref) > band:
            if prefix:
                break  # the steady run ended (derate or charge stop)
            continue  # still ramping up to the plateau
        prefix.append(s)
        if s["ts"] - prefix[0]["ts"] > 1800:  # first 30 min is where the ramp lives
            break
    return prefix


def _ambient_before(db: Database, start_ts: float) -> float | None:
    """Ambient estimate from the idle handle temperature before a session."""
    rows = db.vitals_range(start_ts - 2400, start_ts - 30, 5000)
    idle = [
        r["handle_temp_c"]
        for r in rows
        if not r.get("contactor_closed")
        and (r.get("vehicle_current_a") or 0) < 1
        and r.get("handle_temp_c") is not None
    ]
    if len(idle) < 5 or max(idle) - min(idle) > 2.0:
        return None
    return median(idle) - IDLE_OFFSET_C


def fit_sessions(db: Database, now: float, lookback_days: float = 120.0) -> list[dict]:
    """Per-session fits, oldest first: one dict per session whose charging
    ramp passed the quality gates. rise_ref_c is None when there was no
    stable pre-session idle to estimate ambient from."""
    sessions = [
        s
        for s in db.sessions_range(now - lookback_days * 86400, now)
        if s.get("end_ts") and (s.get("charging_s") or 0) >= MIN_SEGMENT_S
    ][:40]
    fits: list[dict] = []
    for sess in sessions:
        # Only the first ~45 min matters (the ramp), and the narrow window
        # keeps a multi-hour session under the bucket-averaging threshold so
        # the fit sees raw-resolution samples.
        t_hi = min(sess["end_ts"], sess["start_ts"] + 2700)
        samples = db.vitals_range(sess["start_ts"] - 1, t_hi + 1, 5000)
        prefix = _steady_current_prefix(samples)
        seg = [(s["ts"], s["handle_temp_c"]) for s in prefix if s.get("handle_temp_c") is not None]
        if len(seg) < MIN_SEGMENT_SAMPLES or seg[-1][0] - seg[0][0] < MIN_SEGMENT_S:
            continue
        if max(t for _, t in seg) - seg[0][1] < MIN_RISE_SEEN_C:
            continue
        fit = _fit_exponential(seg)
        if fit is None:
            continue
        tau_s, t_inf, rmse = fit
        if rmse > MAX_FIT_RMSE_C or t_inf <= seg[0][1] + 3.0:
            continue
        i_med = median(s["vehicle_current_a"] for s in prefix)
        ambient = _ambient_before(db, sess["start_ts"])
        rise = None
        if ambient is not None:
            rise = (t_inf - ambient) * (REF_CURRENT_A / i_med) ** 2
            if not (RISE_RANGE_C[0] <= rise <= RISE_RANGE_C[1]):
                rise = None
        fits.append(
            {
                "session_id": sess["id"],
                "start_ts": sess["start_ts"],
                "tau_min": round(tau_s / 60.0, 2),
                "rise_ref_c": round(rise, 2) if rise is not None else None,
                "rmse_c": round(rmse, 3),
                "current_a": round(i_med, 1),
            }
        )
    fits.sort(key=lambda f: f["start_ts"])
    return fits


def fit_history(db: Database, now: float, lookback_days: float = 120.0,
                fits: list[dict] | None = None) -> ThermalParams:
    """Aggregate per-session fits into model parameters; defaults where thin."""
    if fits is None:
        fits = fit_sessions(db, now, lookback_days)
    taus = [f["tau_min"] for f in fits]
    rises = [f["rise_ref_c"] for f in fits if f["rise_ref_c"] is not None]
    rmses = [f["rmse_c"] for f in fits]
    return ThermalParams(
        tau_min=median(taus) if taus else DEFAULT_TAU_MIN,
        rise_ref_c=median(rises) if rises else DEFAULT_RISE_REF_C,
        tau_fits=len(taus),
        rise_fits=len(rises),
        fit_rmse_c=median(rmses) if rmses else None,
    )


# ---------------- degradation watch ----------------

# A loose lug or degrading contact shows up as extra resistance: more heat
# rise for the same current. Prediction alone hides that (the rolling median
# just follows it), so the drift watch compares recent sessions against the
# earlier baseline and flags a sustained increase.
DRIFT_RECENT_N = 3
DRIFT_MIN_BASELINE_N = 3
DRIFT_WARN_C = 2.5
DRIFT_ALERT = "Handle heat rise increasing (check connector/wiring)"


def detect_drift(fits: list[dict]) -> dict | None:
    """Compare the last few sessions' fitted rise against the baseline.

    Returns None while there is too little history to judge; otherwise a
    verdict dict with the medians compared. Only rise (not tau) is watched:
    added contact resistance changes how much heat is made, not how fast the
    handle mass warms.
    """
    rises = [(f["start_ts"], f["rise_ref_c"]) for f in fits if f["rise_ref_c"] is not None]
    rises.sort(key=lambda r: r[0])
    if len(rises) < DRIFT_RECENT_N + DRIFT_MIN_BASELINE_N:
        return None
    recent = [r for _, r in rises[-DRIFT_RECENT_N:]]
    baseline = [r for _, r in rises[:-DRIFT_RECENT_N]]
    recent_med = median(recent)
    baseline_med = median(baseline)
    delta = recent_med - baseline_med
    return {
        "drifting": delta >= DRIFT_WARN_C,
        "recent_rise_c": round(recent_med, 2),
        "baseline_rise_c": round(baseline_med, 2),
        "delta_c": round(delta, 2),
        "recent_n": len(recent),
        "baseline_n": len(baseline),
        "threshold_c": DRIFT_WARN_C,
    }


def _minutes_to_trip(t_now: float, t_inf: float, tau_min: float) -> float | None:
    """Minutes until the handle reaches the trip point, or None if it never will."""
    if t_now >= TRIP_HANDLE_C:
        return 0.0
    if t_inf <= TRIP_HANDLE_C + 0.2:
        return None
    return tau_min * math.log((t_inf - t_now) / (t_inf - TRIP_HANDLE_C))


SUGGEST_MARGIN_C = 2.0  # keep the suggested current's steady state this far under the trip


def suggest_max_current(ambient_c: float, params: ThermalParams) -> float | None:
    """Highest charge current whose steady-state handle temp stays safely
    below the trip point at the given ambient — the alternative to letting
    the charger fold back to a blunt 50%. Vehicles take whole amps, so the
    value is floored. None when even a minimal rate would trip (or when no
    cap is needed at all, i.e. full rate is already safe)."""
    headroom = TRIP_HANDLE_C - SUGGEST_MARGIN_C - ambient_c
    if headroom <= 0:
        return None
    amps = math.floor(REF_CURRENT_A * math.sqrt(headroom / params.rise_ref_c))
    if amps < 6:  # J1772 floor — below this the vehicle won't charge anyway
        return None
    if amps >= REF_CURRENT_A:
        return None  # full rate is safe; no cap to suggest
    return float(amps)


def predict(db: Database, now: float, params: ThermalParams) -> dict:
    """Forecast alert-40 for the current state (live session or idle)."""
    out: dict = {"model": params.as_dict(), "state": "no_data", "forecast": None}
    recent = [r for r in db.vitals_range(now - 900, now, 2000) if r.get("handle_temp_c") is not None]
    if not recent:
        return out
    last = recent[-1]
    out.update(
        {
            "ts": last["ts"],
            "handle_c": round(last["handle_temp_c"], 1),
            "current_a": last.get("vehicle_current_a"),
        }
    )
    if now - last["ts"] > 120:
        out["state"] = "stale"
        return out

    tau_min = params.tau_min
    current = last.get("vehicle_current_a") or 0.0
    charging = bool(last.get("contactor_closed")) and current >= 6.0

    if charging:
        out["state"] = "charging"
        band = max(2.0, 0.1 * current)
        window: list[tuple[float, float]] = []
        for s in reversed(recent):
            if (
                not s.get("contactor_closed")
                or abs((s.get("vehicle_current_a") or 0) - current) > band
                or last["ts"] - s["ts"] > 360
            ):
                break
            window.append((s["ts"], s["handle_temp_c"]))
        window.reverse()
        forecast: dict = {}
        if len(window) >= 8 and window[-1][0] - window[0][0] >= 120:
            # Project the steady state from the recent trajectory: with tau
            # known, T(t) = T_inf - C*exp(-t/tau) is linear in (T_inf, C), so
            # an ordinary least-squares line on x = exp(-t/tau) gives an
            # unbiased T_inf (a straight-line slope would read the window's
            # average rate and overshoot during a fast ramp). No ambient
            # input needed.
            n = len(window)
            xs = [math.exp(-(t - window[0][0]) / (tau_min * 60.0)) for t, _ in window]
            mx = sum(xs) / n
            mv = sum(v for _, v in window) / n
            var = sum((x - mx) ** 2 for x in xs)
            cov = sum((x - mx) * (v - mv) for x, (_, v) in zip(xs, window))
            t_inf = mv - (cov / var) * mx if var > 1e-9 else last["handle_temp_c"]
            forecast["basis"] = "trajectory"
        else:
            # Too early in the session for a slope: model from pre-session
            # ambient and the present current scaled by I^2.
            sid = last.get("session_id")
            sess = db.session(int(sid)) if sid else None
            ambient = _ambient_before(db, sess["start_ts"]) if sess else None
            if ambient is None:
                out["forecast"] = {"basis": "insufficient", "will_trip": None}
                return out
            t_inf = ambient + params.rise_ref_c * (current / REF_CURRENT_A) ** 2
            forecast["basis"] = "model"
        t_inf = max(t_inf, last["handle_temp_c"] - 0.5)
        minutes = _minutes_to_trip(last["handle_temp_c"], t_inf, tau_min)
        forecast.update(
            {
                "steady_state_c": round(t_inf, 1),
                "will_trip": minutes is not None,
                "minutes_to_trip": round(minutes, 1) if minutes is not None else None,
                "trip_ts": last["ts"] + minutes * 60.0 if minutes is not None else None,
            }
        )
        if minutes is not None:
            # Ambient implied by the steady state at this current; from it,
            # the highest cap that avoids the trip (and the 50% foldback).
            ambient = t_inf - params.rise_ref_c * (current / REF_CURRENT_A) ** 2
            forecast["suggested_max_a"] = suggest_max_current(ambient, params)
        out["forecast"] = forecast
        return out

    if current < 1.0 and not last.get("contactor_closed"):
        out["state"] = "idle"
        temps = [r["handle_temp_c"] for r in recent if last["ts"] - r["ts"] <= 900]
        stable = len(temps) >= 3 and max(temps) - min(temps) <= 1.5
        ambient = last["handle_temp_c"] - IDLE_OFFSET_C
        out["ambient_c"] = round(ambient, 1)
        out["ambient_stable"] = stable
        # Hypothetical: a full-rate session started right now.
        t_inf = ambient + params.rise_ref_c
        minutes = _minutes_to_trip(last["handle_temp_c"], t_inf, tau_min)
        out["forecast"] = {
            "basis": "hypothetical",
            "steady_state_c": round(t_inf, 1),
            "will_trip": minutes is not None,
            "minutes_to_trip": round(minutes, 1) if minutes is not None else None,
            "trip_ts": None,
            "safe_ambient_max_c": round(TRIP_HANDLE_C - params.rise_ref_c, 1),
            "suggested_max_a": suggest_max_current(ambient, params) if minutes is not None else None,
        }
        return out

    out["state"] = "connected"
    return out
