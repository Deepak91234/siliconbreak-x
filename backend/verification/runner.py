"""Simulation suite runner: lint (real + sim params) -> Verilator build -> scenarios -> oracle.

`run_simulation_suite` is the single entry point the orchestrator uses. It never
fabricates results: if Verilator is missing the report says "unavailable", if the
harness cannot be built or run for reasons outside the design the report says
"error", and only a completed oracle evaluation can produce "passed".
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from backend.core.config import get_settings
from backend.rtl.wrapper import library_paths
from backend.specifications.derive import derive_requirements
from backend.specifications.ir import DesignSpec
from backend.specifications.timing import param_dict

from . import verilator as V
from .oracle import CheckResult, evaluate_scenario
from .testgen import Scenario, TestPlan, TestPlanError, generate_test_plan

Emit = Callable[[str, dict], Awaitable[None]]

MAX_FAILING_IN_DIAGNOSTICS = 24
MAX_LINT_IN_DIAGNOSTICS = 40
SIM_DIR = "sim"


class SimulationReport(BaseModel):
    status: Literal["passed", "failed", "error", "unavailable"]
    lint: dict = Field(default_factory=dict)
    build: dict = Field(default_factory=dict)
    plan: TestPlan | None = None
    scenarios: list[dict] = Field(default_factory=list)
    requirements: list[dict] = Field(default_factory=list)
    passed_checks: int = 0
    failed_checks: int = 0
    diagnostics: dict = Field(default_factory=dict)
    duration_s: float = 0.0


def _tail(text: str, n: int = 4000) -> str:
    return text[-n:]


def _stimulus_text(sc: Scenario) -> str:
    lines = [f"# scenario {sc.id}: {sc.title}", f"end {sc.end_cycle}"]
    lines += [f"{e.cycle} {e.port} {e.value}" for e in sorted(sc.events, key=lambda e: e.cycle)]
    return "\n".join(lines) + "\n"


def _requirements(spec: DesignSpec, checks: list[CheckResult]) -> list[dict]:
    out = []
    for r in derive_requirements(spec):
        mine = [c for c in checks if r.id in c.requirement_ids]
        status = "not_tested" if not mine else ("passed" if all(c.passed for c in mine) else "failed")
        out.append({"id": r.id, "text": r.text, "methods": list(r.methods), "sim_status": status,
                    "checks": len(mine), "failed": sum(1 for c in mine if not c.passed)})
    return out


def _failing_digest(scenarios: dict[str, Scenario], checks: list[CheckResult]) -> list[dict]:
    out = []
    for c in checks:
        if c.passed:
            continue
        sc = scenarios.get(c.scenario_id)
        out.append({
            "scenario": c.scenario_id,
            "scenario_title": sc.title if sc else "",
            "scenario_description": sc.description if sc else "",
            "check": c.id,
            "requirement_ids": c.requirement_ids,
            "summary": f"[{c.scenario_id}/{c.id}] {c.summary}",
            "expected": c.expected,
            "observed": c.observed,
            "first_failure_cycle": c.first_failure_cycle,
            "trace_excerpt": c.details.get("trace_excerpt", []),
        })
        if len(out) >= MAX_FAILING_IN_DIAGNOSTICS:
            break
    return out


async def _say(emit: Emit | None, kind: str, data: dict) -> None:
    if emit is not None:
        await emit(kind, data)



def _cmd(argv: list[str], limit: int = 600) -> str:
    """The exact command line a tool ran (for the dashboard's stage details); long source lists are elided."""
    import shlex
    text = shlex.join(str(a) for a in argv)
    return text if len(text) <= limit else text[:limit] + " ..."

async def run_simulation_suite(
    spec: DesignSpec,
    rtl_files: list[Path],
    workspace: Path,
    emit: Emit | None = None,
    clk_hz: int | None = None,
    *,
    fail_fast: bool = False,
    waveforms: bool = True,
) -> SimulationReport:
    """Lint, build and simulate `rtl_files` (+ the SiliconBreak X library) against the spec's test plan.

    fail_fast stops after the first scenario with a failing check (used by mutation tests);
    waveforms=False skips VCD tracing (faster builds).
    """
    t0 = time.monotonic()
    workspace = Path(workspace).resolve()
    clk = int(clk_hz or get_settings().sim_clk_hz)

    def done(status: str, **kw) -> SimulationReport:
        return SimulationReport(status=status, duration_s=round(time.monotonic() - t0, 3), **kw)

    async def log(line: str) -> None:
        await _say(emit, "log", {"line": line})

    try:
        V.verilator_bin()
    except V.VerilatorUnavailable as e:
        await log(f"simulation unavailable: {e}")
        return done("unavailable", diagnostics={"error": str(e)})

    try:
        plan = generate_test_plan(spec, clk)
    except TestPlanError as e:
        await log(f"test plan error: {e}")
        return done("error", diagnostics={"error": f"test plan: {e}"})
    await log(f"test plan: {len(plan.scenarios)} scenarios, "
              f"{sum(len(s.checks) for s in plan.scenarios)} checks at {clk} Hz (from the spec)")

    sources = [Path(p).resolve() for p in rtl_files] + list(library_paths())
    missing = [str(p) for p in sources if not p.is_file()]
    if missing:
        await log(f"missing RTL sources: {missing}")
        return done("error", plan=plan, diagnostics={"error": f"missing RTL sources: {missing}"})
    sim_dir = workspace / SIM_DIR
    sim_dir.mkdir(parents=True, exist_ok=True)

    # ---- lint at the real board clock and at the simulation clock
    lint: dict[str, dict] = {}
    lint_errors: list[dict] = []
    for label, params in (("real", param_dict(spec, spec.clock_hz)), ("sim", plan.params)):
        try:
            res = await V.lint(sources, workspace, params)
        except (OSError, ValueError) as e:  # e.g. a source outside the workspace
            await log(f"lint could not run: {e}")
            return done("error", plan=plan, diagnostics={"error": f"lint could not run: {e}"})
        lint[label] = {"ok": res.ok, "messages": [m.as_dict() for m in res.messages], "params": label}
        await log(f"$ {_cmd(res.process.argv)}")
        await log(f"lint ({label} params): {'ok' if res.ok else 'FAILED'}, {len(res.messages)} messages")
        for m in res.messages:
            await log(f"  {m.severity} {m.code or ''} {Path(m.file).name if m.file else ''}:{m.line or ''} {m.message}")
        if not res.ok:
            errs = [dict(m.as_dict(), params=label) for m in res.errors]
            if not errs:
                errs = [{"severity": "error", "code": None, "file": None, "line": None, "column": None,
                         "message": _tail(res.process.text or res.process.error or "lint failed", 2000),
                         "fatal": True, "params": label}]
            lint_errors += errs
    if lint_errors:
        seen, uniq = set(), []
        for e in lint_errors:
            key = (e.get("code"), e.get("file"), e.get("line"), e.get("message"))
            if key not in seen:
                seen.add(key)
                uniq.append(e)
        return done("failed", lint=lint, plan=plan, requirements=_requirements(spec, []),
                    diagnostics={"lint_errors": uniq[:MAX_LINT_IN_DIAGNOSTICS], "failing_checks": [],
                                 "summary": f"Verilator lint failed with {len(uniq)} error(s)"})

    # ---- build the harness at the simulation parameters
    await log("building Verilator harness ...")
    tb = time.monotonic()
    build = await V.build_harness(sources, workspace, plan.params, sim_dir / "obj", trace=waveforms)
    build_info = {"ok": build.ok, "duration_s": round(time.monotonic() - tb, 2),
                  "log_tail": _tail(build.process.text, 3000)}
    await log(f"$ {_cmd(build.process.argv)}")
    await log(f"build {'ok' if build.ok else 'FAILED'} in {build_info['duration_s']} s")
    if not build.ok:
        design_errs = [m.as_dict() for m in build.messages if m.fatal]
        if design_errs:
            return done("failed", lint=lint, build=build_info, plan=plan, requirements=_requirements(spec, []),
                        diagnostics={"build_errors": design_errs[:MAX_LINT_IN_DIAGNOSTICS], "failing_checks": [],
                                     "lint_errors": [],
                                     "summary": "Verilator could not compile the design"})
        return done("error", lint=lint, build=build_info, plan=plan,
                    diagnostics={"error": "Verilator harness build failed (infrastructure)",
                                 "log_tail": build_info["log_tail"]})

    # ---- scenarios
    assert build.binary is not None
    by_id = {s.id: s for s in plan.scenarios}
    all_checks: list[CheckResult] = []
    scen_out: list[dict] = []
    infra_errors: list[dict] = []
    for sc in plan.scenarios:
        ts = time.monotonic()
        stim = sim_dir / f"{sc.id}.stim"
        stim.write_text(_stimulus_text(sc))
        trace_path = sim_dir / f"{sc.id}.trace"
        vcd = None
        if waveforms and sc.vcd_window is not None:
            vcd = (sim_dir / f"{sc.id}.vcd", int(sc.vcd_window[0]), int(sc.vcd_window[1]))
        run = await V.run_harness(build.binary, workspace, stim, trace_path, vcd=vcd)
        await log(f"$ {_cmd(run.process.argv)}")
        entry = {"id": sc.id, "title": sc.title, "description": sc.description,
                 "requirement_ids": sc.requirement_ids, "end_cycle": sc.end_cycle,
                 "trace_file": str(trace_path.relative_to(workspace)),
                 "vcd_file": str(run.vcd_path.relative_to(workspace)) if run.vcd_path and run.vcd_path.is_file() else None,
                 "vcd_window": list(sc.vcd_window) if sc.vcd_window else None}
        if not run.ok:
            msg = _tail(run.process.text or run.process.error or "harness failed", 1500)
            infra_errors.append({"scenario": sc.id, "error": msg, "timed_out": run.process.timed_out})
            entry.update(passed=False, checks=[], error=msg, duration_s=round(time.monotonic() - ts, 3))
            scen_out.append(entry)
            await log(f"scenario {sc.id}: harness run failed")
            await _say(emit, "scenario", {"id": sc.id, "title": sc.title, "passed": False, "error": True})
            continue
        trace = V.parse_trace(run.trace_path)
        results = evaluate_scenario(spec, plan, sc, trace)
        all_checks += results
        passed = all(r.passed for r in results)
        entry.update(passed=passed, checks=[r.model_dump() for r in results],
                     duration_s=round(time.monotonic() - ts, 3))
        scen_out.append(entry)
        nfail = sum(1 for r in results if not r.passed)
        await log(f"scenario {sc.id}: {'PASS' if passed else 'FAIL'} "
                  f"({len(results) - nfail}/{len(results)} checks)")
        for r in results:
            if not r.passed:
                await log(f"  FAIL {r.id}: {r.summary}")
        await _say(emit, "scenario", {"id": sc.id, "title": sc.title, "passed": passed,
                                      "checks": len(results), "failed": nfail})
        if fail_fast and not passed:
            break
        await asyncio.sleep(0)

    passed_n = sum(1 for c in all_checks if c.passed)
    failed_n = len(all_checks) - passed_n
    failing = _failing_digest(by_id, all_checks)
    diagnostics: dict = {"failing_checks": failing, "lint_errors": [], "clk_hz": clk, "params": plan.params}
    if failing:
        stim = {}
        for f in failing:
            sc = by_id[f["scenario"]]
            if sc.id not in stim:
                stim[sc.id] = {"description": sc.description, "end_cycle": sc.end_cycle,
                               "events": [[e.cycle, e.port, e.value] for e in sc.events][:40]}
        diagnostics["stimulus"] = stim
    if infra_errors:
        diagnostics["harness_errors"] = infra_errors
    if failed_n:
        status = "failed"
    elif infra_errors or not all_checks:
        status = "error"
    else:
        status = "passed"
    diagnostics["summary"] = (f"{passed_n} passed / {failed_n} failed checks in {len(scen_out)} scenarios"
                              + (f"; {len(infra_errors)} harness errors" if infra_errors else ""))
    await log(f"simulation {status}: {diagnostics['summary']}")
    return done(status, lint=lint, build=build_info, plan=plan, scenarios=scen_out,
                requirements=_requirements(spec, all_checks), passed_checks=passed_n, failed_checks=failed_n,
                diagnostics=diagnostics)
