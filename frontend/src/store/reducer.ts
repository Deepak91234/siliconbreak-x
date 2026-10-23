/**
 * Pure state transitions: applying WebSocket events and merging REST snapshots.
 *
 * Kept free of React and I/O so the semantics (ordering, de-duplication,
 * resync merges) are unit-tested in reducer.test.ts. Nothing here creates a
 * value the server did not send; it only files server data into the right place.
 */

import type {
  Approval,
  CLEvent,
  Environment,
  FaultInfo,
  HardwareStatus,
  Message,
  Project,
  ProjectDetailResponse,
  RevisionSummary,
  Run,
  Stage,
  VisionSessionState,
} from "../api/types";
import { STAGE_KEYS } from "../api/types";
import type { SocketInfo } from "../api/events";

export type TabKey = "pipeline" | "verilog" | "simulation" | "diagram" | "vivado" | "hardware" | "vision" | "history";

export interface LogLine {
  seq: number;
  ts: number;
  stage: string | null;
  source: string;
  line: string;
}

export interface ProjectDetail {
  project: Project;
  revisions: RevisionSummary[]; // ascending by number
  runIds: string[]; // ascending by created_at
  messages: Message[]; // ascending by created_at
  loadedAt: number;
}

export interface BackendInfo {
  reachable: boolean | null; // null = not checked yet
  error: string | null;
  lastOkAt: number | null;
  version: string | null;
}

export interface Selection {
  projectId: string | null;
  /** null = follow the project's active/current revision */
  revisionId: string | null;
  /** null = follow the project's latest run */
  runId: string | null;
  tab: TabKey;
  /** History "compare": the Verilog tab opens a diff of revisionId against this. */
  compareAgainst: string | null;
}

export interface AppState {
  backend: BackendInfo;
  socket: SocketInfo;
  environment: Environment | null;
  environmentError: string | null;
  hardware: HardwareStatus | null;
  projects: Project[] | null;
  projectsError: string | null;
  details: Record<string, ProjectDetail>;
  /** Last error loading GET /api/projects/{id}, by project id. */
  projectErrors: Record<string, string>;
  runs: Record<string, Run>;
  approvals: Record<string, Approval>;
  approvalsLoaded: boolean;
  logs: Record<string, LogLine[]>;
  revisionVersions: Record<string, number>;
  vision: Record<string, VisionSessionState>;
  selection: Selection;
  lastEventAt: number | null;
  /** GET /api/faults; null until loaded, [] when the backend offers none. */
  faults: FaultInfo[] | null;
  faultsError: string | null;
}

export const LOG_CAP_PER_RUN = 6000;
export const GLOBAL_LOG_KEY = "_global";

export function initialState(): AppState {
  return {
    backend: { reachable: null, error: null, lastOkAt: null, version: null },
    socket: { status: "idle", lastSeq: 0, attempts: 0, retryInMs: null, lastError: null, connectedAt: null },
    environment: null,
    environmentError: null,
    hardware: null,
    projects: null,
    projectsError: null,
    details: {},
    projectErrors: {},
    runs: {},
    approvals: {},
    approvalsLoaded: false,
    logs: {},
    revisionVersions: {},
    vision: {},
    selection: { projectId: null, revisionId: null, runId: null, tab: "pipeline", compareAgainst: null },
    lastEventAt: null,
    faults: null,
    faultsError: null,
  };
}

// ---------------------------------------------------------------- helpers

/** Run statuses after which the orchestrator never sets running/waiting again. */
export const TERMINAL_RUN_STATUSES: ReadonlySet<string> = new Set([
  "passed", "passed_sim_only", "failed", "cancelled", "unsupported",
]);

export function isRunActive(run: Pick<Run, "status"> | null | undefined): boolean {
  return !!run && (run.status === "running" || run.status === "waiting");
}

/**
 * A snapshot that would move a finished run back to running/waiting must be
 * older than what we hold (runs never restart; rebuilds create new runs), so it
 * is ignored. This protects against REST responses and replayed events racing.
 */
export function isStaleRun(existing: Run | undefined, incoming: Run): boolean {
  return !!existing && TERMINAL_RUN_STATUSES.has(existing.status) && !TERMINAL_RUN_STATUSES.has(incoming.status);
}

/** Approvals only move forward from `pending`; a pending copy of a decided one is stale. */
export function isStaleApproval(existing: Approval | undefined, incoming: Approval): boolean {
  return !!existing && existing.status !== "pending" && incoming.status === "pending";
}

function byCreated<T extends { created_at: number }>(a: T, b: T): number {
  return a.created_at - b.created_at;
}

export function upsertById<T extends { id: string }>(list: T[], item: T, cmp?: (a: T, b: T) => number): T[] {
  const i = list.findIndex((x) => x.id === item.id);
  let out: T[];
  if (i >= 0) {
    out = list.slice();
    out[i] = item;
  } else {
    out = [...list, item];
  }
  return cmp ? out.sort(cmp) : out;
}

