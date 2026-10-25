/**
 * Building blocks shared by the Dashboard and the Security Lab audit view.
 * Each one renders a slice of the stored audit record (via derive.ts); none of
 * them fabricates a value: missing data is shown as pending / not run.
 */

import { useMemo, useState, type ReactNode } from "react";
import { securityApi, type Finding, type FpgaInfo, type SecurityAudit } from "../../api/security";
import {
  IconAlert, IconBug, IconCheck, IconCheckCircle, IconChip, IconClock, IconEye, IconFingerprint, IconSearch, IconShieldCheck,
  IconWrench, IconXCircle,
} from "../../components/icons";
import { CopyButton, Pill, Segmented, Spinner, cx } from "../../components/ui";
import { fmtDuration } from "../../lib/format";
import { highlightLines } from "../../lib/highlight";
import { findingStatus, formalStatus, type Tone } from "../../lib/status";
import { useResource } from "../../lib/useResource";
import { DiffView, type DiffMode } from "../../tabs/DiffView";
import { SecurityWave } from "../SecurityWave";
import {
  counts, evidenceLadder, fpgaVariants, keyRepair, latestObservation, macroStages, providerLabel, variantName, variantOf,
  type BoardHold, type MacroStage, type StageState,
} from "../derive";

const STAGE_ICON: Record<MacroStage["key"], ReactNode> = {
  investigate: <IconSearch size={18} />,
  reproduce: <IconBug size={18} />,
  repair: <IconWrench size={18} />,
  verify: <IconShieldCheck size={18} />,
  fpga: <IconChip size={18} />,
};

const STATE_TONE: Record<StageState, Tone> = { passed: "ok", failed: "bad", running: "accent", pending: "idle", warning: "warn" };

function StateMark({ state }: { state: StageState }) {
  if (state === "passed") return <span className="flex h-6 w-6 items-center justify-center rounded-full bg-ok text-white shadow-[0_0_14px_-2px_rgb(var(--ok)/0.7)]"><IconCheck size={14} strokeWidth={2.6} /></span>;
  if (state === "failed") return <span className="flex h-6 w-6 items-center justify-center rounded-full bg-bad text-white"><IconXCircle size={14} /></span>;
  if (state === "running") return <span className="flex h-6 w-6 items-center justify-center rounded-full bg-accent/15 text-accent"><Spinner className="h-3.5 w-3.5" /></span>;
  if (state === "warning") return <span className="flex h-6 w-6 items-center justify-center rounded-full bg-warn/20 text-warn"><IconAlert size={13} /></span>;
  return <span className="h-6 w-6 rounded-full border-2 border-dashed border-line-strong" />;
}

/** The five-stage pipeline (investigate → reproduce → repair → verify → FPGA) with facts and durations. */
export function StageCards({ audit, compact = false }: { audit: SecurityAudit; compact?: boolean }) {
  const stages = macroStages(audit);
  return (
    <ol className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5" aria-label="Audit pipeline">
      {stages.map((s, i) => {
        const tone = STATE_TONE[s.state];
        return (
          <li key={s.key} className={cx("relative flex min-w-0 flex-col rounded-xl border bg-panel p-3.5 shadow-panel transition-colors",
            s.state === "running" ? "border-accent/50" : s.state === "passed" ? "border-ok/25" : "border-line")}>
            {i < stages.length - 1 && (
              <span className="absolute -right-[11px] top-7 z-10 hidden h-[2px] w-[10px] lg:block" aria-hidden
                    style={{ background: s.state === "passed" ? "rgb(var(--ok) / 0.6)" : "rgb(var(--line-strong))" }} />
            )}
            <div className="flex items-start justify-between gap-2">
              <span className={cx("flex h-9 w-9 items-center justify-center rounded-xl ring-1 ring-inset",
                tone === "ok" ? "bg-ok/10 text-ok ring-ok/25" : tone === "bad" ? "bg-bad/10 text-bad ring-bad/25" : tone === "accent" ? "bg-accent/10 text-accent ring-accent/25"
                  : tone === "warn" ? "bg-warn/10 text-warn ring-warn/25" : "bg-raised text-faint ring-line")}>
                {STAGE_ICON[s.key]}
              </span>
              <StateMark state={s.state} />
            </div>
            <div className="mt-2.5 text-[13px] font-semibold text-fg">{i + 1}. {s.title}</div>
            <div className={cx("text-xs font-medium", s.state === "pending" ? "text-faint" : tone === "bad" ? "text-bad" : tone === "warn" ? "text-warn" : "text-muted")}>{s.headline}</div>
            {!compact && s.facts.length > 0 && (
              <ul className="mt-2 space-y-1">
                {s.facts.map((f) => (
                  <li key={f} className="flex items-start gap-1.5 text-[11.5px] text-muted">
                    <span className={cx("mt-[5px] h-1 w-1 shrink-0 rounded-full", s.state === "passed" ? "bg-ok" : "bg-faint")} />
                    <span className="min-w-0 break-words">{f}</span>
                  </li>
                ))}
              </ul>
            )}
            <div className="mt-auto pt-2.5 text-[11px] text-faint">
              <span className="inline-flex items-center gap-1"><IconClock size={12} />{s.duration_s != null ? fmtDuration(s.duration_s) : "–"}</span>
            </div>
          </li>
        );
      })}
    </ol>
  );
}

