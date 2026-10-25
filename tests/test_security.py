"""SiliconBreak X: cases, host safety, trusted verifier, formal check, investigation,
repair isolation, the Bitdeer adapter and the audit pipeline (real Verilator/Yosys,
fake model providers, no network)."""

from __future__ import annotations

import asyncio
import importlib
import json
import shutil
import time
from pathlib import Path

import httpx
import pytest

from backend.agent.llm.base import LLMError, LLMResult
from backend.security import formal, investigate as inv, repair as rep
from backend.security.cases import get_case
from backend.security.hostsafety import scan_source
from backend.security.verify import verify_sources

CASE = "debug_bypass_demo"
needs_yosys = pytest.mark.skipif(not shutil.which("yosys"), reason="yosys not installed")


@pytest.fixture
def case():
    c = get_case(CASE)
    assert c is not None
    return c


def _with(case, tmp_path: Path, text: str) -> dict[str, Path]:
    p = tmp_path / "cand" / "debug_access_ctrl.v"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return {"rtl/debug_access_ctrl.v": p}


# ---------------------------------------------------------------- manifests

def test_case_manifest_public_view_hides_ground_truth(case):
    pub = case.public_view()
    assert pub["case_id"] == CASE and pub["source_kind"] == "demo"
    blob = json.dumps(pub)
    assert "ground_truth" not in blob and "reference/" not in blob and "tb_debug_access_ctrl" not in blob
    assert {t["id"] for t in pub["tests"]} >= {"S-UNAUTH-DEBUG-READ", "F-STATUS-READ"}
    assert set(case.trusted_hashes()) == {"tests/tb_debug_access_ctrl.sv", "tests/formal_props.v", "board/basys3_top.v"}


def test_case_paths_cannot_escape(case):
    with pytest.raises(ValueError):
        case.path("../../backend/core/config.py")


# --------------------------------------------------------------- host safety

@pytest.mark.parametrize("snippet,needle", [
    ('initial $display("SB_TEST x S-UNAUTH-READ PASS");', "$display"),
    ('initial $system("rm -rf /");', "$system"),
    ("initial $c(\"system(0)\");", "$c"),
    ('`include "/etc/passwd"', "include"),
    ('import "DPI-C" function int f();', "DPI"),
    ("initial $finish;", "$finish"),
    ('initial begin : b reg [7:0] x; $readmemh("/etc/hosts", x); end', "$readmemh"),
])
def test_host_safety_rejects_host_access_and_forged_verdicts(snippet, needle):
    src = f"module m(input wire clk);\n{snippet}\nendmodule\n"
    issues = scan_source("m.v", src)
    assert issues and any(needle.strip("$`") in i.message for i in issues)


def test_host_safety_ignores_comments_and_strings():
    src = 'module m(input wire a, output wire b);\n// $system("x") `include "y"\nassign b = a; /* $c() */\nendmodule\n'
    assert scan_source("m.v", src) == []


def test_original_case_source_is_host_safe(case):
    for rel in case.editable:
        assert scan_source(rel, case.path(rel).read_text()) == []


# ------------------------------------------------------------------ verifier

@pytest.mark.verilator
async def test_verifier_reproduces_the_vulnerability(case, tmp_path):
    r = await verify_sources(case, {rel: case.path(rel) for rel in case.editable}, tmp_path / "v", label="original")
    assert r.status == "failed"
    assert r.security_status == "failed" and r.functional_status == "passed"
    assert set(r.failed_properties) == {"P-NOLEAK", "P-DEBUG"}
    by = {t.id: t.status for t in r.tests}
    assert by["S-UNAUTH-DEBUG-READ"] == "fail" and by["S-UNAUTH-READ"] == "pass"
    assert (tmp_path / "v" / r.vcd).is_file() and r.trusted_hashes and r.source_hashes
    assert [c.step for c in r.commands] == ["lint", "build", "simulate"]


@pytest.mark.verilator
async def test_verifier_passes_reference_fix(case, tmp_path):
    ref = case.path(case.reference_fix.files["rtl/debug_access_ctrl.v"])
    r = await verify_sources(case, {"rtl/debug_access_ctrl.v": ref}, tmp_path / "v", label="reference")
    assert r.status == "passed", r.message
    assert all(t.status == "pass" for t in r.tests)


@pytest.mark.verilator
async def test_verifier_rejects_over_restrictive_fix_as_functional_regression(case, tmp_path):
    """Denying every SECRET read is not a repair: the functional tests catch it."""
    text = case.path("rtl/debug_access_ctrl.v").read_text().replace("if (authorized || debug_mode) begin", "if (1'b0) begin")
    r = await verify_sources(case, _with(case, tmp_path, text), tmp_path / "v", label="deny-all")
    assert r.status == "failed" and r.security_status == "passed" and r.functional_status == "failed"


