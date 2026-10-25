/**
 * Summaries derived from a stored audit record (and the approvals list), for the
 * Dashboard and Security Lab. Every value comes from backend data; nothing here
 * invents a status. When data is missing the result says so ("pending", null).
 */

import type { Approval } from "../api/types";
import type { BoardObservation, FpgaVariantState, RepairAttempt, SecurityAudit, VerificationReport } from "../api/security";
import type { Tone } from "../lib/status";

export type StageState = "passed" | "failed" | "running" | "pending" | "warning";

export interface MacroStage {
  key: "investigate" | "reproduce" | "repair" | "verify" | "fpga";
  title: string;
  state: StageState;
  headline: string;
  facts: string[];
  duration_s: number | null;
}

export const isRunning = (a: SecurityAudit) => a.status === "running" || a.status === "created";

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

export function macroStages(a: SecurityAudit): MacroStage[] {
  const r = a.record;
  const running = isRunning(a);
  const inv = r.investigation;
  const rep = r.reproduction;
  const repair = keyRepair(a);
  const out: MacroStage[] = [];

  // 1. investigation
  {
    const facts: string[] = [];
    let state: StageState = running ? "running" : "pending";
    let headline = running ? "Investigating…" : "Not run";
    if (inv) {
      state = inv.status === "error" ? (inv.fallback ? "warning" : "failed") : "passed";
      headline = providerLabel(inv.provider, inv.model);
      if (inv.status === "error" && inv.fallback) headline = `${providerLabel(inv.fallback.provider)} (fallback)`;
      facts.push(`${r.findings.length} suspected finding${r.findings.length === 1 ? "" : "s"}`);
      const f = r.findings[0];
      if (f) facts.push(`${f.file.split("/").pop()}:${f.line_start}`);
      if (inv.areas_reviewed.length) facts.push(`${inv.areas_reviewed.length} areas reviewed`);
    }
    out.push({ key: "investigate", title: "Investigation", state, headline, facts, duration_s: inv?.duration_s ?? null });
  }
  // 2. independent reproduction (+ formal on the original)
  {
    const facts: string[] = [];
    let state: StageState = inv && running ? "running" : "pending";
    let headline = state === "running" ? "Running trusted tests…" : "Not run";
    let dur: number | null = null;
    if (rep) {
      const c = counts(rep);
      dur = rep.duration_s + (r.formal_original?.duration_s ?? 0);
      if (rep.status === "error" || rep.status === "timeout") { state = "failed"; headline = `Verifier ${rep.status}`; }
      else {
        state = "passed";
        headline = rep.security_status === "failed" ? "Vulnerability reproduced" : "Not reproduced";
        facts.push(`${c.secFail}/${c.secTotal} security tests fail`);
        facts.push(`functional ${c.funPass}/${c.funTotal} pass`);
      }
      if (r.formal_original) facts.push(r.formal_original.status === "counterexample" ? "formal counterexample"
        : r.formal_original.status === "proven" ? "formal: property holds" : `formal ${r.formal_original.status}`);
    } else if (!a.case_id && inv) { state = "pending"; headline = "No trusted tests (suspected only)"; }
    out.push({ key: "reproduce", title: "Reproduce", state, headline, facts, duration_s: dur });
  }
  // 3. repair
  {
    const facts: string[] = [];
    let state: StageState = "pending";
    let headline = a.case_id ? "No repair yet" : "Needs a case with trusted tests";
    if (repair) {
      state = repair.verdict === "verified" ? "passed" : repair.status === "running" ? "running" : "failed";
      headline = agentLabel(repair);
      const d = diffStat(repair.diff);
      if (repair.changed_files.length) facts.push(`${repair.changed_files.map((x) => x.split("/").pop()).join(", ")} (+${d.add}/-${d.del})`);
      facts.push(`${r.repairs.length} attempt${r.repairs.length === 1 ? "" : "s"}`);
      if (repair.session_id) facts.push(`session ${repair.session_id.slice(0, 8)}`);
    }
    out.push({ key: "repair", title: "Repair", state, headline, facts, duration_s: repair?.duration_s ?? null });
  }
  // 4. independent retest of the patch
  {
    const facts: string[] = [];
    let state: StageState = "pending";
    let headline = "Waiting for a patch";
    let dur: number | null = null;
    const v = repair?.verification;
    if (repair && v) {
      const c = counts(v);
      dur = v.duration_s + (repair.formal?.duration_s ?? 0);
      state = repair.verdict === "verified" ? "passed" : "failed";
      headline = repair.verdict === "verified" ? (repair.agent === "reference" ? "Harness validated" : "Independently verified") : "Rejected by verifier";
      facts.push(`${c.allPass}/${c.allTotal} trusted tests pass`);
      if (repair.formal) facts.push(repair.formal.status === "proven" ? "formal proof complete" : `formal ${repair.formal.status}`);
      facts.push(c.funPass === c.funTotal ? "no functional regressions" : `${c.funTotal - c.funPass} functional regressions`);
    }
    out.push({ key: "verify", title: "Verify", state, headline, facts, duration_s: dur });
  }
  // 5. FPGA build / program / webcam
  {
    const facts: string[] = [];
    const vs = fpgaVariants(a);
    const keys = Object.keys(vs);
    let state: StageState = "pending";
    let headline = a.record.case.board ? "Not built yet" : "No board demo for this case";
    let dur: number | null = null;
    if (keys.length) {
      const built = keys.filter((k) => vs[k].build?.ok);
      const programmed = keys.filter((k) => (vs[k].programs ?? []).some((p) => p.ok));
      const obs = keys.map((k) => latestObservation(vs[k])).filter(Boolean) as BoardObservation[];
      dur = keys.reduce((s, k) => s + (vs[k].build?.duration_s ?? 0), 0) || null;
      facts.push(`${built.length}/${keys.length} bitstreams built`);
      if (programmed.length) facts.push(`programmed: ${programmed.map(variantName).join(", ")}`);
      facts.push(obs.length ? `webcam: ${obs.map((o) => `${variantName(o.variant)} ${OBS_SHORT[o.verdict]}`).join("; ")}` : "no webcam evidence yet");
      state = obs.length ? (obs.some((o) => o.verdict === "unexpected") ? "warning" : "passed") : programmed.length ? "passed" : built.length ? "passed" : "pending";
      headline = obs.length ? "Physically observed" : programmed.length ? "Programmed on Basys 3" : built.length ? "Bitstreams built" : "Simulated only";
    }
    out.push({ key: "fpga", title: "FPGA test", state, headline, facts, duration_s: dur });
  }
  return out;
}

