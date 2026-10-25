/**
 * Dashboard: the first screen. The featured audit (the open one, else the most
 * recent verified repair, else the latest audit) with its pipeline, evidence
 * level, headline numbers, vulnerability, AI analysis, repair and verifier
 * results; an FPGA Lab rail with the recorded webcam evidence, the board twin
 * and the test guide; and the latest benchmark, environment and credits.
 *
 * Nothing here is sample data: with no audits the page says so and offers to
 * start one.
 */

import { useEffect, useMemo, useState } from "react";
import { securityApi, type BenchRun, type DemoStep, type SecurityAudit } from "../api/security";
import {
  IconArrowRight, IconBars, IconCamera, IconChip, IconCode, IconCube, IconSearch, IconShield, IconSparkles,
} from "../components/icons";
import { Button, Empty, Pill, Segmented, Skeleton, cx } from "../components/ui";
import { fmtAgo, fmtDateTime } from "../lib/format";
import { hardwareState, runStatus } from "../lib/status";
import { useNow, useResource } from "../lib/useResource";
import { useAppState, useStore } from "../store/store";
import { DemoBoard, LedRow, SOURCE_LABEL, howTo, observedStep, stepLeds, type LedSource } from "../fpga/demo";
import { auditHeadlineTone, fpgaVariants, keyRepair, latestObservation, latestProgram, variantName, variantOf } from "./derive";
import { AuditHero } from "./lab/hero";
import { AnalysisCard, EvidenceLadder, RepairSummary, StageCards, StatTiles, VerifierChecklist, VulnerabilityCard } from "./lab/parts";
import { SecurityStart } from "./SecurityPanel";

function pickFeatured(audits: SecurityAudit[], selected: string | null): SecurityAudit | null {
  if (selected) {
    const a = audits.find((x) => x.id === selected);
    if (a) return a;
  }
  const sorted = [...audits].sort((a, b) => b.created_at - a.created_at);
  return sorted.find((a) => a.status === "repaired_verified") ?? sorted[0] ?? null;
}

export function Dashboard() {
  const store = useStore();
  const all = useAppState((s) => s.security);
  const selected = useAppState((s) => s.selection.auditId);
  const approvals = useAppState((s) => s.approvals);
  const audits = useMemo(() => Object.values(all), [all]);
  const audit = pickFeatured(audits, selected);
  const fpgaVersion = audit ? JSON.stringify(audit.record.fpga ?? {}) + audit.record.repairs.length : "";
  const fpga = useResource(audit?.case_id && audit.record.case.board ? audit.id : null, fpgaVersion, (sig) => securityApi.fpga(audit!.id, sig));
  const hold = latestProgram(approvals);
  const [loaded, setLoaded] = useState(audits.length > 0);
  const wide = useMinWidth(1536);
  useEffect(() => { void store.loadAudits().catch(() => undefined).finally(() => setLoaded(true)); }, [store]);

  if (!audit && !loaded) {
    return (
      <div className="space-y-4" aria-busy="true">
        <Skeleton className="h-36 rounded-2xl" />
        <div className="grid gap-3 lg:grid-cols-5">{[0, 1, 2, 3, 4].map((i) => <Skeleton key={i} className="h-40 rounded-xl" />)}</div>
        <div className="grid gap-3 lg:grid-cols-4">{[0, 1, 2, 3].map((i) => <Skeleton key={i} className="h-28 rounded-xl" />)}</div>
      </div>
    );
  }
  if (!audit) return <Welcome />;
  return (
    <div className={cx("grid gap-4", wide && "grid-cols-[minmax(0,1fr)_400px]")}>
      <div className="min-w-0 space-y-4">
        <AuditHero audit={audit} eyebrow={selected === audit.id ? "Selected audit" : "Featured audit"}
                   right={<Button size="sm" variant="primary" onClick={() => store.selectAudit(audit.id)}>Open in Security Lab <IconArrowRight size={14} /></Button>} />
        <StageCards audit={audit} />
        <StatTiles audit={audit} fpga={fpga.data} hold={hold} />
        <EvidenceLadder audit={audit} />
        <div className="grid gap-4 xl:grid-cols-2">
          <VulnerabilityCard audit={audit} />
          <AnalysisCard audit={audit} />
        </div>
        <div className="grid gap-4 xl:grid-cols-[1.35fr_1fr]">
          <RepairSummary audit={audit} />
          <VerifierChecklist audit={audit} />
        </div>
        {!wide && <FpgaRail audit={audit} steps={fpga.data?.demo ?? []} layout="wide" />}
        <BottomRow audits={audits} />
      </div>
      {wide && (
        <div className="sticky top-0 min-w-0 space-y-4 self-start">
          <FpgaRail audit={audit} steps={fpga.data?.demo ?? []} layout="rail" />
        </div>
      )}
    </div>
  );
}

