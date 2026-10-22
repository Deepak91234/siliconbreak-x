"""Webcam verification through the real HTTP + WebSocket API, the way the dashboard drives it.

Frames come from the synthetic board renderer (a test double for the camera) and
travel over /ws/vision/{id} in the browser's binary format: 8-byte little-endian
float64 t_ms followed by JPEG bytes. Everything server-side is real: the
orchestrator builds the design (real Verilator, simulation-only), the session is
created over HTTP, the LED box is drawn via auto-roi on the last received frame,
phases are started/ended over HTTP, and finish stores the verdict on the revision.
"""

from __future__ import annotations

import importlib
import struct
import time

import pytest
from starlette.testclient import TestClient

from backend.vision.synthetic import SyntheticBoard, counter_states, frame_stream

pytestmark = pytest.mark.verilator

DEMO1 = ("Build a 4-bit counter shown on LEDs 0-3. It should count at one increment per second "
         "normally and three increments per second when I hold BTNU.")
SCHEDULE = [("calibrate", 0.2, 4.0), ("O-RATE-normal", 4.0, 12.0), ("O-RATE-fast", 13.0, 17.0)]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CIRCUITLOOP_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
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


def _wait_run(c: TestClient, run_id: str, timeout: float = 240) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = c.get(f"/api/runs/{run_id}").json()
        if r.get("finished_at"):
            return r
        time.sleep(0.25)
    raise AssertionError(f"run {run_id} did not finish")


def _next_frame_msg(ws) -> dict:
    while True:
        msg = ws.receive_json()
        if msg["type"] == "error":
            raise AssertionError(msg["message"])
        if msg["type"] == "frame":
            return msg


def test_demo1_camera_verification_over_websocket(client):
    c = client
    res = c.post("/api/projects", json={"text": DEMO1, "options": {"simulation_only": True}}).json()
    run = _wait_run(c, res["run"]["id"])
    assert run["status"] == "passed_sim_only"
    rid = c.get(f"/api/projects/{res['project']['id']}").json()["revisions"][0]["id"]

    st = c.post("/api/vision/sessions", json={"revision_id": rid}).json()
    sid = st["id"]
    assert [p["id"] for p in st["plan"]] == [s[0] for s in SCHEDULE]
    assert c.get(f"/api/vision/sessions?revision_id={rid}").json()[0]["id"] == sid

    board = SyntheticBoard()
    state_fn = counter_states([0, 1, 2, 3], [(0.0, 1.0), (12.4, 3.0)])  # BTNU pressed at 12.4 s
    pending = list(SCHEDULE)
    active = None
    batch = 0
    with c.websocket_connect(f"ws://127.0.0.1/ws/vision/{sid}") as ws:
        for t_ms, jpeg in frame_stream(board, state_fn, SCHEDULE[-1][2] + 0.1):
            t_s = t_ms / 1000.0
            if active is not None and t_s >= active[2]:
                assert c.post(f"/api/vision/sessions/{sid}/phase", json={"action": "end"}).status_code == 200
                active = None
            if active is None and pending and t_s >= pending[0][1]:
                active = pending.pop(0)
                r = c.post(f"/api/vision/sessions/{sid}/phase", json={"action": "start", "phase_id": active[0]})
                assert r.status_code == 200, r.text
            ws.send_bytes(struct.pack("<d", t_ms) + jpeg)
            batch += 1
            if batch % 3 == 0:  # the server answers every 3rd frame: stay in lockstep with it
                _next_frame_msg(ws)
                if batch == 3:  # first frames received: draw the LED box like the dashboard does
                    x, y, w, h = board.led_box(0, 16)
                    r = c.post(f"/api/vision/sessions/{sid}/auto-roi",
                               json={"box": [x, y, w, h], "count": 16, "first_led": 0})
                    assert r.status_code == 200, r.text
                    assert len(r.json()["roi"]["leds"]) == 16
        if active is not None:
            c.post(f"/api/vision/sessions/{sid}/phase", json={"action": "end"})

    bad = c.post(f"/api/vision/sessions/{sid}/auto-roi", json={"box": [2.0, 2.0, 0.5, 0.5], "count": 4, "first_led": 0})
    assert bad.status_code == 422  # invalid box is a client error, not a server crash

    result = c.post(f"/api/vision/sessions/{sid}/finish").json()
    assert result["status"] == "passed", result["summary"]
    ratio = next(o for o in result["observations"] if o["id"] == "O-RATIO")
    assert ratio["status"] == "passed" and 2.85 <= ratio["measured"]["ratio"] <= 3.15
    assert result["programmed_by_circuitloop"] is False

    rev = c.get(f"/api/revisions/{rid}").json()
    assert rev["verification"]["status"] == "passed"
    assert rev["status"] == "sim_passed"  # not 'verified': CircuitLoop never programmed this revision
    run = c.get(f"/api/runs/{run['id']}").json()
    assert run["status"] == "passed_sim_only"  # an unattributed camera verdict does not upgrade the run
    verify = next(s for s in run["stages"] if s["key"] == "verify")
    assert verify["status"] == "passed" and "not confirmed" in verify["summary"]

    plot = next(e for e in result["evidence"] if e["kind"] == "plot")
    rel = plot["path"].split("/runs/", 1)[1]
    assert c.get(f"/api/files/{rel}").status_code == 200
