/**
 * The 14 pipeline stages of a run (docs/api.md order), live from stage.updated /
 * run.updated events. Durations come from the stages' server timestamps; while
 * a stage runs its elapsed time is measured against the wall clock.
 */

import { useMemo, useState } from "react";
import { errorMessage } from "../api/client";
import type { Run, RevisionSummary, Stage } from "../api/types";
import { STAGE_KEYS } from "../api/types";
import { isRunActive, type ProjectDetail, type TabKey } from "../store/reducer";
import { useAppState, useStore } from "../store/store";
import { fmtDuration, fmtTime, prettyJson, spanSeconds } from "../lib/format";
import { revisionKind, revisionStatus, runStatus, stageStatus, TONE_TEXT } from "../lib/status";
import { toast } from "../lib/toast";
import { useNow } from "../lib/useResource";
import { StatusIcon } from "./StatusIcon";
import { Button, Chevron, Empty, Pill, Pre, Select, cx } from "./ui";

const STAGE_TAB: Record<string, TabKey> = {
  spec: "verilog", validate: "verilog", rtl: "verilog",
  lint: "simulation", tests: "simulation", simulate: "simulation", repair: "simulation",
  synth: "vivado", impl: "vivado", timing: "vivado", bitstream: "vivado",
  detect: "hardware", program: "hardware", verify: "vision",
};

/** Placeholder rows for keys the server did not send (never invented status: shown as "not reported"). */
function fullStageList(run: Run): Array<Stage | { key: string; missing: true }> {
  const byKey = new Map(run.stages.map((s) => [s.key, s]));
  const out: Array<Stage | { key: string; missing: true }> = STAGE_KEYS.map((k) => byKey.get(k) ?? { key: k, missing: true as const });
  for (const s of run.stages) if (!(STAGE_KEYS as readonly string[]).includes(s.key)) out.push(s);
  return out;
}

function StageRow({ stage, nowS, onOpenTab }: { stage: Stage; nowS: number; onOpenTab: (t: TabKey) => void }) {
  const [open, setOpen] = useState(false);
  const st = stageStatus(stage.status);
  const dur = spanSeconds(stage.started_at, stage.finished_at, stage.status === "running" ? nowS : undefined);
  const hasDetail = stage.detail && Object.keys(stage.detail).length > 0;
  return (
    <li className={cx("rounded-md", stage.status === "running" && "bg-accent/5 ring-1 ring-accent/20", stage.status === "blocked" && "bg-warn/5 ring-1 ring-warn/25")}>
      <button type="button" onClick={() => setOpen(!open)} className="flex w-full items-start gap-2 rounded-md px-2 py-1.5 text-left hover:bg-raised/60">
        <StatusIcon status={stage.status} className="mt-0.5 h-4 w-4" />
        <div className="min-w-0 flex-1">
          <div className="flex items-baseline gap-2">
            <span className={cx("truncate text-[13px]", stage.status === "pending" ? "text-muted" : "text-fg")}>{stage.label}</span>
            <span className={cx("ml-auto shrink-0 text-[10px]", TONE_TEXT[st.tone])}>{st.label}</span>
          </div>
          {stage.summary ? <div className="truncate text-[11px] text-muted" title={stage.summary}>{stage.summary}</div> : null}
        </div>
        <span className="mt-0.5 w-14 shrink-0 text-right font-mono text-[10px] tabular-nums text-faint">{dur !== null ? fmtDuration(dur) : ""}</span>
      </button>
      {open && (
        <div className="space-y-2 px-8 pb-2 text-[11px]">
          {stage.summary && <p className="whitespace-pre-wrap break-words text-muted">{stage.summary}</p>}
          <div className="flex flex-wrap gap-x-3 text-faint">
            <span>key <span className="font-mono">{stage.key}</span></span>
            <span>started {fmtTime(stage.started_at)}</span>
            <span>finished {fmtTime(stage.finished_at)}</span>
          </div>
          {hasDetail && <Pre maxH="max-h-56">{prettyJson(stage.detail)}</Pre>}
          {STAGE_TAB[stage.key] && (
            <button type="button" className="text-accent hover:underline" onClick={() => onOpenTab(STAGE_TAB[stage.key])}>
              Open {STAGE_TAB[stage.key]} tab
            </button>
          )}
        </div>
      )}
    </li>
  );
}

function RevisionChain({ run, revisions }: { run: Run; revisions: RevisionSummary[] }) {
  const store = useStore();
  const selected = useAppState((s) => s.selection.revisionId);
  const ids = run.revision_ids?.length ? run.revision_ids : run.revision_id ? [run.revision_id] : [];
  if (!ids.length) return null;
  const byId = new Map(revisions.map((r) => [r.id, r]));
  return (
    <div className="space-y-1">
      <div className="text-[10px] font-semibold uppercase tracking-wide text-faint">Revisions in this run{ids.length > 1 ? " (incl. repair attempts)" : ""}</div>
      <div className="flex flex-wrap items-center gap-1">
        {ids.map((id, i) => {
          const r = byId.get(id);
          const kind = revisionKind(r?.kind);
          const rs = revisionStatus(r?.status);
          return (
            <div key={id} className="flex items-center gap-1">
              {i > 0 && <span className="text-faint">&rarr;</span>}
              <button
                type="button"
                onClick={() => store.selectRevision(selected === id ? null : id)}
                className={cx(
                  "flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[11px] ring-1 ring-inset",
                  selected === id ? "bg-accent/15 ring-accent/40" : "ring-line hover:bg-raised",
                )}
                title={r ? `${kind.label} · ${rs.label}${r.generator ? ` · ${r.generator}` : ""}` : id}
              >
                <span className="font-mono font-semibold">{r ? `r${r.number}` : id}</span>
                {r && <span className={TONE_TEXT[kind.tone]}>{kind.label}</span>}
                {r?.sim_status && <span className={r.sim_status === "passed" ? "text-ok" : "text-bad"}>sim {r.sim_status}</span>}
              </button>
            </div>
          );
        })}
      </div>
    </div>
  );
}

