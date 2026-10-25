# CircuitLoop HTTP / WebSocket API (v1)

Backend: FastAPI on `http://127.0.0.1:8000`. Frontend dev server (Vite, port 5173)
proxies `/api` and `/ws` to it. All JSON. Timestamps are Unix seconds (float).
IDs are short strings (`p_…` project, `r_…` revision, `run_…` run, `a_…` approval,
`v_…` vision session). Every value shown in the UI must come from these
endpoints or from WebSocket events — the frontend never invents progress.

## Status vocabularies

* Stage keys (display order): `spec, validate, rtl, lint, tests, simulate, repair, synth, impl, timing, bitstream, detect, program, verify`
* Stage status: `pending | running | passed | failed | skipped | blocked | unavailable | inconclusive | cancelled`
  (`blocked` = waiting for user approval or action; `unavailable` = required tool/hardware absent)
* Run status: `running | waiting | passed | passed_sim_only | failed | cancelled | unsupported`
* Approval status: `pending | approved | denied | executed | failed | expired`
* Hardware state: `connected | no_board | busy | unsupported | error | tool_missing | unknown`
* Vision verdict: `passed | failed | inconclusive | unavailable`

## Core objects

```jsonc
// Stage
{"key": "simulate", "label": "Simulation", "status": "running", "summary": "3/5 scenarios",
 "started_at": 1760000000.1, "finished_at": null, "detail": {}}

// Run
{"id": "run_ab12", "project_id": "p_x", "trigger": "new" | "modify" | "rebuild" | "reprogram",
 "request_text": "...", "status": "running", "revision_id": "r_3",       // latest revision of this run
 "revision_ids": ["r_2", "r_3"],                                          // includes repair revisions
 "stages": [Stage, ...],                                                  // always all 14, in order
 "options": {"inject_fault": null, "simulation_only": false},
 "error": null, "created_at": 0, "finished_at": null}

// Project
{"id": "p_x", "title": "4-bit variable-speed counter", "created_at": 0, "updated_at": 0,
 "current_revision_id": "r_3", "revision_count": 3, "latest_run_id": "run_ab12", "latest_run_status": "passed"}

// Message (chat history)
{"id": "m_1", "project_id": "p_x", "run_id": "run_ab12" | null, "role": "user" | "agent" | "system",
 "kind": "text" | "spec" | "approval" | "error" | "result", "text": "...", "data": {}, "created_at": 0}

// RevisionSummary
{"id": "r_3", "project_id": "p_x", "number": 3, "parent_id": "r_2", "kind": "initial" | "modification" | "repair",
 "user_request": "...", "generator": "llm:claude-opus-5-5" | "template/counter", "interpreter": "llm:…" | "offline-rules",
 "status": "draft | lint_failed | sim_failed | sim_passed | build_failed | built | programmed | verified | verify_failed",
 "sim_status": "passed" | "failed" | null, "build_status": "passed" | "failed" | "unavailable" | null,
 "change_summary": ["..."], "created_at": 0}

// Revision (full) = RevisionSummary + 
{"spec": DesignSpec, "requirements": [Requirement], "observations": [Observation],
 "files": [{"name": "cl_core.v", "role": "core" | "wrapper" | "xdc" | "library", "sha256": "...", "size": 1234}],
 "safety_issues": [...], "explanation": "...",
 "simulation": SimulationSummary | null,      // {"status","passed_checks","failed_checks","scenarios":[{"id","title","passed"}]}
 "build": BuildSummary | null,                // {"status","timing":{...},"utilization":{...},"bitstream_sha256","bitstream_size","checks":[...]}
 "verification": VerificationResult | null}

// Approval
{"id": "a_1", "kind": "program_fpga" | "start_vivado_container" | "start_docker" | "stop_xvcd",
 "status": "pending", "title": "Program the Basys 3", "description": "...",
 "details": {"bitstream_sha256": "...", "bitstream_path": "...", "revision_id": "r_3", "adapter": "openfpgaloader", "volatile": true},
 "run_id": "run_ab12" | null, "project_id": "p_x" | null, "created_at": 0, "decided_at": null, "result": null}
```

