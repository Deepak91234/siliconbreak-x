/**
 * Agent execution timeline. One row per real backend stage (backend/core/events.STAGES),
 * titled in the user's terms. Status, timing, summaries, logs and the exact tool
 * commands all come from run state and `log` events; nothing is estimated.
 *
 * Mapping to the engineering steps users think in:
 *   spec      = understand the request + write the hardware specification
 *   program   = request programming approval (status "blocked") + program the FPGA
 *   repair    = analyze simulation failures + repair the RTL (bounded attempts)
 */

import { useEffect, useMemo, useState } from "react";
import type { Run, Stage } from "../api/types";
import { StatusIcon } from "../components/StatusIcon";
import { Pill, cx } from "../components/ui";
import { fmtDuration, spanSeconds } from "../lib/format";
import { runStatus, stageStatus, TONE_TEXT } from "../lib/status";
import type { TabKey } from "../store/reducer";
import { useAppState, useStore } from "../store/store";

interface StageCopy { title: string; why: string; tab?: TabKey; blockedTitle?: string }

export const STAGE_COPY: Record<string, StageCopy> = {
  spec: { title: "Understand request → hardware specification", why: "Turns your words into an explicit spec: inputs, outputs, rates, reset and expected observations.", tab: "verilog" },
  validate: { title: "Validate against Basys 3 capabilities", why: "Checks buttons, switches, LEDs and timing exist on this board before any code is written." },
  rtl: { title: "Write Verilog", why: "Generates the behaviour core; the board wrapper and pin constraints come only from trusted mappings.", tab: "verilog" },
  lint: { title: "Lint with Verilator", why: "Catches width mismatches, latches and other synthesis hazards at both real and test clock rates.", tab: "simulation" },
  tests: { title: "Generate independent tests", why: "Builds test scenarios from the spec, not from the generated RTL, so they can reject a wrong design.", tab: "simulation" },
  simulate: { title: "Run simulation", why: "Runs the real RTL in Verilator against every scenario and records waveforms.", tab: "simulation" },
  repair: { title: "Analyze failures → repair code", why: "On failures, diagnoses the trace and asks the model for a targeted fix (at most 3 attempts).", tab: "verilog" },
  synth: { title: "Vivado synthesis", why: "Maps the RTL onto Artix-7 primitives in your existing Vivado container.", tab: "vivado" },
  impl: { title: "Placement and routing", why: "Places logic on the xc7a35t and routes it to the board's pins.", tab: "vivado" },
  timing: { title: "Timing and resource analysis", why: "Confirms the design meets 100 MHz timing and fits the device.", tab: "vivado" },
  bitstream: { title: "Generate bitstream", why: "Writes the .bit file and checks its header targets the right FPGA.", tab: "vivado" },
  detect: { title: "Detect FPGA", why: "Looks for the Basys 3 on USB/JTAG; without a board the run stops here honestly." },
  program: { title: "Program FPGA", blockedTitle: "Request programming approval", why: "Loads the bitstream into SRAM only after you approve this exact file." },
  verify: { title: "Verify hardware with webcam", why: "Measures the LEDs with your camera and compares them with the spec.", tab: "vision" },
};

function useTick(active: boolean): number {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    if (!active) return;
    const id = window.setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => window.clearInterval(id);
  }, [active]);
  return now;
}

