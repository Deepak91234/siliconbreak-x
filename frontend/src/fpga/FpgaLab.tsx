/**
 * FPGA Lab: one guided workflow on one Basys 3.
 *
 *   header       target (security demo / Workspace design), board status + Detect,
 *                and what SiliconBreak X last wrote to the board
 *   security demo
 *     1 Bitstream   before / after variant: Simulate + build, approval-gated Program
 *     2 Test        the board twin (2D/3D): each demo step sets switches and holds the
 *                   button; LEDs from the contract, the board simulation or the webcam
 *     3 Camera      webcam verification of the selected variant, expected vs observed
 *     Technical details: board-level simulation table, Vivado build, hashes,
 *                   programming log, pin mapping, connection (adapters, xvcd, raw logs)
 *   Workspace design
 *     the design's hardware inspector (preview, simulation replay, virtual FPGA,
 *     live states, guide, program), its camera verification, and connection details
 *
 * Programming writes volatile SRAM only and always asks for approval.
 */

import { useEffect, useMemo, useState } from "react";
import { errorMessage } from "../api/client";
import { securityApi, type FpgaInfo, type SecurityAudit } from "../api/security";
import { ErrorBoundary } from "../components/ErrorBoundary";
import { IconCamera, IconChip, IconCube } from "../components/icons";
import { Button, Collapsible, Empty, KV, Pill, Segmented, Select, cx } from "../components/ui";
import { HardwareInspector } from "../hardware/HardwareInspector";
import { fmtDateTime } from "../lib/format";
import { isAuditProject } from "../lib/sections";
import { hardwareState, runStatus } from "../lib/status";
import { toast } from "../lib/toast";
import { useResource } from "../lib/useResource";
import { BoardCamera, ObservationTable, VERDICT } from "../security/BoardCamera";
import { latestObservation, latestProgram, variantName } from "../security/derive";
import { BoardSim } from "../security/HardwareDemo";
import { SecurityStart } from "../security/SecurityPanel";
import { effectiveRevisionId, effectiveRunId, runForRevision } from "../store/reducer";
import { useAppState, useStore } from "../store/store";
import { HardwareTab } from "../tabs/HardwareTab";
import { VisionTab } from "../tabs/vision/VisionTab";
import { DemoBoard, LedRow, SOURCE_LABEL, howTo, stepLeds, type LedSource } from "./demo";

type Target = "demo" | "design";

export function FpgaLab() {
  const tab = useAppState((s) => s.selection.tab);
  const all = useAppState((s) => s.security);
  const hasDemo = useMemo(() => Object.values(all).some((a) => a.case_id && a.record.case?.board), [all]);
  const [target, setTarget] = useState<Target>(tab === "vision" ? "design" : "demo");
  useEffect(() => { if (tab === "vision") setTarget("design"); }, [tab]);
  return (
    <div className="mx-auto max-w-7xl space-y-4">
      <BoardHeader target={target} setTarget={setTarget} hasDemo={hasDemo} />
      {target === "demo" ? <DemoWorkflow /> : <DesignWorkflow />}
    </div>
  );
}

