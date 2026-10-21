"""Turn per-frame LED states into measurements, verdicts and requirement statuses.

Everything here is a pure function of recorded frame data (timestamps from the
client's monotonic clock + classified LED states), so it is deterministic and
unit-testable without images.

Timing model and tolerances
---------------------------
An LED change happens somewhere between the last frame showing the old value
and the first frame showing the new one. Its time is estimated as the midpoint
with a half-width h = (gap)/2; at 30 fps with no transitional frames h = 16.7 ms.
Treating the true time as uniform over +-h gives a standard deviation h/sqrt(3)
(9.6 ms at 30 fps), combined in quadrature with the measured timestamp jitter.
Rates are fitted by least squares over event times vs step index, so the
period uncertainty shrinks with the number of events and the observed span.

Decisions are three-zone, so the camera never fails a design on weak evidence:
  * |error| <= tol                 -> passed
  * tol < |error| <= 1.5 x tol     -> inconclusive (ambiguous / marginal)
  * |error| > 1.5 x tol            -> failed (only with enough events and good quality)
with tol = max(floor, 3 sigma). The floors (5%) cover systematic camera timing
effects that the per-event model does not: browsers stamp the frame when the
canvas grabs it, which can lag the exposure by up to one camera frame and can
repeat a frame. The FPGA side is crystal-timed (+-50 ppm), so a correct design
lands well inside 5%, while the mistakes that matter (a 2:1 instead of 3:1
speed-up, a 2 Hz instead of 1 Hz tick) are >= 33% off -- 6x the floor.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from backend.specifications.derive import Observation, Requirement
from backend.specifications.ir import (
    BlinkerBehavior,
    CounterBehavior,
    DesignSpec,
    LedPatternBehavior,
    TrafficLightBehavior,
)

from .frames import TimingStats

# Frames a value must persist before it counts. One-frame values are what an LED
# switching during the exposure, or a rolling-shutter split, looks like.
MIN_STABLE_FRAMES = 2
# Time skipped at the start of every guided phase: the user has just pressed or
# released a button / flipped a switch and clicked Start.
SETTLE_MS = 500.0
MIN_RATE_EVENTS = 5            # >= 4 intervals for a regression with a residual check
MIN_SEQUENCE_EVENTS = 5
REL_TOL_FLOOR = 0.05
RATIO_REL_TOL_FLOOR = 0.05
DUTY_TOL_FLOOR = 0.05          # absolute (5 percentage points)
DURATION_REL_TOL_FLOOR = 0.05
AMBIGUOUS_FACTOR = 1.5
SIGMA_K = 3.0
# Residual (in periods) beyond which steps are irregular: a held button released
# for part of the phase produces a ~2x or ~3x interval, far above this.
MAX_RESIDUAL_PERIODS = 0.35
# Quality gates per phase.
MAX_MOVED_FRACTION = 0.05
MAX_DARK_FRACTION = 0.5
MIN_VALID_FRACTION = 0.6
MIN_FRAMES = 10
# Each visible LED state must span >= 3 frames so a 2-frame stability rule
# cannot drop states (the derive module plans for 4 frames at 30 fps).
MIN_FRAMES_PER_STATE = 3.0
# A static-pattern phase needs this much stable, valid footage of one value.
STATIC_MIN_STABLE_S = 1.0
STATIC_MIN_DOMINANCE = 0.8
# Unused LEDs: lit for at least this long (stable) is a real violation.
IDLE_MIN_LIT_S = 0.5
IDLE_MIN_VALID_S = 2.0


# ----------------------------------------------------------------------------- data


@dataclass
class PhaseSeries:
    """One guided phase's frames, LED bits in Observation.leds (core bit) order."""

    phase_id: str
    t_ms: np.ndarray                      # (n,) client timestamps, after the settle window
    bits: np.ndarray                      # (n, width) int8: 1 / 0 / -1 unknown
    reliable: np.ndarray                  # (n,) bool: camera not moved / tracking ok
    dark: np.ndarray                      # (n,) bool: too-dark frame
    duration_s: float                     # whole phase (incl. settle), client clock
    min_duration_s: float
    timing: TimingStats
    calibration_flags: list[str] = field(default_factory=list)
    expected_value: int | None = None     # static_pattern phases


@dataclass
class Run:
    value: int
    i0: int
    i1: int          # inclusive
    t0: float
    t1: float

    @property
    def frames(self) -> int:
        return self.i1 - self.i0 + 1


@dataclass
class Event:
    t_ms: float
    half_ms: float
    before: int
    after: int
    gap_frames: int           # frames between the two stable runs (unknown / transient)
    i_prev: int               # index of the run before the event


@dataclass
class RateFit:
    hz: float
    sigma_hz: float
    period_ms: float
    sigma_period_ms: float
    n_events: int
    span_s: float
    max_residual_periods: float


def values_from_bits(bits: np.ndarray) -> list[int | None]:
    """Decode rows of bits (bit i has weight 2**i); None if any bit is unknown."""
    out: list[int | None] = []
    w = bits.shape[1] if bits.ndim == 2 else 0
    weights = (1 << np.arange(w)).astype(np.int64)
    for row in bits:
        out.append(None if (row < 0).any() else int((row.astype(np.int64) * weights).sum()))
    return out


