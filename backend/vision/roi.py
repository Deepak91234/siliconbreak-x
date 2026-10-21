"""LED regions of interest (ROIs): the user's LED box -> one ROI per board LED.

Geometry is stored normalised to 0..1 of the frame so a ROI drawn on the
browser preview stays valid whatever resolution the client actually streams.

Physical layout (Basys 3): the 16 user LEDs LD15..LD0 sit in one straight row
above the slide switches, LD15 at the LEFT end and LD0 at the RIGHT end (the
same order as the switches). `auto_roi` therefore splits the user's box into
equal cells and numbers them right-to-left starting at `first_led`. That
numbering is the single place where image position becomes an LED index, so it
is deliberately simple and covered by tests.
"""

from __future__ import annotations

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

import cv2

from .frames import green_signal

# A ROI smaller than this (normalised) would be a handful of pixels at 320x240
# and cannot hold an LED blob plus margin.
MIN_ROI_SIZE = 0.004
# Refinement may move a cell centre by at most this fraction of the cell size:
# enough to absorb a box drawn slightly off the LEDs, not enough to jump to the
# neighbouring LED (which is one full cell away).
MAX_REFINE_SHIFT = 0.3
# A cell is refined only when its brightest blob clearly stands out from the
# cell background (green-weighted units, see frames.green_signal). Unlit LEDs
# are dark, so cells of unlit LEDs simply keep the equal split.
REFINE_MIN_CONTRAST = 0.18


class LedRoi(BaseModel):
    """One LED's region, normalised to 0..1 of the frame."""

    model_config = ConfigDict(extra="forbid")

    led_index: int = Field(ge=0, le=15)
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    w: float = Field(gt=0.0, le=1.0)
    h: float = Field(gt=0.0, le=1.0)

    @model_validator(mode="after")
    def _inside_frame(self) -> "LedRoi":
        if self.w < MIN_ROI_SIZE or self.h < MIN_ROI_SIZE:
            raise ValueError(f"LED{self.led_index} region is too small")
        # small tolerance for float rounding in the browser
        if self.x + self.w > 1.0 + 1e-6 or self.y + self.h > 1.0 + 1e-6:
            raise ValueError(f"LED{self.led_index} region extends outside the frame")
        return self


class RoiConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    leds: list[LedRoi] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def _unique(self) -> "RoiConfig":
        idx = [r.led_index for r in self.leds]
        if len(set(idx)) != len(idx):
            raise ValueError("each LED may have only one region")
        return self

    def indices(self) -> list[int]:
        return [r.led_index for r in self.leds]

    def get(self, led_index: int) -> LedRoi | None:
        for r in self.leds:
            if r.led_index == led_index:
                return r
        return None


def pixel_rects(roi: RoiConfig, width: int, height: int) -> dict[int, tuple[int, int, int, int]]:
    """ROI rectangles in pixels as {led: (x0, y0, x1, y1)} (x1/y1 exclusive), at least 2x2 px."""
    rects: dict[int, tuple[int, int, int, int]] = {}
    for r in roi.leds:
        x0 = int(np.floor(r.x * width))
        y0 = int(np.floor(r.y * height))
        x1 = int(np.ceil((r.x + r.w) * width))
        y1 = int(np.ceil((r.y + r.h) * height))
        x0 = min(max(0, x0), width - 2)
        y0 = min(max(0, y0), height - 2)
        x1 = max(min(width, x1), x0 + 2)
        y1 = max(min(height, y1), y0 + 2)
        rects[r.led_index] = (x0, y0, x1, y1)
    return rects


def median_roi_width_px(roi: RoiConfig, width: int) -> float:
    return float(np.median([r.w * width for r in roi.leds]))


