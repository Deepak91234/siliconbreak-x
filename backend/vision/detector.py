"""LED state detection: per-ROI brightness feature, on/off calibration, hysteresis, camera motion.

Pipeline per frame
------------------
1. Feature: for each ROI, the mean of the brightest TOP_FRACTION of pixels of
   `frames.green_signal` (green-weighted luminance + green-excess bonus). A lit
   LED is a small bright blob somewhere inside its cell, so a top-percentile
   statistic is insensitive to where exactly the blob sits and to the dark
   board around it, while averaging a few pixels (instead of taking the max)
   suppresses single hot pixels and JPEG ringing.
2. Calibration: each ROI's level history is split into two clusters (2-means).
   A ROI counts as calibrated only when both clusters are populated and well
   separated relative to their spread (see MIN_DPRIME). High counter bits change
   rarely (bit 3 of a 1 Hz counter is constant for 8 s), so ROIs that have not
   yet seen both states borrow the on/off separation pooled from calibrated
   ROIs, anchored on their own steady level.
3. Classification with hysteresis: above `hi` -> 1, below `lo` -> 0, in
   between -> keep the previous state; None whenever the ROI is uncalibrated,
   the frame is unreliable (camera moved), or there is no previous state.
4. Camera motion: phase correlation of a downsampled grayscale frame against the
   reference captured when the ROI was set. A shift large enough to move an LED
   out of its cell invalidates every LED state until the ROI is set again.
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass

import cv2
import numpy as np

# ---- feature -----------------------------------------------------------------
TOP_FRACTION = 0.03
TOP_MIN_PIXELS = 4

# ---- calibration ---------------------------------------------------------------
# Hysteresis thresholds as fractions of the on/off separation above the off
# level. With MIN_DPRIME = 6 the far threshold is 0.65*6 = 3.9 noise standard
# deviations away from either cluster centre, so a noise-induced false toggle
# has probability ~5e-5 per frame; the analysis additionally requires a value
# to persist for >= 2 frames before it counts.
LO_FRAC = 0.35
HI_FRAC = 0.65
MIN_DPRIME = 6.0
# Absolute floor on separation (green-weighted units, ~0..1.1): below this the
# LED is not meaningfully brighter than its surroundings even if noise is low,
# e.g. heavy JPEG smoothing of a washed-out scene.
MIN_SEPARATION = 0.06
MIN_CLUSTER = 3                # samples needed in each cluster
NOISE_FLOOR = 0.004            # never trust a spread below ~1 grey level
# A pooled (borrowed) calibration is applied to a ROI only when that ROI's own
# levels sit tightly around one value close to the pooled off or on level.
POOL_MATCH_FRAC = 0.3
CAL_WINDOW = 900               # frames of history used for live calibration (~30 s)
RECAL_EVERY = 5                # live recalibration period in frames
MIN_CAL_FRAMES = 45            # frames before "uncalibrated"/"low_contrast" become flags

# ---- frame quality --------------------------------------------------------------
# Mean grey level (0..1) below which the scene is too dark for a webcam's auto
# exposure to give usable LED contrast; typical indoor scenes are 0.25-0.5.
DARK_BRIGHTNESS = 0.06
# Fraction of clipped pixels (any channel >= 250) above which the image is
# overexposed; lit LED cores clip by design but cover well under 5% of a frame.
OVEREXPOSED_FRACTION = 0.25
# An OFF level this high means the ROI is saturated even when the LED is dark
# (glare / reflection), so on and off cannot be told apart.
REFLECTION_OFF_LEVEL = 0.92

# ---- motion ---------------------------------------------------------------------
# The camera counts as moved when the frame shifted by more than this fraction of
# the median ROI width: an LED blob covers roughly the middle third of its cell,
# so a 0.3-cell shift moves it to the cell edge. Slight hand-held shake (1-2 px)
# stays well below this; MOVE_MIN_PX keeps tiny ROIs from tripping on noise.
MOVE_ROI_FRACTION = 0.3
MOVE_MIN_PX = 4.0
# Phase-correlation peak response below this means the scene no longer
# resembles the reference (occlusion, camera pointed elsewhere).
MIN_TRACK_RESPONSE = 0.04


def roi_levels(signal: np.ndarray, rects: dict[int, tuple[int, int, int, int]],
               offset: tuple[int, int] = (0, 0)) -> dict[int, float]:
    """Top-percentile mean of `signal` inside each rect. `offset` = (x, y) of signal[0, 0] in the frame."""
    ox, oy = offset
    out: dict[int, float] = {}
    for led, (x0, y0, x1, y1) in rects.items():
        patch = signal[y0 - oy:y1 - oy, x0 - ox:x1 - ox].ravel()
        n = patch.size
        if n == 0:
            continue
        k = min(n, max(TOP_MIN_PIXELS, int(round(TOP_FRACTION * n))))
        out[led] = float(np.partition(patch, n - k)[n - k:].mean())
    return out


def union_rect(rects: dict[int, tuple[int, int, int, int]]) -> tuple[int, int, int, int]:
    xs0, ys0, xs1, ys1 = zip(*rects.values())
    return min(xs0), min(ys0), max(xs1), max(ys1)


@dataclass
class LedCalibration:
    led: int
    source: str                  # "own" | "pooled" | "none"
    off: float | None = None
    on: float | None = None
    noise: float | None = None
    n_off: int = 0
    n_on: int = 0

    @property
    def separation(self) -> float | None:
        return None if self.on is None or self.off is None else self.on - self.off

    @property
    def dprime(self) -> float | None:
        s = self.separation
        return None if s is None or not self.noise else s / self.noise

    @property
    def lo(self) -> float | None:
        s = self.separation
        return None if s is None else self.off + LO_FRAC * s  # type: ignore[operator]

    @property
    def hi(self) -> float | None:
        s = self.separation
        return None if s is None else self.off + HI_FRAC * s  # type: ignore[operator]

    def as_dict(self) -> dict:
        d = asdict(self)
        for k in ("off", "on", "noise"):
            if d[k] is not None:
                d[k] = round(d[k], 4)
        d["separation"] = None if self.separation is None else round(self.separation, 4)
        d["dprime"] = None if self.dprime is None else round(self.dprime, 2)
        return d


def two_means(x: np.ndarray, iters: int = 12) -> tuple[float, float, float, int, int]:
    """1-D 2-means: (mu_low, mu_high, pooled within-cluster sd, n_low, n_high)."""
    x = np.asarray(x, dtype=np.float64)
    lo, hi = np.percentile(x, 5), np.percentile(x, 95)
    if hi - lo < 1e-9:
        return float(lo), float(hi), 0.0, int(x.size), 0
    thr = 0.5 * (lo + hi)
    for _ in range(iters):
        a, b = x[x <= thr], x[x > thr]
        if a.size == 0 or b.size == 0:
            break
        new = 0.5 * (a.mean() + b.mean())
        if abs(new - thr) < 1e-7:
            break
        thr = new
    a, b = x[x <= thr], x[x > thr]
    if a.size == 0 or b.size == 0:
        m = float(x.mean())
        return m, m, float(x.std()), int(x.size), 0
    ssd = float(((a - a.mean()) ** 2).sum() + ((b - b.mean()) ** 2).sum())
    sd = (ssd / max(1, x.size - 2)) ** 0.5
    return float(a.mean()), float(b.mean()), sd, int(a.size), int(b.size)


def _robust_sd(x: np.ndarray) -> float:
    mad = float(np.median(np.abs(x - np.median(x))))
    return max(NOISE_FLOOR, 1.4826 * mad)


def calibrate(history: dict[int, np.ndarray]) -> dict[int, LedCalibration]:
    """Calibrate every ROI from its level history (reliable frames only)."""
    cals: dict[int, LedCalibration] = {}
    steady: dict[int, tuple[float, float]] = {}   # led -> (median, robust sd) for unimodal ROIs
    for led, x in history.items():
        x = np.asarray(x, dtype=np.float64)
        if x.size < 2 * MIN_CLUSTER:
            cals[led] = LedCalibration(led, "none")
            continue
        m0, m1, sd, n0, n1 = two_means(x)
        sd = max(sd, NOISE_FLOOR)
        sep = m1 - m0
        if n0 >= MIN_CLUSTER and n1 >= MIN_CLUSTER and sep >= MIN_SEPARATION and sep / sd >= MIN_DPRIME:
            cals[led] = LedCalibration(led, "own", off=m0, on=m1, noise=sd, n_off=n0, n_on=n1)
        else:
            cals[led] = LedCalibration(led, "none")
            steady[led] = (float(np.median(x)), _robust_sd(x))

    own = [c for c in cals.values() if c.source == "own"]
    pooled: tuple[float, float, float] | None = None   # (off, on, noise)
    if own:
        pooled = (float(np.median([c.off for c in own])), float(np.median([c.on for c in own])),
                  float(np.median([c.noise for c in own])))
    elif len(steady) >= 2:
        # No ROI has toggled yet, but some may be steadily lit and others dark
        # (e.g. a counter holding 0b0101): cluster the ROIs' steady levels.
        meds = np.array([v[0] for v in steady.values()])
        noise = float(np.median([v[1] for v in steady.values()]))
        m0, m1, _sd, n0, n1 = two_means(meds)
        if n0 >= 1 and n1 >= 1 and (m1 - m0) >= MIN_SEPARATION and (m1 - m0) / max(noise, NOISE_FLOOR) >= MIN_DPRIME:
            pooled = (m0, m1, noise)
    if pooled is None:
        return cals

    p_off, p_on, p_noise = pooled
    p_sep = p_on - p_off
    for led, (med, rsd) in steady.items():
        if rsd > p_sep / MIN_DPRIME:       # too noisy to trust a borrowed threshold
            continue
        if abs(med - p_off) <= POOL_MATCH_FRAC * p_sep:
            cals[led] = LedCalibration(led, "pooled", off=med, on=med + p_sep, noise=max(rsd, p_noise),
                                       n_off=int(history[led].size))
        elif abs(med - p_on) <= POOL_MATCH_FRAC * p_sep:
            cals[led] = LedCalibration(led, "pooled", off=med - p_sep, on=med, noise=max(rsd, p_noise),
                                       n_on=int(history[led].size))
    return cals


def classify(level: float, cal: LedCalibration | None, prev: int | None) -> int | None:
    """Hysteresis classification; None when uncalibrated or ambiguous with no history."""
    if cal is None or cal.source == "none" or cal.lo is None:
        return None
    if level >= cal.hi:  # type: ignore[operator]
        return 1
    if level <= cal.lo:
        return 0
    return prev


def classify_series(levels: np.ndarray, reliable: np.ndarray, cal: LedCalibration | None) -> list[int | None]:
    """Classify a time series; hysteresis memory resets on unreliable frames."""
    out: list[int | None] = []
    prev: int | None = None
    for lv, ok in zip(levels, reliable):
        if not ok or not np.isfinite(lv):
            prev = None
            out.append(None)
            continue
        st = classify(float(lv), cal, prev)
        out.append(st)
        prev = st
    return out


class OnlineDetector:
    """Live LED states for the preview: rolling calibration + hysteresis."""

    def __init__(self, leds: list[int]) -> None:
        self.leds = list(leds)
        self._hist: dict[int, deque[float]] = {i: deque(maxlen=CAL_WINDOW) for i in self.leds}
        self._prev: dict[int, int | None] = {i: None for i in self.leds}
        self.calibration: dict[int, LedCalibration] = {i: LedCalibration(i, "none") for i in self.leds}
        self.frames = 0

    def update(self, levels: dict[int, float], reliable: bool) -> dict[int, int | None]:
        self.frames += 1
        if reliable:
            for i in self.leds:
                if i in levels:
                    self._hist[i].append(levels[i])
            uncal = any(c.source == "none" for c in self.calibration.values())
            if self.frames % RECAL_EVERY == 0 or (uncal and self.frames % 2 == 0):
                self.calibration = calibrate({i: np.fromiter(h, float) for i, h in self._hist.items()})
        states: dict[int, int | None] = {}
        for i in self.leds:
            if not reliable or i not in levels:
                self._prev[i] = None
                states[i] = None
                continue
            st = classify(levels[i], self.calibration.get(i), self._prev[i])
            self._prev[i] = st
            states[i] = st
        return states

    def summary(self) -> dict:
        return {str(i): c.as_dict() for i, c in self.calibration.items()}


def contrast_flags(cals: dict[int, LedCalibration], history: dict[int, np.ndarray], frames: int) -> list[str]:
    """Calibration-derived quality flags."""
    flags: list[str] = []
    if frames < MIN_CAL_FRAMES or not cals:
        return flags
    usable = [c for c in cals.values() if c.source != "none"]
    if not usable:
        # nothing calibrated: either the LEDs never changed or they cannot be
        # told apart from the background -- both make the camera evidence void
        flags.append("low_contrast")
        flags.append("uncalibrated")
    elif len(usable) < len(cals):
        flags.append("partially_calibrated")
    for c in cals.values():
        if c.source == "own" and c.dprime is not None and c.dprime < 1.5 * MIN_DPRIME:
            flags.append("low_contrast")
            break
    for led, x in history.items():
        c = cals.get(led)
        off = c.off if c is not None and c.off is not None else (float(np.percentile(x, 10)) if len(x) else None)
        if off is not None and off >= REFLECTION_OFF_LEVEL:
            flags.append("saturated")
            break
    return sorted(set(flags))


@dataclass
class MotionReading:
    dx_px: float
    dy_px: float
    response: float

    @property
    def shift_px(self) -> float:
        return float(np.hypot(self.dx_px, self.dy_px))


class MotionMonitor:
    """Phase correlation of low-resolution grayscale frames against a reference."""

    def __init__(self) -> None:
        self.reference: np.ndarray | None = None
        self.scale: float = 1.0
        self._window: np.ndarray | None = None

    def set_reference(self, thumb: np.ndarray, scale: float) -> None:
        self.reference = thumb.astype(np.float32, copy=True)
        self.scale = float(scale)
        h, w = thumb.shape
        self._window = cv2.createHanningWindow((w, h), cv2.CV_32F)

    def clear(self) -> None:
        self.reference = None

    def measure(self, thumb: np.ndarray) -> MotionReading | None:
        if self.reference is None or thumb.shape != self.reference.shape:
            return None
        # Copies on purpose: OpenCV 5's phaseCorrelate windows its inputs in
        # place, which would corrupt the reference a little more every frame.
        (dx, dy), resp = cv2.phaseCorrelate(self.reference.copy(), thumb.astype(np.float32, copy=True), self._window)
        return MotionReading(float(dx) * self.scale, float(dy) * self.scale, float(resp))


def move_threshold_px(median_roi_width_px: float) -> float:
    return max(MOVE_MIN_PX, MOVE_ROI_FRACTION * median_roi_width_px)


def frame_brightness(bgr: np.ndarray) -> tuple[float, float]:
    """(mean grey level 0..1, fraction of clipped pixels) on a 4x subsample (cheap, representative)."""
    sub = bgr[::4, ::4]
    gray = cv2.cvtColor(np.ascontiguousarray(sub), cv2.COLOR_BGR2GRAY)
    clipped = float((sub.max(axis=2) >= 250).mean())
    return float(gray.mean()) / 255.0, clipped