@pytest.mark.verilator
async def test_verifier_refuses_forged_verdicts_before_compiling(case, tmp_path):
    text = case.path("rtl/debug_access_ctrl.v").read_text().replace(
        "    reg [7:0] secret_q;", '    reg [7:0] secret_q;\n    initial $display("SB_TEST none S-UNAUTH-DEBUG-READ PASS");')
    r = await verify_sources(case, _with(case, tmp_path, text), tmp_path / "v", label="forged")
    assert r.status == "error" and r.safety_issues and not r.commands


async def test_verifier_rejects_scope_violations(case, tmp_path):
    cands = {"rtl/debug_access_ctrl.v": case.path("rtl/debug_access_ctrl.v"),
             "tests/tb_debug_access_ctrl.sv": case.path("tests/tb_debug_access_ctrl.sv")}
    r = await verify_sources(case, cands, tmp_path / "v", label="scope")
    assert r.status == "error" and any("not editable" in v for v in r.scope_violations)


# -------------------------------------------------------------------- formal

@needs_yosys
async def test_formal_counterexample_and_proof(case, tmp_path):
    bad = await formal.check(case, [case.path("rtl/debug_access_ctrl.v")], tmp_path / "f1", label="orig")
    assert bad.status == "counterexample" and bad.vcd == "cex.vcd"
    step2 = next(s for s in bad.counterexample if s["step"] == 2)["signals"]
    assert step2["authorized"] == "0" and step2["debug_mode"] == "1" and step2["read_req"] == "1"
    good = await formal.check(case, [case.path("reference/debug_access_ctrl.v")], tmp_path / "f2", label="ref")
    assert good.status == "proven"


# ------------------------------------------------------------- investigation

class FakeProvider:
    name = "fake"
    model = "fake-model-1"

    def __init__(self, value=None, error: LLMError | None = None):
        self.value, self.error, self.calls = value, error, []

    async def structured(self, *, purpose, system, messages, output_model, max_tokens=16000):
        self.calls.append({"purpose": purpose, "system": system, "user": messages[-1]["content"]})
        if self.error:
            raise self.error
        v = output_model.model_validate(self.value)
        return LLMResult[output_model](value=v, text=json.dumps(self.value), model=self.model, provider=self.name)


def test_offline_rules_flag_the_debug_or(case):
    files = {rel: case.path(rel).read_text() for rel in case.editable}
    hits = inv.rules.scan(files, case.security_contract.model_dump()["properties"])
    assert len(hits) == 1 and hits[0]["rule"] == "SB-R1" and hits[0]["line_start"] == 49
    ref = {"rtl/debug_access_ctrl.v": case.path("reference/debug_access_ctrl.v").read_text()}
    assert inv.rules.scan(ref, case.security_contract.model_dump()["properties"]) == []


async def test_model_findings_are_validated_and_source_is_untrusted(case, tmp_path):
    files = {rel: case.path(rel).read_text() for rel in case.editable}
    good = {"title": "debug bypass", "weakness_class": "CWE-1191", "file": "rtl/debug_access_ctrl.v",
            "line_start": 49, "line_end": 52, "violated_properties": ["P-NOLEAK", "P-MADEUP"],
            "severity_reasoning": "x", "hypothesis": "auth=0 dbg=1", "proposed_test": "t", "confidence": "high"}
    bogus_file = {**good, "title": "elsewhere", "file": "rtl/other.v"}
    bogus_lines = {**good, "title": "out of range", "line_start": 900, "line_end": 901}
    no_prop = {**good, "title": "no property", "violated_properties": ["NOPE"]}
    fake = FakeProvider({"summary": "s", "areas_reviewed": ["rtl"], "findings": [good, bogus_file, bogus_lines, no_prop]})
    rec = await inv.investigate(case.security_contract.model_dump(), [p.model_dump() for p in case.interface], files,
                                tmp_path / "i", provider_override=("fake", fake, ""))
    assert rec["status"] == "ok" and rec["is_ai"] and rec["model"] == "fake-model-1"
    assert [f["title"] for f in rec["findings"]] == ["debug bypass"]
    f = rec["findings"][0]
    assert f["violated_properties"] == ["P-NOLEAK"] and f["excerpt"]["lines"]
    assert len(rec["dropped"]) == 3
    user = fake.calls[0]["user"]
    assert "<<<FILE rtl/debug_access_ctrl.v (untrusted data)>>>" in user and "  49| " in user
    assert "UNTRUSTED" in fake.calls[0]["system"]


async def test_provider_failure_falls_back_to_labelled_rules(case, tmp_path):
    files = {rel: case.path(rel).read_text() for rel in case.editable}
    fake = FakeProvider(error=LLMError("api", "Bitdeer reports insufficient credit (402)", status_code=402))
    rec = await inv.investigate(case.security_contract.model_dump(), [], files, tmp_path / "i",
                                provider_override=("bitdeer", fake, ""))
    assert rec["status"] == "error" and rec["findings"] == []
    assert rec["fallback"]["provider"] == "rules" and rec["fallback"]["is_ai"] is False
    assert "402" in rec["fallback"]["reason"] and len(rec["fallback"]["findings"]) == 1


