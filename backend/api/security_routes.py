"""SiliconBreak X REST endpoints (/api/security/*, /api/bench/*).

Live progress uses the existing /ws/events stream: audits are runs, and every
change to an audit record is also published as a `security.updated` event.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi import Path as Path_
from pydantic import BaseModel

from backend.core.process import PathOutsideWorkspace, ensure_within
from backend.security import health
from backend.security.cases import get_case, list_cases
from backend.security.orchestrator import AuditError, security_orchestrator as so
from backend.storage import store

router = APIRouter(prefix="/api")

MAX_TEXT_BYTES = 2 * 1024 * 1024
TEXT_SUFFIXES = {".v", ".sv", ".vh", ".svh", ".diff", ".log", ".md", ".json", ".jsonl", ".txt"}


class AuditRequest(BaseModel):
    """Exactly one source: a security case, an imported repository, or a CircuitLoop project."""
    case_id: Optional[str] = None
    repo_id: Optional[str] = None
    files: Optional[list[str]] = None          # repository audits: explicit file selection
    project_id: Optional[str] = None
    revision_id: Optional[str] = None
    investigator: Optional[Literal["auto", "bitdeer", "anthropic", "rules"]] = None
    repair_agent: Literal["auto", "factory", "model", "reference", "none"] = "none"
    max_attempts: Optional[int] = None


class RepoImportRequest(BaseModel):
    url: str
    ref: Optional[str] = None
    subdir: Optional[str] = None


class BenchRequest(BaseModel):
    case_ids: Optional[list[str]] = None
    arms: Optional[list[str]] = None
    max_attempts: int = 3
    confirm_max_usd: Optional[float] = None       # required when paid arms are selected
    confirm_droid_sessions: Optional[int] = None


class LiveRequest(BaseModel):
    enabled: bool


class RepairRequest(BaseModel):
    agent: Literal["auto", "factory", "model", "reference"]


def _audit(audit_id: str) -> dict:
    a = store.get_security_audit(audit_id)
    if a is None:
        raise HTTPException(404, "audit not found")
    return a


def _audit_path(audit: dict, rel: str) -> Path:
    root = Path(audit["dir"])
    try:
        p = ensure_within(root / rel, root)
    except PathOutsideWorkspace:
        raise HTTPException(403, "path outside the audit directory")
    if not p.is_file():
        raise HTTPException(404, "file not found")
    return p


@router.get("/security/health")
async def security_health(refresh: bool = False) -> dict:
    return await health.collect(force=refresh)


@router.get("/security/cases")
async def security_cases() -> list[dict]:
    out = []
    for c in list_cases():
        audits = store.list_security_audits(case_id=c.case_id)
        out.append(c.public_view() | {"audits": len(audits), "latest_audit_id": audits[-1]["id"] if audits else None})
    return out


@router.get("/security/cases/{case_id}/source")
async def case_source(case_id: str) -> dict:
    c = get_case(case_id)
    if c is None:
        raise HTTPException(404, "case not found")
    return {"files": {rel: c.path(rel).read_text() for rel in c.editable}}


@router.post("/security/audits")
async def create_audit(req: AuditRequest) -> dict:
    sources = [x for x in (req.case_id, req.repo_id, req.project_id) if x]
    if len(sources) != 1:
        raise HTTPException(400, "give exactly one of case_id, repo_id or project_id")
    try:
        if req.case_id:
            return await so.start_case_audit(req.case_id, investigator=req.investigator, repair_agent=req.repair_agent,
                                             max_attempts=req.max_attempts)
        if req.repo_id:
            return await so.start_repo_audit(req.repo_id, req.files, investigator=req.investigator)
        return await so.start_project_audit(req.project_id, req.revision_id, investigator=req.investigator)
    except AuditError as e:
        raise HTTPException(400, str(e))


# ------------------------------------------------------------- repositories

@router.post("/security/repos")
async def import_repo(req: RepoImportRequest) -> dict:
    from backend.security import repo_index as ri

    try:
        idx = await ri.import_repo(req.url, req.ref, req.subdir)
    except ri.RepoError as e:
        raise HTTPException(400, str(e))
    return idx


@router.get("/security/repos")
async def list_repos() -> list[dict]:
    from backend.security import repo_index as ri

    return ri.list_repos()


@router.get("/security/repos/{repo_id}")
async def get_repo(repo_id: str) -> dict:
    from backend.security import repo_index as ri

    idx = ri.load_index(repo_id)
    if idx is None:
        raise HTTPException(404, "repository not found")
    return idx


@router.get("/security/repos/{repo_id}/file")
async def repo_file(repo_id: str, path: str = Query(..., max_length=400)) -> dict:
    from backend.security import repo_index as ri

    try:
        return {"path": path, "text": ri.read_file(repo_id, path)}
    except (ri.RepoError, PathOutsideWorkspace) as e:
        raise HTTPException(404, str(e))


@router.get("/security/audits")
async def list_audits(project_id: Optional[str] = None, case_id: Optional[str] = None) -> list[dict]:
    return [so.public(a) for a in store.list_security_audits(project_id=project_id, case_id=case_id)]


@router.get("/security/audits/{audit_id}")
async def get_audit(audit_id: str) -> dict:
    return so.public(_audit(audit_id))


@router.post("/security/audits/{audit_id}/repair")
async def repair_audit(audit_id: str, req: RepairRequest) -> dict:
    try:
        return await so.start_repair(audit_id, req.agent)
    except KeyError:
        raise HTTPException(404, "audit not found")
    except AuditError as e:
        raise HTTPException(400, str(e))


VARIANT_RE = r"^(vulnerable|attempt-[0-9]{1,3})$"


@router.get("/security/audits/{audit_id}/fpga")
async def audit_fpga(audit_id: str) -> dict:
    a = _audit(audit_id)
    c = get_case(a["case_id"] or "")
    return {"available": bool(c and c.board), "variants": so.fpga_variants(a),
            "demo": [d.model_dump() for d in (c.board.demo if c and c.board else [])],
            "mapping": (c.board.mapping if c and c.board else []), "state": a["record"].get("fpga") or {}}


@router.post("/security/audits/{audit_id}/fpga/{variant}/build")
async def fpga_build(audit_id: str, variant: str = Path_(..., pattern=VARIANT_RE)) -> dict:
    try:
        return await so.start_fpga_build(audit_id, variant)
    except KeyError:
        raise HTTPException(404, "audit not found")
    except AuditError as e:
        raise HTTPException(400, str(e))


@router.post("/security/audits/{audit_id}/fpga/{variant}/program")
async def fpga_program(audit_id: str, variant: str = Path_(..., pattern=VARIANT_RE)) -> dict:
    try:
        return await so.start_fpga_program(audit_id, variant)
    except KeyError:
        raise HTTPException(404, "audit not found")
    except AuditError as e:
        raise HTTPException(400, str(e))


@router.get("/security/audits/{audit_id}/file")
async def audit_file(audit_id: str, path: str = Query(..., max_length=300)) -> dict:
    p = _audit_path(_audit(audit_id), path)
    if p.suffix not in TEXT_SUFFIXES:
        raise HTTPException(415, "only text artifacts can be viewed")
    data = p.read_bytes()[:MAX_TEXT_BYTES]
    return {"path": path, "bytes": p.stat().st_size, "truncated": p.stat().st_size > MAX_TEXT_BYTES,
            "text": data.decode("utf-8", errors="replace")}


@router.get("/security/audits/{audit_id}/sources")
async def audit_sources(audit_id: str, attempt: Optional[int] = None) -> dict:
    """Original editable files, and the patched copy of a repair attempt."""
    a = _audit(audit_id)
    c = get_case(a["case_id"] or "")
    if c is None:
        # repository / project audits: the exact files the investigator read
        root = Path(a["dir"]) / "sources"
        files = (a["record"].get("fileset") or {}).get("audited_files", [])
        out_fs = {}
        for rel in files:
            try:
                out_fs[rel] = ensure_within(root / rel, root).read_text(errors="replace")
            except (PathOutsideWorkspace, OSError):
                continue
        return {"original": out_fs, "patched": None}
    out = {"original": {rel: c.path(rel).read_text() for rel in c.editable}, "patched": None}
    if attempt is not None:
        root = Path(a["dir"])
        ws = root / "repair" / f"attempt-{int(attempt)}" / "workspace"
        if not ws.is_dir():
            raise HTTPException(404, "attempt not found")
        patched = {}
        for rel in c.editable:
            try:
                p = ensure_within(ws / rel, root)
            except PathOutsideWorkspace:
                raise HTTPException(403, "path outside the audit directory")
            patched[rel] = p.read_text(errors="replace") if p.is_file() and not p.is_symlink() else None
        out["patched"] = patched
    return out


@router.get("/security/audits/{audit_id}/waveform")
async def audit_waveform(audit_id: str, path: str = Query(..., max_length=300), start: Optional[int] = None,
                         end: Optional[int] = None, signals: Optional[str] = None) -> dict:
    from backend.verification.waveform import VcdError, vcd_to_json

    p = _audit_path(_audit(audit_id), path)
    if p.suffix != ".vcd":
        raise HTTPException(415, "not a VCD file")
    sig = [x for x in (signals or "").split(",") if x] or None
    try:
        return vcd_to_json(p, signals=sig, start=start, end=end)
    except VcdError as e:
        raise HTTPException(422, str(e))


# ---------------------------------------------------------------- SiliconBench

@router.get("/bench/cases")
async def bench_cases() -> list[dict]:
    from backend.security.bench import ARM_LABELS

    return [c.public_view() | {"arms": ARM_LABELS} for c in list_cases()]


@router.post("/bench/estimate")
async def bench_estimate(req: BenchRequest) -> dict:
    from backend.security import bench

    try:
        return bench.estimate(req.case_ids, req.arms, req.max_attempts)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/bench/runs")
async def start_bench(req: BenchRequest) -> dict:
    from backend.security import bench

    try:
        return bench.start(req.case_ids, req.arms, req.max_attempts, confirm_max_usd=req.confirm_max_usd,
                           confirm_droid_sessions=req.confirm_droid_sessions)
    except bench.ConfirmationRequired as e:
        raise HTTPException(409, {"message": str(e), "estimate": e.estimate})
    except ValueError as e:
        raise HTTPException(400, str(e))


# ------------------------------------------------------------ AI credit guard

@router.get("/ai/budget")
async def ai_budget() -> dict:
    from backend.core import budget

    return budget.summary()


@router.post("/ai/live")
async def ai_live(req: LiveRequest) -> dict:
    """Turn live (billed) AI calls on or off. Only the browser UI or a local script can call this."""
    from backend.api import environment
    from backend.core import budget

    out = budget.set_live(req.enabled)
    await environment.collect(force=True)  # pushes environment.updated so the AI-mode chip follows the switch
    return out


@router.get("/bench/runs")
async def list_bench() -> list[dict]:
    return [{k: v for k, v in b.items() if k != "dir"} for b in store.list_bench_runs()]


@router.get("/bench/runs/{bench_id}")
async def get_bench(bench_id: str) -> dict:
    b = store.get_bench_run(bench_id)
    if b is None:
        raise HTTPException(404, "bench run not found")
    return {k: v for k, v in b.items() if k != "dir"}
