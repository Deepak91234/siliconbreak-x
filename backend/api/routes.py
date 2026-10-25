"""REST endpoints (docs/api.md)."""

from __future__ import annotations

import difflib
import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from backend.agent import approvals
from backend.agent.orchestrator import orchestrator
from backend.api import environment, vision_service
from backend.core.events import bus
from backend.core.process import PathOutsideWorkspace, ensure_within
from backend.storage import store

router = APIRouter(prefix="/api")

TEXT_FILES = {"cl_core.v", "basys3_top.v", "constraints.xdc"}
REPORT_FILES = {"timing_summary.rpt", "utilization_impl.rpt", "utilization_synth.rpt", "utilization_hier.rpt",
                "drc.rpt", "power.rpt", "io.rpt"}


class RunOptions(BaseModel):
    inject_fault: str | None = None
    simulation_only: bool = False


class TextRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    options: RunOptions = Field(default_factory=RunOptions)


class PidRequest(BaseModel):
    pid: int


class RoiRequest(BaseModel):
    leds: list[dict]


class AutoRoiRequest(BaseModel):
    box: tuple[float, float, float, float]
    count: int = Field(ge=1, le=16)
    first_led: int = Field(ge=0, le=15)


class PhaseRequest(BaseModel):
    action: str
    phase_id: str | None = None


class VisionCreate(BaseModel):
    revision_id: str


def _404(what: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"{what} not found")


def _project_payload(pid: str) -> dict:
    p = store.get_project(pid)
    if p is None:
        raise _404("project")
    return {
        "project": p,
        "revisions": [store.revision_summary(r) for r in store.list_revisions(pid)],
        "runs": store.list_runs(pid),
        "messages": store.list_messages(pid),
    }


# ------------------------------------------------------------------ health / environment

@router.get("/health")
async def health() -> dict:
    return {"ok": True, "version": "0.1.0"}


@router.get("/environment")
async def get_environment() -> dict:
    return await environment.collect()


@router.post("/environment/refresh")
async def refresh_environment() -> dict:
    return await environment.collect(force=True)


@router.post("/vivado/start")
async def vivado_start() -> dict:
    from backend.core.config import get_settings
    from backend.vivado import container as ctr

    env = await ctr.detect()
    if env.ready:
        raise HTTPException(409, f"Vivado is already available in {env.active_container}")
    if not env.docker_daemon:
        kind, title, desc = "start_docker", "Start Docker Desktop", "Launch Docker Desktop (open -a Docker)."
    elif env.can_start_headless:
        s = get_settings()
        kind, title = "start_vivado_container", "Start a headless Vivado container"
        desc = (f"Start '{s.vivado_headless_container}' from image '{s.vivado_image}' with your existing "
                "bind mount; no GUI, VNC or xvcd. vivado_container and your projects are untouched.")
    else:
        raise HTTPException(409, "cannot start Vivado automatically: " + "; ".join(env.problems))
    existing = approvals.pending_of_kind(kind)
    return existing or await approvals.request(kind, title, desc, {"problems": env.problems})


@router.post("/vivado/stop-headless")
async def vivado_stop_headless() -> dict:
    from backend.vivado import container as ctr

    return await ctr.stop_headless()


# ------------------------------------------------------------------ approvals

@router.get("/approvals")
async def list_approvals(status: str | None = None) -> list[dict]:
    return store.list_approvals(status)


@router.post("/approvals/{approval_id}/approve")
async def approve(approval_id: str) -> dict:
    return await _decide(approval_id, True)


@router.post("/approvals/{approval_id}/deny")
async def deny(approval_id: str) -> dict:
    return await _decide(approval_id, False)


async def _decide(approval_id: str, ok: bool) -> dict:
    try:
        return await approvals.decide(approval_id, ok)
    except KeyError:
        raise _404("approval")
    except approvals.ApprovalConflict as e:
        raise HTTPException(409, str(e))


# ------------------------------------------------------------------ projects

@router.get("/projects")
async def list_projects() -> list[dict]:
    return store.list_projects()


