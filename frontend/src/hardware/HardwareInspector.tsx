/**
 * Right-hand hardware inspector: the Basys 3 twin (2D or 3D), three clearly
 * separated modes, and the step-by-step operating guide.
 *
 *   Preview     example spec from examples/*.json - explicitly labelled, no LED states
 *   Simulation  real Verilator data: replay a recorded test scenario, or drive the
 *               revision's compiled RTL from virtual buttons/switches
 *   Live        backend-confirmed programming status + LED states measured by the webcam
 *
 * Both board views take the same model, visual state and guide components.
 */

import { Suspense, lazy, useEffect, useMemo, useState } from "react";
import { api, errorMessage } from "../api/client";
import type { Guide, GuideComponent, GuideStep } from "../api/types";
import { Board2D } from "../board/Board2D";
import { useObserved, useReplay, useVirtualFpga } from "../board/hooks";
import { PROVENANCE_LABEL, buildBoardModel, emptyVisual, type BoardVisualState, type Provenance } from "../board/model";
import { Button, Pill, Segmented, Select, Spinner, cx } from "../components/ui";
import { hardwareState } from "../lib/status";
import { toast } from "../lib/toast";
import { highlight, setUi, useUi, type BoardMode } from "../lib/ui";
import { useResource } from "../lib/useResource";
import { effectiveRevisionId } from "../store/reducer";
import { useAppState, useStore } from "../store/store";
import { latestProgram } from "../security/derive";
import { ThreeDBoundary, ThreeDUnavailable, webglAvailable } from "./WebGLFallback";

const Board3D = lazy(() => import("../board/Board3D"));

const MODE_INFO: Record<BoardMode, { label: string; blurb: string }> = {
  preview: { label: "Preview", blurb: "Example design from the bundled examples. Highlights and steps only; no LED states are shown." },
  simulation: { label: "Simulation", blurb: "Predicted behaviour from Verilator runs of this revision's actual RTL." },
  live: { label: "Live hardware", blurb: "Programming status from the backend and LED states measured by the webcam." },
};

const ACTION_VERB: Record<string, string> = { hold: "hold", press: "press", release: "release", set_on: "on", set_off: "off", observe: "watch" };