def stable_runs(values: list[int | None], t_ms: np.ndarray, min_frames: int = MIN_STABLE_FRAMES) -> list[Run]:
    runs: list[Run] = []
    i, n = 0, len(values)
    while i < n:
        v = values[i]
        j = i
        while j + 1 < n and values[j + 1] == v:
            j += 1
        if v is not None and j - i + 1 >= min_frames:
            runs.append(Run(v, i, j, float(t_ms[i]), float(t_ms[j])))
        i = j + 1
    return runs


def merge_same(runs: list[Run]) -> list[Run]:
    """Merge consecutive runs of the same value (separated by unknown/transient frames)."""
    out: list[Run] = []
    for r in runs:
        if out and out[-1].value == r.value:
            out[-1] = Run(r.value, out[-1].i0, r.i1, out[-1].t0, r.t1)
        else:
            out.append(r)
    return out


def events_between(runs: list[Run]) -> list[Event]:
    ev: list[Event] = []
    for k in range(1, len(runs)):
        a, b = runs[k - 1], runs[k]
        ev.append(Event(0.5 * (a.t1 + b.t0), 0.5 * (b.t0 - a.t1), a.value, b.value, b.i0 - a.i1 - 1, k - 1))
    return ev


def event_sigma_ms(e: Event, jitter_ms: float) -> float:
    return math.sqrt(e.half_ms ** 2 / 3.0 + jitter_ms ** 2)


def fit_rate(t_ms: list[float], index: list[float], sigmas_ms: list[float], cycles_per_step: float = 1.0) -> RateFit | None:
    """Least-squares t = a + P*k. Returns steps/s (x cycles_per_step) with a 1-sigma uncertainty.

    sigma(P) is the larger of the timing-model value sigma_t / sqrt(sum (k - kbar)^2)
    and the residual-based standard error, so unexpected scatter is never hidden.
    """
    if len(t_ms) < 3:
        return None
    k = np.asarray(index, dtype=np.float64)
    t = np.asarray(t_ms, dtype=np.float64)
    kc = k - k.mean()
    sxx = float((kc ** 2).sum())
    if sxx <= 0:
        return None
    period = float((kc * (t - t.mean())).sum() / sxx)
    if period <= 0:
        return None
    resid = t - (t.mean() + period * kc)
    sig_model = math.sqrt(float(np.mean(np.square(sigmas_ms)))) / math.sqrt(sxx)
    sig_resid = math.sqrt(float((resid ** 2).sum()) / max(1, len(t) - 2) / sxx)
    sig_p = max(sig_model, sig_resid)
    hz = 1000.0 / period * cycles_per_step
    return RateFit(hz=hz, sigma_hz=hz * sig_p / period, period_ms=period, sigma_period_ms=sig_p,
                   n_events=len(t), span_s=float(t.max() - t.min()) / 1000.0,
                   max_residual_periods=float(np.abs(resid).max() / period))


def judge(measured: float, expected: float, sigma: float, floor: float, absolute: bool = False) -> dict:
    """Three-zone decision. Relative unless absolute=True."""
    if absolute:
        err = measured - expected
        tol = max(floor, SIGMA_K * sigma)
    else:
        err = (measured - expected) / expected
        tol = max(floor, SIGMA_K * sigma / expected)
    a = abs(err)
    status = "passed" if a <= tol else ("inconclusive" if a <= AMBIGUOUS_FACTOR * tol else "failed")
    return {"status": status, "error": round(err, 4), "tolerance": round(tol, 4),
            "mode": "absolute" if absolute else "relative"}


def _r(v: float | None, n: int = 4) -> float | None:
    return None if v is None or not math.isfinite(v) else round(float(v), n)


def _combine(statuses: list[str]) -> str:
    if any(s == "failed" for s in statuses):
        return "failed"
    if statuses and all(s == "passed" for s in statuses):
        return "passed"
    return "inconclusive"


def _result(obs: Observation, phase_id: str | None) -> dict:
    return {"id": obs.id, "kind": obs.kind, "description": obs.description, "phase_id": phase_id,
            "requirement_ids": list(obs.requirement_ids), "status": "inconclusive",
            "measured": {}, "expected": dict(obs.expected), "tolerance": {}, "n_events": 0, "ci": None,
            "notes": [], "flags": [], "requirement_checks": {}, "events_ms": []}


# ----------------------------------------------------------------------------- quality gate


def quality_gate(series: PhaseSeries, visible_changes_per_s: float | None) -> tuple[list[str], list[str]]:
    """(reasons the phase cannot give pass/fail evidence, quality flags)."""
    reasons: list[str] = []
    flags: list[str] = list(series.calibration_flags)
    n = int(series.t_ms.size)
    if n < MIN_FRAMES:
        reasons.append(f"only {n} frames were recorded in this phase")
        return reasons, sorted(set(flags))
    if series.duration_s + 1e-6 < series.min_duration_s:
        reasons.append(f"phase lasted {series.duration_s:.1f} s; at least {series.min_duration_s:g} s is needed")
        flags.append("phase_too_short")
    moved = 1.0 - float(series.reliable.mean())
    if moved > MAX_MOVED_FRACTION:
        reasons.append(f"the camera moved during {moved:.0%} of the phase; set the LED regions again")
        flags.append("camera_moved")
    dark = float(series.dark.mean())
    if dark > MAX_DARK_FRACTION:
        reasons.append("the image is too dark for reliable LED detection")
        flags.append("too_dark")
    valid = float((series.bits >= 0).all(axis=1).mean()) if series.bits.size else 0.0
    if valid < MIN_VALID_FRACTION:
        why = "low contrast" if "low_contrast" in flags else "uncalibrated LEDs" if "uncalibrated" in flags else "unclear LED states"
        reasons.append(f"LED states were reliable in only {valid:.0%} of frames ({why})")
        flags.append("unreliable_states")
    fps = series.timing.fps
    if visible_changes_per_s and fps:
        fps_needed = MIN_FRAMES_PER_STATE * visible_changes_per_s
        if fps < fps_needed:
            reasons.append(f"camera frame rate {fps:.1f} fps is below the {fps_needed:.0f} fps needed for "
                           f"{visible_changes_per_s:g} LED changes per second")
            flags.append("low_fps")
    if series.timing.frames > 1 and series.timing.dropped_frames > 0.2 * series.timing.frames:
        flags.append("dropped_frames")
    return reasons, sorted(set(flags))


