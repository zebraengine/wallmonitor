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

and at idle the handle settles a small offset above ambient, so the charger
doubles as its own ambient thermometer. The offset is not constant: measured
against a week of LAN ambient-sensor overlap it shrinks as the garage warms
(~2.3 C on cool nights, ~0.7 C on hot afternoons; see idle_offset_c), so the
proxy models it as linear in ambient, clamped to the calibrated range. tau
and rise_ref are fitted per install from recorded sessions; the defaults
come from a verified alert-40 event where the fit reproduced the observed
time-to-trip within 1%.

The unit of thermal analysis is the load window — the stretch where current
actually flows — not the plug-in session. Ambient is read at both ends of
the window (flat idle or cool-down tail before; the charge's own cool-down
tail after) and interpolated across it, so weather moving during a charge
is measured and removed instead of leaking into the fitted rise.
"""

from __future__ import annotations

import math
import json
from dataclasses import dataclass
from statistics import median

from .db import Database

TRIP_HANDLE_C = 65.0  # alert 40 raises here (observed, firmware 26.18.0)
CLEAR_HANDLE_C = 60.0  # ...and clears here, but the 50% derate persists
# Idle handle offset above garage air. Calibrated against the LAN ambient
# sensor (source "ecowitt", 2026-08-04..11): 42 settled-idle segments across
# 8 days — samples >= 1 h after any charging, ambient quasi-static, segment
# means used so autocorrelation can't fake precision. The old constant 2.0
# was rejected (mean 1.45, 95% CI [1.13, 1.78]); the offset falls as the
# garage warms — night ~2.3 C vs hot afternoon ~0.7 C, slope -0.124 C/C
# (t = -4.0 under leave-one-day-out jackknife) — so it is modeled linear in
# ambient and clamped to the ambient range the calibration actually covered.
# Recalibrate with contrib/calibrate_idle_offset.py as seasons extend the
# covered range.
#
# Changing these constants reinterprets history: fits are recomputed from
# raw vitals on every read, so adopting a new offset model shifts the
# rise_ref of every fit whose ambient came through the proxy
# (ambient_source "pre_idle" or "cooldown_tail") while leaving
# measured-sourced fits untouched. That shift is the correction working —
# but a drift comparison spanning a recalibration will show it as a step
# in proxy-era fits. Compare rise_ref within one ambient_source tier, or
# re-anchor the verified baseline after recalibrating, before reading any
# movement as hardware degradation.
IDLE_OFFSET_REF_C = 1.4  # offset at IDLE_OFFSET_AMBIENT_REF_C
IDLE_OFFSET_SLOPE = -0.124  # d(offset)/d(ambient)
IDLE_OFFSET_AMBIENT_REF_C = 30.0
IDLE_OFFSET_AMBIENT_RANGE_C = (23.0, 38.5)  # calibration coverage; clamp outside

# These constants are the *seed*, not the calibration: when a stationary
# ambient sensor gives this install ground truth, calibration.maybe_adopt
# refits the same linear model from its own settled-idle history and
# stores it under this settings key; every proxy read then goes through
# the install's model. Without a sensor the seed stands, labelled as such.
IDLE_OFFSET_SETTING = "idle_offset_model"
# Proxy ambient uncertainty when no calibration exists: the built-in fit's
# own segment scatter was ~0.5 C on its install; a different garage can be
# off by a constant this large without any way to know.
IDLE_OFFSET_UNCALIBRATED_SE_C = 1.5


@dataclass(frozen=True)
class IdleOffset:
    """The idle-offset model: offset = ref_c + slope * (ambient - ambient_ref_c),
    linear inside ambient_range_c and held at the boundary value outside it
    (continuous at both edges), so out-of-range readings degrade to a
    constant-offset proxy instead of extrapolating the slope."""

    ref_c: float = IDLE_OFFSET_REF_C
    slope: float = IDLE_OFFSET_SLOPE
    ambient_ref_c: float = IDLE_OFFSET_AMBIENT_REF_C
    ambient_range_c: tuple[float, float] = IDLE_OFFSET_AMBIENT_RANGE_C
    source: str = "built-in"
    segments: int = 0
    days: int = 0
    residual_sd_c: float | None = None
    calibrated_ts: float | None = None

    def offset_c(self, ambient_c: float) -> float:
        lo, hi = self.ambient_range_c
        clamped = min(max(ambient_c, lo), hi)
        return self.ref_c + self.slope * (clamped - self.ambient_ref_c)

    def handle_c(self, ambient_c: float) -> float:
        return ambient_c + self.offset_c(ambient_c)

    def ambient_from_handle(self, handle_c: float) -> float:
        ta = (handle_c - self.ref_c + self.slope * self.ambient_ref_c) / (1.0 + self.slope)
        lo, hi = self.ambient_range_c
        if ta < lo:
            return handle_c - self.offset_c(lo)
        if ta > hi:
            return handle_c - self.offset_c(hi)
        return ta

    @property
    def ambient_se_c(self) -> float:
        """How far a proxy ambient read may sit from the truth (1 sigma):
        the calibration's own segment scatter, or the uncalibrated default."""
        if self.source == "calibrated" and self.residual_sd_c is not None:
            return max(self.residual_sd_c, 0.2)
        return IDLE_OFFSET_UNCALIBRATED_SE_C

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "ref_c": round(self.ref_c, 3),
            "slope": round(self.slope, 4),
            "ambient_ref_c": self.ambient_ref_c,
            "ambient_range_c": [self.ambient_range_c[0], self.ambient_range_c[1]],
            "segments": self.segments,
            "days": self.days,
            "residual_sd_c": round(self.residual_sd_c, 3) if self.residual_sd_c is not None else None,
            "ambient_se_c": round(self.ambient_se_c, 2),
            "calibrated_ts": self.calibrated_ts,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "IdleOffset":
        lo, hi = data["ambient_range_c"]
        return cls(
            ref_c=float(data["ref_c"]),
            slope=float(data["slope"]),
            ambient_ref_c=float(data["ambient_ref_c"]),
            ambient_range_c=(float(lo), float(hi)),
            source=str(data.get("source", "calibrated")),
            segments=int(data.get("segments", 0)),
            days=int(data.get("days", 0)),
            residual_sd_c=data.get("residual_sd_c"),
            calibrated_ts=data.get("calibrated_ts"),
        )