export function HardwareInspector({ onClose }: { onClose?: () => void }) {
  const store = useStore();
  const mode = useUi((s) => s.boardMode);
  const view = useUi((s) => s.boardView);
  const ext = useUi((s) => s.highlight);
  const revisionId = useAppState(effectiveRevisionId);
  const version = useAppState((s) => (revisionId ? s.revisionVersions[revisionId] ?? 0 : 0));
  const hw = useAppState((s) => s.hardware);
  const approvals = useAppState((s) => s.approvals);

  const board = useResource("board", 0, () => api.board());
  const examples = useResource("examples", 0, () => api.examples());
  const [example, setExample] = useState("demo1_counter_spec");
  const guideRes = useResource(mode === "preview" ? `ex:${example}` : revisionId ? `rev:${revisionId}` : null, version,
    (sig) => (mode === "preview" ? api.exampleGuide(example, sig) : api.guide(revisionId as string, sig)));
  const guide: Guide | null = guideRes.data;

  const [simKind, setSimKind] = useState<"replay" | "virtual">("replay");
  const [stepId, setStepId] = useState<string | null>(null);
  const [scenario, setScenario] = useState<string | null>(null);
  const canSim = mode === "simulation" && !!guide && !guide.preview && guide.sim_status != null;
  const replay = useReplay(canSim && simKind === "replay" ? revisionId : null, canSim && simKind === "replay" ? scenario : null);
  const virtual = useVirtualFpga(revisionId, canSim && simKind === "virtual");
  const observed = useObserved(mode === "live" ? revisionId : null);

  useEffect(() => { setStepId(null); }, [revisionId, mode, example]);
  useEffect(() => {
    if (!scenario && guide?.derived_from.test_scenarios.length) setScenario(guide.derived_from.test_scenarios[0]);
  }, [guide, scenario]);

  const model = useMemo(() => buildBoardModel(board.data), [board.data]);
  const comps = useMemo(() => new Map<string, GuideComponent>((guide?.components ?? []).map((c) => [c.id, c])), [guide]);
  const step = guide?.steps.find((s) => s.id === stepId) ?? null;

  const visual: BoardVisualState = useMemo(() => {
    const v = emptyVisual();
    v.used = new Set(guide?.components.map((c) => c.id) ?? []);
    const focus = new Set<string>(ext);
    if (step) {
      step.actions.forEach((a) => focus.add(a.component));
      step.observe.forEach((o) => focus.add(o));
    }
    v.focus = focus;
    let src: Provenance | null = null;
    if (mode === "simulation" && simKind === "replay" && replay.trace) {
      v.leds = replay.leds; v.switches = replay.switches; v.buttons = replay.buttons; src = "simulated";
    } else if (mode === "simulation" && simKind === "virtual" && virtual.active) {
      v.leds = virtual.leds; v.switches = virtual.switches; v.buttons = virtual.buttons; src = "simulated";
      v.interactive = true;
    } else if (mode === "live" && observed.sessionId) {
      v.leds = observed.leds; src = "observed";
    }
    v.ledSource = src;
    return v;
  }, [guide, ext, step, mode, simKind, replay, virtual, observed]);

  const programmed = !!guide?.programmed_by_siliconbreak;
  // The revision's "programmed" status is historical; the board holds whatever SiliconBreak X wrote last.
  const hold = latestProgram(approvals);
  const onBoard = programmed && (!hold || hold.revisionId === revisionId);
  const holdElsewhere = programmed && !!hold && hold.revisionId !== revisionId;
  const hwInfo = hardwareState(hw?.state);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <header className="flex items-center gap-2 border-b border-line px-3 py-2">
        <h2 className="text-sm font-semibold">Hardware</h2>
        <Pill tone={hwInfo.tone} title={hw?.message ?? ""}>{hw ? hwInfo.label : "checking…"}</Pill>
        {mode !== "preview" && guide && !guide.preview && (
          <Pill tone={onBoard ? "ok" : holdElsewhere ? "warn" : "idle"}
                title={holdElsewhere ? `r${guide.revision_number} was programmed earlier; the last bitstream SiliconBreak X wrote is ${hold?.auditId ? `the security demo (${hold.variant}, audit ${hold.auditId})` : `another revision (${hold?.revisionId})`}`
                  : "Confirmed by the backend after an approved programming run"}>
            {onBoard ? `r${guide.revision_number} on the board` : holdElsewhere ? `r${guide.revision_number} programmed earlier` : "not programmed"}
          </Pill>
        )}
        <div className="ml-auto flex items-center gap-1">
          <Segmented value={view} options={[{ value: "2d", label: "2D" }, { value: "3d", label: "3D" }]}
                     onChange={(v) => setUi({ boardView: v })} />
          {onClose && (
            <button type="button" onClick={onClose} className="rounded px-1.5 py-0.5 text-muted hover:bg-raised hover:text-fg" aria-label="Close hardware panel">×</button>
          )}
        </div>
      </header>

      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-3">
        <div>
          <Segmented value={mode} options={(Object.keys(MODE_INFO) as BoardMode[]).map((m) => ({ value: m, label: MODE_INFO[m].label }))}
                     onChange={(m) => setUi({ boardMode: m })} />
          <p className="mt-1.5 text-xs text-muted">{MODE_INFO[mode].blurb}</p>
        </div>

        <ProvenanceBar mode={mode} source={visual.ledSource} guide={guide} />

        {(() => {
          const board2d = (
            <Board2D model={model} visual={visual} guide={comps} onPress={virtual.press} onToggle={virtual.toggle}
                     onHover={(id) => (id && comps.has(id) ? highlight([id], "board") : ext.length && highlight([], null))} />
          );
          if (view === "2d") return <div>{board2d}</div>;
          if (!webglAvailable()) return <div><ThreeDUnavailable reason="this browser could not create a WebGL context" />{board2d}</div>;
          return (
            <ThreeDBoundary fallback={(e) => <div><ThreeDUnavailable reason={e.message} />{board2d}</div>}>
              <div className="h-72">
                <Suspense fallback={<div className="flex h-full items-center justify-center text-xs text-muted"><Spinner className="mr-2 h-4 w-4" />Loading 3D view…</div>}>
                  <Board3D model={model} visual={visual} guide={comps} onPress={virtual.press} onToggle={virtual.toggle} />
                </Suspense>
              </div>
            </ThreeDBoundary>
          );
        })()}

        {mode === "preview" && (
          <div className="flex items-center gap-2 text-xs">
            <span className="text-muted">Example</span>
            <Select value={example} onChange={setExample} className="min-w-0 flex-1">
              {(examples.data ?? []).map((e) => <option key={e.name} value={e.name}>{e.title}</option>)}
            </Select>
          </div>
        )}

        {mode === "simulation" && <SimulationControls guide={guide} simKind={simKind} setSimKind={setSimKind}
                                                      scenario={scenario} setScenario={setScenario} replay={replay} virtual={virtual}
                                                      revisionId={revisionId} />}

        {mode === "live" && <LiveStatus programmed={programmed} holdElsewhere={holdElsewhere ? (hold?.auditId ? `the security demo (${hold.variant})` : "another revision") : null} guide={guide} observed={observed}
                                         onProgram={async () => {
                                           if (!revisionId) return;
                                           try { await api.program(revisionId); toast("Programming approval requested: see the chat.", "info"); }
                                           catch (e) { toast(errorMessage(e), "error"); }
                                         }}
                                         onCamera={() => store.setTab("vision")} />}

        <GuideSteps guide={guide} loading={guideRes.loading} error={guideRes.error ? errorMessage(guideRes.error) : null}
                    notFound={guideRes.notFound} hasRevision={!!revisionId || mode === "preview"} mode={mode}
                    stepId={stepId} setStepId={setStepId}
                    onReplay={(st) => { setUi({ boardMode: "simulation" }); setSimKind("replay"); setScenario(st.scenario_id); setStepId(st.id); }} />
      </div>
    </div>
  );
}