def _validate_box(box: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    if len(box) != 4:
        raise ValueError("box must be [x, y, w, h]")
    x, y, w, h = (float(v) for v in box)
    if not all(np.isfinite(v) for v in (x, y, w, h)):
        raise ValueError("box values must be finite numbers")
    x, y = min(max(x, 0.0), 1.0), min(max(y, 0.0), 1.0)
    w, h = min(w, 1.0 - x), min(h, 1.0 - y)
    if w < MIN_ROI_SIZE or h < MIN_ROI_SIZE:
        raise ValueError("box is empty or outside the frame")
    return x, y, w, h


def split_box(box: tuple[float, float, float, float], count: int, first_led: int) -> RoiConfig:
    """Equal cells across the box, LED indices assigned right-to-left from first_led."""
    if not 1 <= count <= 16:
        raise ValueError("count must be 1..16")
    if not 0 <= first_led <= 15 or first_led + count - 1 > 15:
        raise ValueError(f"LEDs {first_led}..{first_led + count - 1} do not exist (the board has LED0..LED15)")
    x, y, w, h = _validate_box(box)
    cw = w / count
    leds = []
    for k in range(count):  # k = 0 is the leftmost cell -> highest LED index
        leds.append(LedRoi(led_index=first_led + count - 1 - k, x=x + k * cw, y=y, w=cw, h=h))
    return RoiConfig(leds=leds)


def refine_toward_blobs(bgr: np.ndarray, roi: RoiConfig, box: tuple[float, float, float, float]) -> RoiConfig:
    """Re-centre each cell on its brightest green blob, when one clearly exists.

    The cell keeps its size and stays inside the user's box; the centre moves by
    at most MAX_REFINE_SHIFT of the cell size. Only lit LEDs produce a blob, so
    this helps when the user's box is slightly off and is a no-op otherwise.
    """
    hgt, wid = bgr.shape[:2]
    bx, by, bw, bh = _validate_box(box)
    sig = green_signal(bgr)
    sig = cv2.GaussianBlur(sig, (0, 0), 1.0)
    out: list[LedRoi] = []
    for r in roi.leds:
        x0, y0, x1, y1 = pixel_rects(RoiConfig(leds=[r]), wid, hgt)[r.led_index]
        patch = sig[y0:y1, x0:x1]
        if patch.size < 4:
            out.append(r)
            continue
        peak = float(patch.max())
        if peak - float(np.median(patch)) < REFINE_MIN_CONTRAST:
            out.append(r)
            continue
        # centroid of the pixels within 70% of the peak (the blob core)
        mask = patch >= (np.median(patch) + 0.7 * (peak - np.median(patch)))
        ys, xs = np.nonzero(mask)
        cx = (x0 + xs.mean() + 0.5) / wid
        cy = (y0 + ys.mean() + 0.5) / hgt
        ox, oy = r.x + r.w / 2, r.y + r.h / 2
        dx = float(np.clip(cx - ox, -MAX_REFINE_SHIFT * r.w, MAX_REFINE_SHIFT * r.w))
        dy = float(np.clip(cy - oy, -MAX_REFINE_SHIFT * r.h, MAX_REFINE_SHIFT * r.h))
        nx = float(np.clip(r.x + dx, bx, bx + bw - r.w))
        ny = float(np.clip(r.y + dy, by, by + bh - r.h))
        out.append(LedRoi(led_index=r.led_index, x=nx, y=ny, w=r.w, h=r.h))
    return RoiConfig(leds=out)


def draw_overlay(bgr: np.ndarray, roi: RoiConfig | None, states: dict[int, int | None] | None = None,
                 label: str | None = None) -> np.ndarray:
    """Copy of the frame with ROI rectangles (green = on, grey = off, red = unknown)."""
    img = bgr.copy()
    hgt, wid = img.shape[:2]
    if roi is not None:
        for led, (x0, y0, x1, y1) in pixel_rects(roi, wid, hgt).items():
            st = None if states is None else states.get(led)
            color = (0, 220, 0) if st == 1 else (160, 160, 160) if st == 0 else (0, 0, 230)
            cv2.rectangle(img, (x0, y0), (x1 - 1, y1 - 1), color, 1)
            cv2.putText(img, str(led), (x0, max(8, y0 - 2)), cv2.FONT_HERSHEY_SIMPLEX, 0.3, color, 1, cv2.LINE_AA)
    if label:
        cv2.putText(img, label[:60], (4, hgt - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 255, 255), 1, cv2.LINE_AA)
    return img