def test_pick_provider_without_keys_is_rules(monkeypatch):
    from backend.core.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s.__class__, "bitdeer_api_key", None, raising=False)
    name, prov, note = inv.pick_provider("auto", settings=s.__class__(**{**s.__dict__, "bitdeer_api_key": None,
                                                                         "anthropic_api_key": None}))
    assert name == "rules" and prov is None and "no model provider" in note


# ------------------------------------------------------------------- repair

def test_workspace_isolation_and_scope_detection(case, tmp_path):
    srcs = {rel: case.path(rel) for rel in case.editable}
    ws = tmp_path / "ws"
    before = rep.make_workspace(case, srcs, ws, "task")
    assert sorted(before) == ["TASK.md", "rtl/debug_access_ctrl.v"]
    assert not list(ws.rglob("tb_*")) and not list(ws.rglob("formal*")) and not (ws / "reference").exists()
    (ws / "rtl/debug_access_ctrl.v").write_text(case.path("reference/debug_access_ctrl.v").read_text())
    (ws / "TASK.md").write_text("changed")
    (ws / "tests").mkdir()
    (ws / "tests" / "tb.sv").write_text("module tb; endmodule")
    ch = rep.collect_changes(case, ws, before, srcs)
    assert ch["changed_files"] == ["rtl/debug_access_ctrl.v"] and "if (authorized) begin" in ch["diff"]
    assert any("TASK.md" in v for v in ch["scope_violations"]) and ch["extra_files"] == ["tests/tb.sv"]


def test_droid_permission_policy(tmp_path):
    from droid_sdk import (CreateFile, EditAction, ExecuteAction, PermissionOption, PermissionRequest,
                           ToolConfirmationOutcome)
    from droid_sdk._high_level.messages import ToolUseBlock

    ws = tmp_path / "ws"
    (ws / "rtl").mkdir(parents=True)
    rec: dict = {}
    handler = rep.make_permission_handler(ws, ["rtl/debug_access_ctrl.v"], rec)
    tu = ToolUseBlock(id="t1", name="Edit", input={})
    opts = [PermissionOption("once", ToolConfirmationOutcome.PROCEED_ONCE), PermissionOption("no", ToolConfirmationOutcome.CANCEL)]

    def ask(*actions):
        return handler(PermissionRequest(actions=actions, options=opts)).selected_option

    assert ask(EditAction(tu, str(ws / "rtl/debug_access_ctrl.v"), "debug_access_ctrl.v")) == ToolConfirmationOutcome.PROCEED_ONCE
    assert ask(EditAction(tu, "rtl/debug_access_ctrl.v", "debug_access_ctrl.v")) == ToolConfirmationOutcome.PROCEED_ONCE
    assert ask(EditAction(tu, str(ws / "../outside.v"), "outside.v")) == ToolConfirmationOutcome.CANCEL
    assert ask(CreateFile(tu, str(ws / "tests/tb.sv"), "tb.sv", "x")) == ToolConfirmationOutcome.CANCEL
    assert ask(ExecuteAction(tu, "ls", "ls")) == ToolConfirmationOutcome.CANCEL
    assert ask(EditAction(tu, str(ws / "rtl/debug_access_ctrl.v"), "d.v"), ExecuteAction(tu, "make", "make")) \
        == ToolConfirmationOutcome.CANCEL
    assert rec["denied_actions"] and rec["allowed_actions"]


def test_droid_unavailable_is_reported(monkeypatch):
    from backend.core.config import get_settings
    s = get_settings()
    st = rep.droid_status(s.__class__(**{**s.__dict__, "droid_bin": None}))
    assert st["state"] == "unavailable" and "droid" in st["reason"]
    st = rep.droid_status(s.__class__(**{**s.__dict__, "droid_bin": "/nonexistent/droid"}))
    assert st["state"] == "unavailable" and "not found" in st["reason"]


def test_auto_repair_uses_droid_only_when_configured(monkeypatch):
    from backend.security import orchestrator as orch

    from backend.core import budget

    so = orch.SecurityOrchestrator()
    assert so._choose_agent("factory")[0] is None                # live AI off: nothing paid runs, even on request
    monkeypatch.setattr(budget, "live_enabled", lambda: True)
    monkeypatch.setattr(orch.rep, "droid_status", lambda *a: {"state": "unverified", "reason": "no key", "sdk": "x", "cli": "/d"})
    monkeypatch.setattr(orch.inv, "pick_provider", lambda *a, **k: ("rules", None, "no model"))
    assert so._choose_agent("auto")[0] is None            # logged-in CLI alone: not used automatically
    assert so._choose_agent("factory")[0] == "factory"    # but runs when asked for by name
    monkeypatch.setattr(orch.rep, "droid_status", lambda *a: {"state": "configured", "reason": "", "sdk": "x", "cli": "/d"})
    assert so._choose_agent("auto")[0] == "factory"


