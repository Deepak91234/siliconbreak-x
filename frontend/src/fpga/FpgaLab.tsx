/**
 * FPGA Lab: everything about the physical Basys 3 in one place.
 *
 *   Security demo   the audit's before/after demo on the board twin (2D/3D), each
 *                   step's switches and button, LEDs from the contract, the board-
 *                   level simulation or the webcam (labelled); build, approval-gated
 *                   programming and webcam evidence per variant (HardwareDemo)
 *   Design board    the Workspace design's hardware inspector (preview, simulation
 *                   replay / virtual FPGA, live camera states, operating guide)
 *   Connection      adapters, detection, xvcd, last programming result
 *
 * The status strip shows the board connection and what CircuitLoop last wrote to
 * the board (latest executed program approval), so audit and design records
 * never contradict each other.
 */

import { useEffect, useMemo, useState, type ReactNode } from "react";
import { securityApi, type SecurityAudit } from "../api/security";
import { ErrorBoundary } from "../components/ErrorBoundary";
import { IconCamera, IconChip, IconCube, IconPlug, IconShield, IconTerminal } from "../components/icons";
import { Button, Empty, Pill, Segmented, Tabs, cx } from "../components/ui";
import { HardwareInspector } from "../hardware/HardwareInspector";
import { fmtDateTime } from "../lib/format";
import { hardwareState, runStatus } from "../lib/status";
import { useResource } from "../lib/useResource";
import { fpgaVariants, latestObservation, latestProgram, variantName } from "../security/derive";
import { HardwareDemo } from "../security/HardwareDemo";
import { SecurityStart } from "../security/SecurityPanel";
import { effectiveRevisionId, effectiveRunId, runForRevision } from "../store/reducer";
import { useAppState, useStore } from "../store/store";
import { HardwareTab } from "../tabs/HardwareTab";
import { DemoBoard, LedRow, SOURCE_LABEL, howTo, stepLeds, type LedSource } from "./demo";

type View = "demo" | "design" | "connection";

export function FpgaLab() {
  const tab = useAppState((s) => s.selection.tab);
  const [view, setView] = useState<View>(tab === "hardware" ? "connection" : "demo");
  useEffect(() => { if (tab === "hardware") setView("connection"); }, [tab]);
  return (
    <div className="space-y-4">
      <StatusStrip />
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-line">
        <Tabs value={view} onChange={setView} options={[
          { value: "demo", label: <span className="flex items-center gap-1.5"><IconShield size={14} />Security demo</span> },
          { value: "design", label: <span className="flex items-center gap-1.5"><IconCube size={14} />Design board</span> },
          { value: "connection", label: <span className="flex items-center gap-1.5"><IconPlug size={14} />Connection</span> },
        ]} />
        <span className="pb-2 text-[11px] text-faint">programming writes volatile SRAM only and always asks for your approval</span>
      </div>
      {view === "demo" && <SecurityDemo />}
      {view === "design" && (
        <div className="h-[calc(100vh-15rem)] min-h-[40rem] overflow-hidden rounded-xl border border-line bg-panel shadow-panel">
          <ErrorBoundary label="Hardware inspector"><HardwareInspector /></ErrorBoundary>
        </div>
      )}
      {view === "connection" && <ConnectionView />}
    </div>
  );
}

function StatusCard({ icon, label, value, sub, tone }: { icon: ReactNode; label: string; value: ReactNode; sub?: ReactNode; tone: "ok" | "warn" | "idle" | "bad" | "info" }) {
  return (
    <div className="flex min-w-0 items-start gap-3 rounded-xl border border-line bg-panel p-3.5 shadow-panel">
      <span className={cx("flex h-10 w-10 shrink-0 items-center justify-center rounded-xl ring-1 ring-inset",
        tone === "ok" ? "bg-ok/10 text-ok ring-ok/25" : tone === "warn" ? "bg-warn/10 text-warn ring-warn/25" : tone === "bad" ? "bg-bad/10 text-bad ring-bad/25"
          : tone === "info" ? "bg-info/10 text-info ring-info/25" : "bg-raised text-faint ring-line")}>{icon}</span>
      <div className="min-w-0">
        <div className="text-[11px] font-medium text-faint">{label}</div>
        <div className="truncate text-sm font-semibold text-fg">{value}</div>
        {sub && <div className="truncate text-[11px] text-muted">{sub}</div>}
      </div>
    </div>
  );
}

