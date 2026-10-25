/**
 * SiliconBreak X API types and client (/api/security/*, /api/bench/*).
 * Shapes mirror backend/security (verify.py, formal.py, orchestrator.py, bench.py).
 */

import { request } from "./client";
import type { Waveform } from "./types";

export type FindingStatus =
  | "suspected" | "reproduced" | "patch_proposed" | "repaired_verified" | "repair_failed" | "false_positive" | "inconclusive";

export interface ContractProperty { id: string; text: string }

export interface SecurityCase {
  case_id: string;
  title: string;
  source_kind: "demo" | "research_injected" | "historical" | "benchmark_adapted";
  provenance: string;
  cwe: string[];
  repository: string | null;
  commit: string | null;
  license: string;
  top_module: string;
  editable: string[];
  description: string;
  interface: Array<{ port: string; dir: "input" | "output"; width: number; text: string }>;
  security_contract: { summary: string; properties: ContractProperty[] };
  functional_contract: ContractProperty[];
  tests: Array<{ id: string; kind: "security" | "functional"; properties: string[]; title: string }>;
  formal: { available: boolean; properties: string[] };
  has_reference_fix: boolean;
  board: { wrapper: string; mapping: Array<{ control: string; signal: string; text: string }> } | null;
  audits?: number;
  latest_audit_id?: string | null;
}

export interface TestOutcome {
  id: string;
  kind: "security" | "functional";
  title: string;
  properties: string[];
  status: "pass" | "fail" | "not_run";
  message: string;
  span_ns: [number, number] | null;
}

export interface CommandRecord { step: string; command: string; exit_code: number | null; duration_s: number; timed_out: boolean; error: string | null }

export interface VerificationReport {
  label: string;
  case_id: string;
  status: "passed" | "failed" | "error" | "timeout" | "not_run";
  security_status: "passed" | "failed" | "not_run";
  functional_status: "passed" | "failed" | "not_run";
  message: string;
  tests: TestOutcome[];
  failed_properties: string[];
  lint_ok: boolean | null;
  lint_messages: Array<{ severity: string; code: string | null; line: number | null; message: string; fatal: boolean }>;
  safety_issues: Array<{ file: string; line: number; message: string }>;
  scope_violations: string[];
  commands: CommandRecord[];
  tool_versions: Record<string, string>;
  trusted_hashes: Record<string, string>;
  source_hashes: Record<string, string>;
  vcd: string | null;
  log: string | null;
  duration_s: number;
  dir?: string;
}

export interface FormalResult {
  label: string;
  status: "proven" | "counterexample" | "bounded" | "error" | "not_run";
  message: string;
  engine: string;
  properties: string[];
  assumptions: string[];
  max_steps: number;
  induction_length: number | null;
  counterexample: Array<{ step: number; signals: Record<string, string> }>;
  vcd: string | null;
  log: string | null;
  command: CommandRecord | null;
  tool_version: string | null;
  duration_s: number;
  dir?: string;
}

export interface Finding {
  finding_id: string;
  title: string;
  weakness_class: string;
  file: string;
  line_start: number;
  line_end: number;
  violated_properties: string[];
  severity_reasoning: string;
  hypothesis: string;
  proposed_test: string;
  confidence: "low" | "medium" | "high";
  limitations: string;
  rule?: string;
  permalink?: string;
  status: FindingStatus;
  provider: string;
  model: string | null;
  is_ai: boolean;
  excerpt?: { start: number; end: number; lines: string[] };
  history: Array<{ status: FindingStatus; at: number; why: string; evidence?: Record<string, unknown> }>;
}

export interface RepairAttempt {
  attempt: number;
  agent: "factory" | "model" | "reference";
  label: string;
  status: string;
  reason?: string;
  verdict: "verified" | "failed" | "no_patch" | "rejected_scope";
  diff: string;
  changed_files: string[];
  scope_violations: string[];
  extra_files: string[];
  final_text?: string;
  model?: string | null;
  provider?: string;
  session_id?: string | null;
  usage?: Record<string, unknown> | null;
  duration_s?: number;
  denied_actions?: string[];
  allowed_actions?: string[];
  verification: VerificationReport | null;
  formal: FormalResult | null;
  dir: string;
  trace?: string | null;
}

export interface Investigation {
  provider: string;
  model: string | null;
  is_ai: boolean;
  status: "ok" | "error";
  note: string;
  error: { kind: string; message: string } | null;
  duration_s: number;
  usage: Record<string, unknown>;
  summary: string;
  areas_reviewed: string[];
  files: string[];
  dropped: string[];
  fallback: { provider: string; model: string; is_ai: boolean; reason: string; summary: string } | null;
}

