/**
 * The five primary sections of the SiliconBreak X shell and how the existing
 * tab keys map onto them. Workspace keeps every original CircuitLoop view; the
 * old "hardware" (Connection) tab now opens inside the FPGA Lab.
 */

import type { TabKey } from "../store/reducer";

export type Section = "dashboard" | "security" | "bench" | "fpga" | "workspace";

export const WORKSPACE_TABS: TabKey[] = ["pipeline", "verilog", "simulation", "diagram", "vivado", "vision", "history"];

export function sectionOf(tab: TabKey): Section {
  if (tab === "dashboard") return "dashboard";
  if (tab === "security") return "security";
  if (tab === "bench") return "bench";
  if (tab === "fpga" || tab === "hardware") return "fpga";
  return "workspace";
}

export const SECTION_INFO: Record<Section, { label: string; blurb: string; tab: TabKey }> = {
  dashboard: { label: "Dashboard", blurb: "Overview of the latest results", tab: "dashboard" },
  security: { label: "Security Lab", blurb: "AI investigation & repair", tab: "security" },
  bench: { label: "SiliconBench", blurb: "Benchmark & compare", tab: "bench" },
  fpga: { label: "FPGA Lab", blurb: "Board, camera & programming", tab: "fpga" },
  workspace: { label: "Workspace", blurb: "Design, code & build", tab: "pipeline" },
};

/** Projects the security orchestrator creates for audits (not hand-made designs). */
export const isAuditProject = (title: string | null | undefined) => !!title && title.startsWith("Security audit");