def _apply_gate(res: dict, reasons: list[str], flags: list[str]) -> None:
    res["flags"] = sorted(set(res["flags"]) | set(flags))
    if reasons:
        res["notes"] = reasons + res["notes"]
        if res["status"] in ("passed", "failed"):
            res["notes"].append(f"measurement would have been '{res['status']}' but the evidence quality is insufficient")
        res["status"] = "inconclusive"
        for k, v in list(res["requirement_checks"].items()):
            if v in ("passed", "failed"):
                res["requirement_checks"][k] = "inconclusive"


# ----------------------------------------------------------------------------- step rate (counter / pattern)


def _pattern_step(pattern: str, width: int, a: int, b: int) -> str | None:
    """Classify a transition of a moving LED pattern: 'left' / 'right' / 'either' / None (illegal)."""
    mask = (1 << width) - 1
    rotl = ((a << 1) | (a >> (width - 1))) & mask
    rotr = ((a >> 1) | (a << (width - 1))) & mask
    if pattern == "chase":
        if bin(a).count("1") != 1 or bin(b).count("1") != 1:
            return None
        return "left" if b == rotl else "right" if b == rotr else None
    if pattern == "bounce":
        if bin(a).count("1") != 1 or bin(b).count("1") != 1:
            return None
        return "either" if b in ((a << 1) & mask, a >> 1) else None
    if pattern == "fill":
        if a == mask and b == 0:
            return "either"
        if b == (((a << 1) | 1) & mask) and a != mask:
            return "left"
        if b == ((a >> 1) | (1 << (width - 1))) and a != mask:
            return "right"
        return None
    return None


