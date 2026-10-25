/**
 * SiliconBreak X shell.
 *
 *   sidebar   brand, the five sections, recent audits / designs, AI credit meters
 *   top bar   page title, live environment chips, approvals, presentation, theme
 *   main      Dashboard · Security Lab · SiliconBench · FPGA Lab · Workspace
 *
 * Workspace is the complete original CircuitLoop experience (chat, code,
 * simulation, diagram, Vivado, camera, revisions, hardware inspector). All
 * content comes from the store (backend REST + /ws/events); the shell only
 * arranges it.
 */

import { useEffect, useState } from "react";
import { AIProviderDialog } from "./components/AIProviderDialog";
import { ConnectionBanner } from "./components/ConnectionBanner";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { Toasts } from "./components/Toasts";
import { TopBar } from "./components/TopBar";
import { FpgaLab } from "./fpga/FpgaLab";
import { SECTION_INFO, sectionOf, type Section } from "./lib/sections";
import { setUi, useUi } from "./lib/ui";
import { PresentationView } from "./presentation/PresentationView";
import { Dashboard } from "./security/Dashboard";
import { SecurityLab } from "./security/SecurityLab";
import { SiliconBench } from "./security/SiliconBench";
import { Sidebar } from "./shell/Sidebar";
import { useAppState, useStore } from "./store/store";
import { WorkspaceView } from "./workspace/WorkspaceView";

type Theme = "dark" | "light";
const THEME_KEY = "siliconbreak.theme";
const SIDEBAR_KEY = "siliconbreak.sidebar";
const AUTO_COLLAPSE_PX = 1180;
const WORKSPACE_COLLAPSE_PX = 1720;

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

function useWidth(): number {
  const [w, setW] = useState(() => window.innerWidth);
  useEffect(() => {
    const on = () => setW(window.innerWidth);
    window.addEventListener("resize", on);
    return () => window.removeEventListener("resize", on);
  }, []);
  return w;
}

function PageTitle({ section }: { section: Section }) {
  const info = SECTION_INFO[section];
  return (
    <div className="leading-tight">
      <h1 className="text-[15px] font-semibold tracking-tight text-fg">{info.label}</h1>
      <p className="hidden text-[11px] text-faint sm:block">{info.blurb}</p>
    </div>
  );
}

function SectionBody({ section }: { section: Exclude<Section, "workspace"> }) {
  switch (section) {
    case "dashboard": return <Dashboard />;
    case "security": return <SecurityLab />;
    case "bench": return <SiliconBench />;
    case "fpga": return <FpgaLab />;
  }
}

export function App() {
  const store = useStore();
  const [theme, setTheme] = useState<Theme>(() => (readPref(THEME_KEY) === "light" ? "light" : "dark"));
  const width = useWidth();
  const [collapsedPref, setCollapsedPref] = useState(() => readPref(SIDEBAR_KEY) === "collapsed");
  const [narrowOpen, setNarrowOpen] = useState(false);
  const presentation = useUi((s) => s.presentation);
  const aiDialog = useUi((s) => s.aiDialog);
  const tab = useAppState((s) => s.selection.tab);
  const section = sectionOf(tab);
  useEffect(() => setNarrowOpen(false), [section]);
  // The Workspace needs its width for chat + code/waveforms + the hardware inspector.
  const auto = width < AUTO_COLLAPSE_PX || (section === "workspace" && width < WORKSPACE_COLLAPSE_PX);
  const collapsed = auto ? !narrowOpen : collapsedPref;

  useEffect(() => {
    document.documentElement.classList.toggle("dark", theme === "dark");
    writePref(THEME_KEY, theme);
  }, [theme]);
  // Recent audits (sidebar, Dashboard) and decided approvals (what is on the board), independent of the selected project.
  useEffect(() => {
    void store.loadAudits().catch(() => undefined);
    void store.loadAllApprovals();
  }, [store]);

  const toggleSidebar = () => {
    if (auto) setNarrowOpen((o) => !o);
    else setCollapsedPref((c) => { writePref(SIDEBAR_KEY, c ? "expanded" : "collapsed"); return !c; });
  };

  return (
    <div className="flex h-full bg-bg text-fg">
      <Sidebar collapsed={collapsed} onToggle={toggleSidebar} />
      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar theme={theme} onToggleTheme={() => setTheme((t) => (t === "dark" ? "light" : "dark"))} title={<PageTitle section={section} />} />
        <ConnectionBanner />
        <main className="min-h-0 flex-1">
          {section === "workspace" ? (
            <WorkspaceView width={width} />
          ) : (
            <div key={section} className="h-full overflow-y-auto">
              <div className="sbx-enter mx-auto w-full max-w-[1560px] p-4 lg:p-5">
                <ErrorBoundary label={SECTION_INFO[section].label}><SectionBody section={section} /></ErrorBoundary>
              </div>
            </div>
          )}
        </main>
      </div>
      {presentation && <ErrorBoundary label="Presentation mode"><PresentationView /></ErrorBoundary>}
      <AIProviderDialog open={aiDialog} onClose={() => setUi({ aiDialog: false })} />
      <Toasts />
    </div>
  );
}
