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

The unit of thermal analysis is the load window — the stretch where current
actually flows — not the plug-in session. Ambient is read at both ends of
the window (flat idle or cool-down tail before; the charge's own cool-down
tail after) and interpolated across it, so weather moving during a charge
is measured and removed instead of leaking into the fitted rise.
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

# Live-forecast gate: a steady-current window must hold this many samples
# over this much time before its trajectory is projected.
TRAJECTORY_MIN_SAMPLES = 8
TRAJECTORY_MIN_SPAN_S = 120.0
# A session is one plug-in, but charging within it comes in distinct
# segments, often hours apart: the vehicle's own state-of-charge top-offs,
# scheduled-departure preconditioning, or a charging schedule. (The charger
# itself exposes no "scheduled charging" state — verified on firmware
# 26.18.0: with a vehicle-side schedule armed overnight it idles in plain
# connected states until the car starts drawing.) Charging gaps longer than
# this split segments; each segment's opening ramp is a fit candidate.
SEGMENT_SPLIT_GAP_S = 300.0
MAX_SEGMENTS_PER_SESSION = 4


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
    start_ts, start_temp = points[0]
    best: tuple[float, float, float] | None = None
    tau = TAU_RANGE_MIN[0] * 60.0
    while tau <= TAU_RANGE_MIN[1] * 60.0:
        decays = [math.exp(-(ts - start_ts) / tau) for ts, _ in points]
        num = den = 0.0
        for (_, temp), decay in zip(points, decays):
            num += (1.0 - decay) * (temp - start_temp * decay)
            den += (1.0 - decay) ** 2
        if den > 1e-9:
            t_inf = num / den
            sse = sum(
                (t_inf - (t_inf - start_temp) * decay - temp) ** 2
                for (_, temp), decay in zip(points, decays)
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
        sample
        for sample in samples
        if sample.get("contactor_closed") and (sample.get("vehicle_current_a") or 0) >= 16
    ]
    if not charging:
        return []
    charge_start_ts = charging[0]["ts"]
    i_ref = median(
        sample["vehicle_current_a"]
        for sample in charging
        if sample["ts"] - charge_start_ts <= 600
    )
    band = max(2.0, 0.1 * i_ref)
    prefix: list[dict] = []
    for sample in charging:
        if prefix and sample["ts"] - prefix[-1]["ts"] > SEGMENT_SPLIT_GAP_S:
            break  # a charging gap: the next samples belong to a later segment
        if abs(sample["vehicle_current_a"] - i_ref) > band:
            if prefix:
                break  # the steady run ended (derate or charge stop)
            continue  # still ramping up to the plateau
        prefix.append(sample)
        if sample["ts"] - prefix[0]["ts"] > 1800:  # first 30 min is where the ramp lives
            break
    return prefix


def _segments(rows: list[dict]) -> list[tuple[float, float]]:
    """(start, end) timestamps of distinct charging segments in a session.

    Works on bucket-averaged rows as well as raw ones: a bucket that saw any
    charging keeps contactor_closed via MAX(), and its MIN(ts) lands at or
    before the actual charge start, so the per-segment raw fetch that follows
    never misses the ramp's beginning. The end is the last charging row of
    the run — on bucketed rows up to one bucket early, which only trims the
    top of the cool-down tail read from it.
    """
    segments: list[tuple[float, float]] = []
    start: float | None = None
    prev: float | None = None
    for row in rows:
        if not row.get("contactor_closed"):
            continue
        if prev is None or row["ts"] - prev > SEGMENT_SPLIT_GAP_S:
            if start is not None:
                segments.append((start, prev))
            start = row["ts"]
        prev = row["ts"]
    if start is not None and prev is not None:
        segments.append((start, prev))
    return segments


def _ambient_before(db: Database, start_ts: float) -> float | None:
    """Ambient estimate from the idle handle temperature before a session."""
    rows = db.vitals_range(start_ts - 2400, start_ts - 30, 5000)
    idle = [
        row["handle_temp_c"]
        for row in rows
        if not row.get("contactor_closed")
        and (row.get("vehicle_current_a") or 0) < 1
        and row.get("handle_temp_c") is not None
    ]
    if len(idle) < 5 or max(idle) - min(idle) > 2.0:
        return None
    return median(idle) - IDLE_OFFSET_C


# Cool-down-tail ambient: gates for reading ambient from a still-warm
# handle's decay when no flat idle window exists before a segment.
COOLDOWN_MIN_SAMPLES = 10
COOLDOWN_MIN_SPAN_S = 300.0
COOLDOWN_MIN_DROP_C = 1.0
COOLDOWN_MAX_RMSE_C = 0.5