# ------------------------------------------------------------------- bitdeer

async def test_bitdeer_adapter_falls_back_from_json_schema_and_strips_thinking():
    from backend.agent.llm.bitdeer_provider import BitdeerProvider
    from backend.security.investigate import InvestigationOut

    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        seen.append(body.get("response_format", {}).get("type"))
        assert req.headers["authorization"] == "Bearer k-secret"
        if body.get("response_format", {}).get("type") == "json_schema":
            return httpx.Response(400, json={"error": {"message": "response_format json_schema not supported"}})
        content = '<think>looking</think>```json\n{"summary": "ok", "areas_reviewed": [], "findings": []}\n```'
        return httpx.Response(200, json={"id": "r1", "model": "m", "choices": [{"finish_reason": "stop",
                                         "message": {"content": content}}], "usage": {"total_tokens": 5}})

    p = BitdeerProvider("k-secret", "m", transport=httpx.MockTransport(handler))
    res = await p.structured(purpose="t", system="s", messages=[{"role": "user", "content": "u"}], output_model=InvestigationOut)
    assert res.value.summary == "ok" and seen == ["json_schema", "json_object"] and res.provider == "bitdeer"


@pytest.mark.parametrize("status,kind", [(401, "auth"), (402, "api"), (429, "rate_limit"), (500, "api")])
async def test_bitdeer_errors_are_classified_and_scrubbed(status, kind):
    from backend.agent.llm.bitdeer_provider import BitdeerProvider
    from backend.security.investigate import InvestigationOut

    p = BitdeerProvider("k-secret", "m", transport=httpx.MockTransport(
        lambda r: httpx.Response(status, json={"error": {"message": "bad key k-secret"}})))
    with pytest.raises(LLMError) as ei:
        await p.structured(purpose="t", system="s", messages=[{"role": "user", "content": "u"}], output_model=InvestigationOut)
    assert ei.value.kind == kind and "k-secret" not in str(ei.value)


# ------------------------------------------------------------ audit pipeline

@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SILICONBREAK_RUNS_DIR", str(tmp_path / "runs"))
    for k in ("ANTHROPIC_API_KEY", "BITDEER_API_KEY", "FACTORY_API_KEY"):
        monkeypatch.setenv(k, "")
    monkeypatch.setenv("SILICONBREAK_DROID", "/nonexistent/droid")
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


async def _wait(c, audit_id, timeout=300):
    t0 = time.time()
    while time.time() - t0 < timeout:
        a = (await c.get(f"/api/security/audits/{audit_id}")).json()
        if a.get("finished_at"):
            return a
        await asyncio.sleep(0.25)
    raise AssertionError("audit did not finish")


@pytest.mark.verilator
@needs_yosys
async def test_audit_pipeline_without_keys_then_model_repair(client, monkeypatch):
    c = client
    r = (await c.post("/api/security/audits", json={"case_id": CASE})).json()
    aid, rid = r["audit"]["id"], r["run"]["id"]
    a = await _wait(c, aid)
    run = (await c.get(f"/api/runs/{rid}")).json()
    st = {s["key"]: s["status"] for s in run["stages"]}
    assert st == {"contract": "passed", "index": "passed", "investigate": "passed", "reproduce": "failed",
                  "formal": "failed", "repair": "skipped", "retest": "skipped", "evidence": "passed"}
    assert a["status"] == "reproduced" and run["status"] == "reproduced"
    rec = a["record"]
    assert rec["providers"]["investigator"]["provider"] == "rules" and rec["providers"]["investigator"]["is_ai"] is False
    [f] = rec["findings"]
    assert [h["status"] for h in f["history"]] == ["suspected", "reproduced"]
    assert rec["evidence"]["artifacts"] and (await c.get(f"/api/security/audits/{aid}/file", params={"path": "report.md"})).status_code == 200
    assert (await c.get(f"/api/security/audits/{aid}/file", params={"path": "../../x"})).status_code in (403, 404)
    events = (await c.get(f"/api/runs/{rid}/events")).json()
    assert any(e["type"] == "security.updated" for e in events) and any(e["type"] == "stage.updated" for e in events)

    # a model repair (fake provider standing in for Bitdeer/Claude) is judged by the trusted verifier
    from backend.security import orchestrator as orch

    case = get_case(CASE)
    fixed = case.path("reference/debug_access_ctrl.v").read_text()
    fake = FakeProvider({"rationale": "require authorization", "files": [{"path": "rtl/debug_access_ctrl.v", "content": fixed}]})
    monkeypatch.setattr(orch.SecurityOrchestrator, "_choose_agent", lambda self, agent: ("model", ("fake", fake), ""))
    r2 = (await c.post(f"/api/security/audits/{aid}/repair", json={"agent": "model"})).json()
    a = await _wait(c, aid)
    assert a["status"] == "repaired_verified", a["record"]["outcome"]
    f = a["record"]["findings"][0]
    assert [h["status"] for h in f["history"]] == ["suspected", "reproduced", "patch_proposed", "repaired_verified"]
    att = a["record"]["repairs"][-1]
    assert att["verdict"] == "verified" and att["formal"]["status"] == "proven" and "if (authorized) begin" in att["diff"]
    src = (await c.get(f"/api/security/audits/{aid}/sources", params={"attempt": att["attempt"]})).json()
    assert "if (authorized) begin" in src["patched"]["rtl/debug_access_ctrl.v"]

    # a model patch that breaks legitimate behaviour is rejected
    deny_all = case.path("rtl/debug_access_ctrl.v").read_text().replace("if (authorized || debug_mode) begin", "if (1'b0) begin")
    fake2 = FakeProvider({"rationale": "deny all", "files": [{"path": "rtl/debug_access_ctrl.v", "content": deny_all}]})
    monkeypatch.setattr(orch.SecurityOrchestrator, "_choose_agent", lambda self, agent: ("model", ("fake", fake2), ""))
    await c.post(f"/api/security/audits/{aid}/repair", json={"agent": "model"})
    a = await _wait(c, aid)
    assert a["status"] == "repair_failed"
    assert all(x["verdict"] == "failed" for x in a["record"]["repairs"][-3:] if x["agent"] == "model" and x.get("diff") and "1'b0" in x["diff"])