export function CurrentAction({ run, compact = false, showTimer = true }: { run: Run | null; compact?: boolean; showTimer?: boolean }) {
  const running = run?.stages.find((s) => s.status === "running") ?? null;
  const blocked = run?.stages.find((s) => s.status === "blocked") ?? null;
  const now = useTick(!!(running || blocked));
  if (!run) return null;
  const st = running ?? blocked;
  const rs = runStatus(run.status);
  if (!st) {
    return (
      <div className={cx("flex items-center gap-2 rounded-md border border-line bg-panel px-3", compact ? "py-1.5" : "py-2.5")}>
        <span className={cx("text-sm font-medium", TONE_TEXT[rs.tone])}>Run {rs.label}</span>
        {run.error && <span className="truncate text-xs text-muted">· {run.error}</span>}
      </div>
    );
  }
  const copy = STAGE_COPY[st.key];
  const title = st.status === "blocked" && copy?.blockedTitle ? copy.blockedTitle : copy?.title ?? st.label;
  return (
    <div className={cx("flex items-center gap-3 rounded-md border px-3", compact ? "py-1.5" : "py-2.5",
      st.status === "blocked" ? "border-warn/40 bg-warn/5" : "border-accent/40 bg-accent/5")}>
      <StatusIcon status={st.status} className="h-4 w-4" />
      <div className="min-w-0 flex-1">
        <div className="text-2xs uppercase tracking-wide text-faint">{st.status === "blocked" ? "Waiting for you" : "Agent is working"}</div>
        <div className="truncate text-sm font-medium text-fg">{title}</div>
        {!compact && st.summary && <div className="truncate text-xs text-muted">{st.summary}</div>}
      </div>
      {showTimer && <span className="font-mono text-xs tabular-nums text-faint">{fmtDuration(spanSeconds(st.started_at, null, now))}</span>}
    </div>
  );
}

export function Timeline({ run }: { run: Run | null }) {
  const store = useStore();
  const [open, setOpen] = useState<string | null>(null);
  useEffect(() => {
    if (run?.id) void store.backfillRunLogs(run.id);
  }, [run?.id, store]);
  const anyActive = !!run?.stages.some((s) => s.status === "running" || s.status === "blocked");
  const now = useTick(anyActive);
  useEffect(() => {
    const active = run?.stages.find((s) => s.status === "running" || s.status === "blocked" || s.status === "failed");
    if (active) setOpen((o) => o ?? active.key);
  }, [run?.id]); // eslint-disable-line react-hooks/exhaustive-deps
  if (!run) return null;
  return (
    <ol className="relative space-y-1" aria-label="Agent timeline">
      {run.stages.map((s, i) => (
        <TimelineRow key={s.key} stage={s} index={i} last={i === run.stages.length - 1} runId={run.id}
                     open={open === s.key} onToggle={() => setOpen((o) => (o === s.key ? null : s.key))} now={now} />
      ))}
    </ol>
  );
}

function TimelineRow({ stage, index, last, runId, open, onToggle, now }: {
  stage: Stage; index: number; last: boolean; runId: string; open: boolean; onToggle: () => void; now: number;
}) {
  const store = useStore();
  const copy = STAGE_COPY[stage.key] ?? { title: stage.label, why: "" };
  const title = stage.status === "blocked" && copy.blockedTitle ? copy.blockedTitle : copy.title;
  const info = stageStatus(stage.status);
  const dur = spanSeconds(stage.started_at, stage.finished_at, now);
  const idle = stage.status === "pending" || stage.status === "skipped";
  return (
    <li className="relative pl-7">
      {!last && <span className="absolute left-[9px] top-6 h-[calc(100%-0.5rem)] w-px bg-line" aria-hidden />}
      <span className="absolute left-0 top-2"><StatusIcon status={stage.status} className="h-[18px] w-[18px]" /></span>
      <button type="button" onClick={onToggle} aria-expanded={open}
              className={cx("w-full rounded-md px-2.5 py-1.5 text-left transition-colors duration-150",
                open ? "bg-raised" : "hover:bg-raised/60")}>
        <div className="flex items-center gap-2">
          <span className="font-mono text-2xs text-faint">{String(index + 1).padStart(2, "0")}</span>
          <span className={cx("min-w-0 flex-1 truncate text-sm", idle ? "text-muted" : "text-fg")}>{title}</span>
          {stage.status !== "pending" && <Pill tone={info.tone}>{info.label}</Pill>}
          <span className="w-14 text-right font-mono text-2xs tabular-nums text-faint">{dur != null && stage.status !== "skipped" ? fmtDuration(dur) : ""}</span>
        </div>
        {stage.summary && <div className="mt-0.5 truncate pl-6 text-xs text-muted">{stage.summary}</div>}
      </button>
      {open && (
        <div className="mb-2 ml-2.5 mt-1 space-y-2 border-l border-line pl-3 text-xs">
          <p className="text-faint">{copy.why}</p>
          <StageDetail stage={stage} />
          <StageLogs runId={runId} stage={stage.key} />
          {copy.tab && (
            <button type="button" onClick={() => store.setTab(copy.tab as TabKey)} className="text-accent hover:underline">
              Open {({ verilog: "Code", simulation: "Simulation", vivado: "Build", hardware: "FPGA Lab", vision: "FPGA Lab camera", diagram: "Diagram" } as Record<string, string>)[copy.tab] ?? copy.tab} →
            </button>
          )}
        </div>
      )}
    </li>
  );
}

