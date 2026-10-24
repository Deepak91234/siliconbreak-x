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
import hashlib
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
from backend.security import fpga
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
HW_BUILD_STAGES: list[tuple[str, str]] = [
    ("board_sim", "Board-level simulation"),
    ("synth", "Vivado synthesis"),
    ("impl", "Implementation"),
    ("timing", "Timing validation"),
    ("bitstream", "Bitstream"),
]
HW_PROGRAM_STAGES: list[tuple[str, str]] = [("detect", "FPGA detection"), ("program", "Programming")]
REPAIR_AGENTS = ("auto", "factory", "model", "reference", "none")


class AuditError(Exception):
    pass


# Used for repository and CircuitLoop-project audits, which have no case-specific contract.
GENERIC_CONTRACT = {
    "summary": "Generic hardware-security contract (no case-specific contract or trusted tests exist for these files).",
    "properties": [
        {"id": "G-ACCESS", "text": "Protected registers, memories and secrets are readable or writable only with the required privilege or authorization."},
        {"id": "G-DEBUG", "text": "Debug, test and scan interfaces cannot bypass access control or life-cycle gating."},
        {"id": "G-LOCK", "text": "Lock bits stay set until reset and locked registers ignore writes."},
        {"id": "G-ZEROIZE", "text": "Secret material is fully cleared on zeroize, reset or a security state change."},
        {"id": "G-RESET", "text": "Security-relevant state has a defined, safe value after reset."},
        {"id": "G-FSM", "text": "Security state machines cannot reach undefined states or skip required checks."},
    ],
}
FILESET_LIMITS = ("No trusted tests or properties exist for these files, so no finding can be reproduced here; every finding "
                  "stays suspected. Only the listed files were read; the rest of the repository was not audited.")


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

    # ------------------------------------------------------------ hardware
    def fpga_variants(self, audit: dict) -> list[dict]:
        """Buildable variants: the vulnerable source and every verifier-accepted repair attempt."""
        case = get_case(audit.get("case_id") or "")
        if case is None or case.board is None:
            return []
        out = [{"variant": "vulnerable", "label": "vulnerable (case source)", "kind": "vulnerable"}]
        for a in audit["record"].get("repairs", []):
            if a.get("verdict") == "verified":
                kind = "reference" if a.get("agent") == "reference" else "agent"
                label = (f"human reference fix (harness validation, attempt {a['attempt']})" if kind == "reference"
                         else f"patched by {a.get('label')} (attempt {a['attempt']}, verifier-accepted)")
                out.append({"variant": f"attempt-{a['attempt']}", "label": label, "kind": kind})
        return out

    def _variant_sources(self, audit: dict, case: CaseManifest, variant: str) -> tuple[dict, dict[str, Path]]:
        info = next((v for v in self.fpga_variants(audit) if v["variant"] == variant), None)
        if info is None:
            raise AuditError(f"variant {variant!r} is not buildable (only the vulnerable source and verifier-accepted patches are)")
        if variant == "vulnerable":
            return info, {rel: case.path(rel) for rel in case.editable}
        ws = Path(audit["dir"]) / "repair" / variant / "workspace"
        cands = {rel: ws / rel for rel in case.editable}
        for rel, p in cands.items():
            if not p.is_file() or p.is_symlink():
                raise AuditError(f"{variant}: {rel} is missing")
        # the bytes must still be the ones the verifier accepted
        att = next(a for a in audit["record"]["repairs"] if f"attempt-{a['attempt']}" == variant)
        want = ((att.get("verification") or {}).get("source_hashes") or {})
        for rel, p in cands.items():
            if want.get(rel) and sha256_file(p) != want[rel]:
                raise AuditError(f"{variant}: {rel} changed after verification")
        return info, cands

    async def start_fpga_build(self, audit_id: str, variant: str) -> dict:
        audit = store.get_security_audit(audit_id)
        if audit is None:
            raise KeyError(audit_id)
        case = get_case(audit.get("case_id") or "")
        if case is None or case.board is None:
            raise AuditError("this case has no trusted Basys 3 wrapper")
        info, cands = self._variant_sources(audit, case, variant)
        text = f"Basys 3 build: {info['label']}"
        run = store.create_run(audit["project_id"], trigger="security_fpga_build", request_text=text,
                               options={"audit_id": audit_id, "variant": variant}, stages=HW_BUILD_STAGES)
        await bus.emit("run.created", run_id=run["id"], project_id=audit["project_id"], run=run)
        ctx = RunContext(run["id"], audit["project_id"], {"variant": variant})
        self._launch(ctx, audit_id, self._fpga_build_run(ctx, audit_id, case, info, cands), affects_audit=False)
        return {"run": run, "variant": info}

    async def start_fpga_program(self, audit_id: str, variant: str) -> dict:
        audit = store.get_security_audit(audit_id)
        if audit is None:
            raise KeyError(audit_id)
        build = (((audit["record"].get("fpga") or {}).get(variant) or {}).get("build") or {})
        if not build.get("ok") or not fpga.verify_bitstream_file(build.get("bitstream_path"), build.get("bitstream_sha256")):
            raise AuditError("no valid bitstream for this variant; build it first")
        info = next((v for v in self.fpga_variants(audit) if v["variant"] == variant), None) or {"label": variant}
        text = f"Program the Basys 3: {info['label']}"
        run = store.create_run(audit["project_id"], trigger="security_fpga_program", request_text=text,
                               options={"audit_id": audit_id, "variant": variant}, stages=HW_PROGRAM_STAGES)
        await bus.emit("run.created", run_id=run["id"], project_id=audit["project_id"], run=run)
        ctx = RunContext(run["id"], audit["project_id"], {"variant": variant})
        self._launch(ctx, audit_id, self._fpga_program_run(ctx, audit_id, variant, info, build), affects_audit=False)
        return {"run": run}

    async def _record_fpga(self, audit_id: str, variant: str, **fields) -> None:
        def put(r: dict) -> None:
            r.setdefault("fpga", {}).setdefault(variant, {}).update(fields)
        await self._record(audit_id, put)

    async def _fpga_build_run(self, ctx: RunContext, audit_id: str, case: CaseManifest, info: dict,
                              cands: dict[str, Path]) -> None:
        from backend.vivado import build as vb

        variant = info["variant"]
        root = Path(store.get_security_audit(audit_id)["dir"]) / "fpga" / variant
        await self._record_fpga(audit_id, variant, label=info["label"], kind=info["kind"], build=None,
                                source_hashes={rel: sha256_file(p) for rel, p in cands.items()})

        # ---- board-level simulation of the demo sequence
        await ctx.stage("board_sim", "running", "simulating the demo sequence on the full Basys 3 top (Verilator)")

        async def slog(line: str) -> None:
            await ctx.log("board_sim", "verilator", line)

        sim = await fpga.board_sim(case, cands, root / "board_sim", on_line=slog)
        sim["dir"] = f"fpga/{variant}/board_sim"
        await self._record_fpga(audit_id, variant, board_sim=sim)
        if sim["status"] in ("error", "not_run"):
            await ctx.stage("board_sim", "failed" if sim["status"] == "error" else "unavailable", sim["message"])
            for k in ("synth", "impl", "timing", "bitstream"):
                await ctx.stage(k, "skipped", "board-level simulation did not run")
            await ctx.set_status("failed", error=sim["message"], finished=True)
            return
        bad = [st for st in sim["steps"] if not st["matches_contract"]]
        if bad:
            await ctx.stage("board_sim", "failed", "; ".join(f"{st['id']}: LEDs {st['simulated']}, contract requires {st['expected']}"
                                                             for st in bad)[:200], detail={"steps": sim["steps"]})
            await ctx.say(f"Board-level simulation ({info['label']}): " + "; ".join(
                f"{st['id']} {st['title']}: LEDs {st['simulated']} where the contract requires {st['expected']}" for st in bad)
                + ". On the board this shows up on the LEDs. (Simulation, not a hardware observation.)")
        else:
            await ctx.stage("board_sim", "passed", f"all {len(sim['steps'])} demo steps match the contract",
                            detail={"steps": sim["steps"]})
        ctx.check_cancel()

        # ---- Vivado
        await ctx.stage("synth", "running", "checking Vivado environment")
        if not await design_orchestrator._ensure_vivado(ctx):
            for k in ("synth", "impl", "timing", "bitstream"):
                await ctx.stage(k, "unavailable", "Vivado environment not available")
            await ctx.say("Vivado is not available, so no bitstream was built. Start your vivado_container (or approve the "
                          "headless container) and build again.", kind="result")
            await ctx.set_status("failed", error="Vivado unavailable", finished=True)
            return
        while vb.is_busy():
            await ctx.stage("synth", "blocked", "waiting for another Vivado build to finish")
            await asyncio.sleep(3)
            ctx.check_cancel()
        srcs = fpga.board_sources(case, cands, root / "vivado_src")
        xdc = fpga.write_xdc(root / "vivado_src")
        stage_state: dict[str, str] = {}
        current = {"key": "synth"}

        async def on_stage(vstage: str, status: str) -> None:
            key = {"read": "synth", "synth": "synth", "opt": "impl", "place": "impl", "route": "impl",
                   "reports": "timing", "bitstream": "bitstream"}.get(vstage)
            if key is None:
                return
            current["key"] = key
            if status == "running":
                await ctx.stage(key, "running", f"vivado {vstage}")
            elif status == "failed":
                await ctx.stage(key, "failed", f"vivado {vstage} failed")
            elif status == "passed" and vstage in ("synth", "route"):
                stage_state[key] = "passed"
                await ctx.stage(key, "passed", f"vivado {vstage} done")

        async def vlog(line: str) -> None:
            await ctx.log(current["key"], "vivado", line)

        try:
            res = await vb.run_build(fpga.build_id_for(audit_id, variant), srcs, xdc, root / "vivado",
                                     on_line=vlog, on_stage=on_stage, cancel=ctx.cancel)
        except vb.VivadoBusy as e:
            for k in ("synth", "impl", "timing", "bitstream"):
                await ctx.stage(k, "failed", str(e))
            await ctx.set_status("failed", error=str(e), finished=True)
            return
        if res.status == "cancelled":
            raise RunCancelled()
        info_b = fpga.bitstream_info(res)
        await self._record_fpga(audit_id, variant, build=info_b)
        if res.status == "unavailable":
            for k in ("synth", "impl", "timing", "bitstream"):
                await ctx.stage(k, "unavailable", res.message[:200])
            await ctx.set_status("failed", error=res.message, finished=True)
            return
        checks = {c["name"]: c for c in res.checks}
        for key in ("synth", "impl"):
            if stage_state.get(key) != "passed":
                await ctx.stage(key, "failed" if not res.ok else "passed", res.message[:200] if not res.ok else "done")
        t = res.timing or {}
        t_ok = checks.get("timing_met", {}).get("passed") and checks.get("clock_constrained", {}).get("passed", False)
        await ctx.stage("timing", ("passed" if t_ok else "failed") if t else "skipped",
                        f"WNS {t.get('wns_ns')} ns, WHS {t.get('whs_ns')} ns" if t else "no timing report")
        bit_ok = checks.get("bitstream_valid", {}).get("passed", False) and res.ok
        await ctx.stage("bitstream", "passed" if bit_ok else "failed",
                        (f"{info_b['bitstream_bytes']} bytes, sha256 {res.bitstream_sha256[:12]}..." if bit_ok
                         else checks.get("bitstream_valid", {}).get("detail", res.message))[:200],
                        detail={"sha256": res.bitstream_sha256, "path": res.bitstream_path})
        await ctx.say(f"Basys 3 bitstream for {info['label']}: " + ("built" if bit_ok else "build failed")
                      + (f" (WNS {t.get('wns_ns')} ns). Programming needs your approval." if bit_ok else f": {res.message}"),
                      kind="result" if bit_ok else "error")
        await ctx.set_status("passed" if bit_ok else "failed", finished=True)

    async def _fpga_program_run(self, ctx: RunContext, audit_id: str, variant: str, info: dict, build: dict) -> None:
        from backend.agent import approvals
        from backend.programmer.manager import hardware

        bit, sha = Path(build["bitstream_path"]), build["bitstream_sha256"]
        await ctx.stage("detect", "running", "looking for the Basys 3")
        det = await hardware.detect()
        if det.state != "connected":
            await ctx.stage("detect", "unavailable" if det.state in ("no_board", "tool_missing") else "failed",
                            f"{det.state}: {det.message}"[:200], det.model_dump())
            await ctx.stage("program", "skipped", "no board available")
            await ctx.say(f"Basys 3 not available ({det.state}); nothing was programmed.", kind="result")
            await ctx.set_status("failed", error=f"board {det.state}", finished=True)
            return
        await ctx.stage("detect", "passed", f"{det.device or 'xc7a35t'} idcode {det.idcode} via {det.adapter}", det.model_dump())
        if not fpga.verify_bitstream_file(str(bit), sha):
            await ctx.stage("program", "failed", "bitstream missing or hash mismatch")
            await ctx.set_status("failed", error="bitstream integrity check failed", finished=True)
            return
        appr = await approvals.request(
            "program_fpga", f"Program the Basys 3: {info['label']}",
            "Load this security-demo bitstream into the FPGA's configuration SRAM (volatile: the flash is not written; "
            "power-cycling restores the previous design).",
            {"bitstream_sha256": sha, "bitstream_path": str(bit), "audit_id": audit_id, "variant": variant, "volatile": True},
            run_id=ctx.run_id, project_id=ctx.project_id)
        await ctx.stage("program", "blocked", "waiting for your approval")
        await ctx.set_status("waiting")
        decision = await approvals.wait(appr["id"], ctx.cancel)
        ctx.check_cancel()
        await ctx.set_status("running")
        if decision != "approved":
            await ctx.stage("program", "skipped", "programming declined")
            await ctx.set_status("cancelled", finished=True)
            return
        await ctx.stage("program", "running", "programming SRAM")
        res = await hardware.program(bit, sha, approval_id=appr["id"])
        approvals.mark_result(appr["id"], res.ok, res.model_dump())
        await bus.emit("approval.resolved", run_id=ctx.run_id, project_id=ctx.project_id, approval=store.get_approval(appr["id"]))
        for line in (res.raw or "").splitlines()[-40:]:
            await ctx.log("program", res.adapter, line)
        entry = {"at": now(), "ok": res.ok, "adapter": res.adapter, "state": res.state, "message": res.message,
                 "bitstream_sha256": sha}

        def put(r: dict) -> None:
            v = r.setdefault("fpga", {}).setdefault(variant, {})
            v.setdefault("programs", []).append(entry)
            r["fpga"]["loaded"] = {"variant": variant, "at": entry["at"]} if res.ok else r["fpga"].get("loaded")
        await self._record(audit_id, put)
        await ctx.stage("program", "passed" if res.ok else "failed", f"{res.adapter}: {res.message}"[:200], res.model_dump())
        await ctx.say(("Programmed the Basys 3 with " + info["label"] + ". Run the demo steps on the board; the LED "
                       "patterns the contract requires are listed in the Security tab.") if res.ok
                      else f"Programming failed ({res.adapter}): {res.message}", kind="result" if res.ok else "error")
        await ctx.set_status("passed" if res.ok else "failed", finished=True)

    # -------------------------------------------- repository / project audits
    async def start_repo_audit(self, repo_id: str, files: list[str] | None, *, investigator: str | None = None) -> dict:
        from backend.security import repo_index as ri

        idx = ri.load_index(repo_id)
        if idx is None:
            raise AuditError("unknown repository; import it first")
        try:
            sel = ri.select_for_audit(idx, files)
        except ri.RepoError as e:
            raise AuditError(str(e))
        if not sel:
            raise AuditError("no security-relevant files were found to audit; select files explicitly")
        texts = {rel: ri.read_file(repo_id, rel) for rel in sel}
        label = f"{idx['owner']}/{idx['name']}@{idx['sha'][:7]}" + (f" ({idx['subdir']})" if idx["subdir"] else "")
        meta = {"kind": "repository", "repo_id": repo_id, "url": idx["url"], "sha": idx["sha"], "ref": idx["ref"],
                "subdir": idx["subdir"], "indexed_files": idx["totals"]["files"], "indexed_modules": idx["totals"]["modules"],
                "audited_files": sel, "truncated": idx["truncated"],
                "selection": "explicit" if files else "highest security-relevance score within the context budget"}
        project_id = self._project_titled(f"Security audit: {label}")
        return await self._start_fileset(project_id, "repository", label, texts, meta, investigator,
                                         repo_url=idx["url"], commit_sha=idx["sha"])

    async def start_project_audit(self, project_id: str, revision_id: str | None, *, investigator: str | None = None) -> dict:
        project = store.get_project(project_id)
        if project is None:
            raise AuditError("unknown project")
        rev = store.get_revision(revision_id) if revision_id else (
            store.get_revision(project["current_revision_id"]) if project.get("current_revision_id") else None)
        if rev is None or rev["project_id"] != project_id:
            raise AuditError("the project has no revision to audit")
        rtl = Path(rev["dir"]) / "rtl"
        texts = {f"rtl/{n}": (rtl / n).read_text() for n in ("cl_core.v", "basys3_top.v") if (rtl / n).is_file()}
        if not texts:
            raise AuditError("the revision has no RTL files")
        label = f"{project['title']} r{rev['number']}"
        meta = {"kind": "project", "project_id": project_id, "revision_id": rev["id"], "revision_number": rev["number"],
                "audited_files": sorted(texts), "indexed_files": len(texts), "selection": "the revision's generated RTL"}
        return await self._start_fileset(project_id, "project", label, texts, meta, investigator)

    def _project_titled(self, title: str) -> str:
        for p in store.list_projects():
            if p["title"] == title:
                return p["id"]
        return store.create_project(title)["id"]

    async def _start_fileset(self, project_id: str, kind: str, label: str, texts: dict[str, str], meta: dict,
                             investigator: str | None, *, repo_url: str | None = None, commit_sha: str | None = None) -> dict:
        opts = {"investigator": investigator or "auto", "repair_agent": "none"}
        audit = store.create_security_audit(project_id=project_id, source_kind=kind, case_id=None, repo_url=repo_url,
                                            commit_sha=commit_sha, options=opts)
        root = Path(audit["dir"])
        for rel, txt in texts.items():
            dst = root / "sources" / rel
            if not dst.resolve().is_relative_to((root / "sources").resolve()):
                raise AuditError(f"unsafe path {rel!r}")
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(txt)
        pseudo_case = {
            "case_id": None, "title": label, "source_kind": kind,
            "provenance": (f"{meta.get('url')} at pinned commit {commit_sha}. " if kind == "repository" else
                           "RTL generated by CircuitLoop for this project. ") + FILESET_LIMITS,
            "cwe": [], "repository": repo_url, "commit": commit_sha, "license": "", "top_module": "",
            "editable": [], "description": "", "interface": [], "security_contract": GENERIC_CONTRACT,
            "functional_contract": [], "tests": [], "formal": {"available": False, "properties": []},
            "has_reference_fix": False, "board": None,
        }
        record = {**self._initial_record_from(pseudo_case, opts), "fileset": meta,
                  "source": {"kind": kind, "label": label,
                             "files": {rel: hashlib.sha256(t.encode()).hexdigest() for rel, t in texts.items()}}}
        text = f"Security audit: {label}"
        run = store.create_run(project_id, trigger="security_audit", request_text=text, options={"audit_id": audit["id"], **opts},
                               stages=SECURITY_STAGES)
        audit = store.update_security_audit(audit["id"], run_id=run["id"], status="running", record=record)
        await self._emit_audit(audit)
        await bus.emit("run.created", run_id=run["id"], project_id=project_id, run=run)
        ctx = RunContext(run["id"], project_id, opts)
        msg = store.add_message(project_id, "user", text, run_id=run["id"])
        await bus.emit("agent.message", run_id=run["id"], project_id=project_id, message=msg)
        self._launch(ctx, audit["id"], self._fileset_run(ctx, audit["id"], texts, meta))
        return {"audit": self.public(store.get_security_audit(audit["id"])), "run": run, "project": store.get_project(project_id)}

    def _initial_record_from(self, pub: dict, opts: dict) -> dict:
        return {"case": pub, "options": opts, "providers": {}, "investigation": None, "findings": [],
                "unexplained_failures": [], "reproduction": None, "formal_original": None, "repairs": [],
                "harness_validation": None, "outcome": {"status": "running", "summary": ""}, "tool_versions": {},
                "evidence": None}

    async def _fileset_run(self, ctx: RunContext, audit_id: str, texts: dict[str, str], meta: dict) -> None:
        audit = store.get_security_audit(audit_id)
        root = Path(audit["dir"])
        await ctx.stage("contract", "passed", "generic hardware-security contract (no case-specific contract or trusted tests)",
                        detail={"summary": GENERIC_CONTRACT["summary"]})
        lines = sum(t.count("\n") + 1 for t in texts.values())
        mods = sorted({m for t in texts.values() for m in re.findall(r"^\s*module\s+(\w+)", t, re.M)})
        await ctx.stage("index", "passed", f"{meta['indexed_files']} file(s) indexed; auditing {len(texts)} "
                                           f"({lines} lines): {meta['selection']}",
                        detail={"audited": sorted(texts), "modules": mods, "indexed": meta["indexed_files"]})
        await ctx.say(f"Auditing {len(texts)} file(s) of **{store.get_security_audit(audit_id)['record']['case']['title']}** "
                      f"against a generic hardware-security contract. {FILESET_LIMITS}")
        ctx.check_cancel()

        name, provider, note = inv.pick_provider(audit["options"].get("investigator"))
        label = {"bitdeer": "Bitdeer", "anthropic": "Claude", "rules": "offline static rules (not AI)"}[name]
        await ctx.stage("investigate", "running", f"investigating with {label}" + (f" ({provider.model})" if provider else ""))
        extra = ("CONTEXT\n  These files come from " + (f"{meta['url']} at commit {meta['sha']}" if meta["kind"] == "repository"
                 else "a CircuitLoop-generated design") + ". Other files of the design may exist but were not provided.")
        irec = await inv.investigate(GENERIC_CONTRACT, [], texts, root / "investigate", provider_override=(name, provider, note),
                                     extra_context=extra)
        used = irec if irec["status"] == "ok" else (irec.get("fallback") or irec)
        findings = []
        for f in used.get("findings", []):
            f = {**f, "provider": used.get("provider"), "model": used.get("model"), "is_ai": used.get("is_ai", False)}
            if meta["kind"] == "repository":
                f["permalink"] = f"{meta['url']}/blob/{meta['sha']}/{f['file']}#L{f['line_start']}-L{f['line_end']}"
            self._advance(f, "suspected", f"proposed by {used.get('provider')}; no trusted test exists to reproduce it")
            findings.append(f)
        await self._record(audit_id, lambda r: r.update(investigation=irec, findings=findings, providers={
            "investigator": {"provider": name, "model": irec.get("model"), "is_ai": provider is not None, "note": note}}))
        summary = f"{label}: {len(findings)} suspected finding(s)"
        if irec["status"] != "ok":
            summary = f"{label} failed ({(irec.get('error') or {}).get('message', 'error')}); offline rules found {len(findings)}"
        await ctx.stage("investigate", "passed" if irec["status"] == "ok" else "inconclusive", summary,
                        detail={"provider": name, "findings": len(findings)})
        why = "no trusted security test or property exists for these files"
        await ctx.stage("reproduce", "skipped", f"not run: {why}")
        await ctx.stage("formal", "skipped", f"not run: {why}")
        await ctx.stage("repair", "skipped", "repairs need a reproduced violation")
        await ctx.stage("retest", "skipped", "nothing to retest")
        if findings:
            await ctx.say(f"{len(findings)} **suspected** finding(s) ({label}); none is reproduced, because no trusted test "
                          "exists for these files. Treat them as leads for a case-specific contract and test.")
        else:
            await ctx.say(f"The investigation ({label}) reported no suspected weakness in the audited files. "
                          "This is not evidence that the files are secure.")
        await self._evidence_generic(ctx, audit_id)
        await self._finish_audit(ctx, audit_id, "suspected_only" if findings else "no_findings",
                                 f"{len(findings)} suspected finding(s); not reproduced (no trusted tests for these files)."
                                 if findings else "No suspected weakness reported in the audited files (not a proof of security).")

    async def _evidence_generic(self, ctx: RunContext, audit_id: str) -> None:
        await ctx.stage("evidence", "running", "hashing artifacts")
        audit = store.get_security_audit(audit_id)
        root = Path(audit["dir"])
        artifacts = [{"path": _rel(p, root), "bytes": p.stat().st_size, "sha256": sha256_file(p)}
                     for p in sorted(root.rglob("*")) if p.is_file()]
        ev = {"generated_at": now(), "artifacts": artifacts, "audit_id": audit_id, "run_id": ctx.run_id}
        audit = await self._record(audit_id, lambda r: r.update(evidence=ev))
        (root / "audit.json").write_text(json.dumps(self.public(audit), indent=2, default=str))
        (root / "report.md").write_text(render_report(audit))
        await ctx.stage("evidence", "passed", f"{len(artifacts)} artifacts hashed; report.md and audit.json written")

    def _launch(self, ctx: RunContext, audit_id: str, coro, *, affects_audit: bool = True) -> None:
        design_orchestrator.contexts[ctx.run_id] = ctx  # lets POST /api/runs/{id}/cancel stop audits too
        task = asyncio.create_task(self._guard(ctx, audit_id, coro, affects_audit))
        # hardware runs are keyed apart so a build waiting on Vivado does not block repairs
        self.tasks[f"{audit_id}:{ctx.run_id}" if affects_audit else f"hw:{audit_id}:{ctx.run_id}"] = task

    async def _guard(self, ctx: RunContext, audit_id: str, coro, affects_audit: bool = True) -> None:
        try:
            await coro
        except RunCancelled:
            await self._finish_stages(ctx, "cancelled", "cancelled by user")
            await ctx.set_status("cancelled", finished=True)
            if affects_audit:
                await self._set(audit_id, status="cancelled", finished_at=now())
        except Exception as e:
            log.exception("security run %s crashed", ctx.run_id)
            await self._finish_stages(ctx, "failed", f"internal error: {type(e).__name__}")
            await ctx.set_status("failed", error=f"{type(e).__name__}: {e}", finished=True)
            if affects_audit:
                await self._set(audit_id, status="failed", finished_at=now())
            await ctx.say(f"Internal error: {type(e).__name__}: {e}", kind="error",
                          data={"traceback": traceback.format_exc()[-4000:]})
        finally:
            design_orchestrator.contexts.pop(ctx.run_id, None)
            self.tasks.pop(f"{audit_id}:{ctx.run_id}", None)
            self.tasks.pop(f"hw:{audit_id}:{ctx.run_id}", None)

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
        # automatic repairs use Droid only when it is explicitly configured (FACTORY_API_KEY); a CLI that
        # may merely be logged in runs only when the user asks for it by name
        if agent == "factory" and droid["state"] != "unavailable":
            return "factory", None, ""
        if agent == "auto" and droid["state"] == "configured":
            return "factory", None, ""
        if agent == "factory":
            return None, None, f"Factory Droid unavailable: {droid['reason']}"
        name, prov, note = inv.pick_provider(get_settings().security_repair_model)
        if prov is not None and agent in ("auto", "model"):
            return "model", (name, prov), ""
        reasons = [f"Factory Droid: {droid['reason'] or 'not configured for automatic repairs'}"
                   + (" (use 'Repair with Factory Droid' to run it on request)" if droid["state"] == "unverified" else "")] \
            if agent == "auto" else []
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
    fs = r.get("fileset")
    lines = [f"# SiliconBreak X audit {audit['id']}", "",
             f"- Case: **{case.get('title')}** (`{case.get('case_id')}`, {case.get('source_kind')})",
             f"- Provenance: {case.get('provenance')}",
             f"- Outcome: **{(r.get('outcome') or {}).get('status')}**: {(r.get('outcome') or {}).get('summary', '')}",
             f"- Tools: " + ", ".join(f"{k} {v}" for k, v in (r.get("tool_versions") or {}).items()), "",
             "## Security contract", (case.get("security_contract") or {}).get("summary", "")]
    if fs:
        lines[6:6] = [f"- Audited files ({len(fs.get('audited_files', []))} of {fs.get('indexed_files')} indexed): "
                      + ", ".join(fs.get("audited_files", [])), f"- Selection: {fs.get('selection')}", ""]
    for p in (case.get("security_contract") or {}).get("properties", []):
        lines.append(f"- **{p['id']}** {p['text']}")
    inv_ = r.get("investigation") or {}
    lines += ["", "## Investigation",
              f"Provider: {inv_.get('provider')} ({inv_.get('model')}){'' if inv_.get('provider') != 'rules' else ' - offline static rules, not AI'}"]
    for f in r.get("findings", []):
        lines.append(f"- [{f['status']}] {f['title']} - {f['file']}:{f['line_start']}-{f['line_end']} "
                     f"({', '.join(f['violated_properties'])}; {f.get('weakness_class', '')})"
                     + (f" {f['permalink']}" if f.get("permalink") else ""))
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
