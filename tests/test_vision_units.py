"""Unit tests for the vision building blocks: frames, ROI, detector, analysis helpers."""

from __future__ import annotations

import math

import numpy as np
import pydantic
import pytest

from backend.vision import analysis as an
from backend.vision.detector import MotionMonitor, calibrate, classify, classify_series, two_means
from backend.vision.frames import FrameDecodeError, decode_jpeg, encode_jpeg, motion_thumbnail, timing_stats
from backend.vision.roi import LedRoi, RoiConfig, pixel_rects, refine_toward_blobs, split_box
from backend.vision.synthetic import SceneConfig, SyntheticBoard


# ----------------------------------------------------------------------------- frames


def test_decode_jpeg_rejects_bad_input():
    for bad in (b"", b"\x00\x01\x02\x03\x04", b"\xff\xd8garbage-not-a-real-jpeg"):
        with pytest.raises(FrameDecodeError):
            decode_jpeg(bad)
    with pytest.raises(FrameDecodeError):
        decode_jpeg(encode_jpeg(np.zeros((16, 16, 3), np.uint8)))     # below the minimum frame size


def test_decode_roundtrip():
    img = np.full((48, 64, 3), 120, np.uint8)
    out = decode_jpeg(encode_jpeg(img, 90))
    assert out.shape == (48, 64, 3)
    assert abs(float(out.mean()) - 120) < 3


def test_timing_stats_detects_dropped_frames():
    t = [k * 33.333 for k in range(60) if k not in (10, 20, 21)]
    st = timing_stats(t)
    assert st.fps == pytest.approx(30.0, rel=0.01)
    assert st.dropped_frames == 3
    assert timing_stats([5.0]).fps is None


# ----------------------------------------------------------------------------- ROI


def test_roi_validation():
    with pytest.raises(pydantic.ValidationError):
        RoiConfig.model_validate({"leds": [{"led_index": 0, "x": 0.9, "y": 0.5, "w": 0.2, "h": 0.1}]})
    with pytest.raises(pydantic.ValidationError):
        RoiConfig.model_validate({"leds": [{"led_index": 2, "x": 0.1, "y": 0.1, "w": 0.1, "h": 0.1},
                                           {"led_index": 2, "x": 0.3, "y": 0.1, "w": 0.1, "h": 0.1}]})
    with pytest.raises(pydantic.ValidationError):
        LedRoi(led_index=16, x=0.1, y=0.1, w=0.1, h=0.1)


def test_split_box_assigns_indices_right_to_left():
    roi = split_box((0.1, 0.4, 0.8, 0.1), 4, 2)
    assert roi.indices() == [5, 4, 3, 2]                 # leftmost cell = highest LED
    xs = [r.x for r in roi.leds]
    assert xs == sorted(xs)
    assert roi.leds[-1].x + roi.leds[-1].w == pytest.approx(0.9)
    with pytest.raises(ValueError):
        split_box((0.1, 0.4, 0.8, 0.1), 4, 14)            # LED17 does not exist
    with pytest.raises(ValueError):
        split_box((1.5, 0.4, 0.8, 0.1), 4, 0)


def test_auto_roi_cells_cover_the_leds():
    board = SyntheticBoard(SceneConfig(shake_px=0.0))
    bgr = board.render({i: 1.0 for i in range(16)})
    box = board.led_box()
    # a sloppy box: shifted by a third of a pitch
    shift = board.pitch_px / 3 / board.cfg.width
    sloppy = (box[0] + shift, box[1], box[2], box[3])
    roi = refine_toward_blobs(bgr, split_box(sloppy, 16, 0), sloppy)
    rects = pixel_rects(roi, board.cfg.width, board.cfg.height)
    for led in range(16):
        x0, y0, x1, y1 = rects[led]
        cx, cy = board.led_center_px(led)
        assert x0 <= cx <= x1 and y0 <= cy <= y1, (led, rects[led], (cx, cy))


