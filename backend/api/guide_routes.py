"""Endpoints behind the hardware guide, board twin, virtual FPGA, block diagram and AI-provider panel.

Everything returned here is derived from trusted or measured data:
  * /api/board                     boards/basys3/board.json (labels, pins)
  * /api/revisions/{id}/guide      DesignSpec + simulation test plan/report (specifications/guide.py)
  * /api/revisions/{id}/scenarios/{sid}/trace   the stimulus and LED trace Verilator produced for that scenario
  * /api/revisions/{id}/simulate   runs the revision's compiled Verilator harness on user inputs (virtual FPGA)
  * /api/revisions/{id}/diagram    functional blocks from the spec + instance hierarchy parsed from the actual RTL
  * /api/examples/{name}/guide     guide for a bundled example spec (Preview mode; no simulation data)
  * /api/llm, /api/llm/reload      provider mode; the API key itself never leaves the backend
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.core.config import REPO_ROOT, get_settings
from backend.rtl.wrapper import LIBRARY_FILES, library_paths
from backend.specifications.derive import derive_requirements
from backend.specifications.guide import build_guide, board_definition, component_id
from backend.specifications.ir import (
    BlinkerBehavior,
    CounterBehavior,
    DesignSpec,
    LedPatternBehavior,
    TrafficLightBehavior,
)
from backend.storage import store
from backend.verification import verilator as V

router = APIRouter(prefix="/api")
EXAMPLES_DIR = REPO_ROOT / "examples"
_SIM_SLOTS = asyncio.Semaphore(2)
MAX_VIRTUAL_S = 120.0
MAX_VIRTUAL_EVENTS = 500
_SCENARIO_RE = re.compile(r"^[a-z0-9_]{1,64}$")
_EXAMPLE_RE = re.compile(r"^[a-z0-9_]{1,64}$")


def _rev(revision_id: str) -> dict:
    rev = store.get_revision(revision_id)
    if rev is None:
        raise HTTPException(404, "revision not found")
    return rev


def _report(rev: dict) -> dict | None:
    p = Path(rev["dir"]) / "sim" / "report.json"
    return json.loads(p.read_text()) if p.is_file() else None


# ------------------------------------------------------------------ board / guide

@router.get("/board")
async def get_board() -> dict:
    return board_definition()


@router.get("/revisions/{revision_id}/guide")
async def get_guide(revision_id: str) -> dict:
    rev = _rev(revision_id)
    spec = DesignSpec.model_validate(rev["spec"])
    report = _report(rev)
    guide = build_guide(spec, (report or {}).get("plan"), report)
    guide.update({"revision_id": revision_id, "revision_number": rev["number"], "preview": False,
                  "programmed_by_circuitloop": rev["status"] in ("programmed", "verified", "verify_failed"),
                  "sim_status": rev.get("sim_status"), "build_status": rev.get("build_status")})
    return guide


@router.get("/examples")
async def list_examples() -> list[dict]:
    out = []
    for p in sorted(EXAMPLES_DIR.glob("*.json")):
        try:
            spec = DesignSpec.model_validate(json.loads(p.read_text()))
        except (ValueError, OSError):
            continue
        out.append({"name": p.stem, "title": spec.title, "family": spec.family, "summary": spec.summary})
    return out


@router.get("/examples/{name}/guide")
async def example_guide(name: str) -> dict:
    if not _EXAMPLE_RE.match(name):
        raise HTTPException(404, "example not found")
    p = EXAMPLES_DIR / f"{name}.json"
    if not p.is_file():
        raise HTTPException(404, "example not found")
    guide = build_guide(DesignSpec.model_validate(json.loads(p.read_text())))
    guide.update({"preview": True, "example": name, "revision_id": None})
    return guide


# ------------------------------------------------------------------ simulation replay

def _parse_stim(path: Path) -> tuple[list[dict], int]:
    events, end = [], 0
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if parts[0] == "end":
            end = int(parts[1])
        elif len(parts) == 3:
            events.append({"cycle": int(parts[0]), "port": parts[1], "value": int(parts[2], 0)})
    return events, end


@router.get("/revisions/{revision_id}/scenarios/{scenario_id}/trace")
async def scenario_trace(revision_id: str, scenario_id: str) -> dict:
    if not _SCENARIO_RE.match(scenario_id):
        raise HTTPException(404, "scenario not found")
    rev = _rev(revision_id)
    report = _report(rev)
    if report is None:
        raise HTTPException(404, "no simulation report for this revision")
    sc = next((s for s in report.get("scenarios", []) if s.get("id") == scenario_id), None)
    sim = Path(rev["dir"]) / "sim"
    stim, trace = sim / f"{scenario_id}.stim", sim / f"{scenario_id}.trace"
    if sc is None or not stim.is_file() or not trace.is_file():
        raise HTTPException(404, "scenario trace not found")
    clk = int((report.get("plan") or {}).get("clk_hz") or get_settings().sim_clk_hz)
    events, end = _parse_stim(stim)
    tr = V.parse_trace(trace)
    return {
        "source": "verilator", "revision_id": revision_id, "scenario_id": scenario_id,
        "title": sc.get("title"), "description": sc.get("description"), "passed": sc.get("passed"),
        "requirement_ids": sc.get("requirement_ids", []), "clk_hz": clk, "end_cycle": end or tr.end_cycle,
        "duration_s": (end or tr.end_cycle) / clk,
        "inputs": [{**e, "t_s": e["cycle"] / clk} for e in events],
        "outputs": {sig: [[c, v, c / clk] for c, v in changes] for sig, changes in tr.changes.items()},
    }


# ------------------------------------------------------------------ virtual FPGA

class VirtualEvent(BaseModel):
    t_s: float = Field(ge=0.0, le=MAX_VIRTUAL_S)
    port: Literal["sw", "btnC", "btnU", "btnL", "btnR", "btnD"]
    value: int = Field(ge=0, le=0xFFFF)


class VirtualRequest(BaseModel):
    events: list[VirtualEvent] = Field(default_factory=list, max_length=MAX_VIRTUAL_EVENTS)
    duration_s: float = Field(gt=0.0, le=MAX_VIRTUAL_S)


async def _ensure_harness(rev: dict, report: dict) -> Path:
    d = Path(rev["dir"])
    binary = d / "sim" / "obj" / "Vsim"
    if binary.is_file():
        return binary
    params = (report.get("plan") or {}).get("params")
    if not params:
        raise HTTPException(409, "simulation parameters unknown for this revision")
    res = await V.build_harness([d / "rtl" / "basys3_top.v", d / "rtl" / "cl_core.v"] + library_paths(),
                                d, {k: int(v) for k, v in params.items()}, d / "sim" / "obj", trace=False)
    if not res.ok or res.binary is None:
        raise HTTPException(500, "could not rebuild the Verilator harness for this revision")
    return res.binary


@router.post("/revisions/{revision_id}/simulate")
async def simulate_virtual(revision_id: str, req: VirtualRequest) -> dict:
    """Run the revision's real RTL (Verilator harness, accelerated clock) on user inputs."""
    rev = _rev(revision_id)
    report = _report(rev)
    if report is None or rev.get("sim_status") not in ("passed", "failed"):
        raise HTTPException(409, "this revision has not been simulated")
    for e in req.events:
        if e.port != "sw" and e.value not in (0, 1):
            raise HTTPException(422, f"{e.port} takes 0 or 1")
        if e.t_s > req.duration_s:
            raise HTTPException(422, "event after the end of the simulation")
    clk = int((report.get("plan") or {}).get("clk_hz") or get_settings().sim_clk_hz)
    d = Path(rev["dir"])
    vdir = d / "sim" / "virtual"
    vdir.mkdir(parents=True, exist_ok=True)
    tag = secrets.token_hex(6)
    stim, trace = vdir / f"{tag}.stim", vdir / f"{tag}.trace"
    end = max(1, round(req.duration_s * clk))
    lines = [f"end {end}"] + [f"{max(1, round(e.t_s * clk))} {e.port} {e.value}"
                              for e in sorted(req.events, key=lambda e: e.t_s)]
    stim.write_text("\n".join(lines) + "\n")
    async with _SIM_SLOTS:
        binary = await _ensure_harness(rev, report)
        run = await V.run_harness(binary, d, stim, trace, vcd=None, timeout_s=30)
    try:
        if not run.ok:
            raise HTTPException(500, "virtual simulation failed: " + (run.process.text or run.process.error or "")[-300:])
        tr = V.parse_trace(trace)
    finally:
        for p in (stim, trace):
            try:
                os.unlink(p)
            except OSError:
                pass
    return {
        "source": "verilator", "revision_id": revision_id, "revision_number": rev["number"],
        "clk_hz": clk, "duration_s": req.duration_s, "end_cycle": end,
        "note": "Verilator simulation of this revision's RTL with the accelerated test clock; timing is scaled "
                "so simulated seconds equal real seconds. Not a hardware measurement.",
        "outputs": {sig: [[round(c / clk, 6), v] for c, v in changes] for sig, changes in tr.changes.items()},
    }


