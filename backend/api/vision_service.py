"""Registry of live webcam verification sessions (one VisionSession per browser session)."""

from __future__ import annotations

from pathlib import Path

from backend.core.events import bus
from backend.specifications.ir import DesignSpec
from backend.storage import store
from backend.storage.db import now

_sessions: dict = {}
_last_frame: dict[str, bytes] = {}


def create(revision_id: str):
    from backend.vision.session import VisionSession

    rev = store.get_revision(revision_id)
    if rev is None:
        raise KeyError(revision_id)
    run = store.latest_run_for_revision(revision_id)
    spec = DesignSpec.model_validate(rev["spec"])
    row = store.create_vision_session(revision_id, run["id"] if run else None, Path(rev["dir"]) / "vision")
    evidence = Path(rev["dir"]) / "vision" / row["id"]
    evidence.mkdir(parents=True, exist_ok=True)
    store.update_vision_session(row["id"], dir=str(evidence))
    sess = VisionSession(row["id"], spec, evidence)
    _sessions[row["id"]] = {"session": sess, "revision_id": revision_id, "project_id": rev["project_id"]}
    return state(row["id"])


def list_for_revision(revision_id: str | None = None) -> list[dict]:
    """Live sessions held by this backend process (sessions do not survive a restart)."""
    return [state(sid) for sid, e in _sessions.items() if revision_id is None or e["revision_id"] == revision_id]


def get(session_id: str):
    entry = _sessions.get(session_id)
    if entry is None:
        raise KeyError(session_id)
    return entry


def state(session_id: str) -> dict:
    entry = get(session_id)
    d = entry["session"].to_dict()
    d["id"] = session_id
    d["revision_id"] = entry["revision_id"]
    return d


async def publish(session_id: str) -> dict:
    st = state(session_id)
    await bus.emit("vision.updated", project_id=get(session_id)["project_id"], session=st)
    return st


def remember_frame(session_id: str, jpeg: bytes) -> None:
    _last_frame[session_id] = jpeg


def last_frame(session_id: str) -> bytes | None:
    return _last_frame.get(session_id)


async def finish(session_id: str) -> dict:
    from backend.agent.orchestrator import orchestrator

    entry = get(session_id)
    result = entry["session"].finish()
    res = result.model_dump() if hasattr(result, "model_dump") else dict(result)
    res = await orchestrator.record_verification(entry["revision_id"], res)  # adds programmed_by_circuitloop
    store.update_vision_session(session_id, status="finished", result=res, finished_at=now())
    await publish(session_id)
    return res
