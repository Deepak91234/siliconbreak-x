"""SiliconBreak X REST endpoints (/api/security/*, /api/bench/*).

Live progress uses the existing /ws/events stream: audits are runs, and every
change to an audit record is also published as a `security.updated` event.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query
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
    case_id: str
    investigator: Optional[Literal["auto", "bitdeer", "anthropic", "rules"]] = None
    repair_agent: Literal["auto", "factory", "model", "reference", "none"] = "auto"
    max_attempts: Optional[int] = None


class BenchRequest(BaseModel):
    case_ids: Optional[list[str]] = None
    arms: Optional[list[str]] = None
    max_attempts: int = 3


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
    try:
        return await so.start_case_audit(req.case_id, investigator=req.investigator, repair_agent=req.repair_agent,
                                         max_attempts=req.max_attempts)
    except AuditError as e:
        raise HTTPException(400, str(e))


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
        raise HTTPException(404, "case not found")
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


@router.post("/bench/runs")
async def start_bench(req: BenchRequest) -> dict:
    from backend.security import bench

    try:
        return bench.start(req.case_ids, req.arms, req.max_attempts)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/bench/runs")
async def list_bench() -> list[dict]:
    return [{k: v for k, v in b.items() if k != "dir"} for b in store.list_bench_runs()]


@router.get("/bench/runs/{bench_id}")
async def get_bench(bench_id: str) -> dict:
    b = store.get_bench_run(bench_id)
    if b is None:
        raise HTTPException(404, "bench run not found")
    return {k: v for k, v in b.items() if k != "dir"}