BUILTIN_IDLE_OFFSET = IdleOffset()


def load_idle_offset(db: Database) -> IdleOffset:
    """This install's idle-offset model: the calibrated one when a sane one
    is stored, else the built-in seed. Never raises — a corrupt setting
    falls back to the seed."""
    raw = db.get_setting(IDLE_OFFSET_SETTING)
    if not raw:
        return BUILTIN_IDLE_OFFSET
    try:
        model = IdleOffset.from_dict(json.loads(raw))
    except (ValueError, KeyError, TypeError):
        return BUILTIN_IDLE_OFFSET
    if not (-0.95 < model.slope < 0.95) or model.ambient_range_c[0] >= model.ambient_range_c[1]:
        return BUILTIN_IDLE_OFFSET
    return model


def idle_offset_c(ambient_c: float, model: IdleOffset = BUILTIN_IDLE_OFFSET) -> float:
    """How far above garage air the idle handle settles, at this ambient."""
    return model.offset_c(ambient_c)


def idle_handle_c(ambient_c: float, model: IdleOffset = BUILTIN_IDLE_OFFSET) -> float:
    """Idle handle temperature expected at a given garage air temperature."""
    return model.handle_c(ambient_c)


def ambient_from_idle_handle(handle_c: float, model: IdleOffset = BUILTIN_IDLE_OFFSET) -> float:
    """Garage air implied by a settled idle handle: inverse of idle_handle_c."""
    return model.ambient_from_handle(handle_c)
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
# The steady-current window must be long against the install's time
# constant, or rise and tau are not separately identifiable: a window that
# ends before the plateau shows lets the fitter trade a lower rise for a
# faster tau and pass every other gate with a fine RMSE. Judged against the
# install's median tau (not this fit's own, which is exactly the biased
# quantity), so a truncated segment can't vouch for itself — and floored at
# DEFAULT_TAU_MIN, so on a fresh install the very first fit is judged
# against a sane prior rather than its own possibly-truncated tau. At 1.8
# tau the handle has covered ~83% of its rise. The steady-prefix window
# scales with the same tau estimate (PREFIX_SPAN_TAU) so a slow-tau install
# is not starved of fits by a fixed cap it can never clear.
MIN_SPAN_TAU = 1.8
PREFIX_SPAN_TAU = 2.5
PREFIX_SPAN_MIN_S = 1800.0