export type AuditStatus =
  | "created" | "running" | "reproduced" | "repaired_verified" | "repair_failed" | "no_violation_found" | "error"
  | "failed" | "cancelled" | "suspected_only" | "no_findings";

export interface SecurityAudit {
  id: string;
  project_id: string;
  run_id: string | null;
  source_kind: string;
  case_id: string | null;
  status: AuditStatus;
  options: { investigator?: string; repair_agent?: string; max_attempts?: number };
  created_at: number;
  finished_at: number | null;
  record: {
    case: SecurityCase;
    source: { kind: string; case_id: string; label: string; files: Record<string, string> };
    providers: { investigator?: { provider: string; model: string | null; is_ai: boolean; note: string } };
    investigation: Investigation | null;
    findings: Finding[];
    unexplained_failures: Array<{ test: string; properties: string[]; message: string }>;
    reproduction: VerificationReport | null;
    formal_original: FormalResult | null;
    repairs: RepairAttempt[];
    harness_validation: { attempt: number; passed: boolean; summary: string } | null;
    outcome: { status: string; summary: string };
    tool_versions: Record<string, string>;
    evidence: { generated_at: number; artifacts: Array<{ path: string; bytes: number; sha256: string }> } | null;
    fileset?: { kind: "repository" | "project"; url?: string; sha?: string; ref?: string; subdir?: string; repo_id?: string;
                indexed_files: number; indexed_modules?: number; audited_files: string[]; selection: string; truncated?: boolean };
    fpga?: Record<string, FpgaVariantState>;
  };
}

export interface DemoStep { id: string; title: string; set: Record<string, number>; press: string | null; expect_leds: string; expect_text: string }

export interface BoardSimStep {
  id: string; title: string; set: Record<string, number>; press: string | null;
  expected: string; expected_text: string; simulated: string; matches_contract: boolean;
}

export interface FpgaVariantState {
  label: string;
  kind: "vulnerable" | "agent" | "reference";
  source_hashes?: Record<string, string>;
  board_sim?: { status: string; message: string; steps: BoardSimStep[]; note: string; lint_ok: boolean | null;
                commands: Array<{ step: string; command: string; exit_code: number | null }> };
  build?: { status: string; ok: boolean; message: string; timing: { wns_ns?: number; whs_ns?: number } | null;
            utilization: Record<string, { used?: number; available?: number }> | null; bitstream_path: string | null;
            bitstream_sha256: string | null; bitstream_bytes: number | null; duration_s: number; vivado_version: string | null } | null;
  programs?: Array<{ at: number; ok: boolean; adapter: string; state: string; message: string; bitstream_sha256: string }>;
  observations?: BoardObservation[];
}

/** What SiliconBreak X last wrote to the board; camera evidence is only attributed to a confirmed variant. */
export interface Provenance {
  confirmed: boolean; reason?: string; approval_id?: string; at?: number;
  bitstream_sha256?: string | null; audit_id?: string | null; variant?: string | null;
}

export interface ObservedStep {
  step_id: string; title: string; status: "observed" | "inconclusive" | "not_run";
  set?: Record<string, number>; press?: string | null; duration_s?: number; frames?: number;
  expected: string; expected_text?: string; simulated: string | null; observed: string | null;
  matches_contract?: boolean | null; matches_simulation?: boolean | null; reasons?: string[]; lit?: number[]; snapshot?: string;
}

export type ObservationVerdict = "violation_observed" | "contract_met" | "unexpected" | "inconclusive";

export interface BoardObservation {
  kind: "board_observation"; session_id: string; audit_id: string; variant: string; variant_label: string;
  verdict: ObservationVerdict; summary: string; steps: ObservedStep[]; programmed: Provenance;
  frames_recorded: number; method: string; dir?: string; at?: number;
}

export interface CameraSession {
  id: string; kind: "board_observation"; status: "setup" | "ready" | "recording" | "finished";
  audit_id: string; variant: string; variant_label: string;
  roi: { leds: Array<{ led_index: number; x: number; y: number; w: number; h: number }> } | null;
  steps: Array<DemoStep & { simulated: string | null }>;
  active_step: string | null; active_frames: number; step_results: Record<string, ObservedStep>; result: BoardObservation | null;
  live: { t_ms?: number; states?: Record<string, number | null>; levels?: Record<string, number>; quality?: Record<string, unknown> };
  frames_recorded: number; programmed: Provenance;
}

export interface FpgaInfo {
  available: boolean;
  variants: Array<{ variant: string; label: string; kind: "vulnerable" | "agent" | "reference" }>;
  provenance: Record<string, Provenance>;
  demo: DemoStep[];
  mapping: Array<{ control: string; signal: string; text: string }>;
  state: Record<string, FpgaVariantState> & { loaded?: { variant: string; at: number } };
}