function BoardHeader({ target, setTarget, hasDemo }: { target: Target; setTarget: (t: Target) => void; hasDemo: boolean }) {
  const store = useStore();
  const hw = useAppState((s) => s.hardware);
  const approvals = useAppState((s) => s.approvals);
  const all = useAppState((s) => s.security);
  const hwInfo = hardwareState(hw?.state);
  const hold = latestProgram(approvals);
  const [detecting, setDetecting] = useState(false);
  const holdAudit = hold?.auditId ? all[hold.auditId] : undefined;
  const details = useAppState((s) => s.details);
  const holdDesign = hold?.revisionId ? Object.values(details).find((d) => d.revisions.some((r) => r.id === hold.revisionId)) : undefined;
  const holdRev = holdDesign?.revisions.find((r) => r.id === hold?.revisionId);
  const detect = async () => {
    setDetecting(true);
    try {
      const h = await store.detectHardware();
      toast(`Detect: ${h.last_detect?.message ?? hardwareState(h.state).label}`, h.last_detect?.ok ? "ok" : "info");
    } catch (e) {
      toast(`Detect failed: ${errorMessage(e)}`, "error");
    } finally {
      setDetecting(false);
    }
  };
  return (
    <section className="flex flex-wrap items-center gap-3 rounded-2xl border border-line bg-panel px-4 py-3 shadow-panel">
      <Segmented size="md" value={target} onChange={setTarget} options={[
        { value: "demo", label: "Security demo", title: hasDemo ? "before/after bitstreams of an audit" : "run the debug-bypass audit first" },
        { value: "design", label: "Workspace design", title: "the design selected in the Workspace" },
      ]} />
      <span className="h-6 w-px bg-line" aria-hidden />
      <span className="flex items-center gap-2 text-xs">
        <IconChip size={15} className="text-faint" />
        <Pill tone={hwInfo.tone} title={hw?.message ?? ""}>{hw ? hwInfo.label : "checking…"}</Pill>
        <Button size="sm" variant="ghost" busy={detecting} onClick={() => void detect()} title="Probe the USB-JTAG cable and read the FPGA idcode">Detect</Button>
      </span>
      <span className="min-w-0 flex-1 truncate text-right text-[11px] text-muted">
        On the board: <span className="text-fg">{hold ? (hold.auditId ? `${variantName(hold.variant ?? "?")} · ${holdAudit?.record.case?.title ?? hold.auditId}` : (holdDesign && holdRev ? `${holdDesign.project.title} (r${holdRev.number})` : `Workspace design revision ${hold.revisionId ?? "?"}`)) : "nothing programmed by SiliconBreak X"}</span>
        {hold && <> · {fmtDateTime(hold.at)} · lost on power-cycle</>}
      </span>
    </section>
  );
}

// ------------------------------------------------------------------ security demo workflow

function Step({ n, title, subtitle, children, right }: { n: number; title: string; subtitle?: string; children: React.ReactNode; right?: React.ReactNode }) {
  return (
    <section className="rounded-2xl border border-line bg-panel shadow-panel">
      <header className="flex flex-wrap items-center gap-3 border-b border-line px-4 py-3">
        <span className="sbx-brand-bg flex h-7 w-7 items-center justify-center rounded-full text-xs font-bold text-white">{n}</span>
        <div className="min-w-0 flex-1">
          <h3 className="text-sm font-semibold">{title}</h3>
          {subtitle && <p className="text-[11px] text-muted">{subtitle}</p>}
        </div>
        {right}
      </header>
      <div className="p-4">{children}</div>
    </section>
  );
}

function DemoWorkflow() {
  const store = useStore();
  const all = useAppState((s) => s.security);
  const selected = useAppState((s) => s.selection.auditId);
  const boardAudits = useMemo(() => Object.values(all).filter((a) => a.case_id && a.record.case?.board).sort((a, b) => b.created_at - a.created_at), [all]);
  const audit = boardAudits.find((a) => a.id === selected) ?? boardAudits.find((a) => a.status === "repaired_verified") ?? boardAudits[0] ?? null;
  if (!audit) {
    return (
      <div className="space-y-3">
        <Empty title="No audit with a Basys 3 demo yet" icon={<IconChip size={20} />}>Run the debug-bypass case: it ships a trusted board wrapper and a four-step demo.</Empty>
        <SecurityStart />
      </div>
    );
  }
  return (
    <>
      {boardAudits.length > 1 && (
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <span className="text-faint">Audit</span>
          <Select value={audit.id} onChange={(id) => store.selectAudit(id, "fpga")} className="max-w-[28rem]">
            {boardAudits.map((a) => <option key={a.id} value={a.id}>{a.record.case.title} · {fmtDateTime(a.created_at)} · {runStatus(a.status).label}</option>)}
          </Select>
          <button type="button" className="text-accent hover:underline" onClick={() => store.selectAudit(audit.id)}>open in Security Lab</button>
        </div>
      )}
      <DemoSteps key={audit.id} audit={audit} />
    </>
  );
}