# The steady-prefix band (10% of the reference current) is wide enough to
# hide a substantial current reduction: 48.6 A trimmed to 44.7 A never
# leaves it, so the ramp keeps collecting samples whose flattening is
# *caused by the current dropping*. The exponential then reads that as the
# plateau — a lower rise paired with a faster tau, passing every gate with a
# fine RMSE. Measured on one install: windows whose current sagged read a
# median 33.3 C rise against 37.2 C for the same charger's steady windows.
#
# Who moves the current matters less than that it moved, and on that install
# it was mostly *us*: the optional amp controller (contrib/) caps on this
# model's own forecast, 285 times in a month, and the vehicle tapers on its
# own besides. The charger's internal foldback — the one alert 40 raises,
# counted by lifetime `thermal_foldbacks` — had not fired once in the same
# period. So this is first of all a feedback loop: forecast caps the
# current, the cap contaminates the fit, the fit feeds the forecast. Because
# the controller caps sooner in a hot garage, the contamination also tracks
# ambient, which is what turned it into a 7 C "drift" verdict.
#
# So each fit records whether its window was *free-running*: current held
# flat end to end, making the fitted plateau the connector's own equilibrium
# rather than one something else imposed. Only free-running fits are
# compared by the degradation watch. The other half of "the plateau was
# real" — whether the window ran long enough to observe it — is MIN_SPAN_TAU
# above, already enforced before any fit is emitted.
#
# The threshold separates the two populations with room to spare: on that
# install steady windows sagged <= 0.6% while regulated ones sagged >= 3.9%.
# The absolute floor keeps sensor quantization on a low-current charge from
# reading as regulation.
FREE_CURRENT_SAG_FRAC = 0.015
FREE_CURRENT_SAG_MIN_A = 0.5

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
    """The fitted model, portable: tau (minutes), rise at the 48 A reference
    current, how many segment fits back each number, and the median
    per-segment RMSE (the noise floor the amp controller's confidence guard
    measures against). Defaults are from the telemetry-verified alert-40
    event and apply until this install has fits of its own."""

    tau_min: float = DEFAULT_TAU_MIN
    rise_ref_c: float = DEFAULT_RISE_REF_C
    tau_fits: int = 0
    rise_fits: int = 0
    fit_rmse_c: float | None = None

    @property
    def fitted(self) -> bool:
        return self.tau_fits > 0 and self.rise_fits > 0

    # How far a fitted value may sit from the default before the dashboard
    # says the priors were a poor fit for this install. A heuristic, not a
    # statistic: 30% is roughly where the default-driven forecast's plateau
    # error exceeds the fit's own noise and early-session predictions were
    # materially off.
    PRIOR_DEVIATION_FRAC = 0.30

    def prior_deviation(self) -> dict | None:
        """How this install's fitted tau and rise compare to the defaults
        that governed the forecast before its first fit landed — None until
        fitted. The frontend renders it as an honesty note: the priors are
        from one verified install, and a user whose charger differs should
        know that early forecasts were rough and (for a fast tau) that short
        charges no longer teach the model."""
        if not self.fitted:
            return None
        tau_frac = self.tau_min / DEFAULT_TAU_MIN - 1.0
        rise_frac = self.rise_ref_c / DEFAULT_RISE_REF_C - 1.0
        return {
            "default_tau_min": DEFAULT_TAU_MIN,
            "default_rise_ref_c": DEFAULT_RISE_REF_C,
            "tau_frac": round(tau_frac, 3),
            "rise_frac": round(rise_frac, 3),
            "notable": max(abs(tau_frac), abs(rise_frac)) > self.PRIOR_DEVIATION_FRAC,
        }

    def as_dict(self) -> dict:
        """The `model` object served by /api/thermal and the SSE thermal
        frame — fitted values plus the fixed thresholds."""
        return {
            "tau_min": round(self.tau_min, 2),
            "rise_ref_c": round(self.rise_ref_c, 1),
            "ref_current_a": REF_CURRENT_A,
            "trip_c": TRIP_HANDLE_C,
            "clear_c": CLEAR_HANDLE_C,
            "idle_offset_ref_c": IDLE_OFFSET_REF_C,
            "idle_offset_slope": IDLE_OFFSET_SLOPE,
            "tau_fits": self.tau_fits,
            "rise_fits": self.rise_fits,
            "fit_rmse_c": round(self.fit_rmse_c, 3) if self.fit_rmse_c is not None else None,
            "fitted": self.fitted,
            "prior_deviation": self.prior_deviation(),
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


def _steady_current_prefix(samples: list[dict], max_span_s: float = PREFIX_SPAN_MIN_S) -> list[dict]:
    """The session's first steady-current run, capped at max_span_s.

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
        if sample["ts"] - prefix[0]["ts"] > max_span_s:  # the ramp lives in the first few tau
            break
    return prefix


def _current_sag_a(prefix: list[dict]) -> float:
    """How far the window's current fell from its opening to its close.

    Head and tail quarters are compared by median, so a single dropped
    sample or a momentary blip cannot pass for regulation. Signed: a
    negative sag means the current *rose* across the window, which breaks
    the constant-current premise just as thoroughly.
    """
    quarter = max(2, len(prefix) // 4)
    head = median(sample["vehicle_current_a"] for sample in prefix[:quarter])
    tail = median(sample["vehicle_current_a"] for sample in prefix[-quarter:])
    return head - tail


def _free_plateau(prefix: list[dict], sag_a: float) -> bool:
    """Did the charger leave this window alone?

    True when the current held flat across the whole window, so the fitted
    plateau is the connector's own equilibrium at that current. False when
    the charger was trimming current back — the plateau is then a setpoint
    the charger held, and the fitted rise says more about how close the
    handle got to the limit than about connector resistance.
    """
    i_ref = median(sample["vehicle_current_a"] for sample in prefix)
    tolerance = max(FREE_CURRENT_SAG_MIN_A, FREE_CURRENT_SAG_FRAC * i_ref)
    return abs(sag_a) <= tolerance


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


# Measured ambient (an optional LAN sensor POSTing to /api/ambient) beats
# every handle-derived estimate: the handle proxy carries the idle-offset
# assumption and goes blind when the handle is warm, while a sensor reads
# the garage air directly at any moment. All reads fall back to the proxy
# when no samples cover the window, so the sensor can appear, disappear, or
# never exist without configuration.
#
# Samples tagged source "car" come from a vehicle parked in the garage — a
# real thermometer, but one that drives away and reads high for a while
# after a drive (heat-soaked housing). A stationary sensor therefore
# outranks it whenever both report; car samples fill in only when they are
# all there is. The readers return (temp_c, tag) so callers can say which
# kind of measurement they used ("measured" vs "measured_car").
MEASURED_AMBIENT_WINDOW_S = 900.0
MEASURED_AMBIENT_FRESH_S = 600.0
CAR_AMBIENT_SOURCE = "car"


def _split_by_mobility(rows: list[dict]) -> tuple[list[dict], str]:
    """Apply the stationary-beats-car tier (block comment above): drop car
    rows whenever any fixed-sensor row exists in the set."""
    fixed = [row for row in rows if row.get("source") != CAR_AMBIENT_SOURCE]
    return (fixed, "measured") if fixed else (rows, "measured_car")


def _measured_ambient(db: Database, t_from: float, t_to: float) -> tuple[float, str] | None:
    """Median measured ambient over a window (median, so one glitchy sample
    or a warm car pulling in can't drag it far), or None if uncovered."""
    rows = db.ambient_range(t_from, t_to, 500)
    if not rows:
        return None
    rows, tag = _split_by_mobility(rows)
    return median(row["temp_c"] for row in rows), tag


def _latest_measured_ambient(db: Database, now: float) -> tuple[float, str] | None:
    """Newest measured sample no older than MEASURED_AMBIENT_FRESH_S, or
    None — the live-forecast reader, where staleness matters more than
    smoothing."""
    rows = db.ambient_range(now - MEASURED_AMBIENT_FRESH_S, now + 1.0, 500)
    if not rows:
        return None
    rows, tag = _split_by_mobility(rows)
    return rows[-1]["temp_c"], tag


def _ambient_before(db: Database, start_ts: float, model: IdleOffset = BUILTIN_IDLE_OFFSET) -> float | None:
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
    return ambient_from_idle_handle(median(idle), model)


# Cool-down-tail ambient: gates for reading ambient from a still-warm
# handle's decay when no flat idle window exists before a segment.
COOLDOWN_MIN_SAMPLES = 10
COOLDOWN_MIN_SPAN_S = 300.0
COOLDOWN_MIN_DROP_C = 1.0
COOLDOWN_MAX_RMSE_C = 0.5


def _decay_asymptote(tail: list[dict], tau_min: float, model: IdleOffset = BUILTIN_IDLE_OFFSET) -> float | None:
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
    ambient = ambient_from_idle_handle(asymptote, model)
    if not (-30.0 <= ambient <= TRIP_HANDLE_C):
        return None
    return ambient


def _idle_rows(rows: list[dict]) -> list[dict]:
    """Rows where the handle should sit at idle_handle_c(ambient):
    contactor open, no meaningful current, sensor sane (255 sentinel
    excluded by the < 200 guard)."""
    return [
        row
        for row in rows
        if not row.get("contactor_closed")
        and (row.get("vehicle_current_a") or 0) < 1
        and row.get("handle_temp_c") is not None
        and row["handle_temp_c"] < 200
    ]


def _ambient_from_cooldown(db: Database, start_ts: float, tau_min: float,
                          model: IdleOffset = BUILTIN_IDLE_OFFSET) -> float | None:
    """Ambient from the cool-down tail before a segment that starts warm.

    A stop/resume or post-derate segment begins before the handle has
    cooled, so _ambient_before finds no stable idle window and the segment
    loses its rise fit. But the decay itself encodes ambient: idle cooling
    follows the same first-order lag as the charge ramp, settling at
    the idle handle temperature. With tau known from this install's fitted
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
    return _decay_asymptote(tail, tau_min, model)


def _ambient_after(db: Database, end_ts: float, tau_min: float,
                   model: IdleOffset = BUILTIN_IDLE_OFFSET) -> float | None:
    """Ambient at the end of a load window, from the cool-down that follows.

    The moment current stops, the handle decays from its working temperature
    toward idle_handle_c(ambient) along the same first-order lag as the
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
    return _decay_asymptote(tail, tau_min, model)


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
    ambient_source ("measured" for a stationary LAN sensor, "measured_car"
    for a parked vehicle's sensor, else "pre_idle" or "cooldown_tail"),
    ambient_c, and — when bracketed — ambient_end_c and ambient_drift_c
    (end minus start).

    Every fit also carries current_sag_a (how far current fell across the
    window) and free_plateau: False means the charger was trimming current
    back as the handle warmed, so the fitted plateau is a setpoint it held
    rather than the connector's own equilibrium. Such fits still serve the
    forecast — they describe what the handle actually did — but the
    degradation watch compares only free-running ones."""
    sessions = [
        session
        for session in db.sessions_range(now - lookback_days * 86400, now)
        if session.get("end_ts") and (session.get("charging_s") or 0) >= MIN_SEGMENT_S
    ][:40]
    fits: list[dict] = []
    idle_model = load_idle_offset(db)
    for sess in sessions:
        # Coarse pass over the whole session to locate charging segments —
        # bucket-averaged is fine here (and keeps a multi-day session cheap);
        # each segment then gets a narrow raw-resolution fetch, since only
        # the ramp's first ~45 min carries the thermal signal.
        coarse = db.vitals_range(sess["start_ts"] - 1, sess["end_ts"] + 1, 2000)
        segments = _segments(coarse)[:MAX_SEGMENTS_PER_SESSION]
        for idx, (seg_start, seg_end) in enumerate(segments):
            next_start = segments[idx + 1][0] if idx + 1 < len(segments) else sess["end_ts"] + 1
            # The install's tau so far (the default until fits exist) sizes
            # the window: a fixed cap that suits an 11 min tau would leave
            # a 20 min tau install unable to clear the identifiability gate.
            tau_prior = median(fit["tau_min"] for fit in fits) if fits else DEFAULT_TAU_MIN
            fit = None
            for _pass in range(2):
                fit = None
                span_cap_s = max(PREFIX_SPAN_MIN_S, PREFIX_SPAN_TAU * tau_prior * 60.0)
                t_hi = min(sess["end_ts"], seg_start + span_cap_s + 900, next_start - 1)
                samples = db.vitals_range(seg_start - 1, t_hi + 1, 5000)
                prefix = _steady_current_prefix(samples, span_cap_s)
                seg = [
                    (sample["ts"], sample["handle_temp_c"])
                    for sample in prefix
                    if sample.get("handle_temp_c") is not None
                ]
                if len(seg) < MIN_SEGMENT_SAMPLES or seg[-1][0] - seg[0][0] < MIN_SEGMENT_S:
                    break
                if max(temp for _, temp in seg) - seg[0][1] < MIN_RISE_SEEN_C:
                    break
                fit = _fit_exponential(seg)
                if fit is None:
                    break
                # The window was sized from the tau prior. If this segment
                # fits slower than that, the prior under-sized it: widen to
                # the fitted tau and refit once, so a slow-tau install (or
                # the first fit on a fresh one) isn't stuck behind a window
                # it can never clear.
                slower = fit[0] / 60.0 > tau_prior * 1.1
                if not slower or prefix[-1]["ts"] - prefix[0]["ts"] < span_cap_s - 60:
                    break  # window is tau-sized, or the run ended on its own
                tau_prior = fit[0] / 60.0
            if fit is None or len(seg) < MIN_SEGMENT_SAMPLES:
                continue
            tau_s, t_inf, rmse = fit
            if rmse > MAX_FIT_RMSE_C or t_inf <= seg[0][1] + 3.0:
                continue
            i_med = median(sample["vehicle_current_a"] for sample in prefix)
            tau_est = median([tau_s / 60.0] + [fit["tau_min"] for fit in fits])
            if seg[-1][0] - seg[0][0] < MIN_SPAN_TAU * max(tau_est, DEFAULT_TAU_MIN) * 60.0:
                continue  # plateau never observed: rise/tau not identifiable
            measured = _measured_ambient(db, seg_start - MEASURED_AMBIENT_WINDOW_S, seg_start + 60)
            ambient, ambient_source = measured if measured is not None else (None, None)
            if ambient is None:
                ambient = _ambient_before(db, seg_start, idle_model)
                ambient_source = "pre_idle" if ambient is not None else None
            if ambient is None:
                # Hot-handle start (stop/resume, post-derate): read ambient
                # from the previous charge's cool-down tail instead, using
                # this install's fitted tau (this segment's own plus any
                # earlier fits this pass).
                ambient = _ambient_from_cooldown(db, seg_start, tau_est, idle_model)
                if ambient is not None:
                    ambient_source = "cooldown_tail"
            ambient_end = None
            if ambient is not None and seg_end - seg_start > 0:
                measured_end = _measured_ambient(db, seg_end - 60, seg_end + MEASURED_AMBIENT_WINDOW_S)
                ambient_end = measured_end[0] if measured_end is not None else None
                if ambient_end is None:
                    ambient_end = _ambient_after(db, seg_end, tau_est, idle_model)
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
            sag_a = _current_sag_a(prefix)
            fits.append(
                {
                    "session_id": sess["id"],
                    "start_ts": seg[0][0],
                    "tau_min": round(tau_s / 60.0, 2),
                    "rise_ref_c": round(rise, 2) if rise is not None else None,
                    "rmse_c": round(rmse, 3),
                    "current_a": round(i_med, 1),
                    "current_sag_a": round(sag_a, 2),
                    "free_plateau": _free_plateau(prefix, sag_a),
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
# just follows it), so the drift watch models rise against time and flags a
# sustained increase.
#
# It models rather than compares, because a recent-vs-baseline median split
# answers the wrong question. The split asks "are the last few fits higher?",
# which any covariate that moved with the calendar answers for it: a garage
# that cooled between the two halves, or a vehicle capped to a lower current
# whose (48/I)^2 normalization then lifts every recent fit. On one install
# that split reported +7.2 C with a 95% CI of [5.4, 9.1] — "statistically
# confirmed" — from a connector whose rise, regressed on time with ambient
# and current held, was moving +0.01 C/day, indistinguishable from flat. The
# confidence was real; it was confidence in the wrong estimand.
#
# So the watch fits rise_ref ~ days + ambient + current over the whole
# comparable history and reads the *days* coefficient. Ambient and current
# stop being confounders and become covariates, the estimate uses every fit
# instead of six, and when a covariate genuinely cannot be separated from
# time the collinearity inflates the slope's standard error and the verdict
# declines to confirm — which is the honest outcome, reached automatically.
DRIFT_MIN_N = 6
# How many of the newest fits the comparison has to reach into to count as
# describing the install as it charges now, rather than as it used to.
DRIFT_RECENCY_N = 3
DRIFT_WARN_C = 2.5  # materiality floor, not the trigger — see detect_drift
DRIFT_ALERT = "Handle heat rise increasing (check connector/wiring)"

# Cross-current pooling: fits whose ambient was bracketed at both ends are
# trustworthy enough under the I^2 normalization to join the comparison from
# a wider current band; start-only fits must still match the typical current.
# The regression carries a current term of its own, so pooled fits are
# adjusted rather than merely admitted — a residual error in the I^2
# normalization lands on that coefficient instead of on the time slope.
#
# The band is wide because the reason it was narrow is gone. It guarded a
# median against a single off-current fit swinging it; there is no median
# any more, every fit that gets here already held its current steady, and
# the current term adjusts what is left. It has to be this wide to admit a
# *calibration probe* — a charge deliberately held well under the operating
# current so the handle reaches a plateau nothing trimmed (see
# contrib/derate_amp_control.py --probe-amps). Those are the most valuable
# fits an install can produce, and a narrow band silently discarded them.
DRIFT_POOL_BAND_FRAC = 0.45

# A covariate earns a column only when the history actually moved in it.
# Regressing on a covariate that barely varies buys nothing and spends a
# degree of freedom the small-sample t-multiplier charges dearly for.
DRIFT_AMBIENT_SPREAD_C = 3.0
DRIFT_CURRENT_SPREAD_A = 2.0

# Reported, not gated: how strongly a covariate moved with the calendar.
# Past this the two cannot be told apart, and the slope's standard error
# will already be showing it — the flag exists so the UI can say *why* an
# apparently large delta refused to confirm.
DRIFT_COLLINEAR_R = 0.8

# The premise of rise_ref is that subtracting ambient leaves a number that
# depends on the connector and not on the weather. An install where rise
# still moves this much per degree of ambient — materially, and resolved
# well enough to be sure of the sign — has broken that premise: something
# the model does not carry (multi-day heat soak, a charger regulating to a
# fixed handle temperature, a badly sited sensor) is in the measurement.
# The regression adjusts for it, but adjustment is not understanding, so
# such an install can still raise a lead and never an alert.
DRIFT_AMBIENT_CONFOUND_C = 0.3

# The settings key holding the baseline anchor: a timestamp before which
# fits are excluded from the drift comparison. Set it when the hardware has
# been inspected and verified (or fixed) — from then on "baseline" means
# "verified healthy", not "the first charges the monitor happened to see".
BASELINE_ANCHOR_KEY = "thermal_baseline_anchor_ts"

# Two-sided 95% Student-t multipliers by residual degrees of freedom, for
# the slope's confidence interval. A plain 1.96 would overstate the
# confidence exactly when the history is thinnest.
_T95 = {1: 12.71, 2: 4.30, 3: 3.18, 4: 2.78, 5: 2.57, 6: 2.45, 7: 2.36, 8: 2.31, 9: 2.26, 10: 2.23,
        11: 2.20, 12: 2.18, 13: 2.16, 14: 2.14, 15: 2.13, 16: 2.12, 17: 2.11, 18: 2.10, 19: 2.09,
        20: 2.09, 25: 2.06, 30: 2.04, 40: 2.02, 60: 2.00}


def _t95(dof: int) -> float:
    """Two-sided 95% t multiplier. An untabulated dof falls back to the
    next-lower tabulated one, which is the larger multiplier — rounding
    toward caution rather than away from it."""
    if dof < 1:
        return _T95[1]
    if dof in _T95:
        return _T95[dof]
    return _T95[max(key for key in _T95 if key <= dof)]


def _invert(matrix: list[list[float]]) -> list[list[float]] | None:
    """Gauss-Jordan inverse with partial pivoting; None if singular.

    The pivot tolerance is relative to the largest entry, because the design
    matrix mixes columns of wildly different scale (a count of fits against
    a sum of squared day-offsets)."""
    size = len(matrix)
    scale = max((abs(value) for row in matrix for value in row), default=0.0)
    if scale <= 0.0:
        return None
    aug = [row[:] + [1.0 if i == j else 0.0 for j in range(size)] for i, row in enumerate(matrix)]
    for col in range(size):
        pivot = max(range(col, size), key=lambda row: abs(aug[row][col]))
        if abs(aug[pivot][col]) < 1e-10 * scale:
            return None
        aug[col], aug[pivot] = aug[pivot], aug[col]
        divisor = aug[col][col]
        aug[col] = [value / divisor for value in aug[col]]
        for row in range(size):
            if row != col and aug[row][col] != 0.0:
                factor = aug[row][col]
                aug[row] = [value - factor * base for value, base in zip(aug[row], aug[col])]
    return [row[size:] for row in aug]


def _ols(y: list[float], design: list[list[float]]) -> tuple[list[float], list[float], float, int] | None:
    """Ordinary least squares: (coefficients, standard errors, residual sd,
    residual dof), or None when the design is singular or leaves too few
    degrees of freedom for the standard errors to mean anything."""
    rows, cols = len(y), len(design[0])
    dof = rows - cols
    if dof < 2:
        return None
    xtx = [[sum(design[i][a] * design[i][b] for i in range(rows)) for b in range(cols)]
           for a in range(cols)]
    inv = _invert(xtx)
    if inv is None:
        return None
    xty = [sum(design[i][a] * y[i] for i in range(rows)) for a in range(cols)]
    beta = [sum(inv[a][b] * xty[b] for b in range(cols)) for a in range(cols)]
    sse = sum((y[i] - sum(design[i][j] * beta[j] for j in range(cols))) ** 2 for i in range(rows))
    variance = sse / dof
    se = [math.sqrt(max(variance * inv[j][j], 0.0)) for j in range(cols)]
    return beta, se, math.sqrt(variance), dof


def _pearson(xs: list[float], ys: list[float]) -> float:
    """Correlation coefficient; 0.0 when either side is constant."""
    x_bar, y_bar = sum(xs) / len(xs), sum(ys) / len(ys)
    sxy = sum((x - x_bar) * (y - y_bar) for x, y in zip(xs, ys))
    sxx = sum((x - x_bar) ** 2 for x in xs)
    syy = sum((y - y_bar) ** 2 for y in ys)
    return sxy / math.sqrt(sxx * syy) if sxx > 0 and syy > 0 else 0.0


# Actionable warning: the live forecast puts the 65 C trip inside this
# horizon, so the user still has time to lower the vehicle's charge current
# and keep a sustained rate instead of eating the 50% foldback.
DERATE_WARN_MIN = 15.0
DERATE_ALERT = "Derate predicted (lower vehicle charge current to avoid it)"


def detect_drift(fits: list[dict], anchor_ts: float | None = None) -> dict | None:
    """Regress fitted rise on time, holding ambient and current, and read the
    time coefficient.

    Returns None while there is too little comparable history to judge;
    otherwise a verdict dict. Only rise (not tau) is watched: added contact
    resistance changes how much heat is made, not how fast the handle mass
    warms.

    **Only free-running fits are compared.** A window whose current the
    charger was trimming back as the handle warmed has a plateau the charger
    chose, and its fitted rise moves with how close the handle got to the
    limit — which is to say, with ambient. Those fits still describe what the
    handle did, so the forecast keeps them; a degradation comparison cannot
    use them at all.

    **Only sessions near the install's recent operating current**, with
    ambient-bracketed fits pooled in from a wider band. "Typical" is the
    median current of the newest fits, not of all history: when the user caps
    the vehicle at a new current, the watch follows the new operating point.

    anchor_ts, when set, excludes fits from before it: the user has had the
    hardware inspected and verified, so "baseline" means "verified healthy"
    from that moment, not "the first charges the monitor happened to see".

    The estimate is the modelled change across the observed span —
    slope x days — not a difference of medians, so ambient and charge current
    are adjusted for rather than assumed away. Its confidence interval is the
    slope's, at a small-sample Student-t multiplier. "drifting" — the alert —
    needs the interval to clear zero ("confident"), the change to be material
    (>= DRIFT_WARN_C, the floor below which a real increase is not worth an
    inspection), and the measurement itself to be sound: an install whose
    rise still tracks ambient after the subtraction (see
    DRIFT_AMBIENT_CONFOUND_C) is measuring something other than connector
    resistance, and can raise a lead but never an alert. A change past the
    floor that fails either test is a "lead": shown, pushed quietly, no alert
    row. The effective threshold ("threshold_c") is the larger of the floor
    and what this install's own scatter requires.
    """
    usable = [
        fit for fit in fits
        if fit["rise_ref_c"] is not None
        # Fits predating the free-plateau gate carry no verdict either way;
        # nothing better to assume than that the window was steady.
        and fit.get("free_plateau", True)
        and (anchor_ts is None or fit["start_ts"] >= anchor_ts)
    ]
    regulated_n = sum(
        1 for fit in fits
        if fit["rise_ref_c"] is not None
        and not fit.get("free_plateau", True)
        and (anchor_ts is None or fit["start_ts"] >= anchor_ts)
    )
    if len(usable) < DRIFT_MIN_N:
        return None
    usable.sort(key=lambda fit: fit["start_ts"])
    # The install's usual charge current, over its whole comparable history
    # rather than its newest few fits. The old rule took the newest three so
    # the watch would *follow* a cap — necessary when a median split could
    # only compare like with like, and actively harmful now: the regression
    # holds current, so a cap is something to adjust for rather than chase,
    # and an occasional off-current charge taking over "typical" inverts the
    # admission band and pools the operating current out of its own
    # comparison. A monthly calibration probe is exactly such a charge.
    typical_a = median(fit["current_a"] for fit in usable)
    band = max(2.0, 0.1 * typical_a)
    pool_band = DRIFT_POOL_BAND_FRAC * typical_a
    comparable = [
        fit
        for fit in usable
        if abs(fit["current_a"] - typical_a) <= band
        or (
            fit.get("ambient_drift_c") is not None
            and abs(fit["current_a"] - typical_a) <= pool_band
        )
    ]
    if len(comparable) < DRIFT_MIN_N:
        return None
    recent_ts = {fit["start_ts"] for fit in usable[-DRIFT_RECENCY_N:]}
    if not any(fit["start_ts"] in recent_ts for fit in comparable):
        # None of the newest free-running charges made it into the
        # comparison: the install has moved to a current the band excludes,
        # and every fit that did make it describes a way it no longer
        # charges. Report nothing rather than a verdict about the past — that
        # also lets a stale alert clear. As charges at the new current
        # accumulate they become the median and the band follows them.
        # Deliberately "any of the newest few", not "the newest": a single
        # odd charge must not blank a watch that is otherwise current.
        return None

    origin = comparable[0]["start_ts"]
    days = [(fit["start_ts"] - origin) / 86400.0 for fit in comparable]
    span_days = days[-1] - days[0]
    if span_days <= 0.0:
        return None
    rises = [fit["rise_ref_c"] for fit in comparable]
    currents = [fit["current_a"] for fit in comparable]
    # Ambient is missing on fits that read it from neither end; centre what
    # is there on its own mean so the intercept keeps its meaning.
    ambients = [fit.get("ambient_c") for fit in comparable]
    have_ambient = all(value is not None for value in ambients)

    # Each covariate column is earned by variation. Centring them makes the
    # intercept the predicted rise at the first fit under average conditions,
    # which is what the UI reports as the baseline.
    columns: list[tuple[str, list[float]]] = []
    if have_ambient and max(ambients) - min(ambients) >= DRIFT_AMBIENT_SPREAD_C:
        mean_ambient = sum(ambients) / len(ambients)
        columns.append(("ambient", [value - mean_ambient for value in ambients]))
    if max(currents) - min(currents) >= DRIFT_CURRENT_SPREAD_A:
        mean_current = sum(currents) / len(currents)
        columns.append(("current", [value - mean_current for value in currents]))

    # A thin history cannot afford every column. Drop them back to front —
    # current first, ambient last — until the design is estimable, because
    # ambient is the confounder this watch exists to survive and the one
    # most likely to move with the calendar on its own.
    fit_result = None
    while True:
        design = [[1.0, day] + [column[i] for _, column in columns] for i, day in enumerate(days)]
        fit_result = _ols(rises, design)
        if fit_result is not None or not columns:
            break
        columns.pop()
    if fit_result is None:
        return None
    beta, se, resid_sd, dof = fit_result
    names = [name for name, _ in columns]
    coef = {name: (beta[2 + i], se[2 + i]) for i, name in enumerate(names)}

    slope, slope_se = beta[1], se[1]
    delta = slope * span_days
    delta_se = slope_se * span_days
    t_mult = _t95(dof)
    ci_lo, ci_hi = delta - t_mult * delta_se, delta + t_mult * delta_se
    confident = ci_lo > 0.0
    threshold = max(DRIFT_WARN_C, t_mult * delta_se)

    # What could not be told apart from the calendar, and what the ambient
    # subtraction failed to remove. Neither is an error; both are reasons a
    # delta that looks large is not yet an alert.
    compromised: list[str] = []
    collinear = {
        name: _pearson(days, column)
        for name, column in columns
        if abs(_pearson(days, column)) > DRIFT_COLLINEAR_R
    }
    ambient_coef, ambient_se = coef.get("ambient", (None, None))
    ambient_confounded = (
        ambient_coef is not None
        and abs(ambient_coef) >= DRIFT_AMBIENT_CONFOUND_C
        and abs(ambient_coef) > 2.0 * ambient_se
    )
    if ambient_confounded:
        compromised.append("ambient")

    drifting = confident and delta >= DRIFT_WARN_C and not compromised
    cross_current = sum(1 for fit in comparable if abs(fit["current_a"] - typical_a) > band)
    return {
        "drifting": drifting,
        "lead": delta >= DRIFT_WARN_C and not drifting,
        "confident": confident,
        "baseline_rise_c": round(beta[0], 2),
        "recent_rise_c": round(beta[0] + slope * span_days, 2),
        "delta_c": round(delta, 2),
        "delta_ci95_c": [round(ci_lo, 2), round(ci_hi, 2)],
        "slope_c_per_day": round(slope, 4),
        "slope_se_c_per_day": round(slope_se, 4),
        "span_days": round(span_days, 1),
        "n": len(comparable),
        "resid_sd_c": round(resid_sd, 2),
        "dof": dof,
        "covariates": names,
        "ambient_coef_c_per_c": round(ambient_coef, 3) if ambient_coef is not None else None,
        "ambient_coef_se": round(ambient_se, 3) if ambient_se is not None else None,
        "current_coef_c_per_a": (
            round(coef["current"][0], 3) if "current" in coef else None
        ),
        "collinear_with_time": {name: round(value, 2) for name, value in collinear.items()},
        "compromised_by": compromised,
        "regulated_n": regulated_n,
        "typical_current_a": round(typical_a, 1),
        "off_current_n": len(usable) - len(comparable),
        "cross_current_n": cross_current,
        "anchor_ts": anchor_ts,
        "threshold_c": round(threshold, 2),
        "floor_c": DRIFT_WARN_C,
    }


def baseline_anchor(db: Database) -> float | None:
    """The verified-baseline anchor timestamp, or None when unset."""
    raw = db.get_setting(BASELINE_ANCHOR_KEY)
    try:
        return float(raw) if raw is not None else None
    except ValueError:
        return None


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


def _project_t_inf(window: list[tuple[float, float]], tau_min: float) -> tuple[float, float | None]:
    """Steady state projected from a steady-current window's trajectory,
    with the standard error of that projection.

    With tau known, T(t) = T_inf - C*exp(-t/tau) is linear in (T_inf, C), so
    an ordinary least-squares line on x = exp(-t/tau) gives an unbiased T_inf
    (a straight-line slope would read the window's average rate and overshoot
    during a fast ramp). No ambient input needed. A flat window (variance ~0,
    i.e. already converged) reads as the latest temperature.

    T_inf is the fitted line's intercept at decay = 0 (t -> infinity), so its
    standard error comes from the OLS intercept formula:
    SE = s * sqrt(1/n + mean_x^2 / Sxx) with s^2 = SSE / (n - 2). Early in a
    window, before much curvature is visible, mean_x is near 1 and Sxx is
    tiny — the extrapolation is honest about being wild; near the plateau
    it tightens. That is the per-projection uncertainty the amp controller's
    confidence guard wants (issue #4) — fit_rmse_c is a model-adequacy score
    across historical sessions and carries none of this variation. SE is
    None when it cannot be computed (flat window, n <= 2).
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
    if var <= 1e-9:
        return window[-1][1], None
    slope = cov / var
    t_inf = mean_temp - slope * mean_decay
    if count <= 2:
        return t_inf, None
    sse = sum(
        (temp - (t_inf + slope * decay)) ** 2
        for decay, (_, temp) in zip(decays, window)
    )
    se = math.sqrt(sse / (count - 2)) * math.sqrt(1.0 / count + mean_decay**2 / var)
    return t_inf, se


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
        t_inf, _se = _project_t_inf(window, params.tau_min)
        run_current = median(sample["vehicle_current_a"] for sample in run)
        ambient = t_inf - params.rise_ref_c * (run_current / REF_CURRENT_A) ** 2
        if -30.0 <= ambient <= TRIP_HANDLE_C:
            return ambient
    return None


def predict(db: Database, now: float, params: ThermalParams) -> dict:
    """Forecast alert-40 for the current state (live session or idle)."""
    idle_model = load_idle_offset(db)
    out: dict = {"model": {**params.as_dict(), "idle_offset": idle_model.as_dict()},
                 "state": "no_data", "forecast": None}
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
            t_inf, t_inf_se = _project_t_inf(window, tau_min)
            forecast["basis"] = "trajectory"
            # This projection's own uncertainty — what the amp controller's
            # confidence guard compares the margin against. Floored at the
            # handle sensor's 0.1 C quantization: a perfectly smooth window
            # can drive the raw SE below the sensor's resolution, and a
            # guard fed that would claim more confidence than the data has.
            forecast["steady_state_se_c"] = round(max(t_inf_se, 0.1), 2) if t_inf_se is not None else None
        else:
            # Too early at this current for a slope: model from ambient and
            # the present current scaled by I^2. Ambient comes from the LAN
            # sensor when one is reporting, else the idle stretch before the
            # session, or — when sessions run back-to-back and there was
            # none — from the newest steady run in the buffer.
            measured = _latest_measured_ambient(db, now)
            ambient, source = measured if measured is not None else (None, None)
            if ambient is None:
                sid = last.get("session_id")
                sess = db.session(int(sid)) if sid else None
                ambient = _ambient_before(db, sess["start_ts"], idle_model) if sess else None
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
            # A model-basis plateau is only as good as its ambient: a sensor
            # reads air directly; every handle-derived route carries the
            # idle-offset model's own uncertainty, 1:1 into the plateau.
            forecast["ambient_se_c"] = 0.3 if source in ("measured", "measured_car") else idle_model.ambient_se_c
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
        measured = _latest_measured_ambient(db, now)
        if measured is not None:
            # A reporting LAN sensor reads the garage air directly — no idle
            # offset assumption, and valid even while the handle is still
            # cooling from a recent charge (when the proxy reads high).
            ambient, ambient_source = measured
            stable = True
        else:
            ambient = ambient_from_idle_handle(last["handle_temp_c"], idle_model)
            ambient_source = "idle_handle"
        out["ambient_c"] = round(ambient, 1)
        out["ambient_source"] = ambient_source
        out["ambient_se_c"] = 0.3 if measured is not None else idle_model.ambient_se_c
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
