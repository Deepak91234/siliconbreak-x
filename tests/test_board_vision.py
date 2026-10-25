"""Webcam observation of the security demo on the Basys 3 (SiliconBreak X physical evidence).

Frames come from the synthetic board renderer (a test double for the camera) showing
the LED pattern the board would show for each demo step; everything else is real:
LED detection, per-step measurement, verdicts, evidence files, provenance checks, and
(in the API test) the HTTP routes plus the /ws/vision binary frame stream.
"""

from __future__ import annotations

import importlib
import json
import struct
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from backend.security.board_vision import DARK_STEP, BoardObservationSession
from backend.security.cases import get_case
from backend.vision.synthetic import SceneConfig, StreamConfig, SyntheticBoard, frame_stream

CASE = "debug_bypass_demo"
VULN_SIM = {"D1": "0x80A5", "D2": "0x4000", "D3": "0x80A5", "D4": "0x80BC"}
FIXED_SIM = {"D1": "0x4000", "D2": "0x4000", "D3": "0x80A5", "D4": "0x80BC"}
STEP_S = 2.5
GAP_S = 0.6


def _schedule(patterns: dict[str, str]) -> list[tuple[str, float, float, int]]:
    out, t = [], 0.3
    for sid in [DARK_STEP, "D1", "D2", "D3", "D4"]:
        out.append((sid, t, t + STEP_S, int(patterns.get(sid, "0x0000"), 16)))
        t += STEP_S + GAP_S
    return out


def _state_fn(schedule):
    def fn(t: float) -> dict[int, int]:
        for _, t0, t1, v in schedule:
            if t0 <= t < t1:
                return {i: (v >> i) & 1 for i in range(16)}
        return {}          # button released between steps: every LED dark
    return fn


def _drive(feed, start, end, schedule, board: SyntheticBoard, *, skip: set[str] = frozenset(), moves=()):
    """Stream frames; start/end steps at the schedule's boundaries (as the dashboard's Capture button does)."""
    total = schedule[-1][2] + 0.4
    pending = [s for s in schedule if s[0] not in skip]
    active = None
    first = True
    for t_ms, jpeg in frame_stream(board, _state_fn(schedule), total, StreamConfig(moves=list(moves))):
        t = t_ms / 1000.0
        if active and t >= active[2]:
            end()
            active = None
        if active is None and pending and t >= pending[0][1]:
            active = pending.pop(0)
            start(active[0])
        feed(t_ms, jpeg, first)
        first = False
    if active:
        end()


def _session(tmp_path: Path, simulated: dict[str, str]) -> BoardObservationSession:
    case = get_case(CASE)
    return BoardObservationSession("bo_test", audit_id="sa_x", variant="vulnerable", variant_label="vulnerable",
                                   steps=list(case.board.demo), simulated=simulated,
                                   programmed={"confirmed": True}, evidence_dir=tmp_path / "ev")


def _run(tmp_path, simulated, shown, **kw):
    board = SyntheticBoard(kw.pop("scene", None))
    sess = _session(tmp_path, simulated)

    def feed(t_ms, jpeg, first):
        if first:
            sess.set_roi(sess.auto_roi(jpeg, board.led_box(0, 16)))
        sess.ingest_frame(t_ms, jpeg)

    _drive(feed, sess.start_step, sess.end_step, _schedule(shown), board, **kw)
    return sess, sess.finish()


