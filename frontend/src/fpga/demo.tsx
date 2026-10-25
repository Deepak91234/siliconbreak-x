/**
 * The security case's Basys 3 demo on the board twin. A demo step sets switches
 * and holds a button; the LEDs shown come from exactly one labelled source:
 *
 *   expected   what the security contract requires (case manifest)
 *   simulated  board-level Verilator simulation of the chosen variant
 *   observed   the latest webcam observation of that variant (if recorded)
 *
 * Sources are never mixed, and a source without data shows unknown LEDs.
 */

import { Suspense, lazy, useMemo } from "react";
import { api } from "../api/client";
import type { BoardObservation, DemoStep, FpgaInfo, FpgaVariantState, ObservedStep } from "../api/security";
import { Board2D } from "../board/Board2D";
import { buildBoardModel, emptyVisual, type BoardVisualState, type Provenance } from "../board/model";
import { Spinner, cx } from "../components/ui";
import { ThreeDBoundary, ThreeDUnavailable, webglAvailable } from "../hardware/WebGLFallback";
import { useResource } from "../lib/useResource";
import { latestObservation } from "../security/derive";

const Board3D = lazy(() => import("../board/Board3D"));

export type LedSource = "expected" | "simulated" | "observed";

export function ledBits(hex: string | null | undefined): Record<number, boolean | null> {
  const out: Record<number, boolean | null> = {};
  if (!hex) {
    for (let i = 0; i < 16; i++) out[i] = null;
    return out;
  }
  const v = parseInt(hex, 16);
  for (let i = 0; i < 16; i++) out[i] = ((v >> i) & 1) === 1;
  return out;
}

export function LedRow({ hex, size = "sm" }: { hex: string | null | undefined; size?: "sm" | "md" | "lg" }) {
  if (!hex) return <span className="font-mono text-2xs text-faint">unknown</span>;
  const v = parseInt(hex, 16);
  const dims = size === "lg" ? "h-4 w-2.5" : size === "md" ? "h-3 w-2" : "h-2 w-1.5";
  return (
    <span className="inline-flex gap-[3px]" aria-label={`LEDs ${hex}`} title={hex}>
      {Array.from({ length: 16 }, (_, k) => 15 - k).map((i) => {
        const on = ((v >> i) & 1) === 1;
        return (
          <span key={i} className={cx(dims, "rounded-sm transition-colors",
            on ? (i === 15 ? "bg-ok shadow-[0_0_8px_rgb(var(--ok)/0.8)]" : i === 14 ? "bg-bad shadow-[0_0_8px_rgb(var(--bad)/0.8)]" : "bg-warn shadow-[0_0_8px_rgb(var(--warn)/0.7)]") : "bg-line")} />
        );
      })}
    </span>
  );
}

export function howTo(step: { id: string; set: Record<string, number>; press: string | null }): string {
  if (step.id === "DARK") return "Release every button. Gives the camera an all-off reference.";
  const sw = Object.entries(step.set).map(([k, v]) => `${k} ${v ? "up" : "down"}`).join(", ");
  return `${sw}${step.press ? `, then hold ${step.press}` : ""}`;
}

/** LED value for a step from one source, or null when that source has no data. */
export function stepLeds(step: DemoStep, source: LedSource, variant: FpgaVariantState | undefined): string | null {
  if (source === "expected") return step.expect_leds;
  if (source === "simulated") return variant?.board_sim?.steps.find((s) => s.id === step.id)?.simulated ?? null;
  const obs = latestObservation(variant);
  const st = obs?.steps.find((s) => s.step_id === step.id);
  return st && st.status === "observed" ? st.observed : null;
}

export function observedStep(obs: BoardObservation | null, id: string): ObservedStep | null {
  return obs?.steps.find((s) => s.step_id === id) ?? null;
}

/** Board visual state for a demo step: switches set, button held, LEDs from one source. */
export function demoVisual(step: DemoStep | null, leds: string | null, source: LedSource): BoardVisualState {
  const v = emptyVisual();
  if (!step) return v;
  const used = new Set<string>(["btnU", "btnC"]);
  for (let i = 0; i < 16; i++) used.add(`led${i}`);
  const focus = new Set<string>();
  for (const [k, val] of Object.entries(step.set)) {
    const m = /^SW(\d+)$/.exec(k);
    if (m) {
      v.switches[Number(m[1])] = val === 1;
      used.add(`sw${m[1]}`);
      focus.add(`sw${m[1]}`);
    }
  }
  if (step.press) {
    const id = `btn${step.press.slice(3, 4).toUpperCase()}`;
    v.buttons[id] = true;
    focus.add(id);
  }
  v.used = used;
  v.focus = focus;
  v.leds = ledBits(leds);
  v.ledSource = (leds ? source : null) as Provenance | null;
  return v;
}

export function DemoBoard({ step, leds, source, view, height = "h-80" }: {
  step: DemoStep | null; leds: string | null; source: LedSource; view: "2d" | "3d"; height?: string;
}) {
  const board = useResource("board", 0, () => api.board());
  const model = useMemo(() => buildBoardModel(board.data), [board.data]);
  const visual = useMemo(() => demoVisual(step, leds, source), [step, leds, source]);
  const guide = useMemo(() => new Map(), []);
  const flat = <Board2D model={model} visual={visual} guide={guide} />;
  if (view === "2d") return flat;
  if (!webglAvailable()) return <div><ThreeDUnavailable reason="this browser could not create a WebGL context" />{flat}</div>;
  return (
    <ThreeDBoundary fallback={(e) => <div><ThreeDUnavailable reason={e.message} />{flat}</div>}>
      <div className={height}>
        <Suspense fallback={<div className="flex h-full items-center justify-center text-xs text-muted"><Spinner className="mr-2 h-4 w-4" />Loading 3D view…</div>}>
          <Board3D model={model} visual={visual} guide={guide} />
        </Suspense>
      </div>
    </ThreeDBoundary>
  );
}

export const SOURCE_LABEL: Record<LedSource, string> = {
  expected: "Contract (required)",
  simulated: "Simulated (Verilator, board top)",
  observed: "Observed (webcam)",
};

/** Demo steps of the case: from the FPGA info endpoint (case manifest). */
export function demoSteps(info: FpgaInfo | null): DemoStep[] {
  return info?.demo ?? [];
}
