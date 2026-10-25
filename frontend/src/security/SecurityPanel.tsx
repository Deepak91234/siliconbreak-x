/**
 * Detailed SiliconBreak X evidence panels, composed by the Security Lab:
 *
 *   contract -> suspected findings (source-linked) -> independent reproduction
 *   (trusted tests, waveform, commands) -> formal check -> repair attempts
 *   (diff, agent trace) -> independent retest -> evidence
 *
 * Everything shown comes from the stored audit record (GET /api/security/audits)
 * and live `security.updated` events; statuses are the backend's, never inferred.
 */

import { useMemo, useState } from "react";
import { errorMessage } from "../api/client";
import { securityApi, type Finding, type RepairAttempt, type SecurityAudit, type SecurityCase, type TestOutcome,
  type VerificationReport } from "../api/security";
import { Button, Card, Collapsible, CopyButton, ErrorNote, Pill, Pre, Segmented, Spinner, cx } from "../components/ui";
import { highlightLines } from "../lib/highlight";
import { findingStatus, formalStatus, testStatus } from "../lib/status";
import { toast } from "../lib/toast";
import { useResource } from "../lib/useResource";
import { useAppState, useStore } from "../store/store";
import { DiffView, type DiffMode } from "../tabs/DiffView";
import { usd, useBudget } from "./AICredits";
import { SecurityWave } from "./SecurityWave";

export const SOURCE_KIND_LABEL: Record<string, string> = {
  demo: "demonstration design",
  research_injected: "research-injected defect",
  historical: "historical fix (pinned)",
  benchmark_adapted: "adapted benchmark task",
};

// ------------------------------------------------------------------ start

