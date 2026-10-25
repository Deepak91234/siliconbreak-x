/**
 * Security Lab (default page). Without an open audit: choose what the AI
 * security agent audits (a security case, a public GitHub repository at a
 * pinned commit, or a Workspace design) and the list of audits. With an audit:
 * four blocks, each with its evidence expandable in place:
 *
 *   1. Audit target    what was audited, by which investigator, against which contract
 *   2. Findings        suspected weaknesses with source, hypothesis and status history
 *   3. Repair          Factory Droid (or model / reference) patches with diffs
 *   4. Verification    trusted tests + formal on the original and the patch,
 *                      waveforms, commands, hashes, evidence record, hardware result
 *
 * Everything comes from the stored audit records and live `security.updated` events.
 */

import { useEffect, useMemo, useState, type ReactNode } from "react";
import { errorMessage } from "../api/client";
import { securityApi, type Finding, type RepairAttempt, type SecurityAudit, type TestOutcome, type VerificationReport } from "../api/security";
import type { Run } from "../api/types";
import {
  IconAlert, IconArrowLeft, IconArrowRight, IconCheckCircle, IconChip, IconDownload, IconSearch, IconShieldCheck, IconTarget, IconWrench, IconXCircle,
} from "../components/icons";
import { Button, Collapsible, CopyButton, Empty, Loading, Pill, Segmented, Select, cx } from "../components/ui";
import { fmtDateTime, fmtDuration } from "../lib/format";
import { isAuditProject } from "../lib/sections";
import { findingStatus, formalStatus, runStatus } from "../lib/status";
import { toast } from "../lib/toast";
import { useResource } from "../lib/useResource";
import { useAppState, useStore } from "../store/store";
import { DiffView, type DiffMode } from "../tabs/DiffView";
import { CurrentAction } from "../workspace/Timeline";
import { agentLabel, auditHeadlineTone, counts, diffStat, fpgaVariants, keyRepair, latestObservation, providerLabel, variantName } from "./derive";
import { downloadArtifact } from "./lab/hero";
import { SourceExcerpt } from "./lab/parts";
import { RepoImport } from "./RepoImport";
import {
  AuditActions, Commands, ContractCard, EvidenceCard, FormalCard, RepairCard, ScopeCard, SOURCE_KIND_LABEL, SecurityStart, TestTable, TestWave,
  VerifyBadge,
} from "./SecurityPanel";

type Sel = { report: VerificationReport; test: TestOutcome } | null;

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
  return audit ? <AuditView audit={audit} /> : <LabHome />;
}

// ------------------------------------------------------------------ home: target + audits

type Target = "case" | "repo" | "design";

