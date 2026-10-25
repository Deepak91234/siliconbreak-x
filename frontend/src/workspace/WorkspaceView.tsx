/**
 * Workspace: the complete CircuitLoop development experience inside the
 * SiliconBreak X shell.
 *
 *   left    AI agent conversation (ChatPanel): project creation, follow-ups, history
 *   centre  Agent timeline, Code (Verilog), Simulation (waveforms), Diagram,
 *           Vivado / Build, Camera verification, Revisions
 *   right   hardware inspector: Basys 3 twin (2D/3D), modes, operating guide
 *
 * Panels resize (drag or ←/→ on the splitter) and collapse; below 1280 px the
 * inspector becomes a slide-over drawer and below 900 px so does the chat, so
 * code and waveform views keep their width. All content comes from the store.
 */

import { useEffect, useState, type ReactNode } from "react";
import { ChatPanel } from "../components/ChatPanel";
import { ErrorBoundary } from "../components/ErrorBoundary";
import { IconBot, IconCamera, IconChip, IconCode, IconGit, IconLayers, IconPanel, IconWave } from "../components/icons";
import { Select, cx } from "../components/ui";
import { HardwareInspector } from "../hardware/HardwareInspector";
import { Splitter } from "../layout/Splitter";
import { revisionStatus } from "../lib/status";
import { INSPECTOR_DOCK_MIN_PX, setUi, toggleInspector, useUi } from "../lib/ui";
import { effectiveRevisionId, effectiveRunId, runForRevision, type TabKey } from "../store/reducer";
import { useAppState, useStore } from "../store/store";
import { HistoryTab } from "../tabs/HistoryTab";
import { SimulationTab } from "../tabs/SimulationTab";
import { VerilogTab } from "../tabs/VerilogTab";
import { VivadoTab } from "../tabs/VivadoTab";
import { VisionTab } from "../tabs/vision/VisionTab";
import { DiagramView } from "./DiagramView";
import { RevisionChanges } from "./RevisionChanges";
import { CurrentAction, Timeline } from "./Timeline";
import { Welcome } from "./Welcome";

function readPref(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function writePref(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    /* preference only */
  }
}

function usePanelWidth(key: string, initial: number, min: number, max: number): [number, (dx: number) => void] {
  const [w, setW] = useState(() => {
    const v = Number(readPref(`circuitloop.w.${key}`));
    return Number.isFinite(v) && v >= min && v <= max ? v : initial;
  });
  const resize = (dx: number) =>
    setW((cur) => {
      const next = Math.max(min, Math.min(max, cur + dx));
      writePref(`circuitloop.w.${key}`, String(next));
      return next;
    });
  return [w, resize];
}

const TABS: Array<{ key: TabKey; label: string; icon: ReactNode }> = [
  { key: "pipeline", label: "Agent", icon: <IconBot size={15} /> },
  { key: "verilog", label: "Code", icon: <IconCode size={15} /> },
  { key: "simulation", label: "Simulation", icon: <IconWave size={15} /> },
  { key: "diagram", label: "Diagram", icon: <IconLayers size={15} /> },
  { key: "vivado", label: "Vivado / Build", icon: <IconChip size={15} /> },
  { key: "vision", label: "Camera", icon: <IconCamera size={15} /> },
  { key: "history", label: "Revisions", icon: <IconGit size={15} /> },
];

function RevisionPicker() {
  const store = useStore();
  const projectId = useAppState((s) => s.selection.projectId);
  const pinned = useAppState((s) => s.selection.revisionId);
  const detail = useAppState((s) => (projectId ? s.details[projectId] : undefined));
  const effective = useAppState(effectiveRevisionId);
  if (!detail || detail.revisions.length === 0) return null;
  const revs = [...detail.revisions].sort((a, b) => b.number - a.number);
  const eff = revs.find((r) => r.id === effective);
  return (
    <label className="flex items-center gap-1.5 text-xs text-muted">
      <span className="hidden 2xl:inline">Revision</span>
      <Select value={pinned ?? ""} onChange={(v) => store.selectRevision(v || null)} className="max-w-[15rem]">
        <option value="">latest{eff && !pinned ? ` (r${eff.number})` : ""}</option>
        {revs.map((r) => (
          <option key={r.id} value={r.id}>r{r.number} · {r.kind} · {revisionStatus(r.status).label}</option>
        ))}
      </Select>
    </label>
  );
}