# ------------------------------------------------------------------ block diagram

_INST_RE = re.compile(r"^\s*(cl_[a-z0-9_]+)\s*(?:#\s*\((?:[^;]*?)\))?\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(", re.M | re.S)


def _instances(text: str) -> list[tuple[str, str]]:
    lib = {Path(f).stem for f in LIBRARY_FILES} | {"cl_core"}
    return [(m.group(1), m.group(2)) for m in _INST_RE.finditer(text) if m.group(1) in lib]


@router.get("/revisions/{revision_id}/diagram")
async def get_diagram(revision_id: str) -> dict:
    rev = _rev(revision_id)
    spec = DesignSpec.model_validate(rev["spec"])
    rtl = Path(rev["dir"]) / "rtl"
    wrapper = (rtl / "basys3_top.v").read_text() if (rtl / "basys3_top.v").is_file() else ""
    core = (rtl / "cl_core.v").read_text() if (rtl / "cl_core.v").is_file() else ""
    nodes: list[dict] = [{"id": "clk", "column": 0, "kind": "clock", "label": "CLK 100 MHz", "sub": "pin W5", "source": "board"}]
    edges: list[dict] = []
    wrapper_insts = {inst for _mod, inst in _instances(wrapper)}
    for i in spec.inputs:
        cid = component_id(i.port, i.index)
        nodes.append({"id": f"in:{i.name}", "column": 0, "kind": "input", "label": i.board_signal.upper() if i.port != "sw" else f"SW{i.index}",
                      "sub": i.name, "component": cid, "source": "spec"})
        inst = f"u_in_{i.name}"
        cond = {"id": f"cond:{i.name}", "column": 1, "kind": "rtl", "label": "Sync + debounce" if i.debounce_ms > 0 else "Synchronizer",
                "sub": f"cl_input_cond {inst}" + (f" ({i.debounce_ms:g} ms)" if i.debounce_ms > 0 else ""),
                "source": "rtl" if inst in wrapper_insts else "spec"}
        nodes.append(cond)
        edges += [{"from": f"in:{i.name}", "to": cond["id"], "label": i.board_signal},
                  {"from": "clk", "to": cond["id"], "clock": True}]
    # inside cl_core: real library instances parsed from the generated RTL ...
    core_insts = _instances(core)
    for mod, inst in core_insts:
        label = {"cl_tick_div": "Clock-enable divider", "cl_edge_rise": "Edge detector", "cl_sync2": "Synchronizer",
                 "cl_debounce": "Debouncer", "cl_input_cond": "Input conditioner"}.get(mod, mod)
        nodes.append({"id": f"core:{inst}", "column": 2, "kind": "rtl", "label": label, "sub": f"{mod} {inst}", "source": "rtl"})
        edges.append({"from": "clk", "to": f"core:{inst}", "clock": True})
    # ... plus functional blocks named from the validated spec (not netlist cells).
    b = spec.behavior
    func: list[tuple[str, str]] = []
    if isinstance(b, CounterBehavior):
        if len(b.rate_modes) > 1:
            func.append(("rate", f"Rate select ({', '.join(m.name for m in b.rate_modes)})"))
        func.append(("state", f"{spec.output(b.output).width}-bit {'up' if b.direction == 'up' else 'down'} counter"
                     + (" with reset" if b.reset_input else "")))
    elif isinstance(b, BlinkerBehavior):
        func += ([("rate", "Rate select")] if len(b.rate_modes) > 1 else []) + [("state", f"Blink toggle ({b.pattern})")]
    elif isinstance(b, LedPatternBehavior):
        if b.rate_modes and len(b.rate_modes) > 1:
            func.append(("rate", "Rate select"))
        func.append(("state", {"mirror_inputs": "Input mirror", "invert_inputs": "Input inverter"}.get(b.pattern, f"{b.pattern.title()} pattern register")))
    elif isinstance(b, TrafficLightBehavior):
        func.append(("state", f"FSM ({len(b.states) + (1 if b.request_state else 0)} states)"))
        if b.request_input:
            func.append(("latch", "Request latch"))
    for key, label in func:
        nodes.append({"id": f"fn:{key}", "column": 2, "kind": "functional", "label": label, "sub": "functional (from spec)", "source": "spec"})
    state = "fn:state"
    for i in spec.inputs:
        target = "fn:rate" if any(getattr(m, "when_input", None) == i.name for m in getattr(b, "rate_modes", []) or []) \
            and any(n["id"] == "fn:rate" for n in nodes) else ("fn:latch" if getattr(b, "request_input", None) == i.name
                                                               and any(n["id"] == "fn:latch" for n in nodes) else state)
        edges.append({"from": f"cond:{i.name}", "to": target, "label": i.name})
    tick = next((f"core:{inst}" for mod, inst in core_insts if mod == "cl_tick_div"), None)
    if tick:
        if any(n["id"] == "fn:rate" for n in nodes):
            edges.append({"from": "fn:rate", "to": tick, "label": "period"})
        edges.append({"from": tick, "to": state, "label": "tick"})
    if any(n["id"] == "fn:latch" for n in nodes):
        edges.append({"from": "fn:latch", "to": state, "label": "request"})
    edges.append({"from": "clk", "to": state, "clock": True})
    for o in spec.outputs:
        idx = sorted(o.indices)
        if o.port == "display":
            label = "7-segment " + "/".join(f"AN{i}" for i in reversed(idx))
            comps = ["seg"]
        else:
            label = f"LD{idx[0]}–LD{idx[-1]}" if idx == list(range(idx[0], idx[-1] + 1)) and len(idx) > 1 else ", ".join(f"LD{i}" for i in idx)
            comps = [component_id("led", i) for i in o.indices]
        nodes.append({"id": f"out:{o.name}", "column": 3, "kind": "output", "label": label, "sub": f"{o.name}[{o.width - 1}:0]" if o.width > 1 else o.name,
                      "components": comps, "source": "spec"})
        edges.append({"from": state, "to": f"out:{o.name}", "label": o.name})
    return {
        "revision_id": revision_id, "family": spec.family,
        "label": "Functional block diagram: blocks come from the validated spec and the instance hierarchy parsed "
                 "from this revision's RTL. It is not a synthesized gate-level netlist.",
        "explanation": _explain(spec),
        "nodes": nodes, "edges": edges,
        "requirements": [{"id": r.id, "text": r.text} for r in derive_requirements(spec)],
    }