def _decay_asymptote(tail: list[dict], tau_min: float) -> float | None:
    """Ambient from a cooling handle's decay: least-squares asymptote of a
    first-order lag at a known tau, minus the idle offset. Gated on span,
    visible drop, and fit quality; None when the tail can't be trusted."""
    if len(tail) < COOLDOWN_MIN_SAMPLES or tail[-1]["ts"] - tail[0]["ts"] < COOLDOWN_MIN_SPAN_S:
        return None
    start_temp = tail[0]["handle_temp_c"]
    if start_temp - tail[-1]["handle_temp_c"] < COOLDOWN_MIN_DROP_C:
        return None  # not visibly cooling; nothing to extrapolate
    tau_s = tau_min * 60.0
    decays = [math.exp(-(row["ts"] - tail[0]["ts"]) / tau_s) for row in tail]
    num = den = 0.0
    for row, decay in zip(tail, decays):
        num += (1.0 - decay) * (row["handle_temp_c"] - start_temp * decay)
        den += (1.0 - decay) ** 2
    if den < 1e-9:
        return None
    asymptote = num / den
    rmse = math.sqrt(
        sum(
            (asymptote - (asymptote - start_temp) * decay - row["handle_temp_c"]) ** 2
            for row, decay in zip(tail, decays)
        )
        / len(tail)
    )
    if rmse > COOLDOWN_MAX_RMSE_C:
        return None  # not a clean single-exponential decay at this tau
    ambient = asymptote - IDLE_OFFSET_C
    if not (-30.0 <= ambient <= TRIP_HANDLE_C):
        return None
    return ambient


def _idle_rows(rows: list[dict]) -> list[dict]:
    return [
        row
        for row in rows
        if not row.get("contactor_closed")
        and (row.get("vehicle_current_a") or 0) < 1
        and row.get("handle_temp_c") is not None
        and row["handle_temp_c"] < 200
    ]


def _ambient_from_cooldown(db: Database, start_ts: float, tau_min: float) -> float | None:
    """Ambient from the cool-down tail before a segment that starts warm.

    A stop/resume or post-derate segment begins before the handle has
    cooled, so _ambient_before finds no stable idle window and the segment
    loses its rise fit. But the decay itself encodes ambient: idle cooling
    follows the same first-order lag as the charge ramp, settling at
    ambient + IDLE_OFFSET_C. With tau known from this install's fitted
    ramps, the asymptote is a closed-form least squares over a few minutes
    of tail — no flat stretch needed. (The live forecast bridges the same
    gap by inferring ambient *from* the fitted rise; that would be circular
    here, where the rise is the thing being measured.)
    """
    idle = _idle_rows(db.vitals_range(start_ts - 2400, start_ts - 5, 5000))
    if not idle:
        return None
    # The contiguous idle run ending at the segment start (tolerating normal
    # polling gaps) — the tail of the previous charge's cool-down.
    tail = [idle[-1]]
    for row in reversed(idle[:-1]):
        if tail[-1]["ts"] - row["ts"] > 120.0:
            break
        tail.append(row)
    tail.reverse()
    return _decay_asymptote(tail, tau_min)


def _ambient_after(db: Database, end_ts: float, tau_min: float) -> float | None:
    """Ambient at the end of a load window, from the cool-down that follows.

    The moment current stops, the handle decays from its working temperature
    toward ambient + IDLE_OFFSET_C along the same first-order lag as the
    charge ramp, so the tail right after the window closes encodes the
    ambient *then* — the other bracket of the window. A point ambient read
    only at the window's start silently assumes the garage held still for
    the whole charge; on a summer afternoon (or a cooling night) it didn't,
    and the fitted rise absorbed the weather.
    """
    idle = _idle_rows(db.vitals_range(end_ts + 5, end_ts + 2400, 5000))
    if not idle:
        return None
    # The contiguous idle run starting at the window's close (tolerating
    # normal polling gaps) — this charge's own cool-down.
    tail = [idle[0]]
    for row in idle[1:]:
        if row["ts"] - tail[-1]["ts"] > 120.0:
            break
        tail.append(row)
    return _decay_asymptote(tail, tau_min)


