"""CircuitLoop FastAPI application.

Run:  .venv/bin/python -m uvicorn backend.api.app:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import struct
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.agent import approvals
from backend.api import environment, vision_service
from backend.api.guide_routes import router as guide_router
from backend.api.routes import router
from backend.core.config import REPO_ROOT, get_settings
from backend.core.events import Event, bus
from backend.storage import store

log = logging.getLogger("circuitloop")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


async def _persist(ev: Event) -> None:
    store.get_db().append_event(ev.model_dump())


async def _startup_checks() -> None:
    """Detect Docker/Vivado on startup and offer (never force) a headless start."""
    try:
        env = await environment.collect(force=True)
    except Exception:
        log.exception("environment detection failed")
        return
    viv = env["vivado"]
    log.info("Vivado: ready=%s container=%s version=%s problems=%s", viv.get("ready"), viv.get("active_container"),
             viv.get("vivado_version"), viv.get("problems"))
    if viv.get("ready"):
        return
    s = get_settings()
    if not viv.get("docker_daemon"):
        if not approvals.pending_of_kind("start_docker"):
            await approvals.request("start_docker", "Start Docker Desktop",
                                    "Docker Desktop is not running, so Vivado is unavailable. Approve to launch it.",
                                    {"problems": viv.get("problems", [])})
    elif viv.get("can_start_headless") and not approvals.pending_of_kind("start_vivado_container"):
        await approvals.request(
            "start_vivado_container", "Start a headless Vivado container",
            f"No Vivado container is running. Approve to start '{s.vivado_headless_container}' from '{s.vivado_image}' "
            "with your existing mount (no GUI/VNC/xvcd). Your vivado_container is not modified.",
            {"problems": viv.get("problems", [])})


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    s.runs_dir.mkdir(parents=True, exist_ok=True)
    db = store.get_db()
    bus.set_next_seq(db.max_event_seq() + 1)
    bus.add_sink(_persist)
    expired = approvals.expire_stale()
    if expired:
        log.info("expired %d approvals left pending by a previous backend process", expired)
    # runs interrupted by a restart cannot resume: mark them honestly
    for r in db.query("SELECT * FROM runs WHERE status IN ('running', 'waiting')"):
        stages = r["stages"]
        for st in stages:
            if st["status"] in ("running", "blocked", "pending"):
                st["status"] = "cancelled"
                st["summary"] = "backend restarted before this stage finished"
        store.update_run(r["id"], status="failed", error="backend restarted during the run", stages=stages,
                         finished_at=time.time())

    from backend.programmer.manager import hardware

    hardware.set_approval_verifier(approvals.verify_program_approval)
    hardware.set_xvcd_approval_verifier(approvals.verify_stop_xvcd_approval)
    monitor = asyncio.create_task(hardware.monitor())
    startup = asyncio.create_task(_startup_checks())
    log.info("CircuitLoop backend ready (runs dir %s, LLM configured: %s)", s.runs_dir, s.llm_configured)
    try:
        yield
    finally:
        bus.remove_sink(_persist)
        for t in (monitor, startup):
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t


app = FastAPI(title="CircuitLoop", version="0.1.0", lifespan=lifespan)

# ---- local-only access controls -------------------------------------------------
# The API can program the FPGA and start containers (behind user approvals), so it
# must not be drivable by other web pages open in the same browser:
#  * TrustedHostMiddleware rejects DNS-rebinding requests (Host must be localhost).
#  * Cross-origin writes and WebSockets are refused by Origin check. CORS alone only
#    hides responses; it does not stop a "simple" cross-site POST from executing.
# Requests without an Origin header (curl, scripts on this machine) are allowed.
ALLOWED_ORIGINS = {"http://127.0.0.1:5173", "http://localhost:5173", "http://127.0.0.1:8000", "http://localhost:8000"}
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(ALLOWED_ORIGINS),
    allow_methods=["*"], allow_headers=["*"],
)


def _origin_ok(origin: str | None) -> bool:
    return origin is None or origin in ALLOWED_ORIGINS


@app.middleware("http")
async def reject_cross_origin_writes(request: Request, call_next):
    if request.method not in ("GET", "HEAD", "OPTIONS") and not _origin_ok(request.headers.get("origin")):
        return JSONResponse({"detail": "cross-origin request refused"}, status_code=403)
    return await call_next(request)


app.include_router(router)
app.include_router(guide_router)


@app.websocket("/ws/events")
async def ws_events(ws: WebSocket, after: int = 0) -> None:
    if not _origin_ok(ws.headers.get("origin")):
        await ws.close(code=1008)
        return
    await ws.accept()
    q = bus.subscribe()
    try:
        last = after
        for ev in store.get_db().events_after(after, 2000):
            await ws.send_text(json.dumps(ev, default=str))
            last = ev["seq"]
        while True:
            ev = await q.get()
            if ev.seq <= last:
                continue
            last = ev.seq
            await ws.send_text(ev.model_dump_json())
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        bus.unsubscribe(q)


@app.websocket("/ws/vision/{session_id}")
async def ws_vision(ws: WebSocket, session_id: str) -> None:
    if not _origin_ok(ws.headers.get("origin")):
        await ws.close(code=1008)
        return
    await ws.accept()
    try:
        entry = vision_service.get(session_id)
    except KeyError:
        await ws.send_text(json.dumps({"type": "error", "message": "unknown vision session"}))
        await ws.close()
        return
    sess = entry["session"]
    n = 0
    last_state = None
    try:
        while True:
            data = await ws.receive_bytes()
            if len(data) < 9:
                continue
            (t_ms,) = struct.unpack("<d", data[:8])
            jpeg = data[8:]
            vision_service.remember_frame(session_id, jpeg)
            try:
                fr = await asyncio.to_thread(sess.ingest_frame, t_ms, jpeg)
            except Exception as e:  # a bad frame must not kill the stream
                await ws.send_text(json.dumps({"type": "error", "message": f"frame rejected: {e}"}))
                continue
            n += 1
            if n % 3 == 0:
                payload = fr.model_dump() if hasattr(fr, "model_dump") else fr
                await ws.send_text(json.dumps({"type": "frame", **payload}, default=str))
            if n % 15 == 0:
                st = vision_service.state(session_id)
                key = (st.get("status"), st.get("current_phase"))
                if key != last_state:
                    last_state = key
                await ws.send_text(json.dumps({"type": "state", "session": st}, default=str))
    except (WebSocketDisconnect, RuntimeError):
        pass


_dist = REPO_ROOT / "frontend" / "dist"
if _dist.is_dir():  # production build served by the backend; dev uses the Vite server
    app.mount("/", StaticFiles(directory=str(_dist), html=True), name="frontend")