function ProvenanceBar({ mode, source, guide }: { mode: BoardMode; source: Provenance | null; guide: Guide | null }) {
  const label = source ? PROVENANCE_LABEL[source] : mode === "preview" ? PROVENANCE_LABEL.preview : "No LED data";
  const tone = source === "observed" ? "ok" : source === "simulated" ? "info" : mode === "preview" ? "violet" : "idle";
  return (
    <div className="flex flex-wrap items-center gap-2 rounded-md border border-line bg-sunken/60 px-2.5 py-1.5 text-xs">
      <span className="text-faint">LED states:</span>
      <Pill tone={tone}>{label}</Pill>
      {mode === "preview" && guide?.preview && <span className="text-faint">example "{guide.title}", not your design</span>}
      {source === "simulated" && <span className="text-faint">accelerated test clock, time-scaled to real seconds</span>}
    </div>
  );
}

function SimulationControls(props: {
  guide: Guide | null;
  simKind: "replay" | "virtual";
  setSimKind: (k: "replay" | "virtual") => void;
  scenario: string | null;
  setScenario: (s: string | null) => void;
  replay: ReturnType<typeof useReplay>;
  virtual: ReturnType<typeof useVirtualFpga>;
  revisionId: string | null;
}) {
  const { guide, simKind, setSimKind, scenario, setScenario, replay, virtual } = props;
  if (!props.revisionId) return <p className="text-xs text-muted">Select or create a design to simulate it here.</p>;
  if (!guide) return <p className="text-xs text-muted"><Spinner className="mr-1 inline h-3 w-3" />Loading this revision's simulation data…</p>;
  if (guide.sim_status == null)
    return <p className="text-xs text-muted">This revision has not been simulated yet, so there is nothing to replay.</p>;
  const scenarios = guide.derived_from.test_scenarios;
  const dur = replay.trace?.duration_s ?? 0;
  return (
    <div className="space-y-2 rounded-md border border-line p-2.5">
      <Segmented value={simKind} options={[{ value: "replay", label: "Replay a test" }, { value: "virtual", label: "Virtual FPGA" }]}
                 onChange={setSimKind} />
      {simKind === "replay" ? (
        <div className="space-y-2 text-xs">
          <Select value={scenario ?? ""} onChange={(v) => setScenario(v || null)} className="w-full">
            {scenarios.map((s) => <option key={s} value={s}>{s}</option>)}
          </Select>
          {replay.loading && <div className="text-muted"><Spinner className="mr-1 inline h-3 w-3" />loading trace…</div>}
          {replay.error && <div className="text-bad">{replay.error}</div>}
          {replay.trace && (
            <>
              <div className="text-muted">{replay.trace.title} · {replay.trace.passed ? "passed" : "failed"} in simulation</div>
              <div className="flex items-center gap-2">
                <Button size="sm" variant="secondary" onClick={replay.playing ? replay.pause : replay.play}>
                  {replay.playing ? "Pause" : "Play"}
                </Button>
                <input type="range" min={0} max={dur} step={0.01} value={replay.t} aria-label="Replay position"
                       onChange={(e) => replay.seek(Number(e.target.value))} className="min-w-0 flex-1 accent-[rgb(var(--accent))]" />
                <span className="w-20 text-right font-mono tabular-nums text-faint">{replay.t.toFixed(2)} / {dur.toFixed(1)} s</span>
              </div>
              <div className="flex items-center gap-1 text-faint">
                speed
                {[0.5, 1, 2, 4].map((x) => (
                  <button key={x} type="button" onClick={() => replay.setSpeed(x)}
                          className={cx("rounded px-1.5", replay.speed === x ? "bg-raised text-fg" : "hover:text-fg")}>{x}×</button>
                ))}
              </div>
              <InputTimeline trace={replay.trace} t={replay.t} />
            </>
          )}
        </div>
      ) : (
        <div className="space-y-1.5 text-xs">
          <p className="text-muted">Click buttons (hold to press) and switches on the board. Each change re-runs this revision's Verilator model.</p>
          <div className="flex items-center gap-2">
            <Button size="sm" variant="secondary" onClick={virtual.reset}>Reset</Button>
            <span className="font-mono tabular-nums text-faint">t = {virtual.t.toFixed(1)} s · {virtual.events} inputs</span>
            {virtual.computing && <span className="text-faint"><Spinner className="mr-1 inline h-3 w-3" />simulating</span>}
          </div>
          {virtual.error && <div className="text-bad">{virtual.error}</div>}
        </div>
      )}
    </div>
  );
}

