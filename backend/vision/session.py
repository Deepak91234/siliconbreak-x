"""VisionSession: guided webcam verification of one programmed design (contracts section D).

Flow
----
1. The browser streams JPEG frames with performance.now() timestamps
   (`ingest_frame`). Every frame updates the live LED states for the preview.
2. The user marks the LED row (`auto_roi` -> `set_roi`, or `set_roi` directly).
   The frame current at that moment becomes the camera-motion reference.
3. The user walks through the guided phases (`plan`, derived from
   derive_observations(spec)): start_phase -> hold the instructed inputs ->
   end_phase. Phase boundaries are taken from frame timestamps, never from the
   server clock.
4. `finish` re-classifies every recorded frame with the calibration learned
   from the whole session, measures each observation, maps the outcome onto
   every requirement and writes the evidence files.

Only frames that really arrived are analysed; when evidence is missing or poor
the verdict says so (inconclusive / unavailable) instead of guessing.
"""

from __future__ import annotations

import csv
import json
import math
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import cv2
import numpy as np
from pydantic import BaseModel, Field

from backend.specifications.derive import Observation, derive_observations, derive_requirements
from backend.specifications.ir import (
    BlinkerBehavior,
    CounterBehavior,
    DesignSpec,
    LedPatternBehavior,
    TrafficLightBehavior,
)

from . import analysis as an
from .detector import (
    DARK_BRIGHTNESS,
    MIN_TRACK_RESPONSE,
    OVEREXPOSED_FRACTION,
    LedCalibration,
    MotionMonitor,
    OnlineDetector,
    calibrate,
    classify_series,
    contrast_flags,
    frame_brightness,
    move_threshold_px,
    roi_levels,
    union_rect,
)
from .frames import FrameClock, decode_jpeg, green_signal, motion_thumbnail, timing_stats
from .roi import LedRoi, RoiConfig, draw_overlay, median_roi_width_px, pixel_rects, refine_toward_blobs, split_box

__all__ = ["LedRoi", "RoiConfig", "FrameResult", "PhasePlan", "VerificationResult", "VisionSession", "build_plan"]

CALIBRATE_PHASE = "calibrate"
CALIBRATE_MIN_S = 3.0
STATIC_PHASE_MIN_S = 2.0
# 30 minutes at 30 fps; a guided session takes a few minutes. Beyond this the
# session refuses frames instead of silently discarding evidence.
MAX_RECORDED_FRAMES = 54_000


class PhasePlan(BaseModel):
    id: str
    observation_id: str | None
    title: str
    instruction: str
    min_duration_s: float
    conditions: list[dict] = Field(default_factory=list)


class FrameResult(BaseModel):
    t_ms: float
    states: dict[int, int | None]
    levels: dict[int, float]
    quality: dict


class VerificationResult(BaseModel):
    status: Literal["passed", "failed", "inconclusive", "unavailable"]
    summary: str
    observations: list[dict]
    requirements: list[dict]
    quality: dict
    evidence: list[dict]


# ----------------------------------------------------------------------------- plan


def _rate_title(spec: DesignSpec, obs: Observation) -> str:
    mode, hz = obs.expected.get("mode"), obs.expected.get("hz")
    if obs.kind == "toggle_rate":
        return f"{str(mode).capitalize()} blinking ({hz:g} Hz)"
    unit = "step/s" if hz == 1 else "steps/s"
    return f"{str(mode).capitalize()} rate ({hz:g} {unit})"


def _cond_instruction(conds: list[dict], min_s: float) -> str:
    texts = [c["instruction"] for c in conds]
    holds = any(c["instruction"].lower().startswith("press and hold") for c in conds)
    if not texts:
        return f"Leave the inputs alone, start the phase and keep the LEDs in view for at least {min_s:g} s."
    lead = "; ".join(texts)
    if holds:
        return f"{lead}, then click Start. Keep holding until the phase ends (at least {min_s:g} s)."
    return f"{lead}, then click Start. Keep it that way for at least {min_s:g} s."


def _static_patterns(spec: DesignSpec) -> list[dict[str, int]]:
    b = spec.behavior
    assert isinstance(b, LedPatternBehavior)
    n = len(b.source_inputs)
    pats = [{s: 0 for s in b.source_inputs}, {s: int(i % 2 == 0) for i, s in enumerate(b.source_inputs)}]
    if n >= 2:
        pats.append({s: int(i % 2 == 1) for i, s in enumerate(b.source_inputs)})
    return pats


