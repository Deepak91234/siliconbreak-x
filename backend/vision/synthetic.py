"""Synthetic webcam frames of a board-like scene -- FOR TESTS ONLY.

This module renders JPEG frames of a dark PCB with 16 LED footprints in a row
(LED15 left ... LED0 right, like the Basys 3), driven by a time -> LED-state
function, so the vision pipeline can be tested end to end without hardware.
It models the things that make real webcam footage hard: sensor noise, optical
blur, exposure integration (an LED switching mid-exposure renders at partial
brightness), mains-light exposure flicker, small hand-held shake, timestamp
jitter, dropped frames, darkness, washed-out low contrast, and a large camera
movement.

It must NEVER be used as a live camera source: nothing in the server imports
it, and every frame it produces is marked synthetic in its JPEG comment so it
cannot be mistaken for camera evidence.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Iterator, Mapping

import cv2
import numpy as np

from .frames import encode_jpeg

SYNTHETIC_MARKER = b"SiliconBreak X synthetic test frame"

StateFn = Callable[[float], Mapping[int, int]]   # t (seconds) -> {led_index: 0/1}


@dataclass
class SceneConfig:
    width: int = 320
    height: int = 240
    led_y: float = 0.55          # normalised y of the LED row
    led_x_left: float = 0.10     # normalised x of LED15
    led_x_right: float = 0.90    # normalised x of LED0
    ambient: float = 1.0         # scene illumination gain
    exposure: float = 1.0        # camera gain applied to everything (darkness: ~0.1)
    led_gain: float = 1.0        # LED emission strength (washed-out low contrast: small)
    noise_sd: float = 0.012      # additive sensor noise, 0..1 units
    blur_sigma: float = 0.7
    flicker: float = 0.03        # relative exposure flicker amplitude
    shake_px: float = 0.4        # per-frame random translation sd
    exposure_ms: float = 12.0    # integration time (LED edges smear across it)
    jpeg_quality: int = 82
    seed: int = 1


@dataclass
class CameraMove:
    at_s: float
    dx_px: float
    dy_px: float


class SyntheticBoard:
    """Renders frames of the scene for a given LED state function."""

    def __init__(self, cfg: SceneConfig | None = None) -> None:
        self.cfg = cfg or SceneConfig()
        self._rng = np.random.default_rng(self.cfg.seed)
        self._bg = self._background()
        self._halo, self._core = self._led_kernels()

    # ---------------------------------------------------------------- geometry
    def led_center_px(self, led: int) -> tuple[float, float]:
        c = self.cfg
        frac = (15 - led) / 15.0            # LED15 -> 0 (left), LED0 -> 1 (right)
        x = (c.led_x_left + frac * (c.led_x_right - c.led_x_left)) * c.width
        return x, c.led_y * c.height

    @property
    def pitch_px(self) -> float:
        c = self.cfg
        return (c.led_x_right - c.led_x_left) * c.width / 15.0

    def led_box(self, first_led: int = 0, count: int = 16, margin_y: float = 0.045) -> tuple[float, float, float, float]:
        """Normalised [x, y, w, h] box around LEDs first_led..first_led+count-1, half a pitch either side."""
        c = self.cfg
        xl, _ = self.led_center_px(first_led + count - 1)
        xr, _ = self.led_center_px(first_led)
        half = 0.5 * self.pitch_px
        return ((xl - half) / c.width, c.led_y - margin_y, (xr - xl + 2 * half) / c.width, 2 * margin_y)

    # ---------------------------------------------------------------- scene
    def _background(self) -> np.ndarray:
        c, rng = self.cfg, self._rng
        h, w = c.height, c.width
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        img = np.empty((h, w, 3), np.float32)
        # desk with a soft lighting gradient
        desk = 0.30 + 0.10 * (xx / w) - 0.05 * (yy / h)
        img[...] = desk[..., None] * np.array([0.85, 0.95, 1.05], np.float32)
        # PCB
        x0, y0, x1, y1 = int(0.04 * w), int(0.12 * h), int(0.96 * w), int(0.94 * h)
        img[y0:y1, x0:x1] = np.array([0.20, 0.17, 0.14], np.float32)
        # components: chips, connectors, headers (texture for motion tracking)
        for _ in range(28):
            cw, ch = int(rng.integers(6, 34)), int(rng.integers(5, 22))
            cx, cy = int(rng.integers(x0 + 2, x1 - cw - 2)), int(rng.integers(y0 + 2, y1 - ch - 2))
            if abs(cy + ch / 2 - c.led_y * h) < 0.08 * h:   # keep the LED strip clear
                continue
            shade = float(rng.choice([0.06, 0.10, 0.45, 0.60]))
            img[cy:cy + ch, cx:cx + cw] = np.array([shade, shade, shade * 1.05], np.float32)
        # slide switches below the LEDs (white bodies)
        for led in range(16):
            x, y = self.led_center_px(led)
            sx, sy = int(x - 2), int(y + 0.17 * h)
            img[sy:sy + 9, sx:sx + 5] = 0.75
            img[sy + 2:sy + 5, sx + 1:sx + 4] = 0.25
        # silkscreen labels below each LED (neutral grey, outside the LED cells)
        for led in range(16):
            x, y = self.led_center_px(led)
            ly = int(y + 0.075 * h)
            img[ly:ly + 2, int(x - 3):int(x + 3)] = 0.62
        # unlit LED packages: small pale rectangles
        for led in range(16):
            x, y = self.led_center_px(led)
            img[int(y - 1):int(y + 2), int(x - 2):int(x + 3)] = np.array([0.36, 0.42, 0.40], np.float32)
        return img

    def _led_kernels(self) -> tuple[np.ndarray, np.ndarray]:
        r = 8
        yy, xx = np.mgrid[-r:r + 1, -r:r + 1].astype(np.float32)
        d2 = xx ** 2 + yy ** 2
        halo = np.exp(-d2 / (2 * 2.6 ** 2))[..., None] * np.array([0.10, 0.80, 0.18], np.float32)
        core = np.exp(-d2 / (2 * 1.1 ** 2))[..., None] * np.array([1.3, 1.4, 1.3], np.float32)
        return halo, core

    # ---------------------------------------------------------------- rendering
    def render(self, intensities: Mapping[int, float], t_s: float = 0.0,
               offset_px: tuple[float, float] = (0.0, 0.0)) -> np.ndarray:
        """BGR uint8 frame; intensities = {led: fraction of the exposure the LED was lit}."""
        c, rng = self.cfg, self._rng
        img = self._bg * c.ambient
        r = self._halo.shape[0] // 2
        for led, a in intensities.items():
            if a <= 0:
                continue
            x, y = self.led_center_px(led)
            xi, yi = int(round(x)), int(round(y))
            sl = (slice(yi - r, yi + r + 1), slice(xi - r, xi + r + 1))
            img[sl] += (a * c.led_gain) * (self._halo + self._core)
        flick = 1.0 + c.flicker * (0.6 * math.sin(2 * math.pi * 0.7 * t_s) + 0.4 * float(rng.uniform(-1, 1)))
        img = img * (c.exposure * flick)
        dx = offset_px[0] + float(rng.normal(0, c.shake_px))
        dy = offset_px[1] + float(rng.normal(0, c.shake_px))
        if dx or dy:
            m = np.float32([[1, 0, dx], [0, 1, dy]])
            img = cv2.warpAffine(img, m, (c.width, c.height), borderMode=cv2.BORDER_REFLECT)
        if c.blur_sigma > 0:
            img = cv2.GaussianBlur(img, (0, 0), c.blur_sigma)
        img = img + rng.standard_normal(img.shape, dtype=np.float32) * c.noise_sd
        return (np.clip(img, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)

    def jpeg(self, bgr: np.ndarray) -> bytes:
        data = encode_jpeg(bgr, self.cfg.jpeg_quality)
        # Insert a COM segment right after SOI so the frame is self-describing.
        com = b"\xff\xfe" + (len(SYNTHETIC_MARKER) + 2).to_bytes(2, "big") + SYNTHETIC_MARKER
        return data[:2] + com + data[2:]

    def exposure_intensities(self, state_fn: StateFn, t_s: float, leds: list[int]) -> dict[int, float]:
        """Fraction of the exposure window [t - exposure, t] each LED was lit (4-point integration)."""
        exp = self.cfg.exposure_ms / 1000.0
        samples = [state_fn(t_s - exp * (k + 0.5) / 4) for k in range(4)]
        return {i: sum(int(s.get(i, 0)) for s in samples) / 4.0 for i in leds}


@dataclass
class StreamConfig:
    fps: float = 30.0
    jitter_ms: float = 1.5          # client timestamp noise (performance.now around canvas capture)
    drop_prob: float = 0.0          # probability that a frame is skipped by the client
    t0_s: float = 0.0
    moves: list[CameraMove] = field(default_factory=list)
    seed: int = 7


def frame_stream(board: SyntheticBoard, state_fn: StateFn, duration_s: float,
                 stream: StreamConfig | None = None) -> Iterator[tuple[float, bytes]]:
    """Yield (t_ms, jpeg) for duration_s seconds starting at stream.t0_s (client clock = scene clock)."""
    sc = stream or StreamConfig()
    rng = np.random.default_rng(sc.seed)
    n = int(round(duration_s * sc.fps))
    last_ms = -1.0
    leds = list(range(16))
    for k in range(n):
        t_true = sc.t0_s + k / sc.fps + float(rng.normal(0, 0.002))
        if sc.drop_prob and rng.random() < sc.drop_prob:
            continue
        off = (0.0, 0.0)
        for mv in sc.moves:
            if t_true >= mv.at_s:
                off = (off[0] + mv.dx_px, off[1] + mv.dy_px)
        inten = board.exposure_intensities(state_fn, t_true, leds)
        bgr = board.render(inten, t_true, off)
        t_ms = t_true * 1000.0 + float(rng.normal(0, sc.jitter_ms))
        t_ms = max(t_ms, last_ms + 0.5)
        last_ms = t_ms
        yield t_ms, board.jpeg(bgr)


# ---------------------------------------------------------------- behaviour models

def _segments(rate: float | list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Normalise a rate to piecewise-constant segments [(t_from_s, hz), ...] (first starts at -inf)."""
    if isinstance(rate, (int, float)):
        return [(-math.inf, float(rate))]
    segs = sorted((float(t), float(h)) for t, h in rate)
    if not segs:
        raise ValueError("empty rate segments")
    return [(-math.inf, segs[0][1])] + segs[1:]