/** Keep stages in the contract's display order even if the server sends a partial list. */
export function sortStages(stages: Stage[]): Stage[] {
  const order = (k: string) => {
    const i = (STAGE_KEYS as readonly string[]).indexOf(k);
    return i < 0 ? 999 : i;
  };
  return stages.slice().sort((a, b) => order(a.key) - order(b.key));
}

function asObject(x: unknown): Record<string, unknown> | null {
  return x && typeof x === "object" && !Array.isArray(x) ? (x as Record<string, unknown>) : null;
}

/**
 * Mutable working copy for one batch. Each slice is copied at most once per
 * batch (copy-on-write), so applying 2000 replayed events stays cheap.
 */
class Draft {
  s: AppState;
  private copied = new Set<string>();
  private logArrays = new Map<string, LogLine[]>();

  constructor(state: AppState) {
    this.s = { ...state };
  }

  private cow<K extends keyof AppState>(key: K): AppState[K] {
    if (!this.copied.has(key)) {
      const v = this.s[key];
      this.s[key] = (Array.isArray(v) ? v.slice() : { ...(v as object) }) as AppState[K];
      this.copied.add(key);
    }
    return this.s[key];
  }

  get runs() {
    return this.cow("runs");
  }
  get details() {
    return this.cow("details");
  }
  get approvals() {
    return this.cow("approvals");
  }
  get revisionVersions() {
    return this.cow("revisionVersions");
  }
  get vision() {
    return this.cow("vision");
  }

  appendLog(runKey: string, line: LogLine): void {
    let arr = this.logArrays.get(runKey);
    if (!arr) {
      arr = (this.s.logs[runKey] ?? []).slice();
      this.logArrays.set(runKey, arr);
    }
    arr.push(line);
  }

  finish(): AppState {
    if (this.logArrays.size) {
      const logs = { ...this.s.logs };
      for (const [k, arr] of this.logArrays) {
        logs[k] = arr.length > LOG_CAP_PER_RUN ? arr.slice(arr.length - LOG_CAP_PER_RUN) : arr;
      }
      this.s.logs = logs;
    }
    return this.s;
  }
}

function patchProjectFromRun(project: Project, run: Run, knownRuns: Record<string, Run>): Project {
  const latest = project.latest_run_id ? knownRuns[project.latest_run_id] : undefined;
  const isLatest =
    project.latest_run_id === run.id || !project.latest_run_id || !latest || run.created_at >= latest.created_at;
  if (!isLatest) return project;
  if (project.latest_run_id === run.id && project.latest_run_status === run.status) return project;
  return { ...project, latest_run_id: run.id, latest_run_status: run.status };
}

function putRun(d: Draft, run: Run): void {
  const prevRuns = d.s.runs;
  if (isStaleRun(prevRuns[run.id], run)) return;
  d.runs[run.id] = { ...run, stages: sortStages(run.stages ?? []) };
  const det = d.s.details[run.project_id];
  if (det) {
    const runIds = det.runIds.includes(run.id)
      ? det.runIds
      : [...det.runIds, run.id].sort((a, b) => (d.s.runs[a]?.created_at ?? 0) - (d.s.runs[b]?.created_at ?? 0));
    const project = patchProjectFromRun(det.project, run, prevRuns);
    if (runIds !== det.runIds || project !== det.project) d.details[run.project_id] = { ...det, runIds, project };
  }
  if (d.s.projects) {
    const i = d.s.projects.findIndex((p) => p.id === run.project_id);
    if (i >= 0) {
      const patched = patchProjectFromRun(d.s.projects[i], run, prevRuns);
      if (patched !== d.s.projects[i]) {
        const list = d.s.projects.slice();
        list[i] = patched;
        d.s.projects = list;
      }
    }
  }
}

function putStage(d: Draft, runId: string, stage: Partial<Stage> & { key: string }): void {
  const run = d.s.runs[runId];
  if (!run) return; // unknown run: the next resync brings it in full
  const i = run.stages.findIndex((s) => s.key === stage.key);
  let stages: Stage[];
  if (i >= 0) {
    stages = run.stages.slice();
    stages[i] = { ...stages[i], ...stage } as Stage;
  } else {
    stages = sortStages([...run.stages, { label: stage.key, status: "pending", summary: null, started_at: null, finished_at: null, detail: {}, ...stage } as Stage]);
  }
  d.runs[runId] = { ...run, stages };
}

function putMessage(d: Draft, msg: Message): void {
  const det = d.s.details[msg.project_id];
  if (!det) return;
  d.details[msg.project_id] = { ...det, messages: upsertById(det.messages, msg, byCreated) };
}