function InputTimeline({ trace, t }: { trace: NonNullable<ReturnType<typeof useReplay>["trace"]>; t: number }) {
  if (!trace.inputs.length) return <div className="text-faint">No inputs in this scenario: the design runs on its own.</div>;
  return (
    <ul className="max-h-24 space-y-0.5 overflow-y-auto font-mono text-2xs">
      {trace.inputs.map((e, i) => (
        <li key={i} className={e.t_s <= t ? "text-fg" : "text-faint"}>
          {e.t_s.toFixed(3)} s · {e.port} = {e.port === "sw" ? `0x${e.value.toString(16)}` : e.value}
        </li>
      ))}
    </ul>
  );
}

function LiveStatus({ programmed, holdElsewhere, guide, observed, onProgram, onCamera }: {
  programmed: boolean;
  holdElsewhere: string | null;
  guide: Guide | null;
  observed: ReturnType<typeof useObserved>;
  onProgram: () => void;
  onCamera: () => void;
}) {
  const hw = useAppState((s) => s.hardware);
  const state = hw?.state;
  return (
    <div className="space-y-2 rounded-md border border-line p-2.5 text-xs">
      {state !== "connected" && state !== "unknown" ? (
        <p className="text-muted">
          {state === "no_board" ? "No Basys 3 detected. Plug it in with the USB cable; the board appears here within a few seconds." :
            state === "busy" ? "The JTAG cable is held by another program (usually xvcd); SiliconBreak X will use the Vivado hw_server path when available." :
              state === "tool_missing" ? "No programmer available: install openFPGALoader or start the Vivado container." :
                hw?.message ?? "Checking the board…"}
        </p>
      ) : null}
      {!programmed ? (
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-muted">This revision has not been programmed by SiliconBreak X.</span>
          <Button size="sm" onClick={onProgram} disabled={!guide || guide.build_status !== "passed"}
                  title={guide?.build_status !== "passed" ? "Needs a passing Vivado build" : "Requests your approval in the chat"}>
            Program FPGA…
          </Button>
        </div>
      ) : holdElsewhere ? (
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-warn">This revision was programmed earlier, but the last bitstream SiliconBreak X wrote is {holdElsewhere}.</span>
          <Button size="sm" onClick={onProgram} disabled={!guide || guide.build_status !== "passed"} title="Requests your approval in the chat">Program again…</Button>
        </div>
      ) : (
        <p className="text-ok">Programmed: the backend confirmed the bitstream load for this revision, and nothing else has been programmed since.</p>
      )}
      <div className="flex flex-wrap items-center gap-2">
        {observed.sessionId ? (
          <span className="text-muted">Webcam session {observed.status}{observed.fps ? ` · ${observed.fps.toFixed(0)} fps` : ""}
            {observed.result ? ` · verdict ${observed.result}` : ""}</span>
        ) : <span className="text-muted">No webcam session: LED states are not shown until the camera measures them.</span>}
        <Button size="sm" variant="secondary" onClick={onCamera}>Open camera verification</Button>
      </div>
    </div>
  );
}