/** Human-readable parts of the stage detail the backend attached (explanations, artefacts, checks). */
function StageDetail({ stage }: { stage: Stage }) {
  const d = (stage.detail ?? {}) as Record<string, unknown>;
  const rows: Array<[string, string]> = [];
  const str = (k: string) => (typeof d[k] === "string" && d[k] ? String(d[k]) : null);
  if (str("interpreter")) rows.push(["Interpreter", String(d.interpreter)]);
  if (str("generator")) rows.push(["Generator", String(d.generator)]);
  if (str("explanation")) rows.push(["Agent explanation", String(d.explanation)]);
  if (Array.isArray(d.assumptions) && d.assumptions.length) rows.push(["Assumptions", (d.assumptions as string[]).join("; ")]);
  if (Array.isArray(d.change_summary) && d.change_summary.length) rows.push(["Changes", (d.change_summary as string[]).join("; ")]);
  if (str("sha256")) rows.push(["Bitstream sha256", String(d.sha256)]);
  if (str("path")) rows.push(["Artifact", String(d.path)]);
  if (str("message")) rows.push(["Message", String(d.message)]);
  const scen = Array.isArray(d.scenarios) ? (d.scenarios as Array<{ id: string; title: string; passed: boolean }>) : [];
  const issues = Array.isArray(d.issues) ? (d.issues as Array<{ severity: string; message: string }>) : [];
  if (!rows.length && !scen.length && !issues.length) return null;
  return (
    <div className="space-y-1.5">
      {rows.map(([k, v]) => (
        <div key={k}><span className="text-faint">{k}: </span><span className={k.includes("sha") ? "break-all font-mono" : "text-fg"}>{v}</span></div>
      ))}
      {scen.length > 0 && (
        <ul className="grid gap-0.5 sm:grid-cols-2">
          {scen.map((s) => (
            <li key={s.id} className="flex items-center gap-1.5"><StatusIcon status={s.passed ? "passed" : "failed"} className="h-3 w-3" />
              <span className="truncate text-muted" title={s.title}>{s.title || s.id}</span></li>
          ))}
        </ul>
      )}
      {issues.map((x, i) => <div key={i} className={x.severity === "error" ? "text-bad" : "text-warn"}>{x.severity}: {x.message}</div>)}
    </div>
  );
}

function StageLogs({ runId, stage }: { runId: string; stage: string }) {
  const logs = useAppState((s) => s.logs[runId]);
  const lines = useMemo(() => (logs ?? []).filter((l) => l.stage === stage), [logs, stage]);
  const cmds = lines.filter((l) => l.line.startsWith("$ ") || l.line.startsWith("CL_CMD "));
  if (!lines.length) return null;
  return (
    <div className="space-y-1.5">
      {cmds.length > 0 && (
        <div>
          <div className="mb-0.5 text-faint">Commands</div>
          <pre className="max-h-28 overflow-auto whitespace-pre-wrap break-all rounded bg-sunken p-2 font-mono text-2xs text-muted">
            {cmds.map((c) => c.line.replace(/^CL_CMD /, "$ ")).join("\n")}
          </pre>
        </div>
      )}
      <details>
        <summary className="cursor-pointer text-faint hover:text-fg">Log ({lines.length} lines)</summary>
        <pre className="mt-1 max-h-56 overflow-auto rounded bg-sunken p-2 font-mono text-2xs leading-relaxed text-muted">
          {lines.slice(-400).map((l) => l.line).join("\n")}
        </pre>
      </details>
    </div>
  );
}
