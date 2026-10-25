/**
 * Workspace: the CircuitLoop builder in three views plus two panels.
 *
 *   header      compact project + revision selector, New design, Build, History
 *   AI Builder  the conversation (new designs, follow-ups, history, run options,
 *               approvals) beside the agent's run timeline
 *   Code        Verilog files, highlighting and diffs, with a Diagram toggle
 *   Simulation  results and the full interactive waveform viewer
 *   Build       panel: Vivado synthesis, implementation, timing, bitstream, reports, logs
 *   History     panel: revision changes, all projects and revisions, compare
 *
 * The board, programming and camera live in the FPGA Lab. Old tab keys still
 * work: "diagram" opens Code on the diagram, "vivado" opens Build, "history"
 * opens History.
 */

import { useEffect, useState, type ReactNode } from "react";
import { ChatPanel } from "../components/ChatPanel";
import { ErrorBoundary } from "../components/ErrorBoundary";
import { IconBot, IconChip, IconCode, IconGit, IconWave, IconX } from "../components/icons";
import { Button, Segmented, Select, cx } from "../components/ui";
import { revisionStatus } from "../lib/status";
import { effectiveRevisionId, effectiveRunId, runForRevision, type TabKey } from "../store/reducer";
import { useAppState, useStore } from "../store/store";
import { HistoryTab } from "../tabs/HistoryTab";
import { SimulationTab } from "../tabs/SimulationTab";
import { VerilogTab } from "../tabs/VerilogTab";
import { VivadoTab } from "../tabs/VivadoTab";
import { DiagramView } from "./DiagramView";
import { RevisionChanges } from "./RevisionChanges";
import { CurrentAction, Timeline } from "./Timeline";
import { Welcome } from "./Welcome";

type View = "builder" | "code" | "simulation";

const VIEWS: Array<{ key: View; tab: TabKey; label: string; icon: ReactNode }> = [
  { key: "builder", tab: "pipeline", label: "AI Builder", icon: <IconBot size={15} /> },
  { key: "code", tab: "verilog", label: "Code", icon: <IconCode size={15} /> },
  { key: "simulation", tab: "simulation", label: "Simulation", icon: <IconWave size={15} /> },
];

function viewOf(tab: TabKey): View | null {
  if (tab === "pipeline") return "builder";
  if (tab === "verilog" || tab === "diagram") return "code";
  if (tab === "simulation") return "simulation";
  return null; // vivado / history: a panel over the last view
}

function useRun() {
  const projectId = useAppState((s) => s.selection.projectId);
  const revisionId = useAppState(effectiveRevisionId);
  const revRun = useAppState((s) => runForRevision(s, projectId, revisionId));
  const latestRunId = useAppState(effectiveRunId);
  const latestRun = useAppState((s) => (latestRunId ? s.runs[latestRunId] ?? null : null));
  return { projectId, revisionId, run: revRun ?? latestRun };
}

function Selectors() {
  const store = useStore();
  const projects = useAppState((s) => s.projects);
  const projectsError = useAppState((s) => s.projectsError);
  const projectId = useAppState((s) => s.selection.projectId);
  const pinned = useAppState((s) => s.selection.revisionId);
  const detail = useAppState((s) => (projectId ? s.details[projectId] : undefined));
  const effective = useAppState(effectiveRevisionId);
  const revs = detail ? [...detail.revisions].sort((a, b) => b.number - a.number) : [];
  const eff = revs.find((r) => r.id === effective);
  return (
    <div className="flex flex-wrap items-center gap-2">
      <Select value={projectId ?? ""} onChange={(v) => store.selectProject(v || null)} className="max-w-[18rem]" title={projectsError ?? "Project"}>
        <option value="">{projects === null ? (projectsError ? "Projects unavailable" : "Loading projects…") : "— New design —"}</option>
        {projects?.map((p) => <option key={p.id} value={p.id}>{p.title}</option>)}
      </Select>
      {revs.length > 0 && (
        <Select value={pinned ?? ""} onChange={(v) => store.selectRevision(v || null)} className="max-w-[13rem]" title="Revision">
          <option value="">latest{eff && !pinned ? ` (r${eff.number})` : ""}</option>
          {revs.map((r) => <option key={r.id} value={r.id}>r{r.number} · {r.kind} · {revisionStatus(r.status).label}</option>)}
        </Select>
      )}
      <Button size="sm" variant={projectId ? "ghost" : "secondary"} onClick={() => { store.selectProject(null); store.setTab("pipeline"); }} title="Start a new design">
        + New design
      </Button>
    </div>
  );
}

function Panel({ title, icon, onClose, children }: { title: string; icon: ReactNode; onClose: () => void; children: ReactNode }) {
  useEffect(() => {
    const on = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  }, [onClose]);
  return (
    <div className="fixed inset-0 z-30 flex bg-black/45" onClick={onClose} role="presentation">
      <aside role="dialog" aria-label={title} onClick={(e) => e.stopPropagation()}
             className="sbx-enter ml-auto flex h-full w-[min(68rem,96vw)] flex-col border-l border-line bg-bg shadow-pop">
        <header className="flex items-center gap-2 border-b border-line px-4 py-3">
          <span className="text-accent">{icon}</span>
          <h2 className="text-[15px] font-semibold">{title}</h2>
          <button type="button" onClick={onClose} className="ml-auto rounded-lg p-1.5 text-muted hover:bg-raised hover:text-fg" aria-label={`Close ${title}`}><IconX size={16} /></button>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto p-4"><ErrorBoundary label={title}>{children}</ErrorBoundary></div>
      </aside>
    </div>
  );
}