function useMinWidth(px: number): boolean {
  const [ok, setOk] = useState(() => window.innerWidth >= px);
  useEffect(() => {
    const on = () => setOk(window.innerWidth >= px);
    window.addEventListener("resize", on);
    return () => window.removeEventListener("resize", on);
  }, [px]);
  return ok;
}

// ------------------------------------------------------------------ FPGA rail

type RailTab = "camera" | "board" | "guide";

function FpgaRail({ audit, steps, layout }: { audit: SecurityAudit; steps: DemoStep[]; layout: "rail" | "wide" }) {
  const store = useStore();
  const hw = useAppState((s) => s.hardware);
  const approvals = useAppState((s) => s.approvals);
  const hwInfo = hardwareState(hw?.state);
  const vs = fpgaVariants(audit);
  const repair = keyRepair(audit);
  const afterKey = repair ? variantOf(repair) : null;
  const obsAfter = afterKey ? latestObservation(vs[afterKey]) : null;
  const obsBefore = latestObservation(vs.vulnerable);
  const obs = obsAfter ?? obsBefore;
  const [tab, setTab] = useState<RailTab>(obs ? "camera" : "guide");
  const [stepId, setStepId] = useState<string | null>(null);
  const [variant, setVariant] = useState<string>(afterKey && vs[afterKey] ? afterKey : "vulnerable");
  const [source, setSource] = useState<LedSource>(obs ? "observed" : "simulated");
  const [view, setView] = useState<"2d" | "3d">("2d");
  const hold = latestProgram(approvals);
  const step = steps.find((s) => s.id === stepId) ?? steps[0] ?? null;
  const vState = vs[variant];
  const leds = step ? stepLeds(step, source, vState) : null;

  if (!audit.record.case.board) {
    return (
      <section className="rounded-xl border border-line bg-panel p-4 shadow-panel">
        <h3 className="text-sm font-semibold">FPGA Lab</h3>
        <p className="mt-1 text-xs text-muted">This audit's case has no Basys 3 demo. The FPGA Lab still drives your Workspace designs.</p>
        <Button size="sm" variant="secondary" className="mt-3" onClick={() => store.setTab("fpga")}>Open FPGA Lab</Button>
      </section>
    );
  }
  const holdLabel = hold ? (hold.auditId ? `${variantName(hold.variant ?? "?")} of ${hold.auditId === audit.id ? "this audit" : hold.auditId}` : hold.revisionId ? `design revision ${hold.revisionId}` : "unknown design") : null;

  const camera = obs ? (
    <CameraEvidence audit={audit} obs={obs} stepId={step?.id ?? null} onStep={setStepId} steps={steps} />
  ) : (
    <Empty title="No webcam evidence yet" icon={<IconCamera size={20} />}>
      Program a variant from the FPGA Lab, then record the demo steps with the webcam. Only those recordings count as physical results.
    </Empty>
  );
  const boardPanel = (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <Segmented value={variant} onChange={setVariant} options={Object.keys(vs).map((k) => ({ value: k, label: variantName(k) }))} />
        <Segmented value={view} onChange={setView} options={[{ value: "2d", label: "2D" }, { value: "3d", label: "3D" }]} />
      </div>
      <Segmented value={source} onChange={setSource} options={[
        { value: "expected", label: "Contract" }, { value: "simulated", label: "Simulated" }, { value: "observed", label: "Observed" }]} />
      <div className="rounded-lg border border-line bg-sunken/50 p-2"><DemoBoard step={step} leds={leds} source={source} view={view} height="h-64" /></div>
      <p className="text-[11px] text-faint">LEDs: {SOURCE_LABEL[source]}{leds ? "" : " · no data for this step"}. {step ? howTo(step) : ""}</p>
      <StepChips steps={steps} active={step?.id ?? null} onPick={setStepId} />
    </div>
  );
  const guide = (
    <ol className="space-y-2">
      {steps.map((s) => {
        const o = observedStep(obsAfter ?? obsBefore, s.id);
        return (
          <li key={s.id} className={cx("cursor-pointer rounded-lg border px-3 py-2 transition-colors", step?.id === s.id ? "border-accent/50 bg-accent/5" : "border-line hover:border-line-strong")}
              onClick={() => setStepId(s.id)}>
            <div className="flex items-center gap-2">
              <span className="flex h-6 w-6 items-center justify-center rounded-full bg-raised font-mono text-[11px] font-semibold">{s.id}</span>
              <span className="min-w-0 flex-1 text-xs font-semibold">{s.title}</span>
              {o && o.status === "observed" && <Pill tone={o.matches_contract ? "ok" : "bad"}>{o.matches_contract ? "meets" : "violates"}</Pill>}
            </div>
            <p className="mt-1 text-[11px] text-muted">{howTo(s)}</p>
            <div className="mt-1.5 grid grid-cols-[auto_1fr] items-center gap-x-3 gap-y-1 text-[10.5px] text-faint">
              <span>contract</span><LedRow hex={s.expect_leds} />
              {o && <><span>webcam ({variantName(obsAfter ? afterKey! : "vulnerable")})</span><LedRow hex={o.observed} /></>}
            </div>
          </li>
        );
      })}
    </ol>
  );
  const footer = (
    <div className="rounded-lg border border-line bg-sunken/50 px-3 py-2 text-[11px]">
      <div className="text-faint">On the board now</div>
      <div className="font-medium text-fg">{holdLabel ?? "nothing programmed by SiliconBreak X"}</div>
      {hold && <div className="text-faint">programmed {fmtDateTime(hold.at)} · sha {hold.sha?.slice(0, 10)}… (power-cycling reverts the board)</div>}
    </div>
  );
  const header = (
    <header className="flex items-center gap-2.5 border-b border-line px-4 py-3">
      <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-accent/10 text-accent"><IconChip size={17} /></span>
      <h3 className="text-[15px] font-semibold">FPGA Lab</h3>
      <Pill tone={hwInfo.tone} className="ml-auto" title={hw?.message ?? ""}>{hw?.state === "connected" ? "Connected" : hw ? hwInfo.label : "checking…"}</Pill>
    </header>
  );

  if (layout === "wide") {
    return (
      <section className="overflow-hidden rounded-xl border border-line bg-panel shadow-panel">
        {header}
        <div className="grid gap-4 p-4 lg:grid-cols-3">
          <div className="min-w-0"><div className="mb-2 flex items-center gap-1.5 text-xs font-semibold"><IconCamera size={14} className="text-accent" />Camera evidence</div>{camera}</div>
          <div className="min-w-0"><div className="mb-2 flex items-center gap-1.5 text-xs font-semibold"><IconCube size={14} className="text-accent" />Board twin</div>{boardPanel}</div>
          <div className="min-w-0"><div className="mb-2 flex items-center gap-1.5 text-xs font-semibold"><IconSearch size={14} className="text-accent" />Test guide</div>{guide}</div>
        </div>
        <div className="flex flex-wrap items-center gap-3 border-t border-line px-4 py-3">
          <div className="min-w-0 flex-1">{footer}</div>
          <Button variant="primary" onClick={() => store.selectAudit(audit.id, "fpga")}><IconChip size={15} /> Open FPGA Lab</Button>
        </div>
      </section>
    );
  }
  return (
    <section className="overflow-hidden rounded-xl border border-line bg-panel shadow-panel">
      {header}
      <div className="grid grid-cols-3 gap-1 border-b border-line bg-sunken/40 p-1.5">
        {([["camera", "Camera evidence", <IconCamera key="c" size={14} />], ["board", "Board", <IconCube key="b" size={14} />], ["guide", "Test guide", <IconSearch key="g" size={14} />]] as const).map(([k, l, ic]) => (
          <button key={k} type="button" onClick={() => setTab(k)}
                  className={cx("flex items-center justify-center gap-1.5 rounded-lg py-2 text-xs font-semibold transition-all",
                    tab === k ? "sbx-btn-primary text-white" : "text-muted hover:bg-raised hover:text-fg")}>
            {ic}{l}
          </button>
        ))}
      </div>
      <div className="space-y-3 p-4">
        {tab === "camera" && camera}
        {tab === "board" && boardPanel}
        {tab === "guide" && guide}
        {footer}
        <Button variant="primary" className="w-full" onClick={() => store.selectAudit(audit.id, "fpga")}>
          <IconChip size={15} /> Open FPGA Lab
        </Button>
      </div>
    </section>
  );
}

