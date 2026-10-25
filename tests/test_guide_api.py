"""Hardware-guide, virtual-FPGA, replay, diagram and AI-provider endpoints (real Verilator, no mocks)."""

from __future__ import annotations

import asyncio
import importlib
import time

import httpx
import pytest

pytestmark = pytest.mark.verilator
DEMO1 = ("Build a 4-bit counter shown on LEDs 0-3. It should count at one increment per second "
         "normally and three increments per second when I hold BTNU.")
DEMO2 = "Change the counter so it counts backward, and use BTNC to reset it."


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SILICONBREAK_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    from backend.core import config
    from backend.storage import store

    config.get_settings.cache_clear()
    store.get_db.cache_clear()
    import backend.api.app as appmod

    importlib.reload(appmod)
    async with appmod.app.router.lifespan_context(appmod.app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=appmod.app), base_url="http://127.0.0.1") as c:
            yield c
    store.get_db.cache_clear()
    config.get_settings.cache_clear()


async def _finish(c, run_id, timeout=240):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = (await c.get(f"/api/runs/{run_id}")).json()
        if r.get("finished_at"):
            return r
        await asyncio.sleep(0.25)
    raise AssertionError("run did not finish")


@pytest.fixture
async def demo2(client):
    c = client
    res = (await c.post("/api/projects", json={"text": DEMO1, "options": {"simulation_only": True}})).json()
    await _finish(c, res["run"]["id"])
    pid = res["project"]["id"]
    r2 = (await c.post(f"/api/projects/{pid}/messages", json={"text": DEMO2, "options": {"simulation_only": True}})).json()
    run = await _finish(c, r2["run"]["id"])
    assert run["status"] == "passed_sim_only"
    revs = (await c.get(f"/api/projects/{pid}")).json()["revisions"]
    return c, revs[-1]["id"]


async def test_board_definition(client):
    b = (await client.get("/api/board")).json()
    assert b["part"] == "xc7a35tcpg236-1"
    assert b["ports"]["btnU"]["pins"] == ["T18"] and b["ports"]["led"]["pins"][0] == "U16"


async def test_guide_from_spec_and_scenarios(demo2):
    c, rid = demo2
    g = (await c.get(f"/api/revisions/{rid}/guide")).json()
    ids = {x["id"]: x for x in g["components"]}
    assert ids["btnU"]["pin"] == "T18" and ids["btnC"]["pin"] == "U18" and ids["led0"]["pin"] == "U16"
    assert "reset" in ids["btnC"]["function"]
    titles = [s["title"] for s in g["steps"]]
    assert any("BTNU" in t for t in titles) and any("BTNC" in t for t in titles)
    reset = next(s for s in g["steps"] if "BTNC" in s["title"])
    assert reset["scenario_id"] and reset["sim_status"] == "passed"
    assert "0000" in reset["expected"]
    assert g["programmed_by_siliconbreak"] is False and g["preview"] is False


async def test_example_guide_is_marked_preview(client):
    ex = (await client.get("/api/examples")).json()
    assert any(e["name"] == "traffic_light_spec" for e in ex)
    g = (await client.get("/api/examples/traffic_light_spec/guide")).json()
    assert g["preview"] is True and g["steps"]
    assert (await client.get("/api/examples/..%2Fetc/guide")).status_code == 404


async def test_scenario_trace_replay(demo2):
    c, rid = demo2
    sim = (await c.get(f"/api/revisions/{rid}/simulation")).json()
    sid = next(s["id"] for s in sim["scenarios"] if s["id"] == "reset")
    t = (await c.get(f"/api/revisions/{rid}/scenarios/{sid}/trace")).json()
    assert t["source"] == "verilator" and t["clk_hz"] == 10000
    assert any(e["port"] == "btnC" for e in t["inputs"])
    assert t["outputs"]["led"][0][0] == 0
    assert (await c.get(f"/api/revisions/{rid}/scenarios/..%2F..%2Fx/trace")).status_code == 404
    assert (await c.get(f"/api/revisions/{rid}/scenarios/nope/trace")).status_code == 404


async def test_virtual_fpga_runs_real_rtl(demo2):
    c, rid = demo2
    # Demo 2 counts down from 0: first step after ~1 s wraps to 15 (0b1111 on LD0-LD3).
    r = (await c.post(f"/api/revisions/{rid}/simulate", json={"events": [], "duration_s": 2.5})).json()
    led = r["outputs"]["led"]
    assert r["source"] == "verilator" and led[0] == [0.0, 0]
    assert led[1][1] & 0xF == 0xF and 0.99 <= led[1][0] <= 1.02
    # Holding BTNC (reset) from 1.5 s pins the value at 0 while held.
    r = (await c.post(f"/api/revisions/{rid}/simulate", json={
        "events": [{"t_s": 1.5, "port": "btnC", "value": 1}], "duration_s": 4.0})).json()
    led = r["outputs"]["led"]
    to_zero = [t for t, v in led if t > 1.5 and v & 0xF == 0]
    assert to_zero and to_zero[0] < 1.55          # reset seen ~debounce (10 ms) after the press
    assert all(v & 0xF == 0 for t, v in led if t >= to_zero[0])  # and held at 0 until the end


async def test_virtual_fpga_validation(demo2):
    c, rid = demo2
    assert (await c.post(f"/api/revisions/{rid}/simulate", json={"events": [], "duration_s": 500})).status_code == 422
    bad = {"events": [{"t_s": 0.1, "port": "led", "value": 1}], "duration_s": 1}
    assert (await c.post(f"/api/revisions/{rid}/simulate", json=bad)).status_code == 422
    bad = {"events": [{"t_s": 0.1, "port": "btnU", "value": 7}], "duration_s": 1}
    assert (await c.post(f"/api/revisions/{rid}/simulate", json=bad)).status_code == 422
    bad = {"events": [{"t_s": 3, "port": "btnU", "value": 1}], "duration_s": 1}
    assert (await c.post(f"/api/revisions/{rid}/simulate", json=bad)).status_code == 422


async def test_block_diagram_from_spec_and_rtl(demo2):
    c, rid = demo2
    d = (await c.get(f"/api/revisions/{rid}/diagram")).json()
    kinds = {n["id"]: n for n in d["nodes"]}
    assert kinds["cond:speed_btn"]["source"] == "rtl" and kinds["cond:reset_btn"]["source"] == "rtl"
    assert any(n["sub"].startswith("cl_tick_div") and n["source"] == "rtl" for n in d["nodes"])
    assert "not a synthesized gate-level netlist" in d["label"]
    assert any(e["from"] == "fn:state" and e["to"].startswith("out:") for e in d["edges"])


async def test_llm_status_never_exposes_key(client, monkeypatch):
    s = (await client.get("/api/llm")).json()
    assert s["mode"] == "offline-rules" and s["configured"] is False
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-secret-value")
    s = (await client.post("/api/llm/reload")).json()
    # credit protection: a key alone does not enable billed calls
    assert s["mode"] == "offline-rules" and s["key_present"] and s["key_source"] == "environment" and not s["live_ai"]
    await client.post("/api/ai/live", json={"enabled": True})
    s = (await client.get("/api/llm")).json()
    assert s["mode"] == "model" and s["live_ai"]
    assert "sk-ant" not in str(s)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    assert (await client.post("/api/llm/reload")).json()["mode"] == "offline-rules"
