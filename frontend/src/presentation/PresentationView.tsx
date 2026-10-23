/**
 * Presentation mode: one screen that tells the whole story for an audience.
 *
 *   LIVE    the selected project's latest run, updated by real WebSocket events.
 *   REPLAY  a recorded run re-played from its persisted events (GET /api/runs/{id}/events),
 *           badged REPLAY with the original recording time. Idle gaps longer than 2 s are
 *           shortened (and the UI says so); nothing is added or re-ordered.
 *
 * Panels that depend on a stage (simulation results, diagram, board, verification)
 * stay hidden until the live or replayed run has actually reached that stage.
 */

import { useEffect, useMemo, useState } from "react";
import { api, errorMessage } from "../api/client";
import type { CLEvent, Run, Stage } from "../api/types";
import { StatusIcon } from "../components/StatusIcon";
import { Button, Pill, Select, cx } from "../components/ui";
import { fmtDateTime } from "../lib/format";
import { runStatus, stageStatus, verdict } from "../lib/status";
import { setUi } from "../lib/ui";
import { useResource } from "../lib/useResource";
import { effectiveRunId } from "../store/reducer";
import { useAppState } from "../store/store";
import { DiagramView } from "../workspace/DiagramView";
import { CurrentAction, STAGE_COPY } from "../workspace/Timeline";
import { HardwareInspector } from "../hardware/HardwareInspector";

const GAP_CAP_S = 2;

interface ReplayFrame { stages: Record<string, Stage>; messages: Array<{ role: string; text: string; kind: string }>; ended: boolean }