export interface RepoFile {
  path: string; bytes: number; lines: number; modules: string[]; packages: string[]; includes: string[];
  instances: string[]; score: number; keywords: Record<string, number>; skipped: string | null; sha256?: string;
}

export interface RepoIndex {
  repo_id: string; url: string; owner: string; name: string; ref: string; sha: string; subdir: string;
  imported_at: number; duration_s: number; files: RepoFile[]; modules: Record<string, string>;
  totals: { files: number; bytes: number; modules: number; lines: number }; truncated: boolean; commands: string[];
}

export type RepoSummary = Pick<RepoIndex, "repo_id" | "url" | "owner" | "name" | "ref" | "sha" | "subdir" | "imported_at" | "totals" | "truncated">;

export interface AIProviderBudget {
  configured: boolean; model: string; price_in_per_mtok: number; price_out_per_mtok: number; prices_are_estimates: boolean;
  cap_total_usd: number; cap_daily_usd: number; cap_per_call_usd: number; used_usd: number; used_today_usd: number;
  calls: number; worst_case_per_call_usd: number;
}

export interface AIBudget {
  live_enabled: boolean;
  providers: {
    anthropic: AIProviderBudget;
    bitdeer: AIProviderBudget;
    factory: { configured: boolean; model: string; sessions_today: number; cap_sessions_per_day: number;
               max_attempts_per_repair: number; credits_reported_total: number | null; sessions_total: number };
  };
  recent: Array<{ ts: number; provider: string; model: string | null; purpose: string; status: string; cost_usd: number | null;
                  max_cost_usd: number | null; input_tokens: number | null; output_tokens: number | null;
                  detail: { reason?: string; error_kind?: string; factory_credits?: number } | null }>;
}

export interface BenchEstimate {
  case_ids: string[]; arms: string[]; max_attempts: number; usd: { anthropic: number; bitdeer: number }; usd_total: number;
  calls: { anthropic: number; bitdeer: number }; droid_sessions: number; paid: boolean; live_enabled: boolean; note: string;
}

export interface HealthItem { name?: string; role?: string; state: string; detail: string; model?: string }
export interface SecurityHealth {
  checked_at: number;
  providers: Record<"bitdeer" | "anthropic" | "factory" | "rules", HealthItem>;
  tools: Record<"verilator" | "yosys" | "vivado" | "fpga", HealthItem>;
  routing: { investigator: string; repair_model: string };
}

export interface BenchCaseResult {
  case_id: string;
  title: string;
  source_kind: string;
  cwe: string[];
  provenance: string;
  repository: string | null;
  commit: string | null;
  harness: { vulnerable_fails: boolean | null; reference_passes: boolean | null; vulnerable_status: string; reference_status: string;
             failed_properties: string[]; formal_vulnerable: string | null; formal_reference: string | null };
  detection: Record<string, { status: string; provider: string; model: string | null; is_ai: boolean; correct: boolean | null;
                              findings: number; reason?: string }>;
  repair: Record<string, { status: string; verdict: string | null; reason?: string; attempts?: number }>;
  duration_s: number;
}

export interface BenchRun {
  id: string;
  status: "running" | "finished" | "failed";
  config: { case_ids: string[]; arms: string[] };
  result: { cases: BenchCaseResult[]; metrics: Record<string, { numerator: number; denominator: number; not_run: number; label: string }>;
            tool_versions: Record<string, string>; commands: string[] } | null;
  created_at: number;
  finished_at: number | null;
}

const enc = encodeURIComponent;

