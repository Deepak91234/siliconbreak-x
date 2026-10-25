"""End-to-end through the HTTP API with the real Verilator (simulation-only, offline interpreter).

No mocks of tools: the orchestrator runs the real offline intent parser, template
generator, Verilator lint/harness/oracle. Vivado and hardware are excluded via
simulation_only so the test is fast and needs no board; a separate opt-in test
(--run-vivado) covers the Vivado path.
"""

from __future__ import annotations

import asyncio
import importlib
import os
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
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")  # force the honest offline path
    from backend.core import config
    from backend.storage import store

    config.get_settings.cache_clear()
    store.get_db.cache_clear()
    import backend.api.app as appmod

    importlib.reload(appmod)
    async with appmod.app.router.lifespan_context(appmod.app):
        transport = httpx.ASGITransport(app=appmod.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
            yield c
    store.get_db.cache_clear()
    config.get_settings.cache_clear()


async def _wait_run(c: httpx.AsyncClient, run_id: str, timeout: float = 240) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = (await c.get(f"/api/runs/{run_id}")).json()
        if r.get("finished_at"):
            return r
        await asyncio.sleep(0.25)
    raise AssertionError(f"run {run_id} did not finish: {r}")


def _stages(run: dict) -> dict[str, str]:
    return {s["key"]: s["status"] for s in run["stages"]}


async def test_demo1_then_demo2_simulation_only(client):
    c = client
    assert (await c.get("/api/health")).json()["ok"]

    res = (await c.post("/api/projects", json={"text": DEMO1, "options": {"simulation_only": True}})).json()
    run = await _wait_run(c, res["run"]["id"])
    st = _stages(run)
    assert run["status"] == "passed_sim_only", run
    for k in ("spec", "validate", "rtl", "lint", "tests", "simulate"):
        assert st[k] == "passed", (k, run["stages"])
    assert st["repair"] == "skipped"
    assert all(st[k] == "skipped" for k in ("synth", "impl", "timing", "bitstream", "detect", "program", "verify"))

    pid = res["project"]["id"]
    proj = (await c.get(f"/api/projects/{pid}")).json()
    assert len(proj["revisions"]) == 1
    r1 = (await c.get(f"/api/revisions/{proj['revisions'][0]['id']}")).json()
    assert r1["interpreter"] == "offline-rules"
    assert r1["generator"].startswith("template/")
    assert r1["sim_status"] == "passed"
    assert r1["spec"]["behavior"]["direction"] == "up"
    core = (await c.get(f"/api/revisions/{r1['id']}/files/cl_core.v")).json()["content"]
    assert "module cl_core" in core
    sim = (await c.get(f"/api/revisions/{r1['id']}/simulation")).json()
    assert sim["status"] == "passed" and sim["failed_checks"] == 0 and sim["passed_checks"] > 0

    # Demo 2: conversational modification of the same project
    res2 = (await c.post(f"/api/projects/{pid}/messages", json={"text": DEMO2, "options": {"simulation_only": True}})).json()
    run2 = await _wait_run(c, res2["run"]["id"])
    assert run2["status"] == "passed_sim_only", run2
    proj = (await c.get(f"/api/projects/{pid}")).json()
    assert len(proj["revisions"]) == 2
    r2 = (await c.get(f"/api/revisions/{proj['revisions'][1]['id']}")).json()
    assert r2["kind"] == "modification" and r2["parent_id"] == r1["id"]
    beh = r2["spec"]["behavior"]
    assert beh["direction"] == "down" and beh["reset_input"]
    assert any(i["port"] == "btnC" for i in r2["spec"]["inputs"])
    assert any(m.get("when_input") for m in beh["rate_modes"]), "fast mode must be kept"
    diff = (await c.get(f"/api/revisions/{r2['id']}/diff")).json()
    assert diff["against"] == r1["id"] and diff["spec_diff"]

    # chat history contains the user's requests and agent replies
    roles = [m["role"] for m in proj["messages"]]
    assert roles.count("user") == 2 and "agent" in roles

    # events were persisted for replay
    evs = (await c.get(f"/api/runs/{run2['id']}/events")).json()
    assert any(e["type"] == "stage.updated" for e in evs)


async def test_fault_injection_without_llm_reports_repair_unavailable(client):
    c = client
    faults = (await c.get("/api/faults")).json()
    assert faults, "mutation catalogue must expose core faults"
    fault = next((f["name"] for f in faults if not f["families"] or "counter" in f["families"]), None)
    assert fault
    res = (await c.post("/api/projects", json={"text": DEMO1, "options": {"simulation_only": True,
                                                                           "inject_fault": fault}})).json()
    run = await _wait_run(c, res["run"]["id"])
    st = _stages(run)
    assert run["status"] == "failed"
    assert st["lint"] == "failed" or st["simulate"] == "failed", run["stages"]
    assert st["repair"] == "unavailable"  # honest: no LLM configured, so no automatic repair
    assert st["synth"] == "skipped"


async def test_unsupported_request_is_explained(client):
    c = client
    res = (await c.post("/api/projects", json={"text": "Show a bouncing ball on a VGA monitor",
                                               "options": {"simulation_only": True}})).json()
    run = await _wait_run(c, res["run"]["id"])
    assert run["status"] in ("unsupported", "failed")
    assert _stages(run)["spec"] == "failed"
    msgs = (await c.get(f"/api/projects/{res['project']['id']}")).json()["messages"]
    assert any(m["role"] == "agent" and m["kind"] == "error" for m in msgs)


async def test_local_only_access_controls(client):
    c = client
    # DNS rebinding: a foreign Host header is rejected
    assert (await c.get("/api/health", headers={"host": "evil.example"})).status_code == 400
    # a cross-site page cannot trigger writes (CORS alone would not stop a simple POST)
    r = await c.post("/api/approvals/a_nonexistent/approve", headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    # the dashboard's own origin and origin-less local tools are allowed through to the handler
    r = await c.post("/api/approvals/a_nonexistent/approve", headers={"origin": "http://127.0.0.1:5173"})
    assert r.status_code == 404
    assert (await c.post("/api/approvals/a_nonexistent/approve")).status_code == 404
