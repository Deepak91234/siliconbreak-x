"""Frame intake for webcam verification: JPEG decoding, image features, frame timing.

Frames arrive from the browser (getUserMedia -> canvas -> JPEG) together with a
client timestamp `t_ms` taken from `performance.now()`. That clock is monotonic
and has sub-millisecond resolution, so every timing measurement in the vision
module uses these client timestamps -- never the server's receive time, which
adds network and event-loop jitter that has nothing to do with the LEDs.

The bytes are untrusted input: they are size-checked before decoding and the
decoded image dimensions are bounded, so a hostile or broken client cannot make
the server allocate arbitrarily large images.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

import cv2
import numpy as np

# The browser client sends <= 640x480 (docs/api.md). Other clients may send up
# to 1080p; anything larger is refused rather than silently downscaled so the
# ROI geometry the user drew always refers to the frames we analyse.
MAX_JPEG_BYTES = 4 * 1024 * 1024
MAX_FRAME_DIM = 1920
MIN_FRAME_DIM = 32

# Width of the grayscale thumbnail used for camera-movement detection. 160 px
# keeps phase correlation well under a millisecond while still resolving
# sub-pixel shifts (phase correlation is sub-pixel accurate).
MOTION_WIDTH = 160

# An interval longer than this multiple of the median frame interval means the
# browser skipped frames (tab throttling, USB bandwidth, a busy main thread).
DROP_FACTOR = 1.5


class FrameDecodeError(ValueError):
    """The bytes are not a usable JPEG frame."""


def decode_jpeg(data: bytes) -> np.ndarray:
    """Decode JPEG bytes to a BGR uint8 image, enforcing size limits."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise FrameDecodeError("frame must be bytes")
    if len(data) < 4:
        raise FrameDecodeError("frame is empty")
    if len(data) > MAX_JPEG_BYTES:
        raise FrameDecodeError(f"frame is {len(data)} bytes; the limit is {MAX_JPEG_BYTES}")
    buf = np.frombuffer(bytes(data), dtype=np.uint8)
    if buf[0] != 0xFF or buf[1] != 0xD8:
        raise FrameDecodeError("frame is not a JPEG (missing SOI marker)")
    img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if img is None or img.ndim != 3 or img.shape[2] != 3:
        raise FrameDecodeError("JPEG could not be decoded")
    h, w = img.shape[:2]
    if max(h, w) > MAX_FRAME_DIM or min(h, w) < MIN_FRAME_DIM:
        raise FrameDecodeError(f"frame size {w}x{h} is outside {MIN_FRAME_DIM}..{MAX_FRAME_DIM} px")
    return img


def encode_jpeg(bgr: np.ndarray, quality: int = 85) -> bytes:
    ok, buf = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    if not ok:
        raise RuntimeError("JPEG encoding failed")
    return buf.tobytes()


def green_signal(bgr: np.ndarray) -> np.ndarray:
    """Per-pixel brightness emphasising green LED light, roughly 0..1.1.

    The Basys 3 user LEDs (LD0-LD15) are green. A webcam renders a lit LED as a
    near-white clipped core inside a green halo, while reflections, white
    silkscreen and switch bodies are neutral grey. The feature is a
    green-weighted luminance (the clipped white core still scores 1.0) plus a
    bonus for green excess over max(R, B), which lifts the halo and makes a
    green LED out-score a grey reflection of equal luminance.
    """
    f = bgr.astype(np.float32)
    b, g, r = f[..., 0], f[..., 1], f[..., 2]
    excess = np.clip(g - np.maximum(r, b), 0.0, None)
    return (0.6 * g + 0.2 * r + 0.2 * b + 0.5 * excess) * (1.0 / 255.0)


def motion_thumbnail(bgr: np.ndarray) -> tuple[np.ndarray, float]:
    """Downsampled float32 grayscale for phase correlation, and its scale factor.

    Returns (thumbnail, full_px_per_thumb_px).
    """
    h, w = bgr.shape[:2]
    tw = min(MOTION_WIDTH, w)
    th = max(8, int(round(h * tw / w)))
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(gray, (tw, th), interpolation=cv2.INTER_AREA).astype(np.float32)
    # A light blur suppresses sensor noise and JPEG blocking, which otherwise
    # adds a noise floor to the correlation peak.
    small = cv2.GaussianBlur(small, (3, 3), 0)
    return small, w / tw


@dataclass(frozen=True)
class TimingStats:
    """Frame-timing statistics derived from client timestamps only."""

    frames: int
    duration_s: float
    fps: float | None            # 1000 / median interval
    median_interval_ms: float | None
    jitter_ms: float | None      # robust std of the regular (non-dropped) intervals
    max_gap_ms: float | None
    dropped_frames: int          # frames the client evidently skipped

    def as_dict(self) -> dict:
        def r(v: float | None, n: int = 2) -> float | None:
            return None if v is None or not math.isfinite(v) else round(v, n)

        return {
            "frames": self.frames,
            "duration_s": r(self.duration_s, 3),
            "fps": r(self.fps),
            "median_interval_ms": r(self.median_interval_ms),
            "jitter_ms": r(self.jitter_ms),
            "max_gap_ms": r(self.max_gap_ms),
            "dropped_frames": self.dropped_frames,
        }


def timing_stats(t_ms: np.ndarray | list[float]) -> TimingStats:
    t = np.asarray(t_ms, dtype=np.float64)
    n = int(t.size)
    if n < 2:
        return TimingStats(n, 0.0, None, None, None, None, 0)
    dt = np.diff(t)
    med = float(np.median(dt))
    if med <= 0:
        return TimingStats(n, float(t[-1] - t[0]) / 1000.0, None, None, None, None, 0)
    regular = dt[dt <= DROP_FACTOR * med]
    if regular.size >= 3:
        mad = float(np.median(np.abs(regular - np.median(regular))))
        jitter = 1.4826 * mad
        # MAD collapses to 0 when timestamps are quantised to identical
        # intervals; fall back to the plain std so jitter is never understated.
        if jitter == 0.0:
            jitter = float(np.std(regular))
    else:
        jitter = float(np.std(dt))
    gaps = dt[dt > DROP_FACTOR * med]
    dropped = int(sum(max(0, int(round(g / med)) - 1) for g in gaps))
    return TimingStats(
        frames=n,
        duration_s=float(t[-1] - t[0]) / 1000.0,
        fps=1000.0 / med,
        median_interval_ms=med,
        jitter_ms=jitter,
        max_gap_ms=float(dt.max()),
        dropped_frames=dropped,
    )


class FrameClock:
    """Accepts strictly increasing client timestamps and keeps rolling statistics.

    A timestamp that does not increase (a reordered WebSocket message, a client
    that reset its clock) is rejected: accepting it would corrupt every interval
    measurement downstream.
    """

    def __init__(self, window: int = 90) -> None:
        self._recent: deque[float] = deque(maxlen=window)
        self.last_t_ms: float | None = None
        self.rejected = 0

    def accept(self, t_ms: float) -> bool:
        if not isinstance(t_ms, (int, float)) or not math.isfinite(t_ms) or t_ms < 0:
            self.rejected += 1
            return False
        if self.last_t_ms is not None and t_ms <= self.last_t_ms:
            self.rejected += 1
            return False
        self.last_t_ms = float(t_ms)
        self._recent.append(float(t_ms))
        return True

    def recent_stats(self) -> TimingStats:
        return timing_stats(list(self._recent))