def fit_sessions(db: Database, now: float, lookback_days: float = 120.0) -> list[dict]:
    """Per-load-window fits, oldest first: one dict per charging segment
    whose ramp passed the quality gates. A session that idles for hours and
    then charges (a vehicle top-off, preconditioning, a charging schedule)
    yields its fits from wherever the ramps actually are, not just the
    plug-in moment.

    Ambient is a bracket, not a point. The window's start ambient comes from
    the flat idle stretch before the segment, or — when the segment starts
    with a still-warm handle (stop/resume, post-derate) — from the previous
    charge's cool-down tail extrapolated to its asymptote. The window's end
    ambient comes from this charge's own cool-down tail after current stops.
    When both ends read, the samples are de-trended against the linear
    ambient ramp between them and refitted, so a garage that warmed (or
    cooled) during the charge stops masquerading as connector resistance.
    When only the start reads, the fit falls back to the old point-ambient
    behavior and says so (ambient_end_c/ambient_drift_c are None).

    rise_ref_c is None only when no ambient read succeeds; each fit carries
    ambient_source ("pre_idle" or "cooldown_tail"), ambient_c, and — when
    bracketed — ambient_end_c and ambient_drift_c (end minus start)."""
    sessions = [
        session
        for session in db.sessions_range(now - lookback_days * 86400, now)
        if session.get("end_ts") and (session.get("charging_s") or 0) >= MIN_SEGMENT_S
    ][:40]
    fits: list[dict] = []
    for sess in sessions:
        # Coarse pass over the whole session to locate charging segments —
        # bucket-averaged is fine here (and keeps a multi-day session cheap);
        # each segment then gets a narrow raw-resolution fetch, since only
        # the ramp's first ~45 min carries the thermal signal.
        coarse = db.vitals_range(sess["start_ts"] - 1, sess["end_ts"] + 1, 2000)
        segments = _segments(coarse)[:MAX_SEGMENTS_PER_SESSION]
        for idx, (seg_start, seg_end) in enumerate(segments):
            next_start = segments[idx + 1][0] if idx + 1 < len(segments) else sess["end_ts"] + 1
            t_hi = min(sess["end_ts"], seg_start + 2700, next_start - 1)
            samples = db.vitals_range(seg_start - 1, t_hi + 1, 5000)
            prefix = _steady_current_prefix(samples)
            seg = [
                (sample["ts"], sample["handle_temp_c"])
                for sample in prefix
                if sample.get("handle_temp_c") is not None
            ]
            if len(seg) < MIN_SEGMENT_SAMPLES or seg[-1][0] - seg[0][0] < MIN_SEGMENT_S:
                continue
            if max(temp for _, temp in seg) - seg[0][1] < MIN_RISE_SEEN_C:
                continue
            fit = _fit_exponential(seg)
            if fit is None:
                continue
            tau_s, t_inf, rmse = fit
            if rmse > MAX_FIT_RMSE_C or t_inf <= seg[0][1] + 3.0:
                continue
            i_med = median(sample["vehicle_current_a"] for sample in prefix)
            tau_est = median([tau_s / 60.0] + [fit["tau_min"] for fit in fits])
            ambient = _ambient_before(db, seg_start)
            ambient_source = "pre_idle" if ambient is not None else None
            if ambient is None:
                # Hot-handle start (stop/resume, post-derate): read ambient
                # from the previous charge's cool-down tail instead, using
                # this install's fitted tau (this segment's own plus any
                # earlier fits this pass).
                ambient = _ambient_from_cooldown(db, seg_start, tau_est)
                if ambient is not None:
                    ambient_source = "cooldown_tail"
            ambient_end = None
            if ambient is not None and seg_end - seg_start > 0:
                ambient_end = _ambient_after(db, seg_end, tau_est)
            if ambient_end is not None:
                # Bracketed: de-trend the samples against the linear ambient
                # ramp across the load window and refit. With ambient
                # a(t) = a0 + r*t the lag ODE solves to the constant-ambient
                # exponential plus the ramp, its asymptote shifted by -r*tau
                # — so the corrected rise adds r*tau back after the refit.
                rate = (ambient_end - ambient) / (seg_end - seg_start)
                detrended = [
                    (ts, temp - rate * (ts - seg[0][0])) for ts, temp in seg
                ]
                refit = _fit_exponential(detrended)
                if refit is not None and refit[2] <= MAX_FIT_RMSE_C:
                    tau_s, t_inf, rmse = refit
                    t_inf += rate * tau_s
                    # Ambient at the fit's own t0, not the coarse window start
                    # (the steady prefix can begin a ramp-up later).
                    ambient += rate * (seg[0][0] - seg_start)
                else:
                    ambient_end = None  # refit failed gates; fall back
            rise = None
            if ambient is not None:
                rise = (t_inf - ambient) * (REF_CURRENT_A / i_med) ** 2
                if not (RISE_RANGE_C[0] <= rise <= RISE_RANGE_C[1]):
                    rise = None
            fits.append(
                {
                    "session_id": sess["id"],
                    "start_ts": seg[0][0],
                    "tau_min": round(tau_s / 60.0, 2),
                    "rise_ref_c": round(rise, 2) if rise is not None else None,
                    "rmse_c": round(rmse, 3),
                    "current_a": round(i_med, 1),
                    "ambient_source": ambient_source if rise is not None else None,
                    "ambient_c": round(ambient, 2) if rise is not None else None,
                    "ambient_end_c": (
                        round(ambient_end, 2)
                        if rise is not None and ambient_end is not None
                        else None
                    ),
                    "ambient_drift_c": (
                        round(ambient_end - ambient, 2)
                        if rise is not None and ambient_end is not None
                        else None
                    ),
                }
            )
    fits.sort(key=lambda fit: fit["start_ts"])
    return fits