@router.post("/projects")
async def create_project(req: TextRequest) -> dict:
    _check_fault(req.options)
    project, run = await orchestrator.new_project(req.text, req.options.model_dump())
    return {"project": project, "run": run}


@router.get("/projects/{project_id}")
async def get_project(project_id: str) -> dict:
    return _project_payload(project_id)


@router.post("/projects/{project_id}/messages")
async def post_message(project_id: str, req: TextRequest) -> dict:
    _check_fault(req.options)
    try:
        run, msg = await orchestrator.follow_up(project_id, req.text, req.options.model_dump())
    except KeyError:
        raise _404("project")
    return {"run": run, "message": msg}


def _check_fault(opts: RunOptions) -> None:
    if not opts.inject_fault:
        return
    from backend.verification.mutations import MUTATIONS

    m = MUTATIONS.get(opts.inject_fault)
    if m is None:
        raise HTTPException(422, f"unknown fault '{opts.inject_fault}'")
    if getattr(m, "target", "core") != "core":
        raise HTTPException(422, "only cl_core faults can be injected")


@router.get("/faults")
async def list_faults() -> list[dict]:
    """Defects available for the fault-injection test mode (core-target mutations only)."""
    from backend.verification.mutations import MUTATIONS

    out = []
    for name, m in MUTATIONS.items():
        if getattr(m, "target", "core") != "core":
            continue
        fams = getattr(m, "families", None)
        out.append({"name": name, "description": getattr(m, "description", ""),
                    "families": sorted(fams) if fams else []})
    return out


# ------------------------------------------------------------------ revisions

def _rev(revision_id: str) -> dict:
    rev = store.get_revision(revision_id)
    if rev is None:
        raise _404("revision")
    return rev


@router.get("/revisions/{revision_id}")
async def get_revision(revision_id: str) -> dict:
    return store.revision_public(_rev(revision_id))


@router.get("/revisions/{revision_id}/files/{name}")
async def get_revision_file(revision_id: str, name: str) -> dict:
    from backend.rtl.wrapper import LIBRARY_DIR, LIBRARY_FILES

    rev = _rev(revision_id)
    if name in TEXT_FILES:
        p = Path(rev["dir"]) / "rtl" / name
        role = {"cl_core.v": "core", "basys3_top.v": "wrapper", "constraints.xdc": "xdc"}[name]
    elif name in LIBRARY_FILES:
        p, role = LIBRARY_DIR / name, "library"
    else:
        raise _404("file")
    if not p.is_file():
        raise _404("file")
    return {"name": name, "role": role, "content": p.read_text()}


@router.get("/revisions/{revision_id}/diff")
async def diff_revision(revision_id: str, against: str | None = None) -> dict:
    rev = _rev(revision_id)
    other_id = against or rev.get("parent_id")
    if not other_id:
        return {"against": None, "files": [], "spec_diff": ""}
    other = _rev(other_id)
    files = []
    for name in sorted(TEXT_FILES):
        a = Path(other["dir"]) / "rtl" / name
        b = Path(rev["dir"]) / "rtl" / name
        at = a.read_text().splitlines(keepends=True) if a.is_file() else []
        bt = b.read_text().splitlines(keepends=True) if b.is_file() else []
        files.append({"name": name, "diff": "".join(difflib.unified_diff(
            at, bt, fromfile=f"r{other['number']}/{name}", tofile=f"r{rev['number']}/{name}"))})
    sa = json.dumps(other["spec"], indent=2, sort_keys=True).splitlines(keepends=True)
    sb = json.dumps(rev["spec"], indent=2, sort_keys=True).splitlines(keepends=True)
    spec_diff = "".join(difflib.unified_diff(sa, sb, fromfile=f"r{other['number']}/spec.json", tofile=f"r{rev['number']}/spec.json"))
    return {"against": other_id, "files": files, "spec_diff": spec_diff}


@router.get("/revisions/{revision_id}/simulation")
async def get_simulation(revision_id: str) -> dict:
    p = Path(_rev(revision_id)["dir"]) / "sim" / "report.json"
    if not p.is_file():
        raise _404("simulation report")
    return json.loads(p.read_text())