def measure_step_rate(obs: Observation, spec: DesignSpec, series: PhaseSeries) -> dict:
    res = _result(obs, series.phase_id)
    b = spec.behavior
    width = len(obs.leds)
    modulus = 1 << width
    hz_exp = obs.expected.get("hz")
    period_exp_ms = 1000.0 / hz_exp if hz_exp else None
    reasons, flags = quality_gate(series, obs.expected.get("visible_changes_per_s"))
    values = values_from_bits(series.bits)
    runs = merge_same(stable_runs(values, series.t_ms))
    events = events_between(runs)
    jitter = series.timing.jitter_ms or 0.0
    rate_mode = f"R-RATE-{obs.expected.get('mode')}"
    checks: dict[str, str] = {}

    is_counter = isinstance(b, CounterBehavior)
    if is_counter:
        dir_fixed = b.direction_control is None
        expected_dir = b.direction
    else:
        assert isinstance(b, LedPatternBehavior)
        dir_fixed = b.direction_control is None and b.pattern != "bounce"
        expected_dir = "left" if b.direction == "left" else "right"

    # classify every transition
    kinds: list[str] = []
    steps: list[int] = []
    for e in events:
        if is_counter:
            d = (e.after - e.before) % modulus
            if d == 1:
                kinds.append("up"); steps.append(1)
            elif d == modulus - 1:
                kinds.append("down"); steps.append(1)
            else:
                # 2-3 counts in one direction across an unreliable gap long enough
                # to hide them is a missed observation, not a design error
                gap_ok = period_exp_ms is not None and 2 * e.half_ms > 0.5 * period_exp_ms
                if gap_ok and d in (2, 3):
                    kinds.append("missed_up"); steps.append(d)
                elif gap_ok and modulus - d in (2, 3):
                    kinds.append("missed_down"); steps.append(modulus - d)
                else:
                    kinds.append("illegal"); steps.append(0)
        else:
            k = _pattern_step(b.pattern, width, e.before, e.after)
            kinds.append(k or "illegal"); steps.append(1 if k else 0)

    n_up = sum(1 for k in kinds if k in ("up", "left"))
    n_down = sum(1 for k in kinds if k in ("down", "right"))
    n_either = sum(1 for k in kinds if k == "either")
    illegal = [e for e, k in zip(events, kinds) if k == "illegal"]
    illegal_clean = [e for e in illegal if e.gap_frames <= 2]
    fwd_names = ("up", "left") if (is_counter and expected_dir == "up") or (not is_counter and expected_dir == "left") else ("down", "right")
    rev_names = ("down", "right") if fwd_names[0] in ("up", "left") else ("up", "left")
    n_fwd = n_up if fwd_names[0] in ("up", "left") else n_down
    n_rev = n_down if fwd_names[0] in ("up", "left") else n_up
    # the direction actually used for the rate fit
    dominant = fwd_names if n_fwd + n_either >= n_rev else rev_names

    # rate fit over steps in the dominant direction (missed steps advance the index only)
    t_fit, k_fit, s_fit = [], [], []
    idx = 0
    for e, kind, st in zip(events, kinds, steps):
        if kind in dominant or kind == "either":
            idx += 1
            t_fit.append(e.t_ms); k_fit.append(idx); s_fit.append(event_sigma_ms(e, jitter))
        elif kind.startswith("missed") and kind.endswith(dominant[0]):
            idx += st
    fit = fit_rate(t_fit, k_fit, s_fit)
    res["n_events"] = len(t_fit)
    res["events_ms"] = [round(e.t_ms, 1) for e in events]
    wraps = 0
    if is_counter:
        top = modulus - 1
        wraps = sum(1 for e, k in zip(events, kinds) if (k == "up" and e.before == top and e.after == 0)
                    or (k == "down" and e.before == 0 and e.after == top))
    res["measured"] = {
        "hz": _r(fit.hz) if fit else None,
        "sigma_hz": _r(fit.sigma_hz, 5) if fit else None,
        "period_ms": _r(fit.period_ms, 2) if fit else None,
        "span_s": _r(fit.span_s, 3) if fit else None,
        "steps_forward": n_fwd, "steps_reverse": n_rev, "steps_either": n_either,
        "illegal_transitions": len(illegal), "wraps_observed": wraps,
        "values": [r.value for r in runs][:200],
        "rate_valid": bool(fit and fit.n_events >= MIN_RATE_EVENTS),
    }
    if fit:
        res["ci"] = [_r(fit.hz - 1.96 * fit.sigma_hz), _r(fit.hz + 1.96 * fit.sigma_hz)]

    statuses: list[str] = []
    # --- direction / legality
    n_moves = n_fwd + n_rev + n_either
    if dir_fixed and n_moves >= MIN_RATE_EVENTS and n_rev > n_fwd and n_rev >= 0.8 * n_moves:
        dir_status = "failed"
        res["notes"].append(f"steps go the wrong way: {n_rev} of {n_moves} steps are "
                            f"{'down' if 'down' in rev_names else 'right'} (expected {expected_dir})")
    elif dir_fixed and n_rev >= 2 and n_fwd >= 2:
        dir_status = "inconclusive"
        res["notes"].append(f"mixed directions ({n_fwd} forward, {n_rev} reverse steps)")
    elif n_moves >= MIN_RATE_EVENTS:
        dir_status = "passed"
    else:
        dir_status = "inconclusive"
    if len(illegal_clean) >= 2 and len(illegal_clean) >= 0.2 * max(1, len(events)):
        legal_status = "failed"
        bad = ", ".join(f"{e.before}->{e.after}" for e in illegal_clean[:4])
        res["notes"].append(f"{len(illegal_clean)} transitions are not single steps ({bad})")
    elif illegal:
        legal_status = "passed" if n_moves >= MIN_RATE_EVENTS else "inconclusive"
        res["notes"].append(f"{len(illegal)} isolated irregular transition(s) ignored (likely a misread frame)")
    else:
        legal_status = "passed" if n_moves >= MIN_RATE_EVENTS else "inconclusive"
    statuses += [dir_status, legal_status]

    # --- rate
    rate_status = "inconclusive"
    if hz_exp is None:
        rate_status = "passed" if fit else "inconclusive"
    elif fit is None or fit.n_events < MIN_RATE_EVENTS:
        res["notes"].append(f"only {len(t_fit)} steps observed; at least {MIN_RATE_EVENTS} are needed")
    elif fit.max_residual_periods > MAX_RESIDUAL_PERIODS:
        res["notes"].append("step intervals are irregular (was the input held for the whole phase?)")
        flags.append("rate_unsteady")
    else:
        j = judge(fit.hz, float(hz_exp), fit.sigma_hz, REL_TOL_FLOOR)
        rate_status = j["status"]
        res["tolerance"] = {"rate_relative": j["tolerance"], "rule": "max(5%, 3 sigma); fail beyond 1.5x"}
        res["measured"]["rate_error"] = j["error"]
        if rate_status != "passed":
            res["notes"].append(f"rate {fit.hz:.3f}/s vs expected {hz_exp:g}/s ({j['error']:+.1%})")
    statuses.append(rate_status)

    # --- stall at the end (counter stopped)
    if runs and period_exp_ms and events:
        tail = float(series.t_ms[-1]) - runs[-1].t0
        if tail > 3 * period_exp_ms and len(events) >= MIN_RATE_EVENTS:
            v = runs[-1].value
            at_edge = is_counter and v in (0, modulus - 1)
            res["notes"].append(f"value stayed at {v} for the last {tail / 1000:.1f} s")
            if is_counter and b.wrap and at_edge and ((expected_dir == "up") == (v == modulus - 1)):
                checks["R-WRAP"] = "failed"
                statuses.append("failed")
            elif is_counter and not b.wrap and at_edge:
                checks["R-WRAP"] = "passed"
            else:
                statuses.append("inconclusive")

    if is_counter:
        checks["R-DIR"] = _combine([dir_status, legal_status])
        if "R-WRAP" not in checks:
            checks["R-WRAP"] = ("passed" if wraps and dir_status != "failed" else "not_observed") if b.wrap else "not_observed"
    else:
        checks["R-PATTERN"] = legal_status if legal_status != "passed" or dir_status != "failed" else "failed"
        if not dir_fixed:
            checks["R-DIR"] = "not_observed"
    checks[rate_mode] = rate_status if dir_status != "failed" else _combine([rate_status, "failed"])
    res["requirement_checks"] = checks
    res["status"] = _combine(statuses)
    _apply_gate(res, reasons, flags)
    return res