def _cycles(segs: list[tuple[float, float]], t0: float, t: float) -> float:
    """Integral of the piecewise-constant rate from t0 to t (number of steps/cycles)."""
    total = 0.0
    for k, (ts, hz) in enumerate(segs):
        te = segs[k + 1][0] if k + 1 < len(segs) else math.inf
        a, b = max(ts, t0), min(te, t)
        if b > a:
            total += hz * (b - a)
    return total


def counter_states(leds: list[int], rate: float | list[tuple[float, float]], *, start_value: int = 0,
                   direction: int = 1, t_start: float = 0.0, phase0: float = 0.3) -> StateFn:
    """A binary counter on `leds` (core bit order) stepping at a piecewise-constant rate.

    The count is the integral of the rate (like a tick divider whose period
    changes immediately). `phase0` offsets the first step so steps do not align
    with frame boundaries.
    """
    width = len(leds)
    segs = _segments(rate)

    def fn(t: float) -> dict[int, int]:
        steps = int(math.floor(_cycles(segs, t_start, t) + phase0)) if t >= t_start else 0
        v = (start_value + direction * steps) % (1 << width)
        return {led: (v >> bit) & 1 for bit, led in enumerate(leds)}

    return fn


def blinker_states(leds: list[int], rate: float | list[tuple[float, float]], *, pattern: str = "together",
                   duty: float = 0.5, phase0: float = 0.1) -> StateFn:
    """Blink at `rate` full cycles per second with the given duty; 'alternate' inverts odd bits."""
    segs = _segments(rate)

    def fn(t: float) -> dict[int, int]:
        p = _cycles(segs, 0.0, t) + phase0 if t >= 0 else phase0
        on = 1 if (p % 1.0) < duty else 0
        return {led: on if pattern == "together" or bit % 2 == 0 else 1 - on for bit, led in enumerate(leds)}

    return fn


def sequence_states(leds: list[int], states: list[tuple[int, float]], *, t_offset: float = 0.0) -> StateFn:
    """A cyclic FSM: [(lights value in core bit order, duration_s), ...] repeated."""
    period = sum(d for _, d in states)

    def fn(t: float) -> dict[int, int]:
        u = (t + t_offset) % period
        for value, d in states:
            if u < d:
                break
            u -= d
        return {led: (value >> bit) & 1 for bit, led in enumerate(leds)}

    return fn


def static_states(leds: list[int], value_at: Callable[[float], int]) -> StateFn:
    def fn(t: float) -> dict[int, int]:
        v = value_at(t)
        return {led: (v >> bit) & 1 for bit, led in enumerate(leds)}

    return fn