export function PipelineMonitor({ runId, detail }: { runId: string | null; detail: ProjectDetail | undefined }) {
  const store = useStore();
  const run = useAppState((s) => (runId ? s.runs[runId] : undefined));
  const runs = useAppState((s) => s.runs);
  const pinned = useAppState((s) => s.selection.runId);
  const active = isRunActive(run);
  const now = useNow(1000, active);
  const nowS = now / 1000;
  const [cancelling, setCancelling] = useState(false);

  const runList = useMemo(
    () => (detail ? detail.runIds.map((id) => runs[id]).filter((r): r is Run => !!r).sort((a, b) => b.created_at - a.created_at) : []),
    [detail, runs],
  );

  if (!detail) {
    return (
      <div className="p-3">
        <Empty title="No pipeline yet">Select a project or describe a new design to start a run.</Empty>
      </div>
    );
  }
  if (!run) {
    return (
      <div className="p-3">
        <Empty title="No runs in this project">Runs appear here as soon as the agent starts one.</Empty>
      </div>
    );
  }

  const rs = runStatus(run.status);
  const total = spanSeconds(run.created_at, run.finished_at, active ? nowS : undefined);
  const stages = fullStageList(run);
  const doneCount = run.stages.filter((s) => ["passed", "skipped"].includes(s.status)).length;

  const cancel = async () => {
    setCancelling(true);
    try {
      await store.cancelRun(run.id);
      toast("Cancel requested", "info");
    } catch (e) {
      toast(`Cancel failed: ${errorMessage(e)}`, "error");
    } finally {
      setCancelling(false);
    }
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="space-y-2 border-b border-line px-3 py-2">
        <div className="flex items-center gap-2">
          <h2 className="text-sm font-semibold text-fg">Pipeline</h2>
          <Pill tone={rs.tone}>{rs.label}</Pill>
          <span className="ml-auto font-mono text-[11px] tabular-nums text-muted">{fmtDuration(total)}</span>
        </div>
        <div className="flex items-center gap-2">
          <Select value={pinned ?? ""} onChange={(v) => store.selectRun(v || null)} className="min-w-0 flex-1">
            <option value="">Latest run ({run.id})</option>
            {runList.map((r) => (
              <option key={r.id} value={r.id}>
                {r.id} · {r.trigger} · {runStatus(r.status).label}
              </option>
            ))}
          </Select>
          {active && (
            <Button size="sm" variant="danger" busy={cancelling} onClick={() => void cancel()}>
              Cancel
            </Button>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-1 text-[11px] text-muted">
          <span>{run.trigger}</span>
          <span className="text-faint">·</span>
          <span>started {fmtTime(run.created_at)}</span>
          <span className="text-faint">·</span>
          <span>{doneCount}/{run.stages.length} stages done</span>
          {run.options?.simulation_only && <Pill tone="info">simulation only</Pill>}
          {run.options?.inject_fault && <Pill tone="warn" title="Deliberately injected defect">TEST MODE: {run.options.inject_fault}</Pill>}
        </div>
        {run.request_text && <p className="line-clamp-2 text-[11px] italic text-muted" title={run.request_text}>&ldquo;{run.request_text}&rdquo;</p>}
        {run.error && <div className="rounded border border-bad/30 bg-bad/10 px-2 py-1 text-[11px] text-bad">{run.error}</div>}
        <RevisionChain run={run} revisions={detail.revisions} />
      </div>
      <ol className="min-h-0 flex-1 space-y-0.5 overflow-y-auto p-1.5">
        {stages.map((s) =>
          "missing" in s ? (
            <li key={s.key} className="flex items-center gap-2 px-2 py-1.5 text-[12px] text-faint">
              <Chevron open={false} className="opacity-0" />
              <span className="font-mono">{s.key}</span>
              <span className="ml-auto text-[10px]">not reported</span>
            </li>
          ) : (
            <StageRow key={s.key} stage={s} nowS={nowS} onOpenTab={(t) => store.setTab(t)} />
          ),
        )}
      </ol>
    </div>
  );
}

/** Compact horizontal variant for narrower screens: one dot per stage. */
export function PipelineStrip({ runId }: { runId: string | null }) {
  const store = useStore();
  const run = useAppState((s) => (runId ? s.runs[runId] : undefined));
  if (!run) return null;
  const rs = runStatus(run.status);
  const current = run.stages.find((s) => s.status === "running" || s.status === "blocked");
  return (
    <div className="flex items-center gap-3 overflow-x-auto border-b border-line bg-panel px-3 py-2">
      <Pill tone={rs.tone}>{rs.label}</Pill>
      <ol className="flex items-center gap-1">
        {fullStageList(run).map((s) => (
          <li key={s.key} title={"missing" in s ? `${s.key}: not reported` : `${s.label}: ${stageStatus(s.status).label}${s.summary ? ` – ${s.summary}` : ""}`}>
            <button type="button" onClick={() => STAGE_TAB[s.key] && store.setTab(STAGE_TAB[s.key])}>
              {"missing" in s ? <span className="block h-4 w-4 rounded-full border border-dashed border-line" /> : <StatusIcon status={s.status} className="h-4 w-4" />}
            </button>
          </li>
        ))}
      </ol>
      {current && (
        <span className="min-w-0 truncate text-[11px] text-muted">
          <span className="text-fg">{current.label}</span>{current.summary ? ` – ${current.summary}` : ""}
        </span>
      )}
    </div>
  );
}