## REST endpoints

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/api/health` | | `{"ok": true, "version": "0.1.0"}` |
| GET | `/api/environment` | | `Environment` (below), cached ≤ 10 s |
| POST | `/api/environment/refresh` | | `Environment` (fresh) |
| POST | `/api/vivado/start` | | `Approval` (kind `start_vivado_container`, or `start_docker` if the daemon is down) |
| POST | `/api/vivado/stop-headless` | | `{"ok", "message"}` (only the CircuitLoop-labelled container) |
| GET | `/api/approvals?status=pending` | | `[Approval]` |
| POST | `/api/approvals/{id}/approve` | | `Approval` (executes / unblocks the waiting run) |
| POST | `/api/approvals/{id}/deny` | | `Approval` |
| GET | `/api/projects` | | `[Project]` newest first |
| POST | `/api/projects` | `{"text": "...", "options": {"inject_fault": null, "simulation_only": false}}` | `{"project": Project, "run": Run}` |
| GET | `/api/projects/{id}` | | `{"project": Project, "revisions": [RevisionSummary], "runs": [Run], "messages": [Message]}` |
| POST | `/api/projects/{id}/messages` | `{"text": "...", "options": {...}}` | `{"run": Run, "message": Message}` — follow-up that modifies the current revision |
| GET | `/api/revisions/{id}` | | `Revision` |
| GET | `/api/revisions/{id}/files/{name}` | | `{"name","role","content"}` (text files only) |
| GET | `/api/revisions/{id}/diff?against={rid}` | | `{"against": rid, "files": [{"name","diff"}], "spec_diff": "..."}` (unified diffs; default against parent) |
| GET | `/api/revisions/{id}/simulation` | | full `SimulationReport` (docs/contracts.md §A) or 404 |
| GET | `/api/revisions/{id}/waveform?scenario=&start=&end=&signals=a,b` | | waveform JSON (contracts §A `vcd_to_json`) |
| GET | `/api/revisions/{id}/build` | | full `VivadoBuildResult` dict or 404 |
| GET | `/api/revisions/{id}/build/log?tail=2000` | | `{"lines": [...], "total": n}` |
| GET | `/api/revisions/{id}/reports/{name}` | | `{"name","content"}` (`timing_summary.rpt`, `utilization_impl.rpt`, `drc.rpt`, `power.rpt`, `io.rpt`) |
| POST | `/api/revisions/{id}/rebuild` | | `Run` (trigger `rebuild`: Vivado again on the same RTL) |
| POST | `/api/revisions/{id}/program` | | `Approval` (kind `program_fpga`; 409 if no valid bitstream) |
| GET | `/api/runs/{id}` | | `Run` |
| GET | `/api/runs/{id}/events?after=0&limit=2000` | | `[Event]` |
| POST | `/api/runs/{id}/cancel` | | `Run` |
| GET | `/api/faults` | | `[{"name","description","families"}]` core defects available to the fault-injection test mode |
| GET | `/api/hardware` | | `HardwareStatus` (contracts §C) |
| POST | `/api/hardware/detect` | | `HardwareStatus` after a JTAG detect |
| POST | `/api/hardware/stop-xvcd` | `{"pid": 123}` | `Approval` (kind `stop_xvcd`) |
| GET | `/api/vision/sessions?revision_id=` | | `[VisionSessionState]` live sessions in this backend process |
| POST | `/api/vision/sessions` | `{"revision_id": "r_3"}` | `VisionSessionState` |
| GET | `/api/vision/sessions/{id}` | | `VisionSessionState` |
| PUT | `/api/vision/sessions/{id}/roi` | `{"leds": [LedRoi]}` | `VisionSessionState` |
| POST | `/api/vision/sessions/{id}/auto-roi` | `{"box": [x,y,w,h], "count": 4, "first_led": 0}` — box normalised to 0..1 of the frame; uses the last frame received on the vision socket | `VisionSessionState` (422 for an invalid box/count/LED range) |
| POST | `/api/vision/sessions/{id}/phase` | `{"action": "start", "phase_id": "..."}` or `{"action": "end"}` | `VisionSessionState` |
| POST | `/api/vision/sessions/{id}/finish` | | `VerificationResult` (also stored on the revision; updates stage `verify`) |
| GET | `/api/files/{path}` | | raw file under `runs/` (path-guarded): bitstreams, reports, VCD, evidence |
| GET | `/api/board` | | trusted Basys 3 definition from `boards/basys3/board.json` (ports, pins, part) |
| GET | `/api/revisions/{id}/guide` | | operating guide: used components (pins, signals, functions) and steps derived from the spec + simulation test plan/report |
| GET | `/api/examples` / `/api/examples/{name}/guide` | | bundled example specs and their guides (Preview mode; `preview: true`, no simulation data) |
| GET | `/api/revisions/{id}/scenarios/{sid}/trace` | | recorded Verilator stimulus + LED trace for one test scenario (`source: "verilator"`) |
| POST | `/api/revisions/{id}/simulate` | `{"events": [{"t_s", "port", "value"}], "duration_s"}` | virtual FPGA: runs this revision's compiled harness on the inputs; `outputs.led = [[t_s, value]]` (≤ 120 s, ≤ 500 events) |
| GET | `/api/revisions/{id}/diagram` | | functional block diagram: nodes from the spec + RTL instance hierarchy (not a netlist) |
| GET | `/api/llm` · POST `/api/llm/reload` | | AI mode (`model` / `offline-rules`), key presence and source; reload re-reads `.env` (the key is never returned) |

```jsonc
// Environment
{"checked_at": 0,
 "vivado": {...VivadoEnvironment.as_dict(), "build_running": false},
 "verilator": {"available": true, "path": "/opt/homebrew/bin/verilator", "version": "Verilator 5.052 ..."},
 "openfpgaloader": {"available": true, "path": "...", "version": "v1.1.1"},
 "llm": {"provider": "anthropic", "model": "claude-opus-5-5", "configured": false},
 "hardware": HardwareStatus,
 "config": {...Settings.public_dict()}}

