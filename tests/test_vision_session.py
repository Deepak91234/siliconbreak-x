"""End-to-end webcam verification tests through VisionSession with synthetic frames.

The synthetic renderer (backend/vision/synthetic.py) is a test double: it draws
a board-like scene whose LEDs follow a known behaviour, including sensor noise,
blur, exposure smear, flicker, shake and timestamp jitter. Every test drives the
session the way the API does: auto_roi + set_roi, start_phase / ingest_frame /
end_phase, finish.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.specifications.ir import DesignSpec
from backend.vision.frames import FrameDecodeError
from backend.vision.session import RoiConfig, VisionSession
from backend.vision.synthetic import (
    SYNTHETIC_MARKER,
    CameraMove,
    SceneConfig,
    StreamConfig,
    SyntheticBoard,
    blinker_states,
    counter_states,
    frame_stream,
    sequence_states,
)

ROOT = Path(__file__).resolve().parents[1]
DEMO1_LEDS = [0, 1, 2, 3]
BLINK_LEDS = list(range(8))
TL_LEDS = [0, 1, 2, 3]
TL_CYCLE = [(1, 4.0), (2, 1.0), (4, 3.0)]          # green 4 s, yellow 1 s, red 3 s


def load_spec(name: str) -> DesignSpec:
    return DesignSpec.model_validate(json.loads((ROOT / "examples" / f"{name}.json").read_text()))


def run_session(spec, state_fn, schedule, evidence_dir, *, scene=None, stream=None, count=16, first_led=0,
                remark_at=None):
    """Drive a VisionSession like the browser does.

    schedule: [(phase_id, start_s, end_s), ...] on the scene clock. The ROI is
    marked with auto_roi on the first frame (and again at `remark_at`, using the
    box shifted by all camera moves so far), then frames stream continuously.
    """
    board = SyntheticBoard(scene)
    sess = VisionSession("vs-test", spec, evidence_dir)
    total = max(e for _, _, e in schedule) + 0.1
    pending = sorted(schedule, key=lambda s: s[1])
    moves = list(stream.moves) if stream else []
    active = None
    for t_ms, jpeg in frame_stream(board, state_fn, total, stream):
        t_s = t_ms / 1000.0
        remark = remark_at is not None and t_s >= remark_at
        if sess.roi is None or remark:
            x, y, w, h = board.led_box(first_led, count)
            dx = sum(m.dx_px for m in moves if m.at_s <= t_s) / board.cfg.width
            dy = sum(m.dy_px for m in moves if m.at_s <= t_s) / board.cfg.height
            sess.set_roi(sess.auto_roi(jpeg, (x + dx, y + dy, w, h), count, first_led))
            if remark:
                remark_at = None
        if active is not None and t_s >= active[2]:
            sess.end_phase()
            active = None
        if active is None and pending and t_s >= pending[0][1]:
            active = pending.pop(0)
            sess.start_phase(active[0])
        sess.ingest_frame(t_ms, jpeg)
    if active is not None:
        sess.end_phase()
    return sess, sess.finish()


def obs(result, obs_id):
    return next(o for o in result.observations if o["id"] == obs_id)


def phys(result, req_id):
    return next(r for r in result.requirements if r["id"] == req_id)["physical_status"]


def demo1_states(fast_hz=3.0, direction=1, press_at=12.4):
    """Demo 1 board: 1 step/s, BTNU pressed at `press_at` -> fast_hz steps/s."""
    return counter_states(DEMO1_LEDS, [(0.0, 1.0), (press_at, fast_hz)], direction=direction)


DEMO1_SCHEDULE = [("calibrate", 0.2, 4.0), ("O-RATE-normal", 4.0, 12.0), ("O-RATE-fast", 13.0, 17.0)]
BLINK_SCHEDULE = [("calibrate", 0.2, 4.0), ("O-RATE-slow", 4.0, 9.0), ("O-RATE-fast", 10.0, 13.5)]
TL_SCHEDULE = [("calibrate", 0.2, 4.0), ("O-FSM", 4.0, 22.0)]


def assert_no_physical_verdict(result):
    """Insufficient evidence must never turn into observed_pass / observed_fail for measured requirements."""
    bad = {r["id"]: r["physical_status"] for r in result.requirements
           if r["id"] != "R-IDLE" and r["physical_status"] in ("observed_pass", "observed_fail")}
    assert not bad, bad


@pytest.fixture(scope="module")
def demo1_pass(tmp_path_factory):
    ev = tmp_path_factory.mktemp("demo1") / "evidence"
    sess, res = run_session(load_spec("demo1_counter_spec"), demo1_states(), DEMO1_SCHEDULE, ev)
    return sess, res, ev


# ----------------------------------------------------------------------------- Demo 1 counter


def test_demo1_plan_phases(demo1_pass):
    sess, _, _ = demo1_pass
    ids = [p.id for p in sess.plan]
    assert ids == ["calibrate", "O-RATE-normal", "O-RATE-fast"]
    fast = next(p for p in sess.plan if p.id == "O-RATE-fast")
    assert fast.conditions and fast.conditions[0]["input"] == "speed_btn" and fast.conditions[0]["level"] == 1
    assert all(p.min_duration_s > 0 for p in sess.plan)


def test_demo1_counter_passes_with_ratio_three(demo1_pass):
    _, res, _ = demo1_pass
    assert res.status == "passed", res.summary
    normal, fast, ratio = obs(res, "O-RATE-normal"), obs(res, "O-RATE-fast"), obs(res, "O-RATIO")
    assert normal["status"] == fast["status"] == ratio["status"] == "passed"
    assert normal["measured"]["hz"] == pytest.approx(1.0, rel=0.02)
    assert fast["measured"]["hz"] == pytest.approx(3.0, rel=0.02)
    assert ratio["measured"]["ratio"] == pytest.approx(3.0, rel=0.03)
    assert ratio["ci"][0] <= 3.0 <= ratio["ci"][1]
    assert normal["n_events"] >= 5 and fast["n_events"] >= 5
    for rid in ("R-RATE-normal", "R-RATE-fast", "R-RATIO", "R-DIR"):
        assert phys(res, rid) == "observed_pass", rid
    assert phys(res, "R-IDLE") == "observed_pass"       # LED4..LED15 inside the ROI stayed dark
    assert res.quality["flags"] == []


def test_debounce_requirement_not_observable(demo1_pass):
    _, res, _ = demo1_pass
    deb = next(r for r in res.requirements if r["id"] == "R-DEB-speed_btn")
    assert deb["physical_status"] == "not_observable"
    assert deb["note"]


def test_evidence_files_written(demo1_pass):
    _, res, ev = demo1_pass
    kinds = {e["kind"] for e in res.evidence}
    assert {"csv", "plot", "json", "snapshot"} <= kinds
    for e in res.evidence:
        p = Path(e["path"])
        assert p.is_file() and p.stat().st_size > 0, e
        assert ev in p.parents
    data = json.loads((ev / "measurements.json").read_text())
    assert data["result"]["status"] == "passed"
    header = (ev / "frames.csv").read_text().splitlines()[0]
    assert "t_ms" in header
    assert (ev / "led_levels.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_session_state_is_json_safe(demo1_pass):
    sess, res, _ = demo1_pass
    d = sess.to_dict()
    for key in ("status", "plan", "current_phase", "roi", "live", "result"):
        assert key in d
    assert d["status"] == "finished" and d["current_phase"] is None
    assert len(d["roi"]["leds"]) == 16
    json.dumps(d, allow_nan=False)
    dumped = res.model_dump()
    json.dumps(dumped, allow_nan=False)
    assert set(dumped) >= {"status", "summary", "observations", "requirements", "quality", "evidence"}
    for r in dumped["requirements"]:
        assert r["physical_status"] in ("observed_pass", "observed_fail", "inconclusive", "not_observable",
                                        "not_observed")
    for e in dumped["evidence"]:
        assert set(e) >= {"name", "path", "kind"}


def test_demo1_defective_two_to_one_fails(tmp_path):
    _, res = run_session(load_spec("demo1_counter_spec"), demo1_states(fast_hz=2.0), DEMO1_SCHEDULE, tmp_path)
    assert res.status == "failed"
    assert obs(res, "O-RATE-fast")["status"] == "failed"
    assert obs(res, "O-RATE-fast")["measured"]["hz"] == pytest.approx(2.0, rel=0.03)
    assert obs(res, "O-RATIO")["status"] == "failed"
    assert obs(res, "O-RATIO")["measured"]["ratio"] == pytest.approx(2.0, rel=0.03)
    assert phys(res, "R-RATE-fast") == "observed_fail"
    assert phys(res, "R-RATIO") == "observed_fail"
    assert phys(res, "R-RATE-normal") == "observed_pass"


def test_demo1_wrong_direction_fails(tmp_path):
    _, res = run_session(load_spec("demo1_counter_spec"), demo1_states(direction=-1), DEMO1_SCHEDULE, tmp_path)
    assert res.status == "failed"
    assert phys(res, "R-DIR") == "observed_fail"
    assert any("wrong way" in n for n in obs(res, "O-RATE-normal")["notes"])


def test_robust_to_low_fps_jitter_and_dropped_frames(tmp_path):
    stream = StreamConfig(fps=15.0, jitter_ms=4.0, drop_prob=0.08, seed=11)
    _, res = run_session(load_spec("demo1_counter_spec"), demo1_states(), DEMO1_SCHEDULE, tmp_path, stream=stream)
    assert res.status == "passed", res.summary
    assert obs(res, "O-RATIO")["measured"]["ratio"] == pytest.approx(3.0, rel=0.05)


# ----------------------------------------------------------------------------- evidence quality


def test_too_dark_is_inconclusive(tmp_path):
    _, res = run_session(load_spec("demo1_counter_spec"), demo1_states(), DEMO1_SCHEDULE, tmp_path,
                         scene=SceneConfig(exposure=0.12))
    assert res.status == "inconclusive"
    assert "too_dark" in res.quality["flags"]
    assert_no_physical_verdict(res)
    assert phys(res, "R-IDLE") != "observed_pass"     # darkness is not evidence that an LED is off


def test_low_contrast_is_inconclusive(tmp_path):
    _, res = run_session(load_spec("demo1_counter_spec"), demo1_states(), DEMO1_SCHEDULE, tmp_path,
                         scene=SceneConfig(led_gain=0.04))
    assert res.status == "inconclusive"
    assert "low_contrast" in res.quality["flags"]
    assert_no_physical_verdict(res)


def test_large_camera_move_is_inconclusive_with_flag(tmp_path):
    stream = StreamConfig(moves=[CameraMove(at_s=7.0, dx_px=-22.0, dy_px=12.0)])
    _, res = run_session(load_spec("demo1_counter_spec"), demo1_states(), DEMO1_SCHEDULE, tmp_path, stream=stream)
    assert res.status == "inconclusive"
    assert "camera_moved" in res.quality["flags"]
    assert "camera_moved" in obs(res, "O-RATE-fast")["flags"]
    assert res.quality["camera_moved_frames"] > 0
    assert_no_physical_verdict(res)


def test_remarking_roi_after_camera_move_recovers(tmp_path):
    stream = StreamConfig(moves=[CameraMove(at_s=2.0, dx_px=-22.0, dy_px=12.0)])
    _, res = run_session(load_spec("demo1_counter_spec"), demo1_states(), DEMO1_SCHEDULE, tmp_path, stream=stream,
                         remark_at=2.5)
    assert res.status == "passed", res.summary
    assert res.quality["roi_epochs"] == 2


def test_too_short_phase_is_inconclusive(tmp_path):
    schedule = [("calibrate", 0.2, 4.0), ("O-RATE-normal", 4.0, 8.0), ("O-RATE-fast", 9.0, 13.0)]
    _, res = run_session(load_spec("demo1_counter_spec"), demo1_states(press_at=8.4), schedule, tmp_path)
    normal = obs(res, "O-RATE-normal")
    assert normal["status"] == "inconclusive"
    assert "phase_too_short" in normal["flags"]
    assert obs(res, "O-RATE-fast")["status"] == "passed"
    assert obs(res, "O-RATIO")["status"] == "inconclusive"
    assert res.status == "inconclusive"
    assert phys(res, "R-RATE-normal") == "inconclusive"


def test_phase_not_run_is_not_observed(tmp_path):
    schedule = [("calibrate", 0.2, 4.0), ("O-RATE-normal", 4.0, 12.0)]
    _, res = run_session(load_spec("demo1_counter_spec"), demo1_states(), schedule, tmp_path)
    assert obs(res, "O-RATE-fast")["status"] == "not_observed"
    assert res.status == "inconclusive"
    assert phys(res, "R-RATE-normal") == "observed_pass"
    assert phys(res, "R-RATE-fast") == "not_observed"


def test_roi_without_unused_leds_reports_idle_not_observed(tmp_path):
    _, res = run_session(load_spec("demo1_counter_spec"), demo1_states(), DEMO1_SCHEDULE, tmp_path, count=4)
    assert res.status == "passed", res.summary
    assert all(o["id"] != "O-IDLE" for o in res.observations)
    assert phys(res, "R-IDLE") == "not_observed"


# ----------------------------------------------------------------------------- blinker


def test_blinker_rates_pass(tmp_path):
    fn = blinker_states(BLINK_LEDS, [(0.0, 1.0), (9.4, 2.0)])
    _, res = run_session(load_spec("blinker_spec"), fn, BLINK_SCHEDULE, tmp_path)
    assert res.status == "passed", res.summary
    slow, fast = obs(res, "O-RATE-slow"), obs(res, "O-RATE-fast")
    assert slow["measured"]["hz"] == pytest.approx(1.0, rel=0.02)
    assert fast["measured"]["hz"] == pytest.approx(2.0, rel=0.02)
    # 2 Hz at 30 fps is phase-locked to the camera: the duty estimate is off by
    # up to a frame, which the tolerance must absorb.
    assert fast["measured"]["duty"] == pytest.approx(0.5, abs=0.07)
    assert obs(res, "O-RATIO")["measured"]["ratio"] == pytest.approx(2.0, rel=0.03)
    assert phys(res, "R-PATTERN") == "observed_pass"


def test_blinker_wrong_rate_fails(tmp_path):
    fn = blinker_states(BLINK_LEDS, [(0.0, 1.0), (9.4, 1.5)])
    _, res = run_session(load_spec("blinker_spec"), fn, BLINK_SCHEDULE, tmp_path)
    assert res.status == "failed"
    assert phys(res, "R-RATE-fast") == "observed_fail"
    assert obs(res, "O-RATIO")["status"] == "failed"


def test_blinker_wrong_duty_fails(tmp_path):
    fn = blinker_states(BLINK_LEDS, [(0.0, 1.0), (9.4, 2.0)], duty=0.25)
    _, res = run_session(load_spec("blinker_spec"), fn, BLINK_SCHEDULE, tmp_path)
    assert res.status == "failed"
    assert any("duty" in n for n in obs(res, "O-RATE-slow")["notes"])


def test_blinker_wrong_pattern_fails(tmp_path):
    fn = blinker_states(BLINK_LEDS, [(0.0, 1.0), (9.4, 2.0)], pattern="alternate")
    _, res = run_session(load_spec("blinker_spec"), fn, BLINK_SCHEDULE, tmp_path)
    assert res.status == "failed"
    assert phys(res, "R-PATTERN") == "observed_fail"


# ----------------------------------------------------------------------------- traffic light


def test_traffic_light_durations_pass(tmp_path):
    _, res = run_session(load_spec("traffic_light_spec"), sequence_states(TL_LEDS, TL_CYCLE), TL_SCHEDULE, tmp_path)
    assert res.status == "passed", res.summary
    visits = obs(res, "O-FSM")["measured"]["visits"]
    for name, dur in (("green", 4.0), ("yellow", 1.0), ("red", 3.0)):
        assert visits[name], name
        for v in visits[name]:
            assert v["duration_s"] == pytest.approx(dur, abs=0.06), (name, v)
    for rid in ("R-FSM-SEQ", "R-FSM-OUT", "R-FSM-T-green", "R-FSM-T-yellow", "R-FSM-T-red"):
        assert phys(res, rid) == "observed_pass", rid
    # no guided phase presses the buttons: request/reset are left to simulation
    assert phys(res, "R-FSM-REQ") == "not_observed"
    assert phys(res, "R-DEB-walk_btn") == "not_observable"


def test_traffic_light_wrong_duration_fails(tmp_path):
    fn = sequence_states(TL_LEDS, [(1, 4.0), (2, 2.0), (4, 3.0)])
    _, res = run_session(load_spec("traffic_light_spec"), fn, TL_SCHEDULE, tmp_path)
    assert res.status == "failed"
    assert phys(res, "R-FSM-T-yellow") == "observed_fail"
    assert phys(res, "R-FSM-T-green") == "observed_pass"


def test_traffic_light_wrong_order_fails(tmp_path):
    fn = sequence_states(TL_LEDS, [(1, 4.0), (4, 3.0), (2, 1.0)])
    _, res = run_session(load_spec("traffic_light_spec"), fn, TL_SCHEDULE, tmp_path)
    assert res.status == "failed"
    assert phys(res, "R-FSM-SEQ") == "observed_fail"


# ----------------------------------------------------------------------------- API-level behaviour


def test_session_api_errors_and_unavailable(tmp_path):
    spec = load_spec("demo1_counter_spec")
    sess = VisionSession("vs-api", spec, tmp_path / "ev")
    d = sess.to_dict()
    assert d["status"] == "setup" and d["roi"] is None and d["result"] is None
    json.dumps(d, allow_nan=False)
    with pytest.raises(RuntimeError):
        sess.start_phase("calibrate")                 # no ROI yet
    with pytest.raises(RuntimeError):
        sess.end_phase()
    with pytest.raises(FrameDecodeError):
        sess.ingest_frame(0.0, b"not a jpeg at all")
    board = SyntheticBoard()
    frames = list(frame_stream(board, demo1_states(), 0.2))
    assert SYNTHETIC_MARKER in frames[0][1]
    fr = sess.ingest_frame(*frames[0])
    assert "no_roi" in fr.quality["flags"]
    json.dumps(fr.model_dump(), allow_nan=False)
    with pytest.raises(ValueError):
        sess.ingest_frame(frames[0][0], frames[1][1])  # timestamp not increasing
    sess.set_roi(RoiConfig.model_validate({"leds": [{"led_index": 0, "x": 0.85, "y": 0.5, "w": 0.04, "h": 0.1}]}))
    with pytest.raises(KeyError):
        sess.start_phase("no-such-phase")
    plan = sess.start_phase("calibrate")
    assert plan.id == "calibrate" and sess.to_dict()["current_phase"] == "calibrate"
    fr = sess.ingest_frame(*frames[1])
    assert set(fr.states) == {0}
    summary = sess.end_phase()
    assert summary["phase_id"] == "calibrate" and summary["status"] == "inconclusive"
    json.dumps(summary, allow_nan=False)
    res = sess.finish()
    assert res.status == "inconclusive"
    json.dumps(res.model_dump(), allow_nan=False)


def test_no_frames_is_unavailable(tmp_path):
    sess = VisionSession("vs-empty", load_spec("traffic_light_spec"), tmp_path / "ev")
    res = sess.finish()
    assert res.status == "unavailable"
    assert all(r["physical_status"] in ("not_observed", "not_observable") for r in res.requirements)
    json.dumps(sess.to_dict(), allow_nan=False)