# ----------------------------------------------------------------------------- toggle rate (blinker)


def measure_toggle_rate(obs: Observation, spec: DesignSpec, series: PhaseSeries) -> dict:
    res = _result(obs, series.phase_id)
    b = spec.behavior
    assert isinstance(b, BlinkerBehavior)
    width = len(obs.leds)
    mask = (1 << width) - 1
    odd = sum(1 << i for i in range(1, width, 2))
    legal = {0, mask} if b.pattern == "together" else {odd, mask ^ odd}
    reasons, flags = quality_gate(series, obs.expected.get("visible_changes_per_s"))
    values = values_from_bits(series.bits)
    runs_all = stable_runs(values, series.t_ms)
    illegal_runs = [r for r in runs_all if r.value not in legal and r.frames >= 3]
    runs = merge_same([r for r in runs_all if r.value in legal])
    events = events_between(runs)
    jitter = series.timing.jitter_ms or 0.0
    fit = fit_rate([e.t_ms for e in events], list(range(1, len(events) + 1)),
                   [event_sigma_ms(e, jitter) for e in events], cycles_per_step=0.5)
    res["n_events"] = len(events)
    res["events_ms"] = [round(e.t_ms, 1) for e in events]

    # duty from complete runs (bounded by events on both sides): "on" = bit 0 lit
    on_d, off_d = [], []
    for k in range(1, len(events)):
        d = events[k].t_ms - events[k - 1].t_ms
        (on_d if runs[k].value & 1 else off_d).append(d)
    duty = sig_duty = None
    if len(on_d) >= 2 and len(off_d) >= 2:
        # Mean on/off durations: the phase usually holds one more on- than off-interval
        # (or vice versa), so summing them would bias the duty by ~1/n.
        a, c = float(np.mean(on_d)), float(np.mean(off_d))
        duty = a / (a + c)
        # Edge-time quantization (+-half a frame gap) does NOT average out over periods
        # when the blink is phase-locked to the camera (e.g. 2 Hz at exactly 30 fps:
        # every on-interval spans the same number of frames), so the per-interval
        # uncertainty is not divided by sqrt(n). On and off errors are anti-correlated
        # (the period is fixed), giving sigma(duty) = sigma(on-duration) / period.
        sig_edge = math.sqrt((jitter ** 2) + (np.mean([e.half_ms for e in events]) ** 2) / 3.0)
        sig_duty = math.sqrt(2) * sig_edge / (a + c)
    res["measured"] = {"hz": _r(fit.hz) if fit else None, "sigma_hz": _r(fit.sigma_hz, 5) if fit else None,
                       "duty": _r(duty), "sigma_duty": _r(sig_duty, 5), "toggles": len(events),
                       "illegal_patterns": len(illegal_runs),
                       "rate_valid": bool(fit and fit.n_events >= MIN_RATE_EVENTS)}
    if fit:
        res["ci"] = [_r(fit.hz - 1.96 * fit.sigma_hz), _r(fit.hz + 1.96 * fit.sigma_hz)]

    statuses: list[str] = []
    # pattern (together / alternate)
    if len(illegal_runs) >= 2:
        pat = "failed"
        res["notes"].append(f"{len(illegal_runs)} stable LED patterns violate '{b.pattern}'")
    elif len(events) >= MIN_RATE_EVENTS:
        pat = "passed"
    else:
        pat = "inconclusive"
    statuses.append(pat)
    rate = "inconclusive"
    hz_exp = float(obs.expected["hz"])
    if fit is None or fit.n_events < MIN_RATE_EVENTS:
        res["notes"].append(f"only {len(events)} toggles observed; at least {MIN_RATE_EVENTS} are needed")
    elif fit.max_residual_periods > MAX_RESIDUAL_PERIODS:
        res["notes"].append("toggle intervals are irregular (was the input held for the whole phase?)")
        flags.append("rate_unsteady")
    else:
        j = judge(fit.hz, hz_exp, fit.sigma_hz, REL_TOL_FLOOR)
        rate = j["status"]
        res["measured"]["rate_error"] = j["error"]
        res["tolerance"]["rate_relative"] = j["tolerance"]
        if rate != "passed":
            res["notes"].append(f"blink frequency {fit.hz:.3f} Hz vs expected {hz_exp:g} Hz ({j['error']:+.1%})")
    statuses.append(rate)
    if duty is None:
        statuses.append("inconclusive")
        res["notes"].append("not enough complete on/off periods to measure the duty cycle")
    else:
        jd = judge(duty, 0.5, sig_duty or 0.0, DUTY_TOL_FLOOR, absolute=True)
        res["tolerance"]["duty_absolute"] = jd["tolerance"]
        statuses.append(jd["status"])
        if jd["status"] != "passed":
            res["notes"].append(f"duty cycle {duty:.1%} vs expected 50%")
    res["tolerance"]["rule"] = "rate max(5%, 3 sigma); duty max(5 points, 3 sigma); fail beyond 1.5x"
    res["requirement_checks"] = {f"R-RATE-{obs.expected.get('mode')}": _combine(statuses[1:]), "R-PATTERN": pat}
    res["status"] = _combine(statuses)
    _apply_gate(res, reasons, flags)
    return res


# ----------------------------------------------------------------------------- traffic light