function CenterHeader({ maximized, onMaximize, chatDocked, onChat }: {
  maximized: boolean; onMaximize: () => void; chatDocked: boolean; onChat: () => void;
}) {
  const store = useStore();
  const tab = useAppState((s) => s.selection.tab);
  const inspectorOpen = useUi((u) => (window.innerWidth >= INSPECTOR_DOCK_MIN_PX ? u.inspectorOpen : u.inspectorDrawer));
  return (
    <div className="flex flex-wrap items-center gap-x-2 border-b border-line bg-panel/60 px-3">
      {!chatDocked && (
        <button type="button" onClick={onChat} className="order-2 rounded-lg px-2 py-1 text-xs font-medium text-muted hover:bg-raised hover:text-fg sm:order-none">Chat</button>
      )}
      <nav className="-mb-px flex min-w-0 basis-full gap-0.5 overflow-x-auto sm:min-w-[29rem] sm:basis-0 sm:flex-1" role="tablist" aria-label="Workspace views">
        {TABS.map((t) => (
          <button key={t.key} type="button" role="tab" aria-selected={tab === t.key} onClick={() => store.setTab(t.key)}
                  className={cx("flex items-center gap-1.5 whitespace-nowrap border-b-2 px-2.5 py-3 text-[13px] font-medium transition-colors duration-150",
                    tab === t.key ? "border-accent text-fg" : "border-transparent text-muted hover:text-fg")}>
            <span className={cx("hidden 2xl:inline", tab === t.key ? "text-accent" : "text-faint")}>{t.icon}</span>
            {t.label}
          </button>
        ))}
      </nav>
      <div className="order-3 py-1 sm:order-none sm:py-0"><RevisionPicker /></div>
      <button type="button" onClick={toggleInspector} aria-pressed={inspectorOpen} title="Show or hide the hardware inspector"
              className={cx("order-4 flex items-center gap-1.5 rounded-lg px-2 py-1.5 text-xs font-medium sm:order-none",
                inspectorOpen ? "bg-accent/10 text-accent" : "text-muted hover:bg-raised hover:text-fg")}>
        <IconPanel size={15} /> <span className="hidden 2xl:inline">Hardware</span>
      </button>
      <button type="button" onClick={onMaximize} className="order-4 ml-auto rounded-lg p-1.5 text-muted hover:bg-raised hover:text-fg sm:order-none sm:ml-0"
              title={maximized ? "Restore panels" : "Maximize the workspace"} aria-pressed={maximized}>
        <svg viewBox="0 0 20 20" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden>
          {maximized ? <path d="M8 3v5H3M12 17v-5h5M17 8h-5V3M3 12h5v5" strokeLinecap="round" strokeLinejoin="round" />
            : <path d="M3 8V3h5M17 12v5h-5M12 3h5v5M8 17H3v-5" strokeLinecap="round" strokeLinejoin="round" />}
        </svg>
      </button>
    </div>
  );
}