function useReplayRun(runId: string | null, speed: number) {
  const [events, setEvents] = useState<CLEvent[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [startedAt, setStartedAt] = useState(() => performance.now());
  const [now, setNow] = useState(() => performance.now());
  useEffect(() => {
    setEvents(null);
    setError(null);
    if (!runId) return;
    (async () => {
      const all: CLEvent[] = [];
      let after = 0;
      for (let page = 0; page < 10; page++) {
        const evs = await api.runEvents(runId, after, 2000);
        all.push(...evs.filter((e) => e.type !== "log"));
        if (evs.length < 2000) break;
        after = evs[evs.length - 1].seq;
      }
      setEvents(all);
      setStartedAt(performance.now());
    })().catch((e) => setError(errorMessage(e)));
  }, [runId]);
  useEffect(() => {
    const id = window.setInterval(() => setNow(performance.now()), 200);
    return () => window.clearInterval(id);
  }, []);
  // Compressed timeline: real gaps between events, capped at GAP_CAP_S.
  const offsets = useMemo(() => {
    const out: number[] = [];
    let acc = 0;
    events?.forEach((e, i) => {
      if (i > 0) acc += Math.min(GAP_CAP_S, Math.max(0, e.ts - events[i - 1].ts));
      out.push(acc);
    });
    return out;
  }, [events]);
  const elapsed = ((now - startedAt) / 1000) * speed;
  const frame: ReplayFrame = { stages: {}, messages: [], ended: false };
  let shown = 0;
  events?.forEach((e, i) => {
    if (offsets[i] > elapsed) return;
    shown = i + 1;
    const d = e.data as Record<string, unknown>;
    if (e.type === "stage.updated" && d.stage) {
      const st = d.stage as Stage;
      frame.stages[st.key] = st;
    } else if (e.type === "agent.message" && d.message) {
      const m = d.message as { role: string; text: string; kind: string };
      frame.messages.push(m);
    }
  });
  frame.ended = !!events && shown >= events.length;
  return { events, error, frame, restart: () => setStartedAt(performance.now()), total: offsets[offsets.length - 1] ?? 0, elapsed };
}

export function PresentationView() {
  const projectId = useAppState((s) => s.selection.projectId);
  const detail = useAppState((s) => (projectId ? s.details[projectId] : undefined));
  const liveRunId = useAppState(effectiveRunId);
  const liveRun = useAppState((s) => (liveRunId ? s.runs[liveRunId] ?? null : null));
  const allRuns = useAppState((s) => s.runs); // select the stable map; derive the list outside the selector
  const runs = useMemo(() => (detail?.runIds ?? []).map((id) => allRuns[id]).filter(Boolean) as Run[], [detail?.runIds, allRuns]);
  const [source, setSource] = useState<"live" | string>("live");
  const [speed, setSpeed] = useState(2);
  const replay = useReplayRun(source === "live" ? null : source, speed);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setUi({ presentation: false });
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const isReplay = source !== "live";
  const replayRun = isReplay ? runs.find((r) => r.id === source) ?? null : null;
  const baseRun = isReplay ? replayRun : liveRun;
  const stages: Stage[] = useMemo(() => {
    if (!baseRun) return [];
    if (!isReplay) return baseRun.stages;
    return baseRun.stages.map((s) => replay.frame.stages[s.key] ?? { ...s, status: "pending", summary: "", started_at: null, finished_at: null });
  }, [baseRun, isReplay, replay.frame]);
  const runForAction = baseRun ? { ...baseRun, stages, status: isReplay && !replay.frame.ended ? "running" : baseRun.status } as Run : null;
  const reached = (key: string) => {
    const st = stages.find((s) => s.key === key);
    return !!st && !["pending", "running"].includes(st.status);
  };
  const instruction = isReplay ? replayRun?.request_text : liveRun?.request_text;
  const lastAgent = isReplay
    ? [...replay.frame.messages].reverse().find((m) => m.role === "agent")
    : [...(detail?.messages ?? [])].reverse().find((m) => m.role === "agent" && m.run_id === liveRun?.id);
  const revisionId = baseRun?.revision_id ?? null;

  return (
    <div className="fixed inset-0 z-40 flex flex-col bg-bg text-fg">
      <header className="flex flex-wrap items-center gap-3 border-b border-line px-5 py-2.5">
        <span className="text-sm font-semibold tracking-tight">CircuitLoop</span>
        <span className="text-xs text-faint">Presentation</span>
        {isReplay ? (
          <Pill tone="violet">REPLAY · recorded {fmtDateTime(replayRun?.created_at)}</Pill>
        ) : <Pill tone="ok">LIVE</Pill>}
        <div className="ml-auto flex items-center gap-2 text-xs">
          <Select value={source} onChange={setSource} className="max-w-[22rem]">
            <option value="live">Live: latest run</option>
            {runs.filter((r) => r.finished_at).slice().reverse().map((r) => (
              <option key={r.id} value={r.id}>Replay: {r.request_text.slice(0, 48)} ({runStatus(r.status).label})</option>
            ))}
          </Select>
          {isReplay && (
            <>
              <Select value={String(speed)} onChange={(v) => setSpeed(Number(v))}>
                {[1, 2, 4, 8].map((x) => <option key={x} value={x}>{x}× speed</option>)}
              </Select>
              <Button size="sm" variant="secondary" onClick={replay.restart}>Restart</Button>
            </>
          )}
          <Button size="sm" variant="ghost" onClick={() => setUi({ presentation: false })}>Exit (Esc)</Button>
        </div>
      </header>

      {!baseRun ? (
        <div className="flex flex-1 items-center justify-center text-sm text-muted">Select a project with at least one run to present it.</div>
      ) : (
        <main className="grid min-h-0 flex-1 gap-4 overflow-y-auto p-5 xl:grid-cols-[1.25fr_1fr]">
          <div className="space-y-4">
            <section className="rounded-lg border border-line bg-panel p-4">
              <div className="text-2xs uppercase tracking-wide text-faint">Instruction</div>
              <p className="mt-1 text-lg font-medium leading-snug">“{instruction}”</p>
            </section>
            {runForAction && <CurrentAction run={runForAction} showTimer={!isReplay} />}
            {lastAgent && (
              <section className="rounded-lg border border-line bg-panel p-4">
                <div className="text-2xs uppercase tracking-wide text-faint">Agent</div>
                <p className="mt-1 line-clamp-4 whitespace-pre-line text-sm text-muted">{lastAgent.text}</p>
              </section>
            )}
            <section className="grid grid-cols-2 gap-1.5 sm:grid-cols-3 lg:grid-cols-4">
              {stages.map((s) => (
                <div key={s.key} className={cx("flex items-center gap-2 rounded-md border px-2 py-1.5 text-xs",
                  s.status === "running" ? "border-accent/50 bg-accent/5" : s.status === "blocked" ? "border-warn/50" : "border-line")}>
                  <StatusIcon status={s.status} className="h-3.5 w-3.5" />
                  <span className="truncate" title={STAGE_COPY[s.key]?.title}>{STAGE_COPY[s.key]?.title.split(" →")[0] ?? s.label}</span>
                  <span className="sr-only">{stageStatus(s.status).label}</span>
                </div>
              ))}
            </section>
            {isReplay && <p className="text-2xs text-faint">Replay of persisted backend events; idle gaps longer than {GAP_CAP_S} s are shortened.</p>}
            {replay.error && <p className="text-xs text-bad">{replay.error}</p>}
            {reached("rtl") && revisionId && (
              <section className="rounded-lg border border-line bg-panel p-4">
                <div className="mb-2 text-2xs uppercase tracking-wide text-faint">Circuit</div>
                <DiagramView revisionId={revisionId} />
              </section>
            )}
          </div>
          <div className="space-y-4">
            {reached("simulate") && revisionId && <SimResult revisionId={revisionId} />}
            <section className="h-[34rem] overflow-hidden rounded-lg border border-line bg-panel">
              {reached("spec") ? <HardwareInspector /> : <div className="p-4 text-sm text-muted">The board view appears once the specification exists.</div>}
            </section>
            {reached("verify") && revisionId && <VerifyResult revisionId={revisionId} />}
          </div>
        </main>
      )}
    </div>
  );
}

function SimResult({ revisionId }: { revisionId: string }) {
  const res = useResource(revisionId, 0, (sig) => api.revision(revisionId, sig));
  const sim = res.data?.simulation;
  if (!sim) return null;
  return (
    <section className="rounded-lg border border-line bg-panel p-4">
      <div className="text-2xs uppercase tracking-wide text-faint">Simulation (Verilator)</div>
      <div className="mt-1 flex items-baseline gap-3">
        <span className={cx("text-3xl font-semibold tabular-nums", sim.failed_checks ? "text-bad" : "text-ok")}>{sim.passed_checks}</span>
        <span className="text-sm text-muted">checks passed</span>
        {sim.failed_checks > 0 && <span className="text-sm text-bad">{sim.failed_checks} failed</span>}
      </div>
      <ul className="mt-2 grid gap-0.5 text-xs sm:grid-cols-2">
        {(sim.scenarios ?? []).map((s) => (
          <li key={s.id} className="flex items-center gap-1.5"><StatusIcon status={s.passed ? "passed" : "failed"} className="h-3 w-3" />
            <span className="truncate text-muted">{s.title || s.id}</span></li>
        ))}
      </ul>
    </section>
  );
}

function VerifyResult({ revisionId }: { revisionId: string }) {
  const res = useResource(revisionId, 0, (sig) => api.revision(revisionId, sig));
  const v = res.data?.verification as { status?: string; summary?: string; programmed_by_circuitloop?: boolean } | null | undefined;
  return (
    <section className="rounded-lg border border-line bg-panel p-4">
      <div className="text-2xs uppercase tracking-wide text-faint">Physical verification (webcam)</div>
      {v?.status ? (
        <>
          <Pill tone={verdict(v.status).tone} className="mt-1">{verdict(v.status).label}</Pill>
          <p className="mt-1 text-sm text-muted">{v.summary}</p>
          {v.programmed_by_circuitloop === false && <p className="mt-1 text-xs text-warn">CircuitLoop did not program this revision; the result is not attributed to it.</p>}
        </>
      ) : <p className="mt-1 text-sm text-muted">Not physically verified yet.</p>}
    </section>
  );
}
