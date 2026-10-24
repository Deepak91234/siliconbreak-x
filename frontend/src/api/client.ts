/**
 * Typed REST client for docs/api.md.
 *
 * Every call goes to the same origin (`/api/...`), which the Vite dev server
 * proxies to FastAPI. Failures are normalised into ApiError so views can tell
 * "backend down" (retry later, show the banner) apart from "404 - nothing there
 * yet" (show an empty state) and real errors (show the server's `detail`).
 */

import type {
  Approval,
  ApprovalStatus,
  CLEvent,
  DiffResponse,
  Environment,
  HardwareStatus,
  FaultInfo,
  Project,
  ProjectDetailResponse,
  Revision,
  Run,
  RunOptions,
  Message,
  SimulationReport,
  VerificationResult,
  VisionSessionState,
  VivadoBuildResult,
  Waveform,
  LedRoi,
  BoardDef,
  Diagram,
  ExampleInfo,
  Guide,
  LlmStatus,
  ScenarioTrace,
  VirtualEvent,
  VirtualResult,
} from "./types";

export type ApiErrorKind = "unreachable" | "not_found" | "conflict" | "validation" | "server" | "client";

export class ApiError extends Error {
  readonly kind: ApiErrorKind;
  readonly status: number | null;
  readonly path: string;

  constructor(kind: ApiErrorKind, status: number | null, message: string, path: string) {
    super(message);
    this.name = "ApiError";
    this.kind = kind;
    this.status = status;
    this.path = path;
  }

  get isNotFound(): boolean {
    return this.kind === "not_found";
  }
  get isUnreachable(): boolean {
    return this.kind === "unreachable";
  }
}

export function isApiError(e: unknown): e is ApiError {
  return e instanceof ApiError;
}

export function errorMessage(e: unknown): string {
  if (e instanceof Error) return e.message;
  return String(e);
}

/** Observers of backend reachability (the connection banner subscribes). */
type ReachabilityListener = (reachable: boolean, error: ApiError | null) => void;
const reachabilityListeners = new Set<ReachabilityListener>();
export function onReachability(fn: ReachabilityListener): () => void {
  reachabilityListeners.add(fn);
  return () => reachabilityListeners.delete(fn);
}
function report(reachable: boolean, error: ApiError | null) {
  for (const fn of reachabilityListeners) fn(reachable, error);
}

/** FastAPI validation errors carry `detail` as a list of {loc,msg}. */
export function detailText(detail: unknown): string | null {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const parts = detail.map((d) => {
      if (d && typeof d === "object" && "msg" in d) {
        const loc = Array.isArray((d as { loc?: unknown[] }).loc) ? (d as { loc: unknown[] }).loc.join(".") : "";
        return `${loc ? loc + ": " : ""}${String((d as { msg: unknown }).msg)}`;
      }
      return JSON.stringify(d);
    });
    return parts.join("; ");
  }
  if (detail && typeof detail === "object") return JSON.stringify(detail);
  return null;
}

/** Map an HTTP status (+ parsed body) to an ApiError kind. Exported for tests. */
export function classifyResponse(status: number, body: unknown): ApiErrorKind {
  const b = body as { backend_down?: boolean } | null;
  if (b && b.backend_down) return "unreachable";
  if (status === 404) return "not_found";
  if (status === 409) return "conflict";
  if (status === 422) return "validation";
  // The dev proxy answers 502/503/504 (or an empty 500) when FastAPI is not listening.
  if (status === 502 || status === 503 || status === 504) return "unreachable";
  if (status === 500 && body === null) return "unreachable";
  if (status >= 500) return "server";
  return "client";
}

interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "DELETE";
  body?: unknown;
  signal?: AbortSignal;
  timeoutMs?: number;
}

export async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(new DOMException("timeout", "TimeoutError")), opts.timeoutMs ?? 30_000);
  const onAbort = () => controller.abort(opts.signal?.reason);
  opts.signal?.addEventListener("abort", onAbort);
  let res: Response;
  try {
    res = await fetch(path, {
      method: opts.method ?? "GET",
      headers: opts.body !== undefined ? { "Content-Type": "application/json", Accept: "application/json" } : { Accept: "application/json" },
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
      signal: controller.signal,
    });
  } catch (e) {
    if (opts.signal?.aborted) throw e; // caller cancelled; not a connectivity signal
    const err = new ApiError("unreachable", null, `Backend unreachable (${errorMessage(e)})`, path);
    report(false, err);
    throw err;
  } finally {
    clearTimeout(timeout);
    opts.signal?.removeEventListener("abort", onAbort);
  }

  const text = await res.text();
  let body: unknown = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = null;
    }
  }
  if (!res.ok) {
    const kind = classifyResponse(res.status, body);
    const detail = detailText((body as { detail?: unknown } | null)?.detail) ?? (text.slice(0, 300) || res.statusText);
    const err = new ApiError(kind, res.status, detail || `HTTP ${res.status}`, path);
    report(kind !== "unreachable", kind === "unreachable" ? err : null);
    throw err;
  }
  report(true, null);
  if (text && body === null) {
    throw new ApiError("server", res.status, `Invalid JSON from ${path}`, path);
  }
  return body as T;
}