function StepChips({ steps, active, onPick }: { steps: DemoStep[]; active: string | null; onPick: (id: string) => void }) {
  return (
    <div className="flex flex-wrap gap-1.5">
      {steps.map((s) => (
        <button key={s.id} type="button" onClick={() => onPick(s.id)} title={s.title}
                className={cx("rounded-lg px-2.5 py-1 font-mono text-[11px] font-semibold ring-1 ring-inset transition-colors",
                  active === s.id ? "bg-accent text-white ring-accent" : "bg-raised text-muted ring-line hover:text-fg")}>
          {s.id}
        </button>
      ))}
    </div>
  );
}

function CameraEvidence({ audit, obs, stepId, onStep, steps }: {
  audit: SecurityAudit; obs: NonNullable<ReturnType<typeof latestObservation>>; stepId: string | null; onStep: (id: string) => void; steps: DemoStep[];
}) {
  const ids = obs.steps.map((s) => s.step_id);
  const cur = observedStep(obs, stepId && ids.includes(stepId) ? stepId : ids.find((i) => i !== "DARK") ?? ids[0]);
  const verdictTone = obs.verdict === "contract_met" ? "ok" : obs.verdict === "violation_observed" ? "bad" : obs.verdict === "unexpected" ? "warn" : "orange";
  const verdictLabel = { contract_met: "contract met", violation_observed: "violation observed", unexpected: "board ≠ simulation", inconclusive: "inconclusive" }[obs.verdict];
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <Pill tone={verdictTone}>{verdictLabel}</Pill>
        <span className="text-xs text-muted">{variantName(obs.variant)} · {obs.at ? fmtDateTime(obs.at) : ""}</span>
      </div>
      {cur?.snapshot && obs.dir ? (
        <div className="relative overflow-hidden rounded-lg border border-line bg-black">
          <img src={securityApi.cameraImageUrl(audit.id, `${obs.dir}/${cur.snapshot}`)} alt={`webcam frame for ${cur.step_id}`} className="block w-full" />
          <span className="absolute left-2 top-2 rounded-md bg-black/65 px-1.5 py-0.5 text-[10px] font-semibold text-white">RECORDED · {cur.step_id}</span>
        </div>
      ) : <p className="text-xs text-muted">No snapshot stored for this step.</p>}
      <StepChips steps={steps.filter((s) => ids.includes(s.id))} active={cur?.step_id ?? null} onPick={onStep} />
      {cur && (
        <div className="grid grid-cols-3 gap-2 text-center">
          <div className="rounded-lg border border-line p-2">
            <div className="text-[10px] text-faint">Contract</div>
            <div className="mt-1 flex justify-center"><LedRow hex={cur.expected} size="sm" /></div>
            <div className="mt-1 font-mono text-[11px]">{cur.expected}</div>
          </div>
          <div className="rounded-lg border border-line p-2">
            <div className="text-[10px] text-faint">Observed (camera)</div>
            <div className="mt-1 flex justify-center"><LedRow hex={cur.observed} size="sm" /></div>
            <div className={cx("mt-1 font-mono text-[11px]", cur.matches_contract === false && "text-bad")}>{cur.observed ?? "?"}</div>
          </div>
          <div className={cx("flex flex-col items-center justify-center rounded-lg border p-2",
            cur.status !== "observed" ? "border-orange/40" : cur.matches_contract ? "border-ok/40 bg-ok/5" : "border-bad/40 bg-bad/5")}>
            <div className="text-[10px] text-faint">Status</div>
            <div className={cx("mt-1 text-sm font-semibold", cur.status !== "observed" ? "text-orange" : cur.matches_contract ? "text-ok" : "text-bad")}>
              {cur.status !== "observed" ? "?" : cur.matches_contract ? "PASS" : "FAIL"}
            </div>
          </div>
        </div>
      )}
      <p className="text-[11px] text-faint">{obs.summary}</p>
    </div>
  );
}

