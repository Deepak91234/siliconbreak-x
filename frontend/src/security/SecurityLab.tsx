/**
 * Security Lab: start audits (security cases, public repositories, CircuitLoop
 * designs), browse every audit, and open one for the full evidence chain:
 *
 *   pipeline -> evidence level -> headline numbers -> vulnerability + AI analysis
 *   -> repair + verifier checklist -> detailed evidence (trusted tests, waveforms,
 *   formal, every repair attempt, contract, evidence record)
 *
 * Everything comes from the stored audit records and live `security.updated` events.
 */

import { useEffect, useMemo, useState, type ReactNode } from "react";
import type { SecurityAudit, TestOutcome, VerificationReport } from "../api/security";
import type { Run } from "../api/types";
import { IconArrowRight, IconBug, IconChip, IconSearch, IconShieldCheck, IconWrench } from "../components/icons";
import { Button, Card, Empty, Loading, Pill, Tabs, cx } from "../components/ui";
import { fmtDateTime } from "../lib/format";
import { runStatus } from "../lib/status";
import { useResource } from "../lib/useResource";
import { useAppState, useStore } from "../store/store";
import { CurrentAction } from "../workspace/Timeline";
import { AICredits } from "./AICredits";
import { auditHeadlineTone, evidenceLadder, fpgaVariants, latestProgram } from "./derive";
import { AuditHero } from "./lab/hero";
import { AnalysisCard, EvidenceLadder, RepairSummary, StageCards, StatTiles, VerifierChecklist, VulnerabilityCard } from "./lab/parts";
import { ProviderStatus } from "./ProviderStatus";
import { RepoImport } from "./RepoImport";
import {
  AuditActions, Commands, ContractCard, EvidenceCard, FormalCard, ProjectAudit, RepairCard, ScopeCard, SecurityStart, TestTable, TestWave,
  VerifyBadge,
} from "./SecurityPanel";
import { securityApi } from "../api/security";

export function SecurityLab() {
  const store = useStore();
  const auditId = useAppState((s) => s.selection.auditId);
  const all = useAppState((s) => s.security);
  const [missing, setMissing] = useState(false);
  const audit = auditId ? all[auditId] ?? null : null;

  useEffect(() => {
    setMissing(false);
    if (auditId && !all[auditId]) {
      void store.loadAudits().then((list) => setMissing(!list.some((a) => a.id === auditId))).catch(() => setMissing(true));
    }
  }, [auditId]); // eslint-disable-line react-hooks/exhaustive-deps

  if (auditId && !audit) {
    return missing
      ? <Empty title="Audit not found">It may belong to another database. <button type="button" className="text-accent hover:underline" onClick={() => store.selectAudit(null)}>Show all audits</button></Empty>
      : <Loading label="Loading audit…" />;
  }
  return audit ? <AuditView audit={audit} /> : <LabOverview />;
}

// ------------------------------------------------------------------ overview

const STEPS: Array<{ icon: ReactNode; title: string; text: string }> = [
  { icon: <IconSearch size={18} />, title: "Investigate", text: "Bitdeer or Claude reads the RTL against a written security contract and suspects weaknesses." },
  { icon: <IconBug size={18} />, title: "Reproduce", text: "A trusted Verilator testbench and Yosys formal check confirm or reject each suspicion." },
  { icon: <IconWrench size={18} />, title: "Repair", text: "Factory Droid patches an isolated copy; only allowlisted file edits are permitted." },
  { icon: <IconShieldCheck size={18} />, title: "Verify", text: "The same trusted tests and a formal proof judge the patch. The agent's word never counts." },
  { icon: <IconChip size={18} />, title: "Prove on hardware", text: "Before/after bitstreams on the Basys 3, with webcam-recorded LED evidence." },
];

