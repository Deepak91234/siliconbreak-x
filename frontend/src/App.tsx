/**
 * Dashboard shell: environment bar, chat (left), pipeline monitor and the
 * per-revision tabs (right). All content comes from the store, which is fed
 * only by the backend REST API and /ws/events (docs/api.md).
 */

import { useEffect, useState, type ReactNode } from "react";
import { ChatPanel } from "./components/ChatPanel";
import { ConnectionBanner } from "./components/ConnectionBanner";
import { PipelineMonitor, PipelineStrip } from "./components/PipelineMonitor";
import { Toasts } from "./components/Toasts";
import { TopBar } from "./components/TopBar";
import { Select, cx } from "./components/ui";
import { revisionStatus } from "./lib/status";
import { effectiveRevisionId, effectiveRunId, runForRevision, type TabKey } from "./store/reducer";
import { useAppState, useStore } from "./store/store";
import { HardwareTab } from "./tabs/HardwareTab";
import { HistoryTab } from "./tabs/HistoryTab";
import { SimulationTab } from "./tabs/SimulationTab";
import { VerilogTab } from "./tabs/VerilogTab";
import { VivadoTab } from "./tabs/VivadoTab";
import { VisionTab } from "./tabs/vision/VisionTab";

type Theme = "dark" | "light";
const THEME_KEY = "circuitloop.theme";
const PIPE_KEY = "circuitloop.pipelineCollapsed";

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

const TABS: Array<{ key: TabKey; label: string }> = [
  { key: "verilog", label: "Verilog" },
  { key: "simulation", label: "Simulation" },
  { key: "vivado", label: "Vivado" },
  { key: "hardware", label: "Hardware" },
  { key: "vision", label: "Vision" },
  { key: "history", label: "History" },
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
      Revision
      <Select value={pinned ?? ""} onChange={(v) => store.selectRevision(v || null)} className="max-w-[16rem]">
        <option value="">follow latest{eff && !pinned ? ` (r${eff.number})` : ""}</option>
        {revs.map((r) => (
          <option key={r.id} value={r.id}>
            r{r.number} · {r.kind} · {revisionStatus(r.status).label}
          </option>
        ))}
      </Select>
    </label>
  );
}

function TabBar() {
  const store = useStore();
  const tab = useAppState((s) => s.selection.tab);
  return (
    <div className="flex flex-wrap items-center gap-2 border-b border-line">
      <nav className="-mb-px flex min-w-0 flex-1 gap-1 overflow-x-auto" role="tablist" aria-label="Views">
        {TABS.map((t) => (
          <button
            key={t.key}
            type="button"
            role="tab"
            aria-selected={tab === t.key}
            onClick={() => store.setTab(t.key)}
            className={cx(
              "whitespace-nowrap border-b-2 px-3 py-2 text-sm transition-colors",
              tab === t.key ? "border-accent font-medium text-fg" : "border-transparent text-muted hover:text-fg",
            )}
          >
            {t.label}
          </button>
        ))}
      </nav>
      <div className="pb-1.5">
        <RevisionPicker />
      </div>
    </div>
  );
}

function ActiveTab() {
  const tab = useAppState((s) => s.selection.tab);
  const projectId = useAppState((s) => s.selection.projectId);
  const detail = useAppState((s) => (projectId ? s.details[projectId] : undefined));
  const revisionId = useAppState(effectiveRevisionId);
  const revRun = useAppState((s) => runForRevision(s, projectId, revisionId));
  const latestRunId = useAppState(effectiveRunId);
  const latestRun = useAppState((s) => (latestRunId ? s.runs[latestRunId] ?? null : null));
  const run = revRun ?? latestRun;

  let body: ReactNode;
  switch (tab) {
    case "verilog":
      body = <VerilogTab revisionId={revisionId} detail={detail} />;
      break;
    case "simulation":
      body = <SimulationTab revisionId={revisionId} run={run} />;
      break;
    case "vivado":
      body = <VivadoTab revisionId={revisionId} run={run} />;
      break;
    case "hardware":
      body = <HardwareTab revisionId={revisionId} run={run} />;
      break;
    case "vision":
      body = <VisionTab revisionId={revisionId} />;
      break;
    case "history":
      body = <HistoryTab />;
      break;
  }
  return <div className="min-w-0">{body}</div>;
}

function PipelineSection() {
  const projectId = useAppState((s) => s.selection.projectId);
  const detail = useAppState((s) => (projectId ? s.details[projectId] : undefined));
  const runId = useAppState(effectiveRunId);
  const hasRun = useAppState((s) => !!(runId && s.runs[runId]));
  const [collapsed, setCollapsed] = useState(() => readPref(PIPE_KEY) === "1");
  const toggle = () => {
    setCollapsed((c) => {
      writePref(PIPE_KEY, c ? "0" : "1");
      return !c;
    });
  };
  return (
    <section className="rounded-lg border border-line bg-panel">
      <header className="flex items-center justify-between gap-2 border-b border-line px-3 py-1.5">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">Pipeline</h2>
        <button type="button" onClick={toggle} className="text-xs text-muted hover:text-fg" aria-expanded={!collapsed}>
          {collapsed ? "Expand" : "Collapse"}
        </button>
      </header>
      {!collapsed ? (
        <PipelineMonitor runId={runId} detail={detail} />
      ) : hasRun ? (
        <PipelineStrip runId={runId} />
      ) : (
        <p className="px-3 py-2 text-xs text-muted">No run to show yet.</p>
      )}
    </section>
  );
}

export function App() {
  const [theme, setTheme] = useState<Theme>(() => (readPref(THEME_KEY) === "light" ? "light" : "dark"));

  useEffect(() => {
    document.documentElement.classList.toggle("dark", theme === "dark");
    writePref(THEME_KEY, theme);
  }, [theme]);

  return (
    <div className="flex h-full flex-col bg-bg text-fg">
      <TopBar theme={theme} onToggleTheme={() => setTheme((t) => (t === "dark" ? "light" : "dark"))} />
      <ConnectionBanner />
      <main className="flex min-h-0 flex-1 flex-col lg:grid lg:grid-cols-[minmax(20rem,26rem)_minmax(0,1fr)]">
        <aside className="h-[70vh] min-h-0 border-b border-line bg-panel lg:h-auto lg:border-b-0 lg:border-r">
          <ChatPanel />
        </aside>
        <section className="min-h-0 min-w-0 space-y-3 overflow-y-auto p-3">
          <PipelineSection />
          <TabBar />
          <ActiveTab />
        </section>
      </main>
      <Toasts />
    </div>
  );
}
