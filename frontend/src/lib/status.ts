/**
 * Every status vocabulary in docs/api.md mapped to a tone (colour family) and
 * label. Class strings are literal so Tailwind's scanner keeps them. Unknown
 * values (a newer backend) fall back to a neutral tone with the raw text.
 */

export type Tone = "ok" | "bad" | "warn" | "info" | "idle" | "accent" | "violet" | "orange" | "muted";

export const TONE_TEXT: Record<Tone, string> = {
  ok: "text-ok",
  bad: "text-bad",
  warn: "text-warn",
  info: "text-info",
  idle: "text-idle",
  accent: "text-accent",
  violet: "text-violet",
  orange: "text-orange",
  muted: "text-muted",
};

export const TONE_PILL: Record<Tone, string> = {
  ok: "bg-ok/15 text-ok ring-ok/30",
  bad: "bg-bad/15 text-bad ring-bad/30",
  warn: "bg-warn/15 text-warn ring-warn/30",
  info: "bg-info/15 text-info ring-info/30",
  idle: "bg-idle/15 text-muted ring-idle/30",
  accent: "bg-accent/15 text-accent ring-accent/30",
  violet: "bg-violet/15 text-violet ring-violet/30",
  orange: "bg-orange/15 text-orange ring-orange/30",
  muted: "bg-muted/10 text-muted ring-muted/20",
};

export const TONE_DOT: Record<Tone, string> = {
  ok: "bg-ok",
  bad: "bg-bad",
  warn: "bg-warn",
  info: "bg-info",
  idle: "bg-idle",
  accent: "bg-accent",
  violet: "bg-violet",
  orange: "bg-orange",
  muted: "bg-muted",
};

export const TONE_BAR: Record<Tone, string> = TONE_DOT;

export const TONE_BORDER: Record<Tone, string> = {
  ok: "border-ok/40",
  bad: "border-bad/40",
  warn: "border-warn/40",
  info: "border-info/40",
  idle: "border-line",
  accent: "border-accent/40",
  violet: "border-violet/40",
  orange: "border-orange/40",
  muted: "border-line",
};

interface StatusInfo {
  tone: Tone;
  label: string;
}

function lookup(map: Record<string, StatusInfo>, s: string | null | undefined): StatusInfo {
  if (!s) return { tone: "idle", label: "none" };
  return map[s] ?? { tone: "muted", label: s.replace(/_/g, " ") };
}

const STAGE: Record<string, StatusInfo> = {
  pending: { tone: "idle", label: "pending" },
  running: { tone: "accent", label: "running" },
  passed: { tone: "ok", label: "passed" },
  failed: { tone: "bad", label: "failed" },
  skipped: { tone: "muted", label: "skipped" },
  blocked: { tone: "warn", label: "needs you" },
  unavailable: { tone: "orange", label: "unavailable" },
  inconclusive: { tone: "violet", label: "inconclusive" },
  cancelled: { tone: "muted", label: "cancelled" },
};
export const stageStatus = (s: string | null | undefined) => lookup(STAGE, s);

const RUN: Record<string, StatusInfo> = {
  running: { tone: "accent", label: "running" },
  waiting: { tone: "warn", label: "waiting" },
  passed: { tone: "ok", label: "passed" },
  passed_sim_only: { tone: "info", label: "passed (simulation only)" },
  failed: { tone: "bad", label: "failed" },
  cancelled: { tone: "muted", label: "cancelled" },
  unsupported: { tone: "orange", label: "unsupported" },
  reproduced: { tone: "orange", label: "vulnerability reproduced" },
  repaired_verified: { tone: "ok", label: "repaired + verified" },
  repair_failed: { tone: "bad", label: "repair failed" },
  no_violation_found: { tone: "info", label: "no violation reproduced" },
  suspected_only: { tone: "warn", label: "suspected only (not reproduced)" },
  no_findings: { tone: "info", label: "nothing suspected" },
  error: { tone: "bad", label: "error" },
};
export const runStatus = (s: string | null | undefined) => lookup(RUN, s);

const APPROVAL: Record<string, StatusInfo> = {
  pending: { tone: "warn", label: "pending" },
  approved: { tone: "info", label: "approved" },
  denied: { tone: "muted", label: "denied" },
  executed: { tone: "ok", label: "executed" },
  failed: { tone: "bad", label: "failed" },
  expired: { tone: "muted", label: "expired" },
};
export const approvalStatus = (s: string | null | undefined) => lookup(APPROVAL, s);

const HARDWARE: Record<string, StatusInfo> = {
  connected: { tone: "ok", label: "connected" },
  no_board: { tone: "idle", label: "no board" },
  busy: { tone: "warn", label: "busy" },
  unsupported: { tone: "orange", label: "unsupported" },
  error: { tone: "bad", label: "error" },
  tool_missing: { tone: "orange", label: "tool missing" },
  unknown: { tone: "idle", label: "unknown" },
};
export const hardwareState = (s: string | null | undefined) => lookup(HARDWARE, s);