@router.get("/revisions/{revision_id}/waveform")
async def get_waveform(revision_id: str, scenario: str, start: int | None = None, end: int | None = None,
                       signals: str | None = None) -> dict:
    from backend.verification.waveform import vcd_to_json

    rev = _rev(revision_id)
    rep = Path(rev["dir"]) / "sim" / "report.json"
    if not rep.is_file():
        raise _404("simulation report")
    report = json.loads(rep.read_text())
    sc = next((s for s in report.get("scenarios", []) if s.get("id") == scenario), None)
    if sc is None or not sc.get("vcd_file"):
        raise _404("waveform")
    try:
        vcd = ensure_within(Path(sc["vcd_file"]) if Path(sc["vcd_file"]).is_absolute() else Path(rev["dir"]) / sc["vcd_file"],
                            store.runs_root())
    except PathOutsideWorkspace:
        raise HTTPException(403, "waveform outside workspace")
    if not vcd.is_file():
        raise _404("waveform")
    sig = [x for x in signals.split(",") if x] if signals else None
    return vcd_to_json(vcd, signals=sig, start=start, end=end)


@router.get("/revisions/{revision_id}/build")
async def get_build(revision_id: str) -> dict:
    p = Path(_rev(revision_id)["dir"]) / "vivado" / "build_result.json"
    if not p.is_file():
        raise _404("build result")
    return json.loads(p.read_text())


@router.get("/revisions/{revision_id}/build/log")
async def get_build_log(revision_id: str, tail: int = Query(2000, ge=1, le=100000)) -> dict:
    p = Path(_rev(revision_id)["dir"]) / "vivado" / "vivado.log"
    if not p.is_file():
        raise _404("build log")
    lines = p.read_text(errors="replace").splitlines()
    return {"lines": lines[-tail:], "total": len(lines)}


@router.get("/revisions/{revision_id}/reports/{name}")
async def get_report(revision_id: str, name: str) -> dict:
    if name not in REPORT_FILES:
        raise _404("report")
    p = Path(_rev(revision_id)["dir"]) / "vivado" / "reports" / name
    if not p.is_file():
        raise _404("report")
    return {"name": name, "content": p.read_text(errors="replace")}


@router.post("/revisions/{revision_id}/rebuild")
async def rebuild(revision_id: str) -> dict:
    try:
        return await orchestrator.rebuild(revision_id)
    except KeyError:
        raise _404("revision")
    except ValueError as e:
        raise HTTPException(409, str(e))


@router.post("/revisions/{revision_id}/program")
async def program(revision_id: str) -> dict:
    """Start a reprogram run; returns the approval it creates (or 409 when the board is unavailable)."""
    import asyncio

    try:
        run = await orchestrator.reprogram(revision_id)
    except KeyError:
        raise _404("revision")
    except ValueError as e:
        raise HTTPException(409, str(e))
    for _ in range(300):  # detection takes a few seconds
        await asyncio.sleep(0.1)
        pend = [a for a in store.list_approvals("pending") if a.get("run_id") == run["id"]]
        if pend:
            return pend[0]
        r = store.get_run(run["id"])
        if r and r.get("finished_at"):
            det = next((s for s in r["stages"] if s["key"] == "detect"), {})
            raise HTTPException(409, det.get("summary") or r.get("error") or f"run ended with status {r['status']}")
    raise HTTPException(409, "board detection did not finish in time")


# ------------------------------------------------------------------ runs

@router.get("/runs/{run_id}")
async def get_run(run_id: str) -> dict:
    r = store.get_run(run_id)
    if r is None:
        raise _404("run")
    return r


@router.get("/runs/{run_id}/events")
async def get_run_events(run_id: str, after: int = 0, limit: int = Query(2000, ge=1, le=10000)) -> list[dict]:
    return store.get_db().events_after(after, limit, run_id=run_id)


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str) -> dict:
    try:
        return await orchestrator.cancel(run_id)
    except KeyError:
        raise _404("run")


# ------------------------------------------------------------------ hardware