# ------------------------------------------------------- every case is valid

@pytest.mark.verilator
@needs_yosys
@pytest.mark.parametrize("case_id", [c.case_id for c in __import__("backend.security.cases", fromlist=["list_cases"]).list_cases()])
async def test_every_case_harness_is_valid(case_id, tmp_path):
    """A case counts only if its vulnerable source fails a security test (functional tests passing),
    its reference fix passes everything, and formal agrees (counterexample -> proof)."""
    c = get_case(case_id)
    rel = c.editable[0]
    vul = await verify_sources(c, {rel: c.path(rel)}, tmp_path / "v", label="vulnerable")
    assert vul.status == "failed" and vul.security_status == "failed" and vul.functional_status == "passed", vul.message
    ref_src = c.path(c.reference_fix.files[rel])
    ref = await verify_sources(c, {rel: ref_src}, tmp_path / "r", label="reference")
    assert ref.status == "passed", ref.message
    assert (await formal.check(c, [c.path(rel)], tmp_path / "fv", label="v")).status == "counterexample"
    assert (await formal.check(c, [ref_src], tmp_path / "fr", label="r")).status == "proven"
    assert set(c.ground_truth.properties) <= set(c.property_ids())


# ------------------------------------------------------- Basys 3 before/after

def test_demo_steps_and_stimulus(case):
    from backend.security import fpga

    steps = case.board.demo
    assert [s.id for s in steps] == ["D1", "D2", "D3", "D4"] and steps[0].expect_leds == "0x4000"
    text, plan = fpga.stimulus_for(steps)
    assert text.splitlines()[1].startswith("end ") and "btnU 1" in text
    assert all(sample > press for _, press, sample in plan)
    with pytest.raises(Exception):
        type(steps[0]).model_validate({"id": "X", "title": "t", "set": {"SW99": 1}, "expect_leds": "0x0"})


@pytest.mark.verilator
async def test_board_sim_shows_the_leak_only_on_the_vulnerable_board(case, tmp_path):
    from backend.security import fpga

    vul = await fpga.board_sim(case, {"rtl/debug_access_ctrl.v": case.path("rtl/debug_access_ctrl.v")}, tmp_path / "v")
    assert vul["status"] == "differs_from_contract" and vul["lint_ok"]
    d1 = vul["steps"][0]
    assert d1["simulated"] == "0x80A5" and d1["expected"] == "0x4000" and not d1["matches_contract"]
    assert all(s["matches_contract"] for s in vul["steps"][1:])
    ref = await fpga.board_sim(case, {"rtl/debug_access_ctrl.v": case.path("reference/debug_access_ctrl.v")}, tmp_path / "r")
    assert ref["status"] == "matches_contract"


def test_board_xdc_uses_trusted_pins():
    from backend.rtl.wrapper import board_xdc
    from backend.specifications.board import load_board

    x = board_xdc(load_board("basys3"))
    assert "PACKAGE_PIN T18 IOSTANDARD LVCMOS33 } [get_ports btnU]" in x and "[get_ports {led[15]}]" in x


# --------------------------------------------------------- repository import

def test_repo_url_ref_subdir_validation():
    from backend.security import repo_index as ri

    assert ri.parse_url("https://github.com/lowRISC/opentitan") == ("lowRISC", "opentitan")
    assert ri.parse_url("https://github.com/lowRISC/opentitan.git") == ("lowRISC", "opentitan")
    for bad in ("http://github.com/a/b", "https://gitlab.com/a/b", "file:///etc", "https://github.com/a/b/../c",
                "git@github.com:a/b.git", "https://github.com/../b", "https://github.com/a"):
        with pytest.raises(ri.RepoError):
            ri.parse_url(bad)
    assert ri.validate_subdir("hw/ip/rv_dm/rtl/") == "hw/ip/rv_dm/rtl"
    for bad in ("../x", "a/../b", "/abs/../../x", "a b", "a/./b"):
        with pytest.raises(ri.RepoError):
            ri.validate_subdir(bad)