def _explain(spec: DesignSpec) -> str:
    b = spec.behavior
    ins = ", ".join(f"{i.board_signal.upper() if i.port != 'sw' else 'SW' + str(i.index)} ({i.name})" for i in spec.inputs) or "no inputs"
    base = (f"The 100 MHz board clock drives every register. Inputs ({ins}) pass through a two-flop synchronizer "
            "and a debouncer in the trusted board wrapper before the behaviour core sees them. ")
    if isinstance(b, CounterBehavior):
        modes = ", ".join(f"{m.name} = {m.hz:g} steps/s" for m in b.rate_modes)
        return base + (f"A clock-enable divider produces one tick per step ({modes}); each tick moves the counter "
                       f"{'up' if b.direction == 'up' else 'down'} by one, and the value drives the LEDs directly.")
    if isinstance(b, BlinkerBehavior):
        return base + "A clock-enable divider toggles the LED pattern at twice the blink frequency (50% duty)."
    if isinstance(b, LedPatternBehavior):
        return base + ("The LEDs follow the conditioned switch levels combinationally." if b.pattern in ("mirror_inputs", "invert_inputs")
                       else f"A clock-enable divider advances the {b.pattern} pattern register one step per tick.")
    if isinstance(b, TrafficLightBehavior):
        return base + "A cycle timer counts each state's duration; when it expires the FSM moves to the next state and drives that state's light pattern."
    return base