def test_motion_monitor_measures_shift():
    board = SyntheticBoard(SceneConfig(shake_px=0.0, noise_sd=0.005))
    ref, scale = motion_thumbnail(board.render({}))
    mon = MotionMonitor()
    mon.set_reference(ref, scale)
    still = mon.measure(motion_thumbnail(board.render({}))[0])
    moved = mon.measure(motion_thumbnail(board.render({}, offset_px=(12.0, -6.0)))[0])
    assert still is not None and still.shift_px < 1.5
    assert moved is not None and moved.dx_px == pytest.approx(12.0, abs=2.0)
    assert moved.dy_px == pytest.approx(-6.0, abs=2.0)


# ----------------------------------------------------------------------------- detector


def test_two_means_and_calibration():
    rng = np.random.default_rng(0)
    x = np.concatenate([rng.normal(0.30, 0.01, 200), rng.normal(0.90, 0.01, 150)])
    m0, m1, sd, n0, n1 = two_means(x)
    assert m0 == pytest.approx(0.30, abs=0.01) and m1 == pytest.approx(0.90, abs=0.01)
    assert (n0, n1) == (200, 150) and sd < 0.02
    cals = calibrate({0: x, 1: rng.normal(0.30, 0.01, 350), 2: rng.normal(0.31, 0.2, 350)})
    assert cals[0].source == "own"
    assert cals[1].source == "pooled" and cals[1].off == pytest.approx(0.30, abs=0.01)   # never-lit LED
    assert cals[2].source == "none"                                                      # pure noise
    assert classify(0.95, cals[0], None) == 1 and classify(0.31, cals[0], None) == 0
    assert classify(0.60, cals[0], None) is None and classify(0.60, cals[0], 1) == 1   # hysteresis
    series = classify_series(np.array([0.3, 0.9, 0.9, 0.3]), np.array([True, True, False, True]), cals[0])
    assert series == [0, 1, None, 0]


def test_unimodal_noise_is_not_calibrated():
    rng = np.random.default_rng(1)
    cals = calibrate({0: rng.normal(0.5, 0.01, 400)})
    assert cals[0].source == "none"


# ----------------------------------------------------------------------------- analysis helpers


def test_fit_rate_exact():
    t = [1000.0 + 333.333 * k for k in range(10)]
    fit = an.fit_rate(t, list(range(10)), [10.0] * 10)
    assert fit is not None and fit.hz == pytest.approx(3.0, rel=1e-4)
    assert fit.max_residual_periods < 1e-6
    half = an.fit_rate(t, list(range(10)), [10.0] * 10, cycles_per_step=0.5)
    assert half.hz == pytest.approx(1.5, rel=1e-4)
    assert an.fit_rate([0.0, 1.0], [0, 1], [1.0, 1.0]) is None


def test_judge_three_zones():
    assert an.judge(1.02, 1.0, 0.001, 0.05)["status"] == "passed"
    assert an.judge(1.06, 1.0, 0.001, 0.05)["status"] == "inconclusive"
    assert an.judge(1.20, 1.0, 0.001, 0.05)["status"] == "failed"
    assert an.judge(0.54, 0.5, 0.01, 0.05, absolute=True)["status"] == "passed"
    assert an.judge(0.56, 0.5, 0.03, 0.05, absolute=True)["status"] == "passed"   # 3 sigma = 0.09


def test_stable_runs_and_events():
    t = np.arange(12) * 33.0
    vals = [0, 0, 0, 1, None, 1, 1, 1, 2, 2, 2, 2]
    runs = an.merge_same(an.stable_runs(vals, t))
    assert [r.value for r in runs] == [0, 1, 2]
    ev = an.events_between(runs)
    assert len(ev) == 2
    assert ev[0].t_ms == pytest.approx(0.5 * (66.0 + 165.0))   # single-frame '1' at 99 ms is not stable
    assert math.isclose(ev[1].t_ms, 0.5 * (231.0 + 264.0))