/** Suspected → reproduced → repaired → independently verified → physically observed. */
export function EvidenceLadder({ audit }: { audit: SecurityAudit }) {
  const rungs = evidenceLadder(audit);
  return (
    <div className="rounded-xl border border-line bg-panel p-4 shadow-panel">
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="text-sm font-semibold">Evidence level</h3>
        <span className="text-2xs text-faint">AI findings are suspicions; only the trusted verifier and the webcam count as evidence</span>
      </div>
      <ol className="grid gap-2 md:grid-cols-5">
        {rungs.map((r) => {
          const on = r.reached;
          return (
            <li key={r.key} className={cx("relative rounded-lg border px-3 py-2.5", on ? "border-transparent" : "border-dashed border-line")}
                style={on ? { background: `rgb(var(--${r.tone === "bad" ? "bad" : r.tone === "warn" ? "warn" : r.tone === "info" ? "info" : r.tone === "violet" ? "violet" : "ok"}) / 0.10)`,
                               boxShadow: `inset 0 0 0 1px rgb(var(--${r.tone === "bad" ? "bad" : r.tone === "warn" ? "warn" : r.tone === "info" ? "info" : r.tone === "violet" ? "violet" : "ok"}) / 0.35)` } : undefined}>
              <div className="flex items-center gap-1.5">
                {on ? <IconCheckCircle size={15} className={`text-${r.tone}`} /> : <span className="h-[15px] w-[15px] rounded-full border-2 border-line-strong" />}
                <span className={cx("text-xs font-semibold", on ? "text-fg" : "text-faint")}>{r.label}</span>
              </div>
              <p className={cx("mt-1 text-[11px] leading-snug", on ? "text-muted" : "text-faint")}>{r.detail}</p>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

function Tile({ icon, tone, label, value, sub, foot }: { icon: ReactNode; tone: Tone; label: string; value: ReactNode; sub?: ReactNode; foot?: ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col rounded-xl border border-line bg-panel p-4 shadow-panel">
      <div className="flex items-start gap-3">
        <span className={cx("flex h-11 w-11 shrink-0 items-center justify-center rounded-xl ring-1 ring-inset",
          { ok: "bg-ok/10 text-ok ring-ok/25", bad: "bg-bad/10 text-bad ring-bad/25", warn: "bg-warn/10 text-warn ring-warn/25", info: "bg-info/10 text-info ring-info/25",
            accent: "bg-accent/10 text-accent ring-accent/25", violet: "bg-violet/10 text-violet ring-violet/25", orange: "bg-orange/10 text-orange ring-orange/25",
            idle: "bg-raised text-faint ring-line", muted: "bg-raised text-muted ring-line" }[tone])}>{icon}</span>
        <div className="min-w-0">
          <div className="text-xs font-medium text-muted">{label}</div>
          <div className={cx("truncate text-xl font-semibold tracking-tight", `text-${tone === "idle" || tone === "muted" ? "fg" : tone}`)}>{value}</div>
          {sub && <div className="truncate text-[11px] text-faint">{sub}</div>}
        </div>
      </div>
      {foot && <div className="mt-3 border-t border-line pt-2 text-[11px] text-muted">{foot}</div>}
    </div>
  );
}

/** Four headline numbers: trusted tests, formal, physical (webcam) result, and the bitstream. */
export function StatTiles({ audit, fpga, hold }: { audit: SecurityAudit; fpga: FpgaInfo | null; hold: BoardHold | null }) {
  const r = audit.record;
  const repair = keyRepair(audit);
  const verified = repair?.verdict === "verified";
  const report = verified ? repair!.verification : r.reproduction;
  const c = counts(report);
  const formal = verified ? repair!.formal : r.formal_original;
  const fst = formalStatus(formal?.status);
  const vs = fpgaVariants(audit);
  const afterKey = repair ? variantOf(repair) : null;
  const obsAfter = afterKey ? latestObservation(vs[afterKey]) : null;
  const obsBefore = latestObservation(vs.vulnerable);
  const after = afterKey ? vs[afterKey] : undefined;
  const sha = after?.build?.bitstream_sha256 ?? vs.vulnerable?.build?.bitstream_sha256 ?? null;
  const shaVariant = after?.build?.bitstream_sha256 ? afterKey! : "vulnerable";
  const onBoard = !!sha && hold?.sha === sha;
  const confirmed = fpga?.provenance?.[shaVariant]?.confirmed;

  let phys: { tone: Tone; value: string; sub: string };
  if (obsAfter && obsAfter.verdict === "contract_met") phys = { tone: "ok", value: "Fix confirmed", sub: `${variantName(obsAfter.variant)}: every demo step met the contract (webcam)` };
  else if (obsAfter) phys = { tone: obsAfter.verdict === "unexpected" ? "warn" : "orange", value: obsAfter.verdict === "unexpected" ? "Board ≠ simulation" : "Inconclusive", sub: obsAfter.summary };
  else if (obsBefore?.verdict === "violation_observed") phys = { tone: "bad", value: "Leak observed", sub: "vulnerable design on the board (webcam)" };
  else if (Object.values(vs).some((v) => (v.programs ?? []).some((p) => p.ok))) phys = { tone: "info", value: "Programmed", sub: "not camera-verified yet" };
  else if (Object.values(vs).some((v) => v.board_sim)) phys = { tone: "idle", value: "Simulated only", sub: "board-level simulation, no hardware run" };
  else phys = { tone: "idle", value: "Not on hardware", sub: audit.record.case.board ? "build a bitstream in the FPGA Lab" : "this case has no board demo" };

  return (
    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      <Tile icon={<IconShieldCheck size={22} />} tone={!report ? "idle" : verified ? (c.allPass === c.allTotal ? "ok" : "bad") : "bad"}
            label={verified ? "Trusted tests (patched)" : "Trusted tests (original)"}
            value={report ? (verified ? `${c.allPass} / ${c.allTotal}` : `${c.secFail} / ${c.secTotal} fail`) : "not run"}
            sub={report ? (verified ? `security ${c.secPass}/${c.secTotal} · functional ${c.funPass}/${c.funTotal}` : `functional ${c.funPass}/${c.funTotal} still pass`) : "the verifier has not run"}
            foot={report && (
              <span className="flex gap-[3px]" aria-label="test results">
                {report.tests.map((t) => <span key={t.id} title={`${t.id}: ${t.status}`} className={cx("h-1.5 flex-1 rounded-full", t.status === "pass" ? "bg-ok" : t.status === "fail" ? "bg-bad" : "bg-line")} />)}
              </span>
            )} />
      <Tile icon={<IconFingerprint size={22} />} tone={formal ? (formal.status === "proven" ? "ok" : formal.status === "counterexample" ? "bad" : "warn") : "idle"}
            label={verified ? "Formal (patched)" : "Formal (original)"} value={formal ? fst.label : "not run"}
            sub={formal ? `${formal.properties.length} propert${formal.properties.length === 1 ? "y" : "ies"} · ${formal.engine}` : "no property check yet"}
            foot={formal ? formal.message : undefined} />
      <Tile icon={<IconEye size={22} />} tone={phys.tone} label="Physical result (Basys 3)" value={phys.value} sub={phys.sub}
            foot={obsAfter || obsBefore ? `${(obsAfter ?? obsBefore)!.frames_recorded} frames analysed` : "only webcam observations count as physical evidence"} />
      <Tile icon={<IconChip size={22} />} tone={sha ? (onBoard ? "ok" : "info") : "idle"} label={`Bitstream${sha ? ` · ${variantName(shaVariant)}` : ""}`}
            value={sha ? <span className="font-mono text-lg">{sha.slice(0, 10)}…</span> : "not built"}
            sub={sha ? "SHA-256" : "no Vivado build yet"}
            foot={sha && (
              <span className="flex items-center justify-between gap-2">
                <span>{onBoard ? (confirmed === false ? "last programmed, provenance unconfirmed" : "on the board now (last program by SiliconBreak X)") : "built; not the design on the board"}</span>
                <CopyButton text={sha} />
              </span>
            )} />
    </div>
  );
}

// ------------------------------------------------------------------ vulnerability + analysis

export function SourceExcerpt({ f, text, context = 3 }: { f: Finding; text: string | undefined; context?: number }) {
  const lines = useMemo(() => (text ? highlightLines(text, "verilog") : null), [text]);
  const lo = Math.max(1, f.line_start - context);
  const hi = Math.min(lines?.length ?? f.line_end, f.line_end + context);
  return (
    <div className="overflow-x-auto rounded-lg border border-line bg-sunken py-2 font-mono text-[11.5px] leading-[1.6]">
      {lines ? lines.slice(lo - 1, hi).map((html, i) => {
        const n = lo + i;
        const on = n >= f.line_start && n <= f.line_end;
        return (
          <div key={n} className={cx("flex", on && "bg-bad/15")}>
            <span className={cx("w-10 shrink-0 select-none pr-3 text-right", on ? "text-bad" : "text-faint")}>{n}</span>
            <span className="hljs whitespace-pre pr-3" dangerouslySetInnerHTML={{ __html: html || "&nbsp;" }} />
          </div>
        );
      }) : <div className="px-3 text-2xs text-faint">Loading source…</div>}
    </div>
  );
}

export function VulnerabilityCard({ audit }: { audit: SecurityAudit }) {
  const f = audit.record.findings[0];
  const src = useResource(f ? audit.id : null, 0, (sig) => securityApi.sources(audit.id, undefined, sig));
  const c = audit.record.case;
  if (!f) {
    return (
      <section className="rounded-xl border border-line bg-panel p-4 shadow-panel">
        <h3 className="text-sm font-semibold">Vulnerability</h3>
        <p className="mt-2 text-xs text-muted">{audit.record.investigation ? "The investigator reported no suspected contract violation." : "Investigation has not run yet."}</p>
      </section>
    );
  }
  const st = findingStatus(f.status);
  return (
    <section className="rounded-xl border border-line bg-panel shadow-panel">
      <header className="flex items-center gap-2.5 border-b border-line px-4 py-2.5">
        <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-bad/10 text-bad"><IconAlert size={16} /></span>
        <h3 className="text-sm font-semibold">Vulnerability details</h3>
        <Pill tone={st.tone} className="ml-auto">{st.label}</Pill>
      </header>
      <div className="space-y-3 p-4">
        <div className="flex items-start gap-2 rounded-lg border border-bad/30 bg-bad/[0.07] px-3 py-2.5">
          <IconAlert size={16} className="mt-0.5 shrink-0 text-bad" />
          <div className="min-w-0 flex-1">
            <div className="text-sm font-semibold text-fg">{f.title}</div>
            <div className="text-xs text-muted">{c.security_contract.summary}</div>
          </div>
          <Pill tone={f.confidence === "high" ? "bad" : f.confidence === "medium" ? "warn" : "idle"} title="investigator's confidence">{f.confidence} confidence</Pill>
        </div>
        <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1.5 rounded-lg border border-line px-3 py-2.5 text-xs">
          <dt className="text-faint">CWE</dt><dd>{c.cwe.join(", ")}</dd>
          <dt className="text-faint">Weakness</dt><dd>{f.weakness_class}</dd>
          <dt className="text-faint">Violates</dt><dd className="font-mono text-[11px]">{f.violated_properties.join(", ")}</dd>
          <dt className="text-faint">Location</dt>
          <dd>{f.permalink ? <a href={f.permalink} target="_blank" rel="noreferrer" className="font-mono text-[11px] text-accent hover:underline">{f.file}:{f.line_start}-{f.line_end}</a>
            : <span className="font-mono text-[11px]">{f.file}:{f.line_start}-{f.line_end}</span>}</dd>
          <dt className="text-faint">Found by</dt><dd>{f.is_ai ? providerLabel(f.provider, f.model) : `${f.provider}${f.rule ? ` rule ${f.rule}` : ""} (not AI)`}</dd>
          {f.severity_reasoning && <><dt className="text-faint">Impact</dt><dd className="text-muted">{f.severity_reasoning}</dd></>}
        </dl>
        <SourceExcerpt f={f} text={src.data?.original?.[f.file]} />
      </div>
    </section>
  );
}

type AnalysisTab = "findings" | "explanation" | "waveform" | "patch";

export function AnalysisCard({ audit }: { audit: SecurityAudit }) {
  const r = audit.record;
  const repair = keyRepair(audit);
  const [tab, setTab] = useState<AnalysisTab>("findings");
  const [mode, setMode] = useState<DiffMode>("unified");
  const inv = r.investigation;
  const f = r.findings[0];
  const failing = r.reproduction?.tests.find((t) => t.kind === "security" && t.status === "fail" && t.span_ns);
  const tabs: Array<{ v: AnalysisTab; l: string; show: boolean }> = [
    { v: "findings", l: `Findings (${r.findings.length})`, show: true },
    { v: "explanation", l: "Explanation", show: !!f },
    { v: "waveform", l: "Waveform evidence", show: !!(failing && r.reproduction?.vcd) },
    { v: "patch", l: "Patch", show: !!repair?.diff },
  ];
  return (
    <section className="flex min-w-0 flex-col rounded-xl border border-line bg-panel shadow-panel">
      <header className="flex flex-wrap items-center gap-2.5 border-b border-line px-4 py-2.5">
        <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-accent/10 text-accent"><IconSearch size={16} /></span>
        <h3 className="text-sm font-semibold">AI analysis</h3>
        <span className="text-xs text-accent">{inv ? providerLabel(inv.provider, inv.model) : "not run"}</span>
      </header>
      <div className="flex gap-1 border-b border-line px-3 pt-1">
        {tabs.filter((t) => t.show).map((t) => (
          <button key={t.v} type="button" onClick={() => setTab(t.v)}
                  className={cx("-mb-px border-b-2 px-2.5 py-2 text-xs font-medium transition-colors", tab === t.v ? "border-accent text-fg" : "border-transparent text-muted hover:text-fg")}>
            {t.l}
          </button>
        ))}
      </div>
      <div className="min-h-0 flex-1 p-4">
        {tab === "findings" && (
          <div className="space-y-2">
            {inv?.summary && <p className="text-xs text-muted">{inv.summary}</p>}
            {r.findings.length === 0 && <p className="text-xs text-muted">{inv ? "No suspected contract violation was reported." : "Investigation has not run yet."}</p>}
            {r.findings.map((x) => (
              <div key={x.finding_id} className="rounded-lg border border-line px-3 py-2">
                <div className="flex flex-wrap items-center gap-2">
                  <Pill tone={findingStatus(x.status).tone}>{findingStatus(x.status).label}</Pill>
                  <span className="min-w-0 flex-1 text-xs font-semibold">{x.title}</span>
                  <span className="font-mono text-[11px] text-faint">{x.file.split("/").pop()}:{x.line_start}</span>
                </div>
                <p className="mt-1 text-[11.5px] text-muted">{x.hypothesis}</p>
              </div>
            ))}
            {inv && inv.dropped.length > 0 && <p className="text-[11px] text-faint">{inv.dropped.length} unverifiable model claim(s) were dropped by the validator.</p>}
            {r.unexplained_failures.length > 0 && <p className="text-[11px] text-orange">{r.unexplained_failures.length} verifier failure(s) no finding explains (missed by the investigator).</p>}
          </div>
        )}
        {tab === "explanation" && f && (
          <div className="space-y-2.5 text-xs">
            <div><div className="mb-0.5 font-semibold text-fg">Hypothesis</div><p className="text-muted">{f.hypothesis}</p></div>
            <div><div className="mb-0.5 font-semibold text-fg">How the verifier checks it</div><p className="text-muted">{f.proposed_test}</p></div>
            {f.limitations && <div><div className="mb-0.5 font-semibold text-fg">Limitations</div><p className="text-muted">{f.limitations}</p></div>}
            <div>
              <div className="mb-1 font-semibold text-fg">Status history</div>
              <ol className="space-y-1.5 border-l-2 border-line pl-3">
                {f.history.map((h, i) => (
                  <li key={i} className="text-[11.5px] text-muted"><Pill tone={findingStatus(h.status).tone} className="mr-1.5">{findingStatus(h.status).label}</Pill>{h.why}</li>
                ))}
              </ol>
            </div>
          </div>
        )}
        {tab === "waveform" && failing && r.reproduction?.vcd && r.reproduction.dir && (
          <div className="space-y-1.5">
            <p className="text-[11px] text-faint">Trusted test <span className="font-mono">{failing.id}</span> on the original design ({failing.title}); from the verifier's VCD.</p>
            <SecurityWave auditId={audit.id} vcdPath={`${r.reproduction.dir}/${r.reproduction.vcd}`}
                          span={[failing.span_ns![0], Math.min(failing.span_ns![1] + 30, failing.span_ns![0] + 400)]}
                          signals={r.case.interface.filter((p) => p.port !== "clk").map((p) => `tb.${p.port}`)}
                          highlight={["read_grant", "read_data", "access_denied"]} />
          </div>
        )}
        {tab === "patch" && repair?.diff && (
          <div className="overflow-hidden rounded-lg border border-line">
            <div className="flex items-center justify-between border-b border-line bg-sunken/60 px-2.5 py-1.5">
              <span className="text-[11px] text-faint">{repair.changed_files.join(", ")} · attempt {repair.attempt} · {repair.label}</span>
              <span className="flex items-center gap-2">
                <CopyButton text={repair.diff} label="copy patch" />
                <Segmented value={mode} onChange={setMode} options={[{ value: "unified", label: "Unified" }, { value: "split", label: "Split" }]} />
              </span>
            </div>
            <DiffView diff={repair.diff} mode={mode} lang="verilog" />
          </div>
        )}
      </div>
    </section>
  );
}

function CheckRow({ ok, label, value }: { ok: boolean | null; label: string; value: ReactNode }) {
  return (
    <li className="flex items-start gap-2.5 py-1.5">
      {ok === true ? <IconCheckCircle size={17} className="mt-px shrink-0 text-ok" /> : ok === false ? <IconXCircle size={17} className="mt-px shrink-0 text-bad" />
        : <span className="mt-[3px] h-[15px] w-[15px] shrink-0 rounded-full border-2 border-dashed border-line-strong" />}
      <span className="min-w-0">
        <span className="block text-xs font-medium text-fg">{label}</span>
        <span className={cx("block text-[11px]", ok === true ? "text-ok" : ok === false ? "text-bad" : "text-faint")}>{value}</span>
      </span>
    </li>
  );
}

/** Verifier results for the key repair, plus the hardware rungs, as a checklist. */
export function VerifierChecklist({ audit }: { audit: SecurityAudit }) {
  const repair = keyRepair(audit);
  const v = repair?.verification;
  const c = counts(v);
  const vs = fpgaVariants(audit);
  const key = repair ? variantOf(repair) : null;
  const variant = key ? vs[key] : undefined;
  const obs = latestObservation(variant);
  return (
    <section className="rounded-xl border border-line bg-panel shadow-panel">
      <header className="flex items-center gap-2.5 border-b border-line px-4 py-2.5">
        <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-ok/10 text-ok"><IconShieldCheck size={16} /></span>
        <h3 className="text-sm font-semibold">Verifier results</h3>
        {repair && <span className="ml-auto text-[11px] text-faint">attempt {repair.attempt} · {repair.label}</span>}
      </header>
      <ul className="divide-y divide-line px-4 py-1">
        <CheckRow ok={v ? v.security_status === "passed" : null} label="Security tests" value={v ? `${c.secPass} / ${c.secTotal} passed` : "no patch to retest"} />
        <CheckRow ok={repair?.formal ? repair.formal.status === "proven" : null} label="Formal verification"
                  value={repair?.formal ? `${formalStatus(repair.formal.status).label} (${repair.formal.properties.length} properties)` : "not run on a patch"} />
        <CheckRow ok={v ? v.functional_status === "passed" : null} label="Functional regression" value={v ? `${c.funPass} / ${c.funTotal} passed` : "not run"} />
        <CheckRow ok={variant?.board_sim ? variant.board_sim.status === "matches_contract" : null} label="Board-level simulation"
                  value={variant?.board_sim ? variant.board_sim.message : "not simulated on the Basys 3 top"} />
        <CheckRow ok={variant?.build ? variant.build.ok : null} label="Vivado bitstream"
                  value={variant?.build ? (variant.build.ok ? `built · WNS ${variant.build.timing?.wns_ns ?? "?"} ns` : `${variant.build.status}: ${variant.build.message}`) : "not built"} />
        <CheckRow ok={obs ? obs.verdict === "contract_met" : null} label="Physical test (webcam)"
                  value={obs ? `${obs.summary}` : "not observed on the board yet"} />
      </ul>
    </section>
  );
}

/** Repair summary for the key attempt: agent, files, diffstat, session, permissions. */
export function RepairSummary({ audit }: { audit: SecurityAudit }) {
  const repair = keyRepair(audit);
  const [mode, setMode] = useState<DiffMode>("unified");
  return (
    <section className="flex min-w-0 flex-col rounded-xl border border-line bg-panel shadow-panel">
      <header className="flex flex-wrap items-center gap-2.5 border-b border-line px-4 py-2.5">
        <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-info/10 text-info"><IconWrench size={16} /></span>
        <h3 className="text-sm font-semibold">Repair details</h3>
        {repair && <span className="text-xs text-info">{repair.agent === "factory" ? "Factory Droid" : repair.agent === "reference" ? "Reference fix" : providerLabel(repair.provider ?? "model", repair.model)}</span>}
        {repair && <Pill tone={repair.verdict === "verified" ? "ok" : "bad"} className="ml-auto">{repair.verdict === "verified" ? "verifier accepted" : repair.verdict}</Pill>}
      </header>
      {!repair ? (
        <p className="p-4 text-xs text-muted">{audit.case_id ? "No repair attempt yet. Start one from the Security Lab." : "Repairs need a case with trusted tests."}</p>
      ) : (
        <div className="space-y-3 p-4">
          {repair.agent === "reference" && (
            <p className="rounded-lg border border-violet/40 bg-violet/5 px-3 py-2 text-xs text-violet">Human-written reference fix: shows the harness accepts a correct repair. Not an agent repair.</p>
          )}
          <div className="flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-muted">
            <span>status {repair.status}</span>
            {repair.session_id && <span>session <span className="font-mono">{repair.session_id.slice(0, 13)}…</span></span>}
            {repair.duration_s != null && <span>{fmtDuration(repair.duration_s)}</span>}
            {(repair.allowed_actions?.length ?? 0) > 0 && <span className="text-ok">{repair.allowed_actions!.length} action(s) allowed</span>}
            {(repair.denied_actions?.length ?? 0) > 0 && <span className="text-warn">{repair.denied_actions!.length} refused</span>}
          </div>
          {repair.final_text && <p className="line-clamp-4 whitespace-pre-line text-xs text-fg">{repair.final_text}</p>}
          {repair.diff && (
            <div className="overflow-hidden rounded-lg border border-line">
              <div className="flex items-center justify-between border-b border-line bg-sunken/60 px-2.5 py-1.5">
                <span className="text-[11px] text-faint">patch.diff</span>
                <Segmented value={mode} onChange={setMode} options={[{ value: "unified", label: "Unified" }, { value: "split", label: "Split" }]} />
              </div>
              <DiffView diff={repair.diff} mode={mode} lang="verilog" />
            </div>
          )}
        </div>
      )}
    </section>
  );
}