@router.get("/hardware")
async def get_hardware() -> dict:
    from backend.programmer.manager import hardware

    return (await hardware.status(deep=False)).model_dump()


@router.post("/hardware/detect")
async def detect_hardware() -> dict:
    from backend.programmer.manager import hardware

    await hardware.detect()
    st = (await hardware.status(deep=False)).model_dump()
    await bus.emit("hardware.updated", hardware=st)
    return st


@router.post("/hardware/stop-xvcd")
async def stop_xvcd(req: PidRequest) -> dict:
    from backend.programmer.manager import hardware

    st = await hardware.status(deep=False)
    if req.pid not in (st.xvc.xvcd_pids or []):
        raise HTTPException(409, f"pid {req.pid} is not a running xvcd process")
    return await approvals.request(
        "stop_xvcd", f"Stop xvcd (pid {req.pid})",
        "Terminate the macOS xvcd USB-JTAG bridge so openFPGALoader can open the board. "
        + (st.xvc.note or ""), {"pid": req.pid})


# ------------------------------------------------------------------ vision

@router.get("/vision/sessions")
async def list_vision(revision_id: str | None = None) -> list[dict]:
    return vision_service.list_for_revision(revision_id)


@router.post("/vision/sessions")
async def create_vision(req: VisionCreate) -> dict:
    try:
        st = vision_service.create(req.revision_id)
    except KeyError:
        raise _404("revision")
    await vision_service.publish(st["id"])
    return st


def _vs(session_id: str) -> Any:
    try:
        entry = vision_service.get(session_id)
    except KeyError:
        raise _404("vision session")
    if entry.get("kind", "design") != "design":  # security board observations have their own routes
        raise _404("vision session")
    return entry["session"]


@router.get("/vision/sessions/{session_id}")
async def get_vision(session_id: str) -> dict:
    _vs(session_id)
    return vision_service.state(session_id)


@router.put("/vision/sessions/{session_id}/roi")
async def set_roi(session_id: str, req: RoiRequest) -> dict:
    from backend.vision.session import RoiConfig

    s = _vs(session_id)
    try:
        s.set_roi(RoiConfig.model_validate({"leds": req.leds}))
    except ValueError as e:
        raise HTTPException(422, str(e))
    return await vision_service.publish(session_id)


@router.post("/vision/sessions/{session_id}/auto-roi")
async def auto_roi(session_id: str, req: AutoRoiRequest) -> dict:
    s = _vs(session_id)
    frame = vision_service.last_frame(session_id)
    if frame is None:
        raise HTTPException(409, "no camera frame received yet")
    try:
        roi = s.auto_roi(frame, tuple(req.box), req.count, req.first_led)
        s.set_roi(roi)
    except ValueError as e:  # bad box/count/LED range or an undecodable frame
        raise HTTPException(422, str(e))
    return await vision_service.publish(session_id)


@router.post("/vision/sessions/{session_id}/phase")
async def vision_phase(session_id: str, req: PhaseRequest) -> dict:
    s = _vs(session_id)
    try:
        if req.action == "start":
            if not req.phase_id:
                raise HTTPException(422, "phase_id required")
            s.start_phase(req.phase_id)
        elif req.action == "end":
            s.end_phase()
        else:
            raise HTTPException(422, "action must be 'start' or 'end'")
    except (KeyError, ValueError, RuntimeError) as e:
        raise HTTPException(409, str(e))
    return await vision_service.publish(session_id)


@router.post("/vision/sessions/{session_id}/finish")
async def finish_vision(session_id: str) -> dict:
    _vs(session_id)
    return await vision_service.finish(session_id)


# ------------------------------------------------------------------ files

@router.get("/files/{path:path}")
async def get_file(path: str):
    root = store.runs_root()
    try:
        p = ensure_within(root / path, root)
    except PathOutsideWorkspace:
        raise HTTPException(403, "path outside runs directory")
    if not p.is_file() or p.suffix in (".sqlite3", ".sqlite3-wal", ".sqlite3-shm") or p.name.startswith("siliconbreak.sqlite3"):
        raise _404("file")
    return FileResponse(p, filename=p.name)