/** One card per security case, each starting a free offline audit (investigator choice follows the routing settings). */
export function SecurityStart() {
  const store = useStore();
  const cases = useResource<SecurityCase[]>("cases", 0, () => securityApi.cases());
  const [busy, setBusy] = useState<string | null>(null);
  const start = async (caseId: string) => {
    setBusy(caseId);
    try {
      await store.startSecurityAudit(caseId);
    } catch (e) {
      toast(`Could not start the audit: ${errorMessage(e)}`, "error");
    } finally {
      setBusy(null);
    }
  };
  return (
    <div className="space-y-2">
      {cases.error && <ErrorNote error={cases.error} onRetry={cases.reload} />}
      {!cases.data && !cases.error && <div className="flex items-center gap-2 text-xs text-muted"><Spinner className="h-3.5 w-3.5" /> Loading cases…</div>}
      <div className="grid gap-3 md:grid-cols-2 2xl:grid-cols-3">
        {(cases.data ?? []).map((c) => (
          <div key={c.case_id} className="sbx-card-hover flex flex-col rounded-xl border border-line bg-panel p-4 shadow-panel">
            <div className="flex items-start justify-between gap-2">
              <span className="text-sm font-semibold text-fg">{c.title}</span>
              <Pill tone={c.source_kind === "historical" ? "info" : "violet"}>{SOURCE_KIND_LABEL[c.source_kind] ?? c.source_kind}</Pill>
            </div>
            <p className="mt-1.5 text-xs leading-relaxed text-muted">{c.security_contract.summary}</p>
            <div className="mt-2 flex flex-wrap gap-1.5">
              {c.cwe.map((w) => <span key={w} className="rounded-md bg-raised px-1.5 py-0.5 font-mono text-[10.5px] text-muted">{w}</span>)}
              <span className="rounded-md bg-raised px-1.5 py-0.5 text-[10.5px] text-muted">{c.tests.length} trusted tests</span>
              {c.formal.available && <span className="rounded-md bg-raised px-1.5 py-0.5 text-[10.5px] text-muted">formal properties</span>}
              {c.board && <span className="rounded-md bg-accent/10 px-1.5 py-0.5 text-[10.5px] text-accent">Basys 3 demo</span>}
            </div>
            <div className="mt-auto flex items-center gap-2 pt-3">
              <Button size="sm" variant="primary" busy={busy === c.case_id} onClick={() => start(c.case_id)}>Run security audit</Button>
              {(c.audits ?? 0) > 0 && c.latest_audit_id && (
                <button type="button" className="text-xs text-accent hover:underline" onClick={() => store.selectAudit(c.latest_audit_id!)}>
                  open latest ({c.audits})
                </button>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

/** Audit the selected CircuitLoop design (its generated RTL) against the generic contract. */
export function ProjectAudit() {
  const store = useStore();
  const projectId = useAppState((s) => s.selection.projectId);
  const detail = useAppState((s) => (projectId ? s.details[projectId] : undefined));
  const [busy, setBusy] = useState(false);
  if (!projectId || !detail || detail.revisions.length === 0) return null;
  return (
    <section className="rounded-xl border border-line bg-panel p-4 shadow-panel">
      <div className="flex flex-wrap items-center gap-2">
        <div className="min-w-0 flex-1">
          <span className="text-sm font-medium text-fg">Audit the current Workspace design</span>
          <p className="mt-0.5 text-xs text-muted">{detail.project.title}: its generated RTL against the generic hardware-security contract.
            Findings stay suspected (no trusted security tests exist for generated designs).</p>
        </div>
        <Button size="sm" variant="secondary" busy={busy}
                onClick={async () => { setBusy(true); try { await store.startProjectAudit(projectId); } catch (e) { toast(errorMessage(e), "error"); } finally { setBusy(false); } }}>
          Audit this design
        </Button>
      </div>
    </section>
  );
}

// ----------------------------------------------------------------- header

/** Re-run and repair actions for an audit (each paid action confirms its worst-case spend first). */
export function AuditActions({ audit }: { audit: SecurityAudit }) {
  const store = useStore();
  const [busy, setBusy] = useState<string | null>(null);
  const running = audit.status === "running" || audit.status === "created";
  const c = audit.record.case;
  const canRepair = !running && !!audit.record.reproduction && audit.record.reproduction.security_status === "failed";
  const budget = useBudget(audit.record.repairs.length);
  const confirmSpend = (agent: "factory" | "model"): boolean => {
    const bd = budget.data;
    if (!bd) return true;
    if (!bd.live_enabled) {
      toast("Live AI calls are off. Turn them on in the AI credits panel first.", "info");
      return false;
    }
    if (agent === "factory") {
      const f = bd.providers.factory;
      return window.confirm(`Repair with Factory Droid?\n\nUses up to ${f.max_attempts_per_repair} Droid session(s) ` +
        `(${f.sessions_today}/${f.cap_sessions_per_day} used today). Droid edits an isolated copy only.`);
    }
    const p = bd.providers.bitdeer.configured ? bd.providers.bitdeer : bd.providers.anthropic;
    const n = 3;
    return window.confirm(`Repair with a model patch (${p.model})?\n\nWorst case: ${n} attempts x ${usd(p.worst_case_per_call_usd)} = ` +
      `${usd(n * p.worst_case_per_call_usd)}. Used so far: ${usd(p.used_usd)} of ${usd(p.cap_total_usd)}.`);
  };
  const act = async (key: string, fn: () => Promise<unknown>) => {
    setBusy(key);
    try {
      await fn();
    } catch (e) {
      toast(errorMessage(e), "error");
    } finally {
      setBusy(null);
    }
  };
  return (
    <div className="flex flex-wrap items-center gap-2">
      <Button size="sm" variant="secondary" disabled={running} busy={busy === "new"}
              onClick={() => act("new", () => {
                const fs = audit.record.fileset;
                if (fs?.kind === "repository" && fs.repo_id) return store.startRepoAudit(fs.repo_id, fs.audited_files);
                if (fs?.kind === "project") return store.startProjectAudit(audit.project_id);
                return store.startSecurityAudit(audit.case_id ?? "");
              })}>
        Run audit again
      </Button>
      {!audit.case_id && <span className="text-2xs text-faint">Repairs need a case with trusted tests; this audit can only suspect.</span>}
      {audit.case_id && <>
      <Button size="sm" variant="primary" disabled={!canRepair} busy={busy === "factory"}
              title={canRepair ? "Factory Droid edits an isolated copy; the trusted verifier judges the patch" : "Needs a reproduced vulnerability"}
              onClick={() => confirmSpend("factory") && act("factory", () => securityApi.repair(audit.id, "factory").then((r) => store.putAudit(r.audit)))}>
        Repair with Factory Droid
      </Button>
      <Button size="sm" variant="secondary" disabled={!canRepair} busy={busy === "model"}
              title="A model provider (Bitdeer or Claude) proposes a full-file patch"
              onClick={() => confirmSpend("model") && act("model", () => securityApi.repair(audit.id, "model").then((r) => store.putAudit(r.audit)))}>
        Repair with model
      </Button>
      <Button size="sm" variant="ghost" disabled={!canRepair || !c.has_reference_fix} busy={busy === "reference"}
              title="Run the human-written reference fix through the verifier to show the harness can tell right from wrong"
              onClick={() => act("reference", () => securityApi.repair(audit.id, "reference").then((r) => store.putAudit(r.audit)))}>
        Validate harness (reference fix)
      </Button>
      </>}
    </div>
  );
}

// ------------------------------------------------------------------- body

export function ContractCard({ c }: { c: SecurityCase }) {
  return (
    <Card title="Security contract" actions={<span className="text-2xs text-faint">{c.cwe.join(" · ")}</span>}>
      <p className="text-sm text-fg">{c.security_contract.summary}</p>
      <ul className="mt-2 space-y-1">
        {c.security_contract.properties.map((p) => (
          <li key={p.id} className="text-xs text-muted"><span className="mr-1.5 font-mono text-accent">{p.id}</span>{p.text}</li>
        ))}
      </ul>
      {c.functional_contract.length > 0 && <Collapsible title={<span className="text-xs">Functional contract, interface and Basys 3 mapping</span>}>
        <ul className="space-y-1">
          {c.functional_contract.map((p) => (
            <li key={p.id} className="text-xs text-muted"><span className="mr-1.5 font-mono text-info">{p.id}</span>{p.text}</li>
          ))}
        </ul>
        <div className="mt-2 grid gap-x-4 gap-y-0.5 text-2xs text-muted sm:grid-cols-2">
          {c.interface.map((p) => (
            <div key={p.port}><span className="font-mono text-fg">{p.port}</span> <span className="text-faint">{p.dir}[{p.width}]</span> {p.text}</div>
          ))}
        </div>
        {c.board && (
          <div className="mt-2 grid gap-x-4 gap-y-0.5 text-2xs text-muted sm:grid-cols-2">
            {c.board.mapping.map((m) => (
              <div key={m.control}><span className="font-mono text-fg">{m.control}</span> → <span className="font-mono">{m.signal}</span> · {m.text}</div>
            ))}
          </div>
        )}
      </Collapsible>}
    </Card>
  );
}

export function ScopeCard({ audit }: { audit: SecurityAudit }) {
  const fs = audit.record.fileset!;
  return (
    <Card title={fs.kind === "repository" ? "Repository scope" : "Design scope"}
          actions={<span className="text-2xs text-faint">{fs.audited_files.length} audited of {fs.indexed_files} indexed</span>}>
      {fs.kind === "repository" && fs.url && (
        <p className="text-xs">
          <a href={`${fs.url}/tree/${fs.sha}${fs.subdir ? `/${fs.subdir}` : ""}`} target="_blank" rel="noreferrer" className="text-accent hover:underline">
            {fs.url.replace("https://github.com/", "")}
          </a>
          <span className="ml-2 font-mono text-2xs text-faint">commit {fs.sha}</span>
          {fs.ref && fs.ref !== fs.sha && <span className="ml-2 text-2xs text-faint">(resolved from {fs.ref})</span>}
        </p>
      )}
      <p className="mt-1 text-xs text-muted">Selection: {fs.selection}. Files not listed were indexed but not read by the investigator.</p>
      <ul className="mt-1.5 grid gap-0.5 font-mono text-2xs text-muted sm:grid-cols-2">
        {fs.audited_files.map((f) => <li key={f} className="truncate" title={f}>{f}</li>)}
      </ul>
      <p className="mt-2 rounded border border-warn/40 bg-warn/5 px-2 py-1.5 text-2xs text-warn">
        No trusted security tests or properties exist for these files, so reproduction, formal checks and repairs do not run and every
        finding stays suspected. An empty result is not evidence that the files are secure.
      </p>
    </Card>
  );
}

export function FindingsCard({ audit }: { audit: SecurityAudit }) {
  const r = audit.record;
  const inv = r.investigation;
  const src = useResource(audit.id, 0, (sig) => securityApi.sources(audit.id, undefined, sig));
  const label = inv ? (inv.provider === "rules" ? "offline static rules (not AI)" : `${inv.provider} · ${inv.model ?? ""}`) : "";
  return (
    <Card title="Findings" actions={inv && <span className="text-2xs text-faint">investigator: {label}</span>}>
      {!inv && <p className="text-xs text-muted">Investigation has not run yet.</p>}
      {inv?.status === "error" && (
        <p className="mb-2 rounded border border-warn/40 bg-warn/5 px-2 py-1.5 text-xs text-warn">
          {inv.provider} failed: {inv.error?.message}. {inv.fallback ? `Fell back to ${inv.fallback.provider} (not AI).` : ""}
        </p>
      )}
      {inv && r.findings.length === 0 && <p className="text-xs text-muted">The investigator reported no suspected contract violation.</p>}
      <div className="space-y-3">
        {r.findings.map((f) => <FindingRow key={f.finding_id} f={f} files={src.data?.original ?? null} />)}
      </div>
      {r.unexplained_failures.length > 0 && (
        <div className="mt-3 rounded border border-orange/40 bg-orange/5 px-2.5 py-2 text-xs">
          <div className="font-medium text-orange">Verifier failures no finding explains (missed by the investigator)</div>
          {r.unexplained_failures.map((u) => <div key={u.test} className="mt-0.5 text-muted"><span className="font-mono">{u.test}</span> ({u.properties.join(", ")}): {u.message}</div>)}
        </div>
      )}
      {inv && inv.dropped.length > 0 && (
        <Collapsible title={<span className="text-2xs text-faint">{inv.dropped.length} unverifiable model claim(s) dropped</span>}>
          <ul className="list-disc pl-4 text-2xs text-muted">{inv.dropped.map((d) => <li key={d}>{d}</li>)}</ul>
        </Collapsible>
      )}
    </Card>
  );
}

export function FindingRow({ f, files }: { f: Finding; files: Record<string, string> | null }) {
  const st = findingStatus(f.status);
  const text = files?.[f.file];
  const lines = useMemo(() => (text ? highlightLines(text, "verilog") : null), [text]);
  const lo = Math.max(1, f.line_start - 3);
  const hi = Math.min(lines?.length ?? f.line_end, f.line_end + 3);
  return (
    <div className="rounded-md border border-line">
      <div className="flex flex-wrap items-center gap-2 border-b border-line px-3 py-2">
        <Pill tone={st.tone}>{st.label}</Pill>
        <span className="min-w-0 flex-1 text-sm font-medium">{f.title}</span>
        {f.permalink ? (
          <a href={f.permalink} target="_blank" rel="noreferrer" className="font-mono text-2xs text-accent hover:underline">
            {f.file}:{f.line_start}-{f.line_end}
          </a>
        ) : <span className="font-mono text-2xs text-faint">{f.file}:{f.line_start}-{f.line_end}</span>}
      </div>
      <div className="grid gap-3 p-3 lg:grid-cols-[1.1fr_1fr]">
        <div className="overflow-x-auto rounded bg-sunken py-1.5 font-mono text-[11.5px] leading-[1.55]">
          {lines ? lines.slice(lo - 1, hi).map((html, i) => {
            const n = lo + i;
            const on = n >= f.line_start && n <= f.line_end;
            return (
              <div key={n} className={cx("flex", on && "bg-bad/15")}>
                <span className="w-9 shrink-0 select-none pr-2 text-right text-faint">{n}</span>
                <span className="hljs whitespace-pre" dangerouslySetInnerHTML={{ __html: html || "&nbsp;" }} />
              </div>
            );
          }) : <div className="px-2 text-2xs text-faint">Loading source…</div>}
        </div>
        <div className="space-y-1.5 text-xs">
          <div><span className="text-faint">Weakness: </span>{f.weakness_class} · violates {f.violated_properties.join(", ")} · confidence {f.confidence}</div>
          <div><span className="text-faint">Hypothesis: </span>{f.hypothesis}</div>
          <div><span className="text-faint">Proposed test: </span>{f.proposed_test}</div>
          <div><span className="text-faint">Source: </span>{f.is_ai ? `${f.provider} (${f.model})` : `${f.provider}${f.rule ? ` rule ${f.rule}` : ""}, not AI`}</div>
          <ol className="mt-2 space-y-1 border-l border-line pl-3">
            {f.history.map((h, i) => (
              <li key={i} className="text-2xs text-muted"><Pill tone={findingStatus(h.status).tone} className="mr-1.5">{findingStatus(h.status).label}</Pill>{h.why}</li>
            ))}
          </ol>
        </div>
      </div>
    </div>
  );
}

export function VerifyBadge({ report }: { report: VerificationReport }) {
  const c = { s: report.tests.filter((t) => t.kind === "security"), f: report.tests.filter((t) => t.kind === "functional") };
  const pass = (xs: TestOutcome[]) => xs.filter((t) => t.status === "pass").length;
  return (
    <span className="flex items-center gap-1.5">
      <Pill tone={report.security_status === "passed" ? "ok" : report.security_status === "failed" ? "bad" : "idle"}>
        security {pass(c.s)}/{c.s.length}
      </Pill>
      <Pill tone={report.functional_status === "passed" ? "ok" : report.functional_status === "failed" ? "bad" : "idle"}>
        functional {pass(c.f)}/{c.f.length}
      </Pill>
    </span>
  );
}

export function TestTable({ report, selected, onSelect }: { report: VerificationReport; selected: string | null; onSelect: (t: TestOutcome) => void }) {
  if (report.status === "error" || report.status === "timeout" || report.status === "not_run") {
    return (
      <div className="space-y-1 text-xs">
        <p className="text-bad">Verifier {report.status}: {report.message}</p>
        {report.safety_issues.map((i, k) => <p key={k} className="text-muted">{i.file}:{i.line} {i.message}</p>)}
        {report.scope_violations.map((v) => <p key={v} className="text-muted">{v}</p>)}
      </div>
    );
  }
  const groups: Array<[string, TestOutcome[]]> = [
    ["Security", report.tests.filter((t) => t.kind === "security")],
    ["Functional (regression)", report.tests.filter((t) => t.kind === "functional")],
  ];
  return (
    <div className="grid gap-3 md:grid-cols-2">
      {groups.map(([name, ts]) => (
        <div key={name}>
          <div className="mb-1 text-2xs font-semibold uppercase tracking-wide text-faint">{name}</div>
          <ul className="space-y-0.5">
            {ts.map((t) => (
              <li key={t.id}>
                <button type="button" onClick={() => onSelect(t)} disabled={!t.span_ns}
                        className={cx("flex w-full items-start gap-2 rounded px-1.5 py-1 text-left text-xs hover:bg-raised",
                          selected === t.id && "bg-raised ring-1 ring-accent/40")}>
                  <Pill tone={testStatus(t.status).tone} className="mt-px">{testStatus(t.status).label}</Pill>
                  <span className="min-w-0 flex-1">
                    <span className="font-mono text-2xs text-faint">{t.id}</span> {t.title}
                    {t.message && <span className="block truncate text-2xs text-bad" title={t.message}>{t.message}</span>}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}

export function TestWave({ audit, report, test }: { audit: SecurityAudit; report: VerificationReport; test: TestOutcome }) {
  if (!report.vcd || !report.dir || !test.span_ns) return null;
  const ports = audit.record.case.interface.filter((p) => p.port !== "clk").map((p) => `tb.${p.port}`);
  const [a, b] = test.span_ns;
  // pad past the window end so the response to the last request (one cycle later) is visible
  const span: [number, number] = [a, Math.min(b + 30, a + 400)];
  return (
    <div className="mt-3 space-y-1">
      <div className="text-2xs text-faint">
        Waveform: <span className="font-mono">{test.id}</span> ({report.label}), {span[0]}–{span[1]} ns from the verifier's VCD
        {b + 30 > span[1] ? " (first 400 ns of the window)" : ""}
      </div>
      <SecurityWave auditId={audit.id} vcdPath={`${report.dir}/${report.vcd}`} span={span} signals={ports}
                    highlight={["read_grant", "read_data", "access_denied"]} />
    </div>
  );
}

export function Commands({ report }: { report: VerificationReport }) {
  return (
    <Collapsible title={<span className="text-xs">Exact commands, tool versions and hashes</span>}>
      <ul className="space-y-1">
        {report.commands.map((c, i) => (
          <li key={i} className="text-2xs">
            <span className="text-faint">{c.step} · exit {c.exit_code ?? "–"} · {c.duration_s.toFixed(1)} s{c.timed_out ? " · timed out" : ""}</span>
            <Pre maxH="max-h-24" className="mt-0.5">{"$ " + c.command}</Pre>
          </li>
        ))}
      </ul>
      <div className="mt-2 space-y-0.5 text-2xs text-muted">
        {Object.entries(report.tool_versions).map(([k, v]) => <div key={k}>{k}: {v}</div>)}
        {Object.entries(report.trusted_hashes).map(([k, v]) => <div key={k} className="break-all">trusted {k}: <span className="font-mono">{v}</span></div>)}
        {Object.entries(report.source_hashes).map(([k, v]) => <div key={k} className="break-all">candidate {k}: <span className="font-mono">{v}</span></div>)}
      </div>
    </Collapsible>
  );
}

export function FormalCard({ title, f }: { title: string; f: NonNullable<SecurityAudit["record"]["formal_original"]> }) {
  const st = formalStatus(f.status);
  const sigs = f.counterexample.length ? Object.keys(f.counterexample[0].signals).filter((k) => k !== "clk") : [];
  return (
    <Card title={title} actions={<Pill tone={st.tone}>{st.label}</Pill>}>
      <p className="text-xs text-fg">{f.message}</p>
      <p className="mt-1 text-2xs text-faint">{f.engine} · properties {f.properties.join(", ")} · assumptions: {f.assumptions.join("; ")}</p>
      {f.counterexample.length > 0 && (
        <div className="mt-2 overflow-x-auto">
          <table className="text-2xs">
            <thead><tr><th className="pr-3 text-left font-normal text-faint">step</th>{sigs.map((s) => <th key={s} className="px-2 font-mono font-normal text-faint">{s}</th>)}</tr></thead>
            <tbody>
              {f.counterexample.map((st2) => (
                <tr key={st2.step}><td className="pr-3 text-faint">{st2.step}</td>{sigs.map((s) => <td key={s} className="px-2 text-center font-mono">{st2.signals[s]}</td>)}</tr>
              ))}
            </tbody>
          </table>
          <p className="mt-1 text-2xs text-muted">Inputs per clock step of the shortest violating trace found by the solver.</p>
        </div>
      )}
      {f.command && <Pre maxH="max-h-20" className="mt-2 text-2xs">{"$ " + f.command.command}</Pre>}
    </Card>
  );
}

export function RepairCard({ audit, attempt: a, selected, onSelect }: { audit: SecurityAudit; attempt: RepairAttempt; selected: string | null;
  onSelect: (t: TestOutcome) => void }) {
  const [mode, setMode] = useState<DiffMode>("unified");
  const verdictTone = a.verdict === "verified" ? "ok" : a.verdict === "no_patch" ? "orange" : "bad";
  const verdictLabel = a.agent === "reference"
    ? (a.verdict === "verified" ? "reference passes (harness validated)" : `reference ${a.verdict}`)
    : ({ verified: "repaired + verified", failed: "rejected by verifier", no_patch: "no patch", rejected_scope: "scope violation" } as Record<string, string>)[a.verdict] ?? a.verdict;
  return (
    <Card title={`Repair attempt ${a.attempt} · ${a.label}`} actions={<Pill tone={verdictTone}>{verdictLabel}</Pill>}>
      {a.agent === "reference" && (
        <p className="mb-2 rounded border border-violet/40 bg-violet/5 px-2 py-1.5 text-xs text-violet">
          Human-written reference fix, used only to show the trusted harness accepts a correct repair. Not an agent repair.
        </p>
      )}
      {a.reason && <p className="mb-2 text-xs text-muted">{a.reason}</p>}
      {a.final_text && <p className="mb-2 whitespace-pre-line text-xs text-fg">{a.final_text}</p>}
      <div className="mb-2 flex flex-wrap gap-x-4 gap-y-0.5 text-2xs text-faint">
        <span>agent status: {a.status}</span>
        {a.model && <span>model: {a.model}</span>}
        {a.session_id && <span>droid session: <span className="font-mono">{a.session_id}</span></span>}
        {a.duration_s != null && <span>{a.duration_s.toFixed(1)} s</span>}
        {a.changed_files.length > 0 && <span>changed: {a.changed_files.join(", ")}</span>}
      </div>
      {a.scope_violations.length > 0 && <p className="mb-2 text-xs text-bad">Scope violations: {a.scope_violations.join("; ")}</p>}
      {(a.denied_actions?.length ?? 0) > 0 && <p className="mb-2 text-2xs text-warn">Refused agent actions: {a.denied_actions!.join("; ")}</p>}
      {a.diff ? (
        <div className="rounded border border-line">
          <div className="flex items-center justify-between border-b border-line px-2 py-1">
            <span className="text-2xs text-faint">patch.diff</span>
            <span className="flex items-center gap-2">
              <CopyButton text={a.diff} label="copy patch" />
              <Segmented value={mode} onChange={setMode} options={[{ value: "unified", label: "Unified" }, { value: "split", label: "Split" }]} />
            </span>
          </div>
          <DiffView diff={a.diff} mode={mode} lang="verilog" />
        </div>
      ) : <p className="text-xs text-muted">No diff.</p>}
      {a.verification && (
        <div className="mt-3">
          <div className="mb-1.5 flex items-center justify-between">
            <span className="text-2xs font-semibold uppercase tracking-wide text-faint">Independent retest</span>
            <VerifyBadge report={a.verification} />
          </div>
          <TestTable report={a.verification} selected={selected} onSelect={onSelect} />
        </div>
      )}
      {a.formal && (
        <p className="mt-2 text-xs"><span className="text-faint">Formal on the patch: </span>
          <Pill tone={formalStatus(a.formal.status).tone}>{formalStatus(a.formal.status).label}</Pill> <span className="text-muted">{a.formal.message}</span></p>
      )}
      <ArtifactLinks audit={audit} paths={[`${a.dir}/workspace/TASK.md`, ...(a.trace ? [`${a.dir}/${a.trace}`] : [])]} />
    </Card>
  );
}

export function ArtifactLinks({ audit, paths }: { audit: SecurityAudit; paths: string[] }) {
  const [open, setOpen] = useState<string | null>(null);
  const res = useResource(open ? `${audit.id}:${open}` : null, 0, (sig) => securityApi.file(audit.id, open!, sig));
  return (
    <div className="mt-2">
      <div className="flex flex-wrap gap-2">
        {paths.map((p) => (
          <button key={p} type="button" onClick={() => setOpen((o) => (o === p ? null : p))}
                  className={cx("rounded px-1.5 py-0.5 font-mono text-2xs hover:bg-raised", open === p ? "text-accent" : "text-faint")}>
            {p.split("/").slice(-1)[0]}
          </button>
        ))}
      </div>
      {open && (res.error ? <ErrorNote error={res.error} /> : res.data ? <Pre className="mt-1 text-2xs">{res.data.text}</Pre> : null)}
    </div>
  );
}

export function EvidenceCard({ audit }: { audit: SecurityAudit }) {
  const ev = audit.record.evidence;
  if (!ev) return null;
  return (
    <Card title="Evidence record" actions={<span className="text-2xs text-faint">{ev.artifacts.length} artifacts · sha256</span>}>
      <ArtifactLinks audit={audit} paths={["report.md", "audit.json", "reproduce/verify.log", "investigate/prompt.txt"]} />
      <Collapsible title={<span className="text-xs">All artifacts with hashes</span>}>
        <ul className="space-y-0.5 text-2xs">
          {ev.artifacts.map((x) => (
            <li key={x.path} className="flex gap-2"><span className="min-w-0 flex-1 truncate font-mono text-muted">{x.path}</span>
              <span className="font-mono text-faint">{x.sha256.slice(0, 16)}…</span><span className="w-16 text-right text-faint">{x.bytes} B</span></li>
          ))}
        </ul>
      </Collapsible>
    </Card>
  );
}