export const variantName = (v: string) => (v === "vulnerable" ? "before" : v.startsWith("attempt-") ? `after (#${v.slice(8)})` : v);

const OBS_SHORT: Record<string, string> = {
  violation_observed: "leak seen", contract_met: "contract met", unexpected: "≠ simulation", inconclusive: "inconclusive",
};

// ------------------------------------------------------------------ evidence ladder

export interface Rung { key: string; label: string; reached: boolean; detail: string; tone: Tone }

/** How far the evidence for this audit goes (the trusted verifier can reproduce what the investigator missed). */
export function evidenceLadder(a: SecurityAudit): Rung[] {
  const r = a.record;
  const f = r.findings[0];
  const reproduced = r.reproduction?.security_status === "failed";
  const repair = keyRepair(a);
  const verified = repair?.verdict === "verified";
  const vs = fpgaVariants(a);
  const obsAfter = repair ? latestObservation(vs[variantOf(repair)]) : null;
  const obsBefore = latestObservation(vs.vulnerable);
  const physical = [obsBefore, obsAfter].filter((o) => o && o.verdict !== "inconclusive") as BoardObservation[];
  return [
    { key: "suspected", label: "Suspected", reached: r.findings.length > 0, tone: "warn",
      detail: f ? `${f.is_ai ? providerLabel(f.provider, f.model) : "offline rule"} flagged ${f.file.split("/").pop()}:${f.line_start}` : "no finding yet" },
    { key: "reproduced", label: "Reproduced", reached: reproduced, tone: "bad",
      detail: reproduced ? `${counts(r.reproduction).secFail} trusted security tests fail${r.formal_original?.status === "counterexample" ? ", formal counterexample" : ""}${r.findings.length === 0 ? " (missed by the investigator)" : ""}` : "trusted tests have not shown it" },
    { key: "repaired", label: "Repaired", reached: !!repair && !!repair.diff, tone: "info",
      detail: repair?.diff ? `${agentLabel(repair)} proposed a patch` : "no patch yet" },
    { key: "verified", label: "Independently verified", reached: verified, tone: "ok",
      detail: verified ? `${counts(repair!.verification).allPass}/${counts(repair!.verification).allTotal} tests${repair!.formal?.status === "proven" ? " + formal proof" : ""}${repair!.agent === "reference" ? " (reference fix: harness validation only)" : ""}` : "no accepted patch yet" },
    { key: "physical", label: "Physically observed", reached: physical.length > 0, tone: "violet",
      detail: physical.length ? physical.map((o) => `${variantName(o.variant)}: ${OBS_SHORT[o.verdict]}`).join(" · ") + " (webcam)" : "no webcam evidence on the Basys 3 yet" },
  ];
}

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
