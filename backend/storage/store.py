"""Typed access to persisted CircuitLoop state, shaped exactly like docs/api.md objects."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from backend.core.config import get_settings
from backend.core.events import STAGES
from backend.storage.db import Database, new_id, now


@lru_cache(maxsize=1)
def get_db() -> Database:
    return Database(get_settings().db_path)


def runs_root() -> Path:
    return get_settings().runs_dir.resolve()


def revision_dir(project_id: str, revision_id: str) -> Path:
    return runs_root() / "projects" / project_id / revision_id


# ------------------------------------------------------------------- projects

def create_project(title: str) -> dict:
    pid = new_id("p")
    t = now()
    get_db().insert("projects", {"id": pid, "title": title[:120] or "Untitled design", "created_at": t, "updated_at": t})
    return get_project(pid)  # type: ignore[return-value]


def get_project(project_id: str) -> dict | None:
    db = get_db()
    p = db.get("projects", project_id)
    if not p:
        return None
    p["revision_count"] = db.scalar("SELECT COUNT(*) FROM revisions WHERE project_id = ?", (project_id,)) or 0
    p["latest_run_status"] = None
    if p.get("latest_run_id"):
        r = db.get("runs", p["latest_run_id"])
        p["latest_run_status"] = r["status"] if r else None
    return p


def list_projects() -> list[dict]:
    ids = [r["id"] for r in get_db().query("SELECT id FROM projects ORDER BY updated_at DESC")]
    return [p for p in (get_project(i) for i in ids) if p]


def touch_project(project_id: str, **fields) -> None:
    get_db().update("projects", project_id, {"updated_at": now(), **fields})


# ------------------------------------------------------------------ revisions

REVISION_SUMMARY_FIELDS = (
    "id", "project_id", "number", "parent_id", "kind", "user_request", "generator", "interpreter",
    "status", "sim_status", "build_status", "change_summary", "created_at",
)


def create_revision(project_id: str, *, kind: str, parent_id: str | None, user_request: str, spec: dict,
                    interpreter: str, change_summary: list[str] | None = None) -> dict:
    db = get_db()
    rid = new_id("r")
    number = (db.scalar("SELECT COALESCE(MAX(number), 0) FROM revisions WHERE project_id = ?", (project_id,)) or 0) + 1
    d = revision_dir(project_id, rid)
    d.mkdir(parents=True, exist_ok=True)
    db.insert("revisions", {
        "id": rid, "project_id": project_id, "number": number, "parent_id": parent_id, "kind": kind,
        "user_request": user_request, "interpreter": interpreter, "status": "draft", "dir": str(d),
        "spec": spec, "change_summary": change_summary or [], "created_at": now(),
    })
    return get_revision(rid)  # type: ignore[return-value]


def get_revision(revision_id: str) -> dict | None:
    return get_db().get("revisions", revision_id)


def update_revision(revision_id: str, **fields) -> dict:
    get_db().update("revisions", revision_id, fields)
    return get_revision(revision_id)  # type: ignore[return-value]


def revision_summary(rev: dict) -> dict:
    return {k: rev.get(k) for k in REVISION_SUMMARY_FIELDS}


def revision_public(rev: dict) -> dict:
    out = revision_summary(rev)
    out.update({
        "spec": rev.get("spec"),
        "requirements": rev.get("requirements") or [],
        "observations": rev.get("observations") or [],
        "files": rev.get("files") or [],
        "safety_issues": rev.get("safety") or [],
        "explanation": rev.get("explanation") or "",
        "simulation": rev.get("simulation"),
        "build": rev.get("build"),
        "verification": rev.get("verification"),
    })
    return out


def list_revisions(project_id: str) -> list[dict]:
    return get_db().query("SELECT * FROM revisions WHERE project_id = ? ORDER BY number", (project_id,))


# ----------------------------------------------------------------------- runs

def initial_stages() -> list[dict]:
    return [{"key": k, "label": label, "status": "pending", "summary": "", "started_at": None,
             "finished_at": None, "detail": {}} for k, label in STAGES]


def create_run(project_id: str, *, trigger: str, request_text: str, options: dict) -> dict:
    rid = new_id("run")
    get_db().insert("runs", {
        "id": rid, "project_id": project_id, "trigger": trigger, "request_text": request_text,
        "status": "running", "revision_ids": [], "stages": initial_stages(), "options": options,
        "created_at": now(),
    })
    touch_project(project_id, latest_run_id=rid)
    return get_run(rid)  # type: ignore[return-value]


def get_run(run_id: str) -> dict | None:
    return get_db().get("runs", run_id)


def update_run(run_id: str, **fields) -> dict:
    get_db().update("runs", run_id, fields)
    return get_run(run_id)  # type: ignore[return-value]


def list_runs(project_id: str) -> list[dict]:
    return get_db().query("SELECT * FROM runs WHERE project_id = ? ORDER BY created_at", (project_id,))


def latest_run_for_revision(revision_id: str) -> dict | None:
    rows = get_db().query(
        "SELECT * FROM runs WHERE revision_id = ? ORDER BY created_at DESC LIMIT 1", (revision_id,))
    return rows[0] if rows else None


# ------------------------------------------------------------------- messages

def add_message(project_id: str | None, role: str, text: str, *, run_id: str | None = None,
                kind: str = "text", data: dict | None = None) -> dict:
    mid = new_id("m")
    get_db().insert("messages", {"id": mid, "project_id": project_id, "run_id": run_id, "role": role,
                                 "kind": kind, "text": text, "data": data or {}, "created_at": now()})
    return get_db().get("messages", mid)  # type: ignore[return-value]


def list_messages(project_id: str) -> list[dict]:
    return get_db().query("SELECT * FROM messages WHERE project_id = ? ORDER BY created_at", (project_id,))


# ------------------------------------------------------------------ approvals

def create_approval(kind: str, title: str, description: str, details: dict, *, run_id: str | None = None,
                    project_id: str | None = None) -> dict:
    aid = new_id("a")
    get_db().insert("approvals", {"id": aid, "kind": kind, "status": "pending", "title": title,
                                  "description": description, "details": details, "run_id": run_id,
                                  "project_id": project_id, "created_at": now()})
    return get_approval(aid)  # type: ignore[return-value]


def get_approval(approval_id: str) -> dict | None:
    return get_db().get("approvals", approval_id)


def update_approval(approval_id: str, **fields) -> dict:
    get_db().update("approvals", approval_id, fields)
    return get_approval(approval_id)  # type: ignore[return-value]


def list_approvals(status: str | None = None) -> list[dict]:
    if status:
        return get_db().query("SELECT * FROM approvals WHERE status = ? ORDER BY created_at", (status,))
    return get_db().query("SELECT * FROM approvals ORDER BY created_at DESC LIMIT 200")


# ------------------------------------------------------------- vision sessions

def create_vision_session(revision_id: str, run_id: str | None, directory: Path) -> dict:
    vid = new_id("v")
    get_db().insert("vision_sessions", {"id": vid, "revision_id": revision_id, "run_id": run_id,
                                        "status": "setup", "dir": str(directory), "created_at": now()})
    return get_db().get("vision_sessions", vid)  # type: ignore[return-value]


def update_vision_session(session_id: str, **fields) -> None:
    get_db().update("vision_sessions", session_id, fields)


def get_vision_session(session_id: str) -> dict | None:
    return get_db().get("vision_sessions", session_id)