def measure_state_durations(obs: Observation, spec: DesignSpec, series: PhaseSeries) -> dict:
    res = _result(obs, series.phase_id)
    b = spec.behavior
    assert isinstance(b, TrafficLightBehavior)
    width = len(obs.leds)
    mask = (1 << width) - 1
    states = [(s.name, s.lights & mask, s.duration_s) for s in b.states]
    req = (b.request_state.name, b.request_state.lights & mask, b.request_state.duration_s) if b.request_state else None
    all_states = states + ([req] if req else [])
    by_value: dict[int, list[int]] = {}
    for i, (_n, v, _d) in enumerate(all_states):
        by_value.setdefault(v, []).append(i)
    names = [s[0] for s in all_states]
    n_cyc = len(states)
    req_after = next((i for i, s in enumerate(states) if s[0] == b.request_after), None)

    def legal_next(i: int) -> set[int]:
        nxt = {(i + 1) % n_cyc} if i < n_cyc else set()
        if req and req_after is not None:
            if i == req_after:
                nxt.add(n_cyc)
            if i == n_cyc:
                nxt.add((req_after + 1) % n_cyc)
        return nxt

    reasons, flags = quality_gate(series, None)
    values = values_from_bits(series.bits)
    runs_all = stable_runs(values, series.t_ms)
    illegal_runs = [r for r in runs_all if r.value not in by_value and r.frames >= 3
                    and (r.t1 - r.t0) >= 100.0]
    runs = merge_same([r for r in runs_all if r.value in by_value])
    events = events_between(runs)
    jitter = series.timing.jitter_ms or 0.0
    fi = series.timing.median_interval_ms or 33.3

    # decode a state index per run, disambiguating duplicate light patterns by sequence
    idx: list[int] = []
    for k, r in enumerate(runs):
        cands = by_value[r.value]
        if len(cands) > 1 and idx:
            nx = [c for c in cands if c in legal_next(idx[-1])]
            cands = nx or cands
        idx.append(cands[0])
    illegal_tr = [(idx[k - 1], idx[k]) for k in range(1, len(idx)) if idx[k] not in legal_next(idx[k - 1])
                  and events[k - 1].gap_frames <= 2]
    visits: dict[str, list[dict]] = {n: [] for n in names}
    for k in range(1, len(events)):
        e0, e1 = events[k - 1], events[k]
        d = (e1.t_ms - e0.t_ms) / 1000.0
        sig = math.sqrt(event_sigma_ms(e0, jitter) ** 2 + event_sigma_ms(e1, jitter) ** 2) / 1000.0
        visits[names[idx[k]]].append({"duration_s": round(d, 4), "sigma_s": round(sig, 4), "t_ms": round(e0.t_ms, 1)})
    res["n_events"] = len(events)
    res["events_ms"] = [round(e.t_ms, 1) for e in events]
    res["measured"] = {"sequence": [names[i] for i in idx][:200], "visits": visits,
                       "illegal_patterns": [r.value for r in illegal_runs][:20],
                       "illegal_transitions": [f"{names[a]}->{names[c]}" for a, c in illegal_tr][:20]}
    checks: dict[str, str] = {}
    statuses: list[str] = []
    # output patterns
    seen = {names[i] for i in idx}
    if illegal_runs:
        checks["R-FSM-OUT"] = "failed"
        res["notes"].append(f"LED pattern(s) {sorted({r.value for r in illegal_runs})} match no state")
    elif all(s[0] in seen for s in states):
        checks["R-FSM-OUT"] = "passed"
    else:
        checks["R-FSM-OUT"] = "inconclusive"
    # sequence
    if illegal_tr:
        checks["R-FSM-SEQ"] = "failed"
        res["notes"].append("illegal transitions: " + ", ".join(res["measured"]["illegal_transitions"][:4]))
    elif len(events) >= MIN_SEQUENCE_EVENTS and all(s[0] in seen for s in states):
        checks["R-FSM-SEQ"] = "passed"
    else:
        checks["R-FSM-SEQ"] = "inconclusive"
        res["notes"].append(f"{len(events)} transitions observed; at least {MIN_SEQUENCE_EVENTS} covering every state are needed")
    # per-state durations: each visit has two edges -> +-1 frame worst case; floor 5%
    tol_info = {}
    for name, _v, dur in states:
        vs = visits.get(name, [])
        rid = f"R-FSM-T-{name}"
        if not vs:
            checks[rid] = "inconclusive"
            continue
        sts = []
        for v in vs:
            j = judge(v["duration_s"], dur, max(v["sigma_s"], fi / 1000.0 / math.sqrt(3)), DURATION_REL_TOL_FLOOR)
            v["status"] = j["status"]
            sts.append(j["status"])
            tol_info[name] = j["tolerance"]
        checks[rid] = _combine(sts)
        if checks[rid] == "failed":
            res["notes"].append(f"state {name} lasted {np.median([v['duration_s'] for v in vs]):.2f} s (expected {dur:g} s)")
    res["tolerance"] = {"duration_relative": tol_info, "rule": "max(5%, 3 sigma) per visit; fail beyond 1.5x"}
    statuses = list(checks.values())
    res["requirement_checks"] = checks
    res["status"] = _combine(statuses)
    _apply_gate(res, reasons, flags)
    return res


# ----------------------------------------------------------------------------- static pattern


def static_expected_value(spec: DesignSpec, levels: dict[str, int]) -> int:
    """Expected LED value (core bit order) for input levels {input_name: 0/1}."""
    b = spec.behavior
    assert isinstance(b, LedPatternBehavior)
    v = 0
    for i, name in enumerate(b.source_inputs):
        bit = levels.get(name, 0)
        if b.pattern == "invert_inputs":
            bit = 1 - bit
        v |= bit << i
    return v