# ------------------------------------------------------------------ AI provider

def _llm_status() -> dict:
    s = get_settings()
    env_file = REPO_ROOT / ".env"
    source = None
    if os.environ.get("ANTHROPIC_API_KEY"):
        source = "environment"
    elif s.anthropic_api_key:
        source = ".env file"
    from backend.core import budget

    live = budget.live_enabled()
    return {
        "provider": s.llm_provider, "model": s.llm_model, "configured": s.llm_configured and live,
        "live_ai": live,
        "mode": "model" if s.llm_configured and live else "offline-rules",
        "key_present": bool(s.anthropic_api_key), "key_source": source,
        "env_file": str(env_file), "env_file_exists": env_file.is_file(),
        "explanation": ("Requests are interpreted and Verilog is generated and repaired by the configured model."
                        if s.llm_configured and live else
                        "An API key is configured but live AI calls are off (credit protection); turn them on in the "
                        "Security tab's AI credits panel." if s.llm_configured else
                        "No API key: a deterministic rule-based parser handles the supported phrasings and "
                        "preverified templates generate the Verilog. Automatic repair is unavailable."),
    }


@router.get("/llm")
async def llm_status() -> dict:
    return _llm_status()


@router.post("/llm/reload")
async def llm_reload() -> dict:
    """Re-read .env / environment so a newly added key takes effect without restarting the backend."""
    from backend.api import environment

    get_settings.cache_clear()
    await environment.collect(force=True)
    return _llm_status()
