"""Webcam observation of a security case's Basys 3 demo (physical evidence).

The board runs a bitstream CircuitLoop programmed (one variant of one audit). The
user frames all 16 LEDs, then walks through the case's demo steps (set switches,
hold the button) while the browser streams frames. For every step the session
measures which LEDs were lit and compares that with

  * what the contract requires (the case's demo `expect_leds`), and
  * what the board-level simulation of this exact variant predicted.

A leading "dark" step (no button held, all LEDs off) gives an off reference.
LED detection reuses CircuitLoop's webcam pipeline: per-LED green level in each
region, calibration learned from the whole session (LEDs that never toggle borrow
the on/off levels of LEDs that did), hysteresis, and camera-motion rejection.

Verdicts are only what the frames show: a step whose LEDs could not all be read
reliably is inconclusive, never guessed.
"""

from __future__ import annotations

import csv
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from backend.security.cases import DemoStep
from backend.vision.detector import (DARK_BRIGHTNESS, MIN_TRACK_RESPONSE, MotionMonitor, OnlineDetector, calibrate,
                                     classify_series, frame_brightness, move_threshold_px, roi_levels, union_rect)
from backend.vision.frames import FrameClock, decode_jpeg, green_signal, motion_thumbnail
from backend.vision.roi import RoiConfig, draw_overlay, median_roi_width_px, pixel_rects, refine_toward_blobs, split_box
from backend.vision.session import FrameResult

ALL_LEDS = list(range(16))
DARK_STEP = "DARK"
SETTLE_MS = 400.0          # ignore the first frames after a step starts (switch bounce, hand on button)
MIN_STEP_FRAMES = 6        # reliable, classified frames needed per LED
MIN_AGREEMENT = 0.75       # share of frames that must agree on an LED's state
MAX_FRAMES = 30_000


@dataclass
class _Frame:
    t_ms: float
    epoch: int
    levels: dict[int, float]
    reliable: bool
    dark: bool
    run: int | None


@dataclass
class _Run:
    index: int
    step_id: str
    start_ms: float | None
    end_ms: float | None = None
    frames: list[int] = field(default_factory=list)
    snapshot_frame: np.ndarray | None = None