def measure_static_phase(obs: Observation, series: PhaseSeries) -> dict:
    res = _result(obs, series.phase_id)
    reasons, flags = quality_gate(series, None)
    values = values_from_bits(series.bits)
    runs = stable_runs(values, series.t_ms)
    fi = series.timing.median_interval_ms or 33.3
    time_by_val: dict[int, float] = {}
    for r in runs:
        time_by_val[r.value] = time_by_val.get(r.value, 0.0) + (r.t1 - r.t0 + fi)
    total = sum(time_by_val.values())
    exp = series.expected_value
    res["expected"] = {**res["expected"], "value": exp, "bits": None if exp is None else format(exp, f"0{len(obs.leds)}b")}
    if not time_by_val:
        res["notes"].append("no stable LED pattern was observed")
        status = "inconclusive"
    else:
        dom, t_dom = max(time_by_val.items(), key=lambda kv: kv[1])
        res["measured"] = {"value": dom, "bits": format(dom, f"0{len(obs.leds)}b"), "stable_s": _r(t_dom / 1000.0, 3),
                           "dominance": _r(t_dom / total, 3)}
        if t_dom / 1000.0 < STATIC_MIN_STABLE_S or t_dom / total < STATIC_MIN_DOMINANCE:
            status = "inconclusive"
            res["notes"].append("the LED pattern was not steady long enough")
        elif dom == exp:
            status = "passed"
        else:
            status = "failed"
            res["notes"].append(f"LEDs showed {format(dom, f'0{len(obs.leds)}b')} but "
                                f"{format(exp or 0, f'0{len(obs.leds)}b')} was expected for the instructed input settings "
                                "(bit order: highest LED left)")
    res["status"] = status
    res["requirement_checks"] = {rid: status for rid in obs.requirement_ids}
    _apply_gate(res, reasons, flags)
    return res


def combine_static(obs: Observation, phase_results: list[dict], planned: int) -> dict:
    res = _result(obs, None)
    res["measured"] = {"phases": [{"phase_id": p["phase_id"], "status": p["status"], "measured": p["measured"],
                                   "expected": p["expected"].get("bits")} for p in phase_results]}
    sts = [p["status"] for p in phase_results]
    res["n_events"] = len(phase_results)
    res["flags"] = sorted({f for p in phase_results for f in p["flags"]})
    res["notes"] = [n for p in phase_results for n in p["notes"]]
    if any(s == "failed" for s in sts):
        res["status"] = "failed"
    elif len(sts) >= planned and all(s == "passed" for s in sts):
        res["status"] = "passed"
    else:
        res["status"] = "inconclusive"
        if len(sts) < planned:
            res["notes"].append(f"{len(sts)} of {planned} switch settings were checked")
    res["requirement_checks"] = {rid: res["status"] for rid in obs.requirement_ids}
    return res


# ----------------------------------------------------------------------------- ratio


def measure_ratio(obs: Observation, num: dict | None, den: dict | None) -> dict:
    res = _result(obs, None)
    exp = float(obs.expected["ratio"])
    usable = all(r is not None and r.get("measured", {}).get("rate_valid") and r["status"] != "inconclusive"
                 for r in (num, den))
    if not usable:
        res["notes"].append("both rates must be measured with sufficient evidence before the ratio is computed")
        flags = sorted({f for r in (num, den) if r for f in r.get("flags", [])})
        res["flags"] = flags
        if num and den and num.get("measured", {}).get("hz") and den.get("measured", {}).get("hz"):
            res["measured"] = {"ratio": _r(num["measured"]["hz"] / den["measured"]["hz"])}
        res["requirement_checks"] = {rid: "inconclusive" for rid in obs.requirement_ids}
        return res
    f, sf = num["measured"]["hz"], num["measured"]["sigma_hz"]   # type: ignore[index]
    s, ss = den["measured"]["hz"], den["measured"]["sigma_hz"]   # type: ignore[index]
    ratio = f / s
    srel = math.sqrt((sf / f) ** 2 + (ss / s) ** 2)
    sigma = ratio * srel
    j = judge(ratio, exp, sigma, RATIO_REL_TOL_FLOOR)
    res["measured"] = {"ratio": _r(ratio), "sigma": _r(sigma, 5), "numerator_hz": f, "denominator_hz": s,
                       "ratio_error": j["error"]}
    res["ci"] = [_r(ratio - 1.96 * sigma), _r(ratio + 1.96 * sigma)]
    res["tolerance"] = {"ratio_relative": j["tolerance"], "rule": "max(5%, 3 sigma propagated); fail beyond 1.5x"}
    res["n_events"] = int(num.get("n_events", 0)) + int(den.get("n_events", 0))  # type: ignore[union-attr]
    res["status"] = j["status"]
    if j["status"] != "passed":
        res["notes"].append(f"measured ratio {ratio:.2f}:1 vs expected {exp:g}:1")
    res["requirement_checks"] = {rid: j["status"] for rid in obs.requirement_ids}
    return res


# ----------------------------------------------------------------------------- unused LEDs