function DemoSteps({ audit }: { audit: SecurityAudit }) {
  const hw = useAppState((s) => s.hardware);
  const openTech = useAppState((s) => s.selection.tab === "hardware");
  const version = JSON.stringify(audit.record.fpga ?? {}) + audit.record.repairs.length;
  const res = useResource<FpgaInfo>(audit.id, version, (sig) => securityApi.fpga(audit.id, sig));
  const info = res.data;
  const variants = info?.variants ?? [];
  const defaultVariant = [...variants].reverse().find((v) => v.kind !== "vulnerable")?.variant ?? "vulnerable";
  const [variant, setVariant] = useState<string | null>(null);
  const [source, setSource] = useState<LedSource>("simulated");
  const [view, setView] = useState<"2d" | "3d">("3d");
  const [stepId, setStepId] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [camera, setCamera] = useState(false);
  const v = variant ?? defaultVariant;
  const vState = info?.state[v];
  const steps = info?.demo ?? [];
  const step = steps.find((s) => s.id === stepId) ?? steps[0] ?? null;
  const leds = step ? stepLeds(step, source, vState) : null;
  const connected = hw?.state === "connected";
  const prov = info?.provenance?.[v];
  const obs = latestObservation(vState);
  const vInfo = variants.find((x) => x.variant === v);

  const act = async (key: string, fn: () => Promise<unknown>, ok: string) => {
    setBusy(key);
    try {
      await fn();
      toast(ok, "info");
    } catch (e) {
      toast(errorMessage(e), "error");
    } finally {
      setBusy(null);
    }
  };

  if (res.error) return <p className="text-xs text-bad">{errorMessage(res.error)}</p>;
  if (!info) return <p className="text-xs text-muted">Loading the board demo…</p>;

  return (
    <div className="space-y-4">
      <Step n={1} title="Choose and load a bitstream" subtitle="build each variant once; programming asks for your approval and writes SRAM only">
        <div className="grid gap-3 md:grid-cols-2">
          {variants.map((x) => {
            const st = info.state[x.variant];
            const b = st?.build;
            const loaded = info.provenance?.[x.variant]?.confirmed;
            const active = x.variant === v;
            return (
              <div key={x.variant} role="button" tabIndex={0} onClick={() => setVariant(x.variant)} onKeyDown={(e) => e.key === "Enter" && setVariant(x.variant)}
                   className={cx("cursor-pointer rounded-xl border p-3 transition-colors", active ? "border-accent/60 bg-accent/5" : "border-line hover:border-line-strong")}>
                <div className="flex items-center gap-2">
                  <Pill tone={x.kind === "vulnerable" ? "orange" : x.kind === "reference" ? "violet" : "ok"}>
                    {x.kind === "vulnerable" ? "before" : x.kind === "reference" ? "after (reference)" : "after (agent patch)"}
                  </Pill>
                  {loaded && <Pill tone="info">on the board</Pill>}
                  {active && <span className="ml-auto text-[11px] font-medium text-accent">selected</span>}
                </div>
                <div className="mt-1.5 text-xs font-medium">{x.label}</div>
                <div className="mt-0.5 text-[11px] text-muted">
                  {b ? (b.ok ? `bitstream built · WNS ${b.timing?.wns_ns ?? "?"} ns` : `build ${b.status}: ${b.message}`) : st?.board_sim ? "simulated, not built" : "not built yet"}
                </div>
                <div className="mt-2.5 flex flex-wrap gap-2" onClick={(e) => e.stopPropagation()}>
                  <Button size="sm" variant="secondary" busy={busy === `b:${x.variant}`}
                          onClick={() => act(`b:${x.variant}`, () => securityApi.fpgaBuild(audit.id, x.variant), "Build started (board simulation, then Vivado); this card updates when it finishes.")}>
                    {b ? "Rebuild" : "Simulate + build"}
                  </Button>
                  <Button size="sm" variant="primary" disabled={!b?.ok || !connected} busy={busy === `p:${x.variant}`}
                          title={!b?.ok ? "build a bitstream first" : !connected ? (hw?.state === "unknown" ? "press Detect above first" : "connect the Basys 3") : "asks for your approval before programming"}
                          onClick={() => act(`p:${x.variant}`, () => securityApi.fpgaProgram(audit.id, x.variant), "Programming requested: approve it in Approvals (top bar).")}>
                    Program…
                  </Button>
                </div>
              </div>
            );
          })}
        </div>
      </Step>

      <Step n={2} title="Test on the board" subtitle={step ? `${step.id}: ${howTo(step)}` : "no demo steps"}
            right={<Segmented value={view} onChange={setView} options={[{ value: "2d", label: "2D" }, { value: "3d", label: "3D" }]} />}>
        <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_20rem]">
          <div className="rounded-xl border border-line bg-sunken/60 p-2">
            <DemoBoard step={step} leds={leds} source={source} view={view} height="h-[24rem]" />
          </div>
          <div className="space-y-3">
            <div>
              <div className="mb-1 text-[11px] text-faint">Demo step</div>
              <div className="space-y-1.5">
                {steps.map((s) => (
                  <button key={s.id} type="button" onClick={() => setStepId(s.id)}
                          className={cx("w-full rounded-lg px-3 py-2 text-left ring-1 ring-inset transition-colors",
                            step?.id === s.id ? "bg-accent/10 ring-accent/50" : "ring-line hover:ring-line-strong")}>
                    <div className="text-xs font-semibold"><span className="font-mono">{s.id}</span> {s.title}</div>
                    <div className="text-[11px] text-muted">{howTo(s)}</div>
                  </button>
                ))}
              </div>
            </div>
            <div>
              <div className="mb-1 text-[11px] text-faint">LEDs shown on the twin</div>
              <Segmented value={source} onChange={setSource} options={[
                { value: "expected", label: "Contract" }, { value: "simulated", label: "Simulation" }, { value: "observed", label: "Webcam" }]} />
              <p className="mt-1 text-[11px] text-muted">{SOURCE_LABEL[source]} · {variantName(v)}{leds ? "" : " · no data for this step (shown as unknown)"}</p>
            </div>
            {step && (
              <div className="space-y-1.5 rounded-lg border border-line p-2.5 text-[11px]">
                <div className="flex items-center justify-between gap-2"><span className="text-faint">required</span><LedRow hex={step.expect_leds} size="md" /></div>
                <div className="flex items-center justify-between gap-2"><span className="text-faint">simulated</span><LedRow hex={stepLeds(step, "simulated", vState)} size="md" /></div>
                <div className="flex items-center justify-between gap-2"><span className="text-faint">webcam</span><LedRow hex={stepLeds(step, "observed", vState)} size="md" /></div>
                <p className="text-faint">{step.expect_text}</p>
              </div>
            )}
          </div>
        </div>
      </Step>

      <Step n={3} title="Camera verification" subtitle={`records what the board's LEDs show at each step for ${variantName(v)}; only these recordings count as physical results`}>
        {camera ? (
          <BoardCamera auditId={audit.id} variant={v} label={vInfo?.label ?? v} onClose={() => { setCamera(false); res.reload(); }} />
        ) : (
          <div className="space-y-3">
            <div className="flex flex-wrap items-center gap-3">
              <Button variant="primary" disabled={!prov?.confirmed} onClick={() => setCamera(true)}
                      title={prov?.confirmed ? "record the LEDs the board shows at each demo step" : prov?.reason}>
                <IconCamera size={15} /> Verify with webcam
              </Button>
              {!prov?.confirmed && <p className="min-w-0 flex-1 text-xs text-warn">Needs this bitstream on the board: {prov?.reason ?? "program it first"}.</p>}
            </div>
            {obs ? (
              <div className="space-y-2">
                <div className="flex flex-wrap items-center gap-2 text-xs">
                  <Pill tone={VERDICT[obs.verdict].tone}>{VERDICT[obs.verdict].label}</Pill>
                  <span className="text-muted">{obs.at ? fmtDateTime(obs.at) : ""} · {obs.summary}</span>
                </div>
                <ObservationTable obs={obs} auditId={audit.id} />
              </div>
            ) : <p className="text-xs text-muted">No webcam recording for {variantName(v)} yet.</p>}
          </div>
        )}
      </Step>

      <Collapsible defaultOpen={openTech} title={<span className="text-xs">Technical details: board simulation, build, hashes, programming log, pins, connection</span>}>
        <div className="space-y-4">
          {vState?.board_sim ? <BoardSim steps={vState.board_sim.steps} note={vState.board_sim.note} message={vState.board_sim.message} />
            : <p className="text-xs text-muted">{variantName(v)} has not been simulated on the board top yet.</p>}
          {vState?.build && (
            <KV items={[
              ["Vivado", `${vState.build.status}${vState.build.vivado_version ? ` · ${vState.build.vivado_version}` : ""}${vState.build.duration_s ? ` · ${Math.round(vState.build.duration_s)} s` : ""}`],
              ["Timing", vState.build.timing ? `WNS ${vState.build.timing.wns_ns ?? "?"} ns, WHS ${vState.build.timing.whs_ns ?? "?"} ns` : "–"],
              ["Bitstream", vState.build.bitstream_bytes ? `${vState.build.bitstream_bytes} bytes` : "–"],
              ["SHA-256", <span key="s" className="break-all font-mono">{vState.build.bitstream_sha256 ?? "–"}</span>],
            ]} />
          )}
          {(vState?.programs ?? []).length > 0 && (
            <ul className="space-y-0.5 text-[11px]">
              {vState!.programs!.map((p, i) => (
                <li key={i} className={p.ok ? "text-ok" : "text-bad"}>{fmtDateTime(p.at)} · {p.adapter}: {p.ok ? "programmed" : p.state} · {p.message}</li>
              ))}
            </ul>
          )}
          <div className="grid gap-x-4 gap-y-0.5 text-2xs text-muted sm:grid-cols-2">
            {info.mapping.map((m) => <div key={m.control}><span className="font-mono text-fg">{m.control}</span> → <span className="font-mono">{m.signal}</span> · {m.text}</div>)}
          </div>
          <Collapsible defaultOpen={openTech} title={<span className="text-xs">Connection: adapters, USB, xvcd, last programming output</span>}><ConnectionDetails /></Collapsible>
        </div>
      </Collapsible>
    </div>
  );
}