// ------------------------------------------------------------------ bottom row

function BottomRow({ audits }: { audits: SecurityAudit[] }) {
  const store = useStore();
  const now = useNow(30_000);
  const env = useAppState((s) => s.environment);
  const bench = useResource<BenchRun[]>("bench-runs", 0, () => securityApi.benchRuns());
  const latestBench = (bench.data ?? []).sort((a, b) => b.created_at - a.created_at)[0] ?? null;
  const recent = [...audits].sort((a, b) => b.created_at - a.created_at).slice(0, 5);
  const tools = env ? [
    { l: "Docker", ok: env.vivado.docker_daemon },
    { l: "Vivado", ok: env.vivado.ready },
    { l: "Verilator", ok: env.verilator.available },
    { l: "openFPGALoader", ok: env.openfpgaloader.available },
    { l: env.llm.configured ? `AI: ${env.llm.model}` : "AI: offline rules", ok: env.llm.configured },
  ] : [];
  return (
    <div className="grid gap-4 lg:grid-cols-3">
      <section className="rounded-xl border border-line bg-panel shadow-panel">
        <header className="flex items-center gap-2 border-b border-line px-4 py-2.5">
          <IconShield size={15} className="text-accent" /><h3 className="text-sm font-semibold">Recent audits</h3>
          <button type="button" className="ml-auto text-[11px] text-accent hover:underline" onClick={() => store.selectAudit(null)}>all</button>
        </header>
        <ul className="divide-y divide-line">
          {recent.map((a) => (
            <li key={a.id}>
              <button type="button" onClick={() => store.selectAudit(a.id, "dashboard")} className="flex w-full items-center gap-2 px-4 py-2 text-left hover:bg-raised/60">
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-xs font-medium">{a.record.case?.title ?? a.record.source?.label ?? a.id}</span>
                  <span className="block text-[11px] text-faint">{fmtAgo(a.created_at, now)} · {a.record.findings.length} finding(s)</span>
                </span>
                <Pill tone={auditHeadlineTone(a)}>{runStatus(a.status).label}</Pill>
              </button>
            </li>
          ))}
        </ul>
      </section>
      <section className="rounded-xl border border-line bg-panel shadow-panel">
        <header className="flex items-center gap-2 border-b border-line px-4 py-2.5">
          <IconBars size={15} className="text-accent" /><h3 className="text-sm font-semibold">SiliconBench</h3>
          <button type="button" className="ml-auto text-[11px] text-accent hover:underline" onClick={() => store.setTab("bench")}>open</button>
        </header>
        <div className="p-4">
          {!latestBench ? (
            <p className="text-xs text-muted">No benchmark run yet. SiliconBench runs every case through the same verifier; the free arms cost nothing.</p>
          ) : (
            <div className="space-y-2">
              <div className="text-[11px] text-faint">{fmtDateTime(latestBench.created_at)} · {latestBench.status} · {latestBench.config.case_ids.length} case(s)</div>
              {Object.entries(latestBench.result?.metrics ?? {}).slice(0, 4).map(([k, m]) => (
                <div key={k}>
                  <div className="flex justify-between text-[11px]"><span className="text-muted">{m.label}</span>
                    <span className="tabular-nums">{m.denominator ? `${m.numerator}/${m.denominator}` : "not run"}</span></div>
                  <div className="mt-0.5 h-1.5 overflow-hidden rounded-full bg-line/70">
                    <div className="h-full rounded-full sbx-brand-bg" style={{ width: `${m.denominator ? (m.numerator / m.denominator) * 100 : 0}%` }} />
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </section>
      <section className="rounded-xl border border-line bg-panel shadow-panel">
        <header className="flex items-center gap-2 border-b border-line px-4 py-2.5">
          <IconCode size={15} className="text-accent" /><h3 className="text-sm font-semibold">Engineering environment</h3>
          <button type="button" className="ml-auto text-[11px] text-accent hover:underline" onClick={() => store.setTab("pipeline")}>workspace</button>
        </header>
        <ul className="grid grid-cols-1 gap-1.5 p-4">
          {tools.length === 0 && <li className="text-xs text-muted">Checking the environment…</li>}
          {tools.map((t) => (
            <li key={t.l} className="flex items-center gap-2 text-xs">
              <span className={cx("h-2 w-2 rounded-full", t.ok ? "bg-ok shadow-[0_0_8px_rgb(var(--ok)/0.8)]" : "bg-warn")} />
              <span className="min-w-0 truncate">{t.l}</span>
              <span className="ml-auto text-[11px] text-faint">{t.ok ? "ready" : "not ready"}</span>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}

// ------------------------------------------------------------------ first run

function Welcome() {
  const store = useStore();
  return (
    <div className="space-y-6">
      <section className="sbx-hero rounded-2xl border border-line p-8 shadow-panel">
        <div className="max-w-3xl">
          <div className="inline-flex items-center gap-1.5 rounded-full bg-accent/10 px-2.5 py-1 text-[11px] font-semibold text-accent ring-1 ring-inset ring-accent/25">
            <IconSparkles size={13} /> AI hardware security for RTL and FPGAs
          </div>
          <h2 className="mt-3 text-4xl font-semibold tracking-tight">Find hardware vulnerabilities, <span className="sbx-gradient-text">prove them, and fix them</span>.</h2>
          <p className="mt-3 text-sm text-muted">
            SiliconBreak X reads Verilog against a security contract, reproduces suspected weaknesses with trusted tests and formal checks,
            lets Factory Droid repair an isolated copy, re-verifies the patch, and shows the before and after on a real Basys 3.
          </p>
          <div className="mt-5 flex flex-wrap gap-2">
            <Button variant="primary" onClick={() => store.selectAudit(null)}><IconShield size={15} /> Open the Security Lab</Button>
            <Button variant="secondary" onClick={() => store.setTab("pipeline")}><IconCode size={15} /> Build a design in the Workspace</Button>
          </div>
        </div>
      </section>
      <section>
        <h3 className="mb-2 text-base font-semibold">Run your first audit</h3>
        <SecurityStart />
      </section>
    </div>
  );
}