function LabOverview() {
  const store = useStore();
  const all = useAppState((s) => s.security);
  const audits = useMemo(() => Object.values(all).sort((a, b) => b.created_at - a.created_at), [all]);
  useEffect(() => { void store.loadAudits().catch(() => undefined); }, [store]);
  const verified = audits.filter((a) => a.status === "repaired_verified").length;
  const reproduced = audits.filter((a) => a.record.reproduction?.security_status === "failed").length;
  return (
    <div className="space-y-6">
      <section className="sbx-hero rounded-2xl border border-line p-6 shadow-panel">
        <div className="flex flex-wrap items-end justify-between gap-4">
          <div className="max-w-2xl">
            <div className="text-[10.5px] font-semibold uppercase tracking-[0.08em] text-accent">Security Lab</div>
            <h2 className="mt-1 text-3xl font-semibold tracking-tight">Find, prove and fix <span className="sbx-gradient-text">hardware security bugs</span></h2>
            <p className="mt-2 text-sm text-muted">AI suspects, a trusted verifier decides. Every claim on this page is backed by a test run, a formal result or a webcam measurement you can open.</p>
          </div>
          <dl className="flex gap-6 text-right">
            <div><dt className="text-[11px] text-faint">audits</dt><dd className="text-2xl font-semibold tabular-nums">{audits.length}</dd></div>
            <div><dt className="text-[11px] text-faint">reproduced</dt><dd className="text-2xl font-semibold tabular-nums text-bad">{reproduced}</dd></div>
            <div><dt className="text-[11px] text-faint">repaired + verified</dt><dd className="text-2xl font-semibold tabular-nums text-ok">{verified}</dd></div>
          </dl>
        </div>
        <ol className="mt-5 grid gap-2 sm:grid-cols-2 lg:grid-cols-5">
          {STEPS.map((s, i) => (
            <li key={s.title} className="rounded-xl border border-line bg-panel/70 p-3 backdrop-blur">
              <div className="flex items-center gap-2">
                <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-accent/10 text-accent">{s.icon}</span>
                <span className="text-xs font-semibold">{i + 1}. {s.title}</span>
              </div>
              <p className="mt-1.5 text-[11.5px] leading-snug text-muted">{s.text}</p>
            </li>
          ))}
        </ol>
      </section>

      <section>
        <div className="mb-2 flex items-baseline justify-between">
          <h3 className="text-base font-semibold">Start an audit</h3>
          <span className="text-xs text-faint">the investigator follows your AI settings: offline rules unless live AI calls are on</span>
        </div>
        <SecurityStart />
      </section>

      <section>
        <h3 className="mb-2 text-base font-semibold">All audits</h3>
        <AuditTable audits={audits} />
      </section>

      <div className="grid gap-4 xl:grid-cols-2">
        <RepoImport />
        <div className="space-y-4"><ProjectAudit /><ProviderStatus /></div>
      </div>
      <AICredits />
    </div>
  );
}

