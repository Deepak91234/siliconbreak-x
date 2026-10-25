/**
 * The dashboard's single source of client state, fed only by the REST API and
 * the `/ws/events` stream.
 *
 * Consistency model:
 *  - Events are applied in `seq` order as they arrive (reducer.applyEvents).
 *  - REST snapshots are merged with `tracked()`: every event received while the
 *    request was in flight is re-applied on top of the snapshot, so a response
 *    computed slightly earlier on the server cannot erase a newer live update.
 *  - On every (re)connect the server first replays persisted events with
 *    seq > after. We wait for that burst to go quiet before resyncing from REST,
 *    so the snapshot lands after the (older) replay rather than under it.
 *  - The reducer additionally refuses snapshots that would move a finished run
 *    back to running, or a decided approval back to pending.
 *
 * When the backend is down every request reports "unreachable"; the store then
 * polls /api/health with backoff and resyncs as soon as it answers again.
 */

import { createContext, useContext, useSyncExternalStore } from "react";
import { api, ApiError, errorMessage, onReachability } from "../api/client";
import { EventStream, eventsUrl } from "../api/events";
import type { Approval, CLEvent, Environment, HardwareStatus, RunOptions, VisionSessionState } from "../api/types";
import { WORKSPACE_TABS } from "../lib/sections";
import { securityApi, type SecurityAudit } from "../api/security";
import {
  applyEvents,
  initialState,
  isRunActive,
  mergeAllApprovals,
  mergePendingApprovals,
  mergeProjectDetail,
  mergeProjects,
  mergeRunLogs,
  putApproval,
  putMessageState,
  putRunState,
  type AppState,
  type Selection,
  type TabKey,
  putSecurityAudits,
} from "./reducer";

const SEQ_KEY = "siliconbreak:lastSeq";
const TABS: TabKey[] = ["pipeline", "verilog", "simulation", "diagram", "vivado", "hardware", "vision", "history", "dashboard", "security", "bench", "fpga"];

function readSeq(): number {
  try {
    const v = Number(window.localStorage.getItem(SEQ_KEY));
    return Number.isFinite(v) && v > 0 ? Math.floor(v) : 0;
  } catch {
    return 0;
  }
}

function writeSeq(seq: number): void {
  try {
    window.localStorage.setItem(SEQ_KEY, String(seq));
  } catch {
    /* storage unavailable (private window): resume point is per page load only */
  }
}