function StatusStrip() {
  const hw = useAppState((s) => s.hardware);
  const env = useAppState((s) => s.environment);
  const approvals = useAppState((s) => s.approvals);
  const all = useAppState((s) => s.security);
  const hwInfo = hardwareState(hw?.state);
  const hold = latestProgram(approvals);
  const pending = Object.values(approvals).filter((a) => a.status === "pending" && a.kind === "program_fpga").length;
  const holdAudit = hold?.auditId ? all[hold.auditId] : undefined;
  return (
    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      <StatusCard icon={<IconChip size={20} />} label="Basys 3" tone={hw?.state === "connected" ? "ok" : hw?.state === "no_board" ? "idle" : "warn"}
                  value={hw ? hwInfo.label : "checking…"}
                  sub={hw?.board?.idcode ? `idcode ${hw.board.idcode}${hw.preferred_adapter ? ` · ${hw.preferred_adapter}` : ""}` : hw?.message || "connect PROG/UART via USB"} />
      <StatusCard icon={<IconShield size={20} />} label="On the board now (last program by SiliconBreak X)" tone={hold ? "info" : "idle"}
                  value={hold ? (hold.auditId ? `${variantName(hold.variant ?? "?")} · ${holdAudit?.record.case?.title ?? hold.auditId}` : `design revision ${hold.revisionId ?? "?"}`) : "nothing programmed by SiliconBreak X"}
                  sub={hold ? `${fmtDateTime(hold.at)} · sha ${hold.sha?.slice(0, 12)}… · lost on power-cycle` : "power-on design from flash"} />
      <StatusCard icon={<IconTerminal size={20} />} label="Programmer" tone={env?.openfpgaloader.available ? "ok" : "warn"}
                  value={env ? (env.openfpgaloader.available ? `openFPGALoader ${env.openfpgaloader.version?.match(/v?\d+(\.\d+)+/)?.[0] ?? ""}` : "openFPGALoader missing") : "checking…"}
                  sub={env ? (env.vivado.ready ? `Vivado ${env.vivado.vivado_version ?? ""} in ${env.vivado.active_container ?? "container"}` : "Vivado container not ready") : ""} />
      <StatusCard icon={<IconCamera size={20} />} label="Approvals" tone={pending ? "warn" : "idle"}
                  value={pending ? `${pending} programming request${pending === 1 ? "" : "s"} waiting` : "none waiting"}
                  sub="approve or deny in the top bar or the chat" />
    </div>
  );
}

// ------------------------------------------------------------------ security demo

function SecurityDemo() {
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
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs text-faint">Audit</span>
        <select value={audit.id} onChange={(e) => store.selectAudit(e.target.value, "fpga")}
                className="h-8 rounded-lg border border-line bg-panel px-2 text-xs shadow-sm outline-none focus:border-accent">
          {boardAudits.map((a) => <option key={a.id} value={a.id}>{a.record.case.title} · {fmtDateTime(a.created_at)} · {runStatus(a.status).label}</option>)}
        </select>
        <button type="button" className="text-xs text-accent hover:underline" onClick={() => store.selectAudit(audit.id)}>open in Security Lab</button>
      </div>
      <div className="grid gap-4 2xl:grid-cols-[minmax(0,1fr)_minmax(0,0.9fr)]">
        <BoardTwin audit={audit} />
        <div className="min-w-0"><HardwareDemo audit={audit} /></div>
      </div>
    </div>
  );
}