export function WorkspaceView() {
  const store = useStore();
  const tab = useAppState((s) => s.selection.tab);
  const { projectId, revisionId, run } = useRun();
  const detail = useAppState((s) => (projectId ? s.details[projectId] : undefined));
  const [lastView, setLastView] = useState<View>(viewOf(tab) ?? "builder");
  const [codeMode, setCodeMode] = useState<"source" | "diagram">(tab === "diagram" ? "diagram" : "source");
  const current = viewOf(tab);
  useEffect(() => {
    if (current) setLastView(current);
    if (tab === "diagram") setCodeMode("diagram");
    if (tab === "verilog") setCodeMode((m) => (m === "diagram" ? m : "source"));
  }, [tab, current]);
  const view = current ?? lastView;
  const panel = tab === "vivado" ? "build" : tab === "history" ? "history" : null;
  const closePanel = () => store.setTab(VIEWS.find((v) => v.key === view)!.tab);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-line bg-panel/60 px-4 py-2">
        <Selectors />
        <nav className="flex gap-1 rounded-xl bg-sunken p-1 ring-1 ring-line" role="tablist" aria-label="Workspace views">
          {VIEWS.map((v) => (
            <button key={v.key} type="button" role="tab" aria-selected={view === v.key} onClick={() => store.setTab(v.tab)}
                    className={cx("flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[13px] font-medium transition-colors",
                      view === v.key ? "bg-panel text-fg shadow-sm ring-1 ring-line" : "text-muted hover:text-fg")}>
              <span className={view === v.key ? "text-accent" : "text-faint"}>{v.icon}</span>{v.label}
            </button>
          ))}
        </nav>
        <div className="ml-auto flex items-center gap-1.5">
          <Button size="sm" variant="secondary" disabled={!revisionId} onClick={() => store.setTab("vivado")}
                  title="Vivado synthesis, implementation, timing, bitstream and reports">
            <IconChip size={14} /> Build
          </Button>
          <Button size="sm" variant="ghost" onClick={() => store.setTab("history")} title="Revision history and comparisons">
            <IconGit size={14} /> History
          </Button>
        </div>
      </div>

      <div className="min-h-0 flex-1">
        {view === "builder" && (
          <div className="grid h-full min-h-0 lg:grid-cols-[minmax(22rem,30rem)_minmax(0,1fr)]">
            <aside className="min-h-0 border-r border-line bg-panel" aria-label="AI builder conversation">
              <ErrorBoundary label="Chat"><ChatPanel hidePicker /></ErrorBoundary>
            </aside>
            <div className="min-h-0 overflow-y-auto p-4">
              <ErrorBoundary label="Agent timeline">
                {!projectId || !run ? <Welcome /> : (
                  <div className="mx-auto max-w-4xl space-y-4">
                    <CurrentAction run={run} />
                    <Timeline run={run} />
                  </div>
                )}
              </ErrorBoundary>
            </div>
          </div>
        )}
        {view === "code" && (
          <div className="h-full overflow-y-auto p-4">
            {!projectId ? <NoProject /> : (
              <div className="space-y-3">
                <Segmented value={codeMode} onChange={(m) => { setCodeMode(m); store.setTab(m === "diagram" ? "diagram" : "verilog"); }}
                           options={[{ value: "source", label: "Source & diffs" }, { value: "diagram", label: "Diagram" }]} />
                <ErrorBoundary label="Code">
                  {codeMode === "diagram" ? <DiagramView revisionId={revisionId} /> : <VerilogTab revisionId={revisionId} detail={detail} />}
                </ErrorBoundary>
              </div>
            )}
          </div>
        )}
        {view === "simulation" && (
          <div className="h-full overflow-y-auto p-4">
            {!projectId ? <NoProject /> : <ErrorBoundary label="Simulation"><SimulationTab revisionId={revisionId} run={run} /></ErrorBoundary>}
          </div>
        )}
      </div>

      {panel === "build" && (
        <Panel title="Build (Vivado)" icon={<IconChip size={16} />} onClose={closePanel}>
          <VivadoTab revisionId={revisionId} run={run} />
        </Panel>
      )}
      {panel === "history" && (
        <Panel title="History" icon={<IconGit size={16} />} onClose={closePanel}>
          <div className="space-y-4"><RevisionChanges revisionId={revisionId} /><HistoryTab /></div>
        </Panel>
      )}
    </div>
  );
}

function NoProject() {
  const store = useStore();
  return (
    <div className="mx-auto max-w-lg py-16 text-center text-sm text-muted">
      Choose a project above, or describe a new design in the{" "}
      <button type="button" className="text-accent hover:underline" onClick={() => store.setTab("pipeline")}>AI Builder</button>.
    </div>
  );
}