export const securityApi = {
  health: (refresh = false) => request<SecurityHealth>(`/api/security/health${refresh ? "?refresh=true" : ""}`, { timeoutMs: 60_000 }),
  cases: () => request<SecurityCase[]>("/api/security/cases"),
  caseSource: (caseId: string) => request<{ files: Record<string, string> }>(`/api/security/cases/${enc(caseId)}/source`),
  audits: (projectId?: string) =>
    request<SecurityAudit[]>(`/api/security/audits${projectId ? `?project_id=${enc(projectId)}` : ""}`),
  audit: (id: string, signal?: AbortSignal) => request<SecurityAudit>(`/api/security/audits/${enc(id)}`, { signal }),
  startAudit: (body: { case_id: string; investigator?: string; repair_agent?: string }) =>
    request<{ audit: SecurityAudit; run: { id: string }; project: { id: string } }>("/api/security/audits",
      { method: "POST", body, timeoutMs: 60_000 }),
  repair: (id: string, agent: string) =>
    request<{ audit: SecurityAudit; run: { id: string } }>(`/api/security/audits/${enc(id)}/repair`, { method: "POST", body: { agent } }),
  file: (id: string, path: string, signal?: AbortSignal) =>
    request<{ path: string; bytes: number; truncated: boolean; text: string }>(
      `/api/security/audits/${enc(id)}/file?path=${enc(path)}`, { signal }),
  sources: (id: string, attempt?: number, signal?: AbortSignal) =>
    request<{ original: Record<string, string>; patched: Record<string, string | null> | null }>(
      `/api/security/audits/${enc(id)}/sources${attempt != null ? `?attempt=${attempt}` : ""}`, { signal }),
  waveform: (id: string, path: string, opts: { start?: number; end?: number; signals?: string[] } = {}, signal?: AbortSignal) => {
    const p = new URLSearchParams({ path });
    if (opts.start != null) p.set("start", String(opts.start));
    if (opts.end != null) p.set("end", String(opts.end));
    if (opts.signals?.length) p.set("signals", opts.signals.join(","));
    return request<Waveform>(`/api/security/audits/${enc(id)}/waveform?${p.toString()}`, { signal, timeoutMs: 60_000 });
  },
  startRepoAudit: (repoId: string, files?: string[]) =>
    request<{ audit: SecurityAudit; run: { id: string }; project: { id: string } }>("/api/security/audits",
      { method: "POST", body: { repo_id: repoId, files }, timeoutMs: 60_000 }),
  startProjectAudit: (projectId: string, revisionId?: string | null) =>
    request<{ audit: SecurityAudit; run: { id: string }; project: { id: string } }>("/api/security/audits",
      { method: "POST", body: { project_id: projectId, revision_id: revisionId ?? undefined }, timeoutMs: 60_000 }),
  importRepo: (body: { url: string; ref?: string; subdir?: string }) =>
    request<RepoIndex>("/api/security/repos", { method: "POST", body, timeoutMs: 900_000 }),
  repos: () => request<RepoSummary[]>("/api/security/repos"),
  repo: (id: string, signal?: AbortSignal) => request<RepoIndex>(`/api/security/repos/${enc(id)}`, { signal }),
  fpga: (id: string, signal?: AbortSignal) => request<FpgaInfo>(`/api/security/audits/${enc(id)}/fpga`, { signal }),
  fpgaBuild: (id: string, variant: string) =>
    request<{ run: { id: string } }>(`/api/security/audits/${enc(id)}/fpga/${enc(variant)}/build`, { method: "POST" }),
  fpgaProgram: (id: string, variant: string) =>
    request<{ run: { id: string } }>(`/api/security/audits/${enc(id)}/fpga/${enc(variant)}/program`, { method: "POST" }),
  cameraCreate: (id: string, variant: string) =>
    request<CameraSession>(`/api/security/audits/${enc(id)}/fpga/${enc(variant)}/camera`, { method: "POST" }),
  camera: (sid: string, signal?: AbortSignal) => request<CameraSession>(`/api/security/camera/${enc(sid)}`, { signal }),
  cameraAutoRoi: (sid: string, box: [number, number, number, number]) =>
    request<CameraSession>(`/api/security/camera/${enc(sid)}/auto-roi`, { method: "POST", body: { box } }),
  cameraStart: (sid: string, stepId: string) =>
    request<CameraSession>(`/api/security/camera/${enc(sid)}/step`, { method: "POST", body: { action: "start", step_id: stepId } }),
  cameraEnd: (sid: string) =>
    request<{ step: ObservedStep; session: CameraSession }>(`/api/security/camera/${enc(sid)}/step`, { method: "POST", body: { action: "end" } }),
  cameraFinish: (sid: string) =>
    request<BoardObservation>(`/api/security/camera/${enc(sid)}/finish`, { method: "POST", timeoutMs: 60_000 }),
  cameraImageUrl: (id: string, path: string) => `/api/security/audits/${enc(id)}/camera-image?path=${enc(path)}`,
  aiBudget: () => request<AIBudget>("/api/ai/budget"),
  setLiveAI: (enabled: boolean) => request<AIBudget>("/api/ai/live", { method: "POST", body: { enabled } }),
  benchEstimate: (body: { case_ids?: string[]; arms?: string[]; max_attempts?: number }) =>
    request<BenchEstimate>("/api/bench/estimate", { method: "POST", body }),
  benchRuns: () => request<BenchRun[]>("/api/bench/runs"),
  benchRun: (id: string) => request<BenchRun>(`/api/bench/runs/${enc(id)}`),
  startBench: (body: { case_ids?: string[]; arms?: string[]; confirm_max_usd?: number; confirm_droid_sessions?: number }) =>
    request<BenchRun>("/api/bench/runs", { method: "POST", body, timeoutMs: 60_000 }),
};