async def test_repo_bad_ref_is_refused_before_network():
    from backend.security import repo_index as ri

    for bad in ("--upload-pack=x", "a..b", "x y"):
        with pytest.raises(ri.RepoError):
            await ri.resolve_ref("https://github.com/a/b.git", bad)


def test_repo_scoring_and_budgeted_selection():
    from backend.security import repo_index as ri

    s_dbg, kw = ri.score_text("hw/ip/dm/dmi_gate.sv", "assign allow = lc_hw_debug_en & !locked; logic [127:0] key_q;")
    s_plain, _ = ri.score_text("hw/ip/uart/fifo.sv", "assign full = (count == DEPTH);")
    assert s_dbg > 10 and s_plain == 0 and "debug" in kw
    idx = {"files": [{"path": "a.sv", "score": 9.0, "bytes": 1000, "lines": 20, "skipped": None},
                     {"path": "b.sv", "score": 0.0, "bytes": 10, "lines": 1, "skipped": None},
                     {"path": "c.sv", "score": 5.0, "bytes": 500_000, "lines": 9000, "skipped": None},
                     {"path": "d.sv", "score": 3.0, "bytes": 100, "lines": 5, "skipped": None}]}
    assert ri.select_for_audit(idx, None) == ["a.sv", "d.sv"]   # c too big for the budget, b irrelevant
    assert ri.select_for_audit(idx, ["d.sv", "../etc/passwd"]) == ["d.sv"]
    with pytest.raises(ri.RepoError):
        ri.select_for_audit(idx, ["nope.sv"])


@pytest.mark.network
async def test_import_opentitan_rv_dm_at_pinned_commit(tmp_path, monkeypatch):
    monkeypatch.setenv("SILICONBREAK_RUNS_DIR", str(tmp_path / "runs"))
    from backend.core import config
    config.get_settings.cache_clear()
    from backend.security import repo_index as ri

    sha = "478afb033bb2d3e62cb4c5dd097f578de461b2b5"
    idx = await ri.import_repo("https://github.com/lowRISC/opentitan", sha, "hw/ip/rv_dm/rtl")
    assert idx["sha"] == sha and idx["totals"]["files"] >= 5 and not idx["truncated"]
    assert all(f["path"].startswith("hw/ip/rv_dm/rtl/") for f in idx["files"])
    assert "rv_dm_dmi_gate.sv" in ri.select_for_audit(idx, None)[0]
    config.get_settings.cache_clear()


# ---------------------------------------------- project audit and FPGA build run

async def _deny_when_blocked(c, run_id, timeout=300):
    t0 = time.time()
    while time.time() - t0 < timeout:
        run = (await c.get(f"/api/runs/{run_id}")).json()
        if run.get("finished_at"):
            return run
        synth = next((x for x in run["stages"] if x["key"] == "synth"), None)
        if synth and synth["status"] == "blocked":
            for a in (await c.get("/api/approvals", params={"status": "pending"})).json():
                await c.post(f"/api/approvals/{a['id']}/deny")
        await asyncio.sleep(0.25)
    raise AssertionError("run did not finish")


@pytest.mark.verilator
async def test_fpga_build_simulates_then_reports_vivado_unavailable(client, monkeypatch):
    from backend.agent.orchestrator import orchestrator as design_orchestrator

    async def no_vivado(ctx):
        return False
    monkeypatch.setattr(design_orchestrator, "_ensure_vivado", no_vivado)
    c = client
    r = (await c.post("/api/security/audits", json={"case_id": CASE, "repair_agent": "reference"})).json()
    a = await _wait(c, r["audit"]["id"])
    f = (await c.get(f"/api/security/audits/{a['id']}/fpga")).json()
    assert [v["variant"] for v in f["variants"]] == ["vulnerable", "attempt-1"] and f["variants"][1]["kind"] == "reference"
    assert (await c.post(f"/api/security/audits/{a['id']}/fpga/vulnerable/program")).status_code == 400
    assert (await c.post(f"/api/security/audits/{a['id']}/fpga/attempt-7/build")).status_code == 400
    b = (await c.post(f"/api/security/audits/{a['id']}/fpga/vulnerable/build")).json()
    run = await _deny_when_blocked(c, b["run"]["id"])
    st = {s["key"]: s["status"] for s in run["stages"]}
    assert st["board_sim"] == "failed" and st["synth"] == "unavailable" and st["bitstream"] == "unavailable"
    state = (await c.get(f"/api/security/audits/{a['id']}/fpga")).json()["state"]["vulnerable"]
    assert state["board_sim"]["steps"][0]["simulated"] == "0x80A5" and not state.get("build")