const enc = encodeURIComponent;

/** Build a /api/files URL for a path under runs/ (each segment encoded, slashes kept). */
export function fileUrl(relPath: string): string {
  return "/api/files/" + relPath.split("/").filter(Boolean).map(enc).join("/");
}

/**
 * Artifact paths in results are absolute host paths; /api/files serves paths
 * relative to runs/. Returns null when the path is outside the runs directory
 * (the link would be refused by the server's path guard anyway).
 */
export function runsRelative(path: string | null | undefined, runsDir: string | null | undefined): string | null {
  if (!path) return null;
  if (!path.startsWith("/")) return path.replace(/^\.?\/*/, "").replace(/^runs\//, "");
  if (!runsDir) return null;
  const root = runsDir.replace(/\/+$/, "") + "/";
  return path.startsWith(root) ? path.slice(root.length) : null;
}

export interface CreateProjectResponse { project: Project; run: Run }
export interface PostMessageResponse { run: Run; message: Message }

export const api = {
  health: (signal?: AbortSignal) => request<{ ok: boolean; version: string }>("/api/health", { signal, timeoutMs: 5000 }),

  environment: () => request<Environment>("/api/environment"),
  refreshEnvironment: () => request<Environment>("/api/environment/refresh", { method: "POST", timeoutMs: 90_000 }),
  startVivado: () => request<Approval>("/api/vivado/start", { method: "POST" }),
  stopHeadlessVivado: () => request<{ ok: boolean; message: string }>("/api/vivado/stop-headless", { method: "POST", timeoutMs: 90_000 }),

  approvals: (status?: ApprovalStatus) =>
    request<Approval[]>(`/api/approvals${status ? `?status=${enc(status)}` : ""}`),
  approve: (id: string) => request<Approval>(`/api/approvals/${enc(id)}/approve`, { method: "POST", timeoutMs: 120_000 }),
  deny: (id: string) => request<Approval>(`/api/approvals/${enc(id)}/deny`, { method: "POST" }),

  projects: () => request<Project[]>("/api/projects"),
  createProject: (text: string, options: RunOptions) =>
    request<CreateProjectResponse>("/api/projects", { method: "POST", body: { text, options }, timeoutMs: 60_000 }),
  project: (id: string, signal?: AbortSignal) => request<ProjectDetailResponse>(`/api/projects/${enc(id)}`, { signal }),
  postMessage: (projectId: string, text: string, options: RunOptions) =>
    request<PostMessageResponse>(`/api/projects/${enc(projectId)}/messages`, { method: "POST", body: { text, options }, timeoutMs: 60_000 }),

  revision: (id: string, signal?: AbortSignal) => request<Revision>(`/api/revisions/${enc(id)}`, { signal }),
  revisionFile: (id: string, name: string, signal?: AbortSignal) =>
    request<{ name: string; role: string; content: string }>(`/api/revisions/${enc(id)}/files/${enc(name)}`, { signal }),
  revisionDiff: (id: string, against?: string | null, signal?: AbortSignal) =>
    request<DiffResponse>(`/api/revisions/${enc(id)}/diff${against ? `?against=${enc(against)}` : ""}`, { signal }),
  simulation: (id: string, signal?: AbortSignal) => request<SimulationReport>(`/api/revisions/${enc(id)}/simulation`, { signal }),
  waveform: (
    id: string,
    q: { scenario: string; start?: number | null; end?: number | null; signals?: string[] },
    signal?: AbortSignal,
  ) => {
    const p = new URLSearchParams({ scenario: q.scenario });
    if (q.start != null) p.set("start", String(Math.floor(q.start)));
    if (q.end != null) p.set("end", String(Math.ceil(q.end)));
    if (q.signals && q.signals.length) p.set("signals", q.signals.join(","));
    return request<Waveform>(`/api/revisions/${enc(id)}/waveform?${p.toString()}`, { signal, timeoutMs: 60_000 });
  },
  build: (id: string, signal?: AbortSignal) => request<VivadoBuildResult>(`/api/revisions/${enc(id)}/build`, { signal }),
  buildLog: (id: string, tail = 2000, signal?: AbortSignal) =>
    request<{ lines: string[]; total: number }>(`/api/revisions/${enc(id)}/build/log?tail=${tail}`, { signal }),
  report: (id: string, name: string, signal?: AbortSignal) =>
    request<{ name: string; content: string }>(`/api/revisions/${enc(id)}/reports/${enc(name)}`, { signal }),
  rebuild: (id: string) => request<Run>(`/api/revisions/${enc(id)}/rebuild`, { method: "POST" }),
  program: (id: string) => request<Approval>(`/api/revisions/${enc(id)}/program`, { method: "POST" }),

  run: (id: string, signal?: AbortSignal) => request<Run>(`/api/runs/${enc(id)}`, { signal }),
  runEvents: (id: string, after = 0, limit = 2000, signal?: AbortSignal) =>
    request<CLEvent[]>(`/api/runs/${enc(id)}/events?after=${after}&limit=${limit}`, { signal }),
  cancelRun: (id: string) => request<Run>(`/api/runs/${enc(id)}/cancel`, { method: "POST" }),

  hardware: () => request<HardwareStatus>("/api/hardware"),
  detectHardware: () => request<HardwareStatus>("/api/hardware/detect", { method: "POST", timeoutMs: 90_000 }),
  stopXvcd: (pid: number) => request<Approval>("/api/hardware/stop-xvcd", { method: "POST", body: { pid } }),

  createVisionSession: (revisionId: string) =>
    request<VisionSessionState>("/api/vision/sessions", { method: "POST", body: { revision_id: revisionId } }),
  visionSession: (id: string) => request<VisionSessionState>(`/api/vision/sessions/${enc(id)}`),
  setRoi: (id: string, leds: LedRoi[]) =>
    request<VisionSessionState>(`/api/vision/sessions/${enc(id)}/roi`, { method: "PUT", body: { leds } }),
  autoRoi: (id: string, box: [number, number, number, number], count: number, firstLed: number) =>
    request<VisionSessionState>(`/api/vision/sessions/${enc(id)}/auto-roi`, {
      method: "POST",
      body: { box, count, first_led: firstLed },
    }),
  startPhase: (id: string, phaseId: string) =>
    request<VisionSessionState>(`/api/vision/sessions/${enc(id)}/phase`, { method: "POST", body: { action: "start", phase_id: phaseId } }),
  endPhase: (id: string) =>
    request<VisionSessionState>(`/api/vision/sessions/${enc(id)}/phase`, { method: "POST", body: { action: "end" } }),
  finishVision: (id: string) =>
    request<VerificationResult>(`/api/vision/sessions/${enc(id)}/finish`, { method: "POST", timeoutMs: 120_000 }),

  /**
   * Fault-injection catalogue. Served by routes.py but not yet in docs/api.md;
   * a 404 means "no catalogue" and the test-mode selector is disabled.
   */
  faults: () => request<FaultInfo[]>("/api/faults"),

  // hardware guide, board twin, virtual FPGA, diagram, AI provider (backend/api/guide_routes.py)
  board: () => request<BoardDef>("/api/board"),
  guide: (revisionId: string, signal?: AbortSignal) => request<Guide>(`/api/revisions/${enc(revisionId)}/guide`, { signal }),
  examples: () => request<ExampleInfo[]>("/api/examples"),
  exampleGuide: (name: string, signal?: AbortSignal) => request<Guide>(`/api/examples/${enc(name)}/guide`, { signal }),
  scenarioTrace: (revisionId: string, scenarioId: string, signal?: AbortSignal) =>
    request<ScenarioTrace>(`/api/revisions/${enc(revisionId)}/scenarios/${enc(scenarioId)}/trace`, { signal }),
  simulate: (revisionId: string, events: VirtualEvent[], durationS: number, signal?: AbortSignal) =>
    request<VirtualResult>(`/api/revisions/${enc(revisionId)}/simulate`, {
      method: "POST", body: { events, duration_s: durationS }, signal, timeoutMs: 60_000,
    }),
  diagram: (revisionId: string, signal?: AbortSignal) => request<Diagram>(`/api/revisions/${enc(revisionId)}/diagram`, { signal }),
  llm: () => request<LlmStatus>("/api/llm"),
  llmReload: () => request<LlmStatus>("/api/llm/reload", { method: "POST", timeoutMs: 90_000 }),
};

export type Api = typeof api;