class BoardObservationSession:
    def __init__(self, session_id: str, *, audit_id: str, variant: str, variant_label: str, steps: list[DemoStep],
                 simulated: dict[str, str], programmed: dict | None, evidence_dir: Path) -> None:
        self.id = session_id
        self.audit_id = audit_id
        self.variant = variant
        self.variant_label = variant_label
        self.steps = [DemoStep(id=DARK_STEP, title="Reference: no button held (all LEDs off)", set={}, press=None,
                               expect_leds="0x0000", expect_text="every LED dark")] + list(steps)
        self._by_id = {s.id: s for s in self.steps}
        self.simulated = dict(simulated)
        self.simulated.setdefault(DARK_STEP, "0x0000")
        self.programmed = programmed or {}
        self.evidence_dir = Path(evidence_dir)
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.status = "setup"
        self.roi: RoiConfig | None = None
        self.result: dict | None = None
        self._epoch = 0
        self._clock = FrameClock()
        self._detector: OnlineDetector | None = None
        self._motion = MotionMonitor()
        self._last_thumb: tuple[np.ndarray, float] | None = None
        self._last_bgr: np.ndarray | None = None
        self._frames: list[_Frame] = []
        self._runs: list[_Run] = []
        self._active: _Run | None = None
        self._live: dict = {}
        self._step_results: dict[str, dict] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ ROI
    def auto_roi(self, jpeg: bytes, box: tuple[float, float, float, float]) -> RoiConfig:
        bgr = decode_jpeg(jpeg)
        roi = split_box(tuple(box), 16, 0)  # type: ignore[arg-type]
        return refine_toward_blobs(bgr, roi, tuple(box))  # type: ignore[arg-type]

    def set_roi(self, roi: RoiConfig) -> None:
        with self._lock:
            self.roi = RoiConfig.model_validate(roi if isinstance(roi, dict) else roi.model_dump())
            self._epoch += 1
            self._detector = OnlineDetector(self.roi.indices())
            self._motion.clear()
            if self._last_thumb is not None:
                self._motion.set_reference(*self._last_thumb)
            self.status = "ready"

    # ------------------------------------------------------------------ frames
    def ingest_frame(self, t_ms: float, jpeg: bytes) -> FrameResult:
        bgr = decode_jpeg(jpeg)
        with self._lock:
            if not self._clock.accept(t_ms):
                raise ValueError(f"frame timestamp {t_ms!r} is not after the previous frame")
            if len(self._frames) >= MAX_FRAMES:
                raise RuntimeError("recording limit reached; finish the observation")
            thumb, scale = motion_thumbnail(bgr)
            self._last_thumb = (thumb, scale)
            self._last_bgr = bgr
            brightness, clipped = frame_brightness(bgr)
            flags: list[str] = []
            if brightness < DARK_BRIGHTNESS:
                flags.append("too_dark")
            states: dict[int, int | None] = {}
            levels: dict[int, float] = {}
            quality: dict = {"brightness": round(brightness, 4), "clipped_fraction": round(clipped, 4)}
            if self.roi is None or self._detector is None:
                flags.append("no_roi")
            else:
                if self._motion.reference is None:
                    self._motion.set_reference(thumb, scale)
                reading = self._motion.measure(thumb)
                h, w = bgr.shape[:2]
                moved = False
                if reading is not None:
                    thr = move_threshold_px(median_roi_width_px(self.roi, w))
                    quality["shift_px"] = round(reading.shift_px, 2)
                    if reading.shift_px > thr or reading.response < MIN_TRACK_RESPONSE:
                        moved = True
                        flags.append("camera_moved")
                rects = pixel_rects(self.roi, w, h)
                x0, y0, x1, y1 = union_rect(rects)
                levels = roi_levels(green_signal(bgr[y0:y1, x0:x1]), rects, (x0, y0))
                states = self._detector.update(levels, reliable=not moved)
                run_idx = self._active.index if self._active is not None else None
                self._frames.append(_Frame(float(t_ms), self._epoch, levels, not moved, brightness < DARK_BRIGHTNESS,
                                           run_idx))
                if self._active is not None:
                    run = self._active
                    if run.start_ms is None:
                        run.start_ms = float(t_ms)
                    run.frames.append(len(self._frames) - 1)
                    if run.snapshot_frame is None and float(t_ms) - run.start_ms >= SETTLE_MS * 2:
                        run.snapshot_frame = bgr.copy()
            quality["flags"] = sorted(set(flags))
            quality["step"] = self._active.step_id if self._active else None
            lv = {k: round(v, 4) for k, v in levels.items()}
            self._live = {"t_ms": float(t_ms), "states": states, "levels": lv, "quality": quality}
            return FrameResult(t_ms=float(t_ms), states=states, levels=lv, quality=quality)

    # ------------------------------------------------------------------ steps
    def start_step(self, step_id: str) -> DemoStep:
        with self._lock:
            step = self._by_id.get(step_id)
            if step is None:
                raise KeyError(f"unknown step {step_id!r}")
            if self.roi is None:
                raise RuntimeError("frame the LEDs before starting a step")
            if self._active is not None:
                self._end_active()
            self._active = _Run(index=len(self._runs), step_id=step_id, start_ms=None)
            self._runs.append(self._active)
            self.status = "recording"
            self.result = None
            return step

    def end_step(self) -> dict:
        with self._lock:
            if self._active is None:
                raise RuntimeError("no step is recording")
            return self._end_active()

    def _end_active(self) -> dict:
        run = self._active
        assert run is not None
        run.end_ms = self._frames[run.frames[-1]].t_ms if run.frames else run.start_ms
        self._active = None
        self.status = "ready"
        res = self._measure(run, self._classify())
        self._step_results[run.step_id] = res
        return res

    # ------------------------------------------------------------------ analysis
    def _classify(self) -> tuple[np.ndarray, dict[int, dict[int, dict]]]:
        n = len(self._frames)
        states = np.full((n, 16), -1, dtype=np.int8)
        levels = np.full((n, 16), np.nan)
        reliable = np.array([f.reliable and not f.dark for f in self._frames], dtype=bool)
        epochs = np.array([f.epoch for f in self._frames], dtype=np.int64)
        for i, f in enumerate(self._frames):
            for led, v in f.levels.items():
                levels[i, led] = v
        cal_out: dict[int, dict[int, dict]] = {}
        for ep in sorted(set(epochs.tolist())):
            sel = np.nonzero(epochs == ep)[0]
            ok = sel[reliable[sel]]
            hist = {i: levels[ok, i][np.isfinite(levels[ok, i])] for i in ALL_LEDS if np.isfinite(levels[sel, i]).any()}
            cals = calibrate(hist)
            cal_out[ep] = {i: c.as_dict() for i, c in cals.items()}
            for i in hist:
                col = classify_series(levels[sel, i], reliable[sel], cals.get(i))
                states[sel, i] = np.array([-1 if s is None else s for s in col], dtype=np.int8)
        return states, cal_out

    def _measure(self, run: _Run, classified: tuple[np.ndarray, dict]) -> dict:
        states, cals = classified
        step = self._by_id[run.step_id]
        start = run.start_ms or 0.0
        idx = [i for i in run.frames if self._frames[i].t_ms >= start + SETTLE_MS]
        duration = ((run.end_ms or start) - start) / 1000.0
        bits: dict[int, int] = {}
        unread: list[int] = []
        per_led: dict[int, dict] = {}
        for led in ALL_LEDS:
            col = states[idx, led] if idx else np.zeros(0, np.int8)
            known = col[col >= 0]
            on = int((known == 1).sum())
            off = int((known == 0).sum())
            n = on + off
            share = max(on, off) / n if n else 0.0
            per_led[led] = {"on_frames": on, "off_frames": off, "agreement": round(share, 3)}
            if n >= MIN_STEP_FRAMES and share >= MIN_AGREEMENT:
                bits[led] = 1 if on > off else 0
            else:
                unread.append(led)
        reasons = []
        if duration < 1.0:
            reasons.append(f"recorded only {duration:.1f} s (hold for at least 1.5 s)")
        moved = sum(1 for i in run.frames if not self._frames[i].reliable)
        if run.frames and moved / len(run.frames) > 0.3:
            reasons.append("camera moved or the view was too dark during the step")
        epoch_cal = cals.get(self._frames[idx[0]].epoch, {}) if idx else {}
        if unread and not any(c.get("source") != "none" for c in epoch_cal.values()):
            reasons.append("LED on/off levels are not calibrated yet (no LED has changed state so far); "
                           "this step is re-read at Finish, after the other steps")
        elif unread:
            reasons.append("could not read LED" + ", LED".join(map(str, unread)))
        expected = int(step.expect_leds, 16)
        simulated = int(self.simulated.get(run.step_id, step.expect_leds), 16)
        res: dict = {"step_id": run.step_id, "title": step.title, "set": step.set, "press": step.press,
                     "duration_s": round(duration, 2), "frames": len(idx), "expected": f"0x{expected:04X}",
                     "expected_text": step.expect_text, "simulated": f"0x{simulated:04X}", "per_led": per_led}
        if reasons and (unread or duration < 1.0):
            res.update(status="inconclusive", observed=None, reasons=reasons, matches_contract=None,
                       matches_simulation=None)
        else:
            obs = sum(v << i for i, v in bits.items())
            res.update(status="observed", observed=f"0x{obs:04X}", reasons=reasons,
                       matches_contract=obs == expected, matches_simulation=obs == simulated,
                       lit=[i for i in ALL_LEDS if bits.get(i)])
        if run.snapshot_frame is not None and self.roi is not None:
            st = {i: bits.get(i) for i in ALL_LEDS}
            name = f"{run.step_id}_{run.index}.jpg"
            try:
                (self.evidence_dir / "snapshots").mkdir(exist_ok=True)
                label = f"{self.variant} {run.step_id}: observed {res.get('observed') or 'inconclusive'}"
                cv2.imwrite(str(self.evidence_dir / "snapshots" / name), draw_overlay(run.snapshot_frame, self.roi, st, label))
                res["snapshot"] = f"snapshots/{name}"
            except (OSError, cv2.error):
                pass
        return res

    # ------------------------------------------------------------------ finish
    def finish(self) -> dict:
        with self._lock:
            if self._active is not None:
                self._end_active()
            classified = self._classify()
            latest: dict[str, _Run] = {}
            for r in self._runs:
                latest[r.step_id] = r
            steps = []
            for s in self.steps:
                if s.id in latest:
                    steps.append(self._measure(latest[s.id], classified))
                else:
                    steps.append({"step_id": s.id, "title": s.title, "status": "not_run", "expected": s.expect_leds,
                                  "simulated": self.simulated.get(s.id), "observed": None})
            demo = [x for x in steps if x["step_id"] != DARK_STEP]
            if any(x["status"] != "observed" for x in demo):
                verdict = "inconclusive"
                summary = ("not every demo step was observed reliably: "
                           + ", ".join(f"{x['step_id']} {x['status']}" for x in demo if x["status"] != "observed"))
            elif not all(x["matches_simulation"] for x in demo):
                verdict = "unexpected"
                bad = [x["step_id"] for x in demo if not x["matches_simulation"]]
                summary = (f"the board did not behave as this bitstream's simulation predicts at {', '.join(bad)} "
                           "(was the board reprogrammed or power-cycled?)")
            elif all(x["matches_contract"] for x in demo):
                verdict = "contract_met"
                summary = "every demo step showed the LED pattern the security contract requires"
            else:
                verdict = "violation_observed"
                bad = [x for x in demo if not x["matches_contract"]]
                summary = ("the contract violation is visible on the board: "
                           + "; ".join(f"{x['step_id']} lit {x['observed']} where {x['expected']} is required" for x in bad))
            self.result = {
                "kind": "board_observation", "session_id": self.id, "audit_id": self.audit_id, "variant": self.variant,
                "variant_label": self.variant_label, "verdict": verdict, "summary": summary, "steps": steps,
                "programmed": self.programmed, "frames_recorded": len(self._frames),
                "calibration": classified[1].get(max(classified[1]) if classified[1] else 0, {}),
                "method": "webcam: per-LED green level, session-wide calibration, majority over the step window",
            }
            self.status = "finished"
            self._write_evidence()
            return self.result

    def _write_evidence(self) -> None:
        assert self.result is not None
        (self.evidence_dir / "observation.json").write_text(json.dumps(self.result, indent=2, default=str))
        with open(self.evidence_dir / "frames.csv", "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["t_ms", "epoch", "step_run", "reliable", "dark"] + [f"led{i}" for i in ALL_LEDS])
            for f in self._frames:
                w.writerow([f"{f.t_ms:.1f}", f.epoch, "" if f.run is None else f.run, int(f.reliable), int(f.dark)]
                           + [("" if f.levels.get(i) is None else f"{f.levels[i]:.4f}") for i in ALL_LEDS])

    # ------------------------------------------------------------------ state
    def to_dict(self) -> dict:
        with self._lock:
            return {
                "kind": "board_observation", "status": self.status, "audit_id": self.audit_id, "variant": self.variant,
                "variant_label": self.variant_label, "roi": self.roi.model_dump() if self.roi else None,
                "steps": [s.model_dump() | {"simulated": self.simulated.get(s.id)} for s in self.steps],
                "active_step": self._active.step_id if self._active else None,
                "active_frames": len(self._active.frames) if self._active else 0,
                "step_results": self._step_results, "result": self.result, "live": self._live,
                "frames_recorded": len(self._frames), "programmed": self.programmed,
            }
