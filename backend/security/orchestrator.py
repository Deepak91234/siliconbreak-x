"""The security audit pipeline (a CircuitLoop run with security stages).

    contract -> index -> investigate -> reproduce -> formal -> repair -> retest -> evidence

Each stage publishes the same events as a design run (stage.updated, log,
agent.message, run.*) plus `security.updated` carrying the audit record, so the
existing timeline, replay and WebSocket plumbing show audits without changes.

Finding status ladder, enforced here:
  suspected        an investigator (model or offline rules) proposed it
  reproduced       a trusted security test for one of its properties failed
  patch_proposed   a repair agent produced a diff
  repaired_verified  the trusted verifier passed every test on that diff (and
                     the formal check, when the case has one, did not refute it)
  repair_failed    the diff was rejected or did not pass
  false_positive   the trusted tests pass AND formal proves its properties
  inconclusive     neither reproduced nor refuted

A human reference fix is recorded as harness validation and never moves a
finding past `reproduced`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import traceback
from pathlib import Path
from typing import Any

from backend.agent.orchestrator import RunCancelled, RunContext, orchestrator as design_orchestrator
from backend.core.config import get_settings
from backend.core.events import bus
from backend.security import formal as formal_mod
from backend.security import investigate as inv
from backend.security import repair as rep
from backend.security.cases import CaseManifest, get_case, sha256_file
from backend.security.verify import verify_sources
from backend.storage import store
from backend.storage.db import now

log = logging.getLogger("siliconbreak.orchestrator")

SECURITY_STAGES: list[tuple[str, str]] = [
    ("contract", "Security contract"),
    ("index", "Source indexing"),
    ("investigate", "Investigation"),
    ("reproduce", "Independent reproduction"),
    ("formal", "Formal property check"),
    ("repair", "Repair attempt"),
    ("retest", "Independent retest"),
    ("evidence", "Evidence record"),
]
REPAIR_STAGES = [k for k in SECURITY_STAGES if k[0] in ("repair", "retest", "evidence")]
REPAIR_AGENTS = ("auto", "factory", "model", "reference", "none")


class AuditError(Exception):
    pass


def _rel(path: Path, root: Path) -> str:
    return Path(path).resolve().relative_to(root.resolve()).as_posix()


class SecurityOrchestrator:
    def __init__(self) -> None:
        self.tasks: dict[str, asyncio.Task] = {}
        self.locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------ entrypoints
    async def start_case_audit(self, case_id: str, *, investigator: str | None = None, repair_agent: str = "auto",
                               max_attempts: int | None = None) -> dict:
        case = get_case(case_id)
        if case is None:
            raise AuditError(f"unknown security case {case_id!r}")
        if repair_agent not in REPAIR_AGENTS:
            raise AuditError(f"unknown repair agent {repair_agent!r}")
        project_id = self._project_for_case(case)
        opts = {"investigator": investigator or "auto", "repair_agent": repair_agent,
                "max_attempts": max(1, min(int(max_attempts or get_settings().max_repair_attempts), 5))}
        audit = store.create_security_audit(project_id=project_id, source_kind=case.source_kind, case_id=case.case_id,
                                            options=opts)
        text = f"Security audit: {case.title}"
        run = store.create_run(project_id, trigger="security_audit", request_text=text, options={"audit_id": audit["id"], **opts},
                               stages=SECURITY_STAGES)
        audit = store.update_security_audit(audit["id"], run_id=run["id"], status="running",
                                            record=self._initial_record(case, opts))
        await self._emit_audit(audit)
        await bus.emit("run.created", run_id=run["id"], project_id=project_id, run=run)
        ctx = RunContext(run["id"], project_id, opts)
        msg = store.add_message(project_id, "user", text, run_id=run["id"])
        await bus.emit("agent.message", run_id=run["id"], project_id=project_id, message=msg)
        self._launch(ctx, audit["id"], self._audit_run(ctx, audit["id"], case))
        return {"audit": self.public(store.get_security_audit(audit["id"])), "run": run,
                "project": store.get_project(project_id)}

    async def start_repair(self, audit_id: str, agent: str) -> dict:
        audit = store.get_security_audit(audit_id)
        if audit is None:
            raise KeyError(audit_id)
        if agent not in REPAIR_AGENTS or agent == "none":
            raise AuditError(f"unknown repair agent {agent!r}")
        if any(not t.done() for k, t in self.tasks.items() if k.startswith(audit_id)):
            raise AuditError("this audit is still running")
        case = get_case(audit["case_id"] or "")
        if case is None:
            raise AuditError("repairs need a case with trusted tests")
        record = audit["record"]
        if not record.get("reproduction"):
            raise AuditError("the original design has not been verified yet")
        text = f"Repair attempt ({agent}): {case.title}"
        run = store.create_run(audit["project_id"], trigger="security_repair", request_text=text,
                               options={"audit_id": audit_id, "repair_agent": agent}, stages=REPAIR_STAGES)
        store.update_security_audit(audit_id, status="running", run_id=run["id"], finished_at=None)
        await bus.emit("run.created", run_id=run["id"], project_id=audit["project_id"], run=run)
        ctx = RunContext(run["id"], audit["project_id"], {"repair_agent": agent})
        msg = store.add_message(audit["project_id"], "user", text, run_id=run["id"])
        await bus.emit("agent.message", run_id=run["id"], project_id=audit["project_id"], message=msg)
        self._launch(ctx, audit_id, self._repair_run(ctx, audit_id, case, agent, standalone=True))
        return {"audit": self.public(store.get_security_audit(audit_id)), "run": run}

    def _launch(self, ctx: RunContext, audit_id: str, coro) -> None:
        design_orchestrator.contexts[ctx.run_id] = ctx  # lets POST /api/runs/{id}/cancel stop audits too
        task = asyncio.create_task(self._guard(ctx, audit_id, coro))
        self.tasks[f"{audit_id}:{ctx.run_id}"] = task

    async def _guard(self, ctx: RunContext, audit_id: str, coro) -> None:
        try:
            await coro
        except RunCancelled:
            await self._finish_stages(ctx, "cancelled", "cancelled by user")
            await ctx.set_status("cancelled", finished=True)
            await self._set(audit_id, status="cancelled", finished_at=now())
        except Exception as e:
            log.exception("security run %s crashed", ctx.run_id)
            await self._finish_stages(ctx, "failed", f"internal error: {type(e).__name__}")
            await ctx.set_status("failed", error=f"{type(e).__name__}: {e}", finished=True)
            await self._set(audit_id, status="failed", finished_at=now())
            await ctx.say(f"Internal error: {type(e).__name__}: {e}", kind="error",
                          data={"traceback": traceback.format_exc()[-4000:]})
        finally:
            design_orchestrator.contexts.pop(ctx.run_id, None)
            self.tasks.pop(f"{audit_id}:{ctx.run_id}", None)

    async def _finish_stages(self, ctx: RunContext, status: str, summary: str) -> None:
        run = store.get_run(ctx.run_id)
        for s in run["stages"]:
            if s["status"] in ("pending", "running", "blocked"):
                await ctx.stage(s["key"], status, summary)

    # ---------------------------------------------------------------- helpers
    def _project_for_case(self, case: CaseManifest) -> str:
        for a in reversed(store.list_security_audits(case_id=case.case_id)):
            if a.get("project_id") and store.get_project(a["project_id"]):
                return a["project_id"]
        return store.create_project(f"Security audit: {case.title}")["id"]

    def _initial_record(self, case: CaseManifest, opts: dict) -> dict:
        return {
            "case": case.public_view(),
            "options": opts,
            "source": {"kind": "case", "case_id": case.case_id, "label": "case source (as provided)",
                       "files": {rel: sha256_file(case.path(rel)) for rel in case.editable}},
            "providers": {},
            "investigation": None,
            "findings": [],
            "unexplained_failures": [],
            "reproduction": None,
            "formal_original": None,
            "repairs": [],
            "harness_validation": None,
            "outcome": {"status": "running", "summary": ""},
            "tool_versions": {},
            "evidence": None,
        }

    async def _set(self, audit_id: str, **fields) -> dict:
        audit = store.update_security_audit(audit_id, **fields)
        await self._emit_audit(audit)
        return audit

    async def _record(self, audit_id: str, mutate) -> dict:
        audit = store.get_security_audit(audit_id)
        record = audit["record"]
        mutate(record)
        return await self._set(audit_id, record=record)

    async def _emit_audit(self, audit: dict) -> None:
        await bus.emit("security.updated", run_id=audit.get("run_id"), project_id=audit.get("project_id"),
                       audit=self.public(audit))

    @staticmethod
    def public(audit: dict | None) -> dict | None:
        if audit is None:
            return None
        out = {k: v for k, v in audit.items() if k != "dir"}
        return out

    @staticmethod
    def _advance(f: dict, status: str, why: str, evidence: dict | None = None) -> None:
        f["status"] = status
        f.setdefault("history", []).append({"status": status, "at": now(), "why": why, **({"evidence": evidence} if evidence else {})})

    # =============================================================== audit run
    async def _audit_run(self, ctx: RunContext, audit_id: str, case: CaseManifest) -> None:
        audit = store.get_security_audit(audit_id)
        root = Path(audit["dir"])
        opts = audit["options"]

        # ---- contract
        await ctx.stage("contract", "running", "loading the trusted case manifest")
        hashes = case.trusted_hashes()
        props = ", ".join(case.property_ids())
        await ctx.log("contract", "siliconbreak", f"case {case.case_id} ({case.source_kind}): {case.provenance}")
        for rel, h in hashes.items():
            await ctx.log("contract", "siliconbreak", f"trusted {rel} sha256 {h}")
        await ctx.stage("contract", "passed", f"{len(case.security_contract.properties)} security properties ({props}), "
                                              f"{len(case.verification.tests)} trusted tests",
                        detail={"summary": case.security_contract.summary, "trusted_hashes": hashes,
                                "source_kind": case.source_kind})
        await ctx.say(f"Auditing **{case.title}** against its security contract: {case.security_contract.summary} "
                      f"({case.source_kind} case: {case.provenance})")
        ctx.check_cancel()

        # ---- index
        await ctx.stage("index", "running", "indexing editable RTL")
        files = {rel: case.path(rel).read_text() for rel in case.editable}
        modules = {rel: re.findall(r"^\s*module\s+(\w+)", txt, re.M) for rel, txt in files.items()}
        lines = sum(len(t.splitlines()) for t in files.values())
        await ctx.stage("index", "passed", f"{len(files)} file(s), {lines} lines, modules: "
                                           + ", ".join(m for ms in modules.values() for m in ms),
                        detail={"files": {rel: {"lines": len(t.splitlines()), "modules": modules[rel]} for rel, t in files.items()},
                                "audited": sorted(files), "indexed": sorted(files)})
        ctx.check_cancel()

        # ---- investigate
        name, provider, note = inv.pick_provider(opts.get("investigator"))
        label = {"bitdeer": "Bitdeer", "anthropic": "Claude", "rules": "offline static rules (not AI)"}[name]
        await ctx.stage("investigate", "running", f"investigating with {label}"
                        + (f" ({provider.model})" if provider else ""))
        if note:
            await ctx.log("investigate", "siliconbreak", note)
        irec = await inv.investigate(case.security_contract.model_dump(), [p.model_dump() for p in case.interface], files,
                                     root / "investigate", provider_override=(name, provider, note))
        used = irec if irec["status"] == "ok" else (irec.get("fallback") or irec)
        findings = []
        for f in used.get("findings", []):
            f = {**f, "provider": used.get("provider"), "model": used.get("model"), "is_ai": used.get("is_ai", False)}
            self._advance(f, "suspected", f"proposed by {used.get('provider')}")
            findings.append(f)
        for d in used.get("dropped", []):
            await ctx.log("investigate", "siliconbreak", f"dropped unverifiable finding: {d}")
        await self._record(audit_id, lambda r: r.update(investigation=irec, findings=findings,
                                                        providers={**r["providers"], "investigator": {
                                                            "provider": name, "model": irec.get("model"),
                                                            "is_ai": provider is not None, "note": note}}))
        summary = f"{label}: {len(findings)} suspected finding(s)"
        if irec["status"] != "ok":
            err = (irec.get("error") or {}).get("message", "error")
            summary = f"{label} failed ({err}); offline rules found {len(findings)} pattern(s)"
        await ctx.stage("investigate", "passed" if irec["status"] == "ok" else "inconclusive", summary,
                        detail={"provider": name, "model": irec.get("model"), "findings": len(findings),
                                "dropped": used.get("dropped", []), "duration_s": irec.get("duration_s")})
        if findings:
            await ctx.say("Suspected (not yet verified): " + "; ".join(
                f"**{f['title']}** at {f['file']}:{f['line_start']}-{f['line_end']} ({', '.join(f['violated_properties'])})"
                for f in findings) + f". Source: {label}.")
        else:
            await ctx.say(f"The investigation ({label}) reported no suspected contract violation. "
                          "The trusted verifier runs anyway.")
        ctx.check_cancel()

        # ---- reproduce (trusted verifier on the original source)
        await ctx.stage("reproduce", "running", "running the trusted security and functional tests (Verilator)")
        original = {rel: case.path(rel) for rel in case.editable}

        async def vlog(line: str) -> None:
            await ctx.log("reproduce", "verilator", line)

        report = await verify_sources(case, original, root / "reproduce", label="original", on_line=vlog)
        repd = report.model_dump()
        repd["dir"] = "reproduce"
        failed_props = set(report.failed_properties)
        fail_tests = [t for t in report.tests if t.status == "fail" and t.kind == "security"]

        def apply_repro(r: dict) -> None:
            r["reproduction"] = repd
            r["tool_versions"].update(report.tool_versions)
            explained: set[str] = set()
            for f in r["findings"]:
                hits = [t.id for t in fail_tests if set(t.properties) & set(f["violated_properties"])]
                if hits:
                    self._advance(f, "reproduced", f"trusted tests failed: {', '.join(hits)}",
                                  {"tests": hits, "report": "reproduce/report.json"})
                    explained |= set(hits)
            r["unexplained_failures"] = [{"test": t.id, "properties": t.properties, "message": t.message}
                                         for t in fail_tests if t.id not in explained]

        await self._record(audit_id, apply_repro)
        if report.status in ("error", "timeout", "not_run"):
            await ctx.stage("reproduce", "failed" if report.status != "not_run" else "unavailable",
                            f"verifier {report.status}: {report.message}", detail={"report": "reproduce/report.json"})
            await self._finish_audit(ctx, audit_id, "error", f"The trusted verifier could not judge the design: {report.message}")
            return
        c = report.counts()
        if failed_props:
            await ctx.stage("reproduce", "failed",
                            f"violated {', '.join(sorted(failed_props))}: {c['security']['fail']} security test(s) fail; "
                            f"functional {c['functional']['pass']}/{sum(c['functional'].values())} pass",
                            detail={"failed_tests": [t.id for t in fail_tests], "vcd": report.vcd})
            await ctx.say(f"**Reproduced** by the independent verifier: {len(fail_tests)} trusted security test(s) fail "
                          f"({', '.join(sorted(failed_props))}). First failure: {fail_tests[0].id}: {fail_tests[0].message}")
        else:
            await ctx.stage("reproduce", "passed", f"all {len(report.tests)} trusted tests pass; nothing reproduced",
                            detail={"vcd": report.vcd})
        ctx.check_cancel()

        # ---- formal (original)
        await ctx.stage("formal", "running", "Yosys SAT: bounded check + temporal induction")

        async def flog(line: str) -> None:
            await ctx.log("formal", "yosys", line)

        fres = await formal_mod.check(case, [case.path(rel) for rel in case.editable], root / "formal_original",
                                      label="original", on_line=flog)
        fd = fres.model_dump()
        fd["dir"] = "formal_original"

        def apply_formal(r: dict) -> None:
            r["formal_original"] = fd
            if fres.tool_version:
                r["tool_versions"]["yosys"] = fres.tool_version
            if fres.status == "proven" and not failed_props:
                for f in r["findings"]:
                    if f["status"] == "suspected" and set(f["violated_properties"]) <= set(fres.properties):
                        self._advance(f, "false_positive", "trusted tests pass and formal proves the cited properties")
            if fres.status == "counterexample":
                for f in r["findings"]:
                    if f["status"] == "reproduced":
                        f.setdefault("history", [])[-1].setdefault("evidence", {})["formal"] = "formal_original/cex.vcd"

        await self._record(audit_id, apply_formal)
        fstatus = {"proven": "passed", "counterexample": "failed", "bounded": "inconclusive",
                   "error": "failed", "not_run": "unavailable"}[fres.status]
        await ctx.stage("formal", fstatus, fres.message, detail={"status": fres.status, "vcd": fres.vcd})
        ctx.check_cancel()

        # findings never reproduced or refuted
        def settle(r: dict) -> None:
            for f in r["findings"]:
                if f["status"] == "suspected":
                    self._advance(f, "inconclusive", "no trusted test or proof matched the cited properties")
        await self._record(audit_id, settle)

        if not failed_props and fres.status != "counterexample":
            for k in ("repair", "retest"):
                await ctx.stage(k, "skipped", "nothing to repair: no contract violation was reproduced")
            await self._evidence(ctx, audit_id, case)
            await self._finish_audit(ctx, audit_id, "no_violation_found",
                                     "No contract violation was reproduced by the trusted tests"
                                     + (" and the formal check proved the properties." if fres.status == "proven" else "."))
            return

        await self._repair_run(ctx, audit_id, case, opts.get("repair_agent", "auto"), standalone=False)

    # ============================================================= repair run
    async def _repair_run(self, ctx: RunContext, audit_id: str, case: CaseManifest, agent: str, *, standalone: bool) -> None:
        audit = store.get_security_audit(audit_id)
        root = Path(audit["dir"])
        record = audit["record"]
        s = get_settings()
        max_attempts = int(audit["options"].get("max_attempts", s.max_repair_attempts))

        chosen, model_provider, why = self._choose_agent(agent)
        if chosen is None:
            await ctx.stage("repair", "unavailable", why)
            await ctx.stage("retest", "skipped", "no repair attempt was made")
            await ctx.say(f"Repair not attempted: {why}. The vulnerability stays **reproduced**; "
                          "you can validate the harness with the human reference fix from the Security tab.")
            await self._evidence(ctx, audit_id, case)
            await self._finish_audit(ctx, audit_id, "reproduced", f"Vulnerability reproduced; repair unavailable ({why}).")
            return

        report = record["reproduction"]
        formal_orig = record.get("formal_original")
        finding = next((f for f in record["findings"] if f["status"] in ("reproduced", "repair_failed")), None)
        sources = {rel: case.path(rel) for rel in case.editable}
        attempts = 1 if chosen == "reference" else max_attempts
        evidence = rep.failing_evidence_text(case, report, formal_orig)
        verdict = None
        for n in range(1, attempts + 1):
            ctx.check_cancel()
            idx = len(store.get_security_audit(audit_id)["record"]["repairs"]) + 1
            adir = root / "repair" / f"attempt-{idx}"
            ws = adir / "workspace"
            task_md = rep.task_markdown(case, evidence, finding)
            before = rep.make_workspace(case, sources, ws, task_md)
            label = {"factory": "Factory Droid", "model": f"model patch ({model_provider[0] if model_provider else ''})",
                     "reference": "human reference fix (harness validation)"}[chosen]
            await ctx.stage("repair", "running", f"attempt {n}/{attempts}: {label} editing an isolated copy")

            async def on_event(kind: str, item: dict) -> None:
                txt = item.get("text") or item.get("message") or (f"{item.get('name')} {json.dumps(item.get('input', ''))[:300]}"
                                                                 if item.get("type") == "ToolCall" else item.get("content", ""))
                await ctx.log("repair", kind, f"[{item.get('type')}] {str(txt)[:500]}")

            if chosen == "factory":
                arec = await rep.repair_with_droid(case, ws, on_event=on_event)
            elif chosen == "model":
                arec = await rep.repair_with_model(case, ws, model_provider, task_md=task_md)
            else:
                arec = rep.apply_reference(case, ws)
            changes = rep.collect_changes(case, ws, before, sources)
            arec.update(attempt=idx, dir=f"repair/attempt-{idx}", diff=changes["diff"], changed_files=changes["changed_files"],
                        scope_violations=changes["scope_violations"], extra_files=changes["extra_files"],
                        started_at=now())
            (adir / "patch.diff").write_text(changes["diff"])
            for line in (arec.get("final_text") or "").splitlines()[:20]:
                await ctx.log("repair", chosen, line)
            if arec["status"] not in ("completed",) or not changes["changed_files"]:
                why2 = arec.get("reason") or ("the agent made no change to the editable files" if arec["status"] == "completed" else arec["status"])
                arec["verdict"] = "no_patch"
                await self._record(audit_id, lambda r: r["repairs"].append(arec))
                await ctx.stage("repair", "failed" if arec["status"] != "unavailable" else "unavailable",
                                f"attempt {n}: {label}: {why2}")
                verdict = "no_patch"
                if arec["status"] in ("unavailable", "error"):
                    break
                continue

            if chosen != "reference":
                def proposed(r: dict) -> None:
                    for f in r["findings"]:
                        if f["status"] in ("reproduced", "repair_failed"):
                            self._advance(f, "patch_proposed", f"{label} attempt {idx} changed {', '.join(changes['changed_files'])}",
                                          {"diff": f"repair/attempt-{idx}/patch.diff"})
                await self._record(audit_id, proposed)
            added = sum(1 for ln in changes["diff"].splitlines() if ln.startswith("+") and not ln.startswith("+++"))
            removed = sum(1 for ln in changes["diff"].splitlines() if ln.startswith("-") and not ln.startswith("---"))
            await ctx.stage("repair", "passed", f"attempt {n}: {label} changed {', '.join(changes['changed_files'])} "
                                                f"(+{added}/-{removed} lines)",
                            detail={"diff": changes["diff"][:20000], "agent": chosen})

            # ---- retest with the trusted verifier and formal check
            await ctx.stage("retest", "running", f"attempt {n}: trusted verifier on the patched copy")

            async def rlog(line: str) -> None:
                await ctx.log("retest", "verilator", line)

            if changes["scope_violations"]:
                vrep = None
                fres = None
                arec["verdict"] = "rejected_scope"
            else:
                vrep = await verify_sources(case, changes["candidates"], adir / "verify", label=f"attempt-{idx}", on_line=rlog)
                fres = await formal_mod.check(case, [changes["candidates"][rel] for rel in case.editable], adir / "formal",
                                              label=f"attempt-{idx}")
                ok = vrep.status == "passed" and (fres.status in ("proven", "bounded", "not_run"))
                arec["verdict"] = "verified" if ok else "failed"
            arec["verification"] = vrep.model_dump() if vrep else None
            if arec["verification"]:
                arec["verification"]["dir"] = f"repair/attempt-{idx}/verify"
            arec["formal"] = fres.model_dump() if fres else None
            if arec["formal"]:
                arec["formal"]["dir"] = f"repair/attempt-{idx}/formal"
            verdict = arec["verdict"]

            def settle_attempt(r: dict) -> None:
                r["repairs"].append(arec)
                if chosen == "reference":
                    r["harness_validation"] = {"attempt": idx, "passed": verdict == "verified",
                                               "summary": "reference fix passes every trusted test" if verdict == "verified"
                                               else "reference fix does NOT pass: the harness or the reference is wrong"}
                    return
                for f in r["findings"]:
                    if f["status"] == "patch_proposed":
                        if verdict == "verified":
                            self._advance(f, "repaired_verified", f"attempt {idx}: all trusted tests pass"
                                          + (f"; formal {fres.status}" if fres else ""),
                                          {"report": f"repair/attempt-{idx}/verify/report.json"})
                        else:
                            self._advance(f, "repair_failed", f"attempt {idx}: {verdict}")

            await self._record(audit_id, settle_attempt)
            if verdict == "verified":
                fsum = f"; formal {fres.status}" if fres else ""
                await ctx.stage("retest", "passed", f"attempt {n}: all {len(vrep.tests)} trusted tests pass{fsum}",
                                detail={"report": f"repair/attempt-{idx}/verify/report.json"})
                break
            msg = ("patch touched files outside the allowlist: " + "; ".join(changes["scope_violations"])) if vrep is None \
                else f"attempt {n}: {vrep.message}" + (f"; formal {fres.status}" if fres else "")
            await ctx.stage("retest", "failed", msg)
            if vrep is not None:
                evidence = rep.failing_evidence_text(case, vrep.model_dump(), fres.model_dump() if fres else None)

        for st in store.get_run(ctx.run_id)["stages"]:
            if st["key"] in ("repair", "retest") and st["status"] in ("pending", "running"):
                await ctx.stage(st["key"], "skipped", "no patch to retest")
        await self._evidence(ctx, audit_id, case)
        if chosen == "reference":
            hv = store.get_security_audit(audit_id)["record"].get("harness_validation") or {}
            await ctx.say("Harness validation with the **human reference fix**: "
                          + (hv.get("summary") or "no result") + ". This is not an agent repair.")
            await self._finish_audit(ctx, audit_id, "reproduced", "Vulnerability reproduced; harness validated with the reference fix "
                                     "(not an agent repair)." if hv.get("passed") else "Vulnerability reproduced; reference fix failed validation.")
        elif verdict == "verified":
            await ctx.say(f"**Repaired and verified**: {label}'s patch passes every trusted security and functional test. "
                          "Review the diff before using it; programming the board still needs your approval.")
            await self._finish_audit(ctx, audit_id, "repaired_verified", f"Patch by {label} passed every trusted test.")
        else:
            await ctx.say(f"Repair by {label} did not pass the trusted verifier ({verdict}). The vulnerability stays reproduced.")
            await self._finish_audit(ctx, audit_id, "repair_failed", f"{label}: no patch passed the trusted verifier.")

    def _choose_agent(self, agent: str) -> tuple[str | None, tuple[str, Any] | None, str]:
        if agent == "none":
            return None, None, "automatic repair was not requested"
        if agent == "reference":
            return "reference", None, ""
        droid = rep.droid_status()
        if agent in ("auto", "factory"):
            if droid["state"] != "unavailable":
                return "factory", None, ""
            if agent == "factory":
                return None, None, f"Factory Droid unavailable: {droid['reason']}"
        name, prov, note = inv.pick_provider(get_settings().security_repair_model)
        if prov is not None and agent in ("auto", "model"):
            return "model", (name, prov), ""
        reasons = [f"Factory Droid: {droid['reason']}"] if agent == "auto" else []
        reasons.append("no model provider configured (set BITDEER_API_KEY or ANTHROPIC_API_KEY)")
        return None, None, "; ".join(reasons)

    # ================================================================ evidence
    async def _evidence(self, ctx: RunContext, audit_id: str, case: CaseManifest) -> None:
        await ctx.stage("evidence", "running", "hashing artifacts and writing the evidence record")
        audit = store.get_security_audit(audit_id)
        root = Path(audit["dir"])
        artifacts = []
        for p in sorted(root.rglob("*")):
            parts = p.relative_to(root).parts
            if not p.is_file() or "obj" in parts or any(x in rep.AGENT_METADATA_DIRS for x in parts):
                continue
            artifacts.append({"path": _rel(p, root), "bytes": p.stat().st_size, "sha256": sha256_file(p)})
        ev = {"generated_at": now(), "artifacts": artifacts, "case_trusted_hashes": case.trusted_hashes(),
              "audit_id": audit_id, "run_id": ctx.run_id}

        def put(r: dict) -> None:
            r["evidence"] = ev
        audit = await self._record(audit_id, put)
        (root / "audit.json").write_text(json.dumps(self.public(audit), indent=2, default=str))
        (root / "report.md").write_text(render_report(audit))
        await ctx.stage("evidence", "passed", f"{len(artifacts)} artifacts hashed; report.md and audit.json written",
                        detail={"artifacts": len(artifacts)})

    async def _finish_audit(self, ctx: RunContext, audit_id: str, status: str, summary: str) -> None:
        def put(r: dict) -> None:
            r["outcome"] = {"status": status, "summary": summary}
        await self._record(audit_id, put)
        audit = await self._set(audit_id, status=status, finished_at=now())
        root = Path(audit["dir"])
        (root / "audit.json").write_text(json.dumps(self.public(audit), indent=2, default=str))
        (root / "report.md").write_text(render_report(audit))
        await ctx.set_status(status, finished=True)


def render_report(audit: dict) -> str:
    r = audit["record"]
    case = r.get("case") or {}
    lines = [f"# SiliconBreak X audit {audit['id']}", "",
             f"- Case: **{case.get('title')}** (`{case.get('case_id')}`, {case.get('source_kind')})",
             f"- Provenance: {case.get('provenance')}",
             f"- Outcome: **{(r.get('outcome') or {}).get('status')}**: {(r.get('outcome') or {}).get('summary', '')}",
             f"- Tools: " + ", ".join(f"{k} {v}" for k, v in (r.get("tool_versions") or {}).items()), "",
             "## Security contract", (case.get("security_contract") or {}).get("summary", "")]
    for p in (case.get("security_contract") or {}).get("properties", []):
        lines.append(f"- **{p['id']}** {p['text']}")
    inv_ = r.get("investigation") or {}
    lines += ["", "## Investigation",
              f"Provider: {inv_.get('provider')} ({inv_.get('model')}){'' if inv_.get('provider') != 'rules' else ' - offline static rules, not AI'}"]
    for f in r.get("findings", []):
        lines.append(f"- [{f['status']}] {f['title']} - {f['file']}:{f['line_start']}-{f['line_end']} "
                     f"({', '.join(f['violated_properties'])}; {f.get('weakness_class', '')})")
    rp = r.get("reproduction") or {}
    lines += ["", "## Independent reproduction (trusted verifier)", f"{rp.get('status')}: {rp.get('message', '')}"]
    for t in rp.get("tests", []):
        lines.append(f"- {t['status'].upper():7s} {t['id']} ({t['kind']}) {t.get('message', '')}")
    fo = r.get("formal_original") or {}
    lines += ["", "## Formal check (original)", f"{fo.get('status')}: {fo.get('message', '')}"]
    for a in r.get("repairs", []):
        v = a.get("verification") or {}
        lines += ["", f"## Repair attempt {a.get('attempt')}: {a.get('label')}",
                  f"Agent status: {a.get('status')}; verdict: **{a.get('verdict')}**; verifier: {v.get('status')} {v.get('message', '')}",
                  f"Formal: {(a.get('formal') or {}).get('status')}", "", "```diff", (a.get("diff") or "(no diff)").rstrip(), "```"]
    if r.get("harness_validation"):
        lines += ["", "## Harness validation", r["harness_validation"]["summary"] + " (human reference fix, not an agent repair)"]
    lines += ["", "## Limitations",
              "- Results cover only the written contract and the trusted tests/properties of this case.",
              "- A formal proof covers the wrapper's assertions under its assumptions, not the whole design or chip.",
              "- Findings from offline static rules are lexical pattern matches, not AI analysis."]
    return "\n".join(lines) + "\n"


security_orchestrator = SecurityOrchestrator()
