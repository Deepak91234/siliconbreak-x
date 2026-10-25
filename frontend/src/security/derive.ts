/**
 * Summaries derived from a stored audit record (and the approvals list), for the
 * Security Lab and FPGA Lab. Every value comes from backend data; nothing here
 * invents a status. When data is missing the result says so ("pending", null).
 */

import type { Approval } from "../api/types";
import type { BoardObservation, FpgaVariantState, RepairAttempt, SecurityAudit, VerificationReport } from "../api/security";
import type { Tone } from "../lib/status";

export function providerLabel(provider: string | null | undefined, model?: string | null): string {
  if (!provider) return "–";
  if (provider === "rules") return "Offline rules (not AI)";
  const name = ({ bitdeer: "Bitdeer", anthropic: "Claude", factory: "Factory Droid", reference: "Human reference" } as Record<string, string>)[provider] ?? provider;
  return model ? `${name} · ${model}` : name;
}

export function counts(report: VerificationReport | null | undefined) {
  const sec = report?.tests.filter((t) => t.kind === "security") ?? [];
  const fun = report?.tests.filter((t) => t.kind === "functional") ?? [];
  const pass = (xs: typeof sec) => xs.filter((t) => t.status === "pass").length;
  const all = report?.tests ?? [];
  return {
    secPass: pass(sec), secTotal: sec.length, secFail: sec.filter((t) => t.status === "fail").length,
    funPass: pass(fun), funTotal: fun.length, allPass: pass(all), allTotal: all.length,
  };
}

/** +added / -removed lines of a unified diff. */
export function diffStat(diff: string | null | undefined): { add: number; del: number } {
  let add = 0, del = 0;
  for (const ln of (diff ?? "").split("\n")) {
    if (ln.startsWith("+++") || ln.startsWith("---")) continue;
    if (ln.startsWith("+")) add++;
    else if (ln.startsWith("-")) del++;
  }
  return { add, del };
}

/** The repair that matters for the summary: the latest verified one, else the latest attempt. */
export function keyRepair(a: SecurityAudit): RepairAttempt | null {
  const rs = a.record.repairs;
  return [...rs].reverse().find((r) => r.verdict === "verified") ?? rs[rs.length - 1] ?? null;
}

export function agentLabel(r: RepairAttempt): string {
  if (r.agent === "factory") return "Factory Droid";
  if (r.agent === "reference") return "Human reference fix";
  return providerLabel(r.provider ?? "model", r.model);
}

/** Variant key ("attempt-N") of a repair, as used by the FPGA records. */
export const variantOf = (r: RepairAttempt) => `attempt-${r.attempt}`;

export function fpgaVariants(a: SecurityAudit): Record<string, FpgaVariantState> {
  const f = (a.record.fpga ?? {}) as Record<string, unknown>;
  const out: Record<string, FpgaVariantState> = {};
  for (const [k, v] of Object.entries(f)) if (k !== "loaded" && v && typeof v === "object") out[k] = v as FpgaVariantState;
  return out;
}

export function latestObservation(v: FpgaVariantState | undefined): BoardObservation | null {
  const o = v?.observations ?? [];
  return o.length ? o[o.length - 1] : null;
}


export const variantName = (v: string) => (v === "vulnerable" ? "before" : v.startsWith("attempt-") ? `after (#${v.slice(8)})` : v);




// ------------------------------------------------------------------ board provenance

export interface BoardHold { approvalId: string; at: number; sha: string | null; auditId: string | null; variant: string | null; revisionId: string | null }

/** The design CircuitLoop most recently wrote to the board (latest executed program approval), or null. */
export function latestProgram(approvals: Record<string, Approval>): BoardHold | null {
  let best: Approval | null = null;
  for (const a of Object.values(approvals)) {
    if (a.kind !== "program_fpga" || a.status !== "executed") continue;
    if (!best || a.created_at > best.created_at) best = a;
  }
  if (!best) return null;
  const d = (best.details ?? {}) as Record<string, unknown>;
  const str = (k: string) => (typeof d[k] === "string" ? (d[k] as string) : null);
  return { approvalId: best.id, at: best.decided_at ?? best.created_at, sha: str("bitstream_sha256"), auditId: str("audit_id"),
           variant: str("variant"), revisionId: str("revision_id") };
}

export function auditHeadlineTone(a: SecurityAudit): Tone {
  return ({ repaired_verified: "ok", reproduced: "bad", repair_failed: "orange", running: "accent", created: "accent",
            suspected_only: "warn", no_findings: "idle", no_violation_found: "ok", error: "bad", failed: "bad", cancelled: "idle" } as Record<string, Tone>)[a.status] ?? "idle";
}