def fit_history(db: Database, now: float, lookback_days: float = 120.0,
                fits: list[dict] | None = None) -> ThermalParams:
    """Aggregate per-session fits into model parameters; defaults where thin."""
    if fits is None:
        fits = fit_sessions(db, now, lookback_days)
    taus = [fit["tau_min"] for fit in fits]
    rises = [fit["rise_ref_c"] for fit in fits if fit["rise_ref_c"] is not None]
    rmses = [fit["rmse_c"] for fit in fits]
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

# Actionable warning: the live forecast puts the 65 C trip inside this
# horizon, so the user still has time to lower the vehicle's charge current
# and keep a sustained rate instead of eating the 50% foldback.
DERATE_WARN_MIN = 15.0
DERATE_ALERT = "Derate predicted (lower vehicle charge current to avoid it)"


def detect_drift(fits: list[dict]) -> dict | None:
    """Compare the last few sessions' fitted rise against the baseline.

    Returns None while there is too little history to judge; otherwise a
    verdict dict with the medians compared. Only rise (not tau) is watched:
    added contact resistance changes how much heat is made, not how fast the
    handle mass warms.

    Only sessions charging near the install's typical current are compared.
    rise_ref_c is normalized by (REF/I)^2, and far from the measured current
    that normalization amplifies ordinary fit error — a session at 40 A with
    an unremarkable raw rise extrapolates to an alarming number at 48 A, and
    with only DRIFT_RECENT_N recent sessions a single such point can swing
    the median past the threshold and manufacture a drift verdict.

    "Typical" is the median current of the newest fits, not of all history:
    when the user caps the vehicle at a new current, the watch follows the
    new operating point. Until enough same-current history accumulates on
    both sides of the comparison it returns None — the honest "can't judge
    yet" that clears a stale alert — rather than judging new charges against
    a frozen verdict from a current the install no longer uses.
    """
    usable = [fit for fit in fits if fit["rise_ref_c"] is not None]
    if len(usable) < DRIFT_RECENT_N + DRIFT_MIN_BASELINE_N:
        return None
    usable.sort(key=lambda fit: fit["start_ts"])
    typical_a = median(fit["current_a"] for fit in usable[-DRIFT_RECENT_N:])
    band = max(2.0, 0.1 * typical_a)
    comparable = [fit for fit in usable if abs(fit["current_a"] - typical_a) <= band]
    rises = [(fit["start_ts"], fit["rise_ref_c"]) for fit in comparable]
    rises.sort(key=lambda entry: entry[0])
    if len(rises) < DRIFT_RECENT_N + DRIFT_MIN_BASELINE_N:
        return None
    recent = [rise for _, rise in rises[-DRIFT_RECENT_N:]]
    baseline = [rise for _, rise in rises[:-DRIFT_RECENT_N]]
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
        "typical_current_a": round(typical_a, 1),
        "off_current_n": len(usable) - len(comparable),
        "threshold_c": DRIFT_WARN_C,
    }


def _minutes_to_trip(t_now: float, t_inf: float, tau_min: float) -> float | None:
    """Minutes until the handle reaches the trip point, or None if it never will.

    The steady-state check comes first: a handle currently at/above the trip
    point but settling below it (cooling after a current cut or a derate) is
    recovering, not tripping.
    """
    if t_inf <= TRIP_HANDLE_C + 0.2:
        return None
    if t_now >= TRIP_HANDLE_C:
        return 0.0
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