def test_vulnerable_board_shows_the_leak(tmp_path):
    sess, res = _run(tmp_path, VULN_SIM, VULN_SIM)
    # captured first, the dark reference cannot be read yet (nothing has toggled); Finish re-reads it
    live_dark = sess.to_dict()["step_results"][DARK_STEP]
    assert live_dark["status"] == "inconclusive" and "not calibrated yet" in live_dark["reasons"][0]
    by = {s["step_id"]: s for s in res["steps"]}
    assert [s["status"] for s in res["steps"]] == ["observed"] * 5, res["steps"]
    assert by[DARK_STEP]["observed"] == "0x0000"
    assert by["D1"]["observed"] == "0x80A5" and by["D1"]["matches_contract"] is False
    assert by["D1"]["lit"] == [0, 2, 5, 7, 15]
    assert all(by[k]["matches_simulation"] for k in VULN_SIM)
    assert res["verdict"] == "violation_observed"
    assert "D1 lit 0x80A5 where 0x4000 is required" in res["summary"]
    ev = tmp_path / "ev"
    assert json.loads((ev / "observation.json").read_text())["verdict"] == "violation_observed"
    assert (ev / "frames.csv").read_text().count("\n") == res["frames_recorded"] + 1
    assert (ev / by["D1"]["snapshot"]).stat().st_size > 1000


def test_patched_board_meets_the_contract(tmp_path):
    _, res = _run(tmp_path, FIXED_SIM, FIXED_SIM)
    by = {s["step_id"]: s for s in res["steps"]}
    assert by["D1"]["observed"] == "0x4000" and by["D1"]["matches_contract"]
    assert by["D4"]["observed"] == "0x80BC"
    assert res["verdict"] == "contract_met"


def test_board_not_matching_the_simulation_is_flagged(tmp_path):
    # the session expects the patched design, but the board still leaks (e.g. reprogrammed elsewhere)
    _, res = _run(tmp_path, FIXED_SIM, VULN_SIM)
    assert res["verdict"] == "unexpected"
    assert "D1" in res["summary"]


def test_missing_step_is_inconclusive_not_guessed(tmp_path):
    _, res = _run(tmp_path, VULN_SIM, VULN_SIM, skip={"D3"})
    by = {s["step_id"]: s for s in res["steps"]}
    assert by["D3"]["status"] == "not_run" and by["D3"]["observed"] is None
    assert res["verdict"] == "inconclusive"


def test_dark_scene_cannot_produce_a_verdict(tmp_path):
    _, res = _run(tmp_path, VULN_SIM, VULN_SIM, scene=SceneConfig(exposure=0.08))
    assert res["verdict"] == "inconclusive"
    assert all(s["observed"] is None for s in res["steps"])


def test_short_capture_is_inconclusive(tmp_path):
    case = get_case(CASE)
    board = SyntheticBoard()
    sess = _session(tmp_path, VULN_SIM)
    frames = list(frame_stream(board, lambda t: {}, 1.0))
    sess.set_roi(sess.auto_roi(frames[0][1], board.led_box(0, 16)))
    sess.start_step("D1")
    for t_ms, jpeg in frames[:12]:   # 0.4 s
        sess.ingest_frame(t_ms, jpeg)
    step = sess.end_step()
    assert step["status"] == "inconclusive" and any("recorded only" in r for r in step["reasons"])
    assert case.board.demo[0].id == "D1"


def test_steps_require_framing_and_known_ids(tmp_path):
    sess = _session(tmp_path, VULN_SIM)
    with pytest.raises(RuntimeError):
        sess.start_step("D1")
    board = SyntheticBoard()
    t_ms, jpeg = next(frame_stream(board, lambda t: {}, 0.1))
    sess.set_roi(sess.auto_roi(jpeg, board.led_box(0, 16)))
    with pytest.raises(KeyError):
        sess.start_step("D9")
    with pytest.raises(RuntimeError):
        sess.end_step()


# ------------------------------------------------------------ API + WebSocket

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SILICONBREAK_RUNS_DIR", str(tmp_path / "runs"))
    from backend.core import config
    from backend.storage import store

    config.get_settings.cache_clear()
    store.get_db.cache_clear()
    import backend.api.app as appmod

    importlib.reload(appmod)
    with TestClient(appmod.app, base_url="http://127.0.0.1") as c:
        yield c
    store.get_db.cache_clear()
    config.get_settings.cache_clear()