// ------------------------------------------------------------------ Workspace design workflow

function DesignWorkflow() {
  const store = useStore();
  const projects = useAppState((s) => s.projects);
  const projectId = useAppState((s) => s.selection.projectId);
  const revisionId = useAppState(effectiveRevisionId);
  const openTech = useAppState((s) => s.selection.tab === "hardware");
  const designs = (projects ?? []).filter((p) => !isAuditProject(p.title));
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="text-faint">Design</span>
        <Select value={projectId ?? ""} onChange={(id) => store.selectProject(id || null)} className="max-w-[24rem]">
          <option value="">— choose a Workspace design —</option>
          {designs.map((p) => <option key={p.id} value={p.id}>{p.title}</option>)}
        </Select>
        {projectId && <button type="button" className="text-accent hover:underline" onClick={() => store.setTab("pipeline")}>open in Workspace</button>}
      </div>
      {!projectId ? (
        <Empty title="No design selected" icon={<IconCube size={20} />}>Choose a design above, or build one in the Workspace AI Builder.</Empty>
      ) : (
        <>
          <div className="h-[calc(100vh-14rem)] min-h-[36rem] overflow-hidden rounded-2xl border border-line bg-panel shadow-panel">
            <ErrorBoundary label="Board"><HardwareInspector /></ErrorBoundary>
          </div>
          <section className="rounded-2xl border border-line bg-panel p-4 shadow-panel">
            <h3 className="mb-3 flex items-center gap-2 text-sm font-semibold"><IconCamera size={16} className="text-accent" /> Camera verification of this design</h3>
            <ErrorBoundary label="Camera"><VisionTab revisionId={revisionId} /></ErrorBoundary>
          </section>
          <Collapsible defaultOpen={openTech} title={<span className="text-xs">Technical details: connection, program this revision, last programming output</span>}><ConnectionDetails /></Collapsible>
        </>
      )}
    </div>
  );
}

function ConnectionDetails() {
  const projectId = useAppState((s) => s.selection.projectId);
  const revisionId = useAppState(effectiveRevisionId);
  const revRun = useAppState((s) => runForRevision(s, projectId, revisionId));
  const latestRunId = useAppState(effectiveRunId);
  const latestRun = useAppState((s) => (latestRunId ? s.runs[latestRunId] ?? null : null));
  return <HardwareTab revisionId={revisionId} run={revRun ?? latestRun} />;
}