function putRevision(d: Draft, rev: RevisionSummary): void {
  d.revisionVersions[rev.id] = (d.s.revisionVersions[rev.id] ?? 0) + 1;
  const det = d.s.details[rev.project_id];
  if (!det) return;
  d.details[rev.project_id] = {
    ...det,
    revisions: upsertById(det.revisions, rev, (a, b) => a.number - b.number),
  };
}

/** Normalise the event payloads. docs/api.md is authoritative; the older
 * events.py docstring shapes are accepted as well so either backend form works. */
function applyOne(d: Draft, e: CLEvent, opts: ApplyOptions): void {
  const data = e.data ?? {};
  switch (e.type) {
    case "run.created":
    case "run.updated":
    case "run.finished": {
      const run = asObject(data.run) as Run | null;
      if (run && typeof run.id === "string") putRun(d, run);
      return;
    }
    case "stage.updated": {
      if (!e.run_id) return;
      const st = asObject(data.stage);
      if (st && typeof st.key === "string") {
        putStage(d, e.run_id, st as unknown as Stage);
      } else {
        const key = typeof data.stage === "string" ? data.stage : e.stage;
        if (key && typeof data.status === "string") {
          const patch: Partial<Stage> & { key: string } = { key, status: data.status as Stage["status"] };
          if (typeof data.summary === "string") patch.summary = data.summary;
          if (asObject(data.detail)) patch.detail = data.detail as Stage["detail"];
          putStage(d, e.run_id, patch);
        }
      }
      return;
    }
    case "log": {
      if (opts.skipLogs) return;
      const line = typeof data.line === "string" ? data.line : null;
      if (line === null) return;
      d.appendLog(e.run_id ?? GLOBAL_LOG_KEY, {
        seq: e.seq,
        ts: e.ts,
        stage: e.stage ?? (typeof data.stage === "string" ? data.stage : null),
        source: typeof data.source === "string" ? data.source : "unknown",
        line,
      });
      return;
    }
    case "agent.message": {
      const m = asObject(data.message);
      if (m && typeof m.id === "string") {
        putMessage(d, m as unknown as Message);
      } else if (typeof data.text === "string" && e.project_id) {
        putMessage(d, {
          id: `ev_${e.seq}`,
          project_id: e.project_id,
          run_id: e.run_id,
          role: (data.role as Message["role"]) ?? "agent",
          kind: typeof data.kind === "string" ? data.kind : "text",
          text: data.text,
          data: (asObject(data.data) as Message["data"]) ?? {},
          created_at: e.ts,
        });
      }
      return;
    }
    case "approval.requested":
    case "approval.resolved": {
      const a = asObject(data.approval) as Approval | null;
      if (a && typeof a.id === "string" && !isStaleApproval(d.s.approvals[a.id], a)) d.approvals[a.id] = a;
      return;
    }
    case "revision.created":
    case "revision.updated": {
      const r = asObject(data.revision) as RevisionSummary | null;
      if (r && typeof r.id === "string") putRevision(d, r);
      return;
    }
    case "environment.updated": {
      const env = asObject(data.environment) as Environment | null;
      if (env) {
        d.s.environment = env;
        d.s.environmentError = null;
        if (env.hardware) d.s.hardware = env.hardware;
      }
      return;
    }
    case "hardware.updated": {
      const hw = asObject(data.hardware) as HardwareStatus | null;
      if (hw) d.s.hardware = hw;
      return;
    }
    case "vision.updated": {
      const sess = asObject(data.session) as VisionSessionState | null;
      if (sess && typeof sess.id === "string") d.vision[sess.id] = sess;
      return;
    }
    default:
      return;
  }
}

export interface ApplyOptions {
  /** Re-applying buffered events after a REST merge: their log lines are already stored. */
  skipLogs?: boolean;
}

export function applyEvents(state: AppState, events: CLEvent[], opts: ApplyOptions = {}): AppState {
  if (!events.length) return state;
  const d = new Draft(state);
  for (const e of events) applyOne(d, e, opts);
  const out = d.finish();
  out.lastEventAt = events[events.length - 1].ts ?? out.lastEventAt;
  return out;
}

// ---------------------------------------------------------------- REST merges

export function mergeProjectDetail(state: AppState, resp: ProjectDetailResponse, now = Date.now()): AppState {
  const runs = { ...state.runs };
  for (const r of resp.runs) {
    if (isStaleRun(state.runs[r.id], r)) continue;
    runs[r.id] = { ...r, stages: sortStages(r.stages ?? []) };
  }
  const runIds = resp.runs.slice().sort(byCreated).map((r) => r.id);
  const detail: ProjectDetail = {
    project: resp.project,
    revisions: resp.revisions.slice().sort((a, b) => a.number - b.number),
    runIds,
    messages: resp.messages.slice().sort(byCreated),
    loadedAt: now,
  };
  const projects = state.projects
    ? state.projects.some((p) => p.id === resp.project.id)
      ? state.projects.map((p) => (p.id === resp.project.id ? resp.project : p))
      : [resp.project, ...state.projects]
    : state.projects;
  let projectErrors = state.projectErrors;
  if (projectErrors[resp.project.id]) {
    projectErrors = { ...projectErrors };
    delete projectErrors[resp.project.id];
  }
  return { ...state, runs, projects, projectErrors, details: { ...state.details, [resp.project.id]: detail } };
}