def _static_conditions(spec: DesignSpec, levels: dict[str, int]) -> tuple[list[dict], str]:
    conds = []
    up, down = [], []
    for name, lvl in levels.items():
        inp = spec.input(name)
        sig = inp.board_signal
        if inp.port == "sw":
            text = f"Set {sig} {'up (on)' if lvl else 'down (off)'}"
        else:
            text = f"{'Press and hold' if lvl else 'Release'} {sig.upper()}"
        (up if lvl else down).append(sig if inp.port == "sw" else sig.upper())
        conds.append({"input": name, "board_signal": sig, "level": lvl, "instruction": text})
    parts = []
    if up:
        parts.append(", ".join(up) + " up/pressed")
    if down:
        parts.append(", ".join(down) + " down/released")
    return conds, "Set " + " and ".join(parts)


def build_plan(spec: DesignSpec, observations: list[Observation]) -> list[PhasePlan]:
    """Guided phases: calibrate, then one phase per camera-verifiable measurement.

    Rate phases start with the default (unconditional) mode -- the baseline the
    user gets without touching anything -- followed by the conditional modes.
    Ratios need no phase of their own; they are computed at finish.
    """
    b = spec.behavior
    static = isinstance(b, LedPatternBehavior) and b.pattern in ("mirror_inputs", "invert_inputs")
    cal_text = ("Point the camera at the LEDs and keep it still. Flip the source inputs on and off a couple of "
                "times so every LED is seen lit and unlit." if static else
                "Point the camera at the LEDs and keep it still. Let the design run without touching the board "
                "so CircuitLoop can learn what lit and unlit LEDs look like.")
    plan = [PhasePlan(id=CALIBRATE_PHASE, observation_id=None, title="Calibrate LED detection",
                      instruction=cal_text + f" At least {CALIBRATE_MIN_S:g} s.", min_duration_s=CALIBRATE_MIN_S)]
    modes = [m.name for m in getattr(b, "rate_modes", []) or []]
    order = (modes[-1:] + modes[:-1]) if modes else []
    rate_obs = [o for o in observations if o.kind in ("step_rate", "toggle_rate", "count_sequence")]
    rate_obs.sort(key=lambda o: order.index(o.expected.get("mode")) if o.expected.get("mode") in order else 99)
    for o in rate_obs:
        if not o.camera_verifiable:
            continue
        conds = [c.model_dump() for c in o.conditions]
        plan.append(PhasePlan(id=o.id, observation_id=o.id, title=_rate_title(spec, o),
                              instruction=_cond_instruction(conds, o.min_duration_s),
                              min_duration_s=o.min_duration_s, conditions=conds))
    for o in observations:
        if o.kind == "state_durations" and o.camera_verifiable:
            plan.append(PhasePlan(id=o.id, observation_id=o.id, title="Light sequence and durations",
                                  instruction=f"Do not press any buttons. Click Start and let the sequence run for at "
                                              f"least {o.min_duration_s:g} s (two full cycles).",
                                  min_duration_s=o.min_duration_s))
        if o.kind == "static_pattern" and o.camera_verifiable:
            pats = _static_patterns(spec)
            min_s = max(STATIC_PHASE_MIN_S, round(o.min_duration_s / len(pats), 1))
            for k, levels in enumerate(pats, start=1):
                conds, text = _static_conditions(spec, levels)
                plan.append(PhasePlan(id=f"{o.id}-{k}", observation_id=o.id, title=f"Input setting {k} of {len(pats)}",
                                      instruction=f"{text}, then click Start. Keep the setting for at least {min_s:g} s.",
                                      min_duration_s=min_s, conditions=conds))
    return plan


# ----------------------------------------------------------------------------- records


@dataclass
class FrameRecord:
    t_ms: float
    epoch: int
    levels: dict[int, float]
    reliable: bool
    dark: bool
    flags: tuple[str, ...]
    shift_px: float | None
    run: int | None


@dataclass
class PhaseRun:
    index: int
    plan: PhasePlan
    start_ms: float | None
    end_ms: float | None = None
    first_frame: int | None = None
    last_frame: int | None = None
    snapshot: str | None = None
    summary: dict | None = None

    @property
    def active(self) -> bool:
        return self.end_ms is None


@dataclass
class _Classified:
    """All recorded frames classified with per-epoch calibration."""

    t_ms: np.ndarray
    states: np.ndarray                       # (n, 16) int8, -1 unknown / LED without ROI
    levels: np.ndarray                       # (n, 16) float, nan without ROI
    reliable: np.ndarray
    dark: np.ndarray
    calibration: dict[int, dict[int, LedCalibration]] = field(default_factory=dict)
    epoch_flags: dict[int, list[str]] = field(default_factory=dict)


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:60]