function CenterContent() {
  const tab = useAppState((s) => s.selection.tab);
  const projectId = useAppState((s) => s.selection.projectId);
  const detail = useAppState((s) => (projectId ? s.details[projectId] : undefined));
  const revisionId = useAppState(effectiveRevisionId);
  const revRun = useAppState((s) => runForRevision(s, projectId, revisionId));
  const latestRunId = useAppState(effectiveRunId);
  const latestRun = useAppState((s) => (latestRunId ? s.runs[latestRunId] ?? null : null));
  const run = revRun ?? latestRun;

  if (!projectId && tab !== "history") return <Welcome />;
  let body: ReactNode = null;
  switch (tab) {
    case "pipeline":
      body = run ? (
        <div className="mx-auto max-w-4xl space-y-4">
          <CurrentAction run={run} />
          <Timeline run={run} />
        </div>
      ) : <Welcome />;
      break;
    case "verilog": body = <VerilogTab revisionId={revisionId} detail={detail} />; break;
    case "simulation": body = <SimulationTab revisionId={revisionId} run={run} />; break;
    case "diagram": body = <DiagramView revisionId={revisionId} />; break;
    case "vivado": body = <VivadoTab revisionId={revisionId} run={run} />; break;
    case "vision": body = <VisionTab revisionId={revisionId} />; break;
    case "history": body = <div className="space-y-4"><RevisionChanges revisionId={revisionId} /><HistoryTab /></div>; break;
  }
  return <div className="min-w-0">{body}</div>;
}

function Drawer({ side, onClose, children, label }: { side: "left" | "right"; onClose: () => void; children: ReactNode; label: string }) {
  useEffect(() => {
    const on = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  }, [onClose]);
  return (
    <div className="fixed inset-0 z-30 flex bg-black/40" onClick={onClose} role="presentation">
      <aside role="dialog" aria-label={label} onClick={(e) => e.stopPropagation()}
             className={cx("h-full w-[min(26rem,92vw)] bg-panel shadow-pop", side === "right" ? "ml-auto border-l border-line" : "border-r border-line")}>
        {children}
      </aside>
    </div>
  );
}

export function WorkspaceView({ width }: { width: number }) {
  const inspectorOpen = useUi((s) => s.inspectorOpen);
  const [leftW, resizeLeft] = usePanelWidth("chat", 360, 300, 560);
  const [rightW, resizeRight] = usePanelWidth("hardware", 420, 340, 720);
  const [maximized, setMaximized] = useState(false);
  const [chatDrawer, setChatDrawer] = useState(false);
  const chatDocked = width >= 900;
  const inspectorDocked = width >= INSPECTOR_DOCK_MIN_PX;
  const inspectorDrawer = useUi((s) => s.inspectorDrawer);

  return (
    <div className="flex h-full min-h-0">
      {chatDocked && !maximized && (
        <>
          <aside className="min-h-0 flex-none bg-panel" style={{ width: leftW }} aria-label="AI agent conversation">
            <ErrorBoundary label="Chat"><ChatPanel /></ErrorBoundary>
          </aside>
          <Splitter label="Resize chat panel" onResize={resizeLeft} />
        </>
      )}
      <section className="flex min-h-0 min-w-0 flex-1 flex-col" aria-label="Engineering workspace">
        <CenterHeader maximized={maximized} onMaximize={() => setMaximized((m) => !m)} chatDocked={chatDocked && !maximized}
                      onChat={() => (chatDocked ? setMaximized(false) : setChatDrawer(true))} />
        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          <ErrorBoundary label="Workspace"><CenterContent /></ErrorBoundary>
        </div>
      </section>
      {inspectorDocked && inspectorOpen && !maximized && (
        <>
          <Splitter label="Resize hardware panel" onResize={resizeRight} invert />
          <aside className="min-h-0 flex-none border-l border-line bg-panel" style={{ width: rightW }} aria-label="Hardware inspector">
            <ErrorBoundary label="Hardware inspector"><HardwareInspector onClose={() => setUi({ inspectorOpen: false })} /></ErrorBoundary>
          </aside>
        </>
      )}
      {!inspectorDocked && inspectorDrawer && (
        <Drawer side="right" label="Hardware inspector" onClose={() => setUi({ inspectorDrawer: false })}>
          <ErrorBoundary label="Hardware inspector"><HardwareInspector onClose={() => setUi({ inspectorDrawer: false })} /></ErrorBoundary>
        </Drawer>
      )}
      {!chatDocked && chatDrawer && (
        <Drawer side="left" label="AI agent conversation" onClose={() => setChatDrawer(false)}>
          <ChatPanel />
        </Drawer>
      )}
    </div>
  );
}
