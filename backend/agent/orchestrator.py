"""The SiliconBreak X autonomous engineering loop.

One Run = one user request (new design, modification, rebuild or reprogram)
driven through the fixed stage list in backend/core/events.STAGES:

  spec -> validate -> rtl -> lint -> tests -> simulate -> repair (<= N attempts)
       -> synth -> impl -> timing -> bitstream -> detect -> program -> verify

Every stage status published here is the direct result of a real tool call
(LLM, Verilator, Vivado, openFPGALoader / hw_server, OpenCV). Stages that cannot
run because a tool or the board is missing are marked 'unavailable' (never
'passed'); hardware-changing steps wait for an explicit user approval.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.agent import approvals
from backend.core.config import get_settings
from backend.core.events import STAGE_KEYS, Event, bus
from backend.rtl.wrapper import generate_wrapper, generate_xdc, library_paths
from backend.specifications.derive import derive_observations, derive_requirements
from backend.specifications.ir import DesignSpec
from backend.storage import store
from backend.storage.db import now

log = logging.getLogger("siliconbreak.orchestrator")

VIVADO_STAGE_MAP = {"read": "synth", "synth": "synth", "opt": "impl", "place": "impl", "route": "impl",
                    "reports": "timing", "bitstream": "bitstream"}
VIVADO_STAGE_FIRST = {"read": "synth", "opt": "impl", "reports": "timing", "bitstream": "bitstream"}
VIVADO_STAGE_LAST = {"synth": "synth", "route": "impl"}


class RunCancelled(Exception):
    pass


@dataclass
class RunContext:
    run_id: str
    project_id: str
    options: dict
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    current_vivado_stage: str = "synth"

    # ------------------------------------------------------------ publishing
    async def stage(self, key: str, status: str, summary: str | None = None, detail: dict | None = None) -> None:
        run = store.get_run(self.run_id)
        stages = run["stages"]
        for s in stages:
            if s["key"] == key:
                if status == "running" and s["started_at"] is None:
                    s["started_at"] = now()
                if status in ("passed", "failed", "skipped", "unavailable", "inconclusive", "cancelled"):
                    s["finished_at"] = now()
                    if s["started_at"] is None:
                        s["started_at"] = s["finished_at"]
                s["status"] = status
                if summary is not None:
                    s["summary"] = summary
                if detail is not None:
                    s["detail"] = detail
                store.update_run(self.run_id, stages=stages)
                # envelope `stage` is the key; data["stage"] is the full Stage object (docs/api.md)
                await bus.publish(Event(type="stage.updated", run_id=self.run_id, project_id=self.project_id,
                                        stage=key, data={"stage": s}))
                return

    async def set_status(self, status: str, error: str | None = None, finished: bool = False) -> None:
        fields: dict[str, Any] = {"status": status}
        if error is not None:
            fields["error"] = error
        if finished:
            fields["finished_at"] = now()
        run = store.update_run(self.run_id, **fields)
        await bus.emit("run.finished" if finished else "run.updated", run_id=self.run_id,
                       project_id=self.project_id, run=run)

    async def log(self, stage: str, source: str, line: str) -> None:
        await bus.emit("log", run_id=self.run_id, project_id=self.project_id, stage=stage, source=source, line=line)

    async def say(self, text: str, kind: str = "text", data: dict | None = None) -> None:
        msg = store.add_message(self.project_id, "agent", text, run_id=self.run_id, kind=kind, data=data)
        await bus.emit("agent.message", run_id=self.run_id, project_id=self.project_id, message=msg)

    def check_cancel(self) -> None:
        if self.cancel.is_set():
            raise RunCancelled()

    async def skip_rest(self, from_key: str, status: str, summary: str) -> None:
        idx = STAGE_KEYS.index(from_key)
        run = store.get_run(self.run_id)
        for s in run["stages"]:
            if STAGE_KEYS.index(s["key"]) >= idx and s["status"] in ("pending", "running", "blocked"):
                await self.stage(s["key"], status, summary)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _spec_brief(spec: DesignSpec) -> str:
    b = spec.behavior
    ins = ", ".join(f"{i.name}={i.board_signal}" for i in spec.inputs) or "none"
    outs = ", ".join(f"{o.name}=" + (f"display AN{'/'.join(map(str, sorted(o.indices, reverse=True)))}" if o.port == "display"
                                       else f"LED{'/'.join(map(str, o.indices))}") for o in spec.outputs)
    extra = ""
    modes = getattr(b, "rate_modes", None)
    if modes:
        extra = "; rates: " + ", ".join(
            f"{m.name} {m.hz:g}/s" + (f" when {m.when_input}={m.when_level}" if m.when_input else "") for m in modes)
    return f"{spec.title} [{spec.family}] inputs: {ins}; outputs: {outs}{extra}"


class Orchestrator:
    def __init__(self) -> None:
        self.tasks: dict[str, asyncio.Task] = {}
        self.contexts: dict[str, RunContext] = {}

    # ================================================================ entry points
    async def new_project(self, text: str, options: dict) -> tuple[dict, dict]:
        title = text.strip().splitlines()[0][:80] if text.strip() else "New design"
        project = store.create_project(title)
        run = await self._start(project["id"], "new", text, options, base_revision=None)
        return store.get_project(project["id"]), run

    async def follow_up(self, project_id: str, text: str, options: dict) -> tuple[dict, dict]:
        project = store.get_project(project_id)
        if project is None:
            raise KeyError(project_id)
        base = self._base_revision(project_id)
        await self._finalize_waiting(project_id)
        run = await self._start(project_id, "modify" if base else "new", text, options, base_revision=base)
        msg = store.list_messages(project_id)[-1]
        return run, msg

    async def rebuild(self, revision_id: str) -> dict:
        rev = store.get_revision(revision_id)
        if rev is None:
            raise KeyError(revision_id)
        if rev.get("sim_status") != "passed":
            raise ValueError("only revisions that passed simulation can be rebuilt")
        await self._finalize_waiting(rev["project_id"])
        return await self._start(rev["project_id"], "rebuild", f"Rebuild revision {rev['number']}", {}, base_revision=rev)

    async def reprogram(self, revision_id: str) -> dict:
        rev = store.get_revision(revision_id)
        if rev is None:
            raise KeyError(revision_id)
        build = rev.get("build") or {}
        if build.get("status") != "passed" or not build.get("bitstream_path"):
            raise ValueError("this revision has no validated bitstream")
        await self._finalize_waiting(rev["project_id"])
        return await self._start(rev["project_id"], "reprogram", f"Program revision {rev['number']}", {}, base_revision=rev)

    async def cancel(self, run_id: str) -> dict:
        ctx = self.contexts.get(run_id)
        run = store.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        if ctx is not None:
            ctx.cancel.set()
        elif run["status"] in ("running", "waiting"):
            store.update_run(run_id, status="cancelled", finished_at=now())
        return store.get_run(run_id)

    # ================================================================ internals
    def _base_revision(self, project_id: str) -> dict | None:
        project = store.get_project(project_id)
        if project and project.get("current_revision_id"):
            return store.get_revision(project["current_revision_id"])
        revs = store.list_revisions(project_id)
        return revs[-1] if revs else None

    async def _finalize_waiting(self, project_id: str) -> None:
        """A run waiting only for physical verification is closed when the user moves on."""
        for run in store.list_runs(project_id):
            if run["status"] == "waiting" and run["id"] not in self.tasks:
                ctx = RunContext(run["id"], project_id, run.get("options") or {})
                await ctx.stage("verify", "skipped", "superseded by a newer request before camera verification")
                await ctx.set_status("passed_sim_only", finished=True)

    async def _start(self, project_id: str, trigger: str, text: str, options: dict, base_revision: dict | None) -> dict:
        opts = {"inject_fault": options.get("inject_fault"), "simulation_only": bool(options.get("simulation_only"))}
        run = store.create_run(project_id, trigger=trigger, request_text=text, options=opts)
        if trigger in ("new", "modify"):
            msg = store.add_message(project_id, "user", text, run_id=run["id"])
            await bus.emit("agent.message", run_id=run["id"], project_id=project_id, message=msg)
        await bus.emit("run.created", run_id=run["id"], project_id=project_id, run=run)
        ctx = RunContext(run["id"], project_id, opts)
        self.contexts[run["id"]] = ctx
        coro = {
            "new": self._design_run, "modify": self._design_run,
            "rebuild": self._rebuild_run, "reprogram": self._reprogram_run,
        }[trigger](ctx, text, base_revision)
        task = asyncio.create_task(self._guard(ctx, coro))
        self.tasks[run["id"]] = task
        return run

    async def _guard(self, ctx: RunContext, coro) -> None:
        try:
            await coro
        except RunCancelled:
            await ctx.skip_rest("spec", "cancelled", "cancelled by user")
            await ctx.set_status("cancelled", finished=True)
            await ctx.say("Run cancelled.", kind="error")
        except Exception as e:  # never leave a run hanging in 'running'
            log.exception("run %s crashed", ctx.run_id)
            await ctx.skip_rest("spec", "failed", f"internal error: {type(e).__name__}")
            await ctx.set_status("failed", error=f"{type(e).__name__}: {e}", finished=True)
            await ctx.say(f"Internal error: {type(e).__name__}: {e}", kind="error",
                          data={"traceback": traceback.format_exc()[-4000:]})
        finally:
            self.tasks.pop(ctx.run_id, None)
            self.contexts.pop(ctx.run_id, None)

    # ================================================================ design run
    async def _design_run(self, ctx: RunContext, text: str, base: dict | None) -> None:
        from backend.agent.intent import interpret_request
        from backend.agent.llm import get_provider

        provider = get_provider()
        base_spec = DesignSpec.model_validate(base["spec"]) if base else None

        # ---- spec
        await ctx.stage("spec", "running", "interpreting request" + (" with " + provider.model if provider else " (offline rules)"))
        intent = await interpret_request(text, base_spec, provider)
        ctx.check_cancel()
        if intent.kind == "question":
            await ctx.say(intent.message)
            await ctx.skip_rest("spec", "skipped", "question answered; no design change")
            await ctx.set_status("passed", finished=True)
            return
        if intent.kind in ("unsupported", "clarify") or intent.spec is None:
            reasons = "\n".join(f"- {r}" for r in intent.unsupported_reasons)
            await ctx.say(intent.message + (f"\n{reasons}" if reasons else ""), kind="error")
            await ctx.stage("spec", "failed", "unsupported request" if intent.kind == "unsupported" else "needs clarification",
                            {"interpreter": intent.interpreter, "reasons": intent.unsupported_reasons})
            await ctx.skip_rest("validate", "skipped", "no specification")
            await ctx.set_status("unsupported" if intent.kind == "unsupported" else "failed", finished=True)
            return
        spec = intent.spec
        await ctx.stage("spec", "passed", _spec_brief(spec)[:300],
                        {"interpreter": intent.interpreter, "assumptions": intent.assumptions,
                         "change_summary": intent.change_summary, "spec": spec.model_dump()})
        lines = [intent.message] if intent.message else []
        if intent.change_summary:
            lines.append("Changes: " + "; ".join(intent.change_summary))
        if intent.assumptions:
            lines.append("Assumptions: " + "; ".join(intent.assumptions))
        lines.append(f"Specification ({intent.interpreter}): {_spec_brief(spec)}")
        await ctx.say("\n".join(lines), kind="spec", data={"spec": spec.model_dump()})

        # ---- validate
        from backend.specifications.validate import validate_spec

        await ctx.stage("validate", "running", "checking Basys 3 capabilities")
        report = validate_spec(spec)
        issues = [i.model_dump() for i in report.issues]
        errs = [i for i in report.issues if i.severity == "error"]
        warns = [i for i in report.issues if i.severity == "warning"]
        if not report.ok or errs:
            await ctx.stage("validate", "failed", f"{len(errs)} errors", {"issues": issues})
            await ctx.say("The specification cannot be implemented on the Basys 3 as stated:\n" +
                          "\n".join(f"- {e.message}" for e in errs), kind="error")
            await ctx.skip_rest("rtl", "skipped", "specification invalid")
            await ctx.set_status("failed", error="specification failed capability validation", finished=True)
            return
        await ctx.stage("validate", "passed", f"{len(warns)} warnings" if warns else "all checks passed", {"issues": issues})
        if warns:
            await ctx.say("Validation warnings:\n" + "\n".join(f"- {w.message}" for w in warns))

        # ---- revision
        rev = store.create_revision(
            ctx.project_id, kind="modification" if base else "initial", parent_id=base["id"] if base else None,
            user_request=text, spec=spec.model_dump(), interpreter=intent.interpreter,
            change_summary=intent.change_summary)
        store.update_revision(rev["id"], requirements=[r.model_dump() for r in derive_requirements(spec)],
                              observations=[o.model_dump() for o in derive_observations(spec)])
        if not base:
            store.touch_project(ctx.project_id, title=spec.title)
        await self._add_revision_to_run(ctx, rev["id"])

        # ---- rtl
        from backend.agent.rtlgen import generate_core_rtl

        await ctx.stage("rtl", "running", "generating cl_core" + (f" with {provider.model}" if provider else " from preverified template"))
        prev_verilog = self._read_core(base) if base else None
        gen = await generate_core_rtl(spec, provider, previous_verilog=prev_verilog,
                                      change_request=text if base else None)
        ctx.check_cancel()
        core, wrapper = gen.verilog, generate_wrapper(spec)
        generator = gen.generator
        fault = ctx.options.get("inject_fault")
        if fault:
            core, wrapper, generator = await self._inject_fault(ctx, fault, core, wrapper, generator)
        rev = self._write_rtl(rev, spec, core, wrapper, generator, gen.explanation, gen.safety)
        safety_errors = [s for s in gen.safety if s.get("severity") == "error"]
        await ctx.stage("rtl", "passed" if not safety_errors else "failed",
                        f"{generator}; {len(core.splitlines())} lines" + (f"; {len(safety_errors)} safety errors" if safety_errors else ""),
                        {"generator": generator, "explanation": gen.explanation, "safety": gen.safety})
        await ctx.say(f"Generated Verilog ({generator}). {gen.explanation}".strip())

        # ---- verify + bounded repair
        rev = await self._verify_with_repair(ctx, spec, rev, provider)
        if rev is None:
            return
        await self._hardware_flow(ctx, rev)

    async def _inject_fault(self, ctx: RunContext, name: str, core: str, wrapper: str, generator: str):
        from backend.verification.mutations import MUTATIONS, apply_mutation

        m = MUTATIONS.get(name)
        if m is None:
            raise ValueError(f"unknown fault '{name}'")
        target = getattr(m, "target", "core")
        if target != "core":
            # Repairs only touch cl_core (the wrapper is regenerated from the spec), so a wrapper
            # fault would be "fixed" without the LLM -- that would misrepresent the repair loop.
            await ctx.say(f"Fault '{name}' targets the trusted wrapper; only core faults can be injected.", kind="error")
            return core, wrapper, generator
        try:
            core = apply_mutation(name, core)
        except ValueError as e:
            await ctx.say(f"Fault injection '{name}' does not apply to this design ({e}); continuing without it.", kind="error")
            return core, wrapper, generator
        await ctx.say(f"TEST MODE: deliberately injected defect '{name}' into the {target} "
                      f"({getattr(m, 'description', '')}) to demonstrate automatic debugging.", kind="error")
        return core, wrapper, f"{generator}+fault:{name}"

    def _read_core(self, rev: dict) -> str | None:
        p = Path(rev["dir"]) / "rtl" / "cl_core.v"
        return p.read_text() if p.is_file() else None

    def _write_rtl(self, rev: dict, spec: DesignSpec, core: str, wrapper: str, generator: str,
                   explanation: str, safety: list[dict]) -> dict:
        d = Path(rev["dir"]) / "rtl"
        d.mkdir(parents=True, exist_ok=True)
        (d / "cl_core.v").write_text(core)
        (d / "basys3_top.v").write_text(wrapper)
        (d / "constraints.xdc").write_text(generate_xdc(spec))
        files = []
        for name, role in (("cl_core.v", "core"), ("basys3_top.v", "wrapper"), ("constraints.xdc", "xdc")):
            data = (d / name).read_bytes()
            files.append({"name": name, "role": role, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)})
        for lib in library_paths():
            data = lib.read_bytes()
            files.append({"name": lib.name, "role": "library", "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)})
        return store.update_revision(rev["id"], files=files, generator=generator, explanation=explanation, safety=safety)

    async def _add_revision_to_run(self, ctx: RunContext, revision_id: str) -> None:
        run = store.get_run(ctx.run_id)
        ids = list(run.get("revision_ids") or []) + [revision_id]
        run = store.update_run(ctx.run_id, revision_ids=ids, revision_id=revision_id)
        rev = store.get_revision(revision_id)
        await bus.emit("revision.created", run_id=ctx.run_id, project_id=ctx.project_id, revision=store.revision_summary(rev))
        await bus.emit("run.updated", run_id=ctx.run_id, project_id=ctx.project_id, run=run)

    async def _emit_revision(self, ctx: RunContext, rev: dict) -> None:
        await bus.emit("revision.updated", run_id=ctx.run_id, project_id=ctx.project_id, revision=store.revision_summary(rev))

    async def _simulate(self, ctx: RunContext, spec: DesignSpec, rev: dict) -> dict:
        """lint + tests + simulate stages for one revision. Returns the SimulationReport as a dict."""
        from backend.verification.runner import run_simulation_suite

        d = Path(rev["dir"])
        await ctx.stage("lint", "running", f"revision {rev['number']}")
        await ctx.stage("tests", "pending", "")
        await ctx.stage("simulate", "pending", "")
        safety_errors = [s for s in (rev.get("safety") or []) if s.get("severity") == "error"]
        if safety_errors:
            report = {"status": "failed", "lint": {}, "build": {}, "plan": None, "scenarios": [], "requirements": [],
                      "passed_checks": 0, "failed_checks": len(safety_errors), "duration_s": 0.0,
                      "diagnostics": {"safety_errors": safety_errors}}
            await ctx.stage("lint", "failed", f"{len(safety_errors)} RTL safety errors", {"safety": safety_errors})
            await ctx.stage("tests", "skipped", "RTL rejected by safety checker")
            await ctx.stage("simulate", "skipped", "RTL rejected by safety checker")
            return report

        phase = {"stage": "lint", "lint_ok": True}

        async def emit(kind: str, data: dict) -> None:
            if kind == "log":
                line = str(data.get("line", ""))
                if line.startswith("lint (") and "FAILED" in line:
                    phase["lint_ok"] = False
                if line.startswith("building Verilator harness") and phase["stage"] == "lint":
                    # lint is finished once the runner moves on to the harness build
                    if phase["lint_ok"]:
                        await ctx.stage("lint", "passed", "clean at real and test clock parameters")
                    phase["stage"] = "simulate"
                    await ctx.stage("simulate", "running", "building the Verilator harness")
                await ctx.log(phase["stage"], "verilator", line)
            elif kind == "scenario":
                await ctx.log("simulate", "oracle", json.dumps({"scenario": data.get("id"), "passed": data.get("passed")}))
                await ctx.stage("simulate", "running", f"scenario {data.get('id')}: {'pass' if data.get('passed') else 'FAIL'}")
            elif kind == "stage":  # optional fine-grained progress from the runner
                await ctx.stage(data["stage"], data["status"], data.get("summary"))

        rep = await run_simulation_suite(spec, [d / "rtl" / "basys3_top.v", d / "rtl" / "cl_core.v"], d, emit=emit)
        report = rep.model_dump() if hasattr(rep, "model_dump") else dict(rep)
        lint = report.get("lint") or {}
        lint_ok = all(v.get("ok", False) for v in lint.values() if isinstance(v, dict)) if lint else report["status"] != "failed"
        nmsg = sum(len(v.get("messages", [])) for v in lint.values() if isinstance(v, dict))
        if report["status"] == "unavailable":
            await ctx.stage("lint", "unavailable", "Verilator not installed")
            await ctx.stage("tests", "unavailable", "")
            await ctx.stage("simulate", "unavailable", "")
            return report
        await ctx.stage("lint", "passed" if lint_ok else "failed", f"{nmsg} messages", {"lint": lint})
        plan = report.get("plan") or {}
        nscen = len(plan.get("scenarios", [])) if plan else 0
        nchecks = sum(len(s.get("checks", [])) for s in plan.get("scenarios", [])) if plan else 0
        if not lint_ok:
            await ctx.stage("tests", "skipped", "lint failed")
            await ctx.stage("simulate", "skipped", "lint failed")
        else:
            await ctx.stage("tests", "passed" if nscen else "failed", f"{nscen} scenarios, {nchecks} checks from the spec")
            status = {"passed": "passed", "failed": "failed"}.get(report["status"], "failed")
            await ctx.stage("simulate", status,
                            f"{report.get('passed_checks', 0)} passed / {report.get('failed_checks', 0)} failed checks"
                            + (" (infrastructure error)" if report["status"] == "error" else ""),
                            {"scenarios": [{"id": s.get("id"), "title": s.get("title"), "passed": s.get("passed")}
                                           for s in report.get("scenarios", [])]})
        return report

    def _sim_summary(self, report: dict) -> dict:
        return {"status": report.get("status"), "passed_checks": report.get("passed_checks", 0),
                "failed_checks": report.get("failed_checks", 0), "duration_s": report.get("duration_s"),
                "scenarios": [{"id": s.get("id"), "title": s.get("title"), "passed": s.get("passed")}
                              for s in report.get("scenarios", [])],
                "requirements": report.get("requirements", [])}

    async def _verify_with_repair(self, ctx: RunContext, spec: DesignSpec, rev: dict, provider) -> dict | None:
        from backend.agent.rtlgen import repair_core_rtl

        s = get_settings()
        attempt = 0
        while True:
            ctx.check_cancel()
            report = await self._simulate(ctx, spec, rev)
            (Path(rev["dir"]) / "sim").mkdir(exist_ok=True)
            (Path(rev["dir"]) / "sim" / "report.json").write_text(json.dumps(report, indent=2, default=str))
            passed = report.get("status") == "passed"
            rev = store.update_revision(rev["id"], simulation=self._sim_summary(report),
                                        sim_status="passed" if passed else "failed",
                                        status="sim_passed" if passed else
                                        ("lint_failed" if report.get("diagnostics", {}).get("lint_errors") else "sim_failed"))
            await self._emit_revision(ctx, rev)
            if passed:
                store.touch_project(ctx.project_id, current_revision_id=rev["id"])
                if attempt:
                    await ctx.stage("repair", "passed", f"fixed after {attempt} attempt{'s' if attempt > 1 else ''}")
                    await ctx.say(f"Repair attempt {attempt} fixed the design; all simulation checks pass.", kind="result")
                else:
                    await ctx.stage("repair", "skipped", "not needed")
                await ctx.say(f"Simulation passed: {report.get('passed_checks', 0)} checks across "
                              f"{len(report.get('scenarios', []))} scenarios (accelerated clock "
                              f"{report.get('plan', {}).get('clk_hz', s.sim_clk_hz)} Hz).", kind="result")
                return rev
            if report.get("status") in ("unavailable", "error"):
                await ctx.stage("repair", "skipped", "simulation could not run")
                await ctx.say("Simulation could not run: " + json.dumps(report.get("diagnostics", {}))[:800], kind="error")
                await ctx.skip_rest("synth", "skipped", "simulation did not pass")
                await ctx.set_status("failed", error="simulation infrastructure error", finished=True)
                return None
            failing = report.get("diagnostics", {}).get("failing_checks") or []
            await ctx.say(f"Simulation found {report.get('failed_checks', 0)} failing checks"
                          + (": " + "; ".join(str(f.get('summary', f))[:160] for f in failing[:4]) if failing else "."),
                          kind="error")
            if provider is None:
                await ctx.stage("repair", "unavailable", "automatic repair needs an LLM provider (ANTHROPIC_API_KEY)")
                await ctx.skip_rest("synth", "skipped", "simulation failed")
                await ctx.set_status("failed", error="simulation failed; no LLM configured for repair", finished=True)
                return None
            if attempt >= s.max_repair_attempts:
                await ctx.stage("repair", "failed", f"still failing after {attempt} attempts")
                await ctx.say(f"Stopping after {attempt} unsuccessful repair attempts. The last failing revision "
                              f"is r{rev['number']}; see the Simulation tab for diagnostics.", kind="error")
                await ctx.skip_rest("synth", "skipped", "simulation failed")
                await ctx.set_status("failed", error="repair attempts exhausted", finished=True)
                return None
            attempt += 1
            await ctx.stage("repair", "running", f"attempt {attempt}/{s.max_repair_attempts}")
            verilog = self._read_core(rev) or ""
            try:
                gen = await repair_core_rtl(spec, verilog, report.get("diagnostics", {}), attempt, provider)
            except Exception as e:
                await ctx.stage("repair", "failed", f"LLM repair failed: {type(e).__name__}")
                await ctx.say(f"Repair request failed: {e}", kind="error")
                await ctx.skip_rest("synth", "skipped", "simulation failed")
                await ctx.set_status("failed", error=f"repair failed: {e}", finished=True)
                return None
            ctx.check_cancel()
            new = store.create_revision(ctx.project_id, kind="repair", parent_id=rev["id"],
                                        user_request=f"automatic repair attempt {attempt}", spec=rev["spec"],
                                        interpreter=rev.get("interpreter", ""),
                                        change_summary=[gen.explanation[:300]] if gen.explanation else [])
            store.update_revision(new["id"], requirements=rev.get("requirements"), observations=rev.get("observations"))
            # The wrapper is trusted and deterministic: always regenerate it from the spec.
            rev = self._write_rtl(new, spec, gen.verilog, generate_wrapper(spec), gen.generator, gen.explanation, gen.safety)
            await self._add_revision_to_run(ctx, rev["id"])
            await ctx.say(f"Repair attempt {attempt} ({gen.generator}): {gen.explanation}".strip())

    # ================================================================ hardware flow
    async def _hardware_flow(self, ctx: RunContext, rev: dict) -> None:
        if ctx.options.get("simulation_only"):
            await ctx.skip_rest("synth", "skipped", "simulation-only mode")
            await ctx.set_status("passed_sim_only", finished=True)
            await ctx.say("Simulation-only mode: skipping Vivado and hardware.", kind="result")
            return
        built = await self._vivado(ctx, rev)
        rev = store.get_revision(rev["id"])
        if not built:
            return
        await self._program_and_verify(ctx, rev)

    async def _ensure_vivado(self, ctx: RunContext) -> bool:
        from backend.vivado import container as ctr

        env = await ctr.detect()
        if env.ready:
            return True
        if not env.docker_daemon:
            kind, title = "start_docker", "Start Docker Desktop"
            desc = "Docker Desktop is not running. Approve to launch it (open -a Docker); the Vivado container is checked afterwards."
        elif env.can_start_headless:
            kind, title = "start_vivado_container", "Start a headless Vivado container"
            desc = (f"No Vivado container is running. Approve to start '{get_settings().vivado_headless_container}' from image "
                    f"'{get_settings().vivado_image}' with your existing mount (no GUI, no VNC, no xvcd). "
                    "Your vivado_container and class projects are not modified.")
        else:
            await ctx.say("Vivado is not available: " + "; ".join(env.problems), kind="error")
            return False
        existing = approvals.pending_of_kind(kind)
        appr = existing or await approvals.request(kind, title, desc, {"problems": env.problems},
                                                   run_id=ctx.run_id, project_id=ctx.project_id)
        await ctx.stage("synth", "blocked", f"waiting for approval: {title}")
        await ctx.set_status("waiting")
        decision = await approvals.wait(appr["id"], ctx.cancel)
        ctx.check_cancel()
        await ctx.set_status("running")
        if decision != "approved":
            return False
        env = await ctr.detect()
        if not env.ready and kind == "start_docker" and env.docker_daemon and env.can_start_headless:
            return await self._ensure_vivado(ctx)
        return env.ready

    async def _vivado(self, ctx: RunContext, rev: dict) -> bool:
        from backend.vivado import build as vb

        await ctx.stage("synth", "running", "checking Vivado environment")
        if not await self._ensure_vivado(ctx):
            await ctx.skip_rest("synth", "unavailable", "Vivado environment not available")
            await ctx.set_status("passed_sim_only", finished=True)
            await ctx.say("Vivado is not available, so this run stops after simulation (simulation-only result).", kind="result")
            return False
        while vb.is_busy():  # another run is building; wait our turn
            await ctx.stage("synth", "blocked", "waiting for another Vivado build to finish")
            await asyncio.sleep(3)
            ctx.check_cancel()

        d = Path(rev["dir"])
        build_id = re.sub(r"[^a-z0-9_-]", "-", f"{rev['id']}-{ctx.run_id[-6:]}".lower())
        stage_state: dict[str, str] = {}

        async def on_stage(vstage: str, status: str) -> None:
            key = VIVADO_STAGE_MAP.get(vstage)
            if key is None:
                return
            ctx.current_vivado_stage = key
            if status == "running" and vstage in VIVADO_STAGE_FIRST:
                await ctx.stage(key, "running", f"vivado {vstage}")
            elif status == "running":
                await ctx.stage(key, "running", f"vivado {vstage}")
            elif status == "failed":
                await ctx.stage(key, "failed", f"vivado {vstage} failed")
            elif status == "passed" and vstage in VIVADO_STAGE_LAST:
                stage_state[key] = "passed"
                await ctx.stage(key, "passed", f"vivado {vstage} done")

        async def on_line(line: str) -> None:
            await ctx.log(ctx.current_vivado_stage, "vivado", line)

        await ctx.say("Running Vivado (batch mode, existing container): synthesis, implementation, timing, bitstream.")
        try:
            res = await vb.run_build(build_id, [d / "rtl" / "basys3_top.v", d / "rtl" / "cl_core.v"] + library_paths(),
                                     d / "rtl" / "constraints.xdc", d / "vivado", on_line=on_line, on_stage=on_stage,
                                     cancel=ctx.cancel)
        except vb.VivadoBusy as e:
            await ctx.skip_rest("synth", "failed", str(e))
            await ctx.set_status("failed", error=str(e), finished=True)
            return False
        if res.status == "cancelled":
            raise RunCancelled()
        checks = {c["name"]: c for c in res.checks}
        summary = {
            "status": res.status, "timing": res.timing, "utilization": res.utilization,
            "bitstream_sha256": res.bitstream_sha256, "bitstream_path": res.bitstream_path,
            "bitstream_size": (res.bitstream or {}).get("size_bytes"), "checks": res.checks,
            "duration_s": res.duration_s, "stage_times": res.stage_times, "vivado_version": res.vivado_version,
            "container": res.container, "message": res.message,
        }
        if res.status == "unavailable":
            await ctx.skip_rest("synth", "unavailable", res.message[:200])
            rev = store.update_revision(rev["id"], build=summary, build_status="unavailable")
            await self._emit_revision(ctx, rev)
            await ctx.set_status("passed_sim_only", finished=True)
            return False
        # timing / bitstream verdicts come from the acceptance checks, not just tool exit codes
        t_ok = checks.get("timing_met", {}).get("passed") and checks.get("clock_constrained", {}).get("passed", False)
        t = res.timing or {}
        failed_key = VIVADO_STAGE_MAP.get(res.failed_stage or "")
        for key in ("synth", "impl"):
            if stage_state.get(key) == "passed":
                continue
            if failed_key == key or (failed_key is None and not res.ok):
                await ctx.stage(key, "failed", (f"vivado {res.failed_stage} failed: " if res.failed_stage else "") + res.message[:180])
                failed_key = failed_key or key
            else:
                await ctx.stage(key, "skipped", "not reached")
        if t:
            util = (res.utilization or {}).get("summary", {})
            await ctx.stage("timing", "passed" if t_ok else "failed",
                            f"WNS {t.get('wns_ns')} ns, WHS {t.get('whs_ns')} ns; "
                            f"LUT {util.get('lut', {}).get('used', '?')}, FF {util.get('ff', {}).get('used', '?')}",
                            {"timing": t, "utilization": res.utilization})
        else:
            await ctx.stage("timing", "failed" if res.failed_stage else "skipped", "no timing report")
        bit_ok = checks.get("bitstream_valid", {}).get("passed", False)
        await ctx.stage("bitstream", "passed" if bit_ok and res.ok else "failed",
                        checks.get("bitstream_valid", {}).get("detail", res.message)[:200],
                        {"sha256": res.bitstream_sha256, "path": res.bitstream_path})
        rev = store.update_revision(rev["id"], build=summary, build_status="passed" if res.ok else "failed",
                                    status="built" if res.ok else "build_failed")
        await self._emit_revision(ctx, rev)
        if not res.ok:
            failed = [f"{c['name']}: {c['detail']}" for c in res.checks if not c["passed"]]
            await ctx.say("Vivado build did not pass acceptance checks:\n" + "\n".join(f"- {f}" for f in failed), kind="error")
            await ctx.skip_rest("detect", "skipped", "no valid bitstream")
            await ctx.set_status("failed", error="Vivado build failed", finished=True)
            return False
        await ctx.say(f"Vivado build passed in {res.duration_s:.0f} s: WNS {t.get('wns_ns')} ns, WHS {t.get('whs_ns')} ns, "
                      f"bitstream {summary['bitstream_size']} bytes (sha256 {res.bitstream_sha256[:12]}...).", kind="result")
        return True

    async def _program_and_verify(self, ctx: RunContext, rev: dict) -> None:
        from backend.programmer.manager import hardware

        build = rev.get("build") or {}
        bit = Path(build.get("bitstream_path") or "")
        await ctx.stage("detect", "running", "looking for the Basys 3")
        det = await hardware.detect()
        if det.state != "connected":
            label = {"no_board": "Basys 3 not connected", "busy": "JTAG cable busy", "tool_missing": "programmer tool missing",
                     "unsupported": "unsupported device on JTAG", "error": "programmer error"}.get(det.state, det.state)
            await ctx.stage("detect", "unavailable" if det.state in ("no_board", "tool_missing") else "failed",
                            f"{label}: {det.message}"[:200], det.model_dump())
            await ctx.stage("program", "skipped", "no board available")
            await ctx.stage("verify", "skipped", "design was not programmed")
            await ctx.say(f"Hardware: {label}. The bitstream is ready at {bit}; connect the board and use "
                          "'Program FPGA' in the Hardware tab to program it later.", kind="result")
            await ctx.set_status("passed_sim_only", finished=True)
            return
        await ctx.stage("detect", "passed", f"{det.device or 'xc7a35t'} idcode {det.idcode} via {det.adapter}", det.model_dump())
        ok = await self._program(ctx, rev, bit, build.get("bitstream_sha256") or "")
        if not ok:
            return
        await ctx.stage("verify", "blocked", "open the Vision tab to verify the LEDs with the webcam")
        await ctx.set_status("waiting")
        await ctx.say("The FPGA is programmed. Open the Vision tab, select the LED region and follow the guided "
                      "steps to verify the behaviour with the webcam.")

    async def _program(self, ctx: RunContext, rev: dict, bit: Path, sha: str) -> bool:
        from backend.programmer.manager import hardware

        if not bit.is_file() or _sha256(bit) != sha:
            await ctx.stage("program", "failed", "bitstream missing or hash mismatch")
            await ctx.set_status("failed", error="bitstream integrity check failed", finished=True)
            return False
        st = await hardware.status(deep=False)
        appr = await approvals.request(
            "program_fpga", "Program the Basys 3",
            f"Load revision r{rev['number']} into the FPGA's configuration SRAM (volatile: the flash is not written; "
            f"power-cycling restores the previous design). Adapter: {st.preferred_adapter or 'auto'}.",
            {"bitstream_sha256": sha, "bitstream_path": str(bit), "revision_id": rev["id"],
             "adapter": st.preferred_adapter, "volatile": True},
            run_id=ctx.run_id, project_id=ctx.project_id)
        await ctx.stage("program", "blocked", "waiting for your approval")
        await ctx.set_status("waiting")
        decision = await approvals.wait(appr["id"], ctx.cancel)
        ctx.check_cancel()
        await ctx.set_status("running")
        if decision != "approved":
            await ctx.stage("program", "skipped", "programming declined")
            await ctx.stage("verify", "skipped", "design was not programmed")
            await ctx.set_status("passed_sim_only", finished=True)
            await ctx.say("Programming declined; the bitstream stays available for later.", kind="result")
            return False
        await ctx.stage("program", "running", "programming SRAM")
        res = await hardware.program(bit, sha, approval_id=appr["id"])
        approvals.mark_result(appr["id"], res.ok, res.model_dump())
        appr = store.get_approval(appr["id"])
        await bus.emit("approval.resolved", run_id=ctx.run_id, project_id=ctx.project_id, approval=appr)
        for line in (res.raw or "").splitlines()[-40:]:
            await ctx.log("program", res.adapter, line)
        if not res.ok:
            await ctx.stage("program", "failed", f"{res.state}: {res.message}"[:200], res.model_dump())
            await ctx.stage("verify", "skipped", "programming failed")
            await ctx.say(f"Programming failed ({res.adapter}): {res.message}", kind="error")
            await ctx.set_status("failed", error="programming failed", finished=True)
            return False
        await ctx.stage("program", "passed", f"{res.adapter}: {res.message}"[:200], res.model_dump())
        rev = store.update_revision(rev["id"], status="programmed")
        await self._emit_revision(ctx, rev)
        await ctx.say(f"Programmed the Basys 3 via {res.adapter} in {res.duration_s:.1f} s.", kind="result")
        return True

    # ================================================================ rebuild / reprogram
    async def _mark_reused(self, ctx: RunContext, rev: dict, upto: str) -> None:
        for key in STAGE_KEYS[: STAGE_KEYS.index(upto)]:
            await ctx.stage(key, "skipped", f"reused from revision r{rev['number']}")
        await self._add_revision_to_run(ctx, rev["id"])

    async def _rebuild_run(self, ctx: RunContext, text: str, rev: dict) -> None:
        await self._mark_reused(ctx, rev, "synth")
        built = await self._vivado(ctx, rev)
        if built:
            await self._program_and_verify(ctx, store.get_revision(rev["id"]))

    async def _reprogram_run(self, ctx: RunContext, text: str, rev: dict) -> None:
        await self._mark_reused(ctx, rev, "detect")
        await self._program_and_verify(ctx, rev)

    # ================================================================ vision results
    async def record_verification(self, revision_id: str, result: dict) -> dict:
        """Store a camera verdict for a revision and fold it into that revision's latest run.

        The camera cannot know which bitstream is on the board, so the verdict only
        upgrades a run to 'passed'/'failed' when SiliconBreak X itself confirmed
        programming this revision. Otherwise it is stored with
        programmed_by_siliconbreak=False and the run status is left unchanged.
        """
        rev = store.get_revision(revision_id)
        if rev is None:
            return result
        status = result.get("status")
        confirmed = rev["status"] in ("programmed", "verified", "verify_failed")
        result = {**result, "programmed_by_siliconbreak": confirmed}
        new_status = {"passed": "verified", "failed": "verify_failed"}.get(status) if confirmed else None
        rev = store.update_revision(revision_id, verification=result, **({"status": new_status} if new_status else {}))
        run = store.latest_run_for_revision(revision_id)
        if run is None:
            return result
        ctx = RunContext(run["id"], run["project_id"], run.get("options") or {})
        summary = result.get("summary", "")
        if not confirmed:
            summary = "board programming not confirmed by SiliconBreak X for this revision; " + summary
        await ctx.stage("verify", {"passed": "passed", "failed": "failed", "inconclusive": "inconclusive"}.get(status, "unavailable"),
                        summary[:200], {"result": result})
        await bus.emit("revision.updated", run_id=run["id"], project_id=run["project_id"], revision=store.revision_summary(rev))
        if confirmed and run["status"] in ("waiting", "passed_sim_only"):
            final = {"passed": "passed", "failed": "failed"}.get(status, "passed_sim_only")
            await ctx.set_status(final, finished=True)
        reqs = result.get("requirements", [])
        observed = [r["id"] for r in reqs if r.get("physical_status") == "observed_pass"]
        sim_only = [r["id"] for r in reqs if r.get("physical_status") in ("not_observable", "not_observed")]
        caveat = "" if confirmed else ("\nNote: SiliconBreak X did not program this revision, so the camera result is not "
                                       "attributed to it (the board may hold a different design).")
        await ctx.say(f"Physical verification: {status}. {result.get('summary', '')}\n"
                      f"Physically observed: {', '.join(observed) or 'none'}. "
                      f"Simulation-only: {', '.join(sim_only) or 'none'}.{caveat}",
                      kind="result" if status == "passed" else "error")
        return result


orchestrator = Orchestrator()