function GuideSteps({ guide, loading, error, notFound, hasRevision, mode, stepId, setStepId, onReplay }: {
  guide: Guide | null;
  loading: boolean;
  error: string | null;
  notFound: boolean;
  hasRevision: boolean;
  mode: BoardMode;
  stepId: string | null;
  setStepId: (id: string | null) => void;
  onReplay: (s: GuideStep) => void;
}) {
  if (!hasRevision) return <p className="text-xs text-muted">The operating guide appears once the agent has a validated specification.</p>;
  if (loading && !guide) return <p className="text-xs text-muted"><Spinner className="mr-1 inline h-3 w-3" />Building the guide…</p>;
  if (error && !notFound) return <p className="text-xs text-bad">{error}</p>;
  if (!guide) return null;
  const idx = guide.steps.findIndex((s) => s.id === stepId);
  return (
    <section aria-label="Operating guide">
      <div className="mb-1.5 flex items-center justify-between">
        <h3 className="text-sm font-semibold text-fg">
          {mode === "live" ? "Guided hardware mode" : "How to operate this design"}
        </h3>
        <div className="flex gap-1">
          <Button size="sm" variant="ghost" disabled={idx <= 0} onClick={() => setStepId(guide.steps[Math.max(0, idx - 1)]?.id ?? null)}>Back</Button>
          <Button size="sm" variant="ghost" disabled={idx >= guide.steps.length - 1}
                  onClick={() => setStepId(guide.steps[idx + 1]?.id ?? guide.steps[0]?.id ?? null)}>{idx < 0 ? "Start" : "Next"}</Button>
        </div>
      </div>
      <ol className="space-y-1.5">
        {guide.steps.map((s, i) => {
          const active = s.id === stepId;
          return (
            <li key={s.id}>
              <button type="button" onClick={() => setStepId(active ? null : s.id)} aria-expanded={active}
                      className={cx("w-full rounded-md border px-2.5 py-2 text-left text-xs transition-colors duration-150",
                        active ? "border-accent/60 bg-accent/5" : "border-line hover:border-line-strong")}>
                <div className="flex items-start gap-2">
                  <span className={cx("mt-px flex h-4 w-4 flex-none items-center justify-center rounded-full text-2xs font-semibold",
                    active ? "bg-accent text-accent-fg" : "bg-raised text-muted")}>{i + 1}</span>
                  <div className="min-w-0 flex-1">
                    <div className="font-medium text-fg">{s.title}</div>
                    {s.actions.length > 0 && (
                      <div className="mt-0.5 flex flex-wrap gap-1">
                        {s.actions.map((a, k) => (
                          <span key={k} className="rounded bg-raised px-1 font-mono text-2xs text-muted">{a.label} {ACTION_VERB[a.action]}</span>
                        ))}
                      </div>
                    )}
                  </div>
                  {!guide.preview && (
                    <Pill tone={s.sim_status === "passed" ? "ok" : s.sim_status === "failed" ? "bad" : "idle"}
                          title="Result of the simulation checks behind this step">
                      {s.sim_status === "passed" ? "sim ✓" : s.sim_status === "failed" ? "sim ✗" : "not simulated"}
                    </Pill>
                  )}
                </div>
                {active && (
                  <div className="mt-2 space-y-1.5 pl-6 text-muted">
                    <p>{s.instruction}</p>
                    <p><span className="text-faint">Expected:</span> <span className="text-fg">{s.expected}</span></p>
                    <p><span className="text-faint">Confirm:</span> {s.confirm}</p>
                    <div className="flex flex-wrap items-center gap-1.5 pt-0.5">
                      {s.camera_verifiable ? <Pill tone="info">camera-checkable</Pill> : <Pill tone="idle">simulation-only check</Pill>}
                      {s.requirement_ids.map((r) => <span key={r} className="font-mono text-2xs text-faint">{r}</span>)}
                      {s.scenario_id && !guide.preview && (
                        <span role="link" tabIndex={0} className="cursor-pointer text-accent hover:underline"
                              onClick={(e) => { e.stopPropagation(); onReplay(s); }}
                              onKeyDown={(e) => { if (e.key === "Enter") { e.stopPropagation(); onReplay(s); } }}>
                          replay test "{s.scenario_id}"
                        </span>
                      )}
                    </div>
                  </div>
                )}
              </button>
            </li>
          );
        })}
      </ol>
      <p className="mt-2 text-2xs text-faint">{guide.unused_note} Steps are derived from the validated spec
        {guide.derived_from.test_scenarios.length ? ` and test scenarios (${guide.derived_from.test_scenarios.join(", ")})` : ""}.</p>
    </section>
  );
}