// VisionSessionState = VisionSession.to_dict()
{"id": "v_1", "revision_id": "r_3", "status": "setup" | "running" | "finished",
 "plan": [PhasePlan], "current_phase": "O-RATE-normal" | null, "roi": RoiConfig | null,
 "live": {"fps": 29.8, "frames": 1234, "quality": {...}}, "result": VerificationResult | null}
// The backend also returns optional extras (spec_title, phase_elapsed_s, phases[], live.states,
// live.levels, live.recorded_frames, live.rejected_frames, live.calibration).
```

## WebSockets

### `/ws/events?after=<seq>`
Server → client JSON `Event` objects (`backend/core/events.py`):
`{"seq": 42, "ts": 0, "type": "stage.updated", "run_id": "...", "project_id": "...", "stage": "simulate", "data": {...}}`.
On connect the server first replays persisted events with `seq > after` (max 2000), then streams live.
Event types and `data`:

| type | data |
|---|---|
| `run.created` / `run.updated` / `run.finished` | `{"run": Run}` |
| `stage.updated` | `{"stage": Stage}` |
| `log` | `{"source": "verilator" \| "vivado" \| "openfpgaloader" \| "agent", "line": "..."}` (stage in envelope) |
| `agent.message` | `{"message": Message}` |
| `approval.requested` / `approval.resolved` | `{"approval": Approval}` |
| `revision.created` / `revision.updated` | `{"revision": RevisionSummary}` |
| `environment.updated` | `{"environment": Environment}` |
| `hardware.updated` | `{"hardware": HardwareStatus}` |
| `vision.updated` | `{"session": VisionSessionState}` |

The client keeps the last `seq` and reconnects with `after=<seq>`; on reconnect it may also
refetch `/api/projects/{id}` to resync.

### `/ws/vision/{session_id}`
Client → server: binary frames. Each message = 8-byte little-endian float64 `t_ms`
(`performance.now()` at capture) followed by JPEG bytes (≤ 640×480, quality ≈ 0.8).
Server → client JSON:
`{"type": "frame", "t_ms": ..., "states": {"0": 1, "1": 0}, "levels": {"0": 0.83}, "quality": {...}}` (sent for every Nth frame)
`{"type": "state", "session": VisionSessionState}` when phase/ROI/result change
`{"type": "error", "message": "..."}`.

## Errors
Non-2xx responses: `{"detail": "human readable message"}`. 404 unknown id, 409 state conflict
(e.g. Vivado busy, no bitstream, approval already decided), 422 validation errors.

## SiliconBreak X (security) endpoints

Documented in [siliconbreak.md](siliconbreak.md#api): `/api/security/health`, `/api/security/cases`,
`/api/security/audits` (create, list, get, repair, file, sources, waveform), the Basys 3 build/program/webcam routes
under `/api/security/audits/{id}/fpga/...` and `/api/security/camera/{sid}`, and `/api/bench/runs`. Board observation
sessions share `/ws/vision/{sid}` with design verification but are not listed by `/api/vision/sessions` and are refused
by its routes.
Audits are ordinary runs (`trigger: "security_audit"` or `"security_repair"`) whose stages are
`contract, index, investigate, reproduce, formal, repair, retest, evidence`; the event stream adds
`security.updated` `{audit}` and `bench.updated` `{bench}`. Run statuses for audits:
`reproduced`, `repaired_verified`, `repair_failed`, `no_violation_found`, `error`, `cancelled`.