class VisionSession:
    """One webcam verification session for one design revision."""

    def __init__(self, session_id: str, spec: DesignSpec, evidence_dir: Path) -> None:
        self.id = session_id
        self.spec = spec
        self.evidence_dir = Path(evidence_dir)
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.requirements = derive_requirements(spec)
        self.observations = derive_observations(spec)
        self.plan: list[PhasePlan] = build_plan(spec, self.observations)
        self._plan_by_id = {p.id: p for p in self.plan}
        self.status: str = "setup"
        self.result: VerificationResult | None = None
        self.roi: RoiConfig | None = None
        self._epoch = 0
        self._clock = FrameClock()
        self._detector: OnlineDetector | None = None
        self._motion = MotionMonitor()
        self._pending_ref: tuple[np.ndarray, float] | None = None
        self._last_thumb: tuple[np.ndarray, float] | None = None
        self._last_bgr: np.ndarray | None = None
        self._last_shape: tuple[int, int] | None = None
        self._frames: list[FrameRecord] = []
        self._runs: list[PhaseRun] = []
        self._active: PhaseRun | None = None
        self._frames_received = 0
        self._frames_rejected = 0
        self._live: dict = {}
        self._cal_flags: list[str] = []
        self._lock = threading.RLock()
        out = spec.output(spec.behavior.output)
        self._design_leds = list(out.indices)
        used = {i for o in spec.outputs for i in o.indices}
        self._unused_leds = [i for i in range(16) if i not in used]

    # ------------------------------------------------------------------ ROI
    def set_roi(self, roi: RoiConfig) -> None:
        roi = RoiConfig.model_validate(roi if isinstance(roi, dict) else roi.model_dump())
        with self._lock:
            self.roi = roi
            self._epoch += 1
            self._detector = OnlineDetector(roi.indices())
            self._motion.clear()
            ref = self._pending_ref or self._last_thumb
            self._pending_ref = None
            if ref is not None:
                self._set_motion_reference(ref[0], ref[1])

    def auto_roi(self, jpeg: bytes, box: tuple[float, float, float, float], count: int, first_led: int) -> RoiConfig:
        """Split the user's box into `count` cells (LED indices right-to-left from first_led), refined to blobs."""
        bgr = decode_jpeg(jpeg)
        roi = split_box(tuple(box), int(count), int(first_led))  # type: ignore[arg-type]
        roi = refine_toward_blobs(bgr, roi, tuple(box))  # type: ignore[arg-type]
        with self._lock:
            self._pending_ref = motion_thumbnail(bgr)
            self._last_shape = bgr.shape[:2]
        return roi

    def _set_motion_reference(self, thumb: np.ndarray, scale: float) -> None:
        self._motion.set_reference(thumb, scale)
        if self.roi is None or self._motion._window is None:
            return
        # Exclude the LED strip from the motion estimate: it is exactly where
        # image content legitimately changes, and blinking LEDs bias the
        # correlation peak by a pixel or two otherwise.
        th, tw = thumb.shape
        w_full, h_full = tw * scale, th * scale
        rects = pixel_rects(self.roi, int(round(w_full)), int(round(h_full)))
        x0, y0, x1, y1 = union_rect(rects)
        pad = 2
        mask = np.ones((th, tw), np.float32)
        mask[max(0, int(y0 / scale) - pad):min(th, int(math.ceil(y1 / scale)) + pad),
             max(0, int(x0 / scale) - pad):min(tw, int(math.ceil(x1 / scale)) + pad)] = 0.0
        mask = cv2.GaussianBlur(mask, (0, 0), 1.5)
        if float(mask.mean()) > 0.3:      # keep enough of the scene to track
            self._motion._window = (self._motion._window * mask).astype(np.float32)

    # ------------------------------------------------------------------ frames
    def ingest_frame(self, t_ms: float, jpeg: bytes) -> FrameResult:
        bgr = decode_jpeg(jpeg)
        with self._lock:
            if not self._clock.accept(t_ms):
                self._frames_rejected += 1
                raise ValueError(f"frame timestamp {t_ms!r} is not after the previous frame")
            if self.roi is not None and len(self._frames) >= MAX_RECORDED_FRAMES:
                raise RuntimeError("session recording limit reached; finish the verification")
            self._frames_received += 1
            thumb, scale = motion_thumbnail(bgr)
            self._last_thumb = (thumb, scale)
            self._last_bgr = bgr
            shape = bgr.shape[:2]
            resized = self._last_shape is not None and self._last_shape != shape
            self._last_shape = shape
            brightness, clipped = frame_brightness(bgr)
            flags: list[str] = []
            if brightness < DARK_BRIGHTNESS:
                flags.append("too_dark")
            if clipped > OVEREXPOSED_FRACTION:
                flags.append("overexposed")
            states: dict[int, int | None] = {}
            levels: dict[int, float] = {}
            quality: dict = {"brightness": round(brightness, 4), "clipped_fraction": round(clipped, 4)}
            if self.roi is None or self._detector is None:
                flags.append("no_roi")
            else:
                if resized or self._motion.reference is None:
                    self._set_motion_reference(thumb, scale)
                reading = self._motion.measure(thumb)
                h, w = shape
                thr = move_threshold_px(median_roi_width_px(self.roi, w))
                moved = False
                if reading is not None:
                    quality.update({"shift_px": round(reading.shift_px, 2), "dx_px": round(reading.dx_px, 2),
                                    "dy_px": round(reading.dy_px, 2), "tracking_response": round(reading.response, 3),
                                    "move_threshold_px": round(thr, 2)})
                    if reading.shift_px > thr:
                        moved = True
                        flags.append("camera_moved")
                    if reading.response < MIN_TRACK_RESPONSE:
                        moved = True
                        flags.append("tracking_lost")
                rects = pixel_rects(self.roi, w, h)
                x0, y0, x1, y1 = union_rect(rects)
                levels = roi_levels(green_signal(bgr[y0:y1, x0:x1]), rects, (x0, y0))
                states = self._detector.update(levels, reliable=not moved)
                if self._detector.frames % 15 == 1:
                    hist = {i: np.fromiter(hh, float) for i, hh in self._detector._hist.items()}
                    self._cal_flags = contrast_flags(self._detector.calibration, hist, self._detector.frames)
                flags.extend(self._cal_flags)
                run_idx = self._active.index if self._active is not None else None
                rec = FrameRecord(float(t_ms), self._epoch, levels, not moved, brightness < DARK_BRIGHTNESS,
                                  tuple(flags), reading.shift_px if reading else None, run_idx)
                self._frames.append(rec)
                if self._active is not None:
                    run = self._active
                    if run.first_frame is None:
                        run.first_frame = len(self._frames) - 1
                        if run.start_ms is None:
                            run.start_ms = float(t_ms)
                        run.snapshot = self._write_snapshot(bgr, states, f"{run.plan.id}_{run.index}",
                                                            f"{run.plan.title} @ {t_ms:.0f} ms")
                    run.last_frame = len(self._frames) - 1
            stats = self._clock.recent_stats()
            quality.update({"flags": sorted(set(flags)), "fps": stats.as_dict()["fps"],
                            "jitter_ms": stats.as_dict()["jitter_ms"], "dropped_frames": stats.dropped_frames,
                            "calibrated": sum(1 for c in (self._detector.calibration.values() if self._detector else [])
                                              if c.source != "none"),
                            "phase": self._active.plan.id if self._active else None,
                            "value": self._decode_live(states)})
            self._live = {"t_ms": float(t_ms), "states": states, "levels": {k: round(v, 4) for k, v in levels.items()},
                          "quality": quality}
            return FrameResult(t_ms=float(t_ms), states=states, levels={k: round(v, 4) for k, v in levels.items()},
                               quality=quality)

    def _decode_live(self, states: dict[int, int | None]) -> int | None:
        if not states:
            return None
        v = 0
        for bit, led in enumerate(self._design_leds):
            s = states.get(led)
            if s is None:
                return None
            v |= s << bit
        return v

    def _write_snapshot(self, bgr: np.ndarray, states: dict[int, int | None], name: str, label: str) -> str | None:
        try:
            d = self.evidence_dir / "snapshots"
            d.mkdir(parents=True, exist_ok=True)
            p = d / f"{_safe(name)}.jpg"
            cv2.imwrite(str(p), draw_overlay(bgr, self.roi, states, label))
            return str(p)
        except (OSError, cv2.error):
            return None  # evidence is best-effort; the measurement does not depend on it

    # ------------------------------------------------------------------ phases
    def start_phase(self, phase_id: str) -> PhasePlan:
        with self._lock:
            plan = self._plan_by_id.get(phase_id)
            if plan is None:
                raise KeyError(f"unknown phase '{phase_id}'")
            if self.roi is None:
                raise RuntimeError("mark the LED regions before starting a phase")
            if self._active is not None:
                self._end_active()
            run = PhaseRun(index=len(self._runs), plan=plan, start_ms=self._clock.last_t_ms)
            self._runs.append(run)
            self._active = run
            self.status = "running"
            self.result = None
            return plan

    def end_phase(self) -> dict:
        with self._lock:
            if self._active is None:
                raise RuntimeError("no phase is running")
            return self._end_active()

    def _end_active(self) -> dict:
        run = self._active
        assert run is not None
        run.end_ms = self._clock.last_t_ms if run.last_frame is not None else run.start_ms
        self._active = None
        cl = self._classify()
        run.summary = self._measure_run(run, cl)
        return run.summary

    # ------------------------------------------------------------------ analysis
    def _classify(self) -> _Classified:
        n = len(self._frames)
        t = np.array([f.t_ms for f in self._frames], dtype=np.float64)
        states = np.full((n, 16), -1, dtype=np.int8)
        levels = np.full((n, 16), np.nan, dtype=np.float64)
        reliable = np.array([f.reliable for f in self._frames], dtype=bool)
        dark = np.array([f.dark for f in self._frames], dtype=bool)
        for i, f in enumerate(self._frames):
            for led, v in f.levels.items():
                levels[i, led] = v
        out = _Classified(t, states, levels, reliable, dark)
        epochs = np.array([f.epoch for f in self._frames], dtype=np.int64)
        for ep in sorted(set(epochs.tolist())):
            sel = np.nonzero(epochs == ep)[0]
            leds = [i for i in range(16) if np.isfinite(levels[sel, i]).any()]
            ok = sel[reliable[sel]]
            hist = {i: levels[ok, i][np.isfinite(levels[ok, i])] for i in leds}
            cals = calibrate(hist)
            out.calibration[ep] = cals
            out.epoch_flags[ep] = contrast_flags(cals, hist, int(ok.size))
            for i in leds:
                col = classify_series(levels[sel, i], reliable[sel], cals.get(i))
                states[sel, i] = np.array([-1 if s is None else s for s in col], dtype=np.int8)
        return out

    def _series(self, run: PhaseRun, cl: _Classified, leds: list[int], expected_value: int | None = None) -> an.PhaseSeries:
        idx = np.array([i for i, f in enumerate(self._frames) if f.run == run.index], dtype=np.int64)
        start = run.start_ms if run.start_ms is not None else (float(cl.t_ms[idx[0]]) if idx.size else 0.0)
        t_all = cl.t_ms[idx] if idx.size else np.zeros(0)
        duration = (float(t_all[-1]) - start) / 1000.0 if idx.size else 0.0
        settle = 0.0 if run.plan.id == CALIBRATE_PHASE else an.SETTLE_MS
        keep = idx[t_all >= start + settle] if idx.size else idx
        flags = sorted({fl for ep in {self._frames[i].epoch for i in idx.tolist()} for fl in cl.epoch_flags.get(ep, [])})
        missing = [led for led in leds if self.roi is None or self.roi.get(led) is None]
        if missing:
            flags.append("missing_roi")
        return an.PhaseSeries(
            phase_id=run.plan.id,
            t_ms=cl.t_ms[keep],
            bits=cl.states[np.ix_(keep, leds)] if keep.size else np.zeros((0, len(leds)), np.int8),
            reliable=cl.reliable[keep],
            dark=cl.dark[keep],
            duration_s=duration,
            min_duration_s=run.plan.min_duration_s,
            timing=timing_stats(t_all),
            calibration_flags=flags,
            expected_value=expected_value,
        )

    def _obs(self, obs_id: str | None) -> Observation | None:
        return next((o for o in self.observations if o.id == obs_id), None)

    def _measure_run(self, run: PhaseRun, cl: _Classified) -> dict:
        n_frames = sum(1 for f in self._frames if f.run == run.index)
        base = {"phase_id": run.plan.id, "run": run.index, "start_ms": run.start_ms, "end_ms": run.end_ms,
                "frames": n_frames, "snapshot": run.snapshot}
        obs = self._obs(run.plan.observation_id)
        if obs is None:   # calibration phase
            leds = self.roi.indices() if self.roi else []
            series = self._series(run, cl, leds)
            ep = self._epoch
            cals = cl.calibration.get(ep, {})
            calibrated = sorted(i for i, c in cals.items() if c.source != "none")
            ok = bool(calibrated) and series.duration_s >= run.plan.min_duration_s
            return {**base, "duration_s": round(series.duration_s, 3), "status": "passed" if ok else "inconclusive",
                    "calibrated_leds": calibrated, "flags": cl.epoch_flags.get(ep, []),
                    "calibration": {str(i): c.as_dict() for i, c in cals.items()},
                    "notes": [] if ok else ["no LED has been seen both lit and unlit yet" if not calibrated
                                            else "calibration phase was shorter than planned"]}
        res = self._measure_obs_run(obs, run, cl)
        return {**base, "duration_s": res.get("_duration_s"), "status": res["status"], "measured": res["measured"],
                "notes": res["notes"], "flags": res["flags"], "observation_id": obs.id}

    def _measure_obs_run(self, obs: Observation, run: PhaseRun, cl: _Classified) -> dict:
        exp_val = None
        if obs.kind == "static_pattern":
            exp_val = an.static_expected_value(self.spec, {c["input"]: int(c["level"]) for c in run.plan.conditions})
        series = self._series(run, cl, list(obs.leds), exp_val)
        if obs.kind in ("step_rate", "count_sequence"):
            res = an.measure_step_rate(obs, self.spec, series)
        elif obs.kind == "toggle_rate":
            res = an.measure_toggle_rate(obs, self.spec, series)
        elif obs.kind == "state_durations":
            res = an.measure_state_durations(obs, self.spec, series)
        elif obs.kind == "static_pattern":
            res = an.measure_static_phase(obs, series)
        else:
            raise ValueError(f"observation kind {obs.kind} has no phase measurement")
        res["phase_id"] = run.plan.id
        res["_duration_s"] = round(series.duration_s, 3)
        res["measured"]["phase_duration_s"] = round(series.duration_s, 3)
        res["measured"]["frames"] = int(series.t_ms.size)
        res["measured"]["fps"] = series.timing.as_dict()["fps"]
        return res

    def _latest_run(self, phase_id: str) -> PhaseRun | None:
        runs = [r for r in self._runs if r.plan.id == phase_id and r.first_frame is not None]
        return runs[-1] if runs else None

    # ------------------------------------------------------------------ finish
    def finish(self) -> VerificationResult:
        with self._lock:
            if self._active is not None:
                self._end_active()
            cl = self._classify()
            results: dict[str, dict] = {}
            for obs in self.observations:
                if obs.kind in ("rate_ratio", "static_pattern"):
                    continue
                if not obs.camera_verifiable:
                    results[obs.id] = self._not_observable(obs)
                    continue
                run = self._latest_run(obs.id)
                if run is None:
                    results[obs.id] = self._not_observed(obs)
                    continue
                results[obs.id] = self._measure_obs_run(obs, run, cl)
            for obs in self.observations:
                if obs.kind == "static_pattern":
                    phases = [p for p in self.plan if p.observation_id == obs.id]
                    per = [self._measure_obs_run(obs, r, cl) for p in phases if (r := self._latest_run(p.id))]
                    results[obs.id] = (an.combine_static(obs, per, len(phases)) if per else self._not_observed(obs)) \
                        if obs.camera_verifiable else self._not_observable(obs)
                elif obs.kind == "rate_ratio":
                    if not obs.camera_verifiable:
                        results[obs.id] = self._not_observable(obs)
                        continue
                    num = results.get(obs.expected.get("numerator_observation", ""))
                    den = results.get(obs.expected.get("denominator_observation", ""))
                    if (num is None or num["status"] == "not_observed") and (den is None or den["status"] == "not_observed"):
                        results[obs.id] = self._not_observed(obs)
                    else:
                        results[obs.id] = an.measure_ratio(obs, num, den)
            idle_leds = [i for i in self._unused_leds if self.roi is not None and self.roi.get(i) is not None]
            if idle_leds and len(cl.t_ms):
                ok = cl.reliable & ~cl.dark    # a too-dark image is no evidence that an LED is off
                results["O-IDLE"] = an.measure_idle(idle_leds, cl.t_ms[ok], cl.states[np.ix_(np.nonzero(ok)[0], idle_leds)],
                                                    timing_stats(cl.t_ms))
            for r in results.values():
                r.pop("_duration_s", None)
            reqs = an.requirement_report(self.requirements, self.observations, results)
            for r in reqs:
                if r["id"] == "R-IDLE" and "O-IDLE" not in results and r["physical_status"] == "not_observed":
                    r["note"] = "the LED regions do not include the unused LEDs"
            ordered = [results[o.id] for o in self.observations if o.id in results]
            if "O-IDLE" in results:
                ordered.append(results["O-IDLE"])
            status, summary = an.overall_verdict(ordered, self._frames_received > 0)
            if status == "inconclusive" and self.roi is None and self._frames_received:
                summary = "Camera frames arrived but the LED regions were never marked, so no LED could be measured."
            quality = self._quality(cl, ordered)
            evidence = self._write_evidence(cl, ordered, reqs, status, summary, quality)
            self.result = VerificationResult(status=status, summary=summary, observations=ordered, requirements=reqs,
                                             quality=quality, evidence=evidence)
            self._write_json(self.result)
            self.status = "finished"
            return self.result

    def _not_observed(self, obs: Observation) -> dict:
        r = an._result(obs, None)
        r["status"] = "not_observed"
        r["notes"].append("this phase was not run")
        return r

    def _not_observable(self, obs: Observation) -> dict:
        r = an._result(obs, None)
        r["status"] = "not_observable"
        r["notes"].append(obs.reason_not_verifiable or "not camera verifiable")
        return r

    def _quality(self, cl: _Classified, results: list[dict]) -> dict:
        n = len(self._frames)
        counts: dict[str, int] = {}
        for f in self._frames:
            for fl in set(f.flags):
                counts[fl] = counts.get(fl, 0) + 1
        flags = sorted({fl for r in results for fl in r.get("flags", [])} |
                       {fl for fl, c in counts.items() if n and c / n > 0.05 and fl not in ("partially_calibrated",)})
        last_ep = max(cl.calibration) if cl.calibration else None
        return {
            "frames_received": self._frames_received,
            "frames_recorded": n,
            "frames_rejected": self._frames_rejected,
            "timing": timing_stats(cl.t_ms).as_dict() if n else timing_stats([]).as_dict(),
            "flags": flags,
            "flag_fractions": {k: round(v / n, 4) for k, v in sorted(counts.items())} if n else {},
            "camera_moved_frames": int((~cl.reliable).sum()) if n else 0,
            "roi_set": self.roi is not None,
            "roi_epochs": self._epoch,
            "calibration": {str(i): c.as_dict() for i, c in cl.calibration.get(last_ep, {}).items()} if last_ep else {},
            "phases_run": [r.plan.id for r in self._runs if r.first_frame is not None],
        }

    # ------------------------------------------------------------------ evidence
    def _write_evidence(self, cl: _Classified, results: list[dict], reqs: list[dict], status: str, summary: str,
                        quality: dict) -> list[dict]:
        ev: list[dict] = []
        d = self.evidence_dir
        try:
            p = d / "frames.csv"
            self._write_csv(p, cl)
            ev.append({"name": "frames.csv", "path": str(p), "kind": "csv"})
        except OSError:
            pass
        if len(cl.t_ms):
            try:
                p = d / "led_levels.png"
                self._write_plot(p, cl, results)
                ev.append({"name": "led_levels.png", "path": str(p), "kind": "plot"})
            except (OSError, ValueError, RuntimeError):
                pass
        ev.append({"name": "measurements.json", "path": str(d / "measurements.json"), "kind": "json"})
        if self._last_bgr is not None:
            snap = self._write_snapshot(self._last_bgr, self._live.get("states", {}), "final", f"finish: {status}")
            if snap:
                ev.append({"name": "final.jpg", "path": snap, "kind": "snapshot"})
        for r in self._runs:
            if r.snapshot and Path(r.snapshot).exists():
                ev.append({"name": Path(r.snapshot).name, "path": r.snapshot, "kind": "snapshot"})
        return ev

    def _write_json(self, result: VerificationResult) -> None:
        payload = {
            "session_id": self.id,
            "spec_title": self.spec.title,
            "result": result.model_dump(),
            "plan": [p.model_dump() for p in self.plan],
            "phases": [{"phase_id": r.plan.id, "run": r.index, "start_ms": r.start_ms, "end_ms": r.end_ms,
                        "summary": r.summary} for r in self._runs],
            "roi": self.roi.model_dump() if self.roi else None,
            "method": {"settle_ms": an.SETTLE_MS, "min_stable_frames": an.MIN_STABLE_FRAMES,
                       "min_rate_events": an.MIN_RATE_EVENTS, "rel_tol_floor": an.REL_TOL_FLOOR,
                       "ambiguous_factor": an.AMBIGUOUS_FACTOR, "sigma_k": an.SIGMA_K},
        }
        try:
            (self.evidence_dir / "measurements.json").write_text(json.dumps(payload, indent=2, default=str))
        except OSError:
            pass

    def _write_csv(self, path: Path, cl: _Classified) -> None:
        leds = sorted({led for f in self._frames for led in f.levels}, reverse=True)
        run_phase = {r.index: r.plan.id for r in self._runs}
        with path.open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["t_ms", "epoch", "phase", "reliable", "flags"] +
                       [c for led in leds for c in (f"level_led{led}", f"state_led{led}")] + ["value"])
            weights = [(bit, led) for bit, led in enumerate(self._design_leds)]
            for i, f in enumerate(self._frames):
                row = [f"{f.t_ms:.3f}", f.epoch, run_phase.get(f.run, "") if f.run is not None else "",
                       int(f.reliable), ";".join(f.flags)]
                for led in leds:
                    lv = cl.levels[i, led]
                    st = int(cl.states[i, led])
                    row += ["" if not np.isfinite(lv) else f"{lv:.4f}", "" if st < 0 else st]
                bits = [int(cl.states[i, led]) for _b, led in weights]
                row.append("" if any(b < 0 for b in bits) else sum(b << k for k, b in enumerate(bits)))
                w.writerow(row)

    def _write_plot(self, path: Path, cl: _Classified, results: list[dict]) -> None:
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure

        leds = sorted({led for f in self._frames for led in f.levels}, reverse=True)
        t0 = float(cl.t_ms[0])
        ts = (cl.t_ms - t0) / 1000.0
        fig = Figure(figsize=(11, 1.6 + 0.42 * max(4, len(leds))), dpi=100)
        FigureCanvasAgg(fig)
        ax = fig.add_subplot(111)
        last_ep = max(cl.calibration) if cl.calibration else None
        cals = cl.calibration.get(last_ep, {}) if last_ep is not None else {}
        for row, led in enumerate(leds):
            lv = cl.levels[:, led]
            c = cals.get(led)
            if c is not None and c.separation:
                norm = (lv - c.off) / c.separation
            else:
                finite = lv[np.isfinite(lv)]
                lo, hi = (float(finite.min()), float(finite.max())) if finite.size else (0.0, 1.0)
                norm = (lv - lo) / max(hi - lo, 1e-6)
            y = row + 0.75 * np.clip(norm, -0.2, 1.3)
            ax.plot(ts, y, lw=0.8, color="#2e7d32" if led in self._design_leds else "#757575")
            on = cl.states[:, led] == 1
            ax.scatter(ts[on], np.full(on.sum(), row + 0.92), s=2, color="#43a047")
        ax.set_yticks([r + 0.4 for r in range(len(leds))])
        ax.set_yticklabels([f"LED{led}" for led in leds])
        palette = ["#1e88e5", "#fb8c00", "#8e24aa", "#00897b", "#e53935", "#6d4c41"]
        for k, r in enumerate(self._runs):
            if r.first_frame is None:
                continue
            a = ((r.start_ms if r.start_ms is not None else cl.t_ms[r.first_frame]) - t0) / 1000.0
            b = (cl.t_ms[r.last_frame] - t0) / 1000.0
            col = palette[k % len(palette)]
            ax.axvspan(a, b, color=col, alpha=0.10)
            ax.text(a, len(leds) + 0.1, r.plan.id, fontsize=7, color=col, va="bottom")
        for res in results:
            evs = res.get("events_ms") or []
            if evs:
                ax.vlines([(e - t0) / 1000.0 for e in evs], -0.3, len(leds) - 0.1, colors="#d81b60",
                          linestyles=":", linewidth=0.6, alpha=0.6)
        moved = ~cl.reliable
        if moved.any():
            ax.scatter(ts[moved], np.full(moved.sum(), -0.25), s=3, color="#e53935", label="camera moved")
            ax.legend(loc="lower right", fontsize=7)
        ax.set_xlabel("time since first recorded frame (s, client clock)")
        ax.set_ylim(-0.4, len(leds) + 0.6)
        ax.set_title(f"{self.spec.title}: LED levels (normalised to calibration), phases and detected events", fontsize=9)
        fig.tight_layout()
        fig.savefig(str(path))

    # ------------------------------------------------------------------ state
    def to_dict(self) -> dict:
        with self._lock:
            active = self._active
            elapsed = None
            if active is not None and active.start_ms is not None and self._clock.last_t_ms is not None:
                elapsed = round(max(0.0, self._clock.last_t_ms - active.start_ms) / 1000.0, 2)
            stats = self._clock.recent_stats()
            return {
                "id": self.id,
                "status": self.status,
                "spec_title": self.spec.title,
                "plan": [p.model_dump() for p in self.plan],
                "current_phase": active.plan.id if active else None,
                "phase_elapsed_s": elapsed,
                "phases": [{"phase_id": r.plan.id, "run": r.index, "active": r.active,
                            "frames": (r.last_frame - r.first_frame + 1) if r.first_frame is not None and r.last_frame is not None else 0,
                            "summary": r.summary} for r in self._runs],
                "roi": self.roi.model_dump() if self.roi else None,
                "live": {"fps": stats.as_dict()["fps"], "frames": self._frames_received,
                         "recorded_frames": len(self._frames), "rejected_frames": self._frames_rejected,
                         "quality": self._live.get("quality", {}), "states": self._live.get("states", {}),
                         "levels": self._live.get("levels", {}),
                         "calibration": self._detector.summary() if self._detector else {}},
                "result": self.result.model_dump() if self.result else None,
            }