/** Selection <-> location hash (`#tab=vivado&p=<id>&rev=<id>&run=<id>&a=<audit>`), so reloads keep context. */
export function selectionFromHash(hash: string): Partial<Selection> {
  const q = new URLSearchParams(hash.replace(/^#\/?/, ""));
  const tab = q.get("tab") as TabKey | null;
  return {
    projectId: q.get("p") || null,
    revisionId: q.get("rev") || null,
    runId: q.get("run") || null,
    tab: tab && TABS.includes(tab) ? (tab === "dashboard" ? "security" : tab) : "security",
    auditId: q.get("a") || null,
  };
}

export function selectionToHash(sel: Selection): string {
  const q = new URLSearchParams();
  if (sel.projectId) q.set("p", sel.projectId);
  if (sel.tab !== "security") q.set("tab", sel.tab);
  if (sel.revisionId) q.set("rev", sel.revisionId);
  if (sel.runId) q.set("run", sel.runId);
  if (sel.auditId) q.set("a", sel.auditId);
  const s = q.toString();
  return s ? `#${s}` : "";
}

type Timer = ReturnType<typeof setTimeout>;

export class AppStore {
  private state: AppState = initialState();
  private listeners = new Set<() => void>();
  private stream: EventStream | null = null;
  private unsubReach: (() => void) | null = null;
  private started = false;

  private inflight = 0;
  private journal: CLEvent[] = [];

  private awaitingQuiet = false;
  private quietDeadline = 0;
  private quietTimer: Timer | null = null;

  private healthTimer: Timer | null = null;
  private healthDelay = 2000;
  private tick: ReturnType<typeof setInterval> | null = null;
  private lastEnvPoll = 0;
  private lastProjectPoll = 0;
  private projectsTimer: Timer | null = null;
  private seqSavedAt = 0;
  private backfilled = new Set<string>();
  private onHash = () => this.applyHash();
  private onUnload = () => writeSeq(this.state.socket.lastSeq);

  getState = (): AppState => this.state;

  subscribe = (fn: () => void): (() => void) => {
    this.listeners.add(fn);
    return () => {
      this.listeners.delete(fn);
    };
  };

  private set(next: AppState): void {
    if (next === this.state) return;
    this.state = next;
    for (const fn of this.listeners) fn();
  }

  private update(fn: (s: AppState) => AppState): void {
    this.set(fn(this.state));
  }

  // ------------------------------------------------------------ lifecycle

  start(): void {
    if (this.started) return;
    this.started = true;
    const fromHash = selectionFromHash(window.location.hash);
    this.update((s) => ({ ...s, selection: { ...s.selection, ...fromHash, compareAgainst: null } }));
    this.unsubReach = onReachability((ok, err) => this.onReachability(ok, err));
    this.stream = new EventStream({
      url: (after) => eventsUrl(after),
      initialSeq: readSeq(),
      onEvents: (events) => this.onEvents(events),
      onInfo: (info) => this.update((s) => (s.socket === info ? s : { ...s, socket: info })),
      onOpen: () => this.onSocketOpen(),
    });
    this.stream.start();
    window.addEventListener("hashchange", this.onHash);
    window.addEventListener("beforeunload", this.onUnload);
    this.tick = setInterval(() => this.onTick(), 5000);
    void this.resync();
    void this.loadFaults();
  }

  stop(): void {
    if (!this.started) return;
    this.started = false;
    this.stream?.stop();
    this.stream = null;
    this.unsubReach?.();
    this.unsubReach = null;
    for (const t of [this.quietTimer, this.healthTimer, this.projectsTimer]) if (t) clearTimeout(t);
    this.quietTimer = this.healthTimer = this.projectsTimer = null;
    if (this.tick) clearInterval(this.tick);
    this.tick = null;
    window.removeEventListener("hashchange", this.onHash);
    window.removeEventListener("beforeunload", this.onUnload);
    writeSeq(this.state.socket.lastSeq);
  }

  // ------------------------------------------------------------ connectivity

  private onReachability(ok: boolean, err: ApiError | null): void {
    const b = this.state.backend;
    if (ok) {
      const recovered = b.reachable === false;
      if (b.reachable !== true || b.error !== null) {
        this.update((s) => ({ ...s, backend: { ...s.backend, reachable: true, error: null, lastOkAt: Date.now() } }));
      }
      if (this.healthTimer) clearTimeout(this.healthTimer);
      this.healthTimer = null;
      this.healthDelay = 2000;
      if (recovered) {
        this.stream?.retryNow();
        void this.resync();
      }
    } else {
      const msg = err?.message ?? "backend unreachable";
      if (b.reachable !== false || b.error !== msg) {
        this.update((s) => ({ ...s, backend: { ...s.backend, reachable: false, error: msg } }));
      }
      this.scheduleHealth();
    }
  }

  private scheduleHealth(): void {
    if (this.healthTimer || !this.started) return;
    this.healthTimer = setTimeout(() => {
      this.healthTimer = null;
      void this.checkHealth();
    }, this.healthDelay);
    this.healthDelay = Math.min(10_000, Math.round(this.healthDelay * 1.6));
  }

  private async checkHealth(): Promise<void> {
    try {
      const h = await api.health();
      this.update((s) => ({ ...s, backend: { ...s.backend, version: h.version ?? null } }));
    } catch {
      this.scheduleHealth();
    }
  }

  /** The banner's "Retry now": probe the backend and the socket immediately. */
  retryNow(): void {
    if (this.healthTimer) clearTimeout(this.healthTimer);
    this.healthTimer = null;
    this.healthDelay = 2000;
    this.stream?.retryNow();
    void this.checkHealth().then(() => {
      if (this.state.backend.reachable) void this.resync();
    });
  }

  private onSocketOpen(): void {
    this.awaitingQuiet = true;
    this.quietDeadline = Date.now() + 2500;
    this.armQuiet();
    if (this.state.backend.version === null) void this.checkHealth();
  }

  private armQuiet(): void {
    if (this.quietTimer) clearTimeout(this.quietTimer);
    const delay = Math.min(300, this.quietDeadline - Date.now());
    const fire = () => {
      this.quietTimer = null;
      if (!this.awaitingQuiet) return;
      this.awaitingQuiet = false;
      void this.resync();
    };
    if (delay <= 0) fire();
    else this.quietTimer = setTimeout(fire, delay);
  }

  private onEvents(events: CLEvent[]): void {
    if (this.inflight > 0) this.journal.push(...events);
    this.set(applyEvents(this.state, events));
    if (this.awaitingQuiet) this.armQuiet();
    const now = Date.now();
    if (now - this.seqSavedAt > 2000) {
      this.seqSavedAt = now;
      writeSeq(this.state.socket.lastSeq);
    }
    const projects = this.state.projects;
    for (const e of events) {
      // A project created elsewhere (another tab): refresh the list.
      if (e.type === "run.created" && e.project_id && projects && !projects.some((p) => p.id === e.project_id)) {
        this.scheduleProjectsRefetch();
        break;
      }
    }
  }

  private scheduleProjectsRefetch(): void {
    if (this.projectsTimer) return;
    this.projectsTimer = setTimeout(() => {
      this.projectsTimer = null;
      void this.loadProjects();
    }, 400);
  }

  private onTick(): void {
    const now = Date.now();
    const s = this.state;
    if (s.backend.reachable === false) return;
    if (now - this.lastEnvPoll > 30_000) {
      this.lastEnvPoll = now;
      void this.loadEnvironment();
    }
    const pid = s.selection.projectId;
    if (!pid) return;
    const det = s.details[pid];
    const latest = det?.project.latest_run_id ? s.runs[det.project.latest_run_id] : undefined;
    const socketDown = s.socket.status !== "open";
    // Safety nets: without live events poll every 10 s; during an active run
    // re-read the project every 15 s in case the server dropped our subscription.
    const interval = socketDown ? 10_000 : isRunActive(latest) ? 15_000 : 0;
    if (interval && now - this.lastProjectPoll > interval) {
      this.lastProjectPoll = now;
      void this.loadProject(pid).catch(() => undefined);
      if (socketDown) void this.loadPendingApprovals();
    }
  }

  // ------------------------------------------------------------ REST merges

  /**
   * Run a REST read and merge it, re-applying the events that arrived while it
   * was in flight (their log lines are already stored, so logs are skipped).
   */
  private async tracked<T>(fetch: () => Promise<T>, merge: (s: AppState, r: T) => AppState): Promise<T> {
    this.inflight++;
    const start = this.journal.length;
    try {
      const r = await fetch();
      let s = merge(this.state, r);
      const missed = this.journal.slice(start);
      if (missed.length) s = applyEvents(s, missed, { skipLogs: true });
      this.set(s);
      return r;
    } finally {
      this.inflight--;
      if (this.inflight === 0) this.journal = [];
    }
  }

  async resync(): Promise<void> {
    const pid = this.state.selection.projectId;
    this.lastEnvPoll = Date.now();
    this.lastProjectPoll = Date.now();
    await Promise.allSettled([
      this.loadEnvironment(),
      this.loadProjects(),
      this.loadPendingApprovals(),
      pid ? this.loadProject(pid) : Promise.resolve(),
    ]);
  }

  async loadEnvironment(force = false): Promise<Environment | null> {
    try {
      return await this.tracked(
        () => (force ? api.refreshEnvironment() : api.environment()),
        (s, env) => ({ ...s, environment: env, environmentError: null, hardware: env.hardware ?? s.hardware }),
      );
    } catch (e) {
      this.update((s) => ({ ...s, environmentError: errorMessage(e) }));
      if (force) throw e;
      return null;
    }
  }

  async loadProjects(): Promise<void> {
    try {
      await this.tracked(() => api.projects(), mergeProjects);
    } catch (e) {
      this.update((s) => ({ ...s, projectsError: errorMessage(e) }));
    }
  }

  async loadPendingApprovals(): Promise<void> {
    try {
      await this.tracked(() => api.approvals("pending"), mergePendingApprovals);
    } catch {
      /* reachability is reported by the client; cards keep their last known state */
    }
  }

  /** Recent decided approvals too (what was last programmed onto the board, for the FPGA status). */
  async loadAllApprovals(): Promise<void> {
    try {
      await this.tracked(() => api.approvals(), mergeAllApprovals);
    } catch {
      /* optional enrichment */
    }
  }

  async loadProject(id: string): Promise<void> {
    try {
      await this.tracked(() => api.project(id), (s, resp) => mergeProjectDetail(s, resp));
    } catch (e) {
      this.update((s) => ({ ...s, projectErrors: { ...s.projectErrors, [id]: errorMessage(e) } }));
      throw e;
    }
    // Decided approvals referenced by chat history (the pending list omits them).
    try {
      await this.tracked(() => api.approvals(), mergeAllApprovals);
    } catch {
      /* optional enrichment */
    }
  }

  async loadHardware(): Promise<HardwareStatus | null> {
    try {
      return await this.tracked(() => api.hardware(), (s, hw) => ({ ...s, hardware: hw }));
    } catch {
      return null;
    }
  }

  async loadFaults(): Promise<void> {
    try {
      const list = await api.faults();
      this.update((s) => ({ ...s, faults: Array.isArray(list) ? list : [], faultsError: null }));
    } catch (e) {
      this.update((s) => ({
        ...s,
        faults: e instanceof ApiError && e.isNotFound ? [] : s.faults,
        faultsError: e instanceof ApiError && e.isNotFound ? "this backend has no fault catalogue" : errorMessage(e),
      }));
    }
  }

  /**
   * After a page reload the live log of a run is only in the event store. Page
   * through GET /api/runs/{id}/events once per run and merge the log lines.
   */
  async backfillRunLogs(runId: string): Promise<void> {
    if (this.backfilled.has(runId)) return;
    this.backfilled.add(runId);
    let after = 0;
    try {
      for (let page = 0; page < 10; page++) {
        const events = await api.runEvents(runId, after, 2000);
        if (!events.length) break;
        this.update((s) => mergeRunLogs(s, runId, events));
        after = events[events.length - 1].seq;
        if (events.length < 2000) break;
      }
    } catch {
      this.backfilled.delete(runId); // retry next time the view asks
    }
  }

  // ------------------------------------------------------------ selection

  private writeHash(): void {
    const h = selectionToHash(this.state.selection);
    if (h !== window.location.hash && !(h === "" && window.location.hash === "")) {
      window.history.replaceState(null, "", h || window.location.pathname + window.location.search);
    }
  }

  private applyHash(): void {
    const fromHash = selectionFromHash(window.location.hash);
    const cur = this.state.selection;
    if (
      fromHash.projectId === cur.projectId && fromHash.tab === cur.tab &&
      fromHash.revisionId === cur.revisionId && fromHash.runId === cur.runId && fromHash.auditId === cur.auditId
    ) return;
    this.update((s) => ({ ...s, selection: { ...s.selection, ...fromHash, compareAgainst: null } }));
    if (fromHash.projectId) void this.loadProject(fromHash.projectId).catch(() => undefined);
  }

  private select(patch: Partial<Selection>): void {
    this.update((s) => ({ ...s, selection: { ...s.selection, ...patch } }));
    this.writeHash();
  }

  selectProject(id: string | null): void {
    if (id === this.state.selection.projectId) {
      if (id) void this.loadProject(id).catch(() => undefined);
      return;
    }
    this.select({ projectId: id, revisionId: null, runId: null, compareAgainst: null });
    if (id) void this.loadProject(id).catch(() => undefined);
  }

  selectRevision(id: string | null): void {
    this.select({ revisionId: id, compareAgainst: null });
  }

  selectRun(id: string | null): void {
    this.select({ runId: id });
  }

  setTab(tab: TabKey): void {
    if (tab !== this.state.selection.tab) this.select({ tab });
  }

  /** Open an audit in the Security Lab (null = the overview). */
  selectAudit(id: string | null, tab: TabKey = "security"): void {
    this.select({ auditId: id, tab });
  }

  /** History "compare": diff `target` against `base` in the Verilog tab. */
  compare(base: string, target: string): void {
    this.select({ revisionId: target, compareAgainst: base, tab: "verilog" });
  }

  clearCompare(): void {
    if (this.state.selection.compareAgainst) this.select({ compareAgainst: null });
  }

  // ------------------------------------------------------------ actions (all go through the API)

  async createProject(text: string, options: RunOptions): Promise<void> {
    const r = await api.createProject(text, options);
    this.update((s) => {
      const projects = s.projects ? [r.project, ...s.projects.filter((p) => p.id !== r.project.id)] : [r.project];
      return putRunState({ ...s, projects }, r.run);
    });
    const tab = WORKSPACE_TABS.includes(this.state.selection.tab) ? this.state.selection.tab : "pipeline";
    this.select({ projectId: r.project.id, revisionId: null, runId: null, compareAgainst: null, tab });
    await this.loadProject(r.project.id).catch(() => undefined);
  }

  async postMessage(projectId: string, text: string, options: RunOptions): Promise<void> {
    const r = await api.postMessage(projectId, text, options);
    this.update((s) => putMessageState(putRunState(s, r.run), r.message));
    if (this.state.selection.projectId === projectId) this.select({ runId: null, revisionId: null, compareAgainst: null });
  }

  async decideApproval(id: string, approve: boolean): Promise<Approval> {
    try {
      const a = approve ? await api.approve(id) : await api.deny(id);
      this.update((s) => putApproval(s, a));
      return a;
    } catch (e) {
      // 409 = already decided elsewhere: refresh so the card shows the real state.
      void this.loadPendingApprovals();
      void api.approvals().then((all) => this.update((s) => mergeAllApprovals(s, all)), () => undefined);
      throw e;
    }
  }

  async requestVivadoStart(): Promise<Approval> {
    const a = await api.startVivado();
    this.update((s) => putApproval(s, a));
    return a;
  }

  async stopHeadlessVivado(): Promise<{ ok: boolean; message: string }> {
    const r = await api.stopHeadlessVivado();
    void this.loadEnvironment(true).catch(() => undefined);
    return r;
  }

  async detectHardware(): Promise<HardwareStatus> {
    const hw = await api.detectHardware();
    this.update((s) => ({ ...s, hardware: hw }));
    return hw;
  }

  async requestProgram(revisionId: string): Promise<Approval> {
    const a = await api.program(revisionId);
    this.update((s) => putApproval(s, a));
    return a;
  }

  async requestStopXvcd(pid: number): Promise<Approval> {
    const a = await api.stopXvcd(pid);
    this.update((s) => putApproval(s, a));
    return a;
  }

  async rebuild(revisionId: string): Promise<void> {
    const run = await api.rebuild(revisionId);
    this.update((s) => putRunState(s, run));
    this.select({ runId: null });
  }

  async cancelRun(runId: string): Promise<void> {
    const run = await api.cancelRun(runId);
    this.update((s) => putRunState(s, run));
  }

  putVisionSession(sess: VisionSessionState): void {
    this.update((s) => ({ ...s, vision: { ...s.vision, [sess.id]: sess } }));
  }

  // ---------------------------------------------------------- SiliconBreak X
  async loadAudits(projectId?: string): Promise<SecurityAudit[]> {
    const list = await securityApi.audits(projectId);
    this.update((s) => putSecurityAudits(s, list));
    return list;
  }

  putAudit(audit: SecurityAudit): void {
    this.update((s) => putSecurityAudits(s, [audit]));
  }

  /** Audit an imported repository (explicit files, or the backend's budgeted selection). */
  async startRepoAudit(repoId: string, files?: string[]): Promise<SecurityAudit> {
    const res = await securityApi.startRepoAudit(repoId, files);
    return this.openAudit(res);
  }

  /** Audit the current SiliconBreak X project's generated RTL against the generic contract. */
  async startProjectAudit(projectId: string, revisionId?: string | null): Promise<SecurityAudit> {
    const res = await securityApi.startProjectAudit(projectId, revisionId);
    return this.openAudit(res);
  }

  private async openAudit(res: { audit: SecurityAudit; project: { id: string } }): Promise<SecurityAudit> {
    this.putAudit(res.audit);
    await this.loadProjects();
    this.selectProject(res.project.id);
    this.selectAudit(res.audit.id);
    return res.audit;
  }

  /** Start an audit of a security case; selects its project and opens the Security tab. */
  async startSecurityAudit(caseId: string, opts: { investigator?: string; repair_agent?: string } = {}): Promise<SecurityAudit> {
    const res = await securityApi.startAudit({ case_id: caseId, ...opts });
    return this.openAudit(res);
  }
}

// ---------------------------------------------------------------- React bindings

export const StoreContext = createContext<AppStore | null>(null);

export function useStore(): AppStore {
  const s = useContext(StoreContext);
  if (!s) throw new Error("StoreContext missing");
  return s;
}

/**
 * Subscribe to a slice. The selector must return an existing reference (a
 * state slice), not a freshly built object, or React would re-render forever.
 */
export function useAppState<T>(selector: (s: AppState) => T): T {
  const store = useStore();
  return useSyncExternalStore(store.subscribe, () => selector(store.getState()));
}