function LabHome() {
  const store = useStore();
  const all = useAppState((s) => s.security);
  const audits = useMemo(() => Object.values(all).sort((a, b) => b.created_at - a.created_at), [all]);
  const [target, setTarget] = useState<Target>("case");
  useEffect(() => { void store.loadAudits().catch(() => undefined); }, [store]);
  return (
    <div className="mx-auto max-w-6xl space-y-5">
      <section className="sbx-hero rounded-2xl border border-line p-5 shadow-panel">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <div className="text-[10.5px] font-semibold uppercase tracking-[0.08em] text-accent">AI security agent</div>
            <h2 className="mt-1 text-2xl font-semibold tracking-tight">What should the agent audit?</h2>
            <p className="mt-1 max-w-2xl text-sm text-muted">
              The agent suspects weaknesses, a trusted verifier reproduces them, Factory Droid repairs an isolated copy, and the verifier
              decides. The investigator follows Settings & usage: offline rules unless live AI is on.
            </p>
          </div>
          <Segmented size="md" value={target} onChange={setTarget} options={[
            { value: "case", label: "Security case" }, { value: "repo", label: "GitHub repository" }, { value: "design", label: "Workspace design" }]} />
        </div>
        <div className="mt-4">
          {target === "case" && <SecurityStart />}
          {target === "repo" && <RepoImport />}
          {target === "design" && <DesignTarget />}
        </div>
      </section>

      <section>
        <h3 className="mb-2 text-sm font-semibold">Audits</h3>
        {audits.length === 0 ? (
          <Empty title="No audits yet" icon={<IconSearch size={20} />}>Pick a target above to run the first audit.</Empty>
        ) : (
          <div className="overflow-x-auto rounded-xl border border-line bg-panel shadow-panel">
            <table className="w-full min-w-[40rem] text-left text-xs">
              <thead className="border-b border-line bg-sunken/60 text-[11px] text-faint">
                <tr><th className="px-4 py-2 font-medium">Target</th><th className="px-2 font-medium">Status</th><th className="px-2 font-medium">Findings</th>
                  <th className="px-2 font-medium">Repair</th><th className="px-4 font-medium">Started</th><th /></tr>
              </thead>
              <tbody className="divide-y divide-line">
                {audits.map((a) => {
                  const rep = keyRepair(a);
                  return (
                    <tr key={a.id} className="cursor-pointer transition-colors hover:bg-raised/60" onClick={() => store.selectAudit(a.id)}>
                      <td className="px-4 py-2.5">
                        <div className="font-semibold text-fg">{a.record.case?.title ?? a.record.source?.label ?? a.id}</div>
                        <div className="text-[10.5px] text-faint">{a.record.fileset?.kind ?? "security case"} · <span className="font-mono">{a.id}</span></div>
                      </td>
                      <td className="px-2"><Pill tone={auditHeadlineTone(a)}>{runStatus(a.status).label}</Pill></td>
                      <td className="px-2 tabular-nums">{a.record.findings.length}</td>
                      <td className="px-2 text-muted">{rep ? `${agentLabel(rep)} · ${rep.verdict === "verified" ? "verified" : rep.verdict}` : "–"}</td>
                      <td className="px-4 text-muted">{fmtDateTime(a.created_at)}</td>
                      <td className="pr-3 text-faint"><IconArrowRight size={14} /></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}

/** Audit one Workspace design's generated RTL against the generic hardware-security contract. */
function DesignTarget() {
  const store = useStore();
  const projects = useAppState((s) => s.projects);
  const selected = useAppState((s) => s.selection.projectId);
  const designs = (projects ?? []).filter((p) => !isAuditProject(p.title) && p.revision_count > 0);
  const [pid, setPid] = useState<string>(selected && designs.some((d) => d.id === selected) ? selected : designs[0]?.id ?? "");
  const [busy, setBusy] = useState(false);
  if (designs.length === 0) return <p className="text-xs text-muted">No Workspace design with generated RTL yet. Build one in the Workspace AI Builder first.</p>;
  return (
    <div className="flex flex-wrap items-center gap-2 rounded-xl border border-line bg-panel p-4">
      <Select value={pid} onChange={setPid} className="min-w-[16rem]">
        {designs.map((d) => <option key={d.id} value={d.id}>{d.title} ({d.revision_count} rev)</option>)}
      </Select>
      <Button size="sm" variant="primary" busy={busy} disabled={!pid}
              onClick={async () => { setBusy(true); try { await store.startProjectAudit(pid); } catch (e) { toast(errorMessage(e), "error"); } finally { setBusy(false); } }}>
        Audit this design
      </Button>
      <p className="basis-full text-[11px] text-faint">Generated designs have no trusted security tests, so findings stay suspected and no repair runs.</p>
    </div>
  );
}

// ------------------------------------------------------------------ audit view

function Block({ n, icon, title, subtitle, right, children }: { n: number; icon: ReactNode; title: string; subtitle?: ReactNode; right?: ReactNode; children: ReactNode }) {
  return (
    <section className="rounded-xl border border-line bg-panel shadow-panel">
      <header className="flex flex-wrap items-center gap-3 border-b border-line px-4 py-3">
        <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-accent/10 text-accent">{icon}</span>
        <div className="min-w-0 flex-1">
          <h3 className="text-sm font-semibold"><span className="mr-1.5 text-faint">{n}.</span>{title}</h3>
          {subtitle && <div className="text-[11px] text-muted">{subtitle}</div>}
        </div>
        {right}
      </header>
      <div className="space-y-3 p-4">{children}</div>
    </section>
  );
}

function AuditView({ audit }: { audit: SecurityAudit }) {
  const store = useStore();
  const all = useAppState((s) => s.security);
  const run = useAppState((s) => (audit.run_id ? s.runs[audit.run_id] ?? null : null));
  const running = audit.status === "running" || audit.status === "created";
  const siblings = useMemo(() => Object.values(all).filter((a) => a.case_id && a.case_id === audit.case_id).sort((a, b) => b.created_at - a.created_at), [all, audit.case_id]);
  const r = audit.record;
  const hasBoard = !!(audit.case_id && r.case?.board);
  const [exporting, setExporting] = useState(false);
  const [sel, setSel] = useState<Sel>(null);
  useEffect(() => setSel(null), [audit.id]);

  return (
    <div className="mx-auto max-w-6xl space-y-4">
      <section className="sbx-hero rounded-2xl border border-line p-5 shadow-panel">
        <button type="button" onClick={() => store.selectAudit(null)} className="mb-2 inline-flex items-center gap-1.5 text-xs font-medium text-accent hover:underline">
          <IconArrowLeft size={14} /> All audits
        </button>
        <div className="flex flex-wrap items-start gap-3">
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2.5">
              <h2 className="text-2xl font-semibold tracking-tight">{r.case?.title ?? r.source?.label ?? audit.id}</h2>
              <Pill tone={auditHeadlineTone(audit)} className="!px-2.5 !py-1 !text-xs">{runStatus(audit.status).label}</Pill>
            </div>
            <p className="mt-1 max-w-3xl text-sm text-muted">{r.outcome?.summary || "Audit running…"}</p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {siblings.length > 1 && (
              <Select value={audit.id} onChange={(id) => store.selectAudit(id)} className="max-w-[14rem]" title="Other audits of this case">
                {siblings.map((a) => <option key={a.id} value={a.id}>{fmtDateTime(a.created_at)} · {runStatus(a.status).label}</option>)}
              </Select>
            )}
            {r.evidence && (
              <Button size="sm" variant="ghost" busy={exporting} title="Download report.md from the evidence record"
                      onClick={async () => { setExporting(true); await downloadArtifact(audit, "report.md"); setExporting(false); }}>
                <IconDownload size={14} /> Report
              </Button>
            )}
          </div>
        </div>
        <div className="mt-4 flex flex-wrap items-center gap-2 border-t border-line pt-3">
          <AuditActions audit={audit} />
          {hasBoard && (
            <Button size="sm" variant="secondary" className="ml-auto" onClick={() => store.selectAudit(audit.id, "fpga")}>
              <IconChip size={14} /> Test on FPGA
            </Button>
          )}
        </div>
        {run && running && <div className="mt-3"><CurrentAction run={run as Run} compact /></div>}
      </section>

      <TargetBlock audit={audit} />
      <FindingsBlock audit={audit} />
      <RepairBlock audit={audit} sel={sel} onSelect={setSel} />
      <VerificationBlock audit={audit} sel={sel} onSelect={setSel} />

      <Collapsible title={<span className="text-xs">Limitations</span>}>
        <ul className="list-disc space-y-1 pl-4 text-xs text-muted">
          <li>Results cover this case's written contract and its trusted tests and properties only, not the whole design or chip.</li>
          <li>A formal proof holds for the property wrapper's assertions under its assumptions (reset in the first cycle).</li>
          <li>Findings are suspicions until the verifier reproduces them; offline static rules are pattern matches, not AI.</li>
          {r.case?.provenance && <li>{r.case.provenance}</li>}
          <li>Physical results are only claimed from webcam observations of a bitstream SiliconBreak X itself programmed.</li>
        </ul>
      </Collapsible>
    </div>
  );
}

// 1 ------------------------------------------------------------------
function TargetBlock({ audit }: { audit: SecurityAudit }) {
  const r = audit.record;
  const inv = r.investigation;
  const c = r.case;
  const fs = r.fileset;
  const targetLine = fs?.kind === "repository" ? `${fs.url?.replace("https://github.com/", "")} @ ${fs.sha?.slice(0, 10)}`
    : fs?.kind === "project" ? "Workspace design" : c ? `${SOURCE_KIND_LABEL[c.source_kind] ?? c.source_kind} · ${c.top_module}` : "–";
  return (
    <Block n={1} icon={<IconTarget size={17} />} title="Audit target & AI security agent"
           subtitle={<>{targetLine} · investigator <span className="text-fg">{inv ? providerLabel(inv.provider, inv.model) : "pending"}</span>{inv?.duration_s != null ? ` · ${fmtDuration(inv.duration_s)}` : ""}</>}>
      {c && <p className="text-sm text-fg">{c.security_contract.summary}</p>}
      {c && (
        <div className="flex flex-wrap gap-1.5">
          {c.cwe.map((w) => <span key={w} className="rounded-md bg-raised px-1.5 py-0.5 font-mono text-[10.5px] text-muted">{w}</span>)}
          {c.security_contract.properties.map((p) => <span key={p.id} title={p.text} className="rounded-md bg-accent/10 px-1.5 py-0.5 font-mono text-[10.5px] text-accent">{p.id}</span>)}
        </div>
      )}
      {inv?.status === "error" && (
        <p className="rounded-lg border border-warn/40 bg-warn/5 px-3 py-2 text-xs text-warn">
          {inv.provider} failed: {inv.error?.message}. {inv.fallback ? `Fell back to ${inv.fallback.provider} (not AI).` : ""}
        </p>
      )}
      <div className="grid gap-2 lg:grid-cols-2">
        {c && <Collapsible title="Contract, interface and board mapping"><ContractCard c={c} /></Collapsible>}
        {fs && <Collapsible title={fs.kind === "repository" ? "Repository scope" : "Design scope"}><ScopeCard audit={audit} /></Collapsible>}
        {inv && (
          <Collapsible title="What the agent reviewed">
            <div className="space-y-1.5 text-xs">
              {inv.summary && <p className="text-muted">{inv.summary}</p>}
              {inv.areas_reviewed.length > 0 && <ul className="list-disc pl-4 text-muted">{inv.areas_reviewed.map((x) => <li key={x}>{x}</li>)}</ul>}
              {inv.files.length > 0 && <p className="font-mono text-[11px] text-faint">{inv.files.join(", ")}</p>}
              {inv.note && <p className="text-[11px] text-faint">{inv.note}</p>}
            </div>
          </Collapsible>
        )}
      </div>
    </Block>
  );
}

// 2 ------------------------------------------------------------------
function FindingsBlock({ audit }: { audit: SecurityAudit }) {
  const r = audit.record;
  const inv = r.investigation;
  const src = useResource(r.findings.length ? audit.id : null, 0, (sig) => securityApi.sources(audit.id, undefined, sig));
  return (
    <Block n={2} icon={<IconAlert size={17} />} title="Findings"
           subtitle={inv ? `${r.findings.length} suspected weakness${r.findings.length === 1 ? "" : "es"}; status changes only on verifier evidence` : "investigation has not run yet"}>
      {inv && r.findings.length === 0 && <p className="text-xs text-muted">The investigator reported no suspected contract violation.</p>}
      {r.findings.map((f) => <FindingItem key={f.finding_id} f={f} text={src.data?.original?.[f.file]} />)}
      {r.unexplained_failures.length > 0 && (
        <div className="rounded-lg border border-orange/40 bg-orange/5 px-3 py-2 text-xs">
          <div className="font-medium text-orange">Verifier failures no finding explains (missed by the investigator)</div>
          {r.unexplained_failures.map((u) => <div key={u.test} className="mt-0.5 text-muted"><span className="font-mono">{u.test}</span> ({u.properties.join(", ")}): {u.message}</div>)}
        </div>
      )}
      {inv && inv.dropped.length > 0 && (
        <Collapsible title={<span className="text-xs text-faint">{inv.dropped.length} unverifiable model claim(s) dropped by the validator</span>}>
          <ul className="list-disc pl-4 text-2xs text-muted">{inv.dropped.map((d) => <li key={d}>{d}</li>)}</ul>
        </Collapsible>
      )}
    </Block>
  );
}

function FindingItem({ f, text }: { f: Finding; text: string | undefined }) {
  const st = findingStatus(f.status);
  return (
    <div className="rounded-xl border border-line">
      <div className="flex flex-wrap items-center gap-2 border-b border-line px-3 py-2.5">
        <Pill tone={st.tone}>{st.label}</Pill>
        <span className="min-w-0 flex-1 text-sm font-semibold">{f.title}</span>
        <Pill tone={f.confidence === "high" ? "bad" : f.confidence === "medium" ? "warn" : "idle"}>{f.confidence} confidence</Pill>
        {f.permalink ? <a href={f.permalink} target="_blank" rel="noreferrer" className="font-mono text-[11px] text-accent hover:underline">{f.file}:{f.line_start}-{f.line_end}</a>
          : <span className="font-mono text-[11px] text-faint">{f.file}:{f.line_start}-{f.line_end}</span>}
      </div>
      <div className="grid gap-3 p-3 lg:grid-cols-[1.1fr_1fr]">
        <SourceExcerpt f={f} text={text} />
        <div className="space-y-1.5 text-xs">
          <div><span className="text-faint">Weakness </span>{f.weakness_class} · violates <span className="font-mono">{f.violated_properties.join(", ")}</span></div>
          <div><span className="text-faint">Found by </span>{f.is_ai ? providerLabel(f.provider, f.model) : `${f.provider}${f.rule ? ` rule ${f.rule}` : ""} (not AI)`}</div>
          <p className="text-muted">{f.hypothesis}</p>
        </div>
      </div>
      <div className="px-3 pb-3">
        <Collapsible title={<span className="text-xs">Explanation and status history</span>}>
          <div className="space-y-2 text-xs">
            {f.severity_reasoning && <div><div className="font-semibold">Impact</div><p className="text-muted">{f.severity_reasoning}</p></div>}
            <div><div className="font-semibold">How the verifier checks it</div><p className="text-muted">{f.proposed_test}</p></div>
            {f.limitations && <div><div className="font-semibold">Limitations</div><p className="text-muted">{f.limitations}</p></div>}
            <ol className="space-y-1 border-l-2 border-line pl-3">
              {f.history.map((h, i) => <li key={i} className="text-[11.5px] text-muted"><Pill tone={findingStatus(h.status).tone} className="mr-1.5">{findingStatus(h.status).label}</Pill>{h.why}</li>)}
            </ol>
          </div>
        </Collapsible>
      </div>
    </div>
  );
}

// 3 ------------------------------------------------------------------
function RepairBlock({ audit, sel, onSelect }: { audit: SecurityAudit; sel: Sel; onSelect: (s: Sel) => void }) {
  const r = audit.record;
  const key = keyRepair(audit);
  const [mode, setMode] = useState<DiffMode>("unified");
  const others = r.repairs.filter((a) => a !== key);
  return (
    <Block n={3} icon={<IconWrench size={17} />} title="Autonomous repair"
           subtitle={key ? `${agentLabel(key)} · attempt ${key.attempt} of ${r.repairs.length}${key.duration_s != null ? ` · ${fmtDuration(key.duration_s)}` : ""}`
             : "Factory Droid edits an isolated copy; only allowlisted file edits are approved"}
           right={key && <Pill tone={key.verdict === "verified" ? "ok" : key.verdict === "no_patch" ? "orange" : "bad"}>
             {key.verdict === "verified" ? (key.agent === "reference" ? "reference passes (harness validated)" : "verifier accepted") : key.verdict}</Pill>}>
      {!key && (
        <p className="text-xs text-muted">{audit.case_id ? (r.reproduction?.security_status === "failed" ? "No repair yet. Use Repair with Factory Droid above."
          : "Repairs start once the verifier has reproduced a vulnerability.") : "Repairs need a case with trusted tests."}</p>
      )}
      {key && <RepairDetail audit={audit} a={key} mode={mode} setMode={setMode} />}
      {others.length > 0 && (
        <Collapsible title={<span className="text-xs">Other attempts ({others.length}), with their retests</span>}>
          <div className="space-y-3">
            {others.map((a) => (
              <RepairCard key={a.attempt} audit={audit} attempt={a} selected={sel && a.verification && sel.report === a.verification ? sel.test.id : null}
                          onSelect={(t) => a.verification && onSelect({ report: a.verification, test: t })} />
            ))}
            {sel && others.some((a) => a.verification && a.verification === sel.report) && <TestWave audit={audit} report={sel.report} test={sel.test} />}
          </div>
        </Collapsible>
      )}
    </Block>
  );
}

function RepairDetail({ audit, a, mode, setMode }: { audit: SecurityAudit; a: RepairAttempt; mode: DiffMode; setMode: (m: DiffMode) => void }) {
  const d = diffStat(a.diff);
  return (
    <div className="space-y-3">
      {a.agent === "reference" && <p className="rounded-lg border border-violet/40 bg-violet/5 px-3 py-2 text-xs text-violet">Human-written reference fix: shows the harness accepts a correct repair. Not an agent repair.</p>}
      {a.final_text && <p className="whitespace-pre-line text-xs text-fg">{a.final_text}</p>}
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-muted">
        {a.changed_files.length > 0 && <span className="font-mono">{a.changed_files.join(", ")} <span className="text-ok">+{d.add}</span> <span className="text-bad">-{d.del}</span></span>}
        {a.model && <span>model {a.model}</span>}
        {a.session_id && <span>session <span className="font-mono">{a.session_id}</span></span>}
        {(a.allowed_actions?.length ?? 0) > 0 && <span className="text-ok">{a.allowed_actions!.length} edit(s) approved</span>}
        {(a.denied_actions?.length ?? 0) > 0 && <span className="text-warn">{a.denied_actions!.length} action(s) refused</span>}
        {a.reason && <span>{a.reason}</span>}
      </div>
      {a.scope_violations.length > 0 && <p className="text-xs text-bad">Scope violations: {a.scope_violations.join("; ")}</p>}
      {a.diff ? (
        <div className="overflow-hidden rounded-lg border border-line">
          <div className="flex items-center justify-between border-b border-line bg-sunken/60 px-2.5 py-1.5">
            <span className="text-[11px] text-faint">patch.diff</span>
            <span className="flex items-center gap-2"><CopyButton text={a.diff} label="copy patch" />
              <Segmented value={mode} onChange={setMode} options={[{ value: "unified", label: "Unified" }, { value: "split", label: "Split" }]} /></span>
          </div>
          <DiffView diff={a.diff} mode={mode} lang="verilog" />
        </div>
      ) : <p className="text-xs text-muted">No diff.</p>}
      <Collapsible title={<span className="text-xs">Agent task, trace and permissions</span>}>
        <div className="space-y-2 text-xs">
          {(a.denied_actions?.length ?? 0) > 0 && <p className="text-warn">Refused: {a.denied_actions!.join("; ")}</p>}
          {(a.allowed_actions?.length ?? 0) > 0 && <p className="text-muted">Approved: {a.allowed_actions!.join("; ")}</p>}
          <AttemptFiles audit={audit} a={a} />
        </div>
      </Collapsible>
    </div>
  );
}

function AttemptFiles({ audit, a }: { audit: SecurityAudit; a: RepairAttempt }) {
  const [open, setOpen] = useState<string | null>(null);
  const paths = [`${a.dir}/workspace/TASK.md`, ...(a.trace ? [`${a.dir}/${a.trace}`] : [])];
  const res = useResource(open ? `${audit.id}:${open}` : null, 0, (sig) => securityApi.file(audit.id, open!, sig));
  return (
    <div>
      <div className="flex flex-wrap gap-2">
        {paths.map((p) => (
          <button key={p} type="button" onClick={() => setOpen((o) => (o === p ? null : p))}
                  className={cx("rounded px-1.5 py-0.5 font-mono text-2xs hover:bg-raised", open === p ? "text-accent" : "text-faint")}>{p.split("/").slice(-1)[0]}</button>
        ))}
      </div>
      {open && (res.error ? <p className="mt-1 text-2xs text-bad">{errorMessage(res.error)}</p>
        : res.data ? <pre className="mt-1 max-h-72 overflow-auto rounded-lg border border-line bg-sunken p-2 font-mono text-2xs">{res.data.text}</pre> : null)}
    </div>
  );
}

// 4 ------------------------------------------------------------------
function Row({ ok, label, value, children }: { ok: boolean | null; label: string; value: ReactNode; children?: ReactNode }) {
  return (
    <div className="rounded-xl border border-line">
      <div className="flex flex-wrap items-center gap-2.5 px-3 py-2.5">
        {ok === true ? <IconCheckCircle size={18} className="text-ok" /> : ok === false ? <IconXCircle size={18} className="text-bad" />
          : <span className="h-[16px] w-[16px] rounded-full border-2 border-dashed border-line-strong" />}
        <span className="text-xs font-semibold">{label}</span>
        <span className="min-w-0 flex-1 text-xs text-muted">{value}</span>
      </div>
      {children && <div className="space-y-2 border-t border-line px-3 py-2.5">{children}</div>}
    </div>
  );
}

function VerificationBlock({ audit, sel, onSelect }: { audit: SecurityAudit; sel: Sel; onSelect: (s: Sel) => void }) {
  const store = useStore();
  const r = audit.record;
  const rep = r.reproduction;
  const key = keyRepair(audit);
  const v = key?.verification ?? null;
  const co = counts(rep);
  const cp = counts(v);
  const firstFail = rep?.tests.find((t) => t.status === "fail");
  const shownOrig = sel && rep && sel.report === rep ? sel : rep && firstFail ? { report: rep, test: firstFail } : null;
  const vs = fpgaVariants(audit);
  const obs = Object.keys(vs).map((k) => latestObservation(vs[k])).filter(Boolean) as NonNullable<ReturnType<typeof latestObservation>>[];
  const hasBoard = !!(audit.case_id && r.case?.board);
  const origOk = rep ? (rep.status === "error" || rep.status === "timeout" ? false : rep.security_status === "failed" ? true : null) : null;
  return (
    <Block n={4} icon={<IconShieldCheck size={17} />} title="Independent verification"
           subtitle="trusted Verilator tests and Yosys formal, run by SiliconBreak X; the agents' own claims never count">
      <Row ok={origOk} label="Original design"
           value={rep ? (rep.status === "error" || rep.status === "timeout" ? `verifier ${rep.status}: ${rep.message}`
             : rep.security_status === "failed" ? `vulnerability reproduced: ${co.secFail}/${co.secTotal} security tests fail, functional ${co.funPass}/${co.funTotal} pass${r.formal_original ? `, formal ${formalStatus(r.formal_original.status).label}` : ""}`
               : `not reproduced: security ${co.secPass}/${co.secTotal} pass`) : audit.case_id ? "not run yet" : "no trusted tests for this target (findings stay suspected)"}>
        {rep && (
          <Collapsible title={<span className="text-xs">Test results, waveforms and commands</span>} right={<VerifyBadge report={rep} />}>
            <div className="space-y-3">
              <TestTable report={rep} selected={shownOrig ? shownOrig.test.id : null} onSelect={(t) => onSelect({ report: rep, test: t })} />
              {shownOrig && <TestWave audit={audit} report={shownOrig.report} test={shownOrig.test} />}
              <Commands report={rep} />
            </div>
          </Collapsible>
        )}
        {r.formal_original && <Collapsible title={<span className="text-xs">Formal property check on the original</span>}><FormalCard title="Formal property check · original design" f={r.formal_original} /></Collapsible>}
      </Row>

      <Row ok={key && v ? key.verdict === "verified" : null} label={key ? `Patched design (attempt ${key.attempt})` : "Patched design"}
           value={key && v ? `${key.verdict === "verified" ? "accepted" : "rejected"}: ${cp.allPass}/${cp.allTotal} trusted tests pass (security ${cp.secPass}/${cp.secTotal}, functional ${cp.funPass}/${cp.funTotal})${key.formal ? `, formal ${formalStatus(key.formal.status).label}` : ""}`
             : "no patch to verify yet"}>
        {key && v && (
          <Collapsible title={<span className="text-xs">Retest results and waveforms</span>} right={<VerifyBadge report={v} />}>
            <div className="space-y-3">
              <TestTable report={v} selected={sel && sel.report === v ? sel.test.id : null} onSelect={(t) => onSelect({ report: v, test: t })} />
              {sel && sel.report === v && <TestWave audit={audit} report={v} test={sel.test} />}
              <Commands report={v} />
            </div>
          </Collapsible>
        )}
        {key?.formal && <Collapsible title={<span className="text-xs">Formal proof on the patch</span>}><FormalCard title="Formal property check · patched design" f={key.formal} /></Collapsible>}
      </Row>

      {hasBoard && (
        <Row ok={obs.length ? (obs.some((o) => o.verdict === "contract_met") ? true : obs.some((o) => o.verdict === "unexpected") ? false : null) : null}
             label="On the Basys 3 (webcam)"
             value={obs.length ? obs.map((o) => `${variantName(o.variant)}: ${o.summary}`).join(" · ") : "not tested on hardware yet"}>
          <div><Button size="sm" variant="secondary" onClick={() => store.selectAudit(audit.id, "fpga")}><IconChip size={14} /> Test on FPGA</Button></div>
        </Row>
      )}

      {r.evidence && (
        <Collapsible title={<span className="text-xs">Evidence record ({r.evidence.artifacts.length} artifacts with SHA-256)</span>}>
          <EvidenceCard audit={audit} />
        </Collapsible>
      )}
    </Block>
  );
}