def _fake_built_audit(sha: str = "ab" * 32) -> dict:
    """An audit whose vulnerable variant was simulated and built (no Vivado needed for this test)."""
    from backend.storage import store

    p = store.create_project("Security audit: test")
    a = store.create_security_audit(project_id=p["id"], source_kind="case", case_id=CASE)
    steps = [{"id": k, "simulated": v} for k, v in VULN_SIM.items()]
    record = {"fpga": {"vulnerable": {"build": {"ok": True, "bitstream_sha256": sha}, "board_sim": {"steps": steps}}}}
    return store.update_security_audit(a["id"], record=record)


def _program_approval(audit_id: str, variant: str, sha: str) -> None:
    from backend.storage import store

    appr = store.create_approval("program_fpga", "Program", "test",
                                 {"bitstream_sha256": sha, "audit_id": audit_id, "variant": variant})
    store.update_approval(appr["id"], status="executed")


def test_camera_api_requires_provenance_and_records_evidence(client):
    c = client
    audit = _fake_built_audit()
    aid = audit["id"]
    url = f"/api/security/audits/{aid}/fpga/vulnerable/camera"

    r = c.post(url)
    assert r.status_code == 409 and "has not programmed" in r.json()["detail"]
    _program_approval("sa_other", "vulnerable", "cd" * 32)          # the board holds a different design
    r = c.post(url)
    assert r.status_code == 409 and "program this variant first" in r.json()["detail"]
    _program_approval(aid, "vulnerable", "ab" * 32)
    prov = c.get(f"/api/security/audits/{aid}/fpga").json()["provenance"]["vulnerable"]
    assert prov["confirmed"] is True

    st = c.post(url).json()
    sid = st["id"]
    assert st["kind"] == "board_observation" and [s["id"] for s in st["steps"]] == [DARK_STEP, "D1", "D2", "D3", "D4"]
    assert sid not in [v["id"] for v in c.get("/api/vision/sessions").json()]  # not a design-verification session
    assert c.post(f"/api/vision/sessions/{sid}/finish").status_code == 404  # generic routes refuse it

    board = SyntheticBoard()
    with c.websocket_connect(f"ws://127.0.0.1/ws/vision/{sid}") as ws:
        def feed(t_ms, jpeg, first):
            ws.send_bytes(struct.pack("<d", t_ms) + jpeg)
            if first:
                # wait until the server has the frame, then frame the LEDs from it
                for _ in range(200):
                    rr = c.post(f"/api/security/camera/{sid}/auto-roi", json={"box": list(board.led_box(0, 16))})
                    if rr.status_code == 200:
                        break
                assert rr.status_code == 200, rr.text

        def start(step_id):
            assert c.post(f"/api/security/camera/{sid}/step", json={"action": "start", "step_id": step_id}).status_code == 200

        def end():
            assert c.post(f"/api/security/camera/{sid}/step", json={"action": "end"}).status_code == 200

        _drive(feed, start, end, _schedule(VULN_SIM), board)

    res = c.post(f"/api/security/camera/{sid}/finish").json()
    assert res["verdict"] == "violation_observed", res["summary"]
    assert res["programmed"]["confirmed"] is True
    rec = c.get(f"/api/security/audits/{aid}").json()["record"]
    obs = rec["fpga"]["vulnerable"]["observations"]
    assert len(obs) == 1 and obs[0]["session_id"] == sid and obs[0]["dir"].startswith("fpga/vulnerable/camera/")
    d1 = next(s for s in obs[0]["steps"] if s["step_id"] == "D1")
    img = c.get(f"/api/security/audits/{aid}/camera-image", params={"path": f"{obs[0]['dir']}/{d1['snapshot']}"})
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg" and img.content[:2] == b"\xff\xd8"
    assert c.get(f"/api/security/audits/{aid}/camera-image", params={"path": "audit.json"}).status_code in (404, 415)