@pytest.mark.verilator
async def test_project_audit_stays_suspected_without_trusted_tests(client):
    c = client
    res = (await c.post("/api/projects", json={"text": "Blink LEDs 0-7 alternately at 2 Hz.",
                                               "options": {"simulation_only": True}})).json()
    pid = res["project"]["id"]
    t0 = time.time()
    while not (await c.get(f"/api/runs/{res['run']['id']}")).json().get("finished_at"):
        assert time.time() - t0 < 240
        await asyncio.sleep(0.25)
    r = (await c.post("/api/security/audits", json={"project_id": pid})).json()
    a = await _wait(c, r["audit"]["id"])
    assert a["status"] in ("no_findings", "suspected_only") and a["source_kind"] == "project"
    run = (await c.get(f"/api/runs/{r['run']['id']}")).json()
    st = {s["key"]: s["status"] for s in run["stages"]}
    assert st["reproduce"] == st["formal"] == st["repair"] == "skipped" and st["evidence"] == "passed"
    assert all(f["status"] == "suspected" for f in a["record"]["findings"])
    src = (await c.get(f"/api/security/audits/{a['id']}/sources")).json()["original"]
    assert "rtl/cl_core.v" in src and "module cl_core" in src["rtl/cl_core.v"]
    assert (await c.post("/api/security/audits", json={"project_id": pid, "case_id": CASE})).status_code == 400


async def test_bitdeer_salvages_a_complete_answer_from_a_runaway_completion():
    """GLM-5.3 answered correctly, then repeated a sentence until max_tokens (seen live): the complete
    JSON object at the start is used; a truncated object is still an error, with usage kept."""
    from backend.agent.llm.bitdeer_provider import BitdeerProvider
    from backend.security.investigate import InvestigationOut

    good = json.dumps({"summary": "debug bypass {not a brace issue}", "areas_reviewed": ["rtl"], "findings": []})
    bodies = iter([good + " The audit is complete." * 200, '{"summary": "cut off mid-str'])

    def handler(req):
        return httpx.Response(200, json={"id": "r", "model": "m", "usage": {"prompt_tokens": 900, "completion_tokens": 8000},
                                         "choices": [{"finish_reason": "length", "message": {"content": next(bodies)}}]})
    p = BitdeerProvider("k", "m", transport=httpx.MockTransport(handler))
    res = await p.structured(purpose="t", system="s", messages=[{"role": "user", "content": "u"}], output_model=InvestigationOut)
    assert res.stop_reason == "length_salvaged" and res.value.summary.startswith("debug bypass")
    with pytest.raises(LLMError) as ei:
        await p.structured(purpose="t", system="s", messages=[{"role": "user", "content": "u"}], output_model=InvestigationOut)
    assert ei.value.kind == "invalid_output" and ei.value.details["usage"]["completion_tokens"] == 8000


async def test_raw_droid_permission_handler_is_strict(tmp_path):
    """Wire-level handler (droid-sdk 0.5.0 cannot parse ApplyPatch-via-Script requests from CLI 0.249)."""
    ws = tmp_path / "ws"
    (ws / "rtl").mkdir(parents=True)
    rec: dict = {}
    typed = rep.make_permission_handler(ws, ["rtl/debug_access_ctrl.v"], rec)
    h = rep.make_raw_permission_handler(ws, ["rtl/debug_access_ctrl.v"], rec, typed, log_raw=None)
    opts = [{"label": "Yes", "value": "proceed_once"}, {"label": "Always (new)", "value": "proceed_always_script_v2"},
            {"label": "No", "value": "cancel"}]

    def req(details, tool="Script"):
        return {"toolUses": [{"toolUse": {"type": "tool_use", "id": "t", "name": tool, "input": {}},
                              "confirmationType": details.get("type"), "details": details}], "options": opts}

    target = str(ws / "rtl/debug_access_ctrl.v")
    ok = req({"type": "apply_patch", "filePath": target, "fileName": "debug_access_ctrl.v", "patchContent": "...",
              "files": [{"filePath": target, "operation": "update"}]})
    assert (await h(ok))["selectedOption"] == "proceed_once"
    assert rec["sdk_parse_fallbacks"]   # the unknown option value made the SDK's strict parse fail
    for bad in (
        req({"type": "apply_patch", "filePath": str(tmp_path / "x.v"), "files": [{"filePath": str(tmp_path / "x.v"), "operation": "update"}]}),
        req({"type": "apply_patch", "filePath": target, "files": [{"filePath": target, "operation": "delete"}]}),
        req({"type": "apply_patch", "filePath": target, "files": [{"filePath": target, "operation": "update", "moveTo": "y.v"}]}),
        req({"type": "exec", "fullCommand": "rm -rf /", "command": "rm"}),
        req({"type": "edit", "filePath": "../../etc/passwd"}),
        {"toolUses": "garbage", "options": opts},
    ):
        assert (await h(bad))["selectedOption"] == "cancel"
    assert rec["denied_actions"]