function BoardTwin({ audit }: { audit: SecurityAudit }) {
  const version = JSON.stringify(audit.record.fpga ?? {}) + audit.record.repairs.length;
  const info = useResource(audit.id, version, (sig) => securityApi.fpga(audit.id, sig));
  const vs = fpgaVariants(audit);
  const variants = info.data?.variants ?? [];
  const steps = info.data?.demo ?? [];
  const defaultVariant = [...variants].reverse().find((v) => v.kind !== "vulnerable")?.variant ?? "vulnerable";
  const [variant, setVariant] = useState<string | null>(null);
  const [source, setSource] = useState<LedSource>("simulated");
  const [view, setView] = useState<"2d" | "3d">("3d");
  const [stepId, setStepId] = useState<string | null>(null);
  const v = variant ?? defaultVariant;
  const vState = vs[v];
  const step = steps.find((s) => s.id === stepId) ?? steps[0] ?? null;
  const leds = step ? stepLeds(step, source, vState) : null;
  const obs = latestObservation(vState);

  return (
    <section className="min-w-0 overflow-hidden rounded-xl border border-line bg-panel shadow-panel">
      <header className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-2.5">
        <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-accent/10 text-accent"><IconCube size={16} /></span>
        <h3 className="text-sm font-semibold">Basys 3 twin</h3>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          {variants.length > 0 && <Segmented value={v} onChange={setVariant} options={variants.map((x) => ({ value: x.variant, label: variantName(x.variant), title: x.label }))} />}
          <Segmented value={view} onChange={setView} options={[{ value: "2d", label: "2D" }, { value: "3d", label: "3D" }]} />
        </div>
      </header>
      <div className="space-y-3 p-4">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[11px] text-faint">LEDs from</span>
          <Segmented value={source} onChange={setSource} options={[
            { value: "expected", label: "Contract" },
            { value: "simulated", label: "Simulation", title: vState?.board_sim ? "board-level Verilator run of this variant" : "not simulated yet" },
            { value: "observed", label: "Webcam", title: obs ? "latest webcam observation" : "no webcam observation for this variant" },
          ]} />
          <Pill tone={source === "observed" ? "ok" : source === "simulated" ? "info" : "violet"}>{SOURCE_LABEL[source]}</Pill>
        </div>
        <div className="rounded-xl border border-line bg-sunken/60 p-2">
          <DemoBoard step={step} leds={leds} source={source} view={view} height="h-[24rem]" />
        </div>
        {!leds && step && (
          <p className="rounded-lg border border-dashed border-line px-3 py-2 text-xs text-muted">
            {source === "simulated" ? "This variant has not been simulated on the board top yet: press Simulate + build." : source === "observed" ? "No webcam reading of this step for this variant yet." : ""} LEDs are shown as unknown, not guessed.
          </p>
        )}
        <div className="flex flex-wrap gap-1.5">
          {steps.map((s) => (
            <button key={s.id} type="button" onClick={() => setStepId(s.id)}
                    className={cx("rounded-lg px-3 py-1.5 text-left ring-1 ring-inset transition-colors",
                      step?.id === s.id ? "bg-accent/10 text-fg ring-accent/50" : "bg-panel text-muted ring-line hover:text-fg")}>
              <span className="font-mono text-[11px] font-semibold">{s.id}</span>
              <span className="ml-1.5 text-[11px]">{s.title}</span>
            </button>
          ))}
        </div>
        {step && (
          <div className="rounded-xl border border-accent/30 bg-accent/[0.06] px-4 py-3">
            <div className="text-xs font-semibold text-fg">{step.id}: {howTo(step)}</div>
            <div className="mt-2 grid gap-2 text-[11px] sm:grid-cols-3">
              <div><div className="text-faint">contract requires</div><LedRow hex={step.expect_leds} size="md" /><div className="mt-0.5 text-muted">{step.expect_text}</div></div>
              <div><div className="text-faint">simulated ({variantName(v)})</div><LedRow hex={stepLeds(step, "simulated", vState)} size="md" /></div>
              <div><div className="text-faint">webcam ({variantName(v)})</div><LedRow hex={stepLeds(step, "observed", vState)} size="md" /></div>
            </div>
          </div>
        )}
        <p className="text-[11px] text-faint">LED15 = granted (green) · LED14 = denied (red) · LED7–LED0 = data (amber). Switch up = on; SW0 is the rightmost switch.</p>
      </div>
    </section>
  );
}

// ------------------------------------------------------------------ connection

function ConnectionView() {
  const projectId = useAppState((s) => s.selection.projectId);
  const revisionId = useAppState(effectiveRevisionId);
  const revRun = useAppState((s) => runForRevision(s, projectId, revisionId));
  const latestRunId = useAppState(effectiveRunId);
  const latestRun = useAppState((s) => (latestRunId ? s.runs[latestRunId] ?? null : null));
  const store = useStore();
  return (
    <div className="space-y-3">
      {!projectId && (
        <div className="flex flex-wrap items-center gap-2 rounded-lg border border-line bg-panel px-3 py-2 text-xs text-muted">
          Design-specific programming needs a Workspace design.
          <Button size="sm" variant="ghost" onClick={() => store.setTab("pipeline")}>Open Workspace</Button>
        </div>
      )}
      <HardwareTab revisionId={revisionId} run={revRun ?? latestRun} />
    </div>
  );
}
