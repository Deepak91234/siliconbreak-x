"""Explicit user approvals for actions that change hardware or the environment.

Kinds:
  program_fpga            volatile SRAM programming of the Basys 3 (executed by the waiting run)
  start_vivado_container  start the SiliconBreak X-managed headless Vivado container (executed here)
  start_docker            launch Docker Desktop (executed here)
  stop_xvcd               terminate the macOS xvcd USB bridge (executed here, pid re-verified)

An approval can only be decided once, only from the API (i.e. by the user in
the browser), and the hardware manager re-checks it (status + bitstream hash)
immediately before programming.
"""

from __future__ import annotations

import asyncio
import logging

from backend.core.events import bus
from backend.storage import store
from backend.storage.db import now

log = logging.getLogger("siliconbreak.approvals")

_waiters: dict[str, asyncio.Future] = {}


class ApprovalConflict(Exception):
    pass


async def request(kind: str, title: str, description: str, details: dict, *,
                  run_id: str | None = None, project_id: str | None = None) -> dict:
    appr = store.create_approval(kind, title, description, details, run_id=run_id, project_id=project_id)
    _waiters[appr["id"]] = asyncio.get_running_loop().create_future()
    await bus.emit("approval.requested", run_id=run_id, project_id=project_id, approval=appr)
    if project_id:
        msg = store.add_message(project_id, "agent", f"Approval needed: {title}. {description}",
                                run_id=run_id, kind="approval", data={"approval_id": appr["id"]})
        await bus.emit("agent.message", run_id=run_id, project_id=project_id, message=msg)
    return appr


async def wait(approval_id: str, cancel: asyncio.Event | None = None) -> str:
    """Block until the user decides. Returns 'approved', 'denied' or 'cancelled'."""
    fut = _waiters.get(approval_id)
    if fut is None:
        appr = store.get_approval(approval_id)
        return "approved" if appr and appr["status"] in ("approved", "executed") else "denied"
    waits = [fut] + ([asyncio.ensure_future(cancel.wait())] if cancel else [])
    done, pending = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
    for p in pending:
        if p is not fut:
            p.cancel()
    if fut in done:
        return fut.result()
    appr = store.update_approval(approval_id, status="expired", decided_at=now(),
                                 result={"ok": False, "message": "run cancelled before a decision"})
    _waiters.pop(approval_id, None)
    await bus.emit("approval.resolved", run_id=appr.get("run_id"), project_id=appr.get("project_id"), approval=appr)
    return "cancelled"


async def decide(approval_id: str, approve: bool) -> dict:
    appr = store.get_approval(approval_id)
    if appr is None:
        raise KeyError(approval_id)
    if appr["status"] != "pending":
        raise ApprovalConflict(f"approval is already {appr['status']}")
    status = "approved" if approve else "denied"
    appr = store.update_approval(approval_id, status=status, decided_at=now())
    await bus.emit("approval.resolved", run_id=appr.get("run_id"), project_id=appr.get("project_id"), approval=appr)

    if approve and appr["kind"] in ("start_vivado_container", "start_docker", "stop_xvcd"):
        result = await _execute(appr)
        appr = store.update_approval(approval_id, status="executed" if result.get("ok") else "failed", result=result)
        await bus.emit("approval.resolved", run_id=appr.get("run_id"), project_id=appr.get("project_id"), approval=appr)
        if appr.get("project_id"):
            msg = store.add_message(appr["project_id"], "agent",
                                    f"{appr['title']}: {'done' if result.get('ok') else 'failed'} - {result.get('message', '')}",
                                    run_id=appr.get("run_id"), kind="result" if result.get("ok") else "error")
            await bus.emit("agent.message", run_id=appr.get("run_id"), project_id=appr.get("project_id"), message=msg)

    fut = _waiters.pop(approval_id, None)
    if fut is not None and not fut.done():
        fut.set_result(status)
    return appr


async def _execute(appr: dict) -> dict:
    from backend.vivado import container as ctr

    kind = appr["kind"]
    try:
        if kind == "start_vivado_container":
            return await ctr.start_headless()
        if kind == "start_docker":
            return await ctr.start_docker_desktop()
        if kind == "stop_xvcd":
            from backend.programmer.manager import hardware
            return await hardware.stop_xvcd(int(appr["details"]["pid"]), approval_id=appr["id"])
    except Exception as e:  # report, never crash the API
        log.exception("approval action failed")
        return {"ok": False, "message": f"{type(e).__name__}: {e}"}
    return {"ok": False, "message": f"unknown approval kind {kind}"}


def mark_result(approval_id: str, ok: bool, result: dict) -> dict:
    return store.update_approval(approval_id, status="executed" if ok else "failed", result=result)


async def verify_program_approval(approval_id: str, sha256: str) -> bool:
    """Hardware manager hook: only an approved, not-yet-used program approval for this exact bitstream passes."""
    appr = store.get_approval(approval_id)
    return bool(
        appr
        and appr["kind"] == "program_fpga"
        and appr["status"] == "approved"
        and (appr.get("details") or {}).get("bitstream_sha256") == sha256
    )


async def verify_stop_xvcd_approval(approval_id: str, pid: int) -> bool:
    appr = store.get_approval(approval_id)
    return bool(appr and appr["kind"] == "stop_xvcd" and appr["status"] == "approved"
                and int((appr.get("details") or {}).get("pid", -1)) == int(pid))


def expire_stale() -> int:
    """Pending approvals from a previous backend process can no longer be honoured."""
    stale = store.list_approvals("pending")
    for a in stale:
        if a["id"] not in _waiters:
            store.update_approval(a["id"], status="expired", decided_at=now(),
                                  result={"ok": False, "message": "backend restarted before a decision"})
    return len(stale)


def pending_of_kind(kind: str) -> dict | None:
    for a in store.list_approvals("pending"):
        if a["kind"] == kind:
            return a
    return None