export function mergeProjects(state: AppState, list: Project[]): AppState {
  const sorted = list.slice().sort((a, b) => b.updated_at - a.updated_at || b.created_at - a.created_at);
  return { ...state, projects: sorted, projectsError: null };
}

/**
 * Pending approvals from the server are the truth for "what is pending". Any
 * approval we hold as pending that the server no longer lists was decided (or
 * expired) while we were away; drop it rather than show a stale card.
 */
export function mergePendingApprovals(state: AppState, pending: Approval[]): AppState {
  const approvals: Record<string, Approval> = {};
  for (const a of Object.values(state.approvals)) if (a.status !== "pending") approvals[a.id] = a;
  for (const a of pending) if (!isStaleApproval(approvals[a.id], a)) approvals[a.id] = a;
  return { ...state, approvals, approvalsLoaded: true };
}

/** GET /api/approvals (no filter): fills in decided approvals referenced by chat history. */
export function mergeAllApprovals(state: AppState, all: Approval[]): AppState {
  const approvals = { ...state.approvals };
  for (const a of all) if (!isStaleApproval(approvals[a.id], a)) approvals[a.id] = a;
  return { ...state, approvals };
}

export function putApproval(state: AppState, a: Approval): AppState {
  if (isStaleApproval(state.approvals[a.id], a)) return state;
  return { ...state, approvals: { ...state.approvals, [a.id]: a } };
}

export function putRunState(state: AppState, run: Run): AppState {
  const d = new Draft(state);
  putRun(d, run);
  return d.finish();
}

export function putMessageState(state: AppState, msg: Message): AppState {
  const d = new Draft(state);
  putMessage(d, msg);
  return d.finish();
}

/** Merge historic run events (GET /api/runs/{id}/events) into the log store, by seq. */
export function mergeRunLogs(state: AppState, runId: string, events: CLEvent[]): AppState {
  const incoming: LogLine[] = [];
  for (const e of events) {
    if (e.type !== "log" || typeof e.data?.line !== "string") continue;
    incoming.push({
      seq: e.seq,
      ts: e.ts,
      stage: e.stage,
      source: typeof e.data.source === "string" ? e.data.source : "unknown",
      line: e.data.line,
    });
  }
  if (!incoming.length) return state;
  const existing = state.logs[runId] ?? [];
  const seen = new Set(existing.map((l) => l.seq));
  const merged = existing.concat(incoming.filter((l) => !seen.has(l.seq))).sort((a, b) => a.seq - b.seq);
  const capped = merged.length > LOG_CAP_PER_RUN ? merged.slice(merged.length - LOG_CAP_PER_RUN) : merged;
  return { ...state, logs: { ...state.logs, [runId]: capped } };
}

// ---------------------------------------------------------------- derived selections

/** The revision the workspace tabs show when the user has not picked one. */
export function effectiveRevisionId(state: AppState): string | null {
  const sel = state.selection;
  if (sel.revisionId) return sel.revisionId;
  const det = sel.projectId ? state.details[sel.projectId] : undefined;
  if (!det) return null;
  const latestRun = det.project.latest_run_id ? state.runs[det.project.latest_run_id] : undefined;
  if (latestRun && (latestRun.status === "running" || latestRun.status === "waiting") && latestRun.revision_id) {
    return latestRun.revision_id;
  }
  return det.project.current_revision_id ?? det.revisions[det.revisions.length - 1]?.id ?? latestRun?.revision_id ?? null;
}

export function effectiveRunId(state: AppState): string | null {
  const sel = state.selection;
  if (sel.runId) return sel.runId;
  const det = sel.projectId ? state.details[sel.projectId] : undefined;
  if (!det) return null;
  return det.project.latest_run_id ?? det.runIds[det.runIds.length - 1] ?? null;
}

/** Latest run (by created_at) whose revisions include `revisionId`. */
export function runForRevision(state: AppState, projectId: string | null, revisionId: string | null): Run | null {
  if (!projectId || !revisionId) return null;
  const det = state.details[projectId];
  if (!det) return null;
  let best: Run | null = null;
  for (const id of det.runIds) {
    const r = state.runs[id];
    if (!r) continue;
    if (r.revision_id === revisionId || r.revision_ids?.includes(revisionId)) {
      if (!best || r.created_at >= best.created_at) best = r;
    }
  }
  return best;
}