function AuditTable({ audits }: { audits: SecurityAudit[] }) {
  const store = useStore();
  if (audits.length === 0) return <Empty title="No audits yet" icon={<IconSearch size={20} />}>Pick a case above to run the first audit. It costs nothing with offline rules.</Empty>;
  return (
    <div className="overflow-x-auto rounded-xl border border-line bg-panel shadow-panel">
      <table className="w-full min-w-[46rem] text-left text-xs">
        <thead className="border-b border-line bg-sunken/60 text-[11px] text-faint">
          <tr><th className="px-4 py-2 font-medium">Audit</th><th className="px-2 font-medium">Status</th><th className="px-2 font-medium">Evidence level</th>
            <th className="px-2 font-medium">Findings</th><th className="px-2 font-medium">Repairs</th><th className="px-2 font-medium">Hardware</th><th className="px-4 font-medium">Started</th><th /></tr>
        </thead>
        <tbody className="divide-y divide-line">
          {audits.map((a) => {
            const rungs = evidenceLadder(a);
            let top = -1;
            rungs.forEach((r, i) => { if (r.reached) top = i; });
            const vs = fpgaVariants(a);
            const hw = Object.keys(vs).length;
            return (
              <tr key={a.id} className="cursor-pointer transition-colors hover:bg-raised/60" onClick={() => store.selectAudit(a.id)}>
                <td className="px-4 py-2.5">
                  <div className="font-semibold text-fg">{a.record.case?.title ?? a.record.source?.label ?? a.id}</div>
                  <div className="font-mono text-[10.5px] text-faint">{a.id}{a.record.fileset?.kind ? ` · ${a.record.fileset.kind}` : ""}</div>
                </td>
                <td className="px-2"><Pill tone={auditHeadlineTone(a)}>{runStatus(a.status).label}</Pill></td>
                <td className="px-2">
                  <div className="flex items-center gap-1.5">
                    <span className="flex gap-0.5">{rungs.map((r) => <span key={r.key} title={r.label} className={cx("h-1.5 w-4 rounded-full", r.reached ? "sbx-brand-bg" : "bg-line")} />)}</span>
                    <span className="text-muted">{top >= 0 ? rungs[top].label : "none"}</span>
                  </div>
                </td>
                <td className="px-2 tabular-nums">{a.record.findings.length}</td>
                <td className="px-2 tabular-nums">{a.record.repairs.length}{a.record.repairs.some((r) => r.verdict === "verified") ? <span className="ml-1 text-ok">✓</span> : null}</td>
                <td className="px-2 text-muted">{hw ? `${hw} variant${hw === 1 ? "" : "s"}` : "–"}</td>
                <td className="px-4 text-muted">{fmtDateTime(a.created_at)}</td>
                <td className="pr-3 text-faint"><IconArrowRight size={14} /></td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ------------------------------------------------------------------ audit view

type EvidenceTab = "reproduction" | "formal" | "repairs" | "contract" | "scope" | "record";

function AuditView({ audit }: { audit: SecurityAudit }) {
  const store = useStore();
  const all = useAppState((s) => s.security);
  const approvals = useAppState((s) => s.approvals);
  const run = useAppState((s) => (audit.run_id ? s.runs[audit.run_id] ?? null : null));
  const running = audit.status === "running" || audit.status === "created";
  const siblings = useMemo(() => Object.values(all).filter((a) => a.case_id && a.case_id === audit.case_id).sort((a, b) => b.created_at - a.created_at), [all, audit.case_id]);
  const fpgaVersion = JSON.stringify(audit.record.fpga ?? {}) + audit.record.repairs.length;
  const fpga = useResource(audit.case_id && audit.record.case.board ? audit.id : null, fpgaVersion, (sig) => securityApi.fpga(audit.id, sig));
  const hold = latestProgram(approvals);
  const hasBoard = !!(audit.case_id && audit.record.case.board);

  return (
    <div className="space-y-4">
      <AuditHero audit={audit} siblings={siblings} onBack={() => store.selectAudit(null)} actions={<AuditActions audit={audit} />}
                 right={hasBoard ? (
                   <Button size="sm" variant="ghost" onClick={() => store.selectAudit(audit.id, "fpga")}>
                     <IconChip size={14} /> Open in FPGA Lab
                   </Button>
                 ) : null} />
      {run && running && <CurrentAction run={run as Run} compact />}
      <StageCards audit={audit} />
      <EvidenceLadder audit={audit} />
      <StatTiles audit={audit} fpga={fpga.data} hold={hold} />
      <div className="grid gap-4 xl:grid-cols-2">
        <VulnerabilityCard audit={audit} />
        <AnalysisCard audit={audit} />
      </div>
      <div className="grid gap-4 xl:grid-cols-[1.35fr_1fr]">
        <RepairSummary audit={audit} />
        <VerifierChecklist audit={audit} />
      </div>
      <EvidencePanels audit={audit} />
      <div className="grid gap-4 xl:grid-cols-2">
        <AICredits />
        <ProviderStatus />
      </div>
      <Card title="Limitations">
        <ul className="list-disc space-y-1 pl-4 text-xs text-muted">
          <li>Results cover this case's written contract and its trusted tests and properties only, not the whole design or chip.</li>
          <li>A formal proof holds for the property wrapper's assertions under its assumptions (reset in the first cycle).</li>
          <li>Findings are suspicions until the verifier reproduces them; offline static rules are pattern matches, not AI.</li>
          {audit.record.case?.provenance && <li>{audit.record.case.provenance}</li>}
          <li>Physical results are only claimed from webcam observations of a bitstream SiliconBreak X itself programmed.</li>
        </ul>
      </Card>
    </div>
  );
}

/** Full evidence, one tab per kind, so the page stays short. */
function EvidencePanels({ audit }: { audit: SecurityAudit }) {
  const r = audit.record;
  const [tab, setTab] = useState<EvidenceTab>(r.reproduction ? "reproduction" : "contract");
  const [sel, setSel] = useState<{ report: VerificationReport; test: TestOutcome } | null>(null);
  useEffect(() => setSel(null), [audit.id]);
  const firstFail = r.reproduction?.tests.find((t) => t.status === "fail");
  const shown = sel ?? (r.reproduction && firstFail ? { report: r.reproduction, test: firstFail } : null);
  return (
    <section className="rounded-xl border border-line bg-panel shadow-panel">
      <header className="flex flex-wrap items-center justify-between gap-2 border-b border-line px-4 pt-2">
        <div className="pb-1.5">
          <h3 className="text-sm font-semibold">Detailed evidence</h3>
          <p className="text-[11px] text-faint">exact tests, waveforms, solver output, diffs and hashes behind every number above</p>
        </div>
        <Tabs value={tab} onChange={setTab} options={[
          { value: "reproduction", label: "Trusted reproduction", disabled: !r.reproduction },
          { value: "formal", label: "Formal", disabled: !r.formal_original },
          { value: "repairs", label: "Repair attempts", count: r.repairs.length },
          { value: "contract", label: "Contract" },
          ...(r.fileset ? [{ value: "scope" as const, label: "Scope" }] : []),
          { value: "record", label: "Evidence record", disabled: !r.evidence },
        ]} />
      </header>
      <div className="space-y-3 p-4">
        {tab === "reproduction" && r.reproduction && (
          <>
            <div className="flex items-center justify-between"><span className="text-xs text-muted">Original design, trusted verifier</span><VerifyBadge report={r.reproduction} /></div>
            <TestTable report={r.reproduction} selected={shown?.report === r.reproduction ? shown.test.id : null}
                       onSelect={(t) => setSel({ report: r.reproduction!, test: t })} />
            {shown && shown.report === r.reproduction && <TestWave audit={audit} report={shown.report} test={shown.test} />}
            <Commands report={r.reproduction} />
          </>
        )}
        {tab === "formal" && r.formal_original && <FormalCard title="Formal property check · original design" f={r.formal_original} />}
        {tab === "repairs" && (
          r.repairs.length === 0 ? <p className="text-xs text-muted">No repair attempts yet.</p> : (
            <>
              {r.repairs.map((a) => (
                <RepairCard key={a.attempt} audit={audit} attempt={a} selected={shown?.report === a.verification ? shown.test.id : null}
                            onSelect={(t) => a.verification && setSel({ report: a.verification, test: t })} />
              ))}
              {shown && r.repairs.some((a) => a.verification === shown.report) && <TestWave audit={audit} report={shown.report} test={shown.test} />}
            </>
          )
        )}
        {tab === "contract" && r.case && <ContractCard c={r.case} />}
        {tab === "scope" && r.fileset && <ScopeCard audit={audit} />}
        {tab === "record" && <EvidenceCard audit={audit} />}
      </div>
    </section>
  );
}