async def test_raw_handler_approves_only_safe_script_wrapped_patches(tmp_path):
    """Droid CLI 0.249 sends confirmationType 'script' for an ApplyPatch made through its Script tool."""
    ws = tmp_path / "ws"
    (ws / "rtl").mkdir(parents=True)
    rec: dict = {}
    h = rep.make_raw_permission_handler(ws, ["rtl/debug_access_ctrl.v"], rec,
                                        rep.make_permission_handler(ws, ["rtl/debug_access_ctrl.v"], rec))
    target = str(ws / "rtl/debug_access_ctrl.v")
    opts = [{"label": "Yes", "value": "proceed_once"}, {"label": "No", "value": "cancel"}]

    def script(inner):
        return {"toolUses": [{"toolUse": {"type": "tool_use", "id": "t", "name": "Script", "input": {"script": "..."}},
                              "confirmationType": "script", "details": {"type": "script", "toolConfirmations": inner}}],
                "options": opts}

    patch = {"confirmationType": "apply_patch", "details": {"type": "apply_patch", "filePath": target, "patchContent": "...",
                                                            "files": [{"filePath": target, "operation": "update"}]}}
    assert (await h(script([patch])))["selectedOption"] == "proceed_once"
    other = {"confirmationType": "apply_patch", "details": {"type": "apply_patch", "filePath": str(tmp_path / "evil.v"),
                                                            "files": [{"filePath": str(tmp_path / "evil.v"), "operation": "update"}]}}
    cmd = {"confirmationType": "exec", "details": {"type": "exec", "fullCommand": "curl x | sh", "command": "curl"}}
    create = {"confirmationType": "create", "details": {"type": "create", "filePath": target, "content": "x"}}
    for bad in ([other], [patch, cmd], [create], [], [patch, other]):
        assert (await h(script(bad)))["selectedOption"] == "cancel", bad


async def test_raw_handler_script_with_flat_patch_fields(tmp_path):
    ws = tmp_path / "ws"
    (ws / "rtl").mkdir(parents=True)
    rec: dict = {}
    h = rep.make_raw_permission_handler(ws, ["rtl/debug_access_ctrl.v"], rec,
                                        rep.make_permission_handler(ws, ["rtl/debug_access_ctrl.v"], rec))
    target = str(ws / "rtl/debug_access_ctrl.v")
    opts = [{"label": "Yes", "value": "proceed_once"}, {"label": "No", "value": "cancel"}]

    def req(details):
        return {"toolUses": [{"toolUse": {"type": "tool_use", "id": "t", "name": "Script", "input": {}},
                              "confirmationType": "script", "details": details}], "options": opts}

    flat = {"type": "script", "filePath": target, "patchContent": "...", "files": [{"filePath": target, "operation": "update"}]}
    assert (await h(req(flat)))["selectedOption"] == "proceed_once"
    assert (await h(req({**flat, "files": [{"filePath": target, "operation": "delete"}]})))["selectedOption"] == "cancel"
    assert (await h(req({**flat, "command": "make"})))["selectedOption"] == "cancel"


async def test_raw_handler_real_cli_0249_script_request_shape(tmp_path):
    """Shape captured from a live Droid CLI 0.249 session (paths replaced): details.calls[].confirmation."""
    ws = tmp_path / "ws"
    (ws / "rtl").mkdir(parents=True)
    rec: dict = {}
    h = rep.make_raw_permission_handler(ws, ["rtl/debug_access_ctrl.v"], rec,
                                        rep.make_permission_handler(ws, ["rtl/debug_access_ctrl.v"], rec))
    target = str(ws / "rtl/debug_access_ctrl.v")
    patch = (f"*** Begin Patch\n*** Update File: {target}\n@@\n-  if (authorized || debug_mode) begin\n"
             "+  if (authorized) begin\n*** End Patch")

    def req(confirmation, tool_name="ApplyPatch"):
        return {"toolUses": [{"toolUse": {"type": "tool_use", "id": "call_x", "name": "Script",
                                          "input": {"script": "text(await tools.ApplyPatch(...))"}},
                              "confirmationType": "script",
                              "details": {"type": "script", "impactLevel": "low",
                                          "calls": [{"toolName": tool_name, "line": 2, "column": 11,
                                                     "toolInput": {"input": patch}, "confirmation": confirmation}]}}],
                "options": [{"label": "Yes", "value": "proceed_once"}, {"label": "No", "value": "cancel"}]}

    good = {"type": "apply_patch", "filePath": target, "fileName": "debug_access_ctrl.v", "patchContent": patch}
    assert (await h(req(good)))["selectedOption"] == "proceed_once"
    assert (await h(req({**good, "filePath": str(tmp_path / "other.v")})))["selectedOption"] == "cancel"
    assert (await h(req({"type": "exec", "fullCommand": "make", "command": "make"}, "Execute")))["selectedOption"] == "cancel"