def _project_t_inf(window: list[tuple[float, float]], tau_min: float) -> float:
    """Steady state projected from a steady-current window's trajectory.

    With tau known, T(t) = T_inf - C*exp(-t/tau) is linear in (T_inf, C), so
    an ordinary least-squares line on x = exp(-t/tau) gives an unbiased T_inf
    (a straight-line slope would read the window's average rate and overshoot
    during a fast ramp). No ambient input needed. A flat window (variance ~0,
    i.e. already converged) reads as the latest temperature.
    """
    count = len(window)
    decays = [math.exp(-(ts - window[0][0]) / (tau_min * 60.0)) for ts, _ in window]
    mean_decay = sum(decays) / count
    mean_temp = sum(temp for _, temp in window) / count
    var = sum((decay - mean_decay) ** 2 for decay in decays)
    cov = sum(
        (decay - mean_decay) * (temp - mean_temp)
        for decay, (_, temp) in zip(decays, window)
    )
    return mean_temp - (cov / var) * mean_decay if var > 1e-9 else window[-1][1]


def _recent_steady_ambient(recent: list[dict], params: ThermalParams) -> float | None:
    """Ambient inferred from the newest steady-current run in the buffer.

    Back-to-back sessions leave no idle gap to read ambient from, and a
    mid-session current change resets the live trajectory window — but the
    buffer usually still holds an earlier steady run (this session's stretch
    before the change, or the previous session's tail). Its projected steady
    state at that run's current implies the ambient, which the I^2 model then
    rescales to the present current.
    """
    runs: list[list[dict]] = [[]]
    for sample in recent:
        current = sample.get("vehicle_current_a") or 0.0
        run = runs[-1]
        if not sample.get("contactor_closed") or current < 6.0:
            if run:
                runs.append([])
            continue
        ref = run[0]["vehicle_current_a"] if run else current
        if abs(current - ref) > max(2.0, 0.1 * ref):
            runs.append([sample])
            continue
        run.append(sample)
    for run in reversed(runs):
        if len(run) < TRAJECTORY_MIN_SAMPLES or run[-1]["ts"] - run[0]["ts"] < TRAJECTORY_MIN_SPAN_S:
            continue
        window = [(sample["ts"], sample["handle_temp_c"]) for sample in run]
        t_inf = _project_t_inf(window, params.tau_min)
        run_current = median(sample["vehicle_current_a"] for sample in run)
        ambient = t_inf - params.rise_ref_c * (run_current / REF_CURRENT_A) ** 2
        if -30.0 <= ambient <= TRIP_HANDLE_C:
            return ambient
    return None


def predict(db: Database, now: float, params: ThermalParams) -> dict:
    """Forecast alert-40 for the current state (live session or idle)."""
    out: dict = {"model": params.as_dict(), "state": "no_data", "forecast": None}
    recent = [
        row for row in db.vitals_range(now - 900, now, 2000) if row.get("handle_temp_c") is not None
    ]
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
        # Why the window ended matters when the forecast comes up empty:
        # breaking on an out-of-band sample means the current just changed
        # mid-session, not that charging began moments ago.
        gap_reason = "warming_up"
        for sample in reversed(recent):
            if not sample.get("contactor_closed") or last["ts"] - sample["ts"] > 360:
                break
            if abs((sample.get("vehicle_current_a") or 0) - current) > band:
                gap_reason = "current_changed"
                break
            window.append((sample["ts"], sample["handle_temp_c"]))
        window.reverse()
        forecast: dict = {}
        if len(window) >= TRAJECTORY_MIN_SAMPLES and window[-1][0] - window[0][0] >= TRAJECTORY_MIN_SPAN_S:
            t_inf = _project_t_inf(window, tau_min)
            forecast["basis"] = "trajectory"
        else:
            # Too early at this current for a slope: model from ambient and
            # the present current scaled by I^2. Ambient comes from the idle
            # stretch before the session, or — when sessions run back-to-back
            # and there was none — from the newest steady run in the buffer.
            sid = last.get("session_id")
            sess = db.session(int(sid)) if sid else None
            ambient = _ambient_before(db, sess["start_ts"]) if sess else None
            source = "pre_session"
            if ambient is None:
                ambient = _recent_steady_ambient(recent, params)
                source = "recent_trajectory"
            if ambient is None:
                # A session's opening ramp also breaks the band; within the
                # first minutes "just started" is the truthful story even so.
                if sess and last["ts"] - sess["start_ts"] < 180:
                    gap_reason = "warming_up"
                out["forecast"] = {"basis": "insufficient", "will_trip": None, "reason": gap_reason}
                return out
            t_inf = ambient + params.rise_ref_c * (current / REF_CURRENT_A) ** 2
            forecast["basis"] = "model"
            forecast["ambient_source"] = source
        # No flooring of t_inf at the current temperature: a steady state
        # below the handle is real, not noise — it's what cooling toward a
        # lower equilibrium looks like after a current cut or a derate.
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
        temps = [row["handle_temp_c"] for row in recent if last["ts"] - row["ts"] <= 900]
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