const VERDICT: Record<string, StatusInfo> = {
  passed: { tone: "ok", label: "passed" },
  failed: { tone: "bad", label: "failed" },
  inconclusive: { tone: "violet", label: "inconclusive" },
  unavailable: { tone: "orange", label: "unavailable" },
};
export const verdict = (s: string | null | undefined) => lookup(VERDICT, s);

const REVISION: Record<string, StatusInfo> = {
  draft: { tone: "idle", label: "draft" },
  lint_failed: { tone: "bad", label: "lint failed" },
  sim_failed: { tone: "bad", label: "simulation failed" },
  sim_passed: { tone: "info", label: "simulation passed" },
  build_failed: { tone: "bad", label: "build failed" },
  built: { tone: "accent", label: "built" },
  programmed: { tone: "violet", label: "programmed" },
  verified: { tone: "ok", label: "verified" },
  verify_failed: { tone: "bad", label: "verification failed" },
};
export const revisionStatus = (s: string | null | undefined) => lookup(REVISION, s);

const PHYSICAL: Record<string, StatusInfo> = {
  observed_pass: { tone: "ok", label: "observed pass" },
  observed_fail: { tone: "bad", label: "observed fail" },
  inconclusive: { tone: "violet", label: "inconclusive" },
  not_observable: { tone: "idle", label: "not camera-observable" },
  not_observed: { tone: "muted", label: "not observed" },
};
export const physicalStatus = (s: string | null | undefined) => lookup(PHYSICAL, s);

const SIMREQ: Record<string, StatusInfo> = {
  passed: { tone: "ok", label: "passed" },
  failed: { tone: "bad", label: "failed" },
  not_tested: { tone: "idle", label: "not tested" },
};
export const simRequirementStatus = (s: string | null | undefined) => lookup(SIMREQ, s);

const SIMREPORT: Record<string, StatusInfo> = {
  passed: { tone: "ok", label: "passed" },
  failed: { tone: "bad", label: "failed" },
  error: { tone: "bad", label: "error" },
  unavailable: { tone: "orange", label: "unavailable" },
};
export const simReportStatus = (s: string | null | undefined) => lookup(SIMREPORT, s);

const BUILD: Record<string, StatusInfo> = {
  passed: { tone: "ok", label: "passed" },
  ok: { tone: "ok", label: "passed" },
  failed: { tone: "bad", label: "failed" },
  error: { tone: "bad", label: "error" },
  unavailable: { tone: "orange", label: "unavailable" },
  cancelled: { tone: "muted", label: "cancelled" },
  running: { tone: "accent", label: "running" },
};
export const buildStatus = (s: string | null | undefined) => lookup(BUILD, s);

const KIND: Record<string, StatusInfo> = {
  initial: { tone: "accent", label: "initial" },
  modification: { tone: "info", label: "modification" },
  repair: { tone: "violet", label: "repair" },
};
export const revisionKind = (s: string | null | undefined) => lookup(KIND, s);

export function boolTone(b: boolean | null | undefined): Tone {
  return b === true ? "ok" : b === false ? "bad" : "idle";
}

const FINDING: Record<string, StatusInfo> = {
  suspected: { tone: "warn", label: "suspected" },
  reproduced: { tone: "orange", label: "reproduced" },
  patch_proposed: { tone: "info", label: "patch proposed" },
  repaired_verified: { tone: "ok", label: "repaired + verified" },
  repair_failed: { tone: "bad", label: "repair failed" },
  false_positive: { tone: "muted", label: "false positive" },
  inconclusive: { tone: "violet", label: "inconclusive" },
};
export const findingStatus = (s: string | null | undefined) => lookup(FINDING, s);

const TESTSTATUS: Record<string, StatusInfo> = {
  pass: { tone: "ok", label: "pass" },
  fail: { tone: "bad", label: "fail" },
  not_run: { tone: "idle", label: "not run" },
  passed: { tone: "ok", label: "passed" },
  failed: { tone: "bad", label: "failed" },
  error: { tone: "bad", label: "error" },
  timeout: { tone: "bad", label: "timeout" },
};
export const testStatus = (s: string | null | undefined) => lookup(TESTSTATUS, s);

const FORMAL: Record<string, StatusInfo> = {
  proven: { tone: "ok", label: "proven" },
  counterexample: { tone: "bad", label: "counterexample" },
  bounded: { tone: "violet", label: "bounded only" },
  error: { tone: "bad", label: "error" },
  not_run: { tone: "idle", label: "not run" },
};
export const formalStatus = (s: string | null | undefined) => lookup(FORMAL, s);

const HEALTH: Record<string, StatusInfo> = {
  connected: { tone: "ok", label: "connected" },
  verified: { tone: "ok", label: "verified" },
  configured: { tone: "info", label: "configured" },
  unverified: { tone: "warn", label: "unverified" },
  unavailable: { tone: "orange", label: "unavailable" },
  disconnected: { tone: "idle", label: "disconnected" },
  failed: { tone: "bad", label: "failed" },
};
export const healthState = (s: string | null | undefined) => lookup(HEALTH, s);
