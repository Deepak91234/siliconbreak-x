/**
 * The four primary areas of the SiliconBreak X shell and how the existing tab
 * keys map onto them. Old links keep working: "dashboard" opens the Security
 * Lab; "vision" (design camera) and "hardware" (connection) open the FPGA Lab;
 * "diagram", "vivado" and "history" open inside the Workspace.
 */

import type { TabKey } from "../store/reducer";

export type Section = "security" | "bench" | "fpga" | "workspace";

export const WORKSPACE_TABS: TabKey[] = ["pipeline", "verilog", "simulation", "diagram", "vivado", "history"];

export function sectionOf(tab: TabKey): Section {
  if (tab === "security" || tab === "dashboard") return "security";
  if (tab === "bench") return "bench";
  if (tab === "fpga" || tab === "hardware" || tab === "vision") return "fpga";
  return "workspace";
}

export const SECTION_INFO: Record<Section, { label: string; blurb: string; tab: TabKey }> = {
  security: { label: "Security Lab", blurb: "Audit, repair and verify RTL", tab: "security" },
  bench: { label: "SiliconBench", blurb: "Benchmark security agents", tab: "bench" },
  fpga: { label: "FPGA Lab", blurb: "Program and test the Basys 3", tab: "fpga" },
  workspace: { label: "Workspace", blurb: "Build designs with the AI builder", tab: "pipeline" },
};

/** Projects the security orchestrator creates for audits (not hand-made designs). */
export const isAuditProject = (title: string | null | undefined) => !!title && title.startsWith("Security audit");