def measure_idle(leds: list[int], t_ms: np.ndarray, bits: np.ndarray, timing: TimingStats) -> dict:
    """Unused LEDs inside the ROI must stay dark for the whole session."""
    res = {"id": "O-IDLE", "kind": "idle_leds", "description": "LEDs not used by the design stay off",
           "phase_id": None, "requirement_ids": ["R-IDLE"], "status": "inconclusive", "measured": {},
           "expected": {"leds": leds, "state": 0}, "tolerance": {"max_lit_s": IDLE_MIN_LIT_S}, "n_events": 0,
           "ci": None, "notes": [], "flags": [], "requirement_checks": {}, "events_ms": []}
    fi = timing.median_interval_ms or 33.3
    lit_s: dict[str, float] = {}
    valid_s: dict[str, float] = {}
    for c, led in enumerate(leds):
        col = bits[:, c].tolist()
        runs = stable_runs([None if v < 0 else int(v) for v in col], t_ms, 3)
        lit_s[str(led)] = round(sum(r.t1 - r.t0 + fi for r in runs if r.value == 1) / 1000.0, 3)
        valid_s[str(led)] = round(float((bits[:, c] >= 0).sum()) * fi / 1000.0, 3)
    res["measured"] = {"lit_s": lit_s, "valid_s": valid_s}
    if any(v >= IDLE_MIN_LIT_S for v in lit_s.values()):
        res["status"] = "failed"
        res["notes"].append("unused LED(s) " + ", ".join(k for k, v in lit_s.items() if v >= IDLE_MIN_LIT_S) + " were lit")
    elif valid_s and min(valid_s.values()) >= IDLE_MIN_VALID_S:
        res["status"] = "passed"
    else:
        res["notes"].append("not enough reliable frames of the unused LEDs")
    res["requirement_checks"] = {"R-IDLE": res["status"]}
    return res


# ----------------------------------------------------------------------------- requirements & verdict

_PHYS = {"passed": "observed_pass", "failed": "observed_fail", "inconclusive": "inconclusive",
         "not_observed": "not_observed"}


def requirement_report(requirements: list[Requirement], observations: list[Observation],
                       results: dict[str, dict]) -> list[dict]:
    """physical_status for every requirement; never upgrades simulation-only requirements."""
    out = []
    for req in requirements:
        entry = {"id": req.id, "text": req.text, "methods": list(req.methods), "physical_status": "not_observed",
                 "evidence": [], "note": req.physical_note}
        if "physical" not in req.methods:
            entry["physical_status"] = "not_observable"
            entry["note"] = req.physical_note or "verified in simulation only"
            out.append(entry)
            continue
        refs = [o for o in observations if req.id in o.requirement_ids]
        statuses: list[str] = []
        for res in results.values():
            chk = res.get("requirement_checks", {})
            if req.id in chk:
                statuses.append(chk[req.id]); entry["evidence"].append(res["id"])
            elif req.id in res.get("requirement_ids", []) and res["status"] != "not_observable":
                statuses.append(res["status"]); entry["evidence"].append(res["id"])
        if refs and all(not o.camera_verifiable for o in refs):
            entry["physical_status"] = "not_observable"
            entry["note"] = "; ".join(sorted({o.reason_not_verifiable or "not camera verifiable" for o in refs}))
            out.append(entry)
            continue
        statuses = [s for s in statuses if s != "not_observable"]
        if any(s == "failed" for s in statuses):
            entry["physical_status"] = "observed_fail"
        elif any(s == "passed" for s in statuses):
            entry["physical_status"] = "observed_pass"
        elif any(s == "inconclusive" for s in statuses):
            entry["physical_status"] = "inconclusive"
        else:
            entry["physical_status"] = "not_observed"
            if not refs and not statuses:
                entry["note"] = (req.physical_note or "") + ("; " if req.physical_note else "") + \
                    "no guided camera phase exercises this requirement (covered by simulation)"
        out.append(entry)
    return out


def overall_verdict(results: list[dict], any_frames: bool) -> tuple[str, str]:
    if not any_frames:
        return "unavailable", "No camera frames were received, so nothing could be observed physically."
    considered = [r for r in results if r["status"] != "not_observable"]
    if not considered:
        return "inconclusive", "No observation of this design is camera-verifiable."
    failed = [r for r in considered if r["status"] == "failed"]
    missing = [r for r in considered if r["status"] == "not_observed"]

    def describe(r: dict) -> str:
        m = r.get("measured", {})
        if r["kind"] in ("step_rate", "toggle_rate", "count_sequence") and m.get("hz") is not None:
            return f"{r['id']} {m['hz']:.3g}/s (expected {r['expected'].get('hz'):g})"
        if r["kind"] == "rate_ratio" and m.get("ratio") is not None:
            return f"{r['id']} {m['ratio']:.2f}:1 (expected {r['expected'].get('ratio'):g}:1)"
        return r["id"]

    if failed:
        why = "; ".join(f"{describe(r)}: {r['notes'][0] if r['notes'] else 'outside tolerance'}" for r in failed)
        return "failed", f"Physical behaviour does not match the specification: {why}."
    if all(r["status"] == "passed" for r in considered):
        return "passed", "Camera observations match the specification: " + ", ".join(describe(r) for r in considered) + "."
    parts = []
    if missing:
        parts.append("not measured: " + ", ".join(r["id"] for r in missing))
    inc = [r for r in considered if r["status"] == "inconclusive"]
    if inc:
        parts.append("; ".join(f"{r['id']}: {r['notes'][0] if r['notes'] else 'insufficient evidence'}" for r in inc))
    return "inconclusive", "Camera evidence is insufficient for a verdict (" + "; ".join(parts) + ")."
